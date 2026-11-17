"""Explicit, single-account reconciliation. No Telegram mutations or guessed matches."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

from PIL import Image

from core.telegram_client import create_client
from modules.accounts import AccountService
from modules.profile_bindings import ProfileBindings
from modules.profile_apply import ProfileApplyJournal, fingerprint, story_signature


def local_manifest(data_dir, profile):
    if not re.fullmatch(r"[a-z0-9_]{1,64}", profile):
        raise ValueError("Invalid archive profile key")
    archive_root = (Path(data_dir)/"profiles_archive").resolve()
    root = (archive_root/profile).resolve()
    if root == archive_root or not root.is_relative_to(archive_root):
        raise ValueError("Profile archive is outside its root")
    meta = (root/"meta.json").resolve(strict=True)
    if not meta.is_relative_to(root):
        raise ValueError("Archive metadata is outside its profile")
    if meta.stat().st_size > 1024*1024:
        raise ValueError("Archive metadata is too large")
    data = json.loads(meta.read_text(encoding="utf-8-sig"))
    entries = data.get("stories")
    if not isinstance(entries, list) or not 0 < len(entries) <= 500:
        raise ValueError("Review requires 1–500 archived story files")
    result = {}
    total = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise ValueError("Invalid archive media entry")
        path = (root/entry["file"]).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Archive media path is outside its profile")
        if not 0 < path.stat().st_size <= 128*1024*1024:
            raise ValueError("Review media must be non-empty and at most 128 MiB")
        total += path.stat().st_size
        if total > 512*1024*1024:
            raise ValueError("Review archive is larger than 512 MiB")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b""):
                digest.update(chunk)
        key = digest.hexdigest()
        video = bool(entry.get("is_video"))
        if key in result and result[key]["is_video"] != video:
            raise ValueError("Conflicting media kinds in archive")
        result.setdefault(key, {"hash": key, "is_video": video, "index": index+1, "path": path,
                                "step": "story:"+key, "signature": story_signature(key, video)})
    return list(result.values())


def jpeg_preview(data):
    if not data or len(data) > 10*1024*1024:
        raise ValueError("Preview is empty or too large")
    with Image.open(BytesIO(data)) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError("Preview dimensions are too large")
        image = image.convert("RGB")
        image.thumbnail((480, 480))
        output = BytesIO()
        image.save(output, format="JPEG", quality=80)
        return "data:image/jpeg;base64,"+base64.b64encode(output.getvalue()).decode("ascii")


def local_preview(item):
    if item["is_video"]:
        executable = shutil.which("ffmpeg")
        if not executable:
            raise ValueError("FFmpeg is required for the local video preview")
        result = subprocess.run(
            [executable, "-v", "error", "-nostdin", "-threads", "1", "-filter_threads", "1",
             "-protocol_whitelist", "file,pipe", "-f", "mov", "-enable_drefs", "0",
             "-i", str(item["path"]), "-an", "-sn", "-dn",
             "-frames:v", "1", "-vf", "scale=480:480:force_original_aspect_ratio=decrease",
             "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=8, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise ValueError("Cannot preview this video; inspect its original file")
        return jpeg_preview(result.stdout)
    return jpeg_preview(item["path"].read_bytes())


def manifest_key(items):
    return fingerprint(sorted((item["hash"], item["is_video"]) for item in items))


def remote_key(result):
    return fingerprint({"stories": sorted(result["stories"], key=lambda row: row["id"]),
                        "complete": result["complete"]})


class ProfileStoryReview:
    def __init__(self, config, *, clock=time.time):
        self.config = config
        self.accounts = AccountService(config)
        self.bindings = ProfileBindings(config)
        self.journal = ProfileApplyJournal(config.data_dir)
        self.clock = clock
        self.sessions = {}
        self.lock = asyncio.Lock()

    def _scope(self, account_id):
        account = self.accounts.get_account(account_id)
        binding = self.bindings.get(account_id)
        if not account.enabled or not binding:
            raise ValueError("Select an enabled account with a bound archived profile")
        identity = fingerprint([account.id, account.backend, account.session_ref, account.user_id, binding])
        return account, binding["profile_key"], identity

    @asynccontextmanager
    async def _client(self, account, owner_id=None):
        connection = create_client(self.config, account)
        connection.flood_wait_raise_after = 0
        async with connection as client:
            transport = getattr(client, "client", None)
            if transport is not None:
                if account.backend == "telethon":
                    transport.flood_sleep_threshold = 0
                else:
                    transport.sleep_threshold = 0
            owner = (await client.get_me()).get("user_id")
            if type(owner) is not int or owner <= 0 or (account.user_id and account.user_id != owner):
                raise ValueError("Session identity does not match the account")
            if owner_id is not None and owner != owner_id:
                raise ValueError("Telegram session owner changed")
            yield client, owner

    async def _session(self, token, account_id):
        session = self.sessions.get(token) if isinstance(token, str) else None
        if not session or session["account_id"] != account_id or session["used"] or self.clock() >= session["expires"]:
            raise ValueError("Review expired or belongs to another account; reopen it")
        account, profile, identity = self._scope(account_id)
        items = await asyncio.to_thread(local_manifest, self.config.data_dir, profile)
        if identity != session["identity"] or manifest_key(items) != manifest_key(session["items"]):
            raise ValueError("Account binding or archive changed; reopen the review")
        return session, account

    async def open(self, account_id):
        async with asyncio.timeout(90):
            account, profile, identity = self._scope(account_id)
            items = await asyncio.to_thread(local_manifest, self.config.data_dir, profile)
            async with self._client(account) as (client, owner):
                remote = await client.list_own_stories_for_review()
            # Do not issue a token for a snapshot that changed while connecting.
            if self._scope(account_id)[2] != identity:
                raise ValueError("Binding changed during review")
            if type(remote.get("complete")) is not bool:
                raise ValueError("Incomplete story listing response")
            ids = [row["id"] for row in remote["stories"]]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate remote publication IDs")
            expired = [key for key, value in self.sessions.items()
                       if value["used"] or value["expires"] <= self.clock()]
            for key in expired:
                self.sessions.pop(key, None)
            if len(self.sessions) >= 20:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_hex(32)
            expected = self.journal.review_state(account_id, profile, items)
            self.sessions[token] = {
                "account_id": account_id, "profile": profile, "identity": identity, "owner": owner,
                "items": items, "remote": remote, "expected": expected, "previewed": set(),
                "expires": self.clock()+600, "used": False,
            }
            local = []
            for item in items:
                row = expected[item["step"]]
                receipt = json.loads(row[2]) if row else {}
                local.append({k: item[k] for k in ("hash", "index", "is_video")})
                local[-1].update(status=row[0] if row else "legacy", story_id=receipt.get("story_id"))
            return {"token": token, "account_id": account_id, "telegram_user_id": str(owner),
                    "profile": profile, "local": local, "remote": remote["stories"],
                    "complete": remote["complete"], "expires_in": 600}

    async def preview(self, account_id, token, side, key):
        async with asyncio.timeout(60):
            session, account = await self._session(token, account_id)
            if side == "local":
                item = next((item for item in session["items"] if item["hash"] == key), None)
                if not item:
                    raise ValueError("Media does not belong to this review")
                uri = await asyncio.to_thread(local_preview, item)
            elif side == "remote":
                if type(key) is not int or key not in {row["id"] for row in session["remote"]["stories"]}:
                    raise ValueError("Publication does not belong to this review")
                async with self._client(account, session["owner"]) as (client, _owner):
                    data = await client.story_review_thumbnail(key)
                uri = await asyncio.to_thread(jpeg_preview, data)
            else:
                raise ValueError("Invalid preview side")
            session["previewed"].add((side, key))
            return {"side": side, "key": key, "uri": uri}

    async def commit(self, account_id, token, choices, confirmed):
        if confirmed is not True:
            raise ValueError("Explicit visual review and stopped-task confirmation is required")
        async with self.lock, asyncio.timeout(90):
            session, account = await self._session(token, account_id)
            items = session["items"]
            if not isinstance(choices, dict) or set(choices) != {item["hash"] for item in items}:
                raise ValueError("Resolve every local story explicitly")
            if any(type(value) is not int or value < 0 for value in choices.values()):
                raise ValueError("Choose a listed publication or explicitly mark not published")
            positive = [value for value in choices.values() if value]
            if len(positive) != len(set(positive)):
                raise ValueError("Two local files cannot claim the same publication")
            remote = {row["id"]: row for row in session["remote"]["stories"]}
            for item in items:
                row = session["expected"][item["step"]]
                if row and row[0] == "submitted" and self.clock()-row[4] < 300:
                    raise ValueError("A recent upload may still be in flight; stop tasks and wait")
                if not row or row[0] != "done":
                    if ("local", item["hash"]) not in session["previewed"]:
                        raise ValueError("Inspect the local media preview before confirming")
                story_id = choices[item["hash"]]
                if not story_id:
                    if not session["remote"]["complete"]:
                        raise ValueError("Listing is partial: absence cannot authorize an upload")
                    continue
                match = remote.get(story_id)
                if not match or match["kind"] not in {"photo", "video"} or (match["kind"] == "video") != item["is_video"]:
                    raise ValueError("Publication is missing or has a different media kind")
                if not match["pinned"] or not match["public"]:
                    raise ValueError("Existing story is not public and pinned; fix it in Telegram, do not republish")
                if (not row or row[0] != "done") and ("remote", story_id) not in session["previewed"]:
                    raise ValueError("Inspect the Telegram preview before confirming the match")
            async with self._client(account, session["owner"]) as (client, _owner):
                fresh = await client.list_own_stories_for_review()
            if remote_key(fresh) != remote_key(session["remote"]):
                raise ValueError("Telegram publications changed; reopen the review")
            await self._session(token, account_id)
            self.journal.confirm_review(account_id, session["profile"], items, choices, session["expected"],
                                        hashlib.sha256(token.encode()).hexdigest())
            session["used"] = True
            matched = len(positive)
            try:
                self.accounts.update_profile_metadata(account_id, {
                    "profile_apply_v2": True, "stories_uploaded": matched,
                    "last_story_review": {"checked_at": self.clock(), "matched": matched, "ready": len(items)-matched},
                })
            except Exception as exc:
                raise RuntimeError("Receipts were saved, but account metadata could not be updated. Reopen the review; do not upload again.") from exc
            return {"matched": matched, "ready": len(items)-matched,
                    "detail": "Local receipts saved. No stories were uploaded, deleted, pinned or edited."}
