"""Durable profile steps and story receipts. Never retry an ambiguous publication."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.rate_limit import human_delay


class ProfileNeedsReview(RuntimeError):
    pass


class ProfileRejected(ValueError):
    """A local precondition failed before any profile mutation was sent."""


@dataclass
class ProfileApplyReport:
    steps: dict[str, str] = field(default_factory=dict)
    changes: dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    @property
    def ok(self):
        return all(status in {"done", "skipped"} for status in self.steps.values())

    @property
    def stories_uploaded(self):
        return sum(key.startswith("story:") and value == "done" for key, value in self.steps.items())

    def summary(self):
        return "; ".join(f"{key if not key.startswith(('story:', 'avatar:')) else key[:18]}={value}"
                         for key, value in self.steps.items()) + (f"; {self.detail}" if self.detail else "")


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def media_hash(story) -> str:
    data = story.get("bytes")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("Story media is empty or invalid")
    return hashlib.sha256(bytes(data)).hexdigest()


def story_signature(digest, is_video):
    return fingerprint([digest, bool(is_video), "public", "pinned"])


def definite_rejection(exc):
    """Only a received RPC rejection proves the mutation did not succeed."""
    return isinstance(exc, (ProfileRejected, NotImplementedError)) or any(
        cls.__name__ == "RPCError" for cls in type(exc).__mro__)


class ProfileApplyJournal:
    @staticmethod
    def inspect_readonly(data_dir, account, profile):
        """Planning only: never initializes/repairs a missing journal or writes receipts."""
        path = (Path(data_dir)/"profile_apply.sqlite3").resolve()
        if not path.exists():
            return {"available": False, "receipts": []}
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=3)) as db:
            rows = db.execute("SELECT step,fingerprint,status FROM steps WHERE account=? AND profile=? LIMIT 5001", (account, profile)).fetchall()
        if len(rows) > 5000:
            raise ProfileNeedsReview("Receipt history is too large for a bounded local preview")
        return {"available": True, "receipts": [{"step": row[0], "fingerprint": row[1], "status": row[2]} for row in rows]}

    def __init__(self, data_dir):
        self.path = Path(data_dir) / "profile_apply.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS steps(
                account TEXT, profile TEXT, step TEXT, fingerprint TEXT, status TEXT,
                random_id INTEGER, result TEXT, error_kind TEXT, updated REAL,
                PRIMARY KEY(account, profile, step, fingerprint))""")
            db.execute("""CREATE TABLE IF NOT EXISTS review_audit(
                token TEXT, account TEXT, profile TEXT, step TEXT, before_status TEXT,
                after_status TEXT, story_id INTEGER, created REAL, PRIMARY KEY(token, step))""")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def has_profile(self, account, profile):
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM steps WHERE account=? AND profile=? LIMIT 1",
                              (account, profile)).fetchone() is not None

    def begin(self, account, profile, step, signature):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status, random_id, result FROM steps WHERE account=? AND profile=? AND step=? AND fingerprint=?",
                             (account, profile, step, signature)).fetchone()
            if row and row[0] == "done":
                return False, row[1], json.loads(row[2] or "{}")
            if row and row[0] in {"submitted", "uncertain"}:
                raise ProfileNeedsReview(f"{step}: previous request needs review; automatic retry blocked")
            if row and row[0] not in {"failed", "ready"}:
                raise ProfileNeedsReview(f"{step}: unrecognized journal state")
            random_id = row[1] if row else secrets.randbelow(2**63-1)+1
            db.execute("INSERT OR REPLACE INTO steps VALUES (?,?,?,?,?,?,?,?,?)",
                       (account, profile, step, signature, "submitted", random_id, "{}", "", time.time()))
            return True, random_id, {}

    def finish(self, account, profile, step, signature, result):
        self._update(account, profile, step, signature, "done", result, "")

    def fail(self, account, profile, step, signature, exc):
        self._update(account, profile, step, signature,
                     "failed" if definite_rejection(exc) else "uncertain", {}, type(exc).__name__)

    def _update(self, account, profile, step, signature, status, result, error):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE steps SET status=?, result=?, error_kind=?, updated=? WHERE account=? AND profile=? AND step=? AND fingerprint=? AND status='submitted'",
                       (status, json.dumps(result), error, time.time(), account, profile, step, signature))
            if changed.rowcount != 1:
                raise ProfileNeedsReview("Step state changed while the request was running")

    def review_state(self, account, profile, items):
        with closing(self._connect()) as db:
            return self._review_state(db, account, profile, items)

    def _review_state(self, db, account, profile, items):
        return {item["step"]: db.execute(
            "SELECT status, random_id, result, error_kind, updated FROM steps WHERE account=? AND profile=? AND step=? AND fingerprint=?",
            (account, profile, item["step"], item["signature"])).fetchone() for item in items}

    def confirm_review(self, account, profile, items, choices, expected, token):
        """Only called after fresh ownership/media checks and explicit operator confirmation."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = self._review_state(db, account, profile, items)
            if current != expected:
                raise ProfileNeedsReview("Journal changed; reopen the review")
            used_ids = {}
            for step, result in db.execute("SELECT step, result FROM steps WHERE account=? AND status='done'", (account,)):
                value = json.loads(result or "{}").get("story_id")
                if value:
                    used_ids.setdefault(int(value), set()).add(step)
            for item in items:
                step = item["step"]
                row = current[step]
                story_id = choices[item["hash"]]
                if row and row[0] == "done":
                    if story_id != json.loads(row[2] or "{}").get("story_id"):
                        raise ValueError("A confirmed receipt cannot be overwritten")
                if story_id and any(used_step != step for used_step in used_ids.get(story_id, set())):
                    raise ValueError("Publication ID already belongs to another media receipt")
                status = "done" if story_id else "ready"
                result = {"reviewed": True, **({"story_id": story_id} if story_id else {})}
                random_id = row[1] if row else secrets.randbelow(2**63-1)+1
                db.execute("INSERT OR REPLACE INTO steps VALUES (?,?,?,?,?,?,?,?,?)",
                           (account, profile, step, item["signature"], status, random_id,
                            json.dumps(result), "", time.time()))
                db.execute("INSERT INTO review_audit VALUES (?,?,?,?,?,?,?,?)",
                           (token, account, profile, step, row[0] if row else "legacy", status, story_id, time.time()))

    def receipts(self, account, profile):
        with closing(self._connect()) as db:
            return [{"step": row[0], "fingerprint": row[1], "status": row[2], **json.loads(row[3])}
                    for row in db.execute("SELECT step, fingerprint, status, result FROM steps WHERE account=? AND profile=?",
                                          (account, profile))]


async def apply_profile(client, account, source, *, journal, prepare=None, clear_first=False,
                        copy_name=True, copy_bio=True, copy_username=True, copy_avatars=True,
                        copy_music=False, copy_stories=False, copy_birthday=False, max_stories=5,
                        before_step=None, stop_event=None):
    key = (source.username or str(source.user_id)).strip().lstrip("@").lower()
    report = ProfileApplyReport()
    operations = []

    def add(step, value, factory, *, timeout=60):
        operations.append((step, fingerprint(value), factory, timeout))
        report.steps[step] = "not_run"

    if max_stories is not None and (type(max_stories) is not int or max_stories < 0):
        raise ValueError("max_stories must be a non-negative integer or None")
    capabilities = client.profile_capabilities
    requested = {
        "name": copy_name, "bio": copy_bio, "username": copy_username and bool(source.username),
        "avatars": copy_avatars and bool(source.avatars), "music": copy_music and bool(source.music_meta),
        "birthday": copy_birthday and bool(source.birthday),
        "stories": copy_stories and bool(source.stories) and max_stories != 0,
    }
    unsupported = [name for name, required in requested.items() if required and name not in capabilities]
    if requested["music"] and "music" not in unsupported and not await client.supports_profile_music():
        unsupported.append("music")
    if requested["stories"] and "stories" not in unsupported and not await client.supports_stories():
        unsupported.append("stories")
    if unsupported:
        report.steps = {name: "unsupported" if name in unsupported else "not_run"
                        for name, required in requested.items() if required}
        report.detail = "Backend cannot apply requested fields; no profile changes made"
        return report

    async def set_username(_random_id):
        # Local import avoids a module cycle; generation remains in the scraper.
        from modules.profile_scraper import _generate_username
        username = await _generate_username(client, source.username, source.first_name, source.last_name)
        if not username:
            raise ProfileRejected("No available username found")
        await client.set_username(username)
        return {"applied_username": username}

    avatar_hashes = []
    if requested["avatars"]:
        for image in source.avatars[:13]:
            if not isinstance(image, (bytes, bytearray)) or not image:
                raise ValueError("Avatar media is empty or invalid")
            avatar_hashes.append(hashlib.sha256(image).hexdigest())
    if clear_first and requested["avatars"]:
        add("clear_avatars", avatar_hashes, lambda _: client.clear_profile_photos())
    if copy_name:
        add("name", [source.first_name or "User", source.last_name or ""],
            lambda _: client.update_profile(first_name=source.first_name or "User", last_name=source.last_name or ""))
    if copy_bio:
        bio = source.bio or ""
        bio = bio[:67]+"..." if len(bio) > 70 else bio
        async def set_bio(_random_id):
            await client.update_profile(bio=bio)
            return {"last_known_bio": bio}
        add("bio", bio, set_bio)
    if requested["username"]:
        add("username", [source.username, source.first_name, source.last_name], set_username)
    if requested["avatars"]:
        for image, digest in zip(source.avatars[:13], avatar_hashes):
            step = "avatar:"+digest
            if step in report.steps:
                continue
            async def set_avatar(_random_id, image=image):
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / "avatar.jpg"
                    path.write_bytes(image)
                    await client.set_profile_photos([path])
            add(step, digest, set_avatar)
    if requested["music"]:
        doc = source.music_meta
        if not int(doc.get("id") or 0):
            raise ValueError("Music document has no ID")
        add("music", [doc["id"], doc.get("access_hash")], lambda _: client.set_profile_music(doc))
    if requested["birthday"]:
        birthday = source.birthday
        from datetime import date
        date(birthday.get("year") or 2000, birthday["month"], birthday["day"])
        add("birthday", birthday, lambda _: client.set_birthday(
            birthday["day"], birthday["month"], year=birthday.get("year")))
    if requested["stories"]:
        for story in source.stories[:max_stories] if max_stories is not None else source.stories:
            digest = media_hash(story)
            step = "story:"+digest
            if step in report.steps:
                continue
            async def upload(random_id, story=story):
                story_id = await client.upload_story({**story, "random_id": random_id})
                if type(story_id) is not int or story_id <= 0:
                    raise ProfileNeedsReview("Story response has no publication ID")
                return {"story_id": story_id}
            add(step, [digest, bool(story.get("is_video")), "public", "pinned"], upload,
                timeout=180 if story.get("is_video") else 90)
    # Validate the whole plan before claim/metadata and before the first mutation.
    if not operations:
        report.steps["profile"] = "skipped"
        return report
    if prepare:
        prepare()
    for step, signature, factory, timeout in operations:
        if stop_event is not None and stop_event.is_set():
            raise asyncio.CancelledError()
        if before_step:
            before_step()
        try:
            run, random_id, saved = journal.begin(account.id, key, step, signature)
        except ProfileNeedsReview as exc:
            report.steps[step] = "uncertain"
            report.detail = str(exc)
            break
        if not run:
            report.steps[step] = "done"
            report.changes.update(saved)
            continue
        try:
            saved = await asyncio.wait_for(factory(random_id), timeout=timeout)
        except asyncio.CancelledError as exc:
            journal.fail(account.id, key, step, signature, exc)
            raise
        except Exception as exc:
            journal.fail(account.id, key, step, signature, exc)
            report.steps[step] = "failed" if definite_rejection(exc) else "uncertain"
            report.detail = f"{step}: {type(exc).__name__}: {str(exc)[:180]}"
            break
        saved = saved if isinstance(saved, dict) else {}
        journal.finish(account.id, key, step, signature, saved)
        report.steps[step] = "done"
        report.changes.update(saved)
        await human_delay(3.0 if step.startswith("story:") else 1.0, 5.0)
    return report
