from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.models import AccountRecord, utc_now_iso
from core.storage import read_json, update_json


class ProfileSnapshotService:
    def __init__(self, config: AppConfig):
        self.config = config

    @property
    def latest_file(self) -> Path:
        return self.config.data_dir / "profile_snapshots" / "latest.json"

    def save_latest(self, accounts: list[AccountRecord], reason: str) -> Path:
        now = utc_now_iso()
        updates = {
            account.id: {
                "account_id": account.id,
                "username": account.username,
                "first_name": account.first_name,
                "last_name": account.last_name,
                "bio": account.metadata.get("last_known_bio"),
                "avatar": account.metadata.get("last_applied_avatar"),
                "saved_at": now,
            }
            for account in accounts
        }

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            by_id: dict[str, dict[str, Any]] = {}
            for entry in data.get("profiles", []) if isinstance(data, dict) else []:
                if isinstance(entry, dict) and entry.get("account_id"):
                    by_id[str(entry["account_id"])] = dict(entry)
            by_id.update(updates)
            return {
                "created_at": now,
                "reason": reason,
                "profiles": list(by_id.values()),
            }

        update_json(self.latest_file, {"profiles": []}, mutate)
        return self.latest_file

    def load_latest_profiles(self, account_ids: set[str] | None = None) -> list[dict[str, Any]]:
        raw = read_json(self.latest_file, {"profiles": []})
        profiles = raw.get("profiles", [])
        if not isinstance(profiles, list):
            return []
        result: list[dict[str, Any]] = []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            account_id = str(profile.get("account_id") or "")
            if not account_id:
                continue
            if account_ids is not None and account_id not in account_ids:
                continue
            result.append(profile)
        return result
