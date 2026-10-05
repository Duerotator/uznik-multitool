"""Offline regressions for findings from the public security scanners."""
from __future__ import annotations

import sys
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from modules.proxy_manager import _is_geonode_source
from modules.webshare_source import WebshareSource


class SourceHostTests(unittest.TestCase):
    def test_geonode_uses_host_not_url_substring(self):
        for url in ("https://geonode.com/proxies", "https://api.geonode.com/list", "https://GEONODE.COM./list"):
            self.assertTrue(_is_geonode_source(url), url)
        for url in ("https://geonode.com.example.invalid/list", "https://example.invalid/geonode.com",
                    "https://geonode.com@example.invalid/list", "https://example.invalid/?provider=geonode.com"):
            self.assertFalse(_is_geonode_source(url), url)


class WebshareLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_error_never_logs_key_prefix(self):
        await self.check_failure(SimpleNamespace(status_code=403))

    async def test_exception_never_logs_sensitive_message(self):
        await self.check_failure(ValueError("fixture-sensitive-response"))

    async def check_failure(self, result):
        source = WebshareSource()
        client = AsyncMock()
        if isinstance(result, Exception):
            client.get.side_effect = result
        else:
            client.get.return_value = result
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch.object(source, "_load_accounts", return_value=[{"api_key": "fixture-private-api-key"}]), \
             patch("modules.webshare_source.httpx.AsyncClient", return_value=context), \
             self.assertLogs("webshare-source", level="WARNING") as captured:
            self.assertEqual([], await source.fetch())
        logs = "\n".join(captured.output)
        self.assertIn("account #1", logs)
        self.assertNotIn("fixture-pr", logs)
        self.assertNotIn("fixture-sensitive-response", logs)


class PrivatePlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "profile_plan.json"

    def test_generation_and_loading_round_trip_without_cleartext(self):
        from utils.profile_generator import write_profile_plan
        from modules.profile_customizer import ProfileCustomizer
        account = SimpleNamespace(id="private-account-marker", metadata={})
        expected = write_profile_plan([account], self.path.parent / "avatars", self.path, seed=1)
        self.assertNotIn(b"private-account-marker", self.path.read_bytes())
        customizer = ProfileCustomizer.__new__(ProfileCustomizer)
        self.assertEqual(expected, customizer.load_profile_plan(self.path))
        key = self.path.with_name(self.path.name + ".key")
        self.assertTrue(key.read_bytes().startswith(b"DPAPI1\0" if os.name == "nt" else b"FERNET1\0"))
        if os.name != "nt":
            self.assertEqual(0o600, key.stat().st_mode & 0o777)

    def test_legacy_dict_and_list_are_migrated_without_losing_fields(self):
        from modules.profile_customizer import ProfileCustomizer
        customizer = ProfileCustomizer.__new__(ProfileCustomizer)
        profiles = [{"account_id": "private-fixture", "username": "fixture", "custom": 123}]
        for raw in ({"profiles": profiles}, profiles):
            self.path.write_text(json.dumps(raw), encoding="utf-8")
            self.assertEqual(profiles, customizer.load_profile_plan(self.path))
            self.assertNotIn(b"private-fixture", self.path.read_bytes())

    def test_invalid_legacy_plan_is_not_modified(self):
        from modules.profile_customizer import ProfileCustomizer
        customizer = ProfileCustomizer.__new__(ProfileCustomizer)
        original = '{"profiles":"invalid"}'
        self.path.write_text(original, encoding="utf-8")
        with self.assertRaises(ValueError):
            customizer.load_profile_plan(self.path)
        self.assertEqual(original, self.path.read_text())

    def test_missing_key_never_generates_a_replacement(self):
        from core.private_storage import read_private_json, write_private_json
        write_private_json(self.path, {"profiles": []})
        original = self.path.read_bytes()
        key = self.path.with_name(self.path.name + ".key")
        key.unlink()
        with self.assertRaisesRegex(ValueError, "key is missing"):
            read_private_json(self.path)
        self.assertFalse(key.exists())
        self.assertEqual(original, self.path.read_bytes())

    def test_wrong_key_and_tampering_are_rejected(self):
        from core.private_storage import read_private_json, write_private_json
        write_private_json(self.path, {"profiles": []})
        other = self.path.parent / "other.json"
        write_private_json(other, {})
        key = self.path.with_name(self.path.name + ".key")
        original_key = key.read_bytes()
        key.write_bytes(other.with_name(other.name + ".key").read_bytes())
        with self.assertRaisesRegex(ValueError, "cannot be decrypted"):
            read_private_json(self.path)
        key.write_bytes(original_key)
        raw = json.loads(self.path.read_text())
        raw["ciphertext"] = "invalid ciphertext"
        self.path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, "cannot be decrypted"):
            read_private_json(self.path)


class AuthLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_sign_in_error_does_not_log_phone_or_exception_payload(self):
        from modules.auth_controller import AuthManager
        auth = AuthManager.__new__(AuthManager)
        auth._lock = asyncio.Lock()
        phone = "15550001234"
        client = SimpleNamespace(sign_in=AsyncMock(side_effect=RuntimeError(phone + " private-code")))
        auth.sessions = {phone: SimpleNamespace(client=client, proxy_url=None)}
        with self.assertLogs("auth-controller", level="ERROR") as captured:
            result = await auth.sign_in(phone, "fixture-hash", "12345")
        self.assertFalse(result["ok"])
        self.assertNotIn(phone, "\n".join(captured.output))
        self.assertNotIn("private-code", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
