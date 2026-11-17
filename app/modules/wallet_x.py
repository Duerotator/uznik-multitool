"""Standalone DPAPI Master Wallet X controller, batch renter funder, and raw TON tx sender.

Operates independently of Telegram account sessions (account_id = None), using
isolated DPAPI storage (data/financial_keys/wallet_x.sqlite3) and WAL SQLite state
(data/wallet_x.sqlite3). Always previews/emulates before explicit confirmation.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import httpx

from core.ton_address import AddressError, friendly_address, normalize_address
from core.windows_dpapi import CurrentUserDPAPI, DPAPIError
from modules.finance_money import FinanceError, read_history
from modules.financial_vault import FinancialVaultError, _configure_sqlite, _json, _wipe
from modules.money_chain_read import MoneyReadError, TonCenterWalletReader, bounded_json
from modules.money_policy import digest, nano
from modules.money_reconciliation import ReconciliationError, _executed, _final, _hash, _page, _unsigned
from modules.ton_readonly import ReadError, TonCenterReader, toncenter_headers
from modules.wallet_tg_codec import WalletCodecError, WalletTgCodec
from modules.wallet_tg_custody import (
    _wipe as custody_wipe,
    derive_wallet_identity,
    generate_rotation_mnemonic,
    parse_seed_or_mnemonic,
)


WALLET_ID = 'wallet_x'


class WalletXError(ValueError):
    pass


def require(condition, message='Wallet X precondition failed; explicit review required'):
    if not condition:
        raise WalletXError(message)


def _amount_nano(text, *, max_ton=1_000_000, allow_zero=False):
    if isinstance(text, int) and type(text) is int:
        require(0 <= text <= max_ton * 10**9 and (allow_zero or text > 0), 'Amount out of bounded TON range')
        return str(text)
    if not isinstance(text, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,6})(\.[0-9]{1,9})?', text.strip()):
        raise WalletXError('Enter a valid decimal TON amount with at most 9 fractional digits')
    whole, _, fraction = text.strip().partition('.')
    value = int(whole) * 10**9 + int(fraction.ljust(9, '0') or '0')
    if (not allow_zero and value <= 0) or value > max_ton * 10**9:
        raise WalletXError(f'TON amount must be between 0 and {max_ton} TON')
    return str(value)


def _wallet_x_metadata(label, network, address, anchor_public_key, public_key, revision):
    if (
        not isinstance(label, str) or not 1 <= len(label.strip()) <= 80
        or network not in {'mainnet', 'testnet'}
        or not isinstance(anchor_public_key, str) or not re.fullmatch(r'[a-f0-9]{64}', anchor_public_key)
        or not isinstance(public_key, str) or not re.fullmatch(r'[a-f0-9]{64}', public_key)
        or not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{32}', revision)
    ):
        raise WalletXError('Invalid Wallet X metadata/identity')
    canonical = normalize_address(address, network).raw
    return {
        'wallet_id': WALLET_ID,
        'label': label.strip(),
        'network': network,
        'address': canonical,
        'anchor_public_key': anchor_public_key,
        'public_key': public_key,
        'revision': revision,
        'scheme': 'ed25519-wallettg-seed32',
        'version': 1,
    }


def _short_addr(address, network):
    try:
        uq = friendly_address(address, network, bounceable=False)
        return f'{uq[:6]}..{uq[-6:]}'
    except Exception:
        return address[:12]


class WalletXVault:
    """Isolated DPAPI + AES-256-GCM seed/mnemonic store for standalone Wallet X."""

    def __init__(self, data_dir, *, protector=None, locked=False, auto_lock_seconds=None):
        self.path = Path(data_dir).resolve() / 'financial_keys' / 'wallet_x.sqlite3'
        self.protector = protector if protector is not None else CurrentUserDPAPI()
        self._lock = threading.RLock()
        self._locked = bool(locked)
        self._auto_lock_seconds = float(auto_lock_seconds) if auto_lock_seconds is not None and auto_lock_seconds > 0 else None
        self._last_active = time.monotonic()

    def _is_locked(self):
        if not self._locked and self._auto_lock_seconds is not None and (time.monotonic() - self._last_active) > self._auto_lock_seconds:
            self._locked = True
        return self._locked

    @staticmethod
    def _entropy(ident):
        return hashlib.sha256(b'accmanager/wallet-x-vault/current-user/v1/' + ident.encode()).digest()

    @staticmethod
    def _mnemonic_entropy(revision):
        return hashlib.sha256(f'accmanager/wallet-x-mnemonic/v1/{revision}'.encode()).digest()

    @contextmanager
    def _db(self):
        if not self.path.is_file() or self.path.is_symlink():
            raise WalletXError('Wallet X vault does not exist yet')
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            _configure_sqlite(db)
            with db:
                yield db

    def _ensure_created(self):
        with self._lock:
            if self.path.exists():
                return
            ident, master = uuid.uuid4().hex, bytearray(secrets.token_bytes(32))
            try:
                wrapped = self.protector.protect(master, self._entropy(ident))
                if not isinstance(wrapped, bytes) or not 1 <= len(wrapped) <= 4096:
                    raise WalletXError('Invalid DPAPI envelope')
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
                with closing(sqlite3.connect(self.path, timeout=5)) as db:
                    _configure_sqlite(db)
                    with db:
                        db.execute('CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER,vault_id TEXT,wrapped BLOB)')
                        db.execute('CREATE TABLE wallet_x_keys(wallet_id TEXT PRIMARY KEY,metadata TEXT NOT NULL,nonce BLOB NOT NULL,ciphertext BLOB NOT NULL)')
                        db.execute('CREATE TABLE wallet_x_mnemonics(wallet_id TEXT PRIMARY KEY,revision TEXT NOT NULL,wrapped BLOB NOT NULL)')
                        db.execute('INSERT INTO meta VALUES(1,1,?,?)', (ident, wrapped))
                self._locked = False
                self._last_active = time.monotonic()
            except DPAPIError:
                raise WalletXError('CurrentUser DPAPI unavailable; Wallet X vault not created') from None
            finally:
                _wipe(master)

    @contextmanager
    def _master(self, db):
        if self._is_locked():
            raise WalletXError('Wallet X vault is locked in this process; unlock explicitly')
        header = db.execute('SELECT * FROM meta WHERE id=1').fetchone()
        if (
            not header or header['version'] != 1 or not isinstance(header['vault_id'], str)
            or not re.fullmatch(r'[a-f0-9]{32}', header['vault_id'])
            or not isinstance(header['wrapped'], bytes) or not 1 <= len(header['wrapped']) <= 4096
        ):
            raise WalletXError('Damaged Wallet X vault header')
        master = None
        try:
            master = bytearray(self.protector.unprotect(header['wrapped'], self._entropy(header['vault_id'])))
            if len(master) != 32:
                raise WalletXError('Invalid Wallet X vault master key')
            self._last_active = time.monotonic()
            yield master, header['vault_id']
        except DPAPIError:
            raise WalletXError('Cannot open Wallet X vault for this Windows user/context') from None
        finally:
            _wipe(master)

    def lock(self):
        with self._lock:
            self._locked = True

    def unlock(self, *, confirmed=False):
        if confirmed is not True:
            raise WalletXError('Explicit Wallet X unlock confirmation required')
        with self._lock:
            prev = self._locked
            self._locked = False
            self._last_active = time.monotonic()
            try:
                with self._db() as db, self._master(db):
                    pass
                self._last_active = time.monotonic()
            except BaseException:
                self._locked = prev
                raise

    def status(self):
        with self._lock:
            locked = self._is_locked()
            if not self.path.exists():
                return {
                    'exists': False,
                    'locked_in_process': locked,
                    'protection': self.protector.name,
                    'configured': False,
                    'wallet': None,
                    'has_mnemonic': False,
                }
            with self._db() as db:
                row = db.execute('SELECT metadata FROM wallet_x_keys WHERE wallet_id=?', (WALLET_ID,)).fetchone()
                mn = db.execute('SELECT revision FROM wallet_x_mnemonics WHERE wallet_id=?', (WALLET_ID,)).fetchone()
                wallet = None
                if row:
                    meta = json.loads(row['metadata'])
                    expected = _wallet_x_metadata(
                        meta['label'], meta['network'], meta['address'],
                        meta['anchor_public_key'], meta['public_key'], meta['revision'],
                    )
                    if meta != expected:
                        raise WalletXError('Invalid Wallet X key metadata')
                    net = meta['network']
                    wallet = {
                        **meta,
                        'friendly_address': friendly_address(meta['address'], net, bounceable=True),
                        'non_bounceable_address': friendly_address(meta['address'], net, bounceable=False),
                    }
                has_mn = bool(mn and wallet and mn['revision'] == wallet['revision'])
            return {
                'exists': True,
                'locked_in_process': locked,
                'protection': self.protector.name,
                'configured': wallet is not None,
                'wallet': wallet,
                'has_mnemonic': has_mn,
            }

    def _store_identity(self, identity, *, network, label, replace_existing):
        self._ensure_created()
        seed = bytearray(identity['signing_seed'])
        revision = uuid.uuid4().hex
        meta = _wallet_x_metadata(
            label, network, identity['address'],
            identity['anchor_public_key'], identity['signing_public_key'], revision,
        )
        try:
            derived = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
            require(derived == meta['public_key'], 'Derived Wallet X public key mismatch')
            with self._lock, self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT metadata FROM wallet_x_keys WHERE wallet_id=?', (WALLET_ID,)).fetchone()
                if old is not None and replace_existing is not True:
                    raise WalletXError('Wallet X already exists; pass replace_existing=True to overwrite')
                with self._master(db) as (master, ident):
                    nonce = secrets.token_bytes(12)
                    encrypted = AESGCM(master).encrypt(nonce, seed, _json({'vault_id': ident, **meta}))
                db.execute('INSERT OR REPLACE INTO wallet_x_keys VALUES(?,?,?,?)', (WALLET_ID, _json(meta).decode(), nonce, encrypted))
                phrase = identity.get('phrase') or ''
                if phrase:
                    wrapped_mn = self.protector.protect(phrase.encode('utf-8'), self._mnemonic_entropy(revision))
                    db.execute('INSERT OR REPLACE INTO wallet_x_mnemonics VALUES(?,?,?)', (WALLET_ID, revision, wrapped_mn))
                else:
                    db.execute('DELETE FROM wallet_x_mnemonics WHERE wallet_id=?', (WALLET_ID,))
        except DPAPIError:
            raise WalletXError('DPAPI failed while protecting Wallet X mnemonic') from None
        finally:
            _wipe(seed)

    def create_wallet(self, *, network='mainnet', label='Wallet X', rotated=False, confirmed=False, replace_existing=False, codec=None):
        require(confirmed is True, 'Explicit confirmation required to generate Wallet X')
        require(network in {'mainnet', 'testnet'}, 'Unsupported network')
        words = generate_rotation_mnemonic(rotated=rotated)
        identity = derive_wallet_identity(' '.join(words), network=network, codec=codec)
        try:
            self._store_identity(identity, network=network, label=label, replace_existing=replace_existing)
            return {**self.status(), 'mnemonic_phrase': identity.get('phrase', '')}
        finally:
            custody_wipe(identity.get('signing_seed'))
            custody_wipe(identity.get('anchor_seed'))

    def import_wallet(self, phrase_or_seed, *, network='mainnet', label='Wallet X', confirmed=False, replace_existing=False, codec=None):
        require(confirmed is True, 'Explicit confirmation required to import Wallet X')
        require(network in {'mainnet', 'testnet'}, 'Unsupported network')
        identity = derive_wallet_identity(phrase_or_seed, network=network, codec=codec)
        try:
            self._store_identity(identity, network=network, label=label, replace_existing=replace_existing)
            return self.status()
        finally:
            custody_wipe(identity.get('signing_seed'))
            custody_wipe(identity.get('anchor_seed'))

    def export_mnemonic(self, *, confirmed=False):
        require(confirmed is True, 'Explicit confirmation required to view Wallet X recovery phrase')
        with self._lock:
            require(self.path.exists() and not self._is_locked(), 'Wallet X vault missing or locked in this process')
            with self._db() as db:
                row = db.execute('SELECT metadata FROM wallet_x_keys WHERE wallet_id=?', (WALLET_ID,)).fetchone()
                mn = db.execute('SELECT revision, wrapped FROM wallet_x_mnemonics WHERE wallet_id=?', (WALLET_ID,)).fetchone()
            require(row is not None, 'Wallet X is not configured')
            meta = json.loads(row['metadata'])
            require(mn is not None and mn['revision'] == meta['revision'], 'No recovery mnemonic stored (wallet imported from raw seed32)')
            try:
                return self.protector.unprotect(mn['wrapped'], self._mnemonic_entropy(mn['revision'])).decode('utf-8')
            except DPAPIError:
                raise WalletXError('Failed to unprotect Wallet X mnemonic via CurrentUser DPAPI') from None

    def delete_wallet(self, *, confirmation, confirmed=False):
        require(confirmed is True, 'Explicit confirmation required to delete Wallet X')
        with self._lock:
            st = self.status()
            require(st['configured'] and st['wallet'], 'No configured Wallet X to delete')
            rev = st['wallet']['revision']
            require(confirmation == f'DELETE WALLET X {rev[:8]}', f'Type exact confirmation: DELETE WALLET X {rev[:8]}')
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('DELETE FROM wallet_x_keys WHERE wallet_id=?', (WALLET_ID,))
                db.execute('DELETE FROM wallet_x_mnemonics WHERE wallet_id=?', (WALLET_ID,))
        return self.status()

    @contextmanager
    def use_seed(self, *, revision, public_key):
        plain = None
        with self._lock:
            try:
                with self._db() as db:
                    row = db.execute('SELECT * FROM wallet_x_keys WHERE wallet_id=?', (WALLET_ID,)).fetchone()
                    require(
                        row is not None and isinstance(row['nonce'], bytes) and len(row['nonce']) == 12
                        and isinstance(row['ciphertext'], bytes) and len(row['ciphertext']) == 48,
                        'Wallet X key missing or damaged',
                    )
                    meta = json.loads(row['metadata'])
                    expected = _wallet_x_metadata(
                        meta['label'], meta['network'], meta['address'],
                        meta['anchor_public_key'], meta['public_key'], meta['revision'],
                    )
                    require(meta == expected and meta['revision'] == revision and meta['public_key'] == public_key,
                            'Wallet X key revision or public key changed')
                    with self._master(db) as (master, ident):
                        plain = bytearray(AESGCM(master).decrypt(row['nonce'], row['ciphertext'], _json({'vault_id': ident, **expected})))
                derived = Ed25519PrivateKey.from_private_bytes(plain).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
                require(derived == public_key, 'Decrypted Wallet X key mismatch')
                yield memoryview(plain)
            except InvalidTag:
                raise WalletXError('Wallet X seed authentication failed') from None
            finally:
                _wipe(plain)


def _parse_emulate_wallet_x(response, source_address, expected_out_count, *, send_all, expected_outgoing_nano, fee_cap_nano):
    trace = response.get('trace') if isinstance(response, dict) else None
    require(isinstance(trace, dict) and isinstance(trace.get('transaction'), dict), 'Missing emulation trace')
    tx = trace['transaction']
    require(tx.get('account') == source_address, 'Emulation source account mismatch')
    desc = tx.get('description')
    require(isinstance(desc, dict) and desc.get('aborted') is False and desc.get('destroyed') is False,
            'Emulation shows source transaction aborted/destroyed')
    compute = desc.get('compute_ph')
    if isinstance(compute, dict) and not compute.get('skipped'):
        require(compute.get('success') is True and compute.get('exit_code') == 0,
                f"Emulation compute failed (exit_code={compute.get('exit_code')})")
    action = desc.get('action')
    if isinstance(action, dict):
        require(action.get('success') is True and action.get('result_code', 0) == 0,
                f"Emulation action phase failed (code={action.get('result_code')})")
    before = tx.get('account_state_before')
    after = tx.get('account_state_after')
    require(isinstance(before, dict) and isinstance(after, dict), 'Incomplete emulation balance states')
    bal_before = nano(before.get('balance'))
    bal_after = nano(after.get('balance'))
    debit = bal_before - bal_after
    require(debit > 0, 'Emulation shows zero/negative balance debit')
    if send_all:
        require(bal_after == 0, 'Send-all emulation must consume remaining balance')
    out_msgs = tx.get('out_msgs')
    require(isinstance(out_msgs, list) and len(out_msgs) == expected_out_count,
            'Emulated outgoing message count does not match prepared request')
    if send_all:
        outgoing_total = sum(nano(m.get('value')) for m in out_msgs if isinstance(m, dict))
    else:
        outgoing_total = int(expected_outgoing_nano)
    est_fee = max(0, debit - outgoing_total)
    require(est_fee <= int(fee_cap_nano), f'Emulated fee {est_fee} exceeds cap {fee_cap_nano}')
    recipient_state = 'unknown'
    children = trace.get('children') or []
    if isinstance(children, list) and children:
        c0_tx = children[0].get('transaction') if isinstance(children[0], dict) else None
        if isinstance(c0_tx, dict):
            c_before = c0_tx.get('account_state_before') or {}
            c_status = c_before.get('account_status')
            if c_status in {'nonexist', 'uninit'}:
                recipient_state = 'uninitialized_credited'
            elif c_status == 'active':
                recipient_state = 'active'
    return {
        'estimated_fee_nano': str(est_fee),
        'source_debit_nano': str(debit),
        'outgoing_total_nano': str(outgoing_total),
        'source_after_nano': str(bal_after),
        'recipient_state': recipient_state,
        'deployment_projected': (before.get('account_status') in {'nonexist', 'uninit'}) and (after.get('account_status') == 'active'),
    }


async def _emulate_wallet_x(http_client, unsigned_boc, network, source_addr, expected_out_count, *, send_all, expected_outgoing_nano, fee_cap_nano, mc_seqno=None):
    host = 'toncenter.com' if network == 'mainnet' else 'testnet.toncenter.com'
    body = {
        'boc': unsigned_boc,
        'ignore_chksig': True,
        'include_code_data': True,
        'include_address_book': False,
        'include_metadata': False,
        'with_actions': False,
    }
    if type(mc_seqno) is int and mc_seqno >= 0:
        body['mc_block_seqno'] = mc_seqno

    async def call_once(http):
        return await bounded_json(http, 'POST', f'https://{host}/api/emulate/v1/emulateTrace', body=body)

    if http_client is not None:
        resp = await call_once(http_client)
        return _parse_emulate_wallet_x(resp, source_addr, expected_out_count, send_all=send_all,
                                       expected_outgoing_nano=expected_outgoing_nano, fee_cap_nano=fee_cap_nano)

    headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(20, connect=5), trust_env=False, follow_redirects=False) as http:
        last_err = None
        for attempt in range(3):
            try:
                resp = await call_once(http)
                return _parse_emulate_wallet_x(resp, source_addr, expected_out_count, send_all=send_all,
                                               expected_outgoing_nano=expected_outgoing_nano, fee_cap_nano=fee_cap_nano)
            except WalletXError:
                raise
            except (httpx.HTTPError, TimeoutError, MoneyReadError) as exc:
                if isinstance(exc, MoneyReadError) and not exc.retryable:
                    raise WalletXError(f'Simulation provider rejected transaction: {exc}') from None
                last_err = exc
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise WalletXError(f'Simulation provider unavailable: {last_err}') from None


async def _broadcast_boc(http_client, external_boc, network):
    host = 'toncenter.com' if network == 'mainnet' else 'testnet.toncenter.com'
    tonapi_host = 'tonapi.io' if network == 'mainnet' else 'testnet.tonapi.io'

    async def send_via(http):
        try:
            resp = await bounded_json(http, 'POST', f'https://{host}/api/v2/sendBoc', body={'boc': external_boc})
            require(resp.get('ok') is True, 'TON Center sendBoc rejected: ' + str(resp.get('error')))
            return 'toncenter-v2'
        except WalletXError:
            raise
        except (MoneyReadError, httpx.HTTPError, TimeoutError) as exc:
            if isinstance(exc, MoneyReadError) and not exc.retryable:
                raise WalletXError(f'TON Center sendBoc error: {exc}') from None
            try:
                fb = await http.post(
                    f'https://{tonapi_host}/v2/blockchain/message',
                    json={'boc': external_boc},
                    headers={'Accept': 'application/json'},
                    follow_redirects=False,
                )
                require(fb.status_code == 200, f'TonAPI broadcast HTTP {fb.status_code}')
                return 'tonapi-v2'
            except WalletXError:
                raise
            except (httpx.HTTPError, TimeoutError) as fb_exc:
                raise WalletXError(f'Broadcast failed ({type(exc).__name__}/{type(fb_exc).__name__})') from None

    if http_client is not None:
        return await send_via(http_client)
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), trust_env=False, follow_redirects=False) as http:
        return await send_via(http)


class WalletXService:
    """High-level controller for Master Wallet X reads, withdrawals, carousel & rent batching."""

    def __init__(self, config, *, vault=None, accounts=None, reader=None, wallet_reader=None, codec=None, clock=time.time, http_client=None):
        self.config = config
        self.clock = clock
        self.vault = vault or WalletXVault(config.data_dir)
        self.accounts = accounts
        self.codec = codec or WalletTgCodec(clock=clock)
        self.http_client = http_client
        self.reader = reader or TonCenterReader(client=http_client, clock=clock)
        self.wallet_reader = wallet_reader or TonCenterWalletReader(client=http_client, codec=self.codec, clock=clock)
        self.path = Path(config.data_dir) / 'wallet_x.sqlite3'
        self.reviews = {}
        self._exec_lock = asyncio.Lock()

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            _configure_sqlite(db)
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS wallet_x_state(wallet_id TEXT PRIMARY KEY, network TEXT NOT NULL, address TEXT NOT NULL, payload TEXT NOT NULL, checked REAL NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS wallet_x_intents(id TEXT PRIMARY KEY, network TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, seqno INTEGER, messages TEXT NOT NULL, total_nano TEXT NOT NULL, send_all INTEGER NOT NULL, created REAL NOT NULL, expires INTEGER NOT NULL, state TEXT NOT NULL, boc_hash TEXT, batch_id TEXT, error TEXT, UNIQUE(network, source, seqno))')
                db.execute('CREATE TABLE IF NOT EXISTS wallet_x_reconciliation(intent TEXT PRIMARY KEY REFERENCES wallet_x_intents(id), checked REAL NOT NULL, evidence TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS wallet_x_batches(id TEXT PRIMARY KEY, mode TEXT NOT NULL, total_nano TEXT NOT NULL, total_count INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS wallet_x_batch_items(batch_id TEXT NOT NULL REFERENCES wallet_x_batches(id), ordinal INTEGER NOT NULL, account_id TEXT NOT NULL, destination TEXT NOT NULL, amount_nano TEXT NOT NULL, intent_id TEXT, state TEXT NOT NULL, error TEXT, PRIMARY KEY(batch_id, ordinal))')
                db.execute('BEGIN IMMEDIATE')
                yield db

    def status(self):
        st = self.vault.status()
        last_chain = None
        if st['configured'] and self.path.exists():
            try:
                with self.db() as db:
                    row = db.execute('SELECT payload, checked FROM wallet_x_state WHERE wallet_id=?', (WALLET_ID,)).fetchone()
                    if row:
                        last_chain = json.loads(row['payload'])
            except Exception:
                last_chain = None
        return {**st, 'cached_chain': last_chain}

    def create_wallet(self, **kwargs):
        return self.vault.create_wallet(**kwargs, codec=self.codec)

    def import_wallet(self, phrase_or_seed, **kwargs):
        return self.vault.import_wallet(phrase_or_seed, **kwargs, codec=self.codec)

    def export_mnemonic(self, *, confirmed=False):
        return self.vault.export_mnemonic(confirmed=confirmed)

    def delete_wallet(self, *, confirmation, confirmed=False):
        return self.vault.delete_wallet(confirmation=confirmation, confirmed=confirmed)

    def lock(self):
        self.vault.lock()

    def unlock(self, *, confirmed=False):
        self.vault.unlock(confirmed=confirmed)

    def _require_wallet(self):
        st = self.vault.status()
        require(st['configured'] and st['wallet'], 'Configure or import Wallet X first')
        return st

    async def refresh_chain(self, *, network=None, include_history=True):
        st = self._require_wallet()
        w = st['wallet']
        net = network or w['network']
        require(net == w['network'], 'Wallet X network mismatch')
        obs = await self.reader.read(w['address'], net)
        seqno = 0
        mc_block = obs.get('block_seqno')
        if obs['chain_state'] == 'active':
            try:
                full = await self.wallet_reader.read(w['address'], net, w['anchor_public_key'], confirmed=True, allow_deployment=False)
                seqno = full['seqno']
                mc_block = full['mc_block_seqno']
            except (MoneyReadError, WalletCodecError):
                seqno = None
        history = []
        if include_history:
            try:
                history = await read_history(w['address'], net, client=self.http_client)
            except FinanceError:
                history = []
        payload = {
            'address': w['address'],
            'friendly_address': w['friendly_address'],
            'non_bounceable_address': w['non_bounceable_address'],
            'network': net,
            'balance_nano': obs['balance_nano'],
            'balance_native': obs['balance_native'],
            'chain_state': obs['chain_state'],
            'seqno': seqno,
            'mc_block_seqno': mc_block,
            'deploy_needed': obs['chain_state'] == 'uninit' and int(obs['balance_nano']) > 0,
            'provider': obs['provider'],
            'history': history,
            'checked_at': self.clock(),
        }
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO wallet_x_state VALUES(?,?,?,?,?)',
                       (WALLET_ID, net, w['address'], json.dumps(payload, sort_keys=True), self.clock()))
        return payload

    def recent_intents(self, limit=30):
        with self.db() as db:
            rows = db.execute(
                'SELECT id, network, source, kind, seqno, total_nano, send_all, created, expires, state, boc_hash, batch_id, error '
                'FROM wallet_x_intents ORDER BY created DESC LIMIT ?',
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    async def _prepare_review(self, *, kind, messages, send_all, fee_cap_ton='0.2', network=None, valid_until=None):
        st = self._require_wallet()
        w = st['wallet']
        net = network or w['network']
        require(net == w['network'], 'Wallet X network mismatch')
        fee_cap_nano = _amount_nano(str(fee_cap_ton))
        with self.db() as db:
            unresolved = db.execute(
                "SELECT id FROM wallet_x_intents WHERE network=? AND source=? AND state IN ('uncertain','submitted_unverified')",
                (net, w['address']),
            ).fetchone()
            if unresolved is not None:
                raise WalletXError(f"Unresolved Wallet X intent {unresolved['id']}; reconcile it first")
        observed = await self.wallet_reader.read(w['address'], net, w['anchor_public_key'], confirmed=True, allow_deployment=True)
        require(observed['public_key'] == w['public_key'], 'On-chain Wallet X public key differs from local vault key')
        deploy = observed.get('active') is False
        seqno = observed['seqno']
        balance = nano(observed['balance_nano'])
        total_out = 0 if send_all else sum(int(m['amount_nano']) for m in messages)
        require(balance > 0 and (send_all or total_out + int(fee_cap_nano) <= balance or total_out < balance),
                'Insufficient Wallet X balance')
        now = int(self.clock())
        exp = min(int(valid_until), now + 240) if valid_until is not None else now + 180
        require(exp - now >= 30, 'Wallet X transaction expiry window too short')
        emu_prep = self.codec.wallet_x_emulate(
            network=net,
            anchor_public_key=w['anchor_public_key'],
            seqno=seqno,
            valid_until=exp,
            messages=messages,
            deploy=deploy,
            allow_deploy_drain=(deploy and send_all),
        )
        proj = await _emulate_wallet_x(
            self.http_client,
            emu_prep['codec']['unsigned_external_boc'],
            net,
            w['address'],
            len(messages),
            send_all=send_all,
            expected_outgoing_nano=str(total_out),
            fee_cap_nano=fee_cap_nano,
            mc_seqno=observed.get('mc_block_seqno'),
        )
        token = secrets.token_hex(24)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires_at = min(exp - 25, self.clock() + 90)
        warning = None
        if any(m.get('bounce') is True for m in messages) and proj.get('recipient_state') == 'uninitialized_credited':
            warning = 'Destination is uninitialized and bounce=True; funds may bounce back minus fees'
        snapshot = {
            'kind': kind,
            'network': net,
            'wallet_revision': w['revision'],
            'source': w['address'],
            'anchor_public_key': w['anchor_public_key'],
            'public_key': w['public_key'],
            'seqno': seqno,
            'deploy': deploy,
            'allow_deploy_drain': (deploy and send_all),
            'send_all': send_all,
            'messages': messages,
            'total_nano': proj['outgoing_total_nano'] if send_all else str(total_out),
            'balance_nano': observed['balance_nano'],
            'estimated_fee_nano': proj['estimated_fee_nano'],
            'valid_until': exp,
            'expires': expires_at,
            'warning': warning,
            'proj': proj,
        }
        now_clean = self.clock()
        self.reviews = {k: v for k, v in self.reviews.items() if v['expires'] > now_clean}
        self.reviews[token_hash] = snapshot
        return token, snapshot

    async def preview_external_send(self, destination, *, amount_ton=None, amount_nano=None, send_all=False, comment='', bounce=False, fee_cap_ton='0.2', network=None, confirmed=False):
        require(confirmed is True, 'Confirm Wallet X read and unsigned simulation')
        require(type(send_all) is bool and type(bounce) is bool, 'Explicit send_all and bounce booleans required')
        st = self._require_wallet()
        net = network or st['wallet']['network']
        try:
            dest_raw = normalize_address(destination, net).raw
        except AddressError as exc:
            raise WalletXError(f'Invalid TON destination: {exc}') from None
        if send_all:
            require(amount_ton in (None, '') and amount_nano in (None, ''), 'Do not specify amount together with send_all=True')
            amt_nano = '0'
            mode = 130
        else:
            if amount_nano is not None:
                amt_nano = str(nano(str(amount_nano), positive=True))
            else:
                amt_nano = _amount_nano(amount_ton)
            require(int(amt_nano) > 0, 'Transfer amount must be positive')
            mode = 3
        msg = {'destination': dest_raw, 'amount_nano': amt_nano, 'mode': mode, 'bounce': bounce}
        if comment:
            msg['comment'] = comment
        token, snap = await self._prepare_review(kind='external_send', messages=[msg], send_all=send_all, fee_cap_ton=fee_cap_ton, network=net)
        short = _short_addr(dest_raw, net)
        deploy_prefix = 'DEPLOY AND ' if snap['deploy'] else ''
        if send_all:
            expected_conf = f'{deploy_prefix}DRAIN ALL TO {short}'
        else:
            amt_label = f"{int(amt_nano) / 10**9:.9f}".rstrip('0').rstrip('.')
            expected_conf = f'{deploy_prefix}WITHDRAW {amt_label} TON TO {short}'
        snap['expected_confirmation'] = expected_conf
        return {
            'review_token': token,
            'expected_confirmation': expected_conf,
            'source': snap['source'],
            'destination': dest_raw,
            'destination_uq': friendly_address(dest_raw, net, bounceable=False),
            'amount_nano': snap['total_nano'],
            'send_all': send_all,
            'bounce': bounce,
            'comment': comment,
            'deploy': snap['deploy'],
            'seqno': snap['seqno'],
            'balance_nano': snap['balance_nano'],
            'estimated_fee_nano': snap['estimated_fee_nano'],
            'recipient_state': snap['proj']['recipient_state'],
            'warning': snap['warning'],
            'expires': snap['expires'],
        }

    async def execute_reviewed(self, review_token, *, confirmation):
        require(isinstance(review_token, str) and re.fullmatch(r'[a-f0-9]{48}', review_token), 'Invalid review token')
        token_hash = hashlib.sha256(review_token.encode()).hexdigest()
        async with self._exec_lock:
            snap = self.reviews.get(token_hash)
            require(snap is not None and self.clock() < snap['expires'], 'Review expired or not found; preview again')
            require(confirmation == snap['expected_confirmation'], f"Type exact confirmation: {snap['expected_confirmation']}")
            st = self._require_wallet()
            require(not st['locked_in_process'], 'Wallet X vault is locked in this process')
            require(st['wallet']['revision'] == snap['wallet_revision'], 'Wallet X changed after preview')
            self.reviews.pop(token_hash, None)
            return await self._sign_and_broadcast(snap)

    async def _sign_and_broadcast(self, snap, *, batch_id=None):
        net = snap['network']
        ident = uuid.uuid4().hex
        now = self.clock()
        require(snap['valid_until'] - int(now) >= 25, 'Wallet X signed request validity expired')
        observed = await self.wallet_reader.read(snap['source'], net, snap['anchor_public_key'], confirmed=True, allow_deployment=True)
        require(
            observed['seqno'] == snap['seqno']
            and observed['public_key'] == snap['public_key']
            and (observed.get('active') is False) == snap['deploy']
            and observed['balance_nano'] == snap['balance_nano'],
            'Wallet X on-chain state changed since preview; preview again',
        )
        with self.db() as db:
            db.execute(
                'INSERT INTO wallet_x_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (
                    ident, net, snap['source'], snap['kind'], snap['seqno'],
                    json.dumps(snap['messages'], sort_keys=True), snap['total_nano'],
                    1 if snap['send_all'] else 0, now, snap['valid_until'],
                    'uncertain', None, batch_id, None,
                ),
            )
        try:
            prep = self.codec.wallet_x_prepare(
                network=net,
                anchor_public_key=snap['anchor_public_key'],
                seqno=snap['seqno'],
                valid_until=snap['valid_until'],
                messages=snap['messages'],
                deploy=snap['deploy'],
                allow_deploy_drain=snap['allow_deploy_drain'],
            )
            with self.vault.use_seed(revision=snap['wallet_revision'], public_key=snap['public_key']) as seed:
                sig = Ed25519PrivateKey.from_private_bytes(seed).sign(bytes.fromhex(prep['codec']['payload_hash']))
            attached = self.codec.wallet_x_attach(request=prep['request'], signature_hex=sig.hex(), signing_public_key=snap['public_key'])
            decoded = self.codec.inspect_external(
                external_boc=attached['external_boc'],
                signing_public_key=snap['public_key'],
                network=net,
            )
            require(
                decoded.get('signature_valid') is True
                and decoded.get('source') == snap['source']
                and decoded.get('seqno') == snap['seqno'],
                'Signed Wallet X BOC self-verification failed',
            )
            boc_bytes = base64.b64decode(attached['external_boc'], validate=True)
            boc_hash = hashlib.sha256(boc_bytes).hexdigest()
            with self.db() as db:
                db.execute('UPDATE wallet_x_intents SET boc_hash=? WHERE id=?', (boc_hash, ident))
            provider = await _broadcast_boc(self.http_client, attached['external_boc'], net)
            with self.db() as db:
                db.execute("UPDATE wallet_x_intents SET state='submitted_unverified' WHERE id=?", (ident,))
            return {
                'id': ident,
                'state': 'submitted_unverified',
                'provider': provider,
                'seqno': snap['seqno'],
                'boc_hash': boc_hash,
                'payload_hash': prep['codec']['payload_hash'],
                'message_hashes': prep['codec']['message_hashes'],
                'total_nano': snap['total_nano'],
                'messages': snap['messages'],
            }
        except BaseException as exc:
            with self.db() as db:
                db.execute('UPDATE wallet_x_intents SET error=? WHERE id=?', (type(exc).__name__, ident))
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not isinstance(exc, WalletXError):
                raise WalletXError(f'Wallet X execution stopped ({type(exc).__name__}); reconcile intent {ident}') from None
            raise

    async def preview_carousel_funding(self, draft_id, draft_revision, *, amount_ton, scope_ids, ui_context, money_service=None, native_workspace=None, auto_provision_first=False, confirmed=False):
        require(confirmed is True, 'Confirm Wallet X preview for carousel funding')
        require(money_service is not None, 'MoneyService required')
        from modules.money_funding import CarouselFunding
        funding = CarouselFunding(money_service)
        rep = funding.report(draft_id, scope_ids=scope_ids, ui_context=ui_context)
        require(rep['revision'] == draft_revision and rep['state'] in {'awaiting_funding', 'funding_observed', 'needs_review'},
                'Funding draft revision/state invalid')
        node0 = rep['route'][0]['id']
        addr = rep.get('address')
        if not addr:
            ctx, _ = money_service.scope(node0, scope_ids, ui_context)
            try:
                if native_workspace is not None:
                    addr = native_workspace._key(node0, ctx)[0]['address']
            except Exception:
                addr = None
            if not addr and auto_provision_first and native_workspace is not None:
                res = native_workspace.provision_local_wallet(node0, replace_existing=False, scope_ids=scope_ids, ui_context=ui_context, confirmed=True)
                addr = res['provisioned_address']
        require(isinstance(addr, str) and addr, f'Account {node0} lacks a WalletTg address; provision it first')
        preview = await self.preview_external_send(addr, amount_ton=amount_ton, comment='', bounce=False, confirmed=True)
        return {**preview, 'draft_id': draft_id, 'draft_revision': draft_revision, 'node0_account_id': node0}

    async def execute_carousel_funding(self, review_token, *, confirmation):
        return await self.execute_reviewed(review_token, confirmation=confirmation)

    def _resolve_account_address(self, account_id):
        from modules.financial_vault import FinancialVault
        vault = FinancialVault(self.config.data_dir)
        if vault.path.exists():
            rec = next((r for r in vault.status().get('records', []) if r.get('account') == account_id and r.get('network') == 'mainnet'), None)
            if rec and rec.get('address'):
                return normalize_address(rec['address'], 'mainnet').raw
        from modules.asset_registry import AssetStore
        store = AssetStore(self.config.data_dir, clock=self.clock)
        rep = store.report(account_id, 'mainnet')
        binding = rep.get('binding') if isinstance(rep, dict) else None
        if isinstance(binding, dict) and binding.get('address'):
            return normalize_address(binding['address'], 'mainnet').raw
        return None

    async def preview_batch_fund(
        self,
        account_ids,
        *,
        mode='fixed',
        amount_ton=None,
        target_balance_ton=None,
        fee_cap_ton='0.05',
        comment='',
        scope_ids=None,
        ui_context=None,
        confirmed=False,
    ):
        require(confirmed is True, 'Confirm batch Rent funding preview')
        require(mode in {'fixed', 'top_up_to'}, 'Batch mode must be fixed or top_up_to')
        st = self._require_wallet()
        w = st['wallet']
        require(w['network'] == 'mainnet', 'Batch Rent funding operates on mainnet')
        require(isinstance(account_ids, (list, tuple)) and 1 <= len(account_ids) <= 200, 'Select 1 to 200 target accounts')
        ids = list(dict.fromkeys(account_ids))
        if scope_ids is not None:
            ids = [a for a in ids if a in scope_ids]
        require(bool(ids), 'No target accounts in current scope')
        fee_cap_nano = _amount_nano(str(fee_cap_ton))
        fixed_nano = int(_amount_nano(amount_ton)) if mode == 'fixed' else None
        target_nano = int(_amount_nano(target_balance_ton)) if mode == 'top_up_to' else None
        if comment:
            require(not comment.startswith('accmanager:') and len(comment.encode('utf-8')) <= 120, 'Invalid batch comment')

        observed = await self.wallet_reader.read(w['address'], 'mainnet', w['anchor_public_key'], confirmed=True, allow_deployment=True)
        require(observed['public_key'] == w['public_key'], 'Wallet X public key mismatch')
        items = []
        skipped = []
        seen_addrs = set()
        for acc_id in ids:
            dest = self._resolve_account_address(acc_id)
            if not dest:
                skipped.append({'account_id': acc_id, 'reason': 'no_wallet_address'})
                continue
            if dest == w['address'] or dest in seen_addrs:
                skipped.append({'account_id': acc_id, 'reason': 'duplicate_or_self_address'})
                continue
            seen_addrs.add(dest)
            if mode == 'fixed':
                amt = fixed_nano
                cur_bal = None
            else:
                try:
                    acc_obs = await self.reader.read(dest, 'mainnet')
                    cur_bal = int(acc_obs['balance_nano'])
                except ReadError as exc:
                    skipped.append({'account_id': acc_id, 'reason': f'balance_read_failed: {exc}'})
                    continue
                amt = target_nano - cur_bal
                if amt < 10_000_000:
                    skipped.append({'account_id': acc_id, 'reason': 'already_funded', 'balance_nano': str(cur_bal)})
                    continue
                if len(ids) > 1 and self.http_client is None:
                    await asyncio.sleep(0.25)
            items.append({
                'account_id': acc_id,
                'destination': dest,
                'destination_uq': friendly_address(dest, 'mainnet', bounceable=False),
                'amount_nano': str(amt),
                'current_balance_nano': str(cur_bal) if cur_bal is not None else None,
            })

        require(bool(items), 'No accounts require funding')
        total_nano = sum(int(it['amount_nano']) for it in items)
        est_total_fee = len(items) * int(fee_cap_nano)
        require(total_nano + est_total_fee <= int(observed['balance_nano']),
                'Insufficient Wallet X balance for batch amount + fee reserve')
        total_label = f'{total_nano / 10**9:.9f}'.rstrip('0').rstrip('.')
        expected_conf = f'FUND {len(items)} ACCOUNTS {total_label} TON'
        token = secrets.token_hex(24)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        snapshot = {
            'kind': 'batch_fund',
            'mode': mode,
            'network': 'mainnet',
            'wallet_revision': w['revision'],
            'source': w['address'],
            'anchor_public_key': w['anchor_public_key'],
            'public_key': w['public_key'],
            'items': items,
            'skipped': skipped,
            'total_nano': str(total_nano),
            'fee_cap_nano': fee_cap_nano,
            'comment': comment,
            'expected_confirmation': expected_conf,
            'expires': self.clock() + 180,
        }
        now_clean = self.clock()
        self.reviews = {k: v for k, v in self.reviews.items() if v['expires'] > now_clean}
        self.reviews[token_hash] = snapshot
        return {
            'review_token': token,
            'expected_confirmation': expected_conf,
            'mode': mode,
            'source': w['address'],
            'items': items,
            'skipped': skipped,
            'total_nano': str(total_nano),
            'total_ton': total_label,
            'estimated_fee_budget_nano': str(est_total_fee),
            'wallet_x_balance_nano': observed['balance_nano'],
            'seqno': observed['seqno'],
            'expires': snapshot['expires'],
        }

    async def execute_batch_fund(self, review_token, *, confirmation, stop_event=None, progress=None, wait_confirm=True, poll_timeout=90.0):
        require(isinstance(review_token, str) and re.fullmatch(r'[a-f0-9]{48}', review_token), 'Invalid batch review token')
        token_hash = hashlib.sha256(review_token.encode()).hexdigest()
        async with self._exec_lock:
            snap = self.reviews.get(token_hash)
            require(snap is not None and snap.get('kind') == 'batch_fund' and self.clock() < snap['expires'],
                    'Batch review expired or missing')
            require(confirmation == snap['expected_confirmation'], f"Type exact confirmation: {snap['expected_confirmation']}")
            st = self._require_wallet()
            require(not st['locked_in_process'] and st['wallet']['revision'] == snap['wallet_revision'],
                    'Wallet X vault locked or changed')
            self.reviews.pop(token_hash, None)

            batch_id = uuid.uuid4().hex
            items = snap['items']
            with self.db() as db:
                db.execute('INSERT INTO wallet_x_batches VALUES(?,?,?,?,?,?)',
                           (batch_id, snap['mode'], snap['total_nano'], len(items), 'running', self.clock()))
                db.executemany(
                    'INSERT INTO wallet_x_batch_items VALUES(?,?,?,?,?,?,?,?)',
                    [(batch_id, idx, it['account_id'], it['destination'], it['amount_nano'], None, 'pending', None)
                     for idx, it in enumerate(items)],
                )

            sent, confirmed_count, failed = [], 0, []
            for idx, it in enumerate(items):
                if stop_event is not None and stop_event.is_set():
                    with self.db() as db:
                        db.execute("UPDATE wallet_x_batch_items SET state='cancelled' WHERE batch_id=? AND ordinal>=? AND state='pending'",
                                   (batch_id, idx))
                        db.execute("UPDATE wallet_x_batches SET state='stopped' WHERE id=?", (batch_id,))
                    break
                if callable(progress):
                    progress({'phase': 'sending', 'index': idx, 'total': len(items), 'account_id': it['account_id']})
                msg = {'destination': it['destination'], 'amount_nano': it['amount_nano'], 'mode': 3, 'bounce': False}
                if snap['comment']:
                    msg['comment'] = snap['comment']
                try:
                    obs = await self.wallet_reader.read(snap['source'], 'mainnet', snap['anchor_public_key'], confirmed=True, allow_deployment=True)
                    hop_snap = {
                        'kind': 'batch_fund_item',
                        'network': 'mainnet',
                        'wallet_revision': snap['wallet_revision'],
                        'source': snap['source'],
                        'anchor_public_key': snap['anchor_public_key'],
                        'public_key': snap['public_key'],
                        'seqno': obs['seqno'],
                        'deploy': obs.get('active') is False,
                        'allow_deploy_drain': False,
                        'send_all': False,
                        'messages': [msg],
                        'total_nano': it['amount_nano'],
                        'balance_nano': obs['balance_nano'],
                        'valid_until': int(self.clock()) + 180,
                    }
                    res = await self._sign_and_broadcast(hop_snap, batch_id=batch_id)
                    sent.append({**it, 'intent_id': res['id'], 'seqno': res['seqno'], 'boc_hash': res['boc_hash']})
                    with self.db() as db:
                        db.execute("UPDATE wallet_x_batch_items SET intent_id=?, state='submitted_unverified' WHERE batch_id=? AND ordinal=?",
                                   (res['id'], batch_id, idx))
                    if wait_confirm:
                        deadline = time.monotonic() + float(poll_timeout)
                        confirmed = False
                        while time.monotonic() < deadline:
                            if stop_event is not None and stop_event.is_set():
                                break
                            await asyncio.sleep(1.5 if self.http_client is None else 0.01)
                            try:
                                cur = await self.wallet_reader.read(snap['source'], 'mainnet', snap['anchor_public_key'], confirmed=True, allow_deployment=False)
                                if cur['seqno'] > res['seqno']:
                                    confirmed = True
                                    break
                            except MoneyReadError:
                                pass
                        if confirmed:
                            confirmed_count += 1
                            with self.db() as db:
                                db.execute("UPDATE wallet_x_batch_items SET state='confirmed' WHERE batch_id=? AND ordinal=?", (batch_id, idx))
                    if callable(progress):
                        progress({'phase': 'item_done', 'index': idx, 'total': len(items), 'account_id': it['account_id'], 'intent_id': res['id']})
                except Exception as exc:
                    failed.append({'account_id': it['account_id'], 'error': str(exc)})
                    with self.db() as db:
                        db.execute("UPDATE wallet_x_batch_items SET state='failed', error=? WHERE batch_id=? AND ordinal=?",
                                   (str(exc)[:200], batch_id, idx))
                    with self.db() as db:
                        db.execute("UPDATE wallet_x_batches SET state='partial_failed' WHERE id=?", (batch_id,))
                    break
            else:
                with self.db() as db:
                    db.execute("UPDATE wallet_x_batches SET state='completed' WHERE id=?", (batch_id,))
            with self.db() as db:
                final_state = db.execute('SELECT state FROM wallet_x_batches WHERE id=?', (batch_id,)).fetchone()['state']
            return {
                'batch_id': batch_id,
                'state': final_state,
                'total': len(items),
                'sent': sent,
                'confirmed': confirmed_count,
                'failed': failed,
                'skipped': snap['skipped'],
            }

    async def preview_raw_messages(self, messages, *, valid_until=None, fee_cap_ton='0.25', network=None, confirmed=False):
        require(confirmed is True, 'Confirm raw Wallet X transaction preview')
        st = self._require_wallet()
        net = network or st['wallet']['network']
        require(isinstance(messages, (list, tuple)) and 1 <= len(messages) <= 16, 'Provide 1 to 16 raw messages')
        norm_msgs = []
        for m in messages:
            require(isinstance(m, dict), 'Invalid raw message item')
            raw_addr = m.get('address') or m.get('destination')
            addr = normalize_address(raw_addr, net).raw
            if m.get('amount_ton') is not None:
                amt = _amount_nano(m['amount_ton'])
            else:
                raw_amt = m.get('amount') if m.get('amount') is not None else m.get('amount_nano')
                raw_amt_str = str(raw_amt) if type(raw_amt) is int else raw_amt
                amt = str(nano(raw_amt_str, positive=True))
            payload_b64 = m.get('payload') if m.get('payload') is not None else m.get('payload_b64')
            state_init = m.get('stateInit') if m.get('stateInit') is not None else m.get('state_init_b64')
            bounce = m.get('bounce')
            if bounce is None:
                bounce = bool(
                    (isinstance(raw_addr, str) and raw_addr.startswith(('E', 'k')))
                    or payload_b64
                    or state_init
                )
            require(type(bounce) is bool, 'Invalid bounce flag')
            entry = {'destination': addr, 'amount_nano': amt, 'mode': 3, 'bounce': bounce}
            if payload_b64:
                entry['payload_b64'] = payload_b64
            if state_init:
                entry['state_init_b64'] = state_init
            norm_msgs.append(entry)
        token, snap = await self._prepare_review(
            kind='raw_messages', messages=norm_msgs, send_all=False,
            fee_cap_ton=fee_cap_ton, network=net, valid_until=valid_until,
        )
        total_label = f"{int(snap['total_nano']) / 10**9:.9f}".rstrip('0').rstrip('.')
        expected_conf = f'SEND RAW {len(norm_msgs)} MSG {total_label} TON'
        snap['expected_confirmation'] = expected_conf
        return {
            'review_token': token,
            'expected_confirmation': expected_conf,
            'source': snap['source'],
            'count': len(norm_msgs),
            'total_nano': snap['total_nano'],
            'estimated_fee_nano': snap['estimated_fee_nano'],
            'deploy': snap['deploy'],
            'seqno': snap['seqno'],
            'expires': snap['expires'],
        }

    async def execute_raw_messages(self, review_token, *, confirmation):
        return await self.execute_reviewed(review_token, confirmation=confirmation)

    async def reconcile_intent(self, intent_id):
        require(isinstance(intent_id, str) and re.fullmatch(r'[a-f0-9]{32}', intent_id), 'Invalid intent_id')
        st = self._require_wallet()
        w = st['wallet']
        with self.db() as db:
            row = db.execute('SELECT * FROM wallet_x_intents WHERE id=?', (intent_id,)).fetchone()
            require(row is not None, 'Unknown Wallet X intent')
            msgs = json.loads(row['messages'])
        net = row['network']
        host = 'toncenter.com' if net == 'mainnet' else 'testnet.toncenter.com'
        start = max(0, int(row['created']) - 60)
        end = int(row['expires']) + 60
        try:
            chain_obs = await self.wallet_reader.read(w['address'], net, w['anchor_public_key'], confirmed=True, allow_deployment=True)
        except MoneyReadError as exc:
            raise WalletXError(f'Reconciliation chain read failed: {exc}') from None
        async def fetch_txs(http):
            async with http.stream(
                'GET',
                f'https://{host}/api/v3/transactions',
                params={'account': row['source'], 'start_utime': start, 'end_utime': end, 'limit': 100, 'sort': 'asc'},
                headers=toncenter_headers({'Accept-Encoding': 'identity', 'Accept': 'application/json'}),
                follow_redirects=False,
            ) as resp:
                if resp.status_code != 200 or resp.headers.get('content-encoding', 'identity').lower() != 'identity':
                    raise WalletXError(f'Reconciliation provider HTTP {resp.status_code}')
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > 1024 * 1024:
                        raise WalletXError('Reconciliation response exceeds size limit')
                    chunks.append(chunk)
                return _page(b''.join(chunks), 100)
        try:
            if self.http_client is not None:
                tx_rows = await fetch_txs(self.http_client)
            else:
                async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), trust_env=False, follow_redirects=False) as http:
                    tx_rows = await fetch_txs(http)
        except (httpx.HTTPError, TimeoutError, ValueError, ReadError) as exc:
            raise WalletXError(f'Reconciliation query failed: {exc}') from None

        expected_pairs = [(m['destination'], None if row['send_all'] else m['amount_nano']) for m in msgs]
        matched = []
        for tx in tx_rows:
            if normalize_address(tx.get('account', ''), net).raw != row['source']:
                continue
            outgoing = tx.get('out_msgs')
            if not isinstance(outgoing, list) or len(outgoing) != len(expected_pairs):
                continue
            ok = True
            for out_m, (exp_dest, exp_amt) in zip(outgoing, expected_pairs):
                try:
                    dest_norm = normalize_address(out_m.get('destination', ''), net).raw
                    val_str = _unsigned(out_m.get('value'))
                except Exception:
                    ok = False
                    break
                if dest_norm != exp_dest or out_m.get('bounced') is True:
                    ok = False
                    break
                if exp_amt is not None and val_str != exp_amt:
                    ok = False
                    break
                if exp_amt is None and int(val_str) <= 0:
                    ok = False
                    break
            if ok:
                matched.append(tx)

        status = 'not_found'
        evidence = {'status': 'not_found', 'seqno_after': chain_obs['seqno'], 'verification': 'provider_reported_not_chain_proof'}
        if len(matched) == 1:
            tx = matched[0]
            evidence['source_tx'] = _hash(tx.get('hash'))
            evidence['lt'] = str(tx.get('lt', ''))
            if not _final(tx):
                status = 'source_pending'
            elif not _executed(tx):
                status = 'source_needs_review'
            else:
                status = 'provider_delivered'
            evidence['status'] = status
        elif len(matched) > 1:
            status = 'ambiguous'
            evidence['status'] = 'ambiguous'

        new_state = 'provider_delivered' if status == 'provider_delivered' else row['state']
        if status == 'provider_delivered':
            with self.db() as db:
                db.execute('UPDATE wallet_x_intents SET state=? WHERE id=?', (new_state, intent_id))
                db.execute('INSERT OR REPLACE INTO wallet_x_reconciliation VALUES(?,?,?)',
                           (intent_id, self.clock(), json.dumps(evidence, sort_keys=True)))
        return {'id': intent_id, 'state': new_state, 'reconciliation': evidence, 'chain_balance_nano': chain_obs['balance_nano']}

    async def recover_stale_intent(self, intent_id, *, confirmation):
        require(isinstance(intent_id, str) and re.fullmatch(r'[a-f0-9]{32}', intent_id), 'Invalid intent_id')
        require(confirmation == f'RECOVER {intent_id}', f'Type exact confirmation: RECOVER {intent_id}')
        st = self._require_wallet()
        w = st['wallet']
        with self.db() as db:
            row = db.execute('SELECT * FROM wallet_x_intents WHERE id=?', (intent_id,)).fetchone()
            require(row is not None and row['state'] in {'uncertain', 'submitted_unverified'}, 'Intent is not pending recovery')
            require(self.clock() > row['expires'] + 60, 'Wait until request expiry + 60s before recovering seqno')
        obs = await self.wallet_reader.read(w['address'], row['network'], w['anchor_public_key'], confirmed=True, allow_deployment=True)
        require(obs['seqno'] >= row['seqno'], 'Unexpected chain seqno rollback')
        new_state = 'expired_unused_nonce_observed' if obs['seqno'] == row['seqno'] else 'prior_nonce_review'
        with self.db() as db:
            db.execute('UPDATE wallet_x_intents SET seqno=NULL, state=? WHERE id=?', (new_state, intent_id))
        return {'id': intent_id, 'state': new_state, 'chain_seqno': obs['seqno']}
