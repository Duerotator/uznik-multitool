from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Iterable

from core.config import AppConfig
from core.models import AccountRecord, Backend, SessionKind, utc_now_iso
from core.storage import read_json, write_json_atomic
from modules.accounts import AccountService


class UnsupportedSessionError(RuntimeError):
    pass


class SessionManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.config.sessions_dir.mkdir(parents=True, exist_ok=True)

    def import_path(
        self,
        path: Path,
        group: str = "default",
        backend_hint: str = "auto",
    ) -> list[AccountRecord]:
        paths = list(self._expand(path))
        imported: list[AccountRecord] = []
        for item in paths:
            imported.append(self.import_one(item, group=group, backend_hint=backend_hint))
        return imported

    def import_new_from_inbox(
        self,
        group: str = "inbox",
        backend_hint: str = "auto",
    ) -> tuple[list[AccountRecord], dict[str, str]]:
        self.config.import_dir.mkdir(parents=True, exist_ok=True)
        imported: list[AccountRecord] = []
        errors: dict[str, str] = {}
        known_sources = self._known_source_paths()
        known_fingerprints = self._known_session_fingerprints()

        auth_queue = (self.config.import_dir / "auth_input").resolve()
        for item in self._expand(self.config.import_dir):
            # Files waiting for re-authorization must not become ordinary
            # imported accounts before their own local session is created.
            if auth_queue == item.parent or auth_queue in item.parents:
                continue
            resolved = str(item.resolve())
            if resolved in known_sources:
                continue
            if item.is_file() and not self._is_stable_file(item):
                continue
            try:
                backend, kind = self.detect_session(item, backend_hint=backend_hint)
                fingerprint = self._session_fingerprint(item, kind)
                if fingerprint and fingerprint in known_fingerprints:
                    known_sources.add(resolved)
                    continue
                record = self.import_one(
                    item,
                    group=group,
                    backend_hint=backend_hint,
                    known_fingerprint=fingerprint,
                )
            except Exception as exc:
                errors[resolved] = str(exc)
                continue
            imported.append(record)
            known_sources.add(resolved)
            fingerprint = record.metadata.get("source_fingerprint")
            if fingerprint:
                known_fingerprints.add(str(fingerprint))

        return imported, errors

    def import_one(
        self,
        path: Path,
        group: str = "default",
        backend_hint: str = "auto",
        known_fingerprint: str | None = None,
    ) -> AccountRecord:
        path = path.resolve()
        backend, kind = self.detect_session(path, backend_hint=backend_hint)
        fingerprint = known_fingerprint or self._session_fingerprint(path, kind)
        existing = self._find_existing_by_fingerprint(fingerprint) or self._find_existing_by_source_name(path)
        if existing:
            if group not in {"", "inbox", "default"}:
                groups = self._merge_groups(existing.groups, [group])
                existing.groups = groups
                existing.group = groups[0] if groups else "inbox"
                existing.updated_at = utc_now_iso()
                self._upsert_account(existing)
            return existing
        account_id = self._make_account_id(path)
        target_dir = self.config.sessions_dir / backend
        target_dir.mkdir(parents=True, exist_ok=True)

        if kind == "tdata":
            raise UnsupportedSessionError(
                ".tdata detected. Direct Telegram Desktop conversion is intentionally "
                "not bundled because local tdata stores may be encrypted and unofficial "
                "converters are brittle. Export a Pyrogram/Telethon session or JSON "
                "session string and import that instead."
            )

        if kind == "json_dump":
            target = target_dir / f"{account_id}.json"
            shutil.copy2(path, target)
        elif kind == "session_string":
            target = target_dir / f"{account_id}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            write_json_atomic(target, payload)
        else:
            target = target_dir / f"{account_id}.session"
            shutil.copy2(path, target)

        record = AccountRecord(
            id=account_id,
            label=path.stem,
            backend=backend,
            session_kind=kind,
            session_ref=str(target),
            group=group,
            groups=[] if group in {"", "inbox", "default"} else [group],
            metadata={
                "source_path": str(path),
                "source_fingerprint": fingerprint,
                "imported_at": utc_now_iso(),
            },
        )
        self._upsert_account(record)
        return record

    def detect_session(self, path: Path, backend_hint: str = "auto") -> tuple[Backend, SessionKind]:
        if path.is_dir() and path.name.lower() == "tdata":
            return ("telethon", "tdata")
        if path.suffix.lower() == ".session":
            detected = self._detect_sqlite_session(path)
            backend = detected or self._backend_from_hint(backend_hint)
            return (backend, "session_file")
        if path.suffix.lower() == ".json":
            return self._detect_json_session(path, backend_hint=backend_hint)
        raise UnsupportedSessionError(f"Unsupported session source: {path}")

    def _expand(self, path: Path) -> Iterable[Path]:
        if path.is_file() or (path.is_dir() and path.name.lower() == "tdata"):
            yield path
            return
        if not path.exists():
            raise FileNotFoundError(path)
        for child in sorted(path.rglob("*")):
            if child.is_file() and child.suffix.lower() in {".session", ".json"}:
                yield child
            elif child.is_dir() and child.name.lower() == "tdata":
                yield child

    def _known_source_paths(self) -> set[str]:
        raw = read_json(self.config.accounts_file, {"accounts": []})
        known: set[str] = set()
        for item in raw.get("accounts", []):
            source_path = item.get("metadata", {}).get("source_path")
            if source_path:
                try:
                    known.add(str(Path(source_path).resolve()))
                except OSError:
                    known.add(str(source_path))
        return known

    def _known_session_fingerprints(self) -> set[str]:
        raw = read_json(self.config.accounts_file, {"accounts": []})
        known: set[str] = set()
        for item in raw.get("accounts", []):
            fingerprint = item.get("metadata", {}).get("source_fingerprint")
            if fingerprint:
                known.add(str(fingerprint))
        return known

    def _session_fingerprint(self, path: Path, kind: SessionKind) -> str:
        if kind == "session_string":
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if kind == "json_dump":
            payload = path.read_text(encoding="utf-8-sig")
            return hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if kind == "session_file":
            return hashlib.sha256(path.read_bytes()).hexdigest()
        return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()

    def _load(self) -> list[AccountRecord]:
        raw = read_json(self.config.accounts_file, {"accounts": []})
        return [AccountRecord.from_dict(item) for item in raw.get("accounts", [])]

    def _normalize_group(self, group: str) -> str:
        group = group.strip() or "inbox"
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", group)

    def _find_existing_by_source_name(self, path: Path) -> AccountRecord | None:
        """Fall back to the source file name when the fingerprint cannot match.

        Records imported before fingerprints existed carry none, and a session
        copied to another machine has a different source_path — so re-importing
        the same file used to create a second record for an account already in
        the registry. The file stem is the phone number in practice, and
        _make_account_id() keeps it as the id prefix, so it still ties the two
        together.
        """
        stem = self._make_account_id(path).rsplit("_", 1)[0]
        if not stem:
            return None
        for account in self._load():
            if account.metadata.get("source_fingerprint"):
                continue
            if account.id.rsplit("_", 1)[0] == stem:
                return account
        return None

    def _find_existing_by_fingerprint(self, fingerprint: str | None) -> AccountRecord | None:
        if not fingerprint:
            return None
        for account in self._load():
            if str(account.metadata.get("source_fingerprint") or "") == str(fingerprint):
                return account
        return None

    def _merge_groups(self, current: list[str], extra: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in [*current, *extra]:
            normalized = self._normalize_group(str(group))
            if normalized in {"", "inbox", "default"} or normalized in seen:
                continue
            merged.append(normalized)
            seen.add(normalized)
        return merged

    @staticmethod
    def _is_stable_file(path: Path) -> bool:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        return time.time() - stat.st_mtime > 1.5 and stat.st_size > 0

    def _detect_sqlite_session(self, path: Path) -> Backend | None:
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
        except sqlite3.DatabaseError:
            return None

        if {"sessions", "peers", "version"}.issubset(tables):
            return "pyrogram"
        if {"sessions", "entities"}.issubset(tables):
            return "telethon"
        return None

    def _detect_json_session(self, path: Path, backend_hint: str) -> tuple[Backend, SessionKind]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        backend = str(payload.get("backend") or payload.get("library") or "").lower()
        if backend not in {"pyrogram", "telethon"}:
            backend = self._backend_from_hint(backend_hint)
        kind: SessionKind = "session_string" if self._contains_session_string(payload) else "json_dump"
        return (backend, kind)

    def _contains_session_string(self, payload: dict) -> bool:
        return any(
            isinstance(payload.get(key), str) and len(payload[key]) > 64
            for key in ("session_string", "pyrogram_session", "telethon_session", "string_session")
        )

    def _backend_from_hint(self, backend_hint: str) -> Backend:
        if backend_hint in {"pyrogram", "telethon"}:
            return backend_hint  # type: ignore[return-value]
        if self.config.default_backend in {"pyrogram", "telethon"}:
            return self.config.default_backend  # type: ignore[return-value]
        return "pyrogram"

    def _make_account_id(self, path: Path) -> str:
        # Stable path identifier, not a password hash or integrity check.
        digest = hashlib.sha1(str(path).encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
        safe_name = "".join(ch if ch.isalnum() else "_" for ch in path.stem.lower()).strip("_")
        return f"{safe_name or 'account'}_{digest}"

    def _upsert_account(self, record: AccountRecord) -> None:
        raw = read_json(self.config.accounts_file, {"accounts": []})
        accounts = [AccountRecord.from_dict(item) for item in raw.get("accounts", [])]
        replaced = False
        for index, existing in enumerate(accounts):
            if existing.id == record.id:
                record.created_at = existing.created_at
                record.groups = existing.groups
                if record.group not in {"", "inbox", "default"} and record.group not in record.groups:
                    record.groups.append(record.group)
                record.group = record.groups[0] if record.groups else "inbox"
                record.updated_at = utc_now_iso()
                accounts[index] = record
                replaced = True
                break
        if not replaced:
            accounts.append(record)
        write_json_atomic(
            self.config.accounts_file,
            {"accounts": [account.to_dict() for account in accounts]},
        )
        AccountService(self.config)._sync_group_folders(accounts)
