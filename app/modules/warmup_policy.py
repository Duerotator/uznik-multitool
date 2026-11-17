"""Explicit warmup policy and transactional budgets/history shared by processes."""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.storage import update_json


def normalize_channel(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"(?:(?:https://)?t\.me/|@)?([A-Za-z][A-Za-z0-9_]{3,31})/?", value)
    if not match:
        raise ValueError("Use public @channel or https://t.me/channel, not post/invite links")
    return match[1].lower()


@dataclass(frozen=True)
class WarmupPolicy:
    channels: list[str] = field(default_factory=list)
    channel_topics: dict[str, list[str]] = field(default_factory=dict)
    channels_per_account: int = 5
    hourly_budget: int = 20
    daily_budget: int = 80
    cycles: int = 3
    posts_per_cycle: int = 3
    reactions: bool = False
    save_posts: bool = False
    join_channels: bool = False

    @classmethod
    def load(cls, data_dir: Path) -> "WarmupPolicy":
        path = Path(data_dir) / "warmup_policy.json"
        # Invalid policies must NOT be silently replaced/quarantined as defaults.
        raw = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
        return cls.parse(raw)

    @classmethod
    def parse(cls, raw) -> "WarmupPolicy":
        if not isinstance(raw, dict) or set(raw) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown or invalid Warmup policy fields")
        policy = cls(**raw)
        for key, low, high in (("hourly_budget", 1, 500), ("daily_budget", 1, 2000),
                               ("cycles", 0, 100), ("posts_per_cycle", 1, 20),
                               ("channels_per_account", 1, 50)):
            value = getattr(policy, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Warmup {key} must be an integer between {low} and {high}")
        if policy.daily_budget < policy.hourly_budget:
            raise ValueError("Daily budget cannot be smaller than hourly budget")
        if any(type(getattr(policy, key)) is not bool for key in ("reactions", "save_posts", "join_channels")):
            raise ValueError("Warmup action permissions must be booleans")
        def channels(values):
            if not isinstance(values, list) or len(values) > 10000 or any(not isinstance(c, str) for c in values):
                raise ValueError("Warmup channels must be a list of at most 10000 public channels")
            return list(dict.fromkeys(normalize_channel(c) for c in values))
        if not isinstance(policy.channel_topics, dict) or len(policy.channel_topics) > 100:
            raise ValueError("Warmup channel_topics must contain at most 100 topics")
        topics = {}
        total = len(policy.channels) if isinstance(policy.channels, list) else 0
        for topic, values in policy.channel_topics.items():
            if not isinstance(topic, str) or not re.fullmatch(r"[\w .-]{1,64}", topic.strip()):
                raise ValueError("Invalid Warmup topic name")
            key = topic.strip().casefold()
            if key in topics:
                raise ValueError("Duplicate Warmup topic name")
            topics[key] = channels(values)
            total += len(values)
            if total > 20000:
                raise ValueError("Too many Warmup catalog entries")
        result = cls(**{**asdict(policy), "channels": channels(policy.channels), "channel_topics": topics})
        if len(result.channel_pool) > 10000:
            raise ValueError("Warmup catalog must contain at most 10000 distinct channels")
        return result

    @property
    def channel_pool(self) -> list[str]:
        return list(dict.fromkeys(self.channels + [c for values in self.channel_topics.values() for c in values]))

    def candidates_for(self, persona: str | None) -> list[str]:
        key = str(persona or "").strip().casefold()
        # An explicitly selected empty topic must not broaden to the global pool.
        return self.channel_topics[key] if key in self.channel_topics else self.channel_pool

    def catalog_text(self) -> str:
        return json.dumps({key: getattr(self, key) for key in
                           ("channels", "channel_topics", "channels_per_account")}, ensure_ascii=False, indent=2)

    @classmethod
    def save_catalog(cls, data_dir: Path, text: str) -> "WarmupPolicy":
        if not isinstance(text, str) or len(text) > 500000:
            raise ValueError("Warmup catalog text is too large or invalid")
        if text.lstrip().startswith(("{", "[")):
            changes = json.loads(text)
            if isinstance(changes, list):
                changes = {"channels": changes, "channel_topics": {}}
            if not isinstance(changes, dict) or set(changes) - {"channels", "channel_topics", "channels_per_account"}:
                raise ValueError("Catalog accepts channels, channel_topics and channels_per_account only")
        else:
            changes = {"channels": [part for part in re.split(r"[\s,;]+", text.strip()) if part], "channel_topics": {}}
        cls.load(data_dir)
        raw = update_json(Path(data_dir) / "warmup_policy.json", {},
                          lambda raw: asdict(cls.parse({**raw, **changes})))
        return cls.parse(raw)

    @classmethod
    def save_channels(cls, data_dir: Path, channels: list[str]) -> "WarmupPolicy":
        path = Path(data_dir) / "warmup_policy.json"
        # Validate existing config before merging; never discard a broken policy.
        cls.load(data_dir)
        def mutate(raw):
            return asdict(cls.parse({**raw, "channels": channels}))
        raw = update_json(path, {}, mutate)
        return cls.parse(raw)

    def summary(self) -> str:
        actions = ["read"]
        if self.reactions:
            actions.append("react")
        if self.save_posts:
            actions.append("save to Saved Messages")
        if self.join_channels:
            actions.append("join allowed channels")
        return (f"Pool: {len(self.channel_pool)} channels, {len(self.channel_topics)} topics; "
                f"up to {self.channels_per_account} persistent channels/account; actions: {', '.join(actions)}; "
                f"budgets {self.hourly_budget}/hour, {self.daily_budget}/24h; "
                f"cycles: {self.cycles or 'continuous until Stop'}")


class WarmupState:
    def __init__(self, data_dir: Path, *, clock=time.time):
        self.path = Path(data_dir) / "warmup_state.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS attempts(account TEXT NOT NULL, at REAL NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS attempt_idx ON attempts(account, at)")
            connection.execute("CREATE TABLE IF NOT EXISTS cooldown(account TEXT PRIMARY KEY, until REAL NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS history(account TEXT, action TEXT, target TEXT, at REAL, PRIMARY KEY(account, action, target))")
            connection.execute("CREATE TABLE IF NOT EXISTS channel_assignment(account TEXT, channel TEXT, position INTEGER, visits INTEGER NOT NULL DEFAULT 0, persona TEXT NOT NULL, PRIMARY KEY(account, channel))")
            connection.execute("CREATE INDEX IF NOT EXISTS channel_assignment_channel ON channel_assignment(channel)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _assignment(self, connection, account_id, policy, persona):
        persona = str(persona or "").strip().casefold()
        candidates = policy.candidates_for(persona)
        allowed = set(candidates)
        count = min(policy.channels_per_account, len(candidates))
        existing = connection.execute(
            "SELECT channel, position, visits, persona FROM channel_assignment WHERE account=? ORDER BY position",
            (account_id,),
        ).fetchall()
        keep = [row for row in existing if row[0] in allowed and row[3] == persona][:count]
        kept = {row[0] for row in keep}
        for row in existing:
            if row[0] not in kept:
                connection.execute("DELETE FROM channel_assignment WHERE account=? AND channel=?", (account_id, row[0]))
        if len(keep) < count:
            usage = dict(connection.execute("SELECT channel, COUNT(*) FROM channel_assignment GROUP BY channel"))
            # Stable account-specific tie-break; usage avoids giving every account the same subset.
            available = sorted((c for c in candidates if c not in kept), key=lambda c: (
                usage.get(c, 0), hashlib.sha256(f"{account_id}\0{c}".encode()).digest()))
            position = max((row[1] for row in keep), default=-1) + 1
            for channel in available[:count-len(keep)]:
                connection.execute("INSERT INTO channel_assignment VALUES (?,?,?,?,?)",
                                   (account_id, channel, position, 0, persona))
                keep.append((channel, position, 0, persona))
                position += 1
        return keep

    def channels_for(self, account_id: str, policy: WarmupPolicy, persona: str | None = None) -> list[str]:
        """Persist a stable subset without consuming a rotation turn or request budget."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return [row[0] for row in self._assignment(connection, account_id, policy, persona)]

    def next_channel(self, account_id: str, policy: WarmupPolicy, persona: str | None = None) -> str | None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._assignment(connection, account_id, policy, persona)
            if not rows:
                return None
            channel, _, _, _ = min(rows, key=lambda row: (row[2], row[1]))
            connection.execute("UPDATE channel_assignment SET visits=visits+1 WHERE account=? AND channel=?",
                               (account_id, channel))
            return channel

    def reserve(self, account_id: str, policy: WarmupPolicy) -> bool:
        now = self.clock()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM attempts WHERE at <= ?", (now-86400,))
            deadline = connection.execute("SELECT until FROM cooldown WHERE account=?", (account_id,)).fetchone()
            hourly, daily = connection.execute(
                "SELECT COALESCE(SUM(at > ?),0), COUNT(*) FROM attempts WHERE account=?",
                (now-3600, account_id),
            ).fetchone()
            if (deadline and deadline[0] > now) or hourly >= policy.hourly_budget or daily >= policy.daily_budget:
                return False
            connection.execute("INSERT INTO attempts VALUES (?,?)", (account_id, now))
            return True

    def cooldown(self, account_id: str, seconds: float) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO cooldown VALUES (?,?) ON CONFLICT(account) DO UPDATE SET until=MAX(until, excluded.until)",
                               (account_id, self.clock()+max(1, seconds)))

    def cooling_down(self, account_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT until FROM cooldown WHERE account=?", (account_id,)).fetchone()
            return bool(row and row[0] > self.clock())

    def seen(self, account_id: str, action: str, target: str) -> bool:
        with closing(self._connect()) as connection:
            return connection.execute("SELECT 1 FROM history WHERE account=? AND action=? AND target=?",
                                      (account_id, action, target)).fetchone() is not None

    def completed(self, account_id: str, action: str, target: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT OR REPLACE INTO history VALUES (?,?,?,?)", (account_id, action, target, self.clock()))
            connection.execute("DELETE FROM history WHERE account=? AND rowid NOT IN (SELECT rowid FROM history WHERE account=? ORDER BY at DESC, rowid DESC LIMIT 5000)",
                               (account_id, account_id))
