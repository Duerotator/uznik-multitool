"""Metadata-only durable task history. No serialized callbacks, secrets or auto replay."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

OPERATIONS = frozenset({"security-guard", "scan-security", "check-sessions", "check-spamblock", "check-account-age",
    "profile-apply", "apply-saved-profiles", "premium-stories", "profile-scrape", "giveaway-join", "open-account",
    "set-cloud-password", "remove-cloud-password", "change-login-email", "bind-recovery-email", "import",
    "online-mode", "warmup", "proxy-check", "proxy-refresh", "scenario-run", "asset-public-read",
    "process-external-sessions", "assign-proxy", "check-proxies", "download-avatar-pack", "clear-full-profile",
    "rollback-profile-snapshot", "terminate-pc-sessions", "terminate-other-sessions", "delete-passkeys",
    "add-passkeys", "restore-passkeys", "get-telegram-codes", "proxy-assign", "proxy-assign-all", "profile", "chat", "studio-apply"})


class HistoryConflict(RuntimeError):
    pass


def process_alive(pid):
    """Fail closed on access/unknown errors; Windows must NOT use os.kill(pid, 0)."""
    if type(pid) is not int or pid <= 0:
        return True
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() not in {87, 1168}
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def checked_spec(spec):
    if spec is None:
        return None
    if (not isinstance(spec, dict) or set(spec) != {"kind", "account_ids", "context"} or spec["kind"] != "guard-once"
            or not isinstance(spec["context"], str) or not re.fullmatch(r"[a-f0-9]{64}", spec["context"])):
        raise HistoryConflict("Only an exact read-only Guard plan can be resumed")
    ids = spec["account_ids"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 250 or any(not isinstance(value, str) or not 1 <= len(value) <= 160 for value in ids)
            or len(set(ids)) != len(ids)):
        raise HistoryConflict("Choose 1–250 unique exact Guard accounts")
    return {**spec, "account_ids": sorted(ids)}


def checked_done(row, spec):
    done = json.loads(row["done"])
    if (not isinstance(done, list) or any(not isinstance(value, str) for value in done) or len(set(done)) != len(done)
            or not set(done) <= set(spec["account_ids"] if spec else [])):
        raise HistoryConflict("Checkpoint metadata is inconsistent; manual review required")
    return done


class TaskHistory:
    def __init__(self, data_dir, *, clock=time.time, alive=process_alive):
        self.path, self.clock, self.alive = Path(data_dir)/"task_history.sqlite3", clock, alive
        self._init_lock = threading.Lock()
        self._initialized = False

    def _ensure_schema(self) -> None:
        if self._initialized and self.path.exists():
            return
        with self._init_lock:
            if self._initialized and self.path.exists():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
                db.execute("PRAGMA busy_timeout=5000")
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("""CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY,operation TEXT,source TEXT,status TEXT,created REAL,updated REAL,pid INTEGER,
                    revision TEXT,spec TEXT,done TEXT,total INTEGER,completed INTEGER,errors INTEGER,error_kind TEXT,child TEXT)""")
            self._initialized = True

    @contextmanager
    def db(self, *, write: bool = True):
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
            has_jobs = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()
            yield db if has_jobs else None

    def _insert(self, db, operation, source, spec, done=()):
        if source not in {"qt", "web"}:
            raise HistoryConflict("Unknown task source")
        name = str(operation).split(":", 1)[0]
        name = name if name in OPERATIONS else "other"
        task_id, revision, now = uuid.uuid4().hex, uuid.uuid4().hex, self.clock()
        db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (task_id, name, source, "pending", now, now,
            os.getpid(), revision, json.dumps(spec), json.dumps(sorted(done)), len(spec["account_ids"]) if spec else 0,
            len(done), 0, "", ""))
        # Never prune active records or a parent whose child is still active.
        db.execute("DELETE FROM jobs WHERE id IN (SELECT j.id FROM jobs j WHERE j.status NOT IN ('pending','running') AND NOT EXISTS (SELECT 1 FROM jobs c WHERE c.id=j.child AND c.status IN ('pending','running')) ORDER BY j.updated DESC LIMIT -1 OFFSET 200)")
        return task_id

    def create(self, operation, source, spec=None):
        spec = checked_spec(spec)
        if spec and operation != "security-guard":
            raise HistoryConflict("Resume plan does not match the supported operation")
        with self.db() as db:
            return self._insert(db, operation, source, spec)

    def get(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{32}", task_id):
            raise HistoryConflict("Choose a recorded task")
        with self.db(write=False) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone() if db is not None else None
        if not row:
            raise HistoryConflict("Task no longer exists")
        return dict(row)

    def effective_status(self, row):
        return "interrupted" if row["status"] in {"running", "pending"} and not self.alive(row["pid"]) else row["status"]

    def list(self):
        with self.db(write=False) as db:
            if db is None:
                return []
            rows = [dict(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 200")]
        result = []
        for row in rows:
            status = self.effective_status(row)
            spec = checked_spec(json.loads(row["spec"]))
            pending = set(spec["account_ids"])-set(checked_done(row, spec)) if spec else set()
            resumable = bool(spec and pending and status in {"stopped", "failed", "partial", "interrupted"} and not row["child"])
            result.append({key: row[key] for key in ("id", "operation", "source", "created", "updated", "revision", "total", "completed", "errors", "error_kind")}
                          | {"status": status, "resumable": resumable,
                             "resume_reason": "Explicit selection/context confirmation required" if resumable else "Not resumable; never automatically replayed"})
        return result

    def running(self, task_id):
        with self.db() as db:
            if db.execute("UPDATE jobs SET status='running',updated=? WHERE id=? AND status='pending' AND pid=?", (self.clock(), task_id, os.getpid())).rowcount != 1:
                raise HistoryConflict("Task is already started or unavailable")

    def progress(self, task_id, snapshot):
        values = [snapshot.get(key, 0) for key in ("total", "completed", "errors")]
        if any(type(value) is not int or not 0 <= value <= 10000000 for value in values):
            raise HistoryConflict("Invalid task progress")
        with self.db() as db:
            db.execute("UPDATE jobs SET total=?,completed=?,errors=?,updated=? WHERE id=? AND status='running' AND spec='null' AND pid=?", (*values, self.clock(), task_id, os.getpid()))

    def checkpoint(self, task_id, account_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND status='running' AND pid=?", (task_id, os.getpid())).fetchone()
            if not row:
                raise HistoryConflict("Task no longer owns this checkpoint")
            spec = checked_spec(json.loads(row["spec"]))
            if not spec or account_id not in spec["account_ids"]:
                raise HistoryConflict("Checkpoint is outside the approved selection")
            done = sorted(set(checked_done(row, spec)) | {account_id})
            db.execute("UPDATE jobs SET done=?,completed=?,updated=? WHERE id=?", (json.dumps(done), len(done), self.clock(), task_id))

    def finish(self, task_id, status, *, result=None, error=None):
        if status not in {"done", "failed", "stopped"}:
            raise HistoryConflict("Invalid terminal task state")
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND pid=?", (task_id, os.getpid())).fetchone()
            if not row or row["status"] not in {"running", "pending"}:
                return
            spec = checked_spec(json.loads(row["spec"]))
            errors = getattr(result, "errors", 0)
            errors = errors if type(errors) is int and 0 <= errors <= 10000000 else row["errors"]
            if status == "done" and (errors or (spec and set(spec["account_ids"])-set(checked_done(row, spec)))):
                status = "partial"
            # Only exception class, never message, URL, passwords or result.details.
            kind = type(error).__name__[:80] if error else ""
            db.execute("UPDATE jobs SET status=?,errors=?,error_kind=?,updated=?,revision=? WHERE id=?", (status, errors, kind, self.clock(), uuid.uuid4().hex, task_id))

    def resume(self, task_id, revision, spec, source, *, confirmed=False):
        spec = checked_spec(spec)
        if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value) for value in (task_id, revision)):
            raise HistoryConflict("Choose a current recorded task revision")
        if confirmed is not True or spec is None:
            raise HistoryConflict("Confirm a supported read-only plan and exact scope")
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (task_id,)).fetchone()
            if (not row or row["revision"] != revision or row["child"] or row["operation"] != "security-guard"
                    or checked_spec(json.loads(row["spec"])) != spec
                    or self.effective_status(row) not in {"stopped", "failed", "partial", "interrupted"}):
                raise HistoryConflict("Task changed, still running, or scope no longer matches")
            done = checked_done(row, spec)
            if set(done) >= set(spec["account_ids"]):
                raise HistoryConflict("No unfinished accounts remain")
            child = self._insert(db, row["operation"], source, spec, done)
            db.execute("UPDATE jobs SET child=?,revision=? WHERE id=?", (child, uuid.uuid4().hex, task_id))
            return child
