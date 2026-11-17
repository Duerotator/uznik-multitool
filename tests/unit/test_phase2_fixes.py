"""Offline unit regressions covering Phase 2 security, UI, warmup, raffle, and TON fixes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

for _p in (Path(__file__).resolve().parents[2] / "app", Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class LoggingRedactionTests(unittest.TestCase):
    def test_secret_redaction_filter_strips_proxy_creds_query_keys_and_tokens(self):
        from core.logging_setup import SecretRedactionFilter, ShortFormatter, redact_secrets

        raw = (
            "Proxy socks5://alice:s3cr3tPass@10.0.0.1:1080 failed for "
            "https://generativelanguage.googleapis.com/v1beta/models?key=AIzaSyntheticKey999 "
            "Bearer abcdefgh12345 user@example.org"
        )
        scrubbed = redact_secrets(raw)
        for secret in ("alice", "s3cr3tPass", "AIzaSyntheticKey999", "abcdefgh12345", "user@example.org"):
            self.assertNotIn(secret, scrubbed)
        self.assertIn("socks5://***:***@10.0.0.1:1080", scrubbed)
        self.assertIn("?key=***", scrubbed)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(SecretRedactionFilter())
        handler.setFormatter(ShortFormatter("%(levelname)s:%(name)s:%(message)s"))
        record = logging.LogRecord(
            name="modules.very_long_logger_name",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="Connect %s -> %s",
            args=("http://bob:pw@127.0.0.1:8080", "https://api.example.invalid/?token=tok_secret_123"),
            exc_info=None,
        )
        handler.emit(record)
        output = stream.getvalue()
        self.assertNotIn("bob", output)
        self.assertNotIn("pw@", output)
        self.assertNotIn("tok_secret_123", output)
        self.assertEqual("WARNING", record.levelname)
        self.assertEqual("modules.very_long_logger_name", record.name)


class PasskeyDPAPITests(unittest.TestCase):
    def test_passkey_save_encrypts_private_key_and_migrates_legacy_plaintext(self):
        from core.passkey_service import PasskeyService

        with tempfile.TemporaryDirectory() as temp:
            service = PasskeyService(1, "fixture", passkeys_dir=temp)
            _, pem = service.generate_keypair()
            record = {
                "account_id": "acc1",
                "passkey_id": "pk1",
                "cred_id_b64": "cred-id-b64",
                "user_id": 42,
                "dc_id": 2,
                "phone": "+15550001234",
                "private_key_pem": pem,
                "user_handle": "2:42",
                "rp_id": "telegram.org",
                "sign_count": 3,
            }
            path = service.save_passkey("acc1", record)
            disk_bytes = path.read_bytes()
            self.assertNotIn(b"BEGIN PRIVATE KEY", disk_bytes)
            self.assertNotIn(b"+15550001234", disk_bytes)
            envelope = json.loads(disk_bytes.decode("utf-8"))
            if sys.platform == "win32":
                self.assertEqual("uznik-passkey-v1", envelope.get("format"))
                self.assertEqual("windows-current-user-dpapi", envelope.get("protection"))
            loaded = service.load_passkey("acc1")
            self.assertEqual(pem, loaded["private_key_pem"])
            self.assertEqual("+15550001234", loaded["phone"])

            legacy_path = Path(temp) / "legacy.json"
            legacy_record = {**record, "account_id": "legacy"}
            legacy_path.write_text(json.dumps(legacy_record), encoding="utf-8")
            migrated = service.load_passkey("legacy")
            self.assertEqual(pem, migrated["private_key_pem"])
            self.assertNotIn(b"BEGIN PRIVATE KEY", legacy_path.read_bytes())

            cred = service.build_login_credential(
                challenge="fixture-challenge",
                rp_id="telegram.org",
                passkey_id="pk1",
                private_key_pem=pem,
                user_handle="2:42",
                cred_id_b64="cred-id-b64",
                sign_count=7,
            )
            self.assertEqual("cred-id-b64", cred.raw_id)
            self.assertEqual(b"\x00\x00\x00\x07", cred.response.authenticator_data[-4:])


class GiveawayAndMarketAppTests(unittest.TestCase):
    def test_raffle_common_telethon_flood_wait_and_path_ref_links(self):
        from telethon.errors import FloodWaitError
        from modules.raffle_common import extract_ref_link_and_count, flood_wait_seconds

        exc = FloodWaitError(request=None, capture=45)
        self.assertEqual(45, flood_wait_seconds(exc))

        link, count = extract_ref_link_and_count(
            "Пригласите друзей (1/3): https://t.me/Random/JoinLot?startapp=abc123&mode=1",
        )
        self.assertEqual("https://t.me/Random/JoinLot?startapp=abc123&mode=1", link)
        self.assertEqual(2, count)

    def test_captcha_support_fastgiveaway_and_re_require_after_ack(self):
        from modules.captcha_support import CaptchaSupportStore

        with tempfile.TemporaryDirectory() as temp:
            now = 1000.0
            store = CaptchaSupportStore(temp, clock=lambda: now)
            first = store.require_manual("acc1", "fastgiveaway", b"captcha-bytes", kind="image", reason="unreadable")
            self.assertEqual("pending", first["state"])
            listed = store.list(["acc1"])
            self.assertEqual(1, len(listed))
            store.acknowledge("acc1", first["id"], listed[0]["revision"], confirmed=True)
            second = store.require_manual("acc1", "fastgiveaway", b"captcha-bytes", kind="image", reason="unreadable")
            self.assertEqual("pending", second["state"])
            self.assertNotEqual(first["id"], second["id"])

    def test_direct_access_uses_account_secrets_vault(self):
        from core.models import AccountRecord
        from modules.direct_access import DirectAccessService

        service = DirectAccessService(SimpleNamespace(data_dir=Path(tempfile.gettempdir())))
        acc = AccountRecord(
            id="a1", label="a1", backend="pyrogram", session_kind="session_file",
            session_ref="a1.session", metadata={"vault_2fa_managed": True},
        )
        with patch("modules.direct_access.get_account_password", return_value="vault-2fa-secret"), \
             patch.dict(os.environ, {}, clear=True):
            self.assertEqual("vault-2fa-secret", service._resolve_two_factor_password(acc))


class UiProgressAndFrozenTests(unittest.TestCase):
    def test_operation_progress_indeterminate_honors_marks_and_reset(self):
        from core.ui_progress import OperationProgress

        seen = []
        progress = OperationProgress.indeterminate("scan", on_update=lambda snap: seen.append(dict(snap)))
        progress.mark_ok("acc1")
        progress.mark_error("acc2", "err")
        self.assertEqual(2, progress.completed)
        self.assertEqual(1, progress.ok)
        self.assertEqual(1, progress.errors)
        progress.reset("apply", 3)
        self.assertEqual((3, 0, 0, 0), (progress.total, progress.completed, progress.ok, progress.errors))
        self.assertGreaterEqual(len(seen), 3)

    def test_frozen_account_preserves_session_in_scraper_and_ui_status(self):
        from core.config import AppConfig
        from core.models import AccountRecord
        from core.results import ActionResult
        from core.storage import write_json_atomic
        from modules.profile_scraper import ProfileScraperService
        from ui.qt_app import QtDesktopApp

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "TELEGRAM_DATA_DIR": str(Path(temp) / "data"),
            "TELEGRAM_IMPORT_DIR": str(Path(temp) / "imports"),
        }, clear=True):
            cfg = AppConfig.load(Path(temp) / ".env")
            session_path = cfg.sessions_dir / "pyrogram" / "frozen_acc.session"
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.write_bytes(b"keep-session")
            acc = AccountRecord(
                id="frozen_acc", label="frozen_acc", backend="pyrogram",
                session_kind="session_file", session_ref=str(session_path), enabled=True,
            )
            write_json_atomic(cfg.accounts_file, {"accounts": [acc.to_dict()]})
            scraper = ProfileScraperService(cfg)
            result = ActionResult()
            scraper._record_apply_error("frozen_acc", RuntimeError("FROZEN_METHOD_INVALID"), result, None)
            self.assertTrue(session_path.is_file())
            updated = scraper.accounts.get_account("frozen_acc")
            self.assertFalse(updated.enabled)
            self.assertEqual("frozen", updated.metadata.get("health_status"))
            self.assertEqual("frozen", QtDesktopApp.spamblock_status(SimpleNamespace(), updated))


class WarmupSpamBlockScenarioTests(unittest.TestCase):
    def test_cyrillic_scenario_slugs_differ_and_keep_distinct_files(self):
        from modules.scenarios import ScenarioStore

        with tempfile.TemporaryDirectory() as temp:
            store = ScenarioStore(SimpleNamespace(data_dir=Path(temp)))
            s1 = store.slug("Прогрев")
            s2 = store.slug("Тест")
            self.assertTrue(s1 and s2)
            self.assertNotEqual(s1, s2)
            self.assertEqual(s1, store.slug("Прогрев"))

    def test_spambot_parser_checks_until_before_forever_and_dotted_ru_months(self):
        from modules.spamblock import parse_spambot_response

        reply = "Ограничения действуют до 15 янв. 2027, 10:30 UTC (not forever)."
        parsed = parse_spambot_response(reply)
        self.assertEqual("temporary", parsed.status)
        self.assertEqual("2027-01-15T10:30:00+00:00", parsed.until)

    def test_account_age_offline_estimation(self):
        from modules.account_age import estimate_registration_from_user_id

        est = estimate_registration_from_user_id(5_750_000_000)
        self.assertRegex(est or "", r"^~\d{2}/\d{2}$")


class MoneySafetyRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_sync_refuses_replace_wallet_when_ready_and_replace_existing_false(self):
        from core.config import AppConfig
        from core.models import AccountRecord
        from core.storage import write_json_atomic
        from modules.finance_money import MoneyService
        from modules.financial_vault import FinancialVault
        from modules.money_execution import NativeExecutionError
        from modules.money_workspace import NativeMoneyWorkspace

        class FakeProtector:
            name = "synthetic"
            def protect(self, value, entropy):
                return b"P:" + bytes(value)
            def unprotect(self, value, entropy):
                return bytes(value)[2:]

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "TELEGRAM_DATA_DIR": str(Path(temp) / "data"),
            "TELEGRAM_IMPORT_DIR": str(Path(temp) / "imports"),
        }, clear=True):
            cfg = AppConfig.load(Path(temp) / ".env")
            acc = AccountRecord(
                id="acc1", label="acc1", backend="pyrogram",
                session_kind="session_file", session_ref="acc1.session",
                user_id=101, enabled=True,
            )
            write_json_atomic(cfg.accounts_file, {"accounts": [acc.to_dict()]})
            vault = FinancialVault(cfg.data_dir, protector=FakeProtector())
            vault.create(confirmed=True)
            money = MoneyService(cfg)
            client = SimpleNamespace(
                get_me=AsyncMock(return_value={"user_id": 101}),
                native_money_state=AsyncMock(return_value={
                    "availability": "ready",
                    "address": "0:" + "11" * 32,
                    "public_key": "22" * 32,
                    "can_export_phrase": True,
                }),
                native_money_export_phrase=AsyncMock(side_effect=RuntimeError("2FA missing")),
                native_money_replace_wallet=AsyncMock(),
            )
            @asynccontextmanager
            async def fake_factory(*args, **kwargs):
                yield client
            ws = NativeMoneyWorkspace(money, vault=vault, client_factory=fake_factory)
            with self.assertRaisesRegex(NativeExecutionError, "refusing replaceWallet overwrite"):
                await ws.auto_sync_or_provision_wallet(
                    "acc1",
                    scope_ids={"acc1"},
                    ui_context="b" * 64,
                    replace_existing=False,
                    confirmed=True,
                )
            client.native_money_replace_wallet.assert_not_awaited()

    def test_wallet_tg_codec_high_caps_gated_behind_allow_high_caps(self):
        from modules.wallet_tg_codec import WalletTgCodec

        codec = WalletTgCodec(clock=lambda: 1_999_999_900)
        kwargs = dict(
            network="mainnet",
            anchor_public_key="11" * 32,
            seqno=1,
            valid_until=2_000_000_000,
            destination="0:" + "22" * 32,
            amount_nano=str(5 * 10**9),
            intent_id="a" * 32,
            deploy=False,
        )
        with self.assertRaises(ValueError):
            codec.prepare(**kwargs)
        prepared = codec.prepare(**kwargs, allow_high_caps=True)
        self.assertEqual("mainnet", prepared["request"]["network"])
        self.assertTrue(prepared["codec"].get("payload_hash"))


class CoreSecurityRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_manager_verified_proxy_url_fails_closed_under_vpn_gateway(self):
        from modules.auth_controller import AuthManager

        with tempfile.TemporaryDirectory() as temp:
            cfg = SimpleNamespace(global_proxy=None, proxy_pool_db=Path(temp) / "pool.db")
            auth = AuthManager.__new__(AuthManager)
            auth.config = cfg
            with patch("modules.proxy_manager.ProxyPool.acquire", AsyncMock(return_value=None)), \
                 patch.dict(os.environ, {"PROXY_MODE": "vpn_gateway"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "PROXY_MODE=vpn_gateway"):
                    await auth.verified_proxy_url()

    def test_email_mailbox_ignores_year_2026_and_accepts_subdomain_sender(self):
        from modules.email_mailbox import telegram_code

        raw = (
            b"From: Telegram <noreply@mailer.telegram.org>\r\n"
            b"To: user@example.org\r\n"
            b"Subject: Login code\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Your login code is 543219.\r\nCopyright 2026 Telegram."
        )
        self.assertEqual("543219", telegram_code(raw, "user@example.org", after=0, length=None))

    def test_secret_vault_failed_create_leaves_no_zero_byte_db_and_reads_bom(self):
        from core.secret_vault import SecretVault, VaultError, unlock_for_service

        with tempfile.TemporaryDirectory() as temp:
            vault = SecretVault(temp)
            with self.assertRaises(VaultError):
                vault.create("short")
            self.assertFalse(vault.path.exists())

            vault.create("strong-passphrase-12345")
            vault.lock()
            pw_dir = tempfile.TemporaryDirectory()
            self.addCleanup(pw_dir.cleanup)
            pw_file = Path(pw_dir.name) / "master.txt"
            pw_file.write_bytes(b"\xef\xbb\xbfstrong-passphrase-12345\r\n")
            vault2 = SecretVault(temp)
            with patch.dict(os.environ, {"TGB_VAULT_PASSWORD_FILE": str(pw_file)}, clear=True):
                unlock_for_service(vault2)
                self.assertTrue(vault2.unlocked)

    def test_vpn_gateway_offsets_ports_without_collision_over_100_vless_nodes(self):
        from modules.vpn_gateway import write_gateway_config

        nodes = [f"vless://11111111-1111-1111-1111-{i:012d}@198.51.100.1:{443 + i}?security=none#n{i}" for i in range(105)]
        xray_cfg = {
            "inbounds": [{"port": 21100, "protocol": "socks"}],
            "outbounds": [{"protocol": "vless", "settings": {"vnext": []}}],
        }
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "VPN_VLESS_SUBSCRIPTION_URL": "https://example.invalid/vless",
            "VPN_XRAY_SUBSCRIPTION_URL": "https://example.invalid/xray",
        }, clear=True), \
             patch("modules.vpn_gateway.fetch_vless_nodes", return_value=nodes), \
             patch("modules.vpn_gateway.sync_filter_vless_nodes", side_effect=lambda x: x), \
             patch("modules.vpn_gateway.fetch_ultima_configs", return_value=[xray_cfg]):
            ports = write_gateway_config(Path(temp) / "xray.json")
        self.assertEqual(106, len(ports))
        self.assertEqual(len(set(ports)), len(ports))

    async def test_guard_tasks_keeps_skipped_separate_from_errors(self):
        from core.config import AppConfig
        from core.models import AccountRecord
        from core.storage import write_json_atomic
        from core.task_history import TaskHistory
        from core.ui_progress import OperationProgress
        from modules.guard_tasks import guard_plan, run_guard_task
        from modules.security_guard import GuardResult

        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "TELEGRAM_DATA_DIR": str(Path(temp) / "data"),
            "TELEGRAM_IMPORT_DIR": str(Path(temp) / "imports"),
        }, clear=True):
            cfg = AppConfig.load(Path(temp) / ".env")
            acc = AccountRecord(
                id="g1", label="g1", backend="pyrogram",
                session_kind="session_file", session_ref="g1.session", enabled=True,
            )
            write_json_atomic(cfg.accounts_file, {"accounts": [acc.to_dict()]})
            ui_context = {"active_group": "inbox"}
            spec = guard_plan(cfg, ["g1"], ui_context)
            history = TaskHistory(cfg.data_dir)
            job_id = history.create("security-guard", "qt", spec=spec)
            progress = OperationProgress(operation="guard", total=1)
            outcome = GuardResult(skipped=1)
            with patch("modules.guard_tasks.SecurityGuardService.check", AsyncMock(return_value=outcome)):
                res = await run_guard_task(cfg, history, job_id, ui_context, progress=progress)
            self.assertEqual(1, res.skipped)
            self.assertEqual(0, res.errors)
            self.assertEqual(0, progress.errors)


if __name__ == "__main__":
    unittest.main()
