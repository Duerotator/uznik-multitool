"""Unit tests for TEP-0003 Rotation Mnemonic, TdE2E PhraseShares, auto-provisioning, and carousel financial resume."""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.models import AccountRecord
from modules.finance_money import MoneyService
from modules.financial_vault import FinancialVault
from modules.money_carousel import MoneyCarousel
from modules.money_carousel_run import MoneyCarouselRunner
from modules.money_carousel_workspace import CarouselMoneyWorkspace
from modules.money_funding import CarouselFunding
from modules.money_policy import make_policy
from modules.money_transit import claim_funding, setup
from modules.money_workspace import NativeMoneyWorkspace
from modules.wallet_tg_custody import (
    PROOF_CHALLENGE,
    SECRET_PHRASE,
    STATE,
    WalletCustodyError,
    combine_phrase_shares,
    decode_proof_challenge,
    decrypt_phrase_share,
    derive_wallet_identity,
    encrypt_phrase_share,
    export_secret_phrase_on_telegram,
    generate_rotation_mnemonic,
    i32,
    parse_rotation_mnemonic,
    parse_seed_or_mnemonic,
    public_key_hex,
    replace_wallet_on_telegram,
    sign_ownership_proof,
    split_phrase_shares,
    tl_string,
    u32,
    wordlist,
)


class FakeProtector:
    name = 'test_dpapi'

    def protect(self, data: bytes, entropy: bytes) -> bytes:
        return bytes(b ^ entropy[i % len(entropy)] for i, b in enumerate(data))

    def unprotect(self, data: bytes, entropy: bytes) -> bytes:
        return self.protect(data, entropy)


class WalletTgCustodyTests(unittest.IsolatedAsyncioTestCase):
    def test_wordlist_and_rotation_mnemonic_12_and_24_words(self):
        words = wordlist()
        self.assertEqual(len(words), 2048)
        w12 = generate_rotation_mnemonic(rotated=False)
        self.assertEqual(len(w12), 12)
        parsed12 = parse_rotation_mnemonic(' '.join(f'{i+1}. {w}' for i, w in enumerate(w12)))
        self.assertEqual(parsed12['words'], w12)
        self.assertFalse(parsed12['rotated'])
        self.assertEqual(parsed12['anchor_public_key'], parsed12['signing_public_key'])

        w24 = generate_rotation_mnemonic(rotated=True)
        self.assertEqual(len(w24), 24)
        parsed24 = parse_seed_or_mnemonic(' '.join(w24))
        self.assertEqual(parsed24['kind'], 'mnemonic24')
        self.assertTrue(parsed24['rotated'])
        self.assertNotEqual(parsed24['anchor_public_key'], parsed24['signing_public_key'])

        # Corrupt checksum must raise WalletCustodyError
        bad = list(w12)
        bad[-1] = words[(words.index(bad[-1]) + 1) % len(words)]
        with self.assertRaises(WalletCustodyError):
            parse_rotation_mnemonic(bad)

    def test_tde2e_phrase_shares_roundtrip(self):
        words = generate_rotation_mnemonic(rotated=True)
        shares = split_phrase_shares(words, count=3)
        self.assertEqual(len(shares), 3)
        holder_seed = secrets.token_bytes(32)
        holder_pub = bytes.fromhex(public_key_hex(holder_seed))
        encrypted = [encrypt_phrase_share(holder_pub, s) for s in shares]
        decrypted = [decrypt_phrase_share(holder_seed, e) for e in encrypted]
        recovered = combine_phrase_shares(decrypted)
        self.assertEqual(recovered, words)

    async def test_export_and_replace_wallet_on_telegram_wire(self):
        words = generate_rotation_mnemonic()
        info = derive_wallet_identity(words, network='mainnet')

        async def fake_invoke_export(req):
            # Read exported temp public key from request wire
            wire = req.write()
            # u32(EXPORT_SECRET_PHRASE) + u32(INPUT_CHECK_PASSWORD_EMPTY) + tl_bytes(pub32)
            temp_pub = wire[9:41]
            shares = split_phrase_shares(words, count=2)
            parts = [encrypt_phrase_share(temp_pub, s) for s in shares]
            return SimpleNamespace(ID=SECRET_PHRASE, CONSTRUCTOR_ID=SECRET_PHRASE, parts=parts)

        exported = await export_secret_phrase_on_telegram(fake_invoke_export, 'pyrogram', network='mainnet')
        self.assertEqual(exported['words'], words)
        self.assertEqual(exported['address'], info['address'])
        self.assertEqual(exported['signing_public_key'], info['signing_public_key'])

        calls = []

        async def fake_invoke_replace(req):
            calls.append(req.ID)
            if req.ID == 0x2025E697:
                raw = u32(PROOF_CHALLENGE) + tl_string('challenge-payload') + i32(300) + tl_string('wallet.t.me')
                ch = decode_proof_challenge(raw)
                return SimpleNamespace(ID=PROOF_CHALLENGE, CONSTRUCTOR_ID=PROOF_CHALLENGE, **ch)
            return SimpleNamespace(
                ID=STATE,
                CONSTRUCTOR_ID=STATE,
                address=info['address'],
                public_key=bytes.fromhex(info['signing_public_key']),
                balance=0,
                backup_enabled=False,
                can_export_phrase=False,
            )

        state = await replace_wallet_on_telegram(
            fake_invoke_replace,
            'pyrogram',
            signing_seed=info['signing_seed'],
            signing_public_key=info['signing_public_key'],
            anchor_public_key=info['anchor_public_key'],
            address=info['address'],
            network='mainnet',
        )
        self.assertEqual(calls, [0x2025E697, 0xD8C72EEC])
        self.assertEqual(state['availability'], 'ready')
        self.assertEqual(state['address'], info['address'])

    async def test_workspace_import_mnemonic_and_auto_provision(self):
        with tempfile.TemporaryDirectory() as tmp:
            accounts = {
                'acc1': AccountRecord('acc1', 'acc1', 'pyrogram', 'session_file', 'acc1.session', user_id=101, username='user1'),
            }
            money = MoneyService(
                SimpleNamespace(data_dir=Path(tmp)),
                accounts=SimpleNamespace(get_account=accounts.__getitem__),
                clock=lambda: 1000,
            )
            vault = FinancialVault(Path(tmp), protector=FakeProtector())

            @asynccontextmanager
            async def fake_factory(_cfg, account):
                class FakeClient:
                    async def get_me(self_inner):
                        return {'user_id': account.user_id}

                    async def native_money_state(self_inner):
                        return {'availability': 'missing', 'address': None, 'balance_nano': None}

                    async def native_money_replace_wallet(self_inner, **kw):
                        return {
                            'availability': 'ready',
                            'address': kw['address'],
                            'public_key': kw['signing_public_key'],
                            'balance_nano': '0',
                            'balance_native': '0',
                            'backup_enabled': False,
                            'can_export_phrase': False,
                            'verification': 'telegram_reported',
                        }

                yield FakeClient()

            ws = NativeMoneyWorkspace(money, vault=vault, client_factory=fake_factory)
            scope = {'acc1'}
            ui = 'b' * 64

            # 1) Direct mnemonic import
            words = generate_rotation_mnemonic()
            rep = ws.import_mnemonic('acc1', ' '.join(words), scope_ids=scope, ui_context=ui, confirmed=True)
            self.assertIsNotNone(rep['key'])

            # 2) Auto-provision new wallet on Telegram
            res = await ws.auto_sync_or_provision_wallet(
                'acc1',
                replace_existing=True,
                scope_ids=scope,
                ui_context=ui,
                confirmed=True,
            )
            self.assertEqual(res['custody_mode'], 'provisioned_new_on_telegram')
            self.assertEqual(len(res['mnemonic_phrase'].split()), 12)
            self.assertEqual(res['key']['address'], res['provisioned_address'])
            self.assertTrue(res['friendly_address'].startswith('EQ'))
            self.assertTrue(res['non_bounceable_address'].startswith('UQ'))

            # 3) Timeout / dead proxy fallback to local WalletTg
            @asynccontextmanager
            async def timeout_factory(_cfg, _account):
                raise TimeoutError('simulated dead proxy timeout')
                yield  # pragma: no cover

            ws_timeout = NativeMoneyWorkspace(money, vault=vault, client_factory=timeout_factory)
            fallback_res = await ws_timeout.auto_sync_or_provision_wallet(
                'acc1',
                replace_existing=True,
                scope_ids=scope,
                ui_context=ui,
                confirmed=True,
            )
            self.assertEqual(fallback_res['custody_mode'], 'provisioned_local_wallet_tg')
            self.assertTrue(fallback_res['non_bounceable_address'].startswith('UQ'))

            # 4) Batch provisioning across multiple accounts + skipping missing user_id
            accounts['acc2'] = AccountRecord('acc2', 'acc2', 'pyrogram', 'session_file', 'acc2.session', user_id=102, username='user2')
            accounts['acc_no_uid'] = AccountRecord('acc_no_uid', 'acc_no_uid', 'pyrogram', 'session_file', 'acc3.session', user_id=None, username='user3')
            batch_res = await ws_timeout.batch_provision_wallets(
                ['acc1', 'acc2', 'acc_no_uid'],
                replace_existing=False,
                online_sync=False,
                scope_ids={'acc1', 'acc2', 'acc_no_uid'},
                ui_context=ui,
                confirmed=True,
            )
            self.assertEqual(batch_res['total'], 3)
            self.assertEqual(batch_res['already_configured'], 1)
            self.assertEqual(batch_res['created'], 1)
            self.assertEqual(len(batch_res['skipped']), 1)
            self.assertEqual(batch_res['skipped'][0]['account_id'], 'acc_no_uid')
            self.assertTrue(Path(batch_res['txt_path']).is_file())

    async def test_carousel_resume_after_interruption_without_double_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = 1000
            sent = []
            reads = []
            accounts = {
                key: AccountRecord(key, key, 'pyrogram', 'session_file', f'{key}.session', user_id=i, username=f'{key}user')
                for i, key in enumerate(('a', 'b', 'x'), 1)
            }
            fail_first_reconcile = True

            async def reconcile(intent):
                nonlocal fail_first_reconcile
                reads.append(intent['id'])
                if fail_first_reconcile:
                    fail_first_reconcile = False
                    return {
                        'status': 'recipient_pending',
                        'provider': 'toncenter_v3',
                        'verification': 'provider_reported_not_chain_proof',
                        'automatic_retry': False,
                    }
                return {
                    'status': 'provider_delivered',
                    'provider': 'toncenter_v3',
                    'verification': 'provider_reported_not_chain_proof',
                    'automatic_retry': False,
                    'source_tx': 'e' * 64,
                    'recipient_tx': intent['id'] * 2,
                    'message_hash': intent['id'] * 2,
                    'recipient_state': 'active',
                    'recipient_lt': str(100 + len(sent)),
                    'transit_accounting': 'provider_reported_net_not_spend_authorized',
                    'incoming_value_nano': intent['amount'],
                    'recipient_before_nano': '0',
                    'recipient_after_nano': str(int(intent['amount']) - 1000000),
                    'recipient_net_nano': str(int(intent['amount']) - 1000000),
                    'recipient_fee_nano': '1000000',
                }

            money = MoneyService(
                SimpleNamespace(data_dir=Path(tmp)),
                accounts=SimpleNamespace(get_account=accounts.__getitem__),
                clock=lambda: now,
                reconciliation=reconcile,
            )
            scope = set(accounts)
            ui = 'a' * 64
            nodes = [{'account_id': k, 'address': '0:' + str(i) * 64} for i, k in enumerate(accounts, 1)]
            funding = CarouselFunding(money)
            report = funding.create(list(accounts), scope_ids=scope, ui_context=ui, confirmed=True)
            funding_ref = {k: report[k] for k in ('id', 'revision')}
            event = {
                'tx_hash': 'c' * 64,
                'message_hash': 'd' * 64,
                'lt': '1',
                'source': '0:' + '4' * 64,
                'address': nodes[0]['address'],
                'net_credit_nano': '300000000',
                'previous_balance_nano': '0',
                'recipient_state': 'active',
                'verification': 'provider_reported_not_chain_proof',
            }
            with money.db() as db:
                setup(db)
                db.execute(
                    "UPDATE money_funding_drafts SET state='funding_observed',address=?,event=? WHERE id=?",
                    (event['address'], json.dumps(event), report['id']),
                )
                db.execute(
                    'INSERT INTO money_funding_refs VALUES(?,?,?,?,?)',
                    (event['message_hash'], event['tx_hash'], report['id'], event['address'], '1'),
                )
            carousel = MoneyCarousel(money)
            plan = carousel.create(nodes, network='mainnet', scope_ids=scope, ui_context=ui, confirmed=True)
            credentials = {}
            for i, (before, after) in enumerate(zip(nodes, nodes[1:]), 1):
                key = before['account_id']
                context, _ = money.scope(key, scope, ui)
                policy = make_policy(
                    account=key,
                    account_context=context,
                    ui_context=ui,
                    network='mainnet',
                    source=before['address'],
                    targets=[after['address']],
                    scope='transit_only',
                    reserve_nano='0',
                    fee_cap_nano='10000000',
                    transfer_debit_cap_nano='1000000000',
                    daily_reservation_cap_nano='2000000000',
                    confirmed=True,
                )
                identity = {
                    'account': key,
                    'user_id': accounts[key].user_id,
                    'context': context,
                    'network': 'mainnet',
                    'address': before['address'],
                    'public_key': str(i) * 64,
                    'revision': str(i) * 32,
                }
                credentials[key] = {'policy': policy, 'identity': identity, 'anchor': str(i) * 64}
            inventory = {
                'exists': True,
                'locked_in_process': False,
                'records': [{**v['identity'], 'scheme': 'ed25519-seed32', 'version': 1} for v in credentials.values()],
            }

            async def execute(policy, identity, **kwargs):
                step = kwargs['carousel_step']
                ident = uuid.uuid4().hex
                with money.db() as db:
                    snapshot = step.snapshot(money, db, policy, kwargs['target_account_id'], scope, ui)
                    db.execute(
                        'INSERT INTO intents(id,account,context,ui_context,network,source,destination,amount,created,expires,state,prompted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (
                            ident,
                            policy.account,
                            policy.account_context,
                            ui,
                            'mainnet',
                            policy.source,
                            policy.targets[0],
                            str(int(snapshot['net_nano']) - 1000000),
                            now,
                            int(now) + 180,
                            'wallet_submitted_unverified',
                            now,
                        ),
                    )
                    step.link(db, ident)
                    claim_funding(db, snapshot, ident)
                sent.append(ident)
                return {'id': ident, 'state': 'wallet_submitted_unverified'}

            executor = SimpleNamespace(
                money=money,
                allow=lambda: True,
                execute=execute,
                vault=SimpleNamespace(status=lambda: inventory),
                codec=SimpleNamespace(derive=lambda key, network: {'source': '0:' + key}),
            )
            async def advance_sleep(seconds):
                nonlocal now
                now += seconds

            runner = MoneyCarouselRunner(carousel, executor, sleep=advance_sleep)
            runner.READ_ATTEMPTS = 1

            # Initial run sends hop 0, then pauses because READ_ATTEMPTS=1 saw recipient_pending
            with self.assertRaises(Exception):
                await runner.run(
                    plan['id'],
                    plan['revision'],
                    funding_ref=funding_ref,
                    credentials=credentials,
                    scope_ids=scope,
                    ui_context=ui,
                    confirmation='RUN ' + plan['id'],
                )
            self.assertEqual(len(sent), 1)
            snap = carousel.snapshot(plan['id'], scope_ids=scope, ui_context=ui)
            self.assertEqual(snap['state'], 'paused')

            # Now resume: reconciles hop 0 to provider_delivered, executes hop 1, and marks completed!
            resumed = await runner.resume(
                plan['id'],
                snap['revision'],
                funding_ref=funding_ref,
                credentials=credentials,
                scope_ids=scope,
                ui_context=ui,
                confirmation='RESUME ' + plan['id'],
            )
            self.assertEqual(resumed['state'], 'completed')
            self.assertEqual(resumed['completed_hops'], 2)
            self.assertEqual(resumed['resumed_from'], 1)
            self.assertEqual(len(sent), 2)


if __name__ == '__main__':
    unittest.main()
