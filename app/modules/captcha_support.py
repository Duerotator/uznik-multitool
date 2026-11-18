"""Local diagnostics, finite budgets and metadata-only manual handoff.

No challenge solving, model imports, screenshots, answers, tokens or network.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

PROVIDERS = {"random", "randombeast", "bestrandom", "fastgiveaway", "fastgiveaways", "unknown"}
KINDS = {"image", "audio", "web", "unknown"}
REASONS = {"unreadable", "unsupported", "timeout", "dependency_missing", "budget_exhausted", "manual_requested"}


class ManualRequired(RuntimeError):
    pass


def dependency_report():
    """Metadata/stat only; does not execute a binary or load/download a model."""
    packages = []
    for name in ("ddddocr", "onnxruntime", "Pillow", "playwright"):
        try:
            distribution = importlib.metadata.distribution(name)
            version = distribution.version
            if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9.+_-]{1,64}", version):
                version = "unknown"
            models = 0
            if name == "ddddocr":
                root = Path(distribution.locate_file("")).resolve()
                for item in list(distribution.files or [])[:10000]:
                    if Path(str(item)).name not in {"common.onnx", "common_old.onnx"}:
                        continue
                    candidate = Path(distribution.locate_file(item)).resolve()
                    if candidate.is_relative_to(root) and candidate.is_file() and candidate.stat().st_size > 0:
                        models += 1
            packages.append({"name": name, "status": "present", "version": version, "model_files": models if name == "ddddocr" else None})
        except importlib.metadata.PackageNotFoundError:
            packages.append({"name": name, "status": "missing", "version": None, "model_files": None})
        except Exception:
            packages.append({"name": name, "status": "unavailable", "version": None, "model_files": None})
    binaries = []
    for name, candidates in (("tesseract", ("tesseract", r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe", "/usr/bin/tesseract", "/usr/local/bin/tesseract")),
                             ("ffmpeg", ("ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"))):
        found = False
        for candidate in candidates:
            try:
                path_candidate = Path(candidate)
                if path_candidate.is_absolute() and not str(candidate).startswith(("\\\\", "//")) and path_candidate.is_file():
                    found = True
                    break
                target = shutil.which(candidate)
                if target and not str(target).startswith(("\\\\", "//")) and Path(target).is_file():
                    found = True
                    break
            except OSError:
                pass
        binaries.append({"name": name, "status": "present_not_executed" if found else "missing"})
    return {"packages": packages, "binaries": binaries, "runtime_validation": "not_run",
            "browser_installation": "not_checked", "network_requests": 0,
            "detail": "Presence is not a successful OCR/browser check. No solving, installs, model loading, key inspection or paid API calls were performed."}


class CaptchaSupportStore:
    def __init__(self, data_dir, *, clock=time.time):
        self.path, self.clock = Path(data_dir)/"captcha_manual.sqlite3", clock

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("CREATE TABLE IF NOT EXISTS manual(id TEXT PRIMARY KEY,account TEXT,provider TEXT,kind TEXT,evidence TEXT,reason TEXT,created REAL,expires REAL,state TEXT,revision TEXT,updated REAL,UNIQUE(account,provider,evidence))")
        db.execute("CREATE TABLE IF NOT EXISTS budgets(account TEXT,provider TEXT,window_start REAL,attempts INTEGER,PRIMARY KEY(account,provider))")

    @contextmanager
    def db(self, *, read_only: bool = False):
        if read_only:
            if not self.path.exists():
                yield None
                return
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA busy_timeout=5000")
                yield db
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            self._ensure_schema(db)
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            db.execute("UPDATE manual SET state='expired',updated=? WHERE state='pending' AND expires<=?", (now, now))
            db.execute("DELETE FROM manual WHERE state<>'pending' AND updated<?", (now-86400,))
            db.execute("DELETE FROM budgets WHERE window_start<?", (now-86400,))
            yield db

    @staticmethod
    def _identity(account_id, provider):
        if not isinstance(account_id, str) or not 1 <= len(account_id) <= 160 or not isinstance(provider, str) or provider not in PROVIDERS:
            raise ManualRequired("Explicit account/provider is required for manual review")

    def reserve_attempt(self, account_id, provider):
        """Reserve BEFORE a legacy action; 3 attempts/account/provider/15 min.

        Independent of image digest: changing an image cannot reset the budget.
        This method does not invoke any action or acknowledge a challenge.
        """
        self._identity(account_id, provider)
        now = self.clock()
        with self.db() as db:
            row = db.execute("SELECT * FROM budgets WHERE account=? AND provider=?", (account_id, provider)).fetchone()
            if row and now < row["window_start"]:
                raise ManualRequired("Clock moved backwards; wait or review manually")
            if row and now-row["window_start"] < 900 and row["attempts"] >= 3:
                raise ManualRequired("CAPTCHA attempt budget exhausted; stop and review manually")
            start = row["window_start"] if row and now-row["window_start"] < 900 else now
            count = row["attempts"]+1 if row and start == row["window_start"] else 1
            db.execute("INSERT OR REPLACE INTO budgets VALUES(?,?,?,?)", (account_id, provider, start, count))
            return {"attempt": count, "limit": 3, "window_ends_at": start+900}

    def require_manual(self, account_id, provider, evidence, *, kind="unknown", reason="unsupported"):
        self._identity(account_id, provider)
        if (not isinstance(kind, str) or kind not in KINDS or not isinstance(reason, str) or reason not in REASONS
                or not isinstance(evidence, bytes) or not 1 <= len(evidence) <= 2*1024*1024):
            raise ManualRequired("Invalid bounded manual challenge metadata")
        digest = hashlib.sha256(evidence).hexdigest()
        now, record_id, revision = self.clock(), uuid.uuid4().hex, uuid.uuid4().hex
        with self.db() as db:
            old = db.execute("SELECT id,state,expires FROM manual WHERE account=? AND provider=? AND evidence=?", (account_id, provider, digest)).fetchone()
            if old:
                if old["state"] == "pending" and old["expires"] > now:
                    return {"id": old["id"], "state": old["state"]}  # never extend an active pending challenge
                # Expired/acknowledged entries must not silence future challenges with the same digest.
                db.execute("DELETE FROM manual WHERE id=?", (old["id"],))
            if db.execute("SELECT count(*) FROM manual WHERE state='pending'").fetchone()[0] >= 200:
                raise ManualRequired("Manual queue is full; stop rather than submit a guess")
            db.execute("INSERT INTO manual VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (record_id, account_id, provider, kind, digest, reason, now, now+900, "pending", revision, now))
        return {"id": record_id, "state": "pending"}

    def list(self, account_ids):
        if not isinstance(account_ids, list) or not account_ids:
            raise ManualRequired("Select explicit accounts for the manual queue")
        for account in account_ids:
            self._identity(account, "unknown")
        ids = list(dict.fromkeys(account_ids))
        if len(ids) > 200:
            raise ManualRequired("Select at most 200 accounts for manual queue review")
        now = self.clock()
        with self.db(read_only=True) as db:
            if db is None:
                return []
            try:
                has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual'").fetchone()
            except sqlite3.DatabaseError:
                return []
            if not has_table:
                return []
            # Only placeholder COUNT is composed; values remain SQL parameters.
            placeholders = ",".join("?" for _ in ids)
            rows = db.execute(
                "SELECT id,account,provider,kind,reason,created,expires,"
                "CASE WHEN state='pending' AND expires<=? THEN 'expired' ELSE state END AS state,"
                "revision FROM manual WHERE account IN (" + placeholders + ") "  # nosec: B608
                "ORDER BY (state='pending' AND expires>?) DESC,created DESC LIMIT 200",
                [now, *ids, now],
            ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge(self, account_id, record_id, revision, *, confirmed=False):
        self._identity(account_id, "unknown")
        if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value) for value in (record_id, revision)):
            raise ManualRequired("Choose a current manual queue item and revision")
        if confirmed is not True:
            raise ManualRequired("Confirm manual review; no answer or resubmission is accepted here")
        now = self.clock()
        with self.db() as db:
            row = db.execute("SELECT * FROM manual WHERE id=? AND account=?", (record_id, account_id)).fetchone()
            if not row or row["revision"] != revision or row["state"] != "pending" or not row["created"] <= now < row["expires"]:
                raise ManualRequired("Manual challenge changed or expired; do not replay it")
            db.execute("UPDATE manual SET state='acknowledged',revision=?,updated=? WHERE id=?", (uuid.uuid4().hex, now, record_id))
        return {"acknowledged": 1, "detail": "Manual review noted locally. No Telegram/browser action, answer submission or join success was inferred."}


def manual_notice(config, account_id, provider, evidence=None, *, reason="unsupported", kind="image"):
    # Never persist the supplied evidence, URL, answer or exception text.
    safe = evidence if isinstance(evidence, bytes) and 1 <= len(evidence) <= 2*1024*1024 else b"unavailable-or-oversized-evidence"
    CaptchaSupportStore(config.data_dir).require_manual(account_id, provider, safe, kind=kind, reason=reason)
    return f"Manual CAPTCHA review required ({reason}). Stopped without a new answer; use Open acc and review manually. No automatic resume."


async def bounded_legacy_stage(config, account_id, provider, evidence, operation):
    """Bound an existing local stage; never add recognition, retries or API calls.

    Timeout cancels the await, not an already running Python thread. A late
    local result is ignored; external OCR subprocess limits remain in force.
    """
    if not isinstance(evidence, bytes) or not 1 <= len(evidence) <= 2*1024*1024:
        raise ManualRequired(manual_notice(config, account_id, provider, reason="unsupported"))
    try:
        CaptchaSupportStore(config.data_dir).reserve_attempt(account_id, provider)
    except ManualRequired:
        raise ManualRequired(manual_notice(config, account_id, provider, evidence, reason="budget_exhausted")) from None
    try:
        async with asyncio.timeout(25):
            value = await operation()
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise ManualRequired(manual_notice(config, account_id, provider, evidence, reason="timeout")) from None
    except Exception:
        raise ManualRequired(manual_notice(config, account_id, provider, evidence, reason="unreadable")) from None
    if not isinstance(value, str) or not value or len(value) > 16:
        raise ManualRequired(manual_notice(config, account_id, provider, evidence, reason="unreadable"))
    return value


class CaptchaSupportService:
    """Frontend-safe local review; never takes an answer or resumes a task."""
    def __init__(self, config):
        self.config = config

    def _account(self, account_id):
        from modules.accounts import AccountService
        try:
            account = AccountService(self.config).get_account(account_id)
        except KeyError:
            raise ManualRequired("Selected account was removed; refresh selection") from None
        if not account.enabled:
            raise ManualRequired("Selected account is disabled; no manual queue action was applied")
        return account

    @staticmethod
    def _context(account):
        public = [account.id, account.user_id, account.group, sorted(account.groups),
                  account.enabled, account.backend, account.session_ref]
        return hashlib.sha256(("captcha-review:"+json.dumps(public, ensure_ascii=False)).encode()).hexdigest()

    def report(self, account_id=None):
        if account_id is not None and (not isinstance(account_id, str) or not account_id):
            raise ManualRequired("Choose one explicit account or diagnostics-only")
        account = self._account(account_id) if account_id is not None else None
        return {"account_id": account_id, "account_context": self._context(account) if account else None,
                "diagnostics": dependency_report(),
                "items": CaptchaSupportStore(self.config.data_dir).list([account_id]) if account else []}

    def acknowledge(self, account_id, record_id, revision, expected_context, *, confirmed=False):
        account = self._account(account_id)
        if not expected_context or expected_context != self._context(account):
            raise ManualRequired("Account identity/group changed; reload before acknowledging")
        CaptchaSupportStore(self.config.data_dir).acknowledge(account_id, record_id, revision, confirmed=confirmed)
        return self.report(account_id)
