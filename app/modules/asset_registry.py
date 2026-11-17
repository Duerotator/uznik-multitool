"""Public address declarations and provider-reported native TON observations.

No Telegram connection, key material, wallet creation, activation or transfers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from core.results import ActionResult
from core.ton_address import normalize_address, friendly_address
from modules.accounts import AccountService
from modules.ton_readonly import TonCenterReader, ReadError, validate_read

NETWORKS = {"mainnet", "testnet"}
SOURCES = {"manual", "telegram_view", "provider_export"}


class AssetConflict(RuntimeError):
    pass


def _network(value):
    if not isinstance(value, str) or value not in NETWORKS:
        raise AssetConflict("Choose mainnet or testnet explicitly")


def account_context(account):
    # Public account identity/scope only; no session file content is opened.
    return hashlib.sha256(json.dumps([account.id, account.user_id, account.group,
        sorted(account.groups), account.enabled, account.backend, account.session_ref], ensure_ascii=False).encode()).hexdigest()


class AssetStore:
    def __init__(self, data_dir, *, clock=time.time):
        self.path, self.clock = Path(data_dir)/"assets_readonly.sqlite3", clock

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA foreign_keys=ON")
            mode = db.execute("PRAGMA journal_mode").fetchone()
            if not mode or str(mode[0]).lower() != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS bindings(account TEXT,network TEXT,address TEXT,source TEXT,revision TEXT,created REAL,updated REAL,PRIMARY KEY(account,network),UNIQUE(network,address))")
                db.execute("CREATE TABLE IF NOT EXISTS observations(account TEXT,network TEXT,revision TEXT,payload TEXT,checked REAL,error TEXT,error_at REAL,due REAL DEFAULT 0,PRIMARY KEY(account,network))")
                db.execute("CREATE TABLE IF NOT EXISTS leases(account TEXT,network TEXT,revision TEXT,token TEXT,expires REAL,PRIMARY KEY(account,network))")
                db.execute("CREATE TABLE IF NOT EXISTS binding_history(account TEXT,network TEXT,old_address TEXT,new_address TEXT,changed REAL)")
                db.execute("BEGIN IMMEDIATE")
                yield db

    def register(self, account_id, network, address, source, *, expected_revision=None, confirmed=False):
        _network(network)
        if confirmed is not True or not isinstance(source, str) or source not in SOURCES:
            raise AssetConflict("Confirm a public address declaration and its source")
        if not isinstance(account_id, str) or not 1 <= len(account_id) <= 160:
            raise AssetConflict("Choose one explicit account")
        canonical = normalize_address(address, network).raw
        now, revision = self.clock(), uuid.uuid4().hex
        with self.db() as db:
            old = db.execute("SELECT * FROM bindings WHERE account=? AND network=?", (account_id, network)).fetchone()
            if (old and old["revision"] != expected_revision) or (not old and expected_revision is not None):
                raise AssetConflict("Address binding changed; reload before replacing it")
            duplicate = db.execute("SELECT account FROM bindings WHERE network=? AND address=? AND account<>?", (network, canonical, account_id)).fetchone()
            if duplicate:
                raise AssetConflict("This public address is already declared for another account on this network")
            if old and old["address"] == canonical and old["source"] == source:
                return old["revision"]
            db.execute("INSERT OR REPLACE INTO bindings VALUES(?,?,?,?,?,?,?)",
                       (account_id, network, canonical, source, revision, old["created"] if old else now, now))
            db.execute("INSERT INTO binding_history VALUES(?,?,?,?,?)", (account_id, network, old["address"] if old else None, canonical, now))
            db.execute("DELETE FROM leases WHERE account=? AND network=?", (account_id, network))
        return revision

    def report(self, account_id, network):
        _network(network)
        with self.db() as db:
            binding = db.execute("SELECT * FROM bindings WHERE account=? AND network=?", (account_id, network)).fetchone()
            observation = db.execute("SELECT * FROM observations WHERE account=? AND network=?", (account_id, network)).fetchone()
        result = {"account_id": account_id, "network": network, "availability": "unknown", "account_link": "unknown",
                  "contract_type": "unknown", "signer": "unknown", "status": "unknown", "chain_state": "unknown",
                  "balance_nano": None, "checked_at": None, "error": None, "observation": None, "binding": None}
        if not binding:
            return result
        result.update(account_link="declared", binding={"address": binding["address"], "source": binding["source"],
            "revision": binding["revision"], "friendly": friendly_address(binding["address"], network), "updated_at": binding["updated"]})
        if not observation or observation["revision"] != binding["revision"]:
            return result
        now = self.clock()
        value = json.loads(observation["payload"]) if observation["payload"] else None
        stale = bool(observation["error"]) or value is None or not 0 <= now-observation["checked"] <= 300
        if value and not -30 <= now-value["source_sync_time"] <= 300:
            stale = True
        return {**result, "status": "stale" if stale else "observed", "observation": value,
                "chain_state": value["chain_state"] if value else "unknown", "balance_nano": value["balance_nano"] if value else None,
                "checked_at": observation["checked"], "error": observation["error"], "error_at": observation["error_at"],
                "next_check_at": observation["due"]}

    def claim(self, account_id, network, revision):
        _network(network)
        now, token = self.clock(), uuid.uuid4().hex
        with self.db() as db:
            binding = db.execute("SELECT revision FROM bindings WHERE account=? AND network=?", (account_id, network)).fetchone()
            if not binding or binding["revision"] != revision:
                raise AssetConflict("Public address binding changed")
            observation = db.execute("SELECT revision,due FROM observations WHERE account=? AND network=?", (account_id, network)).fetchone()
            lease = db.execute("SELECT expires FROM leases WHERE account=? AND network=?", (account_id, network)).fetchone()
            if ((observation and observation["revision"] == revision and observation["due"] > now)
                    or (lease and lease["expires"] > now)):
                return None
            db.execute("INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?)", (account_id, network, revision, token, now+45))
        return token

    def _current(self, db, account_id, network, revision, token):
        binding = db.execute("SELECT * FROM bindings WHERE account=? AND network=?", (account_id, network)).fetchone()
        lease = db.execute("SELECT * FROM leases WHERE account=? AND network=?", (account_id, network)).fetchone()
        if (not binding or binding["revision"] != revision or not lease or lease["token"] != token
                or lease["revision"] != revision or lease["expires"] <= self.clock()):
            raise AssetConflict("Binding or balance worker changed; response was not saved")
        return binding

    def save(self, account_id, network, revision, token, observation):
        now = self.clock()
        with self.db() as db:
            binding = self._current(db, account_id, network, revision, token)
            checked = validate_read(observation, binding["address"], network, now=now)
            db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,NULL,NULL,?)",
                       (account_id, network, revision, json.dumps(checked), now, now+60))
        return self.report(account_id, network)

    def fail(self, account_id, network, revision, token, kind, *, cooldown=60):
        if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", kind):
            kind = "ReadError"
        now = self.clock()
        with self.db() as db:
            self._current(db, account_id, network, revision, token)
            previous = db.execute("SELECT * FROM observations WHERE account=? AND network=?", (account_id, network)).fetchone()
            keep = previous and previous["revision"] == revision
            db.execute("INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?,?,?,?)", (account_id, network, revision,
                previous["payload"] if keep else None, previous["checked"] if keep else None, kind, now, now+max(60, cooldown)))

    def release(self, account_id, network, token):
        with self.db() as db:
            db.execute("DELETE FROM leases WHERE account=? AND network=? AND token=?", (account_id, network, token))


class AssetReadResult(ActionResult):
    pass


class AssetRegistryService:
    def __init__(self, config, *, reader=None):
        self.accounts = AccountService(config)
        self.store = AssetStore(config.data_dir)
        self.reader = reader if reader is not None else TonCenterReader()

    def targets(self, ids):
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) or not item for item in ids):
            raise AssetConflict("Select explicit account IDs")
        wanted = set(ids)
        result = [item for item in self.accounts.list_accounts() if item.id in wanted]
        if {item.id for item in result} != wanted:
            raise AssetConflict("Selected account was removed; refresh selection")
        return result

    def register(self, account_id, network, address, source, *, expected_revision=None, confirmed=False, expected_context=None):
        account = self.targets([account_id])[0]
        if expected_context is not None and expected_context != account_context(account):
            raise AssetConflict("Account identity/group changed; reload before declaring an address")
        self.store.register(account_id, network, address, source, expected_revision=expected_revision, confirmed=confirmed)
        return self.reports([account_id], network)[0]

    def reports(self, ids, network):
        return [{**self.store.report(item.id, network), "account_context": account_context(item)} for item in self.targets(ids)]

    async def refresh(self, ids, network, *, stop_event=None, progress=None, expected_revisions=None, expected_contexts=None):
        _network(network)
        accounts = self.targets(ids)
        for mapping in (expected_revisions, expected_contexts):
            if mapping is not None and (not isinstance(mapping, dict) or set(mapping) != {item.id for item in accounts}
                    or any(not isinstance(value, str) or not value for value in mapping.values())):
                raise AssetConflict("Each approved account needs an explicit binding revision and context")
        stop = stop_event if stop_event is not None else asyncio.Event()
        result = AssetReadResult()
        if progress:
            progress.set_total(len(accounts))
        for index, account in enumerate(accounts):
            if stop.is_set():
                break
            token, revision = None, None
            try:
                current = self.targets([account.id])[0]
                context = expected_contexts[account.id] if expected_contexts is not None else account_context(account)
                if account_context(current) != context:
                    raise AssetConflict("Approved account identity/group changed before the read")
                report = self.store.report(account.id, network)
                binding = report["binding"]
                if not binding:
                    raise AssetConflict("No public address declared for this account/network")
                revision = binding["revision"]
                if expected_revisions is not None and revision != expected_revisions[account.id]:
                    raise AssetConflict("Approved public address changed before the read")
                token = self.store.claim(account.id, network, revision)
                if token:
                    async with asyncio.timeout(30):
                        observation = await self.reader.read(binding["address"], network)
                    if account_context(self.targets([account.id])[0]) != context:
                        raise AssetConflict("Approved account identity/group changed during the read")
                    self.store.save(account.id, network, revision, token, observation)
                    result.add_ok(account.id)
                    result.details[account.id] = "Native TON balance observed from public provider; Telegram link, Money availability and signer remain unverified."
                else:
                    result.details[account.id] = "Skipped: persistent cooldown or another reader; balance timestamp not refreshed."
                if progress:
                    progress.mark_ok(account.id)
            except asyncio.CancelledError:
                if token:
                    try:
                        self.store.fail(account.id, network, revision, token, "CancelledError")
                    except AssetConflict:
                        pass
                raise
            except Exception as exc:
                if token:
                    retry = exc.retry_after if isinstance(exc, ReadError) else 60
                    try:
                        self.store.fail(account.id, network, revision, token, type(exc).__name__, cooldown=retry)
                    except AssetConflict:
                        pass
                result.add_error(account.id, f"Public balance read stopped ({type(exc).__name__}); no wallet state is inferred.")
                if progress:
                    progress.mark_error(account.id)
            finally:
                if token:
                    self.store.release(account.id, network, token)
            if index+1 < len(accounts):
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.2)
                    break
                except asyncio.TimeoutError:
                    pass
        return result
