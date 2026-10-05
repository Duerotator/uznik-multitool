"""Offline regressions for findings from the public security scanners."""
from __future__ import annotations

import sys
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


if __name__ == "__main__":
    unittest.main()
