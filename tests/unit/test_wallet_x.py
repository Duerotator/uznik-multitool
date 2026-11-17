import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / "app", _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Offline regression tests for Master Wallet X, bulk codec, Rent funding & Carousel X-to-X."""

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import httpx

import test_money_carousel_run as carousel_fixtures
from core.config import AppConfig
from core.models import AccountRecord
from core.storage import write_json_atomic
from core.ton_address import friendly_address
from modules.asset_registry import AssetStore
from modules.finance_money import MoneyService
from modules.financial_vault import FinancialVault
from modules.money_carousel import CarouselError, MoneyCarousel
from modules.money_carousel_run import MoneyCarouselRunner
from modules.money_carousel_workspace import CarouselMoneyWorkspace
from modules.money_funding import CarouselFunding
from modules.money_transit import setup as setup_transit
from modules.wallet_tg_codec import WalletCodecError, WalletTgCodec
from modules.wallet_x import WalletXError, WalletXService, WalletXVault
from test_wallet_tg_codec import AVAILABLE, Protector


DEST_A = "0:" + "11" * 32
DEST_B = "0:" + "22" * 32
DEST_C = "0:" + "33" * 32
DEST_D = "0:" + "44" * 32
DEST_E = "0:" + "55" * 32
PAY_BOC = "te6cckEBAQEABgAACBI0Vniu2sgE"
STATE_INIT_BOC = "te6cckEBAgEABwACATQBAQAAw3VFVg=="


@unittest.skipUnless(AVAILABLE, "Pinned Node/TON codec runtime not installed")
class WalletXVaultAndCodecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_000
        self.codec = WalletTgCodec(clock=lambda: self.now)

    def test_vault_isolation_mnemonic_lock_and_delete(self):
        data_dir = Path(self.temp.name) / "data"
        prot = Protector()
        wx = WalletXVault(data_dir, protector=prot)
        fv = FinancialVault(data_dir, protector=prot)
        fv.create(confirmed=True)

        self.assertFalse(wx.status()["configured"])
        with self.assertRaises(WalletXError):
            wx.create_wallet(confirmed=False, codec=self.codec)

        created = wx.create_wallet(confirmed=True, label="Main X", codec=self.codec)
        self.assertTrue(created["configured"])
        self.assertTrue(created["has_mnemonic"])
        self.assertEqual("Main X", created["wallet"]["label"])
        phrase = created["mnemonic_phrase"]
        self.assertEqual(12, len(phrase.split()))
        self.assertEqual(phrase, wx.export_mnemonic(confirmed=True))
        with self.assertRaises(WalletXError):
            wx.export_mnemonic(confirmed=False)

        # FinancialVault stays completely untouched & independent
        self.assertEqual([], fv.status()["records"])
        self.assertTrue((data_dir / "financial_keys" / "wallet_x.sqlite3").is_file())
        self.assertTrue((data_dir / "financial_keys" / "vault.sqlite3").is_file())

        # Lock/unlock & use_seed wiping
        wx.lock()
        self.assertTrue(wx.status()["locked_in_process"])
        with self.assertRaises(WalletXError):
            wx.export_mnemonic(confirmed=True)
        with self.assertRaises(WalletXError):
            with wx.use_seed(revision=created["wallet"]["revision"], public_key=created["wallet"]["public_key"]):
                pass
        wx.unlock(confirmed=True)
        with wx.use_seed(revision=created["wallet"]["revision"], public_key=created["wallet"]["public_key"]) as mv:
            self.assertEqual(32, len(mv))
            kept = bytes(mv)
            self.assertNotEqual(bytes(32), kept)

        # Prevent silent overwrite
        with self.assertRaises(WalletXError):
            wx.import_wallet(phrase, confirmed=True, replace_existing=False, codec=self.codec)

        # Seed32 import clears mnemonic entry cleanly
        imp = wx.import_wallet(os.urandom(32).hex(), confirmed=True, replace_existing=True, label="Imported Seed", codec=self.codec)
        self.assertFalse(imp["has_mnemonic"])
        with self.assertRaises(WalletXError):
            wx.export_mnemonic(confirmed=True)

        rev = imp["wallet"]["revision"]
        with self.assertRaises(WalletXError):
            wx.delete_wallet(confirmation="WRONG", confirmed=True)
        deleted = wx.delete_wallet(confirmation=f"DELETE WALLET X {rev[:8]}", confirmed=True)
        self.assertFalse(deleted["configured"])

    def test_codec_bulk_0x73896e75_5_messages_drain_and_guards(self):
        seed = os.urandom(32)
        pub = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw().hex()
        source = self.codec.derive(pub, "mainnet")["source"]

        # 5 messages -> SendBulkMessagesRequestE (0x73896e75) with payload_b64/state_init_b64
        msgs = [
            {"destination": DEST_A, "amount_nano": "100000000", "mode": 3, "bounce": False, "comment": "rent:1"},
            {"destination": DEST_B, "amount_nano": "150000000", "mode": 3, "bounce": True, "payload_b64": PAY_BOC},
            {"destination": DEST_C, "amount_nano": "200000000", "mode": 3, "bounce": True, "payload_b64": PAY_BOC, "state_init_b64": STATE_INIT_BOC},
            {"destination": DEST_D, "amount_nano": "250000000", "mode": 3, "bounce": False},
            {"destination": DEST_E, "amount_nano": "300000000", "mode": 3, "bounce": False},
        ]
        emu = self.codec.wallet_x_emulate(
            network="mainnet", anchor_public_key=pub, seqno=0, valid_until=self.now + 120,
            messages=msgs, deploy=True,
        )
        self.assertFalse(emu["codec"]["signature_valid"])
        self.assertFalse(emu["codec"]["broadcastable"])

        prep = self.codec.wallet_x_prepare(
            network="mainnet", anchor_public_key=pub, seqno=0, valid_until=self.now + 120,
            messages=msgs, deploy=True,
        )
        sig = Ed25519PrivateKey.from_private_bytes(seed).sign(bytes.fromhex(prep["codec"]["payload_hash"]))
        attached = self.codec.wallet_x_attach(request=prep["request"], signature_hex=sig.hex(), signing_public_key=pub)
        inspected = self.codec.inspect_external(external_boc=attached["external_boc"], signing_public_key=pub, network="mainnet")
        self.assertTrue(inspected["signature_valid"])
        self.assertEqual(source, inspected["source"])
        self.assertEqual(0, inspected["seqno"])

        # Drain mode 130 with deploy requires allow_deploy_drain=True
        with self.assertRaises(WalletCodecError):
            self.codec.wallet_x_prepare(
                network="mainnet", anchor_public_key=pub, seqno=0, valid_until=self.now + 120,
                messages=[{"destination": DEST_A, "amount_nano": "0", "mode": 130, "bounce": False}],
                deploy=True, allow_deploy_drain=False,
            )
        drain_prep = self.codec.wallet_x_prepare(
            network="mainnet", anchor_public_key=pub, seqno=0, valid_until=self.now + 120,
            messages=[{"destination": DEST_A, "amount_nano": "0", "mode": 130, "bounce": False}],
            deploy=True, allow_deploy_drain=True,
        )
        drain_sig = Ed25519PrivateKey.from_private_bytes(seed).sign(bytes.fromhex(drain_prep["codec"]["payload_hash"]))
        drain_att = self.codec.wallet_x_attach(request=drain_prep["request"], signature_hex=drain_sig.hex(), signing_public_key=pub)
        self.assertTrue(self.codec.inspect_external(external_boc=drain_att["external_boc"], signing_public_key=pub, network="mainnet")["signature_valid"])

        # Reject accmanager: prefix, comment+payload, self-send, and >16 messages
        for bad_msgs in (
            [{"destination": DEST_A, "amount_nano": "100000000", "mode": 3, "bounce": False, "comment": "accmanager:bad"}],
            [{"destination": DEST_A, "amount_nano": "100000000", "mode": 3, "bounce": False, "comment": "hi", "payload_b64": PAY_BOC}],
            [{"destination": source, "amount_nano": "100000000", "mode": 3, "bounce": False}],
            [{"destination": DEST_A, "amount_nano": "100000000", "mode": 3, "bounce": False} for _ in range(17)],
        ):
            with self.assertRaises(WalletCodecError):
                self.codec.wallet_x_prepare(
                    network="mainnet", anchor_public_key=pub, seqno=1, valid_until=self.now + 120,
                    messages=bad_msgs,
                )


@unittest.skipUnless(AVAILABLE, "Pinned Node/TON codec runtime not installed")
class WalletXServiceOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_000.0
        self.env_patch = patch.dict(os.environ, {
            "TELEGRAM_DATA_DIR": str(Path(self.temp.name) / "data"),
            "TELEGRAM_IMPORT_DIR": str(Path(self.temp.name) / "imports"),
        })
        self.addCleanup(self.env_patch.stop)
        self.env_patch.start()
        self.cfg = AppConfig.load(Path(self.temp.name) / ".env")
        self.codec = WalletTgCodec(clock=lambda: int(self.now))
        self.prot = Protector()
        self.vault = WalletXVault(self.cfg.data_dir, protector=self.prot)
        self.created = self.vault.create_wallet(confirmed=True, codec=self.codec)
        self.wx_addr = self.created["wallet"]["address"]
        self.wx_pub = self.created["wallet"]["public_key"]

        self.chain = {
            "balance_nano": "2000000000",
            "seqno": 1,
            "active": True,
            "recipient_state": "uninit",
            "tx_rows": [],
            "broadcasts": [],
            "last_msgs": [],
        }
        orig_emulate = self.codec.wallet_x_emulate

        def tracked_emulate(**kwargs):
            self.chain["last_msgs"] = list(kwargs["messages"])
            return orig_emulate(**kwargs)

        self.codec.wallet_x_emulate = tracked_emulate

        async def http_handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "/api/emulate/v1/emulateTrace" in url:
                msgs = self.chain["last_msgs"]
                out_msgs = [{"value": m["amount_nano"]} for m in msgs]
                send_all = any(m["mode"] == 130 for m in msgs)
                if send_all:
                    bal_before = int(self.chain["balance_nano"])
                    bal_after = 0
                    out_msgs = [{"value": str(bal_before - 5_000_000)}]
                else:
                    bal_before = int(self.chain["balance_nano"])
                    out_sum = sum(int(m["value"]) for m in out_msgs)
                    bal_after = bal_before - out_sum - 5_000_000
                return httpx.Response(200, json={
                    "trace": {
                        "transaction": {
                            "account": self.wx_addr,
                            "description": {
                                "aborted": False, "destroyed": False,
                                "compute_ph": {"skipped": False, "success": True, "exit_code": 0},
                                "action": {"success": True, "result_code": 0},
                            },
                            "account_state_before": {"balance": str(bal_before), "account_status": "active" if self.chain["active"] else "uninit"},
                            "account_state_after": {"balance": str(bal_after), "account_status": "active"},
                            "out_msgs": out_msgs,
                        },
                        "children": [{"transaction": {"account_state_before": {"account_status": self.chain["recipient_state"]}}}],
                    }
                })
            if "/api/v2/sendBoc" in url:
                self.chain["broadcasts"].append(json.loads(req.content)["boc"])
                return httpx.Response(200, json={"ok": True, "result": {"@type": "ok"}})
            if "/api/v3/transactions" in url:
                return httpx.Response(200, json={"transactions": self.chain["tx_rows"]})
            return httpx.Response(404, json={"ok": False})

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(http_handler))
        self.addAsyncCleanup(self.http.aclose)

        self.reader = SimpleNamespace(read=AsyncMock(side_effect=self._read_account))
        self.wallet_reader = SimpleNamespace(read=AsyncMock(side_effect=self._read_wallet))
        self.svc = WalletXService(
            self.cfg, vault=self.vault, reader=self.reader, wallet_reader=self.wallet_reader,
            codec=self.codec, clock=lambda: self.now, http_client=self.http,
        )

    async def asyncTearDown(self):
        self.env_patch.stop()
        self.temp.cleanup()

    async def _read_account(self, address, network="mainnet"):
        return {
            "balance_nano": self.chain["balance_nano"] if address == self.wx_addr else "100000000",
            "balance_native": "2" if address == self.wx_addr else "0.1",
            "chain_state": "active",
            "block_seqno": 500,
            "provider": "toncenter",
            "source_sync_time": self.now,
        }

    async def _read_wallet(self, address, network, anchor_public_key, *, confirmed=False, allow_deployment=False):
        return {
            "address": address,
            "network": network,
            "public_key": self.wx_pub,
            "seqno": self.chain["seqno"],
            "active": self.chain["active"],
            "balance_nano": self.chain["balance_nano"],
            "mc_block_seqno": 500,
        }

    async def test_refresh_send_all_bounce_warning_reconcile_and_recover(self):
        refreshed = await self.svc.refresh_chain(include_history=False)
        self.assertEqual("2", refreshed["balance_native"])
        self.assertEqual(1, refreshed["seqno"])

        # send_all=True, bounce=True against uninit dest produces explicit warning
        prev = await self.svc.preview_external_send(DEST_A, send_all=True, bounce=True, confirmed=True)
        self.assertIn("bounce=True", prev["warning"])
        self.assertTrue(prev["expected_confirmation"].startswith("DRAIN ALL TO "))

        res = await self.svc.execute_reviewed(prev["review_token"], confirmation=prev["expected_confirmation"])
        self.assertEqual("submitted_unverified", res["state"])
        self.assertEqual(1, len(self.chain["broadcasts"]))

        # Verify zero 'accmanager:' comment and signed seqno matches
        inspected = self.codec.inspect_external(external_boc=self.chain["broadcasts"][0], signing_public_key=self.wx_pub, network="mainnet")
        self.assertTrue(inspected["signature_valid"])
        self.assertNotIn("accmanager:", json.dumps(inspected))

        # New action blocked while intent unresolved
        with self.assertRaisesRegex(WalletXError, "Unresolved Wallet X intent"):
            await self.svc.preview_external_send(DEST_A, amount_ton="0.1", confirmed=True)

        # Reconcile to provider_delivered using synthetic toncenter v3 row
        self.chain["seqno"] = 2
        self.chain["tx_rows"] = [{
            "account": self.wx_addr,
            "hash": "aa" * 32,
            "lt": "42",
            "emulated": False,
            "finality": "finalized",
            "description": {"type": "ord", "aborted": False, "compute_ph": {"skipped": False, "success": True, "exit_code": 0}, "action": {"success": True, "valid": True, "no_funds": False, "result_code": 0}},
            "out_msgs": [{"destination": DEST_A, "value": prev["amount_nano"], "bounced": False}],
        }]
        rec = await self.svc.reconcile_intent(res["id"])
        self.assertEqual("provider_delivered", rec["state"])

        # Now another intent works; simulate stale intent recovery after expiry + 60s
        prev2 = await self.svc.preview_external_send(DEST_B, amount_ton="0.2", confirmed=True)
        res2 = await self.svc.execute_reviewed(prev2["review_token"], confirmation=prev2["expected_confirmation"])
        with self.assertRaisesRegex(WalletXError, "expiry \\+ 60s"):
            await self.svc.recover_stale_intent(res2["id"], confirmation=f"RECOVER {res2['id']}")
        self.now += 500
        recovered = await self.svc.recover_stale_intent(res2["id"], confirmation=f"RECOVER {res2['id']}")
        self.assertEqual("expired_unused_nonce_observed", recovered["state"])

    async def test_raw_messages_batch_fund_and_carousel_funding(self):
        # Raw multi-message path (MarketApp style friendly EQ + nanoton strings)
        eq_a = friendly_address(DEST_A, "mainnet", bounceable=True)
        raw_prev = await self.svc.preview_raw_messages(
            [{"address": eq_a, "amount": "150000000", "payload": PAY_BOC, "stateInit": STATE_INIT_BOC}],
            confirmed=True,
        )
        self.assertEqual(1, raw_prev["count"])
        raw_res = await self.svc.execute_raw_messages(raw_prev["review_token"], confirmation=raw_prev["expected_confirmation"])
        self.assertEqual("submitted_unverified", raw_res["state"])
        # Clear unresolved before batch
        with self.svc.db() as db:
            db.execute("UPDATE wallet_x_intents SET state='provider_delivered', seqno=NULL WHERE id=?", (raw_res["id"],))

        # Register 3 accounts in AssetStore: a1 has 0.1 TON (needs 0.4), a2 already funded 0.6, a3 no address
        assets = AssetStore(self.cfg.data_dir, clock=lambda: self.now)
        assets.register("a1", "mainnet", DEST_A, "manual", confirmed=True)
        assets.register("a2", "mainnet", DEST_B, "manual", confirmed=True)

        async def per_acc_read(address, network="mainnet"):
            bal = "100000000" if address == DEST_A else ("600000000" if address == DEST_B else self.chain["balance_nano"])
            return {"balance_nano": bal, "balance_native": bal, "chain_state": "active", "block_seqno": 500, "provider": "toncenter", "source_sync_time": self.now}

        self.reader.read = AsyncMock(side_effect=per_acc_read)
        batch_prev = await self.svc.preview_batch_fund(
            ["a1", "a2", "a3"], mode="top_up_to", target_balance_ton="0.5",
            scope_ids={"a1", "a2", "a3"}, ui_context="x" * 64, confirmed=True,
        )
        self.assertEqual(1, len(batch_prev["items"]))
        self.assertEqual("400000000", batch_prev["items"][0]["amount_nano"])
        self.assertEqual({"a2", "a3"}, {s["account_id"] for s in batch_prev["skipped"]})

        # Simulate chain seqno advancing on post-broadcast poll
        orig_read_wallet = self._read_wallet
        reads = {"n": 0}
        async def advancing_wallet_read(address, network, anchor, *, confirmed=False, allow_deployment=False):
            reads["n"] += 1
            res = await orig_read_wallet(address, network, anchor, confirmed=confirmed, allow_deployment=allow_deployment)
            if not allow_deployment:
                res["seqno"] = self.chain["seqno"] + 1
            return res

        self.wallet_reader.read = AsyncMock(side_effect=advancing_wallet_read)
        events = []
        batch_res = await self.svc.execute_batch_fund(
            batch_prev["review_token"], confirmation=batch_prev["expected_confirmation"],
            progress=events.append, wait_confirm=True, poll_timeout=2.0,
        )
        self.assertEqual("completed", batch_res["state"])
        self.assertEqual(1, batch_res["confirmed"])
        self.assertEqual(["sending", "item_done"], [e["phase"] for e in events])

        # Carousel funding draft from Wallet X -> node[0] using clean empty comment
        with self.svc.db() as db:
            db.execute("UPDATE wallet_x_intents SET state='provider_delivered', seqno=NULL")
        self.chain["seqno"] = 3
        self.wallet_reader.read = AsyncMock(side_effect=self._read_wallet)

        accs = {
            k: AccountRecord(k, k, "pyrogram", "session_file", f"{k}.session", user_id=i, username=f"{k}_user")
            for i, k in enumerate(("a", "b"), 1)
        }
        write_json_atomic(self.cfg.accounts_file, {"accounts": [a.to_dict() for a in accs.values()]})
        money = MoneyService(self.cfg, clock=lambda: self.now)
        with money.db() as db:
            setup_transit(db)
        assets.register("a", "mainnet", DEST_C, "manual", confirmed=True)
        funding = CarouselFunding(money)
        draft = funding.create(["a", "b"], scope_ids=set(accs), ui_context="f" * 64, confirmed=True)
        # Patch draft address to match node a's declared asset
        with money.db() as db:
            db.execute("UPDATE money_funding_drafts SET address=? WHERE id=?", (DEST_C, draft["id"]))
        draft = funding.report(draft["id"], scope_ids=set(accs), ui_context="f" * 64)

        cf_prev = await self.svc.preview_carousel_funding(
            draft["id"], draft["revision"], amount_ton="0.3",
            scope_ids=set(accs), ui_context="f" * 64, money_service=money, confirmed=True,
        )
        self.assertEqual("a", cf_prev["node0_account_id"])
        self.assertEqual("", cf_prev["comment"])
        cf_res = await self.svc.execute_carousel_funding(cf_prev["review_token"], confirmation=cf_prev["expected_confirmation"])
        self.assertEqual("submitted_unverified", cf_res["state"])


@unittest.skipUnless(AVAILABLE, "Pinned Node/TON codec runtime not installed")
class CarouselEndAtWalletXTests(unittest.IsolatedAsyncioTestCase):
    async def test_carousel_workspace_end_at_wallet_x_and_out_of_scope_guard(self):
        f = carousel_fixtures.RunnerTests()
        f.setUp()
        self.addCleanup(f.tearDown)

        client = SimpleNamespace(
            get_me=AsyncMock(return_value={"user_id": 1}),
            native_money_recipient=AsyncMock(return_value={"address": f.nodes[-1]["address"]}),
        )
        @asynccontextmanager
        async def factory(*args):
            yield client
        f.executor.factory = factory
        native = SimpleNamespace(money=f.money, vault=f.executor.vault, executor=f.executor)
        native._key = lambda ident, context: (f.credentials[ident]["identity"], f.credentials[ident]["anchor"])
        with f.money.db() as db:
            db.execute("DELETE FROM money_carousel_hops")
            db.execute("DELETE FROM money_carousels")
            db.execute("DELETE FROM money_carousel_claims")
            db.execute("CREATE TABLE native_key_links(account TEXT PRIMARY KEY, revision TEXT, anchor TEXT)")
            for ident, entry in f.credentials.items():
                db.execute("INSERT INTO native_key_links VALUES(?,?,?)", (ident, entry["identity"]["revision"], entry["anchor"]))

        # Provision standalone Wallet X in same data_dir
        codec = WalletTgCodec(clock=lambda: 1_000)
        wx_vault = WalletXVault(f.money.config.data_dir, protector=Protector())
        wx_created = wx_vault.create_wallet(confirmed=True, codec=codec)
        wx_addr = wx_created["wallet"]["address"]

        # Register terminal sender 'x' key since Wallet X becomes the external terminal
        from modules.money_policy import make_policy
        x_ctx, _ = f.money.scope("x", f.scope, carousel_fixtures.UI)
        x_ident = {
            "account": "x", "user_id": f.accounts["x"].user_id, "context": x_ctx,
            "network": "mainnet", "address": f.nodes[-1]["address"],
            "public_key": "3" * 64, "revision": "3" * 32,
        }
        x_pol = make_policy(
            account="x", account_context=x_ctx, ui_context=carousel_fixtures.UI,
            network="mainnet", source=x_ident["address"], targets=[wx_addr],
            scope="transit_only", reserve_nano="0", fee_cap_nano="10000000",
            transfer_debit_cap_nano="1000000000", daily_reservation_cap_nano="2000000000",
            confirmed=True,
        )
        f.credentials["x"] = {"policy": x_pol, "identity": x_ident, "anchor": "3" * 64}
        f.inventory["records"].append({**x_ident, "scheme": "ed25519-seed32", "version": 1})
        with f.money.db() as db:
            db.execute("INSERT OR REPLACE INTO native_key_links VALUES(?,?,?)", ("x", x_ident["revision"], "3" * 64))

        # Without terminal_external, __wallet_x__ is strictly rejected by MoneyCarousel (out-of-scope guard preserved!)
        with self.assertRaises(CarouselError):
            MoneyCarousel(f.money).create(
                [{"account_id": "a", "address": f.nodes[0]["address"]}, {"account_id": "__wallet_x__", "address": wx_addr}],
                network="mainnet", scope_ids=f.scope, ui_context=carousel_fixtures.UI, confirmed=True, terminal_external=None,
            )

        # With end_at_wallet_x=True, CarouselMoneyWorkspace targets __wallet_x__ -> runs to completion cleanly!
        funding = CarouselFunding(f.money)
        workspace = CarouselMoneyWorkspace(native, funding)
        workspace.runner = MoneyCarouselRunner(workspace.carousel, f.executor, sleep=f.sleep)
        review = await workspace.review(
            f.funding_ref, {"fee": "0.01", "debit": "1", "daily": "5"},
            scope_ids=f.scope, ui_context=carousel_fixtures.UI, confirmed=True, end_at_wallet_x=True,
        )
        self.assertTrue(review["end_at_wallet_x"])
        self.assertEqual("__wallet_x__", review["route"][-1]["account_id"])
        self.assertEqual(wx_addr, review["route"][-1]["address"])

        result = await workspace.run(
            review["token"], scope_ids=f.scope, ui_context=carousel_fixtures.UI, confirmation=review["confirmation"],
        )
        self.assertEqual("completed", result["state"])
        self.assertEqual(3, len(f.sent))
        with f.money.db() as db:
            last_dest = db.execute("SELECT destination FROM intents WHERE id=?", (f.sent[-1],)).fetchone()["destination"]
        self.assertEqual(wx_addr, last_dest)


if __name__ == "__main__":
    unittest.main()
