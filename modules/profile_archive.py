from __future__ import annotations

import json
import logging
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json_atomic

log = logging.getLogger("ps")


class ProfileArchive:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "profiles_archive"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"

    def _load_index(self) -> dict[str, Any]:
        return read_json(self.index_path, {"profiles": {}})

    def save(self, username: str, profile_data: dict[str, Any]) -> None:
        index = self._load_index()
        profiles = index.setdefault("profiles", {})
        key = username.lower().lstrip("@")
        profile_dir = self.dir / key
        meta_path = profile_dir / "meta.json"
        previous = read_json(meta_path, {}) if meta_path.exists() else {}
        previous_avatars = previous.get("avatars", []) or []
        previous_stories = previous.get("stories", []) or []
        incoming_avatars = profile_data.get("avatars") or []
        incoming_stories = profile_data.get("stories") or []
        # A regular scrape often has no story download permission. Keep media
        # previously fetched with a premium session instead of erasing it.
        keep_avatars = not incoming_avatars and bool(previous_avatars)
        keep_stories = not incoming_stories and bool(previous_stories)
        profiles[key] = {
            "username": username,
            "user_id": int(profile_data.get("user_id") or previous.get("user_id") or 0),
            "first_name": profile_data.get("first_name", ""),
            "last_name": profile_data.get("last_name", ""),
            "bio": profile_data.get("bio", ""),
            "photo_count": profile_data.get("photo_count", 0),
            "stories_count": len(incoming_stories) if incoming_stories else len(previous_stories),
            "is_premium": bool(profile_data.get("is_premium")),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "has_avatars": bool(incoming_avatars or previous_avatars),
            "has_stories": bool(incoming_stories or previous_stories),
            "has_music": bool(profile_data.get("music_meta") or previous.get("music")),
            "has_birthday": bool(profile_data.get("birthday") or previous.get("birthday")),
        }
        profile_dir.mkdir(exist_ok=True)
        data = {
            "avatars": list(previous_avatars) if keep_avatars else [],
            "stories": list(previous_stories) if keep_stories else [],
            "music": profile_data.get("music_meta") or previous.get("music"),
            "birthday": profile_data.get("birthday") or previous.get("birthday"),
            "first_name": profile_data.get("first_name", ""),
            "last_name": profile_data.get("last_name", ""),
            "bio": profile_data.get("bio", ""),
            "username": username,
            "user_id": int(profile_data.get("user_id") or previous.get("user_id") or 0),
            "is_premium": bool(profile_data.get("is_premium")),
        }
        if incoming_avatars:
            for i, av in enumerate(incoming_avatars):
                fname = f"avatar_{i}.jpg"
                (profile_dir / fname).write_bytes(av)
                data["avatars"].append(fname)
        if incoming_stories:
            for i, st in enumerate(incoming_stories):
                if isinstance(st, dict) and st.get("bytes"):
                    ext = ".mp4" if st.get("is_video") else ".jpg"
                    fname = f"story_{i}{ext}"
                    (profile_dir / fname).write_bytes(st["bytes"])
                    data["stories"].append({"file": fname, "is_video": st.get("is_video")})
        write_json_atomic(profile_dir / "meta.json", data)
        write_json_atomic(self.index_path, index)
        log.info("archived profile %s", username)

    def load(self, username: str) -> dict[str, Any] | None:
        key = username.lower().lstrip("@")
        profile_dir = self.dir / key
        meta_path = profile_dir / "meta.json"
        if not meta_path.exists():
            return None
        data = read_json(meta_path, None)
        if not data:
            return None
        result: dict[str, Any] = {
            "user_id": int(data.get("user_id") or 0),
            "first_name": data.get("first_name", ""),
            "last_name": data.get("last_name", ""),
            "bio": data.get("bio", ""),
            "username": data.get("username", username),
            "is_premium": data.get("is_premium", False),
            "music_meta": data.get("music"),
            "birthday": data.get("birthday"),
            "avatars": [],
            "stories": [],
        }
        for fname in data.get("avatars", []):
            p = profile_dir / fname
            if p.exists():
                result["avatars"].append(p.read_bytes())
        for st_info in data.get("stories", []):
            p = profile_dir / st_info["file"]
            if p.exists():
                result["stories"].append({
                    "bytes": p.read_bytes(),
                    "is_video": st_info.get("is_video", False),
                    "mime_type": "video/mp4" if st_info.get("is_video") else "image/jpeg",
                })
        return result

    def has_stories(self, username: str) -> bool:
        key = username.lower().lstrip("@")
        data = read_json(self.dir / key / "meta.json", None)
        return bool(data and data.get("stories"))

    def save_stories(self, username: str, stories: list[dict[str, Any]]) -> int:
        key = username.lower().lstrip("@")
        profile_dir = self.dir / key
        meta_path = profile_dir / "meta.json"
        if not meta_path.exists():
            return 0
        data = read_json(meta_path, None)
        if not data:
            return 0
        existing = data.get("stories", []) or []
        known_hashes: set[str] = set()
        for info in existing:
            if not isinstance(info, dict):
                continue
            path = profile_dir / str(info.get("file") or "")
            if path.is_file():
                known_hashes.add(sha256(path.read_bytes()).hexdigest())
        idx = len(existing)
        saved = 0
        for st in stories:
            if not isinstance(st, dict) or not st.get("bytes"):
                continue
            payload = bytes(st["bytes"])
            digest = sha256(payload).hexdigest()
            if digest in known_hashes:
                log.info("skipping duplicate story for %s", username)
                continue
            ext = ".mp4" if st.get("is_video") else ".jpg"
            fname = f"story_{idx}{ext}"
            (profile_dir / fname).write_bytes(payload)
            existing.append({"file": fname, "is_video": bool(st.get("is_video"))})
            known_hashes.add(digest)
            idx += 1
            saved += 1
        if saved:
            data["stories"] = existing
            write_json_atomic(meta_path, data)
            index = self._load_index()
            profiles = index.setdefault("profiles", {})
            if key in profiles:
                profiles[key]["has_stories"] = True
                profiles[key]["stories_count"] = len(existing)
                write_json_atomic(self.index_path, index)
            log.info("saved %d stories for %s", saved, username)
        return saved

    def save_birthday(self, username: str, birthday: dict[str, int]) -> bool:
        """Persist a freshly read birthday without touching archived media."""
        key = username.lower().lstrip("@")
        meta_path = self.dir / key / "meta.json"
        data = read_json(meta_path, None)
        if not data or not birthday.get("day") or not birthday.get("month"):
            return False
        data["birthday"] = {
            "day": int(birthday["day"]),
            "month": int(birthday["month"]),
            "year": int(birthday["year"]) if birthday.get("year") else None,
        }
        write_json_atomic(meta_path, data)
        index = self._load_index()
        profile = index.setdefault("profiles", {}).get(key)
        if isinstance(profile, dict):
            profile["has_birthday"] = True
            write_json_atomic(self.index_path, index)
        return True

    def list_profiles(self) -> list[dict[str, Any]]:
        index = self._load_index()
        profiles = index.get("profiles", {})
        return list(profiles.values())

    def find_key(self, username: str = "", user_id: int = 0) -> str | None:
        """Return the existing archive key for a username or stable Telegram ID."""
        key = username.lower().lstrip("@")
        if key and self.has(key):
            return key
        if not user_id:
            return None
        for archived_key, profile in self._load_index().get("profiles", {}).items():
            if int(profile.get("user_id") or 0) == int(user_id):
                return str(archived_key)
        return None

    def has(self, username: str) -> bool:
        key = username.lower().lstrip("@")
        return (self.dir / key / "meta.json").exists()

    def delete(self, username: str) -> bool:
        import shutil
        key = username.lower().lstrip("@")
        profile_dir = self.dir / key
        if profile_dir.exists():
            shutil.rmtree(profile_dir)
            index = self._load_index()
            index.get("profiles", {}).pop(key, None)
            write_json_atomic(self.index_path, index)
            return True
        return False
