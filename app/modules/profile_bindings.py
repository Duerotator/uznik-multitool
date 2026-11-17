from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.storage import read_json, update_json

log = logging.getLogger("ps")


def _norm_key(username: str) -> str:
    return (username or "").strip().lstrip("@").lower()


class ProfileBindings:
    """Persistent binding between an applied account and the profile it copied.

    Stored at data/profile_bindings.json:
    {"bindings": {account_id: {"profile_key": str, "username": str, "user_id": int, "applied_at": str}}}
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.path = Path(config.data_dir) / "profile_bindings.json"

    def _load(self) -> dict[str, Any]:
        return read_json(self.path, {"bindings": {}})

    def bind(
        self,
        account_id: str,
        profile_username: str,
        *,
        user_id: int | None = None,
        replace_owner: bool = True,
    ) -> None:
        profile_key = _norm_key(profile_username)
        if not profile_key or not account_id:
            raise ValueError("Profile binding requires account ID and profile key")

        def mutate(data):
            bindings = data.setdefault("bindings", {})
            for bound_id, binding in list(bindings.items()):
                if bound_id != account_id and binding.get("profile_key") == profile_key:
                    if not replace_owner:
                        raise ValueError(f"Profile {profile_key} is already bound to another account")
                    bindings.pop(bound_id)
                    log.warning("rebound profile %s from %s to %s", profile_key, bound_id, account_id)
            bindings[account_id] = {
                "profile_key": profile_key,
                "username": (profile_username or "").strip().lstrip("@"),
                "user_id": user_id or 0,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }
            return data

        update_json(self.path, {"bindings": {}}, mutate)

    def unbind(self, account_id: str) -> dict[str, Any] | None:
        return self.unbind_many([account_id]).get(account_id)

    def unbind_profile(self, profile_key: str) -> str | None:
        key = _norm_key(profile_key)
        if not self.path.exists():
            return None
        removed = None

        def mutate(data):
            nonlocal removed
            bindings = data.setdefault("bindings", {})
            for account_id, binding in list(bindings.items()):
                if binding.get("profile_key") == key:
                    bindings.pop(account_id)
                    removed = account_id
            return data

        update_json(self.path, {"bindings": {}}, mutate, skip_unchanged=True)
        return removed

    def get(self, account_id: str) -> dict[str, Any] | None:
        return self._load().get("bindings", {}).get(account_id)

    def owner_of(self, profile_key: str) -> str | None:
        key = _norm_key(profile_key)
        for account_id, binding in self._load().get("bindings", {}).items():
            if binding.get("profile_key") == key:
                return account_id
        return None

    def profile_keys_in_use(self) -> set[str]:
        return {
            binding.get("profile_key")
            for binding in self._load().get("bindings", {}).values()
            if binding.get("profile_key")
        }

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._load().get("bindings", {}))

    def count(self) -> int:
        return len(self._load().get("bindings", {}))

    def unbind_many(self, account_ids) -> dict[str, dict[str, Any]]:
        removed = {}
        if not self.path.exists():
            return removed
        def mutate(data):
            bindings = data.setdefault("bindings", {})
            for key in account_ids:
                if key in bindings:
                    removed[key] = bindings.pop(key)
            return data
        update_json(self.path, {"bindings": {}}, mutate, skip_unchanged=True)
        return removed
