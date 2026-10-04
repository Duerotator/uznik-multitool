"""Offline completeness checks for folder scaffolding, helper tools and branding."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.project_layout import DATA_FOLDERS, IMPORT_FOLDERS, RESOURCE_FOLDERS, EMPTY_TEMPLATES, ensure_project_layout

ROOT = Path(__file__).resolve().parent.parent
HELPERS = (
    "sessions/create_session.py", "sessions/batch_create_sessions.py",
    "sessions/process_auth_input.py", "accounts/cleanup.py", "accounts/dedupe.py",
    "accounts/security.py", "profiles/download_avatar_pack.py", "network/check_setup.py",
)


class FolderTests(unittest.TestCase):
    def test_layout_creation_preserves_existing_templates_and_phone_list(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data, imports = root / "custom-data", root / "custom-imports"
            ensure_project_layout(root, data, imports)
            for base, folders in ((data, DATA_FOLDERS), (imports, IMPORT_FOLDERS), (root, RESOURCE_FOLDERS)):
                for name in folders:
                    self.assertTrue((base / name).is_dir(), name)
            for name in EMPTY_TEMPLATES:
                self.assertEqual("", (root / "templates" / name).read_text())
            template = root / "templates" / "bios.txt"
            phones = root / "sessions/batch_phones.txt"
            mailboxes = imports / "emails/accounts.txt"
            template.write_text("user template fixture", encoding="utf-8")
            phones.write_text("user phone-list fixture", encoding="utf-8")
            mailboxes.write_text("private mailbox fixture", encoding="utf-8")
            ensure_project_layout(root, data, imports)
            self.assertEqual("user template fixture", template.read_text())
            self.assertEqual("user phone-list fixture", phones.read_text())
            self.assertEqual("private mailbox fixture", mailboxes.read_text())

    def test_git_contains_scaffolds_for_all_runtime_folders(self):
        for name in DATA_FOLDERS:
            self.assertTrue((ROOT / "data" / name / ".gitkeep").is_file(), name)
        for name in ("imports/auth_input/processed", "imports/emails", "sessions",
                     "assets/avatar_packs/male", "assets/avatar_packs/female",
                     "assets/avatar_packs/unknown", "assets/avatar_packs_raw"):
            self.assertTrue((ROOT / name / ".gitkeep").is_file(), name)
        self.assertTrue((ROOT / "imports/auth_input/README.md").is_file())

    @unittest.skipUnless(shutil.which("git"), "Git is optional for ZIP installations")
    def test_git_rules_ignore_user_data_but_allow_folder_markers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copy2(ROOT / ".gitignore", root / ".gitignore")
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, capture_output=True)
            ignored = [
                "data/accounts.json", "data/profiles_archive/example/meta.json",
                "data/passkeys/key.json", "data/browser_profiles/example/Local State",
                "imports/source.session", "imports/auth_input/source.json",
                "imports/auth_input/processed/source.session",
                "sessions/batch_phones.txt", "templates/profile_plan.json",
                "imports/emails/accounts.txt", "data/email_mailbox_bindings.json",
                "templates/bios.txt", "assets/avatar_packs/female/user.jpg",
                "assets/avatar_packs_raw/user.jpg", ".env",
            ]
            for path in ignored:
                result = subprocess.run(["git", "check-ignore", "--no-index", path], cwd=root, capture_output=True)
                self.assertEqual(0, result.returncode, path)
            allowed = [
                "data/sessions/pyrogram/.gitkeep", "data/README.md",
                "imports/auth_input/README.md", "imports/auth_input/processed/.gitkeep",
                "imports/emails/README.md", "imports/emails/.gitkeep",
                "sessions/README.md", "sessions/.gitkeep", "sessions/create_session.bat",
                "sessions/create_sessions.bat", "sessions/batch_create_sessions.bat",
                "assets/avatar_packs/male/.gitkeep", "assets/branding/uznik-multitool.ico",
                "config/.env.example",
            ]
            for path in allowed:
                result = subprocess.run(["git", "check-ignore", "--no-index", path], cwd=root, capture_output=True)
                self.assertEqual(1, result.returncode, path)


class HelperTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "WinGet executable aliases are Windows-specific")
    def test_xray_winget_alias_is_found_before_path_refresh(self):
        from modules.vpn_gateway import xray_executable
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"LOCALAPPDATA": temp}, clear=True):
            executable = Path(temp) / "Microsoft/WinGet/Links/xray.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch("modules.vpn_gateway.shutil.which", return_value=None):
                self.assertEqual(str(executable), xray_executable())

    def test_cli_help_works_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            for helper in HELPERS:
                with self.subTest(helper=helper):
                    result = subprocess.run(
                        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / helper), "--help"],
                        cwd=temp, capture_output=True, text=True, encoding="utf-8", timeout=30,
                    )
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn("usage:", result.stdout)

    def test_phone_list_deduplicates_and_skips_comments(self):
        from scripts.sessions.batch_create_sessions import load_phones
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "phones.txt"
            source.write_text("# fixture\n10000000000\n\n10000000000\n10000000001\n", encoding="utf-8-sig")
            self.assertEqual(["10000000000", "10000000001"], load_phones(source))

    def test_batch_continues_after_one_failed_login(self):
        from scripts.sessions.batch_create_sessions import run
        mock_login = AsyncMock(side_effect=[RuntimeError("fixture"), {"ok": True}])
        with patch("scripts.sessions.batch_create_sessions.login_phone", mock_login), \
             patch("scripts.sessions.batch_create_sessions.log_session_error", return_value=None) as log_error, \
             patch("scripts.sessions.batch_create_sessions.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(1, asyncio.run(run(SimpleNamespace(), ["first", "second"], 0)))
        self.assertEqual(2, mock_login.await_count)
        self.assertEqual(1, log_error.call_count)
        self.assertIsInstance(log_error.call_args.args[2], RuntimeError)

    def test_dedupe_preview_uses_backend_identity_and_keeps_unknowns(self):
        from scripts.accounts.dedupe import duplicate_ids
        from modules.accounts import AccountService
        from core.models import AccountRecord

        def record(key, user_id=None, phone=None):
            return AccountRecord(key, key, "pyrogram", "session_file", "fixture", user_id=user_id, phone=phone)
        service = AccountService.__new__(AccountService)
        service.list_accounts = lambda: [
            record("first", 123), record("same_user", 123), record("unknown"),
            record("phone_a", phone="+10000000000"), record("phone_b", phone="10000000000"),
        ]
        self.assertEqual(["same_user", "phone_b"], duplicate_ids(service))

    def test_security_cli_reports_action_result_without_exposing_keys(self):
        from scripts.accounts.security import main
        from core.results import ActionResult
        from core.config import AppConfig
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "TELEGRAM_DATA_DIR": str(Path(temp) / "data"),
            "TELEGRAM_IMPORT_DIR": str(Path(temp) / "imports"),
            "TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "fixture",
        }, clear=True):
            config = AppConfig.load(Path(temp) / ".env")
        original_cwd = Path.cwd()
        try:
            with patch("sys.argv", ["security.py", "register-passkey", "--accounts", "fixture", "--execute"]), \
                 patch("core.config.AppConfig.load", return_value=config), \
                 patch("modules.accounts.AccountService") as accounts, \
                 patch("modules.account_security.AccountSecurityService") as service:
                accounts.return_value.get_account.return_value = SimpleNamespace(id="fixture")
                service.return_value.add_passkeys = AsyncMock(return_value=ActionResult(ok=1))
                self.assertEqual(0, main())
                service.return_value.add_passkeys.assert_awaited_once()
        finally:
            os.chdir(original_cwd)

    def test_avatar_organizer_accepts_uncategorized_local_images(self):
        from scripts.profiles.download_avatar_pack import main
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw, pack = root / "raw", root / "pack"
            raw.mkdir()
            (raw / "holiday.jpg").write_bytes(b"local fixture image")
            original_cwd = Path.cwd()
            try:
                with patch("sys.argv", ["download_avatar_pack.py", "--organize-only", "--raw-dir", str(raw), "--pack-dir", str(pack)]):
                    self.assertEqual(0, main())
                self.assertEqual(1, len(list((pack / "unknown").glob("*.jpg"))))
            finally:
                os.chdir(original_cwd)


class BrandingTests(unittest.TestCase):
    def test_ico_has_small_and_large_windows_sizes(self):
        from PIL import Image
        with Image.open(ROOT / "assets/branding/uznik-multitool.ico") as icon:
            self.assertTrue({(16, 16), (32, 32), (48, 48), (256, 256)}.issubset(icon.ico.sizes()))
        with Image.open(ROOT / "assets/branding/uznik-multitool.png") as image:
            self.assertEqual(0, image.convert("RGBA").getpixel((0, 0))[3])
        shortcut = (ROOT / "scripts/create_shortcut.ps1").read_text(encoding="utf-8")
        self.assertIn("assets\\branding\\uznik-multitool.ico", shortcut)


if __name__ == "__main__":
    unittest.main()
