"""No-network regression tests; all credentials/session files are temporary fixtures."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.models import AccountRecord
from core.storage import write_json_atomic
from core.telegram_client import session_lock
from core.passkey_service import PasskeyService
from modules.session_health import SessionHealthService
from modules.account_security import passkey_restore_candidates


class PasskeyRestoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = self.root / "original.session"
        self.original.write_bytes(b"original-session")
        self.account = AccountRecord(
            id="account", label="account", backend="pyrogram",
            session_kind="session_file", session_ref=str(self.original),
            user_id=123, proxy="socks5://127.0.0.1:21100", enabled=False,
        )
        write_json_atomic(self.root / "passkeys" / "account.json", {
            "account_id": "account", "user_id": 123, "dc_id": 2,
        })
        self.service = SessionHealthService.__new__(SessionHealthService)
        self.service.config = SimpleNamespace(
            data_dir=self.root, api_id=1, api_hash="fixture", global_proxy=None,
        )
        self.service.accounts = Mock()
        self.service.log = Mock()
        self.client = SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(
                id=123, username="fixture", first_name="Fixture", last_name=None,
                phone_number="10000000000",
            )),
            disconnect=AsyncMock(), export_session_string=AsyncMock(return_value="new-string"),
        )
        self.transport = SimpleNamespace(
            _proxy_usable=AsyncMock(return_value=True), _replace_proxy=AsyncMock(),
        )
        self.factory_patch = patch("modules.session_health.create_client", return_value=self.transport)
        self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.addCleanup(self.temp.cleanup)

    async def login(self, **kwargs):
        self.assertTrue(session_lock(self.account).locked())
        self.assertEqual(b"original-session", self.original.read_bytes())
        self.assertEqual(21100, kwargs["proxy"]["port"])
        Path(kwargs["save_session_dir"], "replacement.session").write_bytes(b"new-session")
        return self.client

    async def test_success_verifies_closes_and_retains_original_backup(self):
        with patch.object(PasskeyService, "login_with_passkey", side_effect=self.login):
            self.assertTrue(await self.service._try_restore_revoked_session(self.account))
        self.assertEqual(b"new-session", self.original.read_bytes())
        backup, = self.root.glob("original.session.pre-passkey-*.bak")
        self.assertEqual(b"original-session", backup.read_bytes())
        self.client.disconnect.assert_awaited_once()
        self.service.accounts.set_enabled.assert_called_once_with(["account"], True)
        self.assertFalse(session_lock(self.account).locked())

    async def test_failed_login_preserves_original(self):
        with patch.object(PasskeyService, "login_with_passkey", side_effect=RuntimeError("fixture login failed")):
            self.assertFalse(await self.service._try_restore_revoked_session(self.account))
        self.assertEqual(b"original-session", self.original.read_bytes())
        self.service.accounts.set_enabled.assert_not_called()

    async def test_wrong_identity_closes_client_and_preserves_original(self):
        self.client.get_me.return_value.id = 999
        with patch.object(PasskeyService, "login_with_passkey", side_effect=self.login):
            self.assertFalse(await self.service._try_restore_revoked_session(self.account))
        self.assertEqual(b"original-session", self.original.read_bytes())
        self.client.disconnect.assert_awaited_once()

    async def test_disconnect_failure_does_not_replace_original(self):
        self.client.disconnect.side_effect = RuntimeError("fixture disconnect failed")
        with patch.object(PasskeyService, "login_with_passkey", side_effect=self.login):
            self.assertFalse(await self.service._try_restore_revoked_session(self.account))
        self.assertEqual(b"original-session", self.original.read_bytes())

    async def test_telethon_is_rejected_without_touching_session(self):
        self.account.backend = "telethon"
        self.assertFalse(await self.service._try_restore_revoked_session(self.account))
        self.assertEqual(b"original-session", self.original.read_bytes())
        self.transport._proxy_usable.assert_not_awaited()

    async def test_replace_failure_restores_sqlite_sidecars(self):
        sidecar = Path(str(self.original) + "-wal")
        sidecar.write_bytes(b"original-wal")
        import os
        replace = os.replace

        def fail_replacement(source, target):
            if Path(source).name == "replacement.session":
                raise OSError("fixture replacement failed")
            return replace(source, target)

        with patch.object(PasskeyService, "login_with_passkey", side_effect=self.login), patch(
            "modules.session_health.os.replace", side_effect=fail_replacement,
        ):
            self.assertFalse(await self.service._try_restore_revoked_session(self.account))
        self.assertEqual(b"original-session", self.original.read_bytes())
        self.assertEqual(b"original-wal", sidecar.read_bytes())

    async def test_session_string_is_verified_before_atomic_replacement(self):
        self.account.session_kind = "session_string"
        with patch.object(PasskeyService, "login_with_passkey", side_effect=self.login):
            self.assertTrue(await self.service._try_restore_revoked_session(self.account))
        from core.storage import read_json
        self.assertEqual("new-string", read_json(self.original, {})["session_string"])
        self.client.disconnect.assert_awaited_once()

    async def test_raw_login_requires_proxy_and_cleans_up_failure(self):
        service = PasskeyService(1, "fixture", passkeys_dir=str(self.root))
        with self.assertRaises(ValueError):
            await service.login_with_passkey({"account_id": "account"})
        client = SimpleNamespace(is_connected=True, disconnect=AsyncMock())
        with patch("core.passkey_service.Client", return_value=client), patch.object(
            service, "_login_client", side_effect=RuntimeError("fixture"),
        ):
            with self.assertRaises(RuntimeError):
                await service.login_with_passkey({"account_id": "account"}, proxy={"port": 21100})
        client.disconnect.assert_awaited_once()


class RestoreScopeTests(unittest.TestCase):
    def test_filters_candidates_without_expanding_scope(self):
        from modules.account_filters import AccountFilter, filter_accounts
        accounts = [AccountRecord(
            id=name, label=name, backend="pyrogram", session_kind="session_file",
            session_ref=f"{name}.session", enabled=enabled, metadata=metadata,
        ) for name, enabled, metadata in (
            ("bad-a", False, {}), ("bad-b", True, {"health_status": "invalid"}),
            ("valid", True, {}), ("frozen", False, {"health_status": "frozen"}),
        )]
        self.assertEqual(["bad-a", "bad-b"], [a.id for a in passkey_restore_candidates(accounts)])
        selected = [a for a in accounts if a.id in {"valid", "outside-group"}]
        self.assertEqual([], passkey_restore_candidates(selected))
        filtered = filter_accounts(accounts, AccountFilter(query="bad-b"))
        self.assertEqual(["bad-b"], [a.id for a in passkey_restore_candidates(filtered)])
        self.assertEqual([], passkey_restore_candidates([]))


if __name__ == "__main__":
    unittest.main()
