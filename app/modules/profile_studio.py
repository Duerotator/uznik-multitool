"""Local recipe planning and separately confirmed apply through existing receipts."""
from __future__ import annotations

import hashlib
import asyncio
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path

from core.telegram_client import PyrogramAccountClient, TelethonAccountClient
from modules.accounts import AccountService
from modules.profile_apply import ProfileApplyJournal, fingerprint, story_signature

FIELDS = {"name", "bio", "username", "avatars", "music", "birthday", "stories", "channel", "gifts"}
CAPABILITIES = {"pyrogram": PyrogramAccountClient.profile_capabilities,
                "telethon": TelethonAccountClient.profile_capabilities}
MAX_MEDIA = 128*1024*1024
MAX_TOTAL = 512*1024*1024


class StudioConflict(RuntimeError):
    pass


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise StudioConflict("Duplicate recipe/archive fields")
        result[key] = value
    return result


def _small_json(path, *, max_size=1024*1024):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_size:
            raise StudioConflict("Recipe/archive metadata must be a bounded regular file")
        value = stream.read(max_size+1)
    if len(value) > max_size:
        raise StudioConflict("Recipe/archive metadata is too large")
    try:
        return json.loads(value.decode("utf-8-sig"), object_pairs_hook=_pairs)
    except (ValueError, UnicodeError):
        raise StudioConflict("Invalid recipe/archive JSON; source was not repaired or replaced") from None


def validate_recipe(spec):
    if not isinstance(spec, dict) or set(spec) != {"title", "fields", "assignments", "rights_declared", "max_stories", "clear_avatars"}:
        raise StudioConflict("Recipe fields do not match the supported schema")
    if not isinstance(spec["title"], str) or not 1 <= len(spec["title"].strip()) <= 80:
        raise StudioConflict("Recipe title must be 1–80 characters")
    selected = spec["fields"]
    if not isinstance(selected, list) or not selected or any(not isinstance(value, str) or value not in FIELDS for value in selected) or len(set(selected)) != len(selected):
        raise StudioConflict("Choose explicit, unique supported recipe fields")
    assignments = spec["assignments"]
    if (not isinstance(assignments, dict) or not 1 <= len(assignments) <= 25
            or any(not isinstance(key, str) or not 1 <= len(key) <= 160 or not isinstance(value, str)
                   or not re.fullmatch(r"[a-z0-9_]{1,64}", value) for key, value in assignments.items())):
        raise StudioConflict("Provide 1–25 exact account → local archive assignments")
    if len(set(assignments.values())) != len(assignments):
        raise StudioConflict("Archive assignments must remain 1:1; no implicit reuse across accounts")
    for key in ("rights_declared", "clear_avatars"):
        if type(spec[key]) is not bool:
            raise StudioConflict("Rights/clear options must be explicit booleans")
    if spec["clear_avatars"] and "avatars" not in selected:
        raise StudioConflict("Clearing avatars requires explicitly selecting the avatar step")
    limit = spec["max_stories"]
    if limit is not None and (type(limit) is not int or not 0 <= limit <= 100):
        raise StudioConflict("Story limit must be 0–100 or null (all bounded archive entries)")
    return {**spec, "title": spec["title"].strip(), "fields": sorted(selected), "assignments": dict(sorted(assignments.items()))}


def _media(root, filename, budget, *, video=False):
    if not isinstance(filename, str) or not filename or len(filename) > 200:
        raise StudioConflict("Invalid archive media path")
    path = (root/filename).resolve(strict=True)
    extensions = {".mp4"} if video else {".jpg", ".jpeg", ".png", ".webp"}
    if not path.is_relative_to(root) or path.suffix.lower() not in extensions:
        raise StudioConflict("Archive media escapes its profile or has an unsupported extension")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_MEDIA:
            raise StudioConflict("Archive media must be regular, non-empty and at most 128 MiB")
        budget[0] += before.st_size
        if budget[0] > MAX_TOTAL:
            raise StudioConflict("Preview media budget exceeds 512 MiB; narrow the explicit selection")
        digest, size = hashlib.sha256(), 0
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            size += len(chunk)
            if size > before.st_size:
                raise StudioConflict("Media changed during preview")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if size != before.st_size or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise StudioConflict("Media changed during preview")
    return {"file": filename, "sha256": digest.hexdigest(), "bytes": size, "is_video": video}


def source_manifest(data_dir, key, fields, limit, budget):
    if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", key):
        raise StudioConflict("Invalid local archive key")
    archive = (Path(data_dir)/"profiles_archive").resolve()
    root = (archive/key).resolve(strict=True)
    if not root.is_relative_to(archive) or root == archive:
        raise StudioConflict("Archive source is outside its root")
    meta_path = (root/"meta.json").resolve(strict=True)
    if not meta_path.is_relative_to(root):
        raise StudioConflict("Archive metadata is outside its profile")
    meta = _small_json(meta_path)
    if not isinstance(meta, dict) or not isinstance(meta.get("username"), str) or meta["username"].strip().lstrip("@").lower() != key:
        raise StudioConflict("Archive key and source identity differ; review before planning")
    values = {}
    for field, bound in (("first_name", 64), ("last_name", 64), ("bio", 4096)):
        value = meta.get(field, "")
        if not isinstance(value, str) or len(value) > bound:
            raise StudioConflict("Invalid source profile text")
        values[field] = value
    avatars, stories = [], []
    if "avatars" in fields:
        entries = meta.get("avatars") or []
        if not isinstance(entries, list) or len(entries) > 500:
            raise StudioConflict("Invalid source avatar manifest")
        avatars = [_media(root, value, budget) for value in entries[:13]]
    if "stories" in fields and limit != 0:
        entries = meta.get("stories") or []
        if not isinstance(entries, list) or len(entries) > 500:
            raise StudioConflict("Invalid source story manifest")
        for item in entries[:limit] if limit is not None else entries:
            if not isinstance(item, dict) or (item.get("is_video") is not None and type(item["is_video"]) is not bool):
                raise StudioConflict("Invalid source story type")
            stories.append(_media(root, item.get("file"), budget, video=bool(item.get("is_video"))))
    signatures = {}
    if "name" in fields:
        signatures["name"] = fingerprint([values["first_name"] or "User", values["last_name"]])
    if "bio" in fields:
        bio = values["bio"]
        signatures["bio"] = fingerprint(bio[:67]+"..." if len(bio) > 70 else bio)
    if "username" in fields:
        signatures["username"] = fingerprint([meta["username"], values["first_name"], values["last_name"]])
    for item in avatars:
        signatures["avatar:"+item["sha256"]] = fingerprint(item["sha256"])
    for item in stories:
        step, signature = "story:"+item["sha256"], story_signature(item["sha256"], item["is_video"])
        if step in signatures and signatures[step] != signature:
            raise StudioConflict("Duplicate story bytes have conflicting media types")
        signatures[step] = signature
    music = meta.get("music")
    if "music" in fields and music:
        if not isinstance(music, dict) or type(music.get("id")) is not int or music["id"] <= 0:
            raise StudioConflict("Source music has no valid document reference")
        signatures["music"] = fingerprint([music["id"], music.get("access_hash")])
    birthday = meta.get("birthday")
    if "birthday" in fields and birthday:
        if not isinstance(birthday, dict) or set(birthday)-{"day", "month", "year"}:
            raise StudioConflict("Invalid source birthday")
        if any(type(birthday.get(field)) is not int for field in ("day", "month")) or (birthday.get("year") is not None and type(birthday["year"]) is not int):
            raise StudioConflict("Birthday components must be integers, not guesses")
        try:
            date(birthday.get("year") or 2000, birthday["month"], birthday["day"])
        except (TypeError, ValueError, KeyError):
            raise StudioConflict("Invalid source birthday") from None
        signatures["birthday"] = fingerprint(birthday)
    display = {}
    if "name" in fields: display["name"] = (values["first_name"] or "User")+" "+values["last_name"]
    if "bio" in fields: display["bio"] = values["bio"][:67]+"..." if len(values["bio"])>70 else values["bio"]
    if "username" in fields: display["username_seed_not_final"] = meta["username"]
    if "birthday" in fields: display["birthday"] = birthday
    if "avatars" in fields: display["avatar_entries"] = len(avatars)
    if "stories" in fields:
        display.update(story_entries=len(stories),video_entries=sum(item["is_video"] for item in stories),story_visibility="public / pinned")
    return {"key": key, "meta_fingerprint": fingerprint(meta), "signatures": signatures,"display":display,
            "avatars": avatars, "stories": stories, "music_present": bool(music), "birthday_present": bool(birthday)}


class ProfileRecipeStore:
    def __init__(self, data_dir, *, clock=time.time):
        self.path, self.clock = Path(data_dir)/"profile_recipes.sqlite3", clock
        self._schema_ready = False

    def _ensure_schema(self):
        if self._schema_ready:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS recipes(id TEXT PRIMARY KEY,revision TEXT,spec TEXT,updated REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS previews(token TEXT PRIMARY KEY,recipe TEXT,revision TEXT,inputs TEXT,selection TEXT,created REAL,expires REAL,state TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS apply_claims(account TEXT PRIMARY KEY,token TEXT,pid INTEGER)")
        self._schema_ready = True

    @contextmanager
    def db(self, *, readonly: bool = False):
        self._ensure_schema()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            if not readonly:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM previews WHERE expires<? AND state!='applying'", (self.clock()-86400,))
            yield db

    def save(self, spec, *, recipe_id=None, expected_revision=None, confirmed=False):
        spec = validate_recipe(spec)
        for value in (recipe_id, expected_revision):
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value)):
                raise StudioConflict("Invalid recipe/revision identity")
        if confirmed is not True:
            raise StudioConflict("Confirm saving this local recipe")
        with self.db() as db:
            old = db.execute("SELECT revision FROM recipes WHERE id=?", (recipe_id,)).fetchone() if recipe_id else None
            if (recipe_id and (not old or old["revision"] != expected_revision)) or (not recipe_id and expected_revision is not None):
                raise StudioConflict("Recipe changed; reload before editing")
            recipe_id = recipe_id or secrets.token_hex(16)
            revision = secrets.token_hex(16)
            db.execute("INSERT OR REPLACE INTO recipes VALUES(?,?,?,?)", (recipe_id, revision, json.dumps(spec), self.clock()))
        return self.get(recipe_id)

    def get(self, recipe_id):
        if not isinstance(recipe_id, str) or not re.fullmatch(r"[a-f0-9]{32}", recipe_id):
            raise StudioConflict("Choose an explicit recipe ID")
        with self.db(readonly=True) as db:
            row = db.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if not row:
            raise StudioConflict("Recipe no longer exists")
        return {"id": row["id"], "revision": row["revision"], "spec": validate_recipe(json.loads(row["spec"]))}


class ProfileStudioService:
    def __init__(self, config):
        self.config = config
        self.accounts = AccountService(config)
        self.store = ProfileRecipeStore(config.data_dir)

    def context(self, ids, ui_context):
        accounts = self._targets(ids)
        return fingerprint([ui_context, sorted([item.id, item.user_id, item.enabled, item.backend,
            item.session_ref, item.group, sorted(item.groups)] for item in accounts)])

    def report(self, ids, ui_context):
        context = self.context(ids, ui_context)
        with self.store.db(readonly=True) as db:
            rows = db.execute("SELECT id,revision,spec FROM recipes ORDER BY updated DESC LIMIT 501").fetchall()
        if len(rows) > 500:
            raise StudioConflict("Recipe list exceeds 500 entries; local maintenance required")
        recipes = []
        for row in rows:
            spec = validate_recipe(json.loads(row["spec"]))
            if set(spec["assignments"]) == set(ids):
                recipes.append({"id": row["id"], "revision": row["revision"], "spec": spec})
        return {"selection": sorted(ids), "context": context, "recipes": recipes, "execution_available": False}

    def action(self, action, ids, ui_context, payload):
        """Local-only consumer boundary, exact schema and fresh account/UI context."""
        allowed = {"report": set(), "save": {"spec", "recipe_id", "expected_revision", "confirmed", "expected_context"},
            "preview": {"recipe_id", "expected_revision", "expected_context"}, "review": {"token", "confirmed", "expected_context"}}
        if action not in allowed or not isinstance(payload, dict) or set(payload)-allowed[action]:
            raise StudioConflict("Unsupported Studio action/fields; execution is not available")
        if action == "report":
            return {"report": self.report(ids, ui_context)}
        context = self.context(ids, ui_context)
        if payload.get("expected_context") != context:
            raise StudioConflict("Account/group/filter context changed; close and reopen Studio")
        if action == "save":
            saved = self.save(ids, payload.get("spec"), recipe_id=payload.get("recipe_id"),
                expected_revision=payload.get("expected_revision"), confirmed=payload.get("confirmed"))
            return {"saved": saved, "report": self.report(ids, ui_context)}
        if action == "preview":
            if self.store.get(payload.get("recipe_id"))["revision"] != payload.get("expected_revision"):
                raise StudioConflict("Recipe revision changed; reload before preview")
            plan = self.preview(payload.get("recipe_id"), ids)
            if plan["recipe_revision"] != payload.get("expected_revision"):
                raise StudioConflict("Recipe revision changed during preview; reload")
            # Include the UI scope in the reviewed digest, not only the account fields.
            with self.store.db() as db:
                db.execute("UPDATE previews SET inputs=? WHERE token=?", (fingerprint([context, plan["inputs_digest"]]), plan["token"]))
            return {"plan": plan}
        result = self.confirm_review(payload.get("token"), ids, confirmed=payload.get("confirmed"), review_context=context)
        return {"review": result}

    def _targets(self, ids):
        if not isinstance(ids, list) or not 1 <= len(ids) <= 25 or any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
            raise StudioConflict("Choose 1–25 unique explicit accounts")
        wanted = set(ids)
        accounts = [item for item in self.accounts.list_accounts() if item.id in wanted and item.enabled]
        if {item.id for item in accounts} != wanted:
            raise StudioConflict("An account was removed/disabled; refresh selection")
        return accounts

    def save(self, ids, spec, **confirmation):
        self._targets(ids)
        checked = validate_recipe(spec)
        if set(checked["assignments"]) != set(ids):
            raise StudioConflict("Recipe assignments must exactly match the approved selected accounts")
        return self.store.save(checked, **confirmation)

    def _bindings(self):
        path = Path(self.config.data_dir)/"profile_bindings.json"
        if not path.exists():
            return {}
        data = _small_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("bindings"), dict):
            raise StudioConflict("Bindings are unreadable; no data was repaired")
        return data["bindings"]

    def _build(self, recipe, ids, *, capture=None):
        accounts = self._targets(ids)
        spec = recipe["spec"]
        if set(spec["assignments"]) != set(ids):
            raise StudioConflict("Preview selection must exactly match saved assignments")
        bindings = self._bindings()
        budget, rows, inputs = [0], [], []
        owners = {}
        for owner, value in bindings.items():
            if (not isinstance(value, dict) or not isinstance(value.get("profile_key"), str)
                    or not re.fullmatch(r"[a-z0-9_]{1,64}", value["profile_key"])):
                raise StudioConflict("Invalid profile bindings; manual review required")
            key = value["profile_key"]
            if key in owners:
                raise StudioConflict("Conflicting profile owners; manual review required")
            owners[key] = owner
        for account in accounts:
            key = spec["assignments"][account.id]
            binding = bindings.get(account.id)
            blockers = []
            if not spec["rights_declared"]:
                blockers.append("rights_not_declared")
            if type(account.user_id) is not int or account.user_id <= 0:
                blockers.append("account_owner_unverified_needs_check")
            if key in owners and owners[key] != account.id:
                blockers.append("source_owned_by_other_account")
            if binding and binding["profile_key"] != key:
                blockers.append("account_bound_to_other_source")
            source = source_manifest(self.config.data_dir, key, spec["fields"], spec["max_stories"], budget) if not blockers else None
            if capture is not None and source is not None:
                capture[key] = source
            checkpoint = ProfileApplyJournal.inspect_readonly(self.config.data_dir, account.id, key)
            if binding and (account.metadata.get("profile_apply_v2") is not True or not checkpoint["available"] or not checkpoint["receipts"]):
                blockers.append("legacy_or_missing_receipts_needs_review")
            if any(row["status"] in {"submitted", "uncertain"} for row in checkpoint["receipts"]):
                blockers.append("uncertain_receipt_needs_review")
            if any(row["status"] not in {"done", "failed", "ready", "submitted", "uncertain"} for row in checkpoint["receipts"]):
                blockers.append("unrecognized_receipt_needs_review")
            steps = []
            if source:
                signatures = dict(source["signatures"])
                if spec["clear_avatars"] and source["avatars"]:
                    signatures["clear_avatars"] = fingerprint([item["sha256"] for item in source["avatars"]])
                by_key = {(item["step"], item["fingerprint"]): item["status"] for item in checkpoint["receipts"]}
                for field in (value for value in ("name", "bio", "username", "avatars", "music", "birthday", "stories", "channel", "gifts") if value in spec["fields"]):
                    capability = "supported_offline" if field in CAPABILITIES.get(account.backend, set()) else "unsupported"
                    if capability == "unsupported":
                        blockers.append("unsupported:"+field)
                    matches = [name for name in signatures if name == field or (field == "avatars" and name.startswith(("avatar:", "clear_avatars"))) or (field == "stories" and name.startswith("story:"))]
                    if not matches and capability != "unsupported":
                        blockers.append("missing_source:"+field)
                    for name in matches:
                        saved = by_key.get((name, signatures[name]), "not_run")
                        if saved not in {"not_run", "done", "failed", "ready", "submitted", "uncertain"}:
                            blockers.append("unrecognized_receipt_needs_review")
                        steps.append({"step": name, "fingerprint": signatures[name], "checkpoint": saved,
                            "capability": capability, "runtime_preflight": "required" if field in {"music", "stories", "username"} else "not_checked"})
                order = {"clear_avatars": 0, "name": 1, "bio": 2, "username": 3, "avatars": 4, "music": 5, "birthday": 6, "stories": 7}
                steps.sort(key=lambda item: order.get("avatars" if item["step"].startswith("avatar:") else "stories" if item["step"].startswith("story:") else item["step"], 99))
            rows.append({"account_id": account.id, "source": key, "backend": account.backend,
                         "summary":source["display"] if source else {},"clear_avatars":spec["clear_avatars"],
                         "blockers": sorted(set(blockers)), "steps": steps, "status": "blocked" if blockers else "ready_for_local_review"})
            inputs.append([account.id, account.user_id, account.enabled, account.backend, account.session_ref, account.group,
                sorted(account.groups), account.metadata.get("profile_apply_v2"), binding, checkpoint, source])
        return {"recipe_id": recipe["id"], "recipe_revision": recipe["revision"], "accounts": rows, "selection": sorted(ids),
                "inputs_digest": fingerprint([recipe, inputs]), "rights": "operator_declared_not_independently_verified",
                "execution_available": False, "detail": "Local plan only. No Telegram requests, publishing or receipt changes. Runtime capability/username/quota checks remain required."}

    def preview(self, recipe_id, ids):
        recipe = self.store.get(recipe_id)
        result = self._build(recipe, ids)
        token, now = secrets.token_hex(24), self.store.clock()
        with self.store.db() as db:
            db.execute("INSERT INTO previews VALUES(?,?,?,?,?,?,?,?)", (token, recipe_id, recipe["revision"], result["inputs_digest"], json.dumps(sorted(ids)), now, now+600, "previewed"))
        return {**result, "token": token, "expires_in": 600}

    def confirm_review(self, token, ids, *, confirmed=False, review_context=None):
        self._targets(ids)
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{48}", token):
            raise StudioConflict("Choose a current local preview token")
        if confirmed is not True:
            raise StudioConflict("Confirm this local preview review; no Telegram apply is available")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM previews WHERE token=?", (token,)).fetchone()
        now = self.store.clock()
        if not row or row["state"] != "previewed" or not row["created"] <= now < row["expires"] or json.loads(row["selection"]) != sorted(ids):
            raise StudioConflict("Preview changed, expired or scope differs; build it again")
        current = self._build(self.store.get(row["recipe"]), ids)
        digest = fingerprint([review_context, current["inputs_digest"]]) if review_context is not None else current["inputs_digest"]
        if current["recipe_revision"] != row["revision"] or digest != row["inputs"]:
            raise StudioConflict("Recipe/source/account/bindings/receipts changed; rebuild preview")
        if any(account["blockers"] for account in current["accounts"]):
            raise StudioConflict("Blocked steps require review; local confirmation cannot bypass them")
        with self.store.db() as db:
            changed = db.execute("UPDATE previews SET state='reviewed_locally' WHERE token=? AND state='previewed' AND expires>? AND revision=(SELECT revision FROM recipes WHERE id=previews.recipe)", (token, self.store.clock()))
            if changed.rowcount != 1:
                raise StudioConflict("Preview was already reviewed or expired")
        return {"reviewed_locally": True, "execution_available": False,
                "apply_token": token, "apply_expires_at": row["expires"],
                "detail": "Reviewed locally only. Apply requires a separate confirmation before expiry; it changes the selected profile fields and may publish public pinned stories."}

    def _approved(self, token, ids, ui_context):
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{48}", token):
            raise StudioConflict("Choose a reviewed preview token")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM previews WHERE token=?", (token,)).fetchone()
        now = self.store.clock()
        if (not row or row["state"] != "reviewed_locally" or not row["created"] <= now < row["expires"]
                or json.loads(row["selection"]) != sorted(ids)):
            raise StudioConflict("Reviewed preview is expired, consumed or scope differs")
        recipe = self.store.get(row["recipe"])
        manifests = {}
        plan = self._build(recipe, ids, capture=manifests)
        context = self.context(ids, ui_context)
        if (recipe["revision"] != row["revision"] or fingerprint([context,plan["inputs_digest"]]) != row["inputs"]
                or any(account["blockers"] for account in plan["accounts"])):
            raise StudioConflict("Recipe/source/account/bindings/receipts changed; rebuild and review")
        return dict(row), recipe, context, manifests

    def validate_apply_request(self, token, ids, ui_context, *, expected_context=None, confirmed=False):
        if confirmed is not True or self.context(ids,ui_context) != expected_context:
            raise StudioConflict("Confirm Apply for the exact current account context")
        if not isinstance(token,str) or not re.fullmatch(r"[a-f0-9]{48}",token):
            raise StudioConflict("Choose a current reviewed preview")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM previews WHERE token=?",(token,)).fetchone()
        now = self.store.clock()
        if (not row or row["state"] != "reviewed_locally" or not row["created"] <= now < row["expires"]
                or json.loads(row["selection"]) != sorted(ids)):
            raise StudioConflict("Apply approval expired, consumed or belongs to another selection")

    def _snapshot(self, key, spec, budget, expected):
        """Load only selected, previously hashed regular media; never legacy archive.load."""
        from modules.profile_scraper import ProfileData
        manifest = source_manifest(self.config.data_dir,key,spec["fields"],spec["max_stories"],budget)
        if fingerprint(manifest) != fingerprint(expected):
            raise StudioConflict("Source differs from the reviewed manifest")
        root = (Path(self.config.data_dir)/"profiles_archive"/key).resolve(strict=True)
        archive = (Path(self.config.data_dir)/"profiles_archive").resolve()
        meta_path = (root/"meta.json").resolve(strict=True)
        if root==archive or not root.is_relative_to(archive) or not meta_path.is_relative_to(root):
            raise StudioConflict("Snapshot source escaped its archive")
        meta = _small_json(meta_path)
        if fingerprint(meta) != manifest["meta_fingerprint"]:
            raise StudioConflict("Metadata changed during snapshot")
        def read(item):
            path = (root/item["file"]).resolve(strict=True)
            if not path.is_relative_to(root):
                raise StudioConflict("Media escaped archive during snapshot")
            fd = os.open(path,os.O_RDONLY|getattr(os,"O_NONBLOCK",0)|getattr(os,"O_NOFOLLOW",0))
            with os.fdopen(fd,"rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != item["bytes"]:
                    raise StudioConflict("Media changed during snapshot")
                data = stream.read(item["bytes"]+1)
            if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise StudioConflict("Media bytes changed during snapshot")
            return data
        avatars = [read(item) for item in manifest["avatars"]]
        stories = [{"bytes":read(item),"is_video":item["is_video"],"mime_type":"video/mp4" if item["is_video"] else "image/jpeg"} for item in manifest["stories"]]
        return ProfileData(user_id=int(meta.get("user_id") or 0),username=meta["username"],first_name=meta.get("first_name") or "",
            last_name=meta.get("last_name") or "",bio=meta.get("bio") or "",photo_count=len(avatars),stories_count=len(stories),
            is_premium=bool(meta.get("is_premium")),avatars=avatars,stories=stories,
            music_meta=meta.get("music") or {},birthday=meta.get("birthday"))

    async def apply_reviewed(self, token, ids, ui_context, *, confirmed=False, expected_context=None,
                             stop_event=None, progress=None):
        from core.task_history import process_alive
        from core.results import ActionResult
        from core.telegram_client import create_client
        from modules.profile_scraper import ProfileScraperService
        self.validate_apply_request(token,ids,ui_context,confirmed=confirmed,expected_context=expected_context)
        row, recipe, context, manifests = await asyncio.to_thread(self._approved,token,ids,ui_context)
        spec, sources, budget = recipe["spec"], {}, [0]
        for key in spec["assignments"].values():
            sources[key] = await asyncio.to_thread(self._snapshot,key,spec,budget,manifests[key])
        # Snapshot loading must not make an expired/changed approval executable.
        await asyncio.to_thread(self._approved,token,ids,ui_context)
        with self.store.db() as db:
            for account_id in ids:
                claim = db.execute("SELECT * FROM apply_claims WHERE account=?",(account_id,)).fetchone()
                if claim and process_alive(claim["pid"]):
                    raise StudioConflict("Another Studio apply owns this account; no concurrent replay")
                if claim:
                    db.execute("DELETE FROM apply_claims WHERE account=?",(account_id,))
            changed = db.execute("UPDATE previews SET state='applying' WHERE token=? AND state='reviewed_locally' AND expires>? AND revision=(SELECT revision FROM recipes WHERE id=previews.recipe)",(token,self.store.clock()))
            if changed.rowcount != 1:
                raise StudioConflict("Apply approval already consumed, changed or expired")
            for account_id in ids:
                db.execute("INSERT INTO apply_claims VALUES(?,?,?)",(account_id,token,os.getpid()))
        result, state = ActionResult(), "needs_review"
        try:
            service = ProfileScraperService(self.config)
            for account_id in sorted(ids):
                if stop_event is not None and stop_event.is_set():
                    raise asyncio.CancelledError()
                key = spec["assignments"][account_id]
                def guard():
                    if self.context(ids,ui_context) != context or self.store.get(recipe["id"])["revision"] != recipe["revision"]:
                        raise StudioConflict("Approved account or recipe changed during apply")
                    bindings = self._bindings()
                    if any(value.get("profile_key")==key and owner!=account_id for owner,value in bindings.items()) or (account_id in bindings and bindings[account_id].get("profile_key")!=key):
                        raise StudioConflict("Profile ownership changed during apply")
                guard()
                account = self.accounts.get_account(account_id)
                async with create_client(self.config,account) as client:
                    async with asyncio.timeout(60):
                        me = await client.get_me()
                    if not isinstance(me,dict) or type(me.get("user_id")) is not int or me["user_id"] != account.user_id:
                        raise StudioConflict("Connected session owner differs from the approved account; no profile mutation sent")
                    report = await service._apply_source(client,account,sources[key],before_prepare=guard,before_step=guard,stop_event=stop_event,
                        clear_first=spec["clear_avatars"],max_stories=spec["max_stories"],
                        **{"copy_"+field:field in spec["fields"] for field in ("name","bio","username","avatars","music","stories","birthday")})
                if report.ok:
                    result.add_ok(account_id)
                    if progress: progress.mark_ok(account_id)
                else:
                    result.add_error(account_id,"Studio apply incomplete; inspect existing profile receipts before retry")
                    if progress: progress.mark_error(account_id)
                    break  # Never proceed blindly after unsupported/flood/uncertain.
            state = "applied" if result.ok==len(ids) and not result.errors else "needs_review"
            return result
        finally:
            with self.store.db() as db:
                db.execute("UPDATE previews SET state=? WHERE token=? AND state='applying'",(state,token))
                db.execute("DELETE FROM apply_claims WHERE token=?",(token,))
