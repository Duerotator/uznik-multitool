"""Warmup policy, durable deduplication and bounded execution; no real Telegram."""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from core.storage import write_json_atomic
from core.telegram_client import PyrogramAccountClient, TelethonAccountClient, ReactionChoice
from modules.warmup_engine import PersonaStore, WarmupBudgetExhausted, WarmupEngine, WarmupRateLimiter, _BudgetedClient
from modules.warmup_settings import WarmupHistory, WarmupOptions, normalize_channels


class WarmupPolicyTests(unittest.TestCase):
    def test_channels_are_normalized_deduplicated_and_not_invented(self):
        self.assertEqual(["fixture", "other"], normalize_channels("@Fixture\nhttps://t.me/fixture other"))
        for invalid in ("https://t.me/+invite", "https://t.me/fixture/1", "https://example.org/fixture", "bad!", "https://t.me/fixture?x=1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_channels(invalid)
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual({}, PersonaStore(Path(temp) / "missing.json").personas)

    def test_policy_roundtrip_and_safe_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            policy = WarmupOptions.load(path)
            self.assertEqual([], policy.channels)
            self.assertFalse(policy.reactions or policy.save_posts or policy.join_channels)
            policy.channels = ["@Fixture"]
            policy.save(path)
            self.assertEqual(["fixture"], WarmupOptions.load(path).channels)
        for kwargs in ({"posts_per_cycle": 0}, {"cycles": -1}, {"daily_budget": 1}, {"reactions": "false"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                WarmupOptions(**kwargs).validate()

    def test_rolling_daily_budget_and_actual_wait_survive_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "limits.json"
            with patch("modules.warmup_engine.time.time", return_value=10000):
                limiter = WarmupRateLimiter(2, path, max_per_day=3)
                limiter.record("a")
                limiter.record("a")
                self.assertEqual(3600, limiter.wait_seconds("a"))
            with patch("modules.warmup_engine.time.time", return_value=14000):
                limiter = WarmupRateLimiter(2, path, max_per_day=3)
                self.assertEqual(1, limiter.remaining("a"))
                limiter.record("a")
                with self.assertRaises(WarmupBudgetExhausted):
                    limiter.record("a")
                self.assertEqual(82400, limiter.wait_seconds("a"))
            with patch("modules.warmup_engine.time.time", return_value=96401):
                self.assertEqual(2, limiter.remaining("a"))

    def test_channel_blocks_and_successful_actions_are_account_scoped_and_persistent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.json"
            history = WarmupHistory(path)
            history.record("a", "https://t.me/fixture/1", "viewed")
            history.record_join("a", "fixture")
            history.block("a", "fixture", 600)
            loaded = WarmupHistory(path)
            self.assertTrue(loaded.done("a", "https://t.me/fixture/1", "viewed"))
            self.assertTrue(loaded.joined("a", "fixture"))
            self.assertFalse(loaded.available("a", "fixture"))
            self.assertTrue(loaded.available("b", "fixture"))
            self.assertFalse(loaded.done("b", "https://t.me/fixture/1", "viewed"))
            with patch("modules.warmup_settings.time.time", return_value=10**12):
                self.assertTrue(loaded.available("a", "fixture"))

    def test_assigned_persona_is_stable_and_unassigned_does_not_choose_random(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "personas.json"
            write_json_atomic(path, [{"id": "one", "channels": ["@Fixture"]}, {"id": "two", "channels": ["other"]}])
            engine = WarmupEngine.__new__(WarmupEngine)
            engine.personas, engine.options = PersonaStore(path), WarmupOptions()
            self.assertEqual(["fixture"], engine.channels_for(SimpleNamespace(persona="one")))
            self.assertEqual([], engine.channels_for(SimpleNamespace(persona=None)))
            with self.assertRaisesRegex(ValueError, "no channels"):
                engine.validate_accounts([SimpleNamespace(persona=None, enabled=True)])


class WarmupExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = SimpleNamespace(id="a", persona=None, enabled=True, phone=None)
        self.engine = WarmupEngine.__new__(WarmupEngine)
        self.engine.options = WarmupOptions(channels=["fixture"], cycles=2)
        self.engine.config = SimpleNamespace(max_concurrency=1)
        self.engine.accounts = Mock(get_account=Mock(return_value=self.account))
        self.engine.history = WarmupHistory(Path(self.temp.name) / "history.json")
        self.engine.limiter = WarmupRateLimiter()
        self.engine.scheduler = Mock(is_sleeping=Mock(return_value=False))
        self.engine.on_status = Mock()
        self.engine.log = logging.getLogger("warmup-test")
        self.engine._wait = AsyncMock()
        self.client = Mock(get_recent_chat_messages=AsyncMock(return_value=[{"id": 2}, {"id": 1}, {"id": 2}]),
                           view_post=AsyncMock(), random_reaction=AsyncMock(), send_message=AsyncMock(), join_chat=AsyncMock())
        self.context = Mock(__aenter__=AsyncMock(return_value=self.client), __aexit__=AsyncMock(return_value=False))

    def stats(self):
        return dict.fromkeys(("viewed", "reacted", "saved", "joined", "errors", "cycles", "skipped"), 0)

    async def test_read_only_and_no_repeated_views_after_restart(self):
        stats = self.stats()
        await self.engine._simulate_browser(self.client, self.account, "fixture", stats)
        self.assertEqual(2, stats["viewed"])
        self.client.random_reaction.assert_not_awaited()
        self.client.send_message.assert_not_awaited()
        self.client.join_chat.assert_not_awaited()
        self.engine.history = WarmupHistory(Path(self.temp.name) / "history.json")
        await self.engine._simulate_browser(self.client, self.account, "fixture", stats)
        self.assertEqual(2, self.client.view_post.await_count)

    async def test_opt_in_actions_are_success_recorded_and_not_repeated(self):
        self.engine.options.reactions = self.engine.options.save_posts = self.engine.options.join_channels = True
        for _ in range(3):
            await self.engine._simulate_browser(self.client, self.account, "fixture", self.stats())
        self.client.join_chat.assert_awaited_once_with("fixture")
        self.assertEqual(2, self.client.random_reaction.await_count)
        self.assertEqual(2, self.client.send_message.await_count)
        self.assertEqual(2, self.client.view_post.await_count)
        self.assertTrue(all(call.kwargs == {"mark_viewed": False} for call in self.client.random_reaction.await_args_list))

    async def test_failed_view_is_not_remembered_as_success(self):
        self.client.view_post.side_effect = RuntimeError("temporary failure")
        stats = self.stats()
        await self.engine._simulate_browser(self.client, self.account, "fixture", stats)
        self.assertEqual(0, stats["viewed"])
        self.assertEqual(2, stats["errors"])
        self.assertFalse(self.engine.history.done("a", "https://t.me/fixture/2", "viewed"))

    async def test_finite_run_completes_and_reports_terminal_for_each_account(self):
        with patch("modules.warmup_engine.create_client", return_value=self.context):
            result = await asyncio.wait_for(self.engine.run_for_group("inbox", asyncio.Event(), accounts=[self.account]), 1)
        self.assertEqual(2, result["a"]["cycles"])
        self.assertEqual(2, result["a"]["viewed"])
        self.assertEqual(2, self.context.__aexit__.await_count)
        terminal = self.engine.on_status.call_args.args[0]
        self.assertTrue(terminal["terminal"])
        self.assertEqual("Finished", terminal["status"])

    async def test_long_cooldown_returns_deferred_without_connection(self):
        self.engine.limiter.cooldown("a", 86400)
        with patch("modules.warmup_engine.create_client") as create:
            stats = await self.engine.run_session(self.account, asyncio.Event())
        create.assert_not_called()
        self.assertGreaterEqual(stats["skipped"], 1)
        self.assertEqual(0, stats["cycles"])

    async def test_unavailable_channel_is_not_requested_on_next_run(self):
        self.client.get_recent_chat_messages.side_effect = RuntimeError("USERNAME_NOT_OCCUPIED")
        with patch("modules.warmup_engine.create_client", return_value=self.context):
            first = await self.engine.run_session(self.account, asyncio.Event())
            second = await self.engine.run_session(self.account, asyncio.Event())
        self.client.get_recent_chat_messages.assert_awaited_once()
        self.assertEqual(1, first["errors"])
        self.assertEqual(0, second["errors"])

    async def test_hung_request_times_out_and_uses_one_budget_attempt(self):
        async def hang(*args):
            await asyncio.Event().wait()
        client = _BudgetedClient(Mock(view_post=hang), self.engine.limiter, "a", asyncio.Event())
        with patch("modules.warmup_engine.REQUEST_TIMEOUT", 0.01), self.assertRaises(TimeoutError):
            await client.view_post("fixture")
        self.assertEqual(1, len(self.engine.limiter.accounts["a"]))

    async def test_three_transient_failures_pause_account_instead_of_looping(self):
        self.engine.options.cycles = 0
        self.client.get_recent_chat_messages.side_effect = TimeoutError("offline fixture")
        with patch("modules.warmup_engine.create_client", return_value=self.context):
            stats = await self.engine.run_session(self.account, asyncio.Event())
        self.assertEqual(3, stats["errors"])
        self.assertEqual(3, self.client.get_recent_chat_messages.await_count)
        self.assertEqual(0, self.engine.limiter.remaining("a"))

    async def test_continuous_progress_does_not_reset_at_rotation(self):
        self.engine.options.cycles = 0
        stop = asyncio.Event()
        sessions = []
        async def run(*args, **kwargs):
            sessions.append(True)
            stats = self.stats()
            stats["viewed"] = 2
            self.engine._report("a", "Reading", stats)
            if len(sessions) == 2:
                stop.set()
            return stats
        self.engine.run_session = run
        result = await self.engine.run_for_group("inbox", stop, accounts=[self.account])
        reading = [call.args[0]["stats"]["viewed"] for call in self.engine.on_status.call_args_list if call.args[0]["status"] == "Reading"]
        self.assertEqual([2, 4], reading)
        self.assertEqual(4, result["a"]["viewed"])

    async def test_optional_history_includes_uncaptioned_media_in_both_backends(self):
        async def messages(*args, **kwargs):
            yield SimpleNamespace(id=1, text="", message="", caption=None, media=object())
            yield SimpleNamespace(id=2, text="", message="", caption=None, media=None)
        for cls in (PyrogramAccountClient, TelethonAccountClient):
            client = cls.__new__(cls)
            client.client = Mock(get_chat_history=messages, iter_messages=messages)
            self.assertEqual([], await client.get_recent_chat_messages("fixture"))
            rows = await client.get_recent_chat_messages("fixture", include_media=True)
            self.assertEqual([1], [row["id"] for row in rows])

    async def test_reaction_can_avoid_recounting_view_in_both_backends(self):
        for cls in (PyrogramAccountClient, TelethonAccountClient):
            client = cls.__new__(cls)
            client.log = logging.getLogger("fixture")
            client.client = Mock(get_input_entity=AsyncMock(return_value="fixture-peer"))
            client.view_post = AsyncMock()
            client._allowed_reactions = AsyncMock(return_value=[ReactionChoice.emoji("👍")])
            client._send_reaction = AsyncMock()
            client._send_reaction_to_peer = AsyncMock()
            await client.random_reaction("https://t.me/fixture/1", mark_viewed=False)
            client.view_post.assert_not_awaited()
            await client.random_reaction("https://t.me/fixture/1")
            client.view_post.assert_awaited_once()

    async def test_repeated_failed_views_also_trigger_failed_cycle_guard(self):
        self.engine.options.cycles = 0
        self.client.view_post.side_effect = RuntimeError("temporary view failure")
        with patch("modules.warmup_engine.create_client", return_value=self.context):
            stats = await self.engine.run_session(self.account, asyncio.Event())
        self.assertEqual(3, stats["cycles"])
        self.assertEqual(6, stats["errors"])
        self.assertEqual(0, self.engine.limiter.remaining("a"))


if __name__ == "__main__":
    unittest.main()
