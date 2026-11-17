"""Rental inventory + verified own-profile visibility. No Fragment signer/transfer."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import secrets
import time

from core.telegram_client import AccountBusyError, create_client
from modules.accounts import AccountService
from modules.marketapp_catalog import CatalogError, MarketappCatalogReader
from modules.marketapp_credentials import MarketappCredentials
from utils.telegram_errors import flood_wait_seconds, is_frozen_error, is_invalid_auth_error, short_error


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class MarketappRentalService:
    def __init__(self, config, *, reader=None, accounts=None, client_factory=None, token_source=None, clock=time.time):
        self.config = config
        self.reader = reader if reader is not None else MarketappCatalogReader(proxy=getattr(config, "global_proxy", None))
        self.accounts = accounts if accounts is not None else AccountService(config)
        self.factory = client_factory or create_client
        self.token_source = token_source or MarketappCredentials(config.data_dir).get
        self.clock = clock
        self.lock = asyncio.Lock()
        self.inventory = None
        self.snapshots = {}
        self.observations = {}

    def account(self, account_id, ui_context):
        account = self.accounts.get_account(account_id)
        if not account.enabled or type(account.user_id) is not int or account.user_id <= 0:
            raise CatalogError("Select an enabled, identity-verified account; run the normal session check first")
        context = fingerprint([ui_context, account.id, account.user_id, account.backend, account.session_kind,
                               account.session_ref, account.group, account.groups, account.enabled])
        return account, context

    @asynccontextmanager
    async def client(self, account):
        connection = self.factory(self.config, account)
        connection.flood_wait_raise_after = 0
        if hasattr(connection, "lock_wait_timeout") and connection.lock_wait_timeout is None:
            connection.lock_wait_timeout = 10.0
        try:
            async with connection as client:
                transport = getattr(client, "client", None)
                if transport is not None:
                    if account.backend == "telethon":
                        transport.flood_sleep_threshold = 0
                    else:
                        transport.sleep_threshold = 0
                me = await client.get_me()
                if type(me.get("user_id")) is not int or me["user_id"] != account.user_id:
                    raise CatalogError("Telegram session owner does not match the selected account")
                yield client
        except CatalogError:
            raise
        except AccountBusyError as exc:
            raise CatalogError(str(exc)) from None
        except Exception as exc:
            fw = flood_wait_seconds(exc)
            if fw is not None:
                raise CatalogError(f"Telegram FloodWait {fw}s; retry later") from None
            if is_frozen_error(exc):
                raise CatalogError("Selected Telegram account is frozen") from None
            if is_invalid_auth_error(exc):
                raise CatalogError("Selected Telegram session authorization is invalid") from None
            raise CatalogError(f"Telegram session check failed: {short_error(exc)}") from None

    def decorate(self, scope_ids, ui_context):
        inventory = self.inventory
        rows = []
        for rental in inventory["page"]["items"]:
            hosts = []
            for account_id, observed in self.observations.items():
                if account_id not in scope_ids or observed["ui_context"] != ui_context:
                    continue
                try:
                    account, context = self.account(account_id, ui_context)
                except (CatalogError, KeyError):
                    continue
                row = observed["gifts"].get(rental["nft_address"])
                if row and context == observed["context"]:
                    hosts.append({"account_id": account_id, "visible": row["visible"],
                                  "hosting_verified": row["host_user_id"] == account.user_id,
                                  "checked_at": observed["checked_at"], "fresh": self.clock()-observed["checked_at"] < 300})
            rows.append({**rental, "expired": rental["end_time"] <= self.clock(), "observed_accounts": hosts})
        return {**inventory["page"], "items": rows, "checked_at": inventory["checked_at"],
                "hosting_scope": "Only explicitly scanned accounts in current group/filter; absence is not globally unlinked",
                "fragment_assignment": "manual_required", "fragment_detach": "manual_required"}

    def current_inventory(self):
        if (not self.inventory or self.clock()-self.inventory["checked_at"] >= 300
                or self.inventory["token_tag"] != fingerprint(self.token_source())):
            raise CatalogError("Rental page expired/token changed; refresh My rentals and scan the account again")
        return self.inventory

    @staticmethod
    def _rental_unchanged(cached, fresh):
        keys = ("nft_address", "duration", "end_time", "price_per_day_nanograms", "is_sellable_during_rent")
        return all(cached.get(k) == fresh.get(k) for k in keys)

    async def execute(self, action, payload, *, scope_ids, ui_context, scope_check=None):
        def guard_scope():
            if scope_check is not None and not scope_check():
                raise CatalogError("Group/filter scope changed; reopen and scan again")
        allowed = {"rentals": {"confirmed", "limit", "cursor"},
                   "scan": {"confirmed", "account_id"},
                   "visibility": {"confirmed", "account_id", "review_token", "nft_address", "visible"}}
        if action not in allowed or not isinstance(payload, dict) or set(payload)-allowed[action] or payload.get("confirmed") is not True:
            raise CatalogError("Confirm an exact rental action; unsupported fields are forbidden")
        if self.lock.locked():
            raise CatalogError("Another rental operation is running")
        async with self.lock:
            async with asyncio.timeout(120):
                guard_scope()
                if action == "rentals":
                    token = self.token_source()
                    page = await self.reader.read_rented(token=token, **payload)
                    self.inventory = {"page": page, "params": dict(payload), "checked_at": self.clock(),
                                      "token_tag": fingerprint(token), "revision": secrets.token_hex(16)}
                    self.snapshots.clear()
                    return {"catalog": self.decorate(scope_ids, ui_context)}
                account_id = payload.get("account_id")
                if not isinstance(account_id, str) or account_id not in scope_ids:
                    raise CatalogError("Select exactly one account within the current group/filter")
                account, context = self.account(account_id, ui_context)
                inventory = self.current_inventory()
                if action == "scan":
                    # Invalidate old evidence before any I/O; a failed scan is not fresh.
                    self.observations.pop(account_id, None)
                    self.snapshots = {key: row for key, row in self.snapshots.items()
                                      if row["expires"] > self.clock() and row["account_id"] != account_id}
                    async with self.client(account) as client:
                        remote = await client.list_own_hosted_gifts()
                    if self.account(account_id, ui_context)[1] != context:
                        raise CatalogError("Selected account changed during scan")
                    guard_scope()
                    self.current_inventory()
                    gifts = {row["nft_address"]: row for row in remote["gifts"]}
                    self.observations[account_id] = {"gifts": gifts, "context": context,
                        "ui_context": ui_context, "checked_at": self.clock()}
                    review = secrets.token_hex(24)
                    self.snapshots[review] = {"account_id": account_id, "context": context, "gifts": gifts,
                        "expires": self.clock()+120, "inventory_revision": inventory["revision"], "complete": remote["complete"] is True}
                    if len(self.snapshots) > 32:
                        self.snapshots.pop(next(iter(self.snapshots)))
                    if len(self.observations) > 100:
                        self.observations.pop(next(iter(self.observations)))
                    return {"catalog": self.decorate(scope_ids, ui_context), "review_token": review,
                            "account_id": account_id, "complete": remote["complete"] is True}
                review = payload.get("review_token")
                snap = self.snapshots.get(review) if isinstance(review, str) else None
                if (not snap or snap["expires"] <= self.clock() or snap["account_id"] != account_id
                        or snap["context"] != context or not snap["complete"]
                        or snap["inventory_revision"] != inventory["revision"] or type(payload.get("visible")) is not bool):
                    self.snapshots.pop(review, None) if isinstance(review, str) else None
                    raise CatalogError("Review expired/changed/incomplete; scan the selected account again")
                address = payload.get("nft_address")
                expected = snap["gifts"].get(address) if isinstance(address, str) else None
                rental = next((row for row in inventory["page"]["items"] if row["nft_address"] == address), None)
                if not rental or rental["end_time"] <= self.clock() or not expected or expected["host_user_id"] != account.user_id:
                    self.snapshots.pop(review, None)
                    raise CatalogError("Gift is not an active rental hosted by this account; use Fragment first")
                # Recheck the same provider page before consuming the one-time review token.
                fresh_page = await self.reader.read_rented(
                    token=self.token_source(), bypass_cooldown=True, **inventory["params"],
                )
                fresh_rental = next((row for row in fresh_page["items"] if row["nft_address"] == address), None)
                if not fresh_rental or not self._rental_unchanged(rental, fresh_rental) or fresh_rental["end_time"] <= self.clock():
                    self.snapshots.pop(review, None)
                    raise CatalogError("Rental changed/ended; refresh and scan again")
                guard_scope()
                self.current_inventory()
                self.snapshots.pop(review, None)
                self.observations.pop(account_id, None)
                async with self.client(account) as client:
                    before = await client.list_own_hosted_gifts()
                    current = next((row for row in before["gifts"] if row["nft_address"] == address), None)
                    if current != expected or self.account(account_id, ui_context)[1] != context:
                        raise CatalogError("Gift hosting/account changed; no visibility change sent")
                    guard_scope()
                    self.current_inventory()
                    if rental["end_time"] <= self.clock() or snap["expires"] <= self.clock():
                        raise CatalogError("Rental or review expired before visibility change")
                    try:
                        await client.set_hosted_gift_visibility(expected["slug"], payload["visible"])
                        after = await client.list_own_hosted_gifts()
                        actual = next((row for row in after["gifts"] if row["nft_address"] == address), None)
                        if not actual or actual["visible"] != payload["visible"] or actual["host_user_id"] != account.user_id:
                            raise CatalogError("Visibility outcome requires a fresh scan")
                    except BaseException:
                        # Do not replay a request after timeout/cancel/unknown outcome.
                        self.snapshots.clear()
                        raise
                if self.account(account_id, ui_context)[1] != context:
                    raise CatalogError("Account changed after request; scan again")
                guard_scope()
                self.observations[account_id] = {"gifts": {row["nft_address"]: row for row in after["gifts"]},
                    "context": context, "ui_context": ui_context, "checked_at": self.clock()}
                return {"catalog": self.decorate(scope_ids, ui_context), "account_id": account_id,
                        "detail": "Telegram visibility verified. Hiding does not detach hosting or end the rental."}
