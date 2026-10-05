"""Offline contracts for new authorizations and the isolated import queue."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.config import AppConfig
from core.session_manager import SessionManager
from modules.auth_controller import AuthManager, AuthSession
from modules.session_onboarding import ForeignSessionOnboarding, pending_external_sessions, queue_external_sessions

PHONE = "10000000000"
FINGERPRINT = {
    "device_model": "Test Device",
    "system_version": "Test OS",
    "app_version": "1.0",
    "lang_code": "en",
}


def configuration(root: Path, *, api: bool = False, proxy: str = "") -> AppConfig:
    with patch.dict(os.environ, {
        "TELEGRAM_DATA_DIR": str(root / "data"),
        "TELEGRAM_IMPORT_DIR": str(root / "imports"),
        "TELEGRAM_API_ID": "123" if api else "",
        "TELEGRAM_API_HASH": "fixture" if api else "",
        "TELEGRAM_GLOBAL_PROXY": proxy,
    }, clear=True):
        return AppConfig.load(root / "no.env")


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration(self.root)

    def test_copy_keeps_original_and_does_not_queue_json(self):
        supplier = self.root / "supplier"
        supplier.mkdir()
        source = supplier / f"{PHONE}.session"
        source.write_bytes(b"fixture session")
        (supplier / "session.json").write_text("{}", encoding="utf-8")
        queued = queue_external_sessions(self.config, [supplier])
        self.assertEqual(1, len(queued))
        self.assertEqual(source.read_bytes(), queued[0].read_bytes())
        self.assertTrue(source.exists())
        self.assertEqual(queued, queue_external_sessions(self.config, [source]))

    def test_filename_collision_preserves_both_files(self):
        first = self.root / f"{PHONE}.session"
        first.write_bytes(b"first")
        original_target = queue_external_sessions(self.config, [first])[0]
        first.write_bytes(b"second")
        second_target = queue_external_sessions(self.config, [first])[0]
        self.assertNotEqual(original_target, second_target)
        self.assertEqual(b"first", original_target.read_bytes())
        self.assertEqual(b"second", second_target.read_bytes())

    def test_queue_excludes_partial_files_and_processed_archive(self):
        queue = self.config.import_dir / "auth_input"
        source = queue / "complete.SESSION"
        source.write_bytes(b"fixture")
        os.utime(source, (time.time() - 3, time.time() - 3))
        (queue / "partial.session").touch()
        (queue / "still_copying.session").write_bytes(b"fixture")
        (queue / "processed" / "old.session").write_bytes(b"fixture")
        self.assertEqual([source], pending_external_sessions(self.config))
        queued = queue_external_sessions(self.config, [queue])
        self.assertNotIn("old.session", [path.name for path in queued])

    def test_regular_import_ignores_whole_auth_queue(self):
        queue = self.config.import_dir / "auth_input"
        source = queue / "pending.session"
        source.write_bytes(b"not a real session but a stable local fixture")
        (queue / "processed" / "old.session").write_bytes(b"old")
        ordinary = self.config.import_dir / "ordinary.json"
        ordinary.write_text('{"backend":"pyrogram","session_string":"' + "x" * 100 + '"}', encoding="utf-8")
        os.utime(ordinary, (time.time() - 3, time.time() - 3))
        manager = SessionManager(self.config)
        imported, errors = manager.import_new_from_inbox()
        self.assertEqual({}, errors)
        self.assertEqual(["ordinary"], [record.label for record in imported])
        self.assertTrue(source.exists())
        again, errors = manager.import_new_from_inbox()
        self.assertEqual([], again)
        self.assertEqual({}, errors)


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration(self.root, api=True)
        self.source = self.config.import_dir / "auth_input" / f"{PHONE}.session"
        self.source.write_bytes(b"supplied session fixture")
        self.auth = SimpleNamespace(
            send_code=AsyncMock(return_value={"ok": True, "phone_code_hash": "fixture"}),
            sign_in=AsyncMock(return_value={"ok": True, "imported": True}),
            check_password=AsyncMock(return_value={"ok": True, "imported": True}),
            close_session=AsyncMock(),
            verified_proxy_url=AsyncMock(return_value="socks5://proxy.invalid:1080"),
        )
        self.service = ForeignSessionOnboarding(self.config, self.auth)
        self.service.sessions.detect_session = Mock(return_value=("pyrogram", "session_file"))
        self.copy_dir = self.root / "temporary-copy"
        self.copy_dir.mkdir()
        self.close = AsyncMock()
        self.legacy = {
            "tmp": self.copy_dir,
            "close": self.close,
            "get_me": AsyncMock(return_value=SimpleNamespace(phone_number=PHONE)),
            "get_service_messages": AsyncMock(side_effect=[
                [{"id": 10, "text": "Old code 54321"}],
                [{"id": 11, "text": "New Telegram code 12345"}, {"id": 10, "text": "Old code 54321"}],
            ]),
        }
        self.service._open_legacy_copy = AsyncMock(return_value=self.legacy)

    async def test_fresh_code_and_successful_source_archive(self):
        result = await self.service.process_one(self.source)
        self.assertEqual("authorized", result.status)
        self.assertTrue(result.imported)
        self.auth.sign_in.assert_awaited_once_with(PHONE, "fixture", "12345")
        self.close.assert_awaited_once()
        self.auth.close_session.assert_awaited_once_with(PHONE)
        self.assertFalse(self.copy_dir.exists())
        archived = self.source.parent / "processed" / self.source.name
        self.assertEqual(b"supplied session fixture", archived.read_bytes())
        self.assertFalse(self.source.exists())
        self.assertEqual([], pending_external_sessions(self.config))

    async def test_two_factor_uses_only_user_supplied_password(self):
        self.auth.sign_in.return_value = {"ok": False, "status": "2fa_required"}
        password = AsyncMock(return_value="fixture-user-password")
        result = await self.service.process_one(self.source, password_provider=password)
        self.assertEqual("authorized", result.status)
        self.auth.check_password.assert_awaited_once_with(PHONE, "fixture-user-password")

    async def test_missing_password_preserves_source(self):
        self.auth.sign_in.return_value = {"ok": False, "status": "2fa_required"}
        result = await self.service.process_one(self.source)
        self.assertEqual("needs_2fa", result.status)
        self.assertTrue(self.source.exists())
        self.auth.check_password.assert_not_awaited()

    async def test_failed_request_keeps_source_and_closes_both_clients(self):
        self.auth.send_code.return_value = {"ok": False, "error": "fixture network failure"}
        result = await self.service.process_one(self.source)
        self.assertEqual("error", result.status)
        self.assertTrue(self.source.exists())
        self.assertFalse(self.copy_dir.exists())
        self.close.assert_awaited_once()
        self.auth.close_session.assert_awaited_once_with(PHONE)
        self.auth.sign_in.assert_not_awaited()

    async def test_cancelled_read_releases_clients_and_retains_source(self):
        self.service._read_recent_code = AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            await self.service.process_one(self.source)
        self.assertTrue(self.source.exists())
        self.close.assert_awaited_once()
        self.auth.close_session.assert_awaited_once_with(PHONE)
        self.assertFalse(self.copy_dir.exists())

    async def test_old_code_is_never_used(self):
        self.legacy["get_service_messages"] = AsyncMock(return_value=[{"id": 10, "text": "Old code 54321"}])
        code = AsyncMock(return_value=None)
        with patch("modules.session_onboarding.asyncio.sleep", new=AsyncMock()):
            result = await self.service.process_one(self.source, code_provider=code)
        self.assertEqual("needs_code", result.status)
        self.auth.sign_in.assert_not_awaited()
        self.assertTrue(self.source.exists())

    async def test_connection_failure_discards_only_temporary_copy(self):
        # Use the actual opener with a fake Telegram client.
        del self.service._open_legacy_copy
        client = SimpleNamespace(connect=AsyncMock(side_effect=RuntimeError("fixture")), disconnect=AsyncMock())
        with patch("modules.session_onboarding.tempfile.mkdtemp", return_value=str(self.copy_dir)), \
             patch("pyrogram.Client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "fixture"):
                await self.service._open_legacy_copy(self.source, "pyrogram")
        client.disconnect.assert_awaited_once()
        self.assertFalse(self.copy_dir.exists())
        self.assertTrue(self.source.exists())

    async def test_unreadable_baseline_never_accepts_a_previous_code(self):
        self.legacy["get_service_messages"] = AsyncMock(side_effect=[
            RuntimeError("baseline unavailable"), [{"id": 10, "text": "Old code 54321"}],
        ])
        result = await self.service.process_one(self.source, code_provider=AsyncMock(return_value=None))
        self.assertEqual("needs_code", result.status)
        self.auth.sign_in.assert_not_awaited()
        self.assertTrue(self.source.exists())

    async def test_legacy_copy_can_connect_without_a_proxy(self):
        self.auth.verified_proxy_url.return_value = None
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), get_me=AsyncMock())
        with patch("modules.session_onboarding.tempfile.mkdtemp", return_value=str(self.copy_dir)), patch("pyrogram.Client", return_value=client) as constructor:
            legacy = await ForeignSessionOnboarding._open_legacy_copy(self.service, self.source, "pyrogram")
        self.assertIsNone(constructor.call_args.kwargs["proxy"])
        client.connect.assert_awaited_once()
        await self.service._close_legacy(legacy)
        self.assertTrue(self.source.is_file())

    async def test_uppercase_session_suffix_opens_the_supplied_database_copy(self):
        del self.service._open_legacy_copy
        source = self.source.with_suffix(".SESSION")
        source.write_bytes(b"uppercase fixture")
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), get_me=AsyncMock())
        with patch("modules.session_onboarding.tempfile.mkdtemp", return_value=str(self.copy_dir)), \
             patch("pyrogram.Client", return_value=client):
            legacy = await self.service._open_legacy_copy(source, "pyrogram")
        copied = self.copy_dir / f"{source.stem}.session"
        self.assertEqual(b"uppercase fixture", copied.read_bytes())
        await self.service._close_legacy(legacy)
        client.disconnect.assert_awaited_once()


class AuthManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration(self.root, api=True, proxy="socks5://proxy.invalid:1080")
        self.auth = AuthManager(self.config)
        self.auth.fingerprints.get_params = Mock(return_value=FINGERPRINT)

    async def test_own_global_proxy_is_validated_before_pool(self):
        with patch("modules.proxy_manager.validate_proxy", new=AsyncMock(return_value=(True, "fixture"))) as validate, \
             patch("modules.proxy_manager.ProxyPool") as pool:
            self.assertEqual(self.config.global_proxy, await self.auth.verified_proxy_url())
        pool.assert_not_called()
        validate.assert_awaited_once_with("proxy.invalid", 1080, "socks5", timeout=8.0, auth=None)

    async def test_explicit_broken_proxy_does_not_fall_back_to_direct(self):
        with patch("modules.proxy_manager.validate_proxy", new=AsyncMock(return_value=(False, "fixture"))), \
             patch("modules.proxy_manager.ProxyPool") as pool:
            pool.return_value.acquire = AsyncMock(return_value=None)
            with self.assertRaisesRegex(RuntimeError, "прокси"):
                await self.auth.verified_proxy_url()
            pool.assert_not_called()

    async def test_no_configured_proxy_and_empty_pool_allows_direct(self):
        self.auth = AuthManager(configuration(self.root, api=True))
        with patch("modules.proxy_manager.ProxyPool") as pool:
            pool.return_value.acquire = AsyncMock(return_value=None)
            self.assertIsNone(await self.auth.verified_proxy_url())

    async def test_invalid_proxy_does_not_expose_credentials(self):
        with self.assertRaises(RuntimeError) as error:
            await self.auth.verified_proxy_url("socks5://private:secret@host.invalid")
        self.assertNotIn("secret", str(error.exception))

    async def test_proxy_pool_timeout_does_not_fall_back_to_direct(self):
        self.auth = AuthManager(configuration(self.root, api=True))
        async def hang(**kwargs):
            await asyncio.Event().wait()
        with patch("modules.proxy_manager.ProxyPool") as pool, patch("modules.auth_controller.CONNECT_TIMEOUT", 0.01):
            pool.return_value.acquire = AsyncMock(side_effect=hang)
            with self.assertRaisesRegex(RuntimeError, "прокси"):
                await self.auth.verified_proxy_url()

    async def test_available_pool_proxy_is_used(self):
        self.auth = AuthManager(configuration(self.root, api=True))
        with patch("modules.proxy_manager.ProxyPool") as pool:
            pool.return_value.acquire = AsyncMock(return_value=SimpleNamespace(url="socks5://pool.invalid:1080"))
            self.assertEqual("socks5://pool.invalid:1080", await self.auth.verified_proxy_url())

    async def test_direct_send_code_passes_no_proxy_and_keeps_session(self):
        client = SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(),
            send_code=AsyncMock(return_value=SimpleNamespace(phone_code_hash="fixture", type="app", timeout=120)),
        )
        self.auth.verified_proxy_url = AsyncMock(return_value=None)
        with patch("pyrogram.Client", return_value=client) as constructor:
            result = await self.auth.send_code(PHONE)
        self.assertTrue(result["ok"])
        self.assertIsNone(constructor.call_args.kwargs["proxy"])
        self.assertIsNone(self.auth.sessions[PHONE].proxy_url)
        client.connect.assert_awaited_once()
        client.send_code.assert_awaited_once_with(PHONE)
        client.disconnect.assert_not_awaited()
        await self.auth.close_session(PHONE)

    async def test_connect_timeout_reports_local_ip_and_cleans_up(self):
        async def hang():
            await asyncio.Event().wait()
        client = SimpleNamespace(connect=AsyncMock(side_effect=hang), disconnect=AsyncMock(), send_code=AsyncMock())
        self.auth.verified_proxy_url = AsyncMock(return_value=None)
        with patch("pyrogram.Client", return_value=client), patch("modules.auth_controller.CONNECT_TIMEOUT", 0.01):
            result = await self.auth.send_code(PHONE)
        self.assertFalse(result["ok"])
        self.assertIn("локальный IP", result["error"])
        client.disconnect.assert_awaited_once()
        client.send_code.assert_not_awaited()
        self.assertFalse(self.auth.sessions)

    async def test_send_code_timeout_reports_proxy_without_credentials(self):
        async def hang(*args):
            await asyncio.Event().wait()
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), send_code=AsyncMock(side_effect=hang))
        self.auth.verified_proxy_url = AsyncMock(return_value="socks5://private:secret@proxy.invalid:1080")
        with patch("pyrogram.Client", return_value=client), patch("modules.auth_controller.REQUEST_TIMEOUT", 0.01):
            result = await self.auth.send_code(PHONE)
        self.assertIn("прокси", result["error"])
        self.assertNotIn("secret", result["error"])
        client.disconnect.assert_awaited_once()
        self.assertFalse(self.auth.sessions)

    async def test_telegram_rpc_error_is_not_reported_as_network_error(self):
        from pyrogram.errors import PhoneNumberInvalid
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), send_code=AsyncMock(side_effect=PhoneNumberInvalid()))
        self.auth.verified_proxy_url = AsyncMock(return_value=None)
        with patch("pyrogram.Client", return_value=client):
            result = await self.auth.send_code(PHONE)
        self.assertFalse(result["ok"])
        self.assertIn("PHONE_NUMBER_INVALID", result["error"])
        self.assertNotIn("локальный IP", result["error"])

    async def test_cancelled_send_code_disconnects_unregistered_memory_client(self):
        client = SimpleNamespace(
            connect=AsyncMock(),
            send_code=AsyncMock(side_effect=asyncio.CancelledError),
            disconnect=AsyncMock(),
        )
        self.auth.verified_proxy_url = AsyncMock(return_value=self.config.global_proxy)
        with patch("pyrogram.Client", return_value=client):
            with self.assertRaises(asyncio.CancelledError):
                await self.auth.send_code(PHONE)
        client.disconnect.assert_awaited_once()
        self.assertEqual({}, self.auth.sessions)

    async def test_finalize_saves_proxy_identity_and_preserves_prior_session(self):
        previous = self.config.sessions_dir / "pyrogram" / f"{PHONE}_Test_Device.json"
        previous.write_bytes(b"old authorization must not be overwritten")
        client = SimpleNamespace(export_session_string=AsyncMock(return_value="x" * 100), disconnect=AsyncMock())
        auth_session = AuthSession(PHONE, client, "hash", FINGERPRINT, self.config.global_proxy)
        self.auth.sessions[PHONE] = auth_session
        user = SimpleNamespace(id=123456, username="fixture_user", first_name="Fixture", last_name="User")
        result = await self.auth._finalize_auth(PHONE, auth_session, user)
        self.assertTrue(result["ok"])
        self.assertTrue(result["imported"])
        self.assertNotEqual(str(previous), result["session_path"])
        self.assertEqual(b"old authorization must not be overwritten", previous.read_bytes())
        [record] = self.auth.accounts.list_accounts()
        self.assertEqual(user.id, record.user_id)
        self.assertEqual(user.username, record.username)
        self.assertEqual(self.config.global_proxy, record.proxy)
        self.assertEqual(FINGERPRINT, record.metadata["fingerprint"])
        client.disconnect.assert_awaited_once()
        self.assertEqual({}, self.auth.sessions)

    async def test_failed_import_is_not_reported_as_success(self):
        client = SimpleNamespace(export_session_string=AsyncMock(return_value="x" * 100), disconnect=AsyncMock())
        auth_session = AuthSession(PHONE, client, "hash", FINGERPRINT)
        self.auth.session_mgr.import_path = Mock(side_effect=RuntimeError("fixture import error"))
        result = await self.auth._finalize_auth(PHONE, auth_session, SimpleNamespace(id=123))
        self.assertFalse(result["ok"])
        self.assertFalse(result["imported"])
        self.assertTrue(Path(result["session_path"]).is_file())

    async def test_close_session_is_idempotent_and_does_not_close_other_login(self):
        client = SimpleNamespace(disconnect=AsyncMock())
        other = SimpleNamespace(disconnect=AsyncMock())
        self.auth.sessions[PHONE] = AuthSession(PHONE, client, "hash", FINGERPRINT)
        self.auth.sessions["other"] = AuthSession("other", other, "hash", FINGERPRINT)
        await self.auth.close_session(PHONE)
        await self.auth.close_session(PHONE)
        client.disconnect.assert_awaited_once()
        other.disconnect.assert_not_awaited()


class DirectAccountClientTests(unittest.IsolatedAsyncioTestCase):
    """A newly created direct session must also work in desktop actions."""

    def setUp(self):
        from core.models import AccountRecord
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration(self.root, api=True)
        self.account = AccountRecord("fixture", "Fixture", "pyrogram", "session_file", str(self.root / "fixture.session"))

    async def test_both_backends_can_connect_directly(self):
        from core.telegram_client import PyrogramAccountClient, TelethonAccountClient
        for backend, adapter, target in (
            ("pyrogram", PyrogramAccountClient, "pyrogram.Client"),
            ("telethon", TelethonAccountClient, "telethon.TelegramClient"),
        ):
            with self.subTest(backend=backend):
                self.account.backend = backend
                native = SimpleNamespace(connect=AsyncMock(return_value=True), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True))
                client = adapter(self.config, self.account)
                client._replace_proxy = AsyncMock()
                client._proxy_usable = AsyncMock()
                with patch(target, return_value=native) as constructor, patch("modules.fingerprint_generator.FingerprintGenerator") as fingerprint:
                    fingerprint.return_value.params_for_account.return_value = FINGERPRINT
                    await client.start()
                self.assertIsNone(constructor.call_args.kwargs["proxy"])
                client._replace_proxy.assert_not_awaited()
                client._proxy_usable.assert_not_awaited()
                await client.stop()
                native.disconnect.assert_awaited_once()

    async def test_both_backends_time_out_and_release_direct_client(self):
        from core.telegram_client import PyrogramAccountClient, TelethonAccountClient
        async def hang():
            await asyncio.Event().wait()
        for backend, adapter, target in (
            ("pyrogram", PyrogramAccountClient, "pyrogram.Client"),
            ("telethon", TelethonAccountClient, "telethon.TelegramClient"),
        ):
            with self.subTest(backend=backend):
                self.account.backend = backend
                native = SimpleNamespace(connect=AsyncMock(side_effect=hang), disconnect=AsyncMock())
                client = adapter(self.config, self.account)
                client._replace_proxy = AsyncMock()
                client._startup_proxy = AsyncMock(return_value=None)
                with patch(target, return_value=native), patch("modules.fingerprint_generator.FingerprintGenerator") as fingerprint, patch("core.telegram_client.CONNECT_TIMEOUT", 0.01):
                    fingerprint.return_value.params_for_account.return_value = FINGERPRINT
                    with self.assertRaisesRegex(RuntimeError, "локальный IP"):
                        await client.start()
                self.assertIsNone(client.client)
                native.disconnect.assert_awaited_once()
                client._replace_proxy.assert_not_awaited()

    async def test_unauthorized_session_does_not_trigger_network_fallback(self):
        from core.telegram_client import PyrogramAccountClient
        native = SimpleNamespace(connect=AsyncMock(return_value=False), disconnect=AsyncMock())
        client = PyrogramAccountClient(self.config, self.account)
        with patch("pyrogram.Client", return_value=native), patch("modules.fingerprint_generator.FingerprintGenerator") as fingerprint:
            fingerprint.return_value.params_for_account.return_value = FINGERPRINT
            with self.assertRaisesRegex(RuntimeError, "not authorized"):
                await client.start()
        native.disconnect.assert_awaited_once()

    async def test_account_without_assigned_proxy_uses_available_pool(self):
        from core.telegram_client import PyrogramAccountClient
        client = PyrogramAccountClient(self.config, self.account)
        with patch("core.telegram_client._proxy_pool") as pool:
            pool.return_value.acquire = AsyncMock(return_value=SimpleNamespace(url="socks5://pool.invalid:1080"))
            proxy = await client._startup_proxy()
        self.assertEqual("pool.invalid", proxy.hostname)
        self.assertEqual("socks5://pool.invalid:1080", self.account.proxy)

    async def test_timed_out_operation_cannot_hang_forever_during_cleanup(self):
        from core.telegram_client import PyrogramAccountClient
        client = PyrogramAccountClient(self.config, self.account)
        client.start = AsyncMock()
        client.set_offline = AsyncMock()
        async def hang():
            await asyncio.Event().wait()
        client.stop = AsyncMock(side_effect=hang)
        with patch("core.telegram_client.CLEANUP_TIMEOUT", 0.01):
            with self.assertRaisesRegex(TimeoutError, "request timed out"):
                async with client:
                    raise TimeoutError("request timed out")
        self.assertFalse(client._lock.locked())


if __name__ == "__main__":
    unittest.main()
