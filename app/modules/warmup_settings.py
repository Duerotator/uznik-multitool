"""Local Warmup policy and successful-action history; never contains session keys."""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from core.storage import read_json, update_json


def normalize_channels(values: list[str] | str) -> list[str]:
    if isinstance(values, str):
        values = re.split(r"[\s,;]+", values.strip())
    channels = []
    for value in values:
        value = str(value).strip()
        if not value:
            continue
        if value.startswith(("https://", "http://", "t.me/", "telegram.me/")):
            url = urlsplit(value if "://" in value else "https://" + value)
            if url.hostname not in {"t.me", "telegram.me"} or url.query or url.fragment:
                raise ValueError("Warmup expects a public @channel or https://t.me/channel, not a post or invite link.")
            value = url.path.strip("/")
        value = value.removeprefix("@").lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]{3,31}", value):
            raise ValueError("Warmup expects public channel usernames (4–32 characters), not post/private invite links.")
        if value not in channels:
            channels.append(value)
    return channels


@dataclass
class WarmupOptions:
    channels: list[str] = field(default_factory=list)
    posts_per_cycle: int = 3
    cycles: int = 3  # Zero is explicitly selected continuous mode.
    hourly_budget: int = 20
    daily_budget: int = 80
    reactions: bool = False
    save_posts: bool = False
    join_channels: bool = False

    def validate(self) -> "WarmupOptions":
        self.channels = normalize_channels(self.channels)
        for name, low, high in (("posts_per_cycle", 1, 6), ("cycles", 0, 100),
                                ("hourly_budget", 1, 100), ("daily_budget", 1, 1000)):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError(f"Warmup {name} must be between {low} and {high}.")
        if self.daily_budget < self.hourly_budget:
            raise ValueError("Warmup daily budget must be at least the hourly budget.")
        for name in ("reactions", "save_posts", "join_channels"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"Warmup {name} must be true or false.")
        return self

    @classmethod
    def load(cls, path: Path) -> "WarmupOptions":
        data = read_json(path, {})
        if not isinstance(data, dict):
            raise ValueError("Warmup settings must be a JSON object.")
        return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__}).validate()

    def save(self, path: Path) -> None:
        self.validate()
        update_json(path, {}, lambda _: asdict(self))


class WarmupHistory:
    """Deduplicate the latest 5000 successful post/actions per account across restarts."""
    def __init__(self, path: Path | None = None):
        self.path = path
        self.data = read_json(path, {}) if path else {}
        if not isinstance(self.data, dict):
            raise ValueError("Warmup history must be a JSON object.")
        self._dirty_accounts: set[str] = set()

    def _account(self, account_id: str) -> dict:
        return self.data.setdefault(account_id, {"posts": {}, "joined": [], "blocked": {}})

    def done(self, account_id: str, link: str, action: str) -> bool:
        return action in self._account(account_id)["posts"].get(link, [])

    def record(self, account_id: str, link: str, action: str) -> None:
        posts = self._account(account_id)["posts"]
        actions = posts.setdefault(link, [])
        if action not in actions:
            actions.append(action)
        while len(posts) > 5000:
            del posts[next(iter(posts))]
        self._dirty_accounts.add(account_id)
        self._save()

    def joined(self, account_id: str, channel: str) -> bool:
        return channel in self._account(account_id)["joined"]

    def record_join(self, account_id: str, channel: str) -> None:
        channels = self._account(account_id)["joined"]
        if channel not in channels:
            channels.append(channel)
            self._dirty_accounts.add(account_id)
            self._save()

    def available(self, account_id: str, channel: str) -> bool:
        return self._account(account_id)["blocked"].get(channel, 0) <= time.time()

    def block(self, account_id: str, channel: str, seconds: float = 21600) -> None:
        self._account(account_id)["blocked"][channel] = time.time() + seconds
        self._dirty_accounts.add(account_id)
        self._save()

    def _save(self) -> None:
        if not self.path or not self._dirty_accounts:
            return
        dirty = set(self._dirty_accounts)

        def mutate(disk: dict) -> dict:
            if not isinstance(disk, dict):
                disk = {}
            for account_id in dirty:
                local_acc = self.data.get(account_id)
                if not isinstance(local_acc, dict):
                    continue
                disk_acc = disk.get(account_id)
                if not isinstance(disk_acc, dict):
                    disk_acc = {"posts": {}, "joined": [], "blocked": {}}
                merged_posts = dict(disk_acc.get("posts") or {})
                for link, actions in (local_acc.get("posts") or {}).items():
                    merged_posts[link] = list(dict.fromkeys([*merged_posts.get(link, []), *actions]))
                while len(merged_posts) > 5000:
                    del merged_posts[next(iter(merged_posts))]
                merged_joined = list(dict.fromkeys([*(disk_acc.get("joined") or []), *(local_acc.get("joined") or [])]))
                merged_blocked = dict(disk_acc.get("blocked") or {})
                for ch, until in (local_acc.get("blocked") or {}).items():
                    prev = float(merged_blocked.get(ch, 0) or 0)
                    merged_blocked[ch] = max(prev, float(until or 0))
                disk[account_id] = {
                    "posts": merged_posts,
                    "joined": merged_joined,
                    "blocked": merged_blocked,
                }
            return disk

        merged = update_json(self.path, {}, mutate)
        for account_id in dirty:
            if account_id in merged:
                self.data[account_id] = merged[account_id]
        self._dirty_accounts.clear()
