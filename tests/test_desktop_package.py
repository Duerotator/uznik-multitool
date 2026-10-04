from __future__ import annotations

import ast
import asyncio
import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))


class DesktopPackageTests(unittest.TestCase):
    def test_desktop_has_no_server_imports_or_density_control(self):
        source = (APP / "ui/qt_app.py").read_text(encoding="utf-8")
        self.assertIn('QSettings("UznikMultiTool", "Desktop")', source)
        self.assertIn('setWindowTitle("Uznik MultiTool")', source)
        for marker in ("density_combo", "change_density", "session_sync", "mobile_web", "ui.bot"):
            self.assertNotIn(marker, source)
        self.assertFalse((APP / "modules/session_sync.py").exists())
        self.assertEqual({"__init__.py", "qt_app.py"}, {p.name for p in (APP / "ui").glob("*.py")})

    def test_first_party_imports_are_complete(self):
        # Include lazy imports so optional desktop actions do not break later.
        for folder in ("core", "modules", "ui", "utils", "scripts"):
            base = ROOT if folder == "scripts" else APP
            for path in (base / folder).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        names = [node.module]
                    for name in names:
                        if name.split(".")[0] not in {"core", "modules", "ui", "utils", "scripts"}:
                            continue
                        target_base = ROOT if name.split(".")[0] == "scripts" else APP
                        target = target_base.joinpath(*name.split("."))
                        self.assertTrue(target.with_suffix(".py").is_file() or (target / "__init__.py").is_file(),
                                        f"Missing {name}, imported by {path.name}")

    def test_empty_config_contains_no_bot_or_private_settings(self):
        from core.config import AppConfig
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "TELEGRAM_DATA_DIR": str(Path(temp) / "data"),
            "TELEGRAM_IMPORT_DIR": str(Path(temp) / "imports"),
        }, clear=True):
            config = AppConfig.load(Path(temp) / ".env")
            self.assertEqual(0, config.api_id)
            self.assertEqual("", config.api_hash)
            self.assertIsNone(config.global_proxy)
            self.assertEqual("", config.email_inbox_token)
            self.assertFalse(hasattr(config, "bot_token"))
            self.assertFalse(hasattr(config, "giveaway_manager_bot_token"))
            self.assertTrue(config.sessions_dir.is_dir())
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_API_ID"):
                config.require_telegram_api()

    def test_launchers_are_portable(self):
        launcher = (APP / "launch.pyw").read_text(encoding="utf-8")
        shortcut = (ROOT / "scripts/create_shortcut.ps1").read_text(encoding="utf-8")
        self.assertIn("Path(__file__).resolve().parent", launcher)
        self.assertIn("$PSScriptRoot", shortcut)
        self.assertIn("'Uznik MultiTool.lnk'", shortcut)
        for source in (launcher, shortcut):
            self.assertNotIn("tgbetaaccmanager", source)
            self.assertNotIn("C:\\Users", source)

    def test_sensitive_paths_are_ignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        for pattern in ("data/**", "imports/**", ".env", "*.session*", "*.db*", "*.lnk", "templates/*"):
            self.assertIn(pattern, ignore)
        self.assertIn("!config/.env.example", ignore)

    def test_gateway_never_discovers_personal_subscription(self):
        from modules.vpn_gateway import _subscription_url, fetch_vless_nodes
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "VPN_VLESS_SUBSCRIPTION_URL"):
                _subscription_url("VPN_VLESS_SUBSCRIPTION_URL")
            with self.assertRaisesRegex(RuntimeError, "VPN_VLESS_SUBSCRIPTION_URL"):
                fetch_vless_nodes()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "subscription.txt"
            expected = "vless://fixture@example.invalid:443?security=tls#Example"
            for content in (expected, base64.b64encode(expected.encode()).decode()):
                source.write_text(content, encoding="utf-8")
                with patch.dict(os.environ, {"VPN_VLESS_FILE": str(source)}, clear=True):
                    self.assertEqual([expected], fetch_vless_nodes())

    def test_no_shared_two_factor_password(self):
        from modules.direct_access import DirectAccessService
        service = DirectAccessService(SimpleNamespace())
        # With no configured password, no browser interaction is attempted.
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(asyncio.run(service._fill_two_factor_if_requested(Mock())))


if __name__ == "__main__":
    unittest.main()
