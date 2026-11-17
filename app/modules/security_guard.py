"""Opt-in read-only Guard. No login codes, reset declines or session revocations."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from core.models import utc_now_iso
from core.results import ActionResult
from core.telegram_client import create_client
from modules.accounts import AccountService
from utils.telegram_errors import is_frozen_error


class GuardConflict(RuntimeError):
    pass


def source_key(account):
    ref = str(getattr(account, "session_ref", "") or "")
    normalized = ref.replace("\\", "/")
    marker = "/data/sessions/"
    idx = normalized.lower().rfind(marker)
    if idx != -1:
        portable = "sessions/" + normalized[idx + len(marker):]
        if os.name == "nt":
            portable = portable.lower()
        return hashlib.sha256(f"{account.backend}/{portable}".encode()).hexdigest()
    resolved = str(Path(ref).resolve())
    if os.name == "nt":
        resolved = resolved.lower()
    return hashlib.sha256(f"{account.backend}/{resolved}".encode()).hexdigest()


def _validate(snapshot):
    expected = {"owner_id", "sessions", "twofa_enabled", "recovery_email_set", "password_reset_pending",
                "password_reset_deadline", "other_sessions", "unconfirmed_sessions"}
    if not isinstance(snapshot, dict) or set(snapshot) != expected:
        raise GuardConflict("Incomplete Guard snapshot")
    if not isinstance(snapshot["owner_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,19}", snapshot["owner_id"]):
        raise GuardConflict("Invalid Guard owner")
    for key in ("twofa_enabled", "recovery_email_set", "password_reset_pending"):
        if type(snapshot[key]) is not bool:
            raise GuardConflict("Invalid Guard flags")
    deadline = snapshot["password_reset_deadline"]
    if deadline is not None and (type(deadline) is not int or deadline <= 0):
        raise GuardConflict("Invalid Guard reset deadline")
    if snapshot["password_reset_pending"] != bool(deadline):
        raise GuardConflict("Inconsistent Guard reset state")
    rows = snapshot["sessions"]
    if not isinstance(rows, list) or len(rows) > 1000:
        raise GuardConflict("Invalid authorization list")
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"fingerprint", "label", "unconfirmed"}
                or not isinstance(row["fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["fingerprint"])
                or not isinstance(row["label"], str) or len(row["label"]) > 96 or type(row["unconfirmed"]) is not bool):
            raise GuardConflict("Invalid authorization evidence")
    if len({row["fingerprint"] for row in rows}) != len(rows):
        raise GuardConflict("Duplicate authorization evidence")
    for key, count in (("other_sessions", len(rows)), ("unconfirmed_sessions", sum(row["unconfirmed"] for row in rows))):
        if type(snapshot[key]) is not int or snapshot[key] != count:
            raise GuardConflict("Inconsistent authorization count")


class GuardStore:
    def __init__(self, data_dir, *, clock=time.time):
        self.path = Path(data_dir)/"security_guard.sqlite3"
        self.clock = clock
        self._init_lock = threading.Lock()
        self._initialized = False

    def _ensure_schema(self):
        if self._initialized and self.path.exists():
            return
        with self._init_lock:
            if self._initialized and self.path.exists():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE IF NOT EXISTS observations(account TEXT PRIMARY KEY, source TEXT, owner TEXT, snapshot TEXT, token TEXT, checked REAL, error TEXT, error_at REAL, due REAL NOT NULL DEFAULT 0, baseline TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS events(account TEXT, kind TEXT, evidence TEXT, first_seen REAL, last_seen REAL, active INTEGER, PRIMARY KEY(account,kind,evidence))")
                db.execute("CREATE TABLE IF NOT EXISTS leases(account TEXT PRIMARY KEY, token TEXT, expires REAL)")
            self._initialized = True

    @contextmanager
    def db(self, *, write=True):
        if write:
            self._ensure_schema()
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("BEGIN IMMEDIATE")
                yield db
            return
        if not self.path.exists():
            yield None
            return
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA query_only=ON")
            has_table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
            ).fetchone()
            yield db if has_table else None

    def claim(self, account_id):
        now = self.clock()
        token = uuid.uuid4().hex
        with self.db() as db:
            row = db.execute("SELECT due FROM observations WHERE account=?", (account_id,)).fetchone()
            lease = db.execute("SELECT expires FROM leases WHERE account=?", (account_id,)).fetchone()
            if (row and row["due"] > now) or (lease and lease["expires"] > now):
                return None
            db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?)", (account_id, token, now+120))
        return token

    def _lease(self, db, account_id, token):
        row = db.execute("SELECT token,expires FROM leases WHERE account=?", (account_id,)).fetchone()
        if not row or row["token"] != token or row["expires"] <= self.clock():
            raise GuardConflict("A newer Guard worker owns this observation")

    def release(self, account_id, token):
        with self.db() as db:
            db.execute("DELETE FROM leases WHERE account=? AND token=?", (account_id, token))

    def save(self, account, snapshot, lease, *, extra_conditions=()):
        _validate(snapshot)
        if account.user_id is not None and str(account.user_id) != snapshot["owner_id"]:
            raise GuardConflict("Telegram owner differs from the selected account")
        now, source, token = self.clock(), source_key(account), uuid.uuid4().hex
        with self.db() as db:
            self._lease(db, account.id, lease)
            old = db.execute("SELECT * FROM observations WHERE account=?", (account.id,)).fetchone()
            baseline = old["baseline"] if old and old["source"] == source and old["owner"] == snapshot["owner_id"] else None
            trusted = set(json.loads(baseline)) if baseline is not None else set()
            wanted = self._conditions(snapshot, trusted, baseline is not None)
            if extra_conditions:
                wanted.update(extra_conditions)
            if old and old["owner"] and (old["source"] != source or old["owner"] != snapshot["owner_id"]):
                wanted.add(("identity_changed", source+"/"+snapshot["owner_id"]))
            self._events(db, account.id, wanted, now)
            db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,NULL,NULL,?,?)",
                       (account.id, source, snapshot["owner_id"], json.dumps(snapshot), token, now, now+300, baseline))
        return self.report(account.id, account=account)

    @staticmethod
    def _conditions(snapshot, trusted, baseline_exists):
        conditions = set()
        if not baseline_exists:
            conditions.add(("baseline_missing", "review"))
        for row in snapshot["sessions"]:
            if row["fingerprint"] not in trusted:
                conditions.add(("unknown_authorization", row["fingerprint"]))
            if row["unconfirmed"]:
                conditions.add(("unconfirmed_authorization", row["fingerprint"]))
        if snapshot["password_reset_pending"]:
            conditions.add(("password_reset", str(snapshot["password_reset_deadline"])))
        if not snapshot["twofa_enabled"]:
            conditions.add(("twofa_off", "observed"))
        if not snapshot["recovery_email_set"]:
            conditions.add(("recovery_off", "observed"))
        return conditions

    @staticmethod
    def _events(db, account_id, conditions, now):
        db.execute("UPDATE events SET active=0 WHERE account=?", (account_id,))
        for kind, evidence in conditions:
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,1) ON CONFLICT(account,kind,evidence) DO UPDATE SET active=1,last_seen=excluded.last_seen",
                       (account_id, kind, evidence, now, now))
        # Bound resolved history; active alerts are never pruned.
        db.execute("DELETE FROM events WHERE account=? AND active=0 AND rowid NOT IN (SELECT rowid FROM events WHERE account=? ORDER BY last_seen DESC LIMIT 200)", (account_id, account_id))

    def failed(self, account_id, lease, error_kind, *, cooldown=300):
        # Only the exception class is stored, never message text or credentials.
        now = self.clock()
        kind = error_kind if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", error_kind) else "GuardError"
        with self.db() as db:
            self._lease(db, account_id, lease)
            db.execute("INSERT OR IGNORE INTO observations(account) VALUES(?)", (account_id,))
            db.execute("UPDATE observations SET error=?,error_at=?,due=? WHERE account=?", (kind, now, now+max(300, cooldown), account_id))

    def report(self, account_id, *, account=None):
        now = self.clock()
        with self.db(write=False) as db:
            if db is None:
                return {"account_id": account_id, "status": "never_checked", "snapshot": None, "events": []}
            row = db.execute("SELECT * FROM observations WHERE account=?", (account_id,)).fetchone()
            events = [dict(item) for item in db.execute("SELECT kind,evidence,first_seen,last_seen,active FROM events WHERE account=? ORDER BY active DESC,last_seen DESC LIMIT 200", (account_id,))]
        if not row:
            return {"account_id": account_id, "status": "never_checked", "snapshot": None, "events": []}
        age = None if row["checked"] is None else max(0, int(now-row["checked"]))
        binding_changed = bool(account and row["source"] and (row["source"] != source_key(account)
                               or (account.user_id is not None and str(account.user_id) != row["owner"])))
        stale = binding_changed or age is None or now < row["checked"] or age > 1800 or bool(row["error"])
        return {"account_id": account_id, "status": "stale" if stale else "needs_review" if any(e["active"] for e in events) else "observed",
                "snapshot": json.loads(row["snapshot"]) if row["snapshot"] else None,
                "snapshot_token": row["token"], "checked_at": row["checked"], "age_seconds": age,
                "binding_changed": binding_changed,
                "error": row["error"], "error_at": row["error_at"], "next_check_at": row["due"],
                "baseline": json.loads(row["baseline"]) if row["baseline"] is not None else None, "events": events}

    def trust(self, account, snapshot_token, fingerprints, *, confirmed=False):
        if confirmed is not True or not isinstance(fingerprints, list) or any(not isinstance(value, str) for value in fingerprints):
            raise GuardConflict("Explicit reviewed authorization selection is required")
        now = self.clock()
        with self.db() as db:
            row = db.execute("SELECT * FROM observations WHERE account=?", (account.id,)).fetchone()
            if (not row or not snapshot_token or row["token"] != snapshot_token or row["error"]
                    or not 0 <= now-row["checked"] <= 600 or row["source"] != source_key(account)
                    or (account.user_id is not None and str(account.user_id) != row["owner"])):
                raise GuardConflict("Observation changed, expired or failed; check again before trusting")
            snapshot = json.loads(row["snapshot"])
            selectable = {item["fingerprint"] for item in snapshot["sessions"] if not item["unconfirmed"]}
            trusted = set(fingerprints)
            if not trusted <= selectable:
                raise GuardConflict("Unknown or unconfirmed authorization cannot be trusted")
            db.execute("UPDATE observations SET baseline=?,token=? WHERE account=?", (json.dumps(sorted(trusted)), uuid.uuid4().hex, account.id))
            self._events(db, account.id, self._conditions(snapshot, trusted, True), now)
        return self.report(account.id)


@dataclass
class GuardResult(ActionResult):
    skipped: int = 0

    def summary(self, label):
        return super().summary(label)+f", skipped={self.skipped}"


async def pause(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


class SecurityGuardService:
    def __init__(self, config):
        self.config = config
        self.accounts = AccountService(config)
        self.store = GuardStore(config.data_dir)
        self.log = logging.getLogger("security-guard")

    def targets(self, ids):
        if not isinstance(ids, list) or not ids:
            raise GuardConflict("Select explicit accounts; no implicit whole-group Guard")
        wanted = set(ids)
        result = [item for item in self.accounts.list_accounts() if item.id in wanted and item.enabled]
        if {item.id for item in result} != wanted:
            raise GuardConflict("Selection was removed or disabled; refresh it")
        return result

    def trust(self, account_id, token, fingerprints, *, confirmed=False):
        account = self.targets([account_id])[0]
        return self.store.trust(account, token, fingerprints, confirmed=confirmed)

    def report(self, account_id):
        account = self.targets([account_id])[0]
        return self.store.report(account_id, account=account)

    async def check(self, ids, *, cycles=1, interval=900, stop_event=None, progress=None):
        if type(cycles) is not int or not 1 <= cycles <= 96 or type(interval) is not int or not 300 <= interval <= 86400:
            raise GuardConflict("Guard requires 1–96 finite passes and 300–86400 second intervals")
        scoped = self.targets(ids)
        stop = stop_event if stop_event is not None else asyncio.Event()
        result = GuardResult()
        if progress:
            progress.set_total(len(scoped)*cycles)
        for cycle in range(cycles):
            for index, original in enumerate(scoped):
                if stop.is_set():
                    return result
                lease = None
                freeze_meta: dict[str, object] = {}
                try:
                    try:
                        account = await asyncio.to_thread(self.accounts.get_account, original.id)
                    except KeyError:
                        account = None
                    if (account is None or not account.enabled or source_key(account) != source_key(original)
                            or account.group != original.group or account.groups != original.groups):
                        result.skipped += 1
                        result.details[original.id] = "Skipped: selected account changed or was disabled/removed."
                    else:
                        lease = await asyncio.to_thread(self.store.claim, account.id)
                        if not lease:
                            result.skipped += 1
                            result.details[account.id] = "Skipped: persistent cooldown or another Guard check; previous observation is not refreshed."
                        else:
                            extra_conditions: set[tuple[str, str]] = set()
                            async with asyncio.timeout(90):
                                async with create_client(self.config, account) as client:
                                    try:
                                        snapshot = await client.inspect_guard()
                                    except Exception as inspect_exc:
                                        if is_frozen_error(inspect_exc) and hasattr(client, "get_app_config"):
                                            try:
                                                app_cfg = await asyncio.wait_for(client.get_app_config(), timeout=10)
                                                for key in ("freeze_since_date", "freeze_until_date", "freeze_appeal_url"):
                                                    if app_cfg.get(key):
                                                        freeze_meta[key] = app_cfg[key]
                                            except Exception:
                                                pass
                                        raise
                                    if (
                                        account.backend == "pyrogram"
                                        and hasattr(client, "client")
                                        and getattr(client, "client", None) is not None
                                    ):
                                        try:
                                            from core.passkey_service import PasskeyService
                                            pk_dir = str(self.config.data_dir / "passkeys")
                                            pk_svc = PasskeyService(self.config.api_id, self.config.api_hash, passkeys_dir=pk_dir)
                                            remote_pks = await pk_svc.get_remote_passkeys(client.client)
                                            remote_ids = [str(getattr(pk, "id", "")) for pk in remote_pks if getattr(pk, "id", None)]
                                            local_id = None
                                            try:
                                                local_id = str(pk_svc.load_passkey(account.id).get("passkey_id") or "")
                                            except FileNotFoundError:
                                                local_id = None
                                            for pid in remote_ids:
                                                if pid != local_id:
                                                    extra_conditions.add(("unknown_passkey", pid[:80]))
                                        except Exception:
                                            pass
                            # Refresh scope after the request too; an account may
                            # have been removed/replaced while the client ran.
                            current = await asyncio.to_thread(self.accounts.get_account, account.id)
                            if (not current.enabled or source_key(current) != source_key(account)
                                    or current.group != original.group or current.groups != original.groups):
                                raise GuardConflict("Selected account changed during check")
                            report = await asyncio.to_thread(
                                lambda: self.store.save(current, snapshot, lease, extra_conditions=extra_conditions)
                            )
                            result.add_ok(account.id)
                            active = [event["kind"] for event in report["events"] if event["active"]]
                            result.details[account.id] = ("Guard observation: "+(", ".join(sorted(set(active))) or "no listed conditions")
                                                          +". Read-only; not proof of SIM safety.")
                            self.log.warning("%s: %s", account.id, result.details[account.id]) if active else self.log.info("%s: observed", account.id)
                            if progress:
                                progress.mark_ok(original.id)
                except asyncio.CancelledError:
                    if lease:
                        try:
                            await asyncio.to_thread(self.store.failed, original.id, lease, "CancelledError")
                        except GuardConflict:
                            pass
                    raise
                except Exception as exc:
                    if is_frozen_error(exc):
                        try:
                            await asyncio.to_thread(
                                self.accounts.update_profile_metadata,
                                original.id,
                                {"health_status": "frozen", "health_checked_at": utc_now_iso(), **freeze_meta},
                            )
                            await asyncio.to_thread(
                                self.accounts.mark_error, original.id, type(exc).__name__, disable=True
                            )
                        except Exception:
                            pass
                    if lease:
                        seconds = getattr(exc, "value", None) or getattr(exc, "seconds", None)
                        cooldown = seconds if type(seconds) is int and seconds > 0 else 300
                        try:
                            await asyncio.to_thread(
                                self.store.failed, original.id, lease, type(exc).__name__, cooldown=cooldown
                            )
                        except GuardConflict:
                            pass
                    result.add_error(original.id, f"Guard observation failed ({type(exc).__name__}); previous snapshot is stale.")
                    if progress:
                        progress.mark_error(original.id)
                finally:
                    if lease:
                        await asyncio.to_thread(self.store.release, original.id, lease)
                if index+1 < len(scoped) and await pause(stop, 2):
                    return result
            if cycle+1 < cycles and await pause(stop, interval):
                return result
        return result
