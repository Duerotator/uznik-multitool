"""Offline session ownership, Warmup pauses and email contention regression tests."""
from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.telegram_client import AccountBusyError, PyrogramAccountClient, session_lock
from modules.account_security import AccountSecurityService
from modules.warmup_engine import WarmupEngine, WarmupRateLimiter
from modules.warmup_settings import WarmupHistory, WarmupOptions


class SessionCoordinationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # No storage or real session is opened; all network methods are replaced.
        self.account = SimpleNamespace(id="fixture", session_ref=f"fixture-{id(self)}.session",
                                       enabled=True, phone=None, persona=None, proxy=None)
        self.config = SimpleNamespace(email_inbox_backend="imap", max_concurrency=1,
                                      min_action_delay=0, max_action_delay=0)
        self.lock = session_lock(self.account)

    def client(self, *args, lock_wait_timeout=None):
        client = PyrogramAccountClient(self.config, self.account)
        client.lock_wait_timeout = lock_wait_timeout
        client.start, client.stop, client.set_offline = AsyncMock(), AsyncMock(), AsyncMock()
        client.set_login_email, client.set_recovery_email = AsyncMock(), AsyncMock()
        return client

    async def test_busy_wait_does_not_start_or_release_the_other_owner(self):
        await self.lock.acquire()
        try:
            client = self.client(lock_wait_timeout=0.01)
            with self.assertRaisesRegex(AccountBusyError, "Account is busy"):
                async with client:
                    self.fail("Busy session was acquired")
            self.assertTrue(self.lock.locked())
            self.assertFalse(client._lock_acquired)
            client.start.assert_not_awaited()
            client.stop.assert_not_awaited()
        finally:
            self.lock.release()

    async def test_cancelled_wait_does_not_release_the_other_owner(self):
        await self.lock.acquire()
        try:
            client = self.client(lock_wait_timeout=1)
            task = asyncio.create_task(client.__aenter__())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.lock.locked())
            client.start.assert_not_awaited()
        finally:
            self.lock.release()

    async def test_waiter_continues_after_owner_releases(self):
        await self.lock.acquire()
        client = self.client(lock_wait_timeout=1)
        async def use():
            async with client:
                self.assertTrue(client._lock_acquired)
        task = asyncio.create_task(use())
        await asyncio.sleep(0)
        self.lock.release()
        await task
        self.assertFalse(self.lock.locked())
        client.stop.assert_awaited_once()

    def engine(self):
        engine = WarmupEngine.__new__(WarmupEngine)
        engine.config, engine.accounts = self.config, Mock()
        engine.personas = Mock()
        engine.personas.get.return_value = SimpleNamespace(name="Fixture", channels=["fixture"])
        engine.scheduler = Mock(is_sleeping=Mock(return_value=False))
        engine.limiter = WarmupRateLimiter()
        engine.options, engine.history = WarmupOptions(channels=["fixture"]), WarmupHistory()
        engine.on_status = None
        engine.log = logging.getLogger("warmup-test")
        engine._simulate_browser = AsyncMock()
        return engine

    async def test_warmup_releases_session_before_browse_delay_and_cycle_pause(self):
        engine, stop = self.engine(), asyncio.Event()
        slots = asyncio.Semaphore(1)
        pauses = []
        async def wait(event, seconds):
            self.assertFalse(self.lock.locked())
            self.assertFalse(slots.locked())
            pauses.append(seconds)
            if len(pauses) == 2:
                # A foreground task can take the session during the Warmup pause.
                async with self.client(lock_wait_timeout=0.01):
                    pass
                event.set()
        async def browse(*args):
            self.assertTrue(self.lock.locked())
            self.assertTrue(slots.locked())
        engine._wait, engine._simulate_browser = wait, AsyncMock(side_effect=browse)
        with patch("modules.warmup_engine.create_client", self.client):
            stats = await engine.run_session(self.account, stop, semaphore=slots)
        self.assertEqual(2, len(pauses))
        self.assertEqual(0, stats["errors"])
        self.assertFalse(self.lock.locked())

    async def test_warmup_sleep_and_quota_waits_never_open_session(self):
        for sleeping in (True, False):
            with self.subTest(sleeping=sleeping):
                engine, stop = self.engine(), asyncio.Event()
                engine.scheduler.is_sleeping.return_value = sleeping
                engine.scheduler.wake_at.return_value = 60
                engine.limiter.max_per_hour = 0
                engine._wait = AsyncMock(side_effect=lambda *args: stop.set())
                with patch("modules.warmup_engine.create_client") as create:
                    await engine.run_session(self.account, stop)
                create.assert_not_called()

    async def test_warmup_cancellation_releases_active_session(self):
        engine = self.engine()
        engine._wait = AsyncMock()
        engine._simulate_browser.side_effect = asyncio.CancelledError
        with patch("modules.warmup_engine.create_client", self.client):
            await engine.run_session(self.account, asyncio.Event())
        self.assertFalse(self.lock.locked())

    async def test_warmup_failed_cycle_still_uses_budget_and_releases_session(self):
        engine, stop = self.engine(), asyncio.Event()
        pauses = []
        async def wait(*args):
            pauses.append(True)
            if len(pauses) == 2:
                stop.set()
        engine._wait = wait
        async def browse(client, *args):
            await client.get_recent_chat_messages("fixture")
        engine._simulate_browser.side_effect = browse
        native = self.client()
        native.get_recent_chat_messages = AsyncMock(side_effect=RuntimeError("fixture unreadable channel"))
        with patch("modules.warmup_engine.create_client", return_value=native):
            stats = await engine.run_session(self.account, stop)
        self.assertEqual(1, stats["errors"])
        self.assertEqual(1, len(engine.limiter.accounts[self.account.id]))
        self.assertFalse(self.lock.locked())

    async def test_warmup_busy_cycle_defers_without_request_or_budget_charge(self):
        engine, stop = self.engine(), asyncio.Event()
        pauses = []
        async def wait(*args):
            pauses.append(True)
            if len(pauses) == 2:
                stop.set()
        engine._wait = wait
        client = self.client(lock_wait_timeout=0.01)
        await self.lock.acquire()
        try:
            with patch("modules.warmup_engine.create_client", return_value=client):
                stats = await engine.run_session(self.account, stop)
            self.assertTrue(self.lock.locked())
            self.assertEqual(0, stats["errors"])
            self.assertEqual({}, engine.limiter.accounts)
            engine._simulate_browser.assert_not_awaited()
            client.start.assert_not_awaited()
        finally:
            self.lock.release()

    async def test_both_email_actions_report_busy_without_retries_or_health_changes(self):
        for method in ("change_login_emails", "bind_recovery_emails"):
            with self.subTest(method=method):
                service = AccountSecurityService.__new__(AccountSecurityService)
                service.config, service.accounts = self.config, Mock()
                service.log = logging.getLogger("security-test")
                inbox = Mock(healthcheck=AsyncMock(), prepare=AsyncMock())
                inbox.address_for.return_value = "fixture@example.org"
                service._email_inbox = Mock(return_value=inbox)
                progress = Mock()
                await self.lock.acquire()
                try:
                    with patch("modules.account_security.create_client", side_effect=self.client) as create, \
                         patch("modules.account_security.EMAIL_SESSION_WAIT_TIMEOUT", 0.01), \
                         patch("modules.account_security.human_delay", AsyncMock()):
                        result = await getattr(service, method)([self.account], "", "", "", progress=progress)
                    self.assertIn("Account is busy", result.details[self.account.id])
                    self.assertTrue(self.lock.locked())
                    self.assertEqual(1, create.call_count)
                    inbox.prepare.assert_awaited_once()
                    service.accounts.update_profile_metadata.assert_not_called()
                    service.accounts.mark_error.assert_not_called()
                    progress.mark_error.assert_called_once_with(self.account.id)
                finally:
                    self.lock.release()


if __name__ == "__main__":
    unittest.main()
