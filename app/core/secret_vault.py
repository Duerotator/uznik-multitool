"""Opt-in authenticated storage. Never writes plaintext or persists the master key."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


class VaultError(RuntimeError):
    pass


class VaultLocked(VaultError):
    pass


class VaultConflict(VaultError):
    pass


MAX_FILE = 16*1024*1024
MAX_PLAIN = 8*1024*1024
KDF = {"name": "scrypt", "n": 2**17, "r": 8, "p": 1, "length": 32}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise VaultError("Duplicate vault fields")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise VaultError("Invalid vault data") from exc


def _decode(value, size=None):
    if not isinstance(value, str):
        raise VaultError("Invalid vault encoding")
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise VaultError("Invalid vault encoding") from exc
    if size is not None and len(result) != size:
        raise VaultError("Invalid vault field length")
    return result


def _read_backup(path):
    # Nonblocking open prevents a supplied FIFO from hanging a worker.
    fd = os.open(Path(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise VaultError("Backup must be a regular file within the size limit")
        raw = stream.read(MAX_FILE+1)
    if len(raw) > MAX_FILE:
        raise VaultError("Backup exceeds the supported size")
    return raw


def _header(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_FILE:
        raise VaultError("Vault file exceeds the supported size")
    data = _json(raw)
    if not isinstance(data, dict) or set(data) != {"format", "version", "id", "revision", "kdf", "nonce", "ciphertext"}:
        raise VaultError("Invalid vault envelope")
    if data["format"] != "tgb-secret-vault" or type(data["version"]) is not int or data["version"] != 1:
        raise VaultError("Unsupported vault version")
    if not isinstance(data["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", data["id"]):
        raise VaultError("Invalid vault identity")
    if type(data["revision"]) is not int or not 1 <= data["revision"] < 2**63:
        raise VaultError("Invalid vault revision")
    kdf = data["kdf"]
    if not isinstance(kdf, dict) or set(kdf) != {*KDF, "salt"}:
        raise VaultError("Unsupported vault KDF")
    if any(type(kdf[key]) is not type(value) or kdf[key] != value for key, value in KDF.items()):
        raise VaultError("Unsupported vault KDF parameters")
    _decode(kdf["salt"], 16)
    _decode(data["nonce"], 12)
    ciphertext = _decode(data["ciphertext"])
    if not 16 <= len(ciphertext) <= MAX_PLAIN+16:
        raise VaultError("Invalid vault ciphertext size")
    return data


def _password(value, *, create=False):
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise VaultError("Invalid master passphrase length")
    if create and len(value) < 12:
        raise VaultError("Use a unique master passphrase of at least 12 characters")
    return value.encode()


def _derive(password, header):
    kdf = header["kdf"]
    return Scrypt(salt=_decode(kdf["salt"], 16), length=32, n=KDF["n"], r=8, p=1).derive(_password(password))


def _records(value):
    if not isinstance(value, dict) or set(value) != {"schema", "secrets"} or type(value["schema"]) is not int or value["schema"] != 1:
        raise VaultError("Invalid vault payload")
    result = value["secrets"]
    if not isinstance(result, dict) or len(result) > 10000:
        raise VaultError("Invalid vault record count")
    for key, secret in result.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_./:-]{1,160}", key):
            raise VaultError("Invalid secret key")
        if not isinstance(secret, str) or not secret or len(secret.encode()) > 65536:
            raise VaultError("Invalid secret value")
    return result


def _decrypt(header, key):
    aad = _canonical({k: v for k, v in header.items() if k != "ciphertext"})
    try:
        plain = AESGCM(key).decrypt(_decode(header["nonce"], 12), _decode(header["ciphertext"]), aad)
    except InvalidTag as exc:
        raise VaultError("Wrong master passphrase or damaged vault") from exc
    return _records(_json(plain))


def _encrypt(header, key, records):
    plain = _canonical({"schema": 1, "secrets": records})
    _records(_json(plain))
    if len(plain) > MAX_PLAIN:
        raise VaultError("Vault payload exceeds the supported size")
    header = {**header, "nonce": base64.b64encode(secrets.token_bytes(12)).decode()}
    header.pop("ciphertext", None)
    encrypted = AESGCM(key).encrypt(_decode(header["nonce"], 12), plain, _canonical(header))
    return _canonical({**header, "ciphertext": base64.b64encode(encrypted).decode()})


class SecretVault:
    def __init__(self, data_dir, *, idle_seconds=600, clock=time.monotonic):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir/"vault"/"secrets.sqlite3"
        self.idle_seconds, self.clock = idle_seconds, clock
        self._mutex = threading.RLock()
        self._key = None
        self._id = None
        self._touched = 0
        self.manual_lock = False

    def __repr__(self):
        return "<SecretVault>"

    def _connect(self, *, create: bool = False):
        if not self.path.exists():
            raise VaultError("Vault is not initialized")
        if self.path.stat().st_size == 0:
            raise VaultError("Invalid vault database; it was not replaced")
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _write_new(self, raw: bytes, *, conflict_msg: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(tmp.as_uri() + "?mode=rw", uri=True, timeout=5)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE vault(id INTEGER PRIMARY KEY CHECK(id=1), envelope BLOB NOT NULL)")
                db.execute("INSERT INTO vault VALUES (1,?)", (raw,))
                db.commit()
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            for suffix in ("-wal", "-shm"):
                Path(str(tmp) + suffix).unlink(missing_ok=True)
            with self._mutex:
                if self.path.exists():
                    raise VaultConflict(conflict_msg)
                try:
                    os.link(tmp, self.path)
                except FileExistsError:
                    raise VaultConflict(conflict_msg) from None
                except OSError:
                    os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(tmp) + suffix).unlink(missing_ok=True)

    def _read(self, db):
        try:
            row = db.execute("SELECT envelope FROM vault WHERE id=1").fetchone()
        except sqlite3.DatabaseError as exc:
            raise VaultError("Invalid vault database; it was not replaced") from exc
        if row is None:
            raise VaultError("Vault is empty or incomplete")
        return _header(bytes(row[0]))

    def lock(self, *, manual=True):
        with self._mutex:
            if self._key is not None:
                self._key[:] = b"\0"*len(self._key)
            self._key, self._id = None, None
            self.manual_lock = manual or self.manual_lock

    @property
    def unlocked(self):
        with self._mutex:
            if self._key is not None and self.clock()-self._touched >= self.idle_seconds:
                self.lock(manual=False)
            return self._key is not None

    def _open_records(self, db):
        if not self.unlocked:
            raise VaultLocked("Unlock the vault in this process first")
        header = self._read(db)
        if header["id"] != self._id:
            self.lock()
            raise VaultLocked("Vault was replaced; unlock again")
        try:
            records = _decrypt(header, bytes(self._key))
        except VaultError:
            self.lock()
            raise
        return header, records

    def create(self, password):
        _password(password, create=True)
        with self._mutex:
            if self.path.exists():
                raise VaultConflict("A vault already exists; it was not replaced")
            header = {
                "format": "tgb-secret-vault", "version": 1, "id": secrets.token_hex(16), "revision": 1,
                "kdf": {**KDF, "salt": base64.b64encode(secrets.token_bytes(16)).decode()},
            }
            key = _derive(password, header)
            raw = _encrypt(header, key, {})
            self._write_new(raw, conflict_msg="A vault already exists; it was not replaced")
        self.unlock(password)

    def unlock(self, password):
        with self._mutex:
            self.lock()
            with closing(self._connect()) as db:
                header = self._read(db)
                key = _derive(password, header)
                _decrypt(header, key)
            self._key, self._id = bytearray(key), header["id"]
            self._touched, self.manual_lock = self.clock(), False

    def status(self):
        with self._mutex:
            result = {"exists": self.path.exists(), "unlocked": self.unlocked,
                      "entries": None, "idle_seconds": self.idle_seconds}
            if result["exists"]:
                with closing(self._connect()) as db:
                    self._read(db)
                    if result["unlocked"]:
                        result["entries"] = len(self._open_records(db)[1])
            return result

    def get(self, key):
        with self._mutex, closing(self._connect()) as db:
            _, records = self._open_records(db)
            self._touched = self.clock()
            return records.get(key)

    def has(self, key):
        """Presence only; status checks must not extend the idle timeout."""
        with self._mutex, closing(self._connect()) as db:
            _, records = self._open_records(db)
            return key in records

    def mutate(self, operation):
        with self._mutex, closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            header, records = self._open_records(db)
            operation(records)
            raw = _encrypt({**header, "revision": header["revision"]+1}, bytes(self._key), records)
            # Read-back authentication before the database is changed.
            _decrypt(_header(raw), bytes(self._key))
            db.execute("UPDATE vault SET envelope=? WHERE id=1", (raw,))
            self._touched = self.clock()

    def put_many(self, entries, *, replace=False):
        def operation(records):
            for key, value in entries.items():
                if key in records and records[key] != value and not replace:
                    raise VaultConflict("A different secret is already stored")
            records.update(entries)
        self.mutate(operation)

    def backup(self, target):
        target = Path(target).resolve()
        if target == self.path:
            raise VaultConflict("Backup must be a separate file")
        with self._mutex, closing(self._connect()) as db:
            header, _ = self._open_records(db)
            data = _canonical(header)
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                _decrypt(_header(_read_backup(target)), bytes(self._key))
            except Exception:
                # Only the new, exclusively created incomplete backup is removed.
                target.unlink(missing_ok=True)
                raise
            self._touched = self.clock()
            return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    @staticmethod
    def verify_backup(path, password):
        raw = _read_backup(path)
        header = _header(raw)
        records = _decrypt(header, _derive(password, header))
        return {"entries": len(records), "sha256": hashlib.sha256(raw).hexdigest()}

    def restore_new(self, path, password, *, allowed_keys=None):
        raw = _read_backup(path)
        header = _header(raw)
        records = _decrypt(header, _derive(password, header))
        if allowed_keys is not None and not set(records) <= set(allowed_keys):
            raise VaultError("Backup belongs to another credential namespace")
        with self._mutex:
            if self.path.exists():
                raise VaultConflict("Restore requires a new vault; existing data was not replaced")
            self._write_new(raw, conflict_msg="Restore requires a new vault; existing data was not replaced")
        self.unlock(password)

    @contextmanager
    def backed_entries(self, entries, backup):
        """Keep other vault writers out until the caller completes metadata CAS."""
        saved_raw = _read_backup(backup)
        with self._mutex, closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            header, current = self._open_records(db)
            saved_header = _header(saved_raw)
            if saved_header["id"] != header["id"]:
                raise VaultConflict("Backup belongs to another vault")
            saved = _decrypt(saved_header, bytes(self._key))
            if any(current.get(key) != value or saved.get(key) != value for key, value in entries.items()):
                raise VaultConflict("Both the vault and verified backup must contain every selected secret")
            yield
            self._touched = self.clock()


_VAULTS = {}
_REGISTRY_LOCK = threading.RLock()


def get_vault(data_dir):
    key = os.path.normcase(str(Path(data_dir).resolve()))
    with _REGISTRY_LOCK:
        if key not in _VAULTS:
            _VAULTS[key] = SecretVault(data_dir)
        return _VAULTS[key]


def unlock_for_service(vault, *, password_file_env="TGB_VAULT_PASSWORD_FILE"):
    if vault.unlocked:
        return
    if vault.manual_lock:
        raise VaultLocked("Vault was locked in this process; unlock it explicitly")
    raw = os.getenv(password_file_env)
    if not raw:
        raise VaultLocked("Vault is locked; unlock locally or configure an external password file for headless use")
    path = Path(raw).resolve(strict=True)
    repo = Path(__file__).resolve().parents[1]
    if path.is_relative_to(repo) or path.is_relative_to(vault.data_dir):
        raise VaultError("Master password file must be outside the repository and runtime data")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise VaultError("Invalid master password file")
        if os.name != "nt" and info.st_mode & 0o077:
            raise VaultError("Master password file must not be accessible by group/others")
        data = stream.read(4097)
    try:
        password = data.decode("utf-8-sig").removesuffix("\n").removesuffix("\r")
    except UnicodeError as exc:
        raise VaultError("Master password file must use UTF-8") from exc
    vault.unlock(password)
