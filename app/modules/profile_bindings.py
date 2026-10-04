from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.storage import read_json, write_json_atomic

log = logging.getLogger("ps")


def _norm_key(username: str) -> str:
    return (username or "").lower().lstrip("@").strip()


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

    def _save(self, data: dict[str, Any]) -> None:
        write_json_atomic(self.path, data)

    def bind(
        self,
        account_id: str,
        profile_username: str,
        *,
        user_id: int | None = None,
    ) -> None:
        data = self._load()
        bindings = data.setdefault("bindings", {})
        profile_key = _norm_key(profile_username)
        # One archived profile belongs to one account.  Without this cleanup,
        # concurrent apply runs can schedule duplicate story downloads.
        for bound_account_id, binding in list(bindings.items()):
            if bound_account_id != account_id and binding.get("profile_key") == profile_key:
                bindings.pop(bound_account_id, None)
                log.warning("rebound profile %s from %s to %s", profile_key, bound_account_id, account_id)
        bindings[account_id] = {
            "profile_key": profile_key,
            "username": (profile_username or "").strip().lstrip("@"),
            "user_id": user_id or 0,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save(data)

    def unbind(self, account_id: str) -> dict[str, Any] | None:
        data = self._load()
        bindings = data.setdefault("bindings", {})
        removed = bindings.pop(account_id, None)
        if removed is not None:
            self._save(data)
        return removed

    def unbind_profile(self, profile_key: str) -> str | None:
        key = _norm_key(profile_key)
        data = self._load()
        bindings = data.setdefault("bindings", {})
        for account_id, binding in list(bindings.items()):
            if binding.get("profile_key") == key:
                bindings.pop(account_id, None)
                self._save(data)
                return account_id
        return None

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
