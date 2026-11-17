import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Synthetic Marketapp contract + MockTransport, no credentials/network/money."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import httpx

from modules.marketapp_catalog import CatalogError, ENDPOINT, MAX_RESPONSE, MarketappCatalogReader, parse_catalog


def item(**changes):
    return {"nft_address": "0:" + "01" * 32, "owner": "0:" + "02" * 32,
            "nft_name": "Synthetic gift #1", "attributes": [{"trait_type": "Model", "value": "Test"}],
            "min_duration": 86400, "max_duration": 604800, "price_per_day": "1234567890123456789",
            "discount_per_day": 1.5, "listed_at": None, "is_sellable_during_rent": False, **changes}


def body(rows=None, **changes):
    return json.dumps({"cursor": None, "items": rows if rows is not None else [item()], **changes}).encode()


class CatalogTests(unittest.TestCase):
    def test_cli_without_confirmation_is_offline_and_sanitized(self):
        result = subprocess.run([sys.executable, "scripts/marketapp_catalog.py"],
                                cwd=Path(__file__).resolve().parents[2],
                                env={**os.environ, "MARKETAPP_API_TOKEN": "synthetic-secret"},
                                capture_output=True, text=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("confirmation", result.stderr)
        self.assertNotIn("synthetic-secret", result.stdout + result.stderr)

    def test_exact_amount_projection_and_no_false_quote(self):
        result = parse_catalog(body([item(private="not exported")]))
        self.assertEqual(result["items"][0]["price_per_day_nanograms"], "1234567890123456789")
        self.assertIsNone(result["items"][0]["total_quote"])
        self.assertFalse(result["purchase_available"])
        self.assertNotIn("private", result["items"][0])
        self.assertEqual(result["verification"], "provider_reported")

    def test_sellable_is_hidden_by_default_and_flagged_when_requested(self):
        raw = body([item(is_sellable_during_rent=True)])
        self.assertEqual(parse_catalog(raw)["items"], [])
        self.assertTrue(parse_catalog(raw, include_sellable=True)["items"][0]["early_termination_risk"])
        missing = item()
        del missing["is_sellable_during_rent"]
        with self.assertRaises(CatalogError):
            parse_catalog(body([missing]))

    def test_malformed_rows_fail_closed(self):
        for changes in ({"price_per_day": 1}, {"price_per_day": "-1"}, {"min_duration": True},
                        {"max_duration": 1}, {"discount_per_day": float("nan")},
                        {"discount_per_day": 10**300}, {"is_sellable_during_rent": "false"},
                        {"owner": "https://evil.invalid"}, {"nft_name": "a\nsecret"},
                        {"attributes": [{"trait_type": "x", "value": True}]}, {"listed_at": -1}):
            with self.subTest(changes=list(changes)), self.assertRaises(CatalogError):
                parse_catalog(body([item(**changes)]))

    def test_page_limits_duplicate_json_and_unknown_schema(self):
        for raw in (b'{"items":[],"cursor":null,"cursor":"x"}', b'[]', b'{"items":[]}',
                    body([item(), item()]), body(cursor="x"*2049), b' '* (MAX_RESPONSE+1)):
            with self.assertRaises(CatalogError):
                parse_catalog(raw)
        with self.assertRaises(CatalogError):
            parse_catalog(body(), limit=True)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_fixed_get_raw_auth_and_validated_filters(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.method, "GET")
            self.assertEqual(str(request.url).split("?")[0], ENDPOINT)
            self.assertEqual(request.headers["authorization"], "synthetic-token")
            self.assertEqual(request.url.params["include_sellable"], "false")
            return httpx.Response(200, content=body())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reader = MarketappCatalogReader(client=client)
            await reader.read(token="synthetic-token", confirmed=True, model="Test", limit=1)
            with self.assertRaises(CatalogError):
                await reader.read(token="synthetic-token", confirmed=True)
        self.assertEqual(len(requests), 1)

    async def test_no_confirmation_bad_token_or_filter_never_sends(self):
        def handler(request):
            self.fail("Must not send")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for changes in ({"confirmed": False}, {"token": "bad\r\nheader"}, {"limit": 101},
                            {"sort_by": "bad"}, {"include_sellable": 1}, {"collection_address": "bad"}):
                args = {"token": "synthetic-token", "confirmed": True, **changes}
                with self.assertRaises(CatalogError):
                    await MarketappCatalogReader(client=client).read(**args)

    async def test_redirect_auth_error_and_retry_after_no_retry_or_body_leak(self):
        for status in (302, 401, 429, 500):
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(status, headers={"Location": "https://evil.invalid", "Retry-After": "120"},
                                      text="private provider diagnostic synthetic-token")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                reader = MarketappCatalogReader(client=client, clock=lambda: 1000)
                with self.assertRaises(CatalogError) as caught:
                    await reader.read(token="synthetic-token", confirmed=True)
                self.assertNotIn("synthetic-token", str(caught.exception))
                self.assertEqual(reader._next_read, 1120)
                with self.assertRaises(CatalogError):
                    await reader.read(token="synthetic-token", confirmed=True)
            self.assertEqual(len(requests), 1)

    async def test_size_encoding_timeout_and_cancel(self):
        for response in (httpx.Response(200, content=b'x'*(MAX_RESPONSE+1)),
                         httpx.Response(200, headers={"Content-Encoding": "br"}, content=b'')):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
                with self.assertRaises(CatalogError):
                    await MarketappCatalogReader(client=client).read(token="synthetic", confirmed=True)
        async def timeout(request):
            raise httpx.ReadTimeout("secret-token must not escape", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with self.assertRaises(CatalogError) as caught:
                await MarketappCatalogReader(client=client).read(token="synthetic", confirmed=True)
            self.assertNotIn("secret-token", str(caught.exception))
        async def cancel(request):
            raise asyncio.CancelledError()
        async with httpx.AsyncClient(transport=httpx.MockTransport(cancel)) as client:
            with self.assertRaises(asyncio.CancelledError):
                await MarketappCatalogReader(client=client).read(token="synthetic", confirmed=True)


if __name__ == "__main__":
    unittest.main()
