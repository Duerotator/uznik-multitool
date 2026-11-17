import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / "app", _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Offline regression tests for Phase 4 Rent automation & MarketApp pipelines."""

import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from core.config import AppConfig
from core.models import AccountRecord
from core.storage import write_json_atomic
from modules.asset_registry import AssetStore
from modules.gift_profile_transport import _valid_slug, list_own_gifts, set_visibility
from modules.marketapp_catalog import (
    CatalogError,
    MarketappCatalogReader,
    format_expiry_countdown,
    format_marketapp_amount,
    parse_send_tx,
    parse_usernames,
)
from modules.marketapp_rentals import MarketappRentalService
from modules.rent_orchestrator import RentJobStore, RentOrchestratorService


GIFT_A = "0:" + "11" * 32
GIFT_B = "0:" + "22" * 32
OWNER_A = "0:" + "aa" * 32
TC_URI = "https://app.tonkeeper.com/ton-connect?v=2&id=abc"
VALID_BOC_B64 = base64.b64encode(b"synthetic-boc-payload").decode()


class _FakeConn:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _tx_body(messages=None, valid_until=2_000):
    msgs = messages if messages is not None else [
        {
            "address": GIFT_A,
            "amount": "150000000",
            "payload": VALID_BOC_B64,
            "stateInit": None,
        }
    ]
    return json.dumps({"transaction": {"validUntil": valid_until, "messages": msgs}}).encode()


class GiftTransportAndCatalogPhase4Tests(unittest.TestCase):
    def test_hyphenated_collectible_slug_and_rejected_slugs(self):
        self.assertTrue(_valid_slug("B-DayCandle-42"))
        self.assertTrue(_valid_slug("Plush_Pepe-9999"))
        for bad in ("-bad-1", "no_number_suffix", "bad space-1", ""):
            self.assertFalse(_valid_slug(bad))

        class _Saved:
            def __init__(self, gift):
                self.gift = gift
                self.unsaved = False
                self.pinned_to_top = True

        class _StarGiftUnique:
            def __init__(self):
                self.gift_address = GIFT_A
                self.slug = "B-DayCandle-42"
                self.title = "B-Day Candle"
                self.host_id = SimpleNamespace(user_id=101)

        class _PeerUser:
            def __init__(self, user_id):
                self.user_id = user_id

        _StarGiftUnique.host_id_cls = _PeerUser

        async def run():
            unique = _StarGiftUnique()
            unique.host_id = _PeerUser(101)
            types = SimpleNamespace(
                StarGiftUnique=_StarGiftUnique,
                PeerUser=_PeerUser,
                InputSavedStarGiftSlug=lambda slug: ("slug", slug),
            )
            functions = SimpleNamespace(
                GetSavedStarGifts=lambda **kw: ("get", kw),
                SaveStarGift=lambda **kw: ("save", kw),
            )

            async def invoke(req):
                if req[0] == "get":
                    return SimpleNamespace(gifts=[_Saved(unique)], count=1, next_offset="")
                if req[0] == "save":
                    return True
                raise AssertionError(req)

            listed = await list_own_gifts(invoke, functions, types, "me")
            self.assertTrue(listed["complete"])
            self.assertEqual("B-DayCandle-42", listed["gifts"][0]["slug"])
            self.assertTrue(listed["gifts"][0]["pinned_to_top"])
            await set_visibility(invoke, functions, types, "B-DayCandle-42", True)

        asyncio.run(run())

    def test_parse_send_tx_and_usernames_and_formatting(self):
        self.assertEqual("1.5 GRAM", format_marketapp_amount("1500000000"))
        cd = format_expiry_countdown(2_000, now=1_000)
        self.assertFalse(cd["expired"])
        self.assertEqual("16m left", cd["countdown_text"])

        parsed_tx = parse_send_tx(_tx_body())
        self.assertEqual("150000000", parsed_tx["total_ton_nano"])
        self.assertEqual("0.15", parsed_tx["total_ton"])

        # Reject 0 or >4 outgoing messages
        with self.assertRaises(CatalogError):
            parse_send_tx(_tx_body(messages=[]))
        five = [{"address": GIFT_A, "amount": "100", "payload": None, "stateInit": None} for _ in range(5)]
        with self.assertRaises(CatalogError):
            parse_send_tx(_tx_body(messages=five))

        uname_raw = json.dumps({
            "cursor": None,
            "items": [{
                "nft_address": GIFT_A,
                "owner": OWNER_A,
                "nft_name": "@demo_user",
                "attributes": [],
                "min_duration": 86400,
                "max_duration": 604800,
                "price_per_day": "2000000000",
                "discount_per_day": 0.0,
                "listed_at": None,
                "is_sellable_during_rent": False,
            }],
        }).encode()
        page = parse_usernames(uname_raw)
        self.assertEqual("usernames", page["inventory_kind"])
        self.assertEqual("username", page["items"][0]["asset_kind"])
        self.assertEqual("1d", page["items"][0]["min_duration_text"])
        self.assertEqual("2 GRAM", page["items"][0]["min_total_formatted"])


class MarketappRentalsAndOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1_000.0
        self.env_patch = patch.dict(os.environ, {
            "TELEGRAM_DATA_DIR": str(Path(self.temp.name) / "data"),
            "TELEGRAM_IMPORT_DIR": str(Path(self.temp.name) / "imports"),
        }, clear=True)
        self.env_patch.start()
        self.cfg = AppConfig.load(Path(self.temp.name) / ".env")
        self.accounts_data = [
            AccountRecord(id="acc1", label="acc1", backend="pyrogram", session_kind="session_file", session_ref="acc1.session", user_id=101, enabled=True),
            AccountRecord(id="acc2", label="acc2", backend="pyrogram", session_kind="session_file", session_ref="acc2.session", user_id=102, enabled=True),
            AccountRecord(id="disabled", label="off", backend="pyrogram", session_kind="session_file", session_ref="off.session", user_id=103, enabled=False),
        ]
        write_json_atomic(self.cfg.accounts_file, {"accounts": [a.to_dict() for a in self.accounts_data]})
        self.ui_context = "a" * 64
        self.scope_ids = {"acc1", "acc2", "disabled"}

    async def asyncTearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    def _hosted_state(self):
        return {
            "acc1": {
                "user_id": 101,
                "username": "host_one",
                "gifts": {
                    GIFT_A: {"nft_address": GIFT_A, "slug": "B-DayCandle-42", "title": "Candle", "host_user_id": 101, "visible": True, "pinned_to_top": False},
                    GIFT_B: {"nft_address": GIFT_B, "slug": "PlushPepe-7", "title": "Pepe", "host_user_id": 101, "visible": False, "pinned_to_top": False},
                },
            },
            "acc2": {
                "user_id": 102,
                "username": "host_two",
                "gifts": {},
            },
        }

    def _make_services(self, *, http_handler, hosted_state=None):
        state = hosted_state if hosted_state is not None else self._hosted_state()
        client = httpx.AsyncClient(transport=httpx.MockTransport(http_handler))
        self.addAsyncCleanup(client.aclose)
        reader = MarketappCatalogReader(client=client, clock=lambda: self.now)

        def client_factory(cfg, account):
            acc_state = state[account.id]

            async def get_me():
                return {"user_id": acc_state["user_id"], "username": acc_state["username"]}

            async def list_gifts():
                return {"gifts": [dict(g) for g in acc_state["gifts"].values()], "complete": True}

            async def set_vis(slug, visible):
                for row in acc_state["gifts"].values():
                    if row["slug"] == slug:
                        row["visible"] = visible
                        return True
                raise AssertionError(f"Unknown slug {slug}")

            return _FakeConn(SimpleNamespace(
                get_me=AsyncMock(side_effect=get_me),
                list_own_hosted_gifts=AsyncMock(side_effect=list_gifts),
                set_hosted_gift_visibility=AsyncMock(side_effect=set_vis),
            ))

        rentals = MarketappRentalService(
            self.cfg, reader=reader, client_factory=client_factory,
            token_source=lambda: "synthetic-token", clock=lambda: self.now,
        )
        return reader, rentals

    async def test_consecutive_visibility_tokens_sync_batch_and_batch_visibility(self):
        rented_payload = json.dumps({
            "cursor": None,
            "items": [
                {"nft_address": GIFT_A, "nft_name": "Candle #42", "duration": 7, "end_time": 5_000, "price_per_day": "1000000000", "is_sellable_during_rent": False},
                {"nft_address": GIFT_B, "nft_name": "Pepe #7", "duration": 7, "end_time": 5_000, "price_per_day": "1000000000", "is_sellable_during_rent": False},
            ],
        }).encode()
        reader, rentals = self._make_services(http_handler=lambda req: httpx.Response(200, content=rented_payload))

        batch = await rentals.sync_batch(["acc1", "acc2", "disabled", "outside"], scope_ids={"acc1", "acc2", "disabled"}, ui_context=self.ui_context)
        self.assertEqual(2, batch["scanned_count"])
        self.assertEqual(2, batch["error_count"])
        self.assertIn("outside", batch["errors"])
        self.assertIn("disabled", batch["errors"])
        tok1 = batch["review_tokens"]["acc1"]

        # Toggle Gift A False, then reuse the FRESH returned token to toggle Gift B True without re-scanning
        res1 = await rentals.execute(
            "visibility",
            {"confirmed": True, "account_id": "acc1", "review_token": tok1, "nft_address": GIFT_A, "visible": False},
            scope_ids=self.scope_ids, ui_context=self.ui_context,
        )
        tok2 = res1["review_token"]
        self.assertTrue(isinstance(tok2, str) and tok2 and tok2 != tok1)

        res2 = await rentals.execute(
            "visibility",
            {"confirmed": True, "account_id": "acc1", "review_token": tok2, "nft_address": GIFT_B, "visible": True},
            scope_ids=self.scope_ids, ui_context=self.ui_context,
        )
        tok3 = res2["review_token"]

        # Batch set all hosted gifts on acc1 -> visible=False
        batch_vis = await rentals.set_account_visibility_all(
            "acc1", visible=False, confirmed=True, review_token=tok3,
            scope_ids=self.scope_ids, ui_context=self.ui_context,
        )
        self.assertEqual([GIFT_B], batch_vis["updated"])
        self.assertIn(GIFT_A, batch_vis["skipped"])
        self.assertEqual([], list(batch_vis["failed"]))

    async def test_sync_workspace_pay_pipeline_uncertain_double_pay_block_and_resume(self):
        flags = {"rented_active": False, "hosted": False, "tonconnect_calls": 0}
        hosted = self._hosted_state()
        hosted["acc1"]["gifts"] = {}

        def handler(req):
            url = str(req.url)
            if "/v1/rent/gifts/" in url:
                return httpx.Response(200, content=json.dumps({
                    "cursor": None,
                    "items": [{
                        "nft_address": GIFT_A, "owner": OWNER_A, "nft_name": "B-DayCandle-42",
                        "attributes": [], "min_duration": 1, "max_duration": 30,
                        "price_per_day": "500000000", "discount_per_day": 0.0,
                        "listed_at": None, "is_sellable_during_rent": False,
                    }],
                }).encode())
            if "/v1/rent/usernames/" in url:
                return httpx.Response(500, text="temporary upstream error")
            if "/v1/rent/my-rented/" in url:
                rows = []
                if flags["rented_active"]:
                    rows.append({
                        "nft_address": GIFT_A, "nft_name": "B-DayCandle-42", "duration": 3,
                        "end_time": 10_000, "price_per_day": "500000000", "is_sellable_during_rent": False,
                    })
                return httpx.Response(200, content=json.dumps({"cursor": None, "items": rows}).encode())
            if f"/v1/rent/{GIFT_A}/pay/" in url:
                return httpx.Response(200, content=_tx_body(valid_until=2_500))
            if f"/v1/rent/{GIFT_A}/extend/" in url:
                return httpx.Response(200, content=_tx_body(valid_until=2_500))
            if "/v1/rent/cancel/" in url:
                return httpx.Response(200, content=_tx_body(valid_until=2_500))
            if "/tonconnect/" in url:
                flags["tonconnect_calls"] += 1
                return httpx.Response(200, content=json.dumps({"ok": True, "status": "bound"}).encode())
            raise AssertionError(url)

        reader, rentals = self._make_services(http_handler=handler, hosted_state=hosted)
        ton_reader = SimpleNamespace(
            read=AsyncMock(return_value={
                "balance_nano": "400000000", "balance_native": "0.400000000",
                "chain_state": "active", "provider": "toncenter", "source_sync_time": self.now,
            })
        )
        assets = AssetStore(self.cfg.data_dir, clock=lambda: self.now)
        assets.register("acc1", "mainnet", GIFT_B, "manual", expected_revision=None, confirmed=True)
        store = RentJobStore(self.cfg.data_dir, clock=lambda: self.now)
        svc = RentOrchestratorService(
            self.cfg, rentals=rentals, reader=reader, ton_reader=ton_reader,
            asset_store=assets, job_store=store, token_source=lambda: "synthetic-token",
            clock=lambda: self.now, sleep=AsyncMock(),
        )

        # sync_workspace merges catalogs + balances while isolating usernames endpoint failure
        ws = await svc.sync_workspace(["acc1", "acc2"], scope_ids=self.scope_ids, ui_context=self.ui_context)
        self.assertIsNotNone(ws["gifts_catalog"])
        self.assertIn("usernames_catalog", ws["section_errors"])
        self.assertEqual("ok", ws["balances"]["acc1"]["status"])
        self.assertEqual("unbound", ws["balances"]["acc2"]["status"])

        # Prepare quote for pay (3 days = 259200s per OpenAPI spec)
        quote = await svc.prepare_rent_pipeline(
            operation="pay", nft_address=GIFT_A, asset_kind="gift", duration=259_200,
            price_per_day="500000000", account_id="acc1", nft_name="B-DayCandle-42",
            auto_visible=True, scope_ids=self.scope_ids, ui_context=self.ui_context,
        )
        self.assertEqual(259_200, quote["duration_seconds"])
        self.assertEqual("3d", quote["duration_text"])
        self.assertEqual("1.5 GRAM", quote["total_gram_formatted"])
        job_id = quote["job_id"]

        # 1) Preflight error rolls state back to quoted
        bad_wallet = SimpleNamespace(
            preview_raw_messages=AsyncMock(side_effect=RuntimeError("insufficient TON")),
            execute_raw_messages=AsyncMock(),
        )
        with self.assertRaisesRegex(CatalogError, "preflight failed"):
            await svc.execute_rent_pipeline(
                job_id, review_token=quote["review_token"], confirmation=quote["confirmation_text"],
                wallet_x=bad_wallet, scope_ids=self.scope_ids, ui_context=self.ui_context,
            )
        self.assertEqual("quoted", svc.get_job(job_id)["state"])
        bad_wallet.execute_raw_messages.assert_not_awaited()

        # 2) Broadcast timeout -> tx_uncertain, blocks second pay job for same NFT, then resume completes
        uncertain_wallet = SimpleNamespace(
            preview_raw_messages=AsyncMock(return_value={"review_token": "wx-tok", "expected_confirmation": "WX OK"}),
            execute_raw_messages=AsyncMock(side_effect=RuntimeError("network drop after sendBoc")),
        )
        with self.assertRaisesRegex(CatalogError, "uncertain"):
            await svc.execute_rent_pipeline(
                job_id, review_token=quote["review_token"], confirmation=quote["confirmation_text"],
                wallet_x=uncertain_wallet, scope_ids=self.scope_ids, ui_context=self.ui_context,
            )
        self.assertEqual("tx_uncertain", svc.get_job(job_id)["state"])

        dup_quote = await svc.prepare_rent_pipeline(
            operation="pay", nft_address=GIFT_A, asset_kind="gift", duration=259_200,
            price_per_day="500000000", account_id="acc1", nft_name="B-DayCandle-42",
            scope_ids=self.scope_ids, ui_context=self.ui_context,
        )
        with self.assertRaisesRegex(CatalogError, "already covers this NFT"):
            await svc.execute_rent_pipeline(
                dup_quote["job_id"], review_token=dup_quote["review_token"], confirmation=dup_quote["confirmation_text"],
                wallet_x=uncertain_wallet, scope_ids=self.scope_ids, ui_context=self.ui_context,
            )

        # Activate rental & hosting -> resume finishes without touching execute_raw_messages again
        flags["rented_active"] = True
        hosted["acc1"]["gifts"][GIFT_A] = {
            "nft_address": GIFT_A, "slug": "B-DayCandle-42", "title": "Candle",
            "host_user_id": 101, "visible": False, "pinned_to_top": False,
        }
        resumed = await svc.resume_rent_job(
            job_id, tonconnect_url=TC_URI, scope_ids=self.scope_ids, ui_context=self.ui_context,
            poll_timeout=5.0, poll_interval=0.0,
        )
        self.assertTrue(resumed["ok"])
        self.assertEqual("visible_verified", resumed["state"])
        self.assertTrue(hosted["acc1"]["gifts"][GIFT_A]["visible"])
        self.assertEqual(1, flags["tonconnect_calls"])
        self.assertEqual(1, uncertain_wallet.execute_raw_messages.await_count)

    async def test_handoff_required_extend_and_cancel_paths(self):
        state_flags = {"end_time": 10_000, "cancelled": False}

        def handler(req):
            url = str(req.url)
            if "/v1/rent/my-rented/" in url:
                rows = [] if state_flags["cancelled"] else [{
                    "nft_address": GIFT_A, "nft_name": "Candle #42", "duration": 2,
                    "end_time": state_flags["end_time"], "price_per_day": "500000000", "is_sellable_during_rent": False,
                }]
                return httpx.Response(200, content=json.dumps({"cursor": None, "items": rows}).encode())
            if "/extend/" in url or "/cancel/" in url or "/pay/" in url:
                return httpx.Response(200, content=_tx_body(valid_until=2_500))
            raise AssertionError(url)

        reader, rentals = self._make_services(http_handler=handler)
        svc = RentOrchestratorService(
            self.cfg, rentals=rentals, reader=reader,
            ton_reader=SimpleNamespace(read=AsyncMock()),
            token_source=lambda: "synthetic-token",
            clock=lambda: self.now, sleep=AsyncMock(),
        )

        # Handoff flow when wallet_x is None
        q_pay = await svc.prepare_rent_pipeline(
            operation="pay", nft_address=GIFT_B, asset_kind="username", duration_days=2,
            price_per_day="500000000", account_id="acc2", nft_name="@host_two",
            scope_ids=self.scope_ids, ui_context=self.ui_context,
        )
        handoff = await svc.execute_rent_pipeline(
            q_pay["job_id"], review_token=q_pay["review_token"], confirmation=q_pay["confirmation_text"],
            wallet_x=None, scope_ids=self.scope_ids, ui_context=self.ui_context,
        )
        self.assertEqual("handoff_required", handoff["state"])
        with self.assertRaisesRegex(CatalogError, "external_tx_submitted"):
            await svc.resume_rent_job(q_pay["job_id"], external_tx_submitted=False, scope_ids=self.scope_ids, ui_context=self.ui_context)

        # Extend + Cancel with fake WalletX (test duration_days convenience)
        good_wx = SimpleNamespace(
            preview_raw_messages=AsyncMock(return_value={"review_token": "wx-r", "expected_confirmation": "OK"}),
            execute_raw_messages=AsyncMock(return_value={"boc_hash": "ab" * 32, "id": "wx1"}),
        )
        q_ext = await svc.prepare_rent_pipeline(
            operation="extend", nft_address=GIFT_A, asset_kind="gift", duration_days=1,
            price_per_day="500000000", account_id="acc1", nft_name="Candle #42",
            scope_ids=self.scope_ids, ui_context=self.ui_context,
        )
        self.assertEqual(86_400, q_ext["duration"])
        state_flags["end_time"] = 96_400
        ext_res = await svc.execute_rent_pipeline(
            q_ext["job_id"], review_token=q_ext["review_token"], confirmation=q_ext["confirmation_text"],
            wallet_x=good_wx, scope_ids=self.scope_ids, ui_context=self.ui_context,
            poll_timeout=5.0, poll_interval=0.0,
        )
        self.assertEqual("extended_verified", ext_res["state"])

        q_can = await svc.prepare_rent_pipeline(
            operation="cancel", nft_addresses=[GIFT_A], asset_kind="gift",
            nft_name="Candle #42", scope_ids=self.scope_ids, ui_context=self.ui_context,
        )
        state_flags["cancelled"] = True
        can_res = await svc.execute_rent_pipeline(
            q_can["job_id"], review_token=q_can["review_token"], confirmation=q_can["confirmation_text"],
            wallet_x=good_wx, scope_ids=self.scope_ids, ui_context=self.ui_context,
            poll_timeout=5.0, poll_interval=0.0,
        )
        self.assertEqual("cancelled_verified", can_res["state"])


if __name__ == "__main__":
    unittest.main()
