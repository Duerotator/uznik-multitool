from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.models import AccountRecord, utc_now_iso
from core.storage import read_json, write_json_atomic


class ProfileSnapshotService:
    def __init__(self, config: AppConfig):
        self.config = config

    @property
    def latest_file(self) -> Path:
        return self.config.data_dir / "profile_snapshots" / "latest.json"

    def save_latest(self, accounts: list[AccountRecord], reason: str) -> Path:
        payload = {
            "created_at": utc_now_iso(),
            "reason": reason,
            "profiles": [
                {
                    "account_id": account.id,
                    "username": account.username,
                    "first_name": account.first_name,
                    "last_name": account.last_name,
                    "bio": account.metadata.get("last_known_bio"),
                    "avatar": account.metadata.get("last_applied_avatar"),
                }
                for account in accounts
            ],
        }
        write_json_atomic(self.latest_file, payload)
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
