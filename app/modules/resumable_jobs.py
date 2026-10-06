"""Explicitly resumable finite batches; no passwords or executable closures on disk."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from core.results import ActionResult
from core.storage import read_json, update_json
from core.models import utc_now_iso
from modules.accounts import AccountService


OPERATIONS = {"check-sessions", "check-spamblock", "apply-saved-profiles"}
PROFILE_OPTIONS = {"copy_name", "copy_bio", "copy_username", "copy_avatars", "copy_music",
                   "copy_stories", "copy_birthday", "accept_legacy_stories", "max_stories_per_account"}


def validate_options(operation: str, options: dict) -> None:
    allowed = PROFILE_OPTIONS if operation == "apply-saved-profiles" else set()
    if operation not in OPERATIONS or not isinstance(options, dict) or set(options) - allowed:
        raise ValueError("Unsupported or secret batch parameters")
    for key, value in options.items():
        if key == "max_stories_per_account":
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Invalid story limit")
        elif type(value) is not bool:
            raise ValueError("Invalid batch option")


class ResumableJobs:
    def __init__(self, config):
        self.config = config
        self.path = Path(config.data_dir) / "resumable_jobs.json"

    def get(self, job_id: str) -> dict[str, Any]:
        return read_json(self.path, {"jobs": {}})["jobs"][job_id]

    def update(self, job_id: str, **fields: Any) -> dict[str, Any]:
        def mutate(data):
            data["jobs"][job_id].update(fields, updated_at=utc_now_iso())
            return data
        return update_json(self.path, {"jobs": {}}, mutate)["jobs"][job_id]

    def mark(self, job_id: str, account_id: str, status: str, detail: str = "") -> None:
        def mutate(data):
            job = data["jobs"][job_id]
            job["accounts"][account_id] = {"status": status, "detail": detail}
            job["updated_at"] = utc_now_iso()
            return data
        update_json(self.path, {"jobs": {}}, mutate)

    def create(self, operation: str, accounts, *, group: str = "", options: dict | None = None) -> str:
        if operation not in OPERATIONS:
            raise ValueError("This operation cannot be resumed safely")
        options = dict(options or {})
        validate_options(operation, options)
        job_id = uuid.uuid4().hex
        record = {"id": job_id, "operation": operation, "group": group, "options": options,
                  "created_at": utc_now_iso(), "status": "pending",
                  "accounts": {a.id: {"status": "pending", "detail": ""} for a in accounts}}
        def mutate(data):
            data.setdefault("jobs", {})[job_id] = record
            return data
        update_json(self.path, {"jobs": {}}, mutate)
        return job_id

    def pending_ids(self, job_id: str) -> list[str]:
        return [key for key, item in self.get(job_id)["accounts"].items() if item["status"] != "done"]

    def incomplete(self) -> list[dict]:
        jobs = read_json(self.path, {"jobs": {}}).get("jobs", {}).values()
        return sorted((job for job in jobs if any(a["status"] != "done" for a in job["accounts"].values())),
                      key=lambda job: job["created_at"], reverse=True)

    async def _execute(self, operation: str, account, options: dict) -> ActionResult:
        if operation == "check-sessions":
            from modules.session_health import SessionHealthService
            return await SessionHealthService(self.config).validate([account])
        if operation == "check-spamblock":
            from modules.spamblock import SpamBlockService
            return await SpamBlockService(self.config).check([account])
        if operation == "apply-saved-profiles":
            from modules.profile_scraper import ProfileScraperService
            return await ProfileScraperService(self.config).apply_saved_profiles([account], **options)
        raise ValueError("Unknown resumable operation")

    async def run(self, job_id: str, stop: asyncio.Event, *, progress=None, execute=None) -> ActionResult:
        job = self.get(job_id)
        if job["operation"] not in OPERATIONS:
            raise ValueError("Unknown resumable operation")
        # Validate persisted options too, without creating a second job.
        validate_options(job["operation"], job["options"])
        account_service = AccountService(self.config)
        pending = self.pending_ids(job_id)
        if progress:
            progress.set_total(len(pending))
        result = ActionResult()
        self.update(job_id, status="running")
        try:
            for account_id in pending:
                if stop.is_set():
                    self.update(job_id, status="stopped")
                    return result
                account = next((a for a in account_service.list_accounts() if a.id == account_id), None)
                self.mark(job_id, account_id, "running")
                try:
                    if account is None or not account.enabled:
                        raise RuntimeError("Account is missing or disabled; not replaced by another account")
                    outcome = await (execute or self._execute)(job["operation"], account, job["options"])
                    if outcome.errors or outcome.ok != 1:
                        raise RuntimeError(outcome.details.get(account_id) or "Operation did not confirm success")
                    self.mark(job_id, account_id, "done")
                    result.add_ok(account_id)
                    if progress:
                        progress.mark_ok(account_id)
                except asyncio.CancelledError:
                    self.mark(job_id, account_id, "interrupted")
                    raise
                except Exception as exc:
                    self.mark(job_id, account_id, "failed", type(exc).__name__)
                    result.add_error(account_id, str(exc))
                    if progress:
                        progress.mark_error(account_id)
            self.update(job_id, status="partial" if self.pending_ids(job_id) else "done")
            return result
        except BaseException:
            self.update(job_id, status="interrupted")
            raise
