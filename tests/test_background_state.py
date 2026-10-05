"""Offline regressions for schedules, scopes, budgets and desktop task state."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from core.models import AccountRecord
from core.task_runner import TaskRunner
from modules.account_filters import AccountFilter, effective_proxy, filter_accounts, login_mail_state
from modules.online_mode import OnlineModeService
from modules.sleep_scheduler import SleepScheduler
from modules.warmup_engine import WarmupBudgetExhausted, WarmupEngine, WarmupRateLimiter, _BudgetedClient
from modules.warmup_settings import WarmupHistory, WarmupOptions
from ui.qt_app import QtDesktopApp


def account(key="a", **kwargs):
    return AccountRecord(id=key, label=key, backend="pyrogram", session_kind="session_file", session_ref=f"fixture-{key}.session", **kwargs)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "sleep.json"
        self.scheduler = SleepScheduler(str(self.path))

    def test_unassigned_is_unknown_and_blocks_background(self):
        self.assertEqual("unknown", self.scheduler.sleep_status("a"))
        self.assertTrue(self.scheduler.is_sleeping("a"))
        self.assertFalse(self.path.exists())

    def test_assignments_are_deterministic_not_guessed_from_phone(self):
        self.assertEqual("UTC", self.scheduler.assign_timezone("a", "+10000000000"))
        self.assertEqual("UTC", self.scheduler.assign_timezone("b", None))

    def test_overnight_and_daytime_boundaries(self):
        for start, end, sleeping, awake in ((23, 7, (23, 0, 6), (7, 22)), (1, 7, (1, 6), (0, 7))):
            self.scheduler.configure(["a"], timezone="UTC", start=start, end=end, enabled=True)
            for hour in sleeping + awake:
                now = datetime(2026, 10, 5, hour, tzinfo=timezone.utc)
                self.assertEqual("sleeping" if hour in sleeping else "awake", self.scheduler.state("a", now=now))

    def test_legacy_file_and_other_instance_refresh(self):
        self.path.write_text('{"a":"Europe/Moscow"}', encoding="utf-8")
        other = SleepScheduler(str(self.path))
        self.assertEqual("Europe/Moscow", other.get_timezone("a"))
        self.scheduler.configure(["b"], timezone="UTC", start=23, end=7, enabled=False)
        self.assertEqual("Europe/Moscow", other.get_timezone("a"))
        self.assertEqual("UTC", other.get_timezone("b"))
        self.assertEqual("awake", other.state("unassigned"))

    def test_missing_timezone_data_never_looks_awake(self):
        self.scheduler._zones["a"] = "invalid/fixture-zone"
        with self.assertLogs("sleep-scheduler", level="ERROR"):
            self.assertEqual("unknown", self.scheduler.state("a"))
        self.assertTrue(self.scheduler.is_sleeping("a"))

    def test_invalid_hours_do_not_write(self):
        with self.assertRaises(ValueError):
            self.scheduler.configure(["a"], timezone="UTC", start=7, end=7, enabled=True)
        self.assertFalse(self.path.exists())

    def test_wake_time_across_midnight(self):
        self.scheduler.configure(["a"], timezone="UTC", start=23, end=7, enabled=True)
        with patch("modules.sleep_scheduler.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 5, 23, tzinfo=timezone.utc)
            self.assertEqual(8 * 3600, self.scheduler.wake_at("a"))


class FilterStateTests(unittest.TestCase):
    def test_email_and_proxy_match_table(self):
        item = account(metadata={"login_email": "fixture@example.org", "proxy": "socks5://localhost:1234"})
        self.assertEqual("custom", login_mail_state(item))
        self.assertEqual([item], filter_accounts([item], AccountFilter(login_mail="custom", proxy="with")))
        self.assertEqual("unknown", login_mail_state(account()))
        self.assertEqual([item], filter_accounts([item], AccountFilter(proxy="with"), global_proxy="socks5://localhost:4321"))
        self.assertEqual("socks5://localhost:4321", effective_proxy(account(), "socks5://localhost:4321"))

    def test_unknown_schedule_is_not_in_awake_filter(self):
        items = [account("awake"), account("sleep"), account("unknown")]
        states = {"awake": "awake", "sleep": "sleeping", "unknown": "unknown"}
        for state, expected in (("awake", "awake"), ("sleeping", "sleep"), ("unknown", "unknown")):
            self.assertEqual([expected], [a.id for a in filter_accounts(items, AccountFilter(sleep=state), sleep_states=states)])


class DesktopStateTests(unittest.TestCase):
    def window(self):
        window = Mock()
        window.current_task_id, window.current_task_name = "new", "check-sessions"
        window._task_start_pending = False
        return window

    def test_old_completion_does_not_clear_new_state(self):
        window = self.window()
        QtDesktopApp.clear_task_state(window, "old")
        self.assertEqual("new", window.current_task_id)
        window.clear_progress.assert_not_called()
        QtDesktopApp.clear_task_state(window, "new")
        self.assertEqual("", window.current_task_id)
        window.clear_progress.assert_called_once()

    def test_transition_completion_preserves_next_progress(self):
        window = self.window()
        window._task_start_pending = True
        QtDesktopApp.clear_task_state(window, "new")
        window.clear_progress.assert_not_called()

    def test_second_click_cannot_start_or_replace_progress(self):
        window = self.window()
        saved = window.current_progress
        QtDesktopApp.begin_operation_progress(window, "other", [account()])
        self.assertIs(saved, window.current_progress)
        QtDesktopApp.start_managed_task(window, "other", AsyncMock())
        window.worker.submit.assert_not_called()

    def test_queued_old_progress_is_ignored(self):
        window = self.window()
        QtDesktopApp.on_progress_updated(window, {"progress_token": id(object()), "total": 10})
        window.progress_bar.setRange.assert_not_called()

    def test_rendering_sleep_status_does_not_assign_timezone(self):
        window = self.window()
        QtDesktopApp.sleep_status(window, account())
        window.scheduler.assign_timezone.assert_not_called()

    def test_close_preserves_diagnostic_errors(self):
        window = self.window()
        window.table_model.columnCount.return_value = 0
        window.vpn_gateway_future = None
        done = Future()
        done.set_result(None)
        def submit(coroutine):
            coroutine.close()
            return done
        window.direct_access.close_all = AsyncMock()
        window.tasks.stop_all = AsyncMock()
        window.worker.submit.side_effect = submit
        QtDesktopApp.closeEvent(window, Mock())
        window.accounts.clear_all_errors.assert_not_called()

    def test_scenario_never_expands_saved_ids(self):
        window = self.window()
        window.accounts.list_accounts.return_value = [account("kept"), account("new")]
        window.account_filter = AccountFilter()
        window.config.global_proxy = None
        params = {"group": "inbox", "account_ids": ["kept"], "account_filter": {}}
        self.assertEqual(["kept"], [a.id for a in QtDesktopApp.scenario_accounts(window, params)])
        params["account_ids"] = []
        self.assertEqual([], QtDesktopApp.scenario_accounts(window, params))


class BackgroundAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_runner_enforces_single_task_before_factory_runs(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = TaskRunner(SimpleNamespace(tasks_file=Path(temp) / "tasks.json"))
            started = asyncio.Event()
            async def work(stop):
                started.set()
                await stop.wait()
            task_id = runner.start("first", work)
            await started.wait()
            rejected = AsyncMock()
            with self.assertRaisesRegex(RuntimeError, "already running"):
                runner.start("second", rejected)
            rejected.assert_not_awaited()
            await runner.stop(task_id)
            next_id = runner.start("next", AsyncMock(return_value=1))
            self.assertEqual(1, await runner.tasks[next_id])

    async def test_online_waves_have_no_duplicate_accounts(self):
        service = OnlineModeService.__new__(OnlineModeService)
        service.config = SimpleNamespace(max_concurrency=5, max_action_delay=0, min_action_delay=0)
        service.scheduler = Mock(is_sleeping=Mock(return_value=False))
        service.log = logging.getLogger("online-test")
        stop, seen = asyncio.Event(), []
        async def run(item, *args):
            seen.append(item.id)
            if len(seen) == 3:
                stop.set()
        service._run_wave_account = run
        await service.run([account("a"), account("b"), account("c")], stop)
        self.assertEqual(["a", "b", "c"], seen)

    async def test_warmup_filtered_scope_does_not_reload_whole_group(self):
        engine = WarmupEngine.__new__(WarmupEngine)
        engine.config = SimpleNamespace(max_concurrency=1)
        engine.options, engine.on_status = WarmupOptions(channels=["fixture"]), None
        engine.accounts, engine.log = Mock(), logging.getLogger("warmup-test")
        selected, stop = account("selected"), asyncio.Event()
        engine.accounts.get_account.return_value = selected
        async def run(*args, **kwargs):
            stop.set()
            return {"viewed": 2}
        engine.run_session = AsyncMock(side_effect=run)
        result = await engine.run_for_group("inbox", stop, accounts=[selected])
        engine.accounts.list_accounts.assert_not_called()
        self.assertEqual({"selected"}, set(result))
        self.assertEqual(2, result["selected"]["viewed"])

    async def test_disabled_warmup_worker_exits_without_connecting(self):
        engine = WarmupEngine.__new__(WarmupEngine)
        engine.config = SimpleNamespace(max_concurrency=1)
        engine.options, engine.on_status = WarmupOptions(channels=["fixture"]), None
        engine.accounts, engine.log = Mock(), logging.getLogger("warmup-test")
        engine.accounts.get_account.return_value = account(enabled=False)
        engine.run_session = AsyncMock()
        await asyncio.wait_for(engine.run_for_group("inbox", asyncio.Event(), accounts=[account()]), 1)
        engine.run_session.assert_not_awaited()

    async def test_request_budget_stops_before_another_action(self):
        native = Mock(get_recent_chat_messages=AsyncMock(), view_post=AsyncMock())
        limiter = WarmupRateLimiter(max_per_hour=2)
        client = _BudgetedClient(native, limiter, "a", asyncio.Event())
        await client.get_recent_chat_messages("fixture")
        await client.view_post("fixture")
        with self.assertRaises(WarmupBudgetExhausted):
            await client.view_post("fixture")
        native.view_post.assert_awaited_once()

    async def test_flood_in_view_is_not_swallowed_and_no_later_actions_run(self):
        from pyrogram.errors import FloodWait
        engine = WarmupEngine.__new__(WarmupEngine)
        engine.log = logging.getLogger("warmup-test")
        engine.options, engine.history = WarmupOptions(channels=["fixture"]), WarmupHistory()
        engine.on_status = None
        engine._recent_post_links = AsyncMock(return_value=["https://t.me/fixture/1", "https://t.me/fixture/2"])
        native = Mock(view_post=AsyncMock(side_effect=FloodWait(86400)), random_reaction=AsyncMock(), send_message=AsyncMock())
        stats = {"viewed": 0, "reacted": 0, "saved": 0, "joined": 0, "errors": 0}
        with self.assertRaises(FloodWait):
            await engine._simulate_browser(native, account(), "fixture", stats)
        native.view_post.assert_awaited_once()
        native.random_reaction.assert_not_awaited()
        native.send_message.assert_not_awaited()


class BudgetPersistenceTests(unittest.TestCase):
    def test_hour_budget_and_cooldown_survive_restart(self):
        with tempfile.TemporaryDirectory() as temp, patch("modules.warmup_engine.time.time", return_value=10000):
            path = Path(temp) / "limits.json"
            limiter = WarmupRateLimiter(2, path)
            limiter.record("a")
            limiter.record("a")
            limiter.cooldown("b", 86400)
            loaded = WarmupRateLimiter(2, path)
            self.assertEqual(0, loaded.remaining("a"))
            self.assertEqual(0, loaded.remaining("b"))
            with self.assertRaises(WarmupBudgetExhausted):
                loaded.record("a")
        with patch("modules.warmup_engine.time.time", return_value=10000 + 86401):
            self.assertEqual(2, loaded.remaining("a"))
            self.assertEqual(2, loaded.remaining("b"))


class WindowsIdentityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Shell required")
    def test_shortcut_property_is_written_and_verified_on_a_temporary_link(self):
        from core.desktop_identity import APP_ID, set_shortcut_app_id
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fixture.lnk"
            escaped = str(path).replace("'", "''")
            code = f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{escaped}'); $s.TargetPath='C:\\Windows\\notepad.exe'; $s.Save()"
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", code], check=True, capture_output=True, timeout=20)
            set_shortcut_app_id(str(path))
            self.assertIn(APP_ID.encode("utf-16-le"), path.read_bytes())


if __name__ == "__main__":
    unittest.main()
