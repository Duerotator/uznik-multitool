"""Local-only financial Ed25519 seed store; no wallet RPC, HTTP, signer or broadcast.

DPAPI wraps a random AES key; record AAD binds exact public identity/context.
This does NOT verify that the declared address is controlled by the signing key.
"""
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

from core.ton_address import normalize_address
from core.windows_dpapi import CurrentUserDPAPI, DPAPIError


class FinancialVaultError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def _wipe(value):
    if isinstance(value, bytearray):
        value[:] = bytes(len(value))


def _metadata(account, user_id, context, network, address, public_key, revision):
    if (not isinstance(account, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', account)
            or type(user_id) is not int or not 0 < user_id < 2**63
            or not isinstance(context, str) or not re.fullmatch('[a-f0-9]{64}', context)
            or not isinstance(public_key, str) or not re.fullmatch('[a-f0-9]{64}', public_key)
            or not isinstance(revision, str) or not re.fullmatch('[a-f0-9]{32}', revision)):
        raise FinancialVaultError('Invalid financial key identity/context')
    return {'account': account, 'user_id': user_id, 'context': context, 'network': network,
            'address': normalize_address(address, network).raw, 'public_key': public_key, 'revision': revision,
            'scheme': 'ed25519-seed32', 'version': 1}


def _configure_sqlite(db, *, set_wal=True):
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('PRAGMA foreign_keys=ON')
    if set_wal:
        mode = db.execute('PRAGMA journal_mode').fetchone()
        if not mode or str(mode[0]).lower() != 'wal':
            db.execute('PRAGMA journal_mode=WAL')


class FinancialVault:
    def __init__(self, data_dir, *, protector=None, locked=False, auto_lock_seconds=None):
        self.path = Path(data_dir).resolve() / 'financial_keys' / 'vault.sqlite3'
        self.protector = protector if protector is not None else CurrentUserDPAPI()
        self._lock = threading.RLock()
        self._locked = bool(locked)
        self._auto_lock_seconds = float(auto_lock_seconds) if auto_lock_seconds is not None and auto_lock_seconds > 0 else None
        self._last_active = time.monotonic()

    @contextmanager
    def _db(self):
        if not self.path.is_file() or self.path.is_symlink():
            raise FinancialVaultError('Financial vault is absent or an unsupported link; no auto-create/fallback')
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            _configure_sqlite(db)
            with db:
                yield db

    def create(self, *, confirmed=False):
        if confirmed is not True:
            raise FinancialVaultError('Explicit local vault creation confirmation required')
        with self._lock:
            ident, master = uuid.uuid4().hex, bytearray(secrets.token_bytes(32))
            try:
                wrapped = self.protector.protect(master, self._entropy(ident))
                if not isinstance(wrapped, bytes) or not 1 <= len(wrapped) <= 4096:
                    raise FinancialVaultError('Invalid DPAPI envelope')
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
                with closing(sqlite3.connect(self.path, timeout=5)) as db:
                    _configure_sqlite(db)
                    with db:
                        db.execute('CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER,vault_id TEXT,wrapped BLOB)')
                        db.execute('CREATE TABLE keys(account TEXT,network TEXT,metadata TEXT,nonce BLOB,ciphertext BLOB,PRIMARY KEY(account,network))')
                        db.execute('INSERT INTO meta VALUES(1,1,?,?)', (ident, wrapped))
                self._locked = False
                self._last_active = time.monotonic()
            except DPAPIError:
                raise FinancialVaultError('CurrentUser DPAPI unavailable; financial vault not created') from None
            except FileExistsError:
                raise FinancialVaultError('Financial vault already exists; never overwrite it') from None
            finally:
                _wipe(master)

    @staticmethod
    def _entropy(ident):
        return hashlib.sha256(b'accmanager/financial-vault/current-user/v1/' + ident.encode()).digest()

    def _is_locked_unlocked_check(self):
        if not self._locked and self._auto_lock_seconds is not None and (time.monotonic() - self._last_active) > self._auto_lock_seconds:
            self._locked = True
        return self._locked

    @contextmanager
    def _master(self, db):
        if self._is_locked_unlocked_check():
            raise FinancialVaultError('Financial vault locked in this process; unlock explicitly')
        header = db.execute('SELECT * FROM meta WHERE id=1').fetchone()
        if (not header or header['version'] != 1 or not isinstance(header['vault_id'], str)
                or not re.fullmatch('[a-f0-9]{32}', header['vault_id'])
                or not isinstance(header['wrapped'], bytes) or not 1 <= len(header['wrapped']) <= 4096):
            raise FinancialVaultError('Unsupported/damaged financial vault header')
        master = None
        try:
            master = bytearray(self.protector.unprotect(header['wrapped'], self._entropy(header['vault_id'])))
            if len(master) != 32:
                raise FinancialVaultError('Invalid financial vault key')
            self._last_active = time.monotonic()
            yield master, header['vault_id']
        except DPAPIError:
            raise FinancialVaultError('Cannot open financial vault for this Windows identity/context') from None
        finally:
            _wipe(master)

    def lock(self):
        with self._lock:
            self._locked = True

    def unlock(self, *, confirmed=False):
        if confirmed is not True:
            raise FinancialVaultError('Explicit local unlock confirmation required')
        with self._lock:
            # Prove DPAPI usability before clearing the process lock.
            previous = self._locked
            self._locked = False
            self._last_active = time.monotonic()
            try:
                with self._db() as db, self._master(db):
                    pass
                self._last_active = time.monotonic()
            except BaseException:
                self._locked = previous
                raise

    def put(self, *, account, user_id, context, network, address, public_key, seed,
            confirmed=False, expected_revision=None):
        if confirmed is not True or not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
            raise FinancialVaultError('Explicit local import of one Ed25519 seed32 required')
        revision = uuid.uuid4().hex
        metadata = _metadata(account, user_id, context, network, address, public_key, revision)
        derived = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        if derived != public_key:
            raise FinancialVaultError('Seed does not match the declared current public key')
        with self._lock, self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT metadata FROM keys WHERE account=? AND network=?', (account, network)).fetchone()
            if (old and json.loads(old['metadata'])['revision'] != expected_revision) or (not old and expected_revision is not None):
                raise FinancialVaultError('Financial key revision changed; replacement requires exact previous revision')
            with self._master(db) as (master, ident):
                nonce = secrets.token_bytes(12)
                encrypted = AESGCM(master).encrypt(nonce, seed, _json({'vault_id': ident, **metadata}))
            db.execute('INSERT OR REPLACE INTO keys VALUES(?,?,?,?,?)', (account, network, _json(metadata).decode(), nonce, encrypted))
        return revision

    @contextmanager
    def use_seed(self, *, account, user_id, context, network, address, public_key, revision):
        expected = _metadata(account, user_id, context, network, address, public_key, revision)
        plain = None
        with self._lock:
            try:
                with self._db() as db:
                    row = db.execute('SELECT * FROM keys WHERE account=? AND network=?', (account, network)).fetchone()
                    if (not row or row['metadata'] != _json(expected).decode()
                            or not isinstance(row['nonce'], bytes) or len(row['nonce']) != 12
                            or not isinstance(row['ciphertext'], bytes) or len(row['ciphertext']) != 48):
                        raise FinancialVaultError('Financial key missing or identity/context/revision changed')
                    with self._master(db) as (master, ident):
                        plain = bytearray(AESGCM(master).decrypt(row['nonce'], row['ciphertext'], _json({'vault_id': ident, **expected})))
                derived = Ed25519PrivateKey.from_private_bytes(plain).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
                if derived != public_key:
                    raise FinancialVaultError('Decrypted financial key has wrong public identity')
                yield memoryview(plain)
            except InvalidTag:
                raise FinancialVaultError('Financial key authentication failed; no fallback') from None
            finally:
                _wipe(plain)

    def status(self):
        """Public inventory only; does not auto-unprotect the DPAPI master key."""
        with self._lock:
            locked = self._is_locked_unlocked_check()
            if not self.path.exists():
                return {'exists': False, 'locked_in_process': locked, 'records': [], 'signing': 'unsupported'}
            with self._db() as db:
                rows = db.execute('SELECT metadata FROM keys ORDER BY account,network').fetchall()
                records = [json.loads(row['metadata']) for row in rows]
                for row in records:
                    validated = _metadata(**{k: row[k] for k in ('account','user_id','context','network','address','public_key','revision')})
                    if row != validated:
                        raise FinancialVaultError('Invalid financial inventory metadata')
            return {'exists': True, 'protection': self.protector.name, 'locked_in_process': locked,
                    'records': records, 'address_ownership': 'unverified', 'signing': 'unsupported'}
