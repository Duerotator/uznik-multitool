from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage import read_json, update_json, write_json_atomic

log = logging.getLogger("scrape_cache")


def _is_expired(cached_at: Any, max_age_seconds: float | None) -> bool:
    if max_age_seconds is None or not cached_at:
        return False
    try:
        ts = datetime.fromisoformat(str(cached_at).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() > max_age_seconds
    except (TypeError, ValueError):
        return False


class ScrapeCache:
    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _key(self, target: str) -> str:
        return hashlib.sha256(target.encode()).hexdigest()[:16]

    def _path(self, target: str) -> Path:
        return self.dir / f"{self._key(target)}.json"

    def load_usernames(self, target: str, *, max_age_seconds: float | None = 7 * 86400) -> list[dict[str, Any]] | None:
        p = self._path(target)
        if not p.exists():
            return None
        data = read_json(p, None)
        if not data or not isinstance(data, dict):
            return None
        if _is_expired(data.get("cached_at"), max_age_seconds):
            return None
        users = data.get("users")
        if not isinstance(users, list):
            return None
        return users

    def save_usernames(self, target: str, users: list[dict[str, Any]], source: str) -> None:
        payload = {
            "target": target,
            "source": source,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "count": len(users),
            "users": users,
        }
        write_json_atomic(self._path(target), payload)
        log.info("cache saved %d usernames for %s", len(users), target[:40])


class ProfileFactsCache:
    """Persistent facts from full-profile lookups, separate from downloaded media."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        data = read_json(self.path, {"by_id": {}, "by_username": {}})
        if not isinstance(data, dict):
            return {"by_id": {}, "by_username": {}}
        data.setdefault("by_id", {})
        data.setdefault("by_username", {})
        return data

    @staticmethod
    def _username_key(username: str) -> str:
        return (username or "").strip().lstrip("@").lower()

    def get(self, user_id: int, username: str, *, max_age_seconds: float | None = 7 * 86400) -> dict[str, Any] | None:
        data = self._load()
        if user_id:
            found = data["by_id"].get(str(user_id))
            if isinstance(found, dict) and not _is_expired(found.get("cached_at"), max_age_seconds):
                return dict(found)
        key = self._username_key(username)
        found = data["by_username"].get(key) if key else None
        if isinstance(found, dict) and not _is_expired(found.get("cached_at"), max_age_seconds):
            return dict(found)
        return None

    def save(self, full: dict[str, Any]) -> None:
        user_id = int(full.get("user_id") or 0)
        username = str(full.get("username") or "").strip().lstrip("@")
        if not user_id and not username:
            return
        fact: dict[str, Any] = {
            "user_id": user_id,
            "username": username,
            "first_name": str(full.get("first_name") or ""),
            "last_name": str(full.get("last_name") or ""),
            "bio": str(full.get("bio") or ""),
            "photo_count": int(full.get("photo_count") or 0),
            "stories_count": int(full.get("stories_count") or 0),
            "is_premium": bool(full.get("is_premium")),
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        if isinstance(full.get("birthday"), dict):
            fact["birthday"] = {
                k: int(v) for k, v in full["birthday"].items() if k in {"day", "month", "year"} and v is not None
            }
        raw_music = full.get("music_meta")
        if isinstance(raw_music, dict) and raw_music:
            fact["music_meta"] = {
                k: v for k, v in raw_music.items() if not isinstance(v, (bytes, bytearray))
            }

        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            if not isinstance(data, dict):
                data = {}
            data.setdefault("by_id", {})
            data.setdefault("by_username", {})
            if user_id:
                data["by_id"][str(user_id)] = fact
            if username:
                data["by_username"][self._username_key(username)] = fact
            return data

        update_json(self.path, {"by_id": {}, "by_username": {}}, mutate)
