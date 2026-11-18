"""Account removal releases local references without deleting archived profiles."""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import weakref
from contextlib import closing
from pathlib import Path

from core.storage import update_json


_BROWSERS: dict[str, weakref.WeakSet] = {}


def register_browser_service(data_dir: Path, service) -> None:
    _BROWSERS.setdefault(str(Path(data_dir).resolve()), weakref.WeakSet()).add(service)


def remove_browser_profile(data_dir: Path, account_id: str) -> None:
    root = (Path(data_dir) / "browser_profiles").resolve()
    target = root / hashlib.sha256(account_id.encode()).hexdigest()[:24]
    if target.is_symlink() or target.resolve().parent != root:
        raise RuntimeError("Unsafe browser profile path")
    if target.is_dir():
        shutil.rmtree(target)


def _pop_accounts_from_json(path: Path, wanted: set[str], containers: tuple[str | None, ...]) -> None:
    if not path.exists():
        return

    def mutate(data):
        if not isinstance(data, dict):
            return data
        for container in containers:
            if container is None:
                records = data
            else:
                records = data.get(container)
                if not isinstance(records, dict):
                    continue
            for key in wanted:
                records.pop(key, None)
        return data

    update_json(path, {}, mutate, skip_unchanged=True)


def _cleanup_sqlite_tables(path: Path, wanted: list[str], tables: tuple[str, ...]) -> None:
    if not path.exists() or not wanted:
        return
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        existing = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        target_tables = [table for table in tables if table in existing]
        if not target_tables:
            return
        db.execute("BEGIN IMMEDIATE")
        for offset in range(0, len(wanted), 500):
            chunk = wanted[offset : offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            for table in target_tables:
                db.execute(f"DELETE FROM {table} WHERE account IN ({placeholders})", chunk)  # nosec: B608


def cleanup_removed_accounts(config, account_ids: list[str]) -> None:
    wanted = set(account_ids)
    if not wanted:
        return
    data_dir = Path(config.data_dir)
    for filename, containers in (
        ("profile_bindings.json", ("bindings",)),
        ("fingerprints.json", (None,)),
        ("story_publications.json", (None,)),
        ("warmup_history.json", (None,)),
        ("warmup_limits.json", ("accounts", "cooldowns")),
    ):
        _pop_accounts_from_json(data_dir / filename, wanted, containers)

    sleep_path = data_dir / "sleep_zones.json"
    if sleep_path.exists():
        def mutate_sleep(data):
            if not isinstance(data, dict):
                return data
            records = data["zones"] if isinstance(data.get("zones"), dict) else data
            for key in wanted:
                records.pop(key, None)
            return data
        update_json(sleep_path, {}, mutate_sleep, skip_unchanged=True)

    jobs_path = data_dir / "resumable_jobs.json"
    if jobs_path.exists():
        def mutate_jobs(data):
            jobs = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(jobs, dict):
                return data
            for job in jobs.values():
                accounts = job.get("accounts") if isinstance(job, dict) else None
                if isinstance(accounts, dict):
                    for key in wanted:
                        accounts.pop(key, None)
            return data
        update_json(jobs_path, {"jobs": {}}, mutate_jobs, skip_unchanged=True)

    wanted_list = sorted(wanted)
    for db_name, tables in (
        ("warmup_state.sqlite3", ("attempts", "cooldown", "history", "channel_assignment")),
        ("profile_apply.sqlite3", ("steps", "review_audit")),
        ("profile_recipes.sqlite3", ("apply_claims",)),
    ):
        _cleanup_sqlite_tables(data_dir / db_name, wanted_list, tables)

    passkeys_root = (data_dir / "passkeys").resolve()
    if passkeys_root.is_dir():
        for account_id in wanted:
            target = passkeys_root / f"{account_id}.json"
            if target.is_symlink():
                continue
            resolved = target.resolve()
            if resolved.is_relative_to(passkeys_root) and resolved != passkeys_root and resolved.is_file():
                resolved.unlink(missing_ok=True)

    services = list(_BROWSERS.get(str(data_dir.resolve()), ()))
    for account_id in wanted:
        closing_ctx = any([service.account_removed(account_id) for service in services])
        if not closing_ctx:
            remove_browser_profile(config.data_dir, account_id)
