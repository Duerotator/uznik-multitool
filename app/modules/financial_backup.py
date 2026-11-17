"""Portable password-encrypted financial backups. Never plaintext files or HTTP.

Encrypted binary payload = magic + AES master32 + public encrypted-record snapshot.
Master/seed are never fields of metadata JSON. Restore is new-target-only.
"""
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from modules.financial_vault import FinancialVaultError, _json, _metadata, _wipe
from core.windows_dpapi import DPAPIError

MAX_FILE = 16 * 1024 * 1024
MAX_PAYLOAD = 8 * 1024 * 1024
KDF = {'name': 'scrypt', 'n': 2**17, 'r': 8, 'p': 1, 'length': 32}
MAGIC = b'FV1\x00'


def _parse(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise FinancialVaultError('Duplicate financial backup field')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise FinancialVaultError('Invalid financial backup JSON') from None


def _decode(value, length=None):
    if not isinstance(value, str):
        raise FinancialVaultError('Invalid financial backup encoding')
    try:
        result = base64.b64decode(value, validate=True)
    except ValueError:
        raise FinancialVaultError('Invalid financial backup encoding') from None
    if length is not None and len(result) != length:
        raise FinancialVaultError('Invalid financial backup field length')
    return result


def _encoded(value):
    return base64.b64encode(value).decode('ascii')


def _password(password):
    if not isinstance(password, str) or not 16 <= len(password) <= 1024:
        raise FinancialVaultError('Use a unique backup passphrase of 16–1024 characters')
    return password.encode('utf-8')


def _read(path):
    path = Path(path)
    if path.is_symlink():
        raise FinancialVaultError('Backup must not be a symbolic link')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= MAX_FILE:
            raise FinancialVaultError('Backup must be a bounded regular file')
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise FinancialVaultError('Financial backup exceeds size limit')
    return raw


def _validate(snapshot, master):
    if (not isinstance(snapshot, dict) or set(snapshot) != {'vault_id', 'records'}
            or not isinstance(snapshot['vault_id'], str) or not re.fullmatch('[a-f0-9]{32}', snapshot['vault_id'])
            or not isinstance(snapshot['records'], list) or len(snapshot['records']) > 10000):
        raise FinancialVaultError('Unsupported financial backup payload')
    seen, previous = set(), None
    for record in snapshot['records']:
        if not isinstance(record, dict) or set(record) != {'metadata', 'nonce', 'ciphertext'}:
            raise FinancialVaultError('Unsupported financial backup record')
        metadata = record['metadata']
        if not isinstance(metadata, dict) or set(metadata) != {'account','user_id','context','network','address','public_key','revision','scheme','version'}:
            raise FinancialVaultError('Unsupported financial backup namespace')
        expected = _metadata(**{k: metadata[k] for k in ('account','user_id','context','network','address','public_key','revision')})
        if metadata != expected or type(metadata['version']) is not int:
            raise FinancialVaultError('Financial backup metadata mismatch')
        key = (metadata['account'], metadata['network'])
        if key in seen:
            raise FinancialVaultError('Duplicate financial backup account/network')
        if previous is not None and key < previous:
            raise FinancialVaultError('Financial backup records are not canonically ordered')
        seen.add(key)
        previous = key
        nonce, ciphertext = _decode(record['nonce'], 12), _decode(record['ciphertext'], 48)
        plain = None
        try:
            plain = bytearray(AESGCM(master).decrypt(nonce, ciphertext, _json({'vault_id': snapshot['vault_id'], **metadata})))
            derived = Ed25519PrivateKey.from_private_bytes(plain).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
            if derived != metadata['public_key']:
                raise FinancialVaultError('Financial backup public key mismatch')
        except InvalidTag:
            raise FinancialVaultError('Financial backup record authentication failed') from None
        finally:
            _wipe(plain)


def _open(path, password):
    raw = _read(path)
    header = _parse(raw)
    if (not isinstance(header, dict) or set(header) != {'format','version','kdf','salt','nonce','ciphertext'}
            or header['format'] != 'tgb-financial-vault-backup' or type(header['version']) is not int or header['version'] != 1
            or header['kdf'] != KDF or any(type(header['kdf'].get(k)) is not int for k in ('n','r','p','length'))):
        raise FinancialVaultError('Unsupported financial backup envelope/KDF; no generic vault import')
    salt, nonce = _decode(header['salt'], 16), _decode(header['nonce'], 12)
    cipher = _decode(header['ciphertext'])
    if not 52 <= len(cipher) <= MAX_PAYLOAD + 16:
        raise FinancialVaultError('Invalid financial backup ciphertext size')
    key = bytearray(Scrypt(salt=salt,length=32,n=KDF['n'],r=8,p=1).derive(_password(password)))
    payload, master = None, None
    try:
        aad = {k:v for k,v in header.items() if k != 'ciphertext'}
        try:
            payload = bytearray(AESGCM(key).decrypt(nonce, cipher, _json(aad)))
        except InvalidTag:
            raise FinancialVaultError('Wrong backup passphrase or damaged financial backup') from None
        if payload[:4] != MAGIC or not 36 < len(payload) <= MAX_PAYLOAD:
            raise FinancialVaultError('Unsupported financial backup binary payload')
        master = bytearray(payload[4:36])
        snapshot = _parse(payload[36:])
        _validate(snapshot, master)
        return raw, snapshot, bytearray(master)
    finally:
        _wipe(key); _wipe(payload); _wipe(master)


def _report(raw, snapshot):
    records = [r['metadata'] for r in snapshot['records']]
    return {'file_hash': hashlib.sha256(raw).hexdigest(),
            'manifest_hash': hashlib.sha256(_json(snapshot)).hexdigest(),
            'records': records, 'source_vault_id': snapshot['vault_id'],
            'ownership': 'unverified', 'signing': 'unsupported', 'chain_reverification_required': True}


def verify_backup(path, password):
    raw, snapshot, master = _open(path, password)
    try:
        return _report(raw, snapshot)
    finally:
        _wipe(master)


def backup(vault, target, password, *, confirmed=False):
    if confirmed is not True:
        raise FinancialVaultError('Explicit local encrypted backup confirmation required')
    _password(password)
    with vault._lock, vault._db() as db:
        db.execute('BEGIN IMMEDIATE')  # Freeze a single snapshot across processes.
        with vault._master(db) as (master, ident):
            rows = db.execute('SELECT metadata,nonce,ciphertext FROM keys ORDER BY account,network LIMIT 10001').fetchall()
            snapshot = {'vault_id': ident, 'records': [{'metadata': _parse(row['metadata']),
                'nonce': _encoded(row['nonce']), 'ciphertext': _encoded(row['ciphertext'])} for row in rows]}
            _validate(snapshot, master)
            payload = bytearray(MAGIC)
            payload.extend(master)
            payload.extend(_json(snapshot))
            if len(payload) > MAX_PAYLOAD:
                _wipe(payload)
                raise FinancialVaultError('Financial backup payload exceeds limit')
            salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
            key = None
            try:
                key = bytearray(Scrypt(salt=salt,length=32,n=KDF['n'],r=8,p=1).derive(_password(password)))
                header = {'format':'tgb-financial-vault-backup','version':1,'kdf':KDF,
                          'salt':_encoded(salt),'nonce':_encoded(nonce)}
                raw = _json({**header,'ciphertext':_encoded(AESGCM(key).encrypt(nonce,payload,_json(header)))})
            finally:
                _wipe(payload); _wipe(key)
    target = Path(target)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(raw); output.flush(); os.fsync(output.fileno())
    # Verify exact written bytes, not merely successful encryption in memory.
    result = verify_backup(target, password)
    if result['file_hash'] != hashlib.sha256(raw).hexdigest():
        raise FinancialVaultError('Backup changed during verification; do not use it')
    return result


def restore_new(vault, source, password, *, confirmed=False, expected_file_hash=None, expected_manifest_hash=None):
    if (confirmed is not True or not isinstance(expected_file_hash, str) or not re.fullmatch('[a-f0-9]{64}', expected_file_hash)
            or not isinstance(expected_manifest_hash, str) or not re.fullmatch('[a-f0-9]{64}', expected_manifest_hash)):
        raise FinancialVaultError('Restore requires confirmation of the exact reviewed backup file/manifest')
    raw, snapshot, master = _open(source, password)
    try:
        report = _report(raw, snapshot)
        if report['file_hash'] != expected_file_hash or report['manifest_hash'] != expected_manifest_hash:
            raise FinancialVaultError('Backup changed since review; target untouched')
        with vault._lock:
            if vault.path.exists() or vault.path.is_symlink():
                raise FinancialVaultError('Restore only into a new target; existing vault never overwritten/merged')
            try:
                wrapped = vault.protector.protect(master, vault._entropy(snapshot['vault_id']))
            except DPAPIError:
                raise FinancialVaultError('Target CurrentUser DPAPI unavailable; no restore fallback') from None
            if not isinstance(wrapped, bytes) or not 1 <= len(wrapped) <= 4096:
                raise FinancialVaultError('Invalid restored DPAPI envelope')
            vault.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = vault.path.with_name(
                f".{vault.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            try:
                fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.close(fd)
                with closing(sqlite3.connect(temp_path, timeout=5)) as db:
                    db.execute('PRAGMA journal_mode=WAL')
                    with db:
                        db.execute('CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER,vault_id TEXT,wrapped BLOB)')
                        db.execute('CREATE TABLE keys(account TEXT,network TEXT,metadata TEXT,nonce BLOB,ciphertext BLOB,PRIMARY KEY(account,network))')
                        db.execute('INSERT INTO meta VALUES(1,1,?,?)', (snapshot['vault_id'], wrapped))
                        for record in snapshot['records']:
                            metadata = record['metadata']
                            db.execute(
                                'INSERT INTO keys VALUES(?,?,?,?,?)',
                                (
                                    metadata['account'],
                                    metadata['network'],
                                    _json(metadata).decode(),
                                    _decode(record['nonce'], 12),
                                    _decode(record['ciphertext'], 48),
                                ),
                            )
                    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

                # Prove that the staged target really unwraps for this Windows identity
                # before atomically placing vault.sqlite3 on disk.
                with closing(sqlite3.connect(temp_path, timeout=5)) as db:
                    db.row_factory = sqlite3.Row
                    header = db.execute('SELECT * FROM meta WHERE id=1').fetchone()
                    if not header or header['vault_id'] != snapshot['vault_id']:
                        raise FinancialVaultError('Restored financial master verification failed')
                    restored_master = None
                    try:
                        try:
                            restored_master = bytearray(
                                vault.protector.unprotect(header['wrapped'], vault._entropy(header['vault_id']))
                            )
                        except DPAPIError:
                            raise FinancialVaultError('Cannot open financial vault for this Windows identity/context') from None
                        if not secrets.compare_digest(master, restored_master):
                            raise FinancialVaultError('Restored financial master verification failed')
                        rows = db.execute('SELECT metadata,nonce,ciphertext FROM keys ORDER BY account,network').fetchall()
                        written = {
                            'vault_id': header['vault_id'],
                            'records': [
                                {
                                    'metadata': _parse(row['metadata']),
                                    'nonce': _encoded(row['nonce']),
                                    'ciphertext': _encoded(row['ciphertext']),
                                }
                                for row in rows
                            ],
                        }
                        # Canonical ordering is fixed at export; do not remap context/revisions.
                        if _json(written) != _json(snapshot):
                            raise FinancialVaultError('Restored financial records changed during verification')
                        _validate(written, restored_master)
                    finally:
                        _wipe(restored_master)

                for suffix in ('-wal', '-shm'):
                    Path(str(temp_path) + suffix).unlink(missing_ok=True)
                if vault.path.exists() or vault.path.is_symlink():
                    raise FinancialVaultError('Restore only into a new target; existing vault never overwritten/merged')
                os.replace(temp_path, vault.path)
            finally:
                vault._locked = True  # No automatic signer/replay after restore.
                temp_path.unlink(missing_ok=True)
                for suffix in ('-wal', '-shm'):
                    Path(str(temp_path) + suffix).unlink(missing_ok=True)
            return {**report, 'restored': True, 'locked_in_process': True}
    finally:
        _wipe(master)
