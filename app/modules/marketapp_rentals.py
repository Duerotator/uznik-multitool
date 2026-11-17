"""Rental inventory + verified own-profile visibility. No Fragment signer/transfer."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import secrets
import time

from core.telegram_client import AccountBusyError, create_client
from modules.accounts import AccountService
from modules.marketapp_catalog import CatalogError, MarketappCatalogReader, annotate_rent_item
from modules.marketapp_credentials import MarketappCredentials
from utils.telegram_errors import flood_wait_seconds, is_frozen_error, is_invalid_auth_error, short_error


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _is_stopped(stop_event):
    if stop_event is None:
        return False
    flag = getattr(stop_event, "is_set", None)
    return bool(flag()) if callable(flag) else bool(stop_event)


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
        self._last_usernames = {}

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
                raw_user = me.get("username")
                self._last_usernames[account.id] = raw_user.lstrip("@").lower() if isinstance(raw_user, str) and raw_user.strip() else None
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

    def _store_snapshot(self, account_id, *, context, gifts, inventory_revision, complete, max_snapshots=64):
        self.snapshots = {
            key: row for key, row in self.snapshots.items()
            if row["expires"] > self.clock() and row["account_id"] != account_id
        }
        review = secrets.token_hex(24)
        self.snapshots[review] = {
            "account_id": account_id,
            "context": context,
            "gifts": gifts,
            "expires": self.clock() + 120,
            "inventory_revision": inventory_revision,
            "complete": complete is True,
        }
        while len(self.snapshots) > max(32, max_snapshots):
            self.snapshots.pop(next(iter(self.snapshots)))
        while len(self.observations) > 100:
            self.observations.pop(next(iter(self.observations)))
        return review

    def decorate(self, scope_ids, ui_context):
        inventory = self.inventory
        now = self.clock()
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
                                  "username": observed.get("username"),
                                  "checked_at": observed["checked_at"], "fresh": now - observed["checked_at"] < 300})
            annotated = annotate_rent_item(rental, now=now)
            rows.append({**annotated, "expired": rental["end_time"] <= now, "observed_accounts": hosts})
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
                    self.observations[account_id] = {
                        "gifts": gifts,
                        "username": self._last_usernames.get(account_id),
                        "context": context,
                        "ui_context": ui_context,
                        "checked_at": self.clock(),
                    }
                    review = self._store_snapshot(
                        account_id, context=context, gifts=gifts,
                        inventory_revision=inventory["revision"],
                        complete=remote["complete"] is True,
                    )
                    return {"catalog": self.decorate(scope_ids, ui_context), "review_token": review,
                            "account_id": account_id, "complete": remote["complete"] is True}
                review = payload.get("review_token")
                snap = self._validate_snapshot(
                    review, account_id=account_id, context=context,
                    inventory_revision=inventory["revision"],
                )
                if type(payload.get("visible")) is not bool:
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
                updated_gifts = {row["nft_address"]: row for row in after["gifts"]}
                self.observations[account_id] = {
                    "gifts": updated_gifts,
                    "username": self._last_usernames.get(account_id),
                    "context": context,
                    "ui_context": ui_context,
                    "checked_at": self.clock(),
                }
                new_review = self._store_snapshot(
                    account_id, context=context, gifts=updated_gifts,
                    inventory_revision=inventory["revision"],
                    complete=after.get("complete") is True,
                )
                return {
                    "catalog": self.decorate(scope_ids, ui_context),
                    "review_token": new_review,
                    "account_id": account_id,
                    "complete": after.get("complete") is True,
                    "detail": "Telegram visibility verified. Hiding does not detach hosting or end the rental.",
                }

    def _validate_snapshot(self, review, *, account_id, context, inventory_revision):
        snap = self.snapshots.get(review) if isinstance(review, str) else None
        if (not snap or snap["expires"] <= self.clock() or snap["account_id"] != account_id
                or snap["context"] != context or not snap["complete"]
                or snap["inventory_revision"] != inventory_revision):
            if isinstance(review, str):
                self.snapshots.pop(review, None)
            raise CatalogError("Review expired/changed/incomplete; scan the selected account again")
        return snap

    async def sync_batch(self, account_ids, *, scope_ids, ui_context, confirmed=True,
                         max_concurrency=3, refresh_rentals=False, rentals_limit=50,
                         scope_check=None, stop_event=None):
        """Scan hosted StarGifts and @usernames across multiple accounts concurrently."""
        if confirmed is not True:
            raise CatalogError("Confirm batch account scan")
        if not isinstance(account_ids, (list, tuple, set)) or not account_ids:
            raise CatalogError("Select at least one account to scan")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 5:
            raise CatalogError("max_concurrency must be between 1 and 5")
        if self.lock.locked():
            raise CatalogError("Another rental operation is running")
        ordered_ids = []
        seen = set()
        for aid in account_ids:
            if isinstance(aid, str) and aid not in seen:
                seen.add(aid)
                ordered_ids.append(aid)
        if not ordered_ids:
            raise CatalogError("Select at least one valid account to scan")

        async with self.lock:
            async with asyncio.timeout(max(120, len(ordered_ids) * 20)):
                if scope_check is not None and not scope_check():
                    raise CatalogError("Group/filter scope changed; reopen and scan again")
                if refresh_rentals or not self.inventory:
                    token = self.token_source()
                    params = {"confirmed": True, "limit": max(1, min(100, int(rentals_limit)))}
                    page = await self.reader.read_rented(token=token, **params)
                    self.inventory = {
                        "page": page, "params": params, "checked_at": self.clock(),
                        "token_tag": fingerprint(token), "revision": secrets.token_hex(16),
                    }
                    self.snapshots.clear()
                inventory = self.current_inventory()

                valid_targets = []
                errors = {}
                for aid in ordered_ids:
                    if aid not in scope_ids:
                        errors[aid] = "Account is outside the current group/filter scope"
                        continue
                    try:
                        account, context = self.account(aid, ui_context)
                    except (CatalogError, KeyError) as exc:
                        errors[aid] = str(exc)
                        continue
                    self.observations.pop(aid, None)
                    valid_targets.append((aid, account, context))

                sem = asyncio.Semaphore(max_concurrency)

                async def _scan_one(aid, account, context):
                    if _is_stopped(stop_event):
                        return aid, None, "Cancelled by stop request"
                    async with sem:
                        if _is_stopped(stop_event):
                            return aid, None, "Cancelled by stop request"
                        try:
                            async with self.client(account) as client:
                                remote = await client.list_own_hosted_gifts()
                            return aid, (account, context, remote), None
                        except CatalogError as exc:
                            return aid, None, str(exc)

                results = await asyncio.gather(*(_scan_one(aid, acc, ctx) for aid, acc, ctx in valid_targets))
                if scope_check is not None and not scope_check():
                    raise CatalogError("Group/filter scope changed; reopen and scan again")
                self.current_inventory()

                scanned = {}
                review_tokens = {}
                snap_cap = max(64, len(ordered_ids) * 2)
                for aid, data, err in results:
                    if err is not None:
                        errors[aid] = err
                        continue
                    account, context, remote = data
                    if self.account(aid, ui_context)[1] != context:
                        errors[aid] = "Selected account changed during scan"
                        continue
                    gifts = {row["nft_address"]: row for row in remote["gifts"]}
                    username = self._last_usernames.get(aid)
                    self.observations[aid] = {
                        "gifts": gifts,
                        "username": username,
                        "context": context,
                        "ui_context": ui_context,
                        "checked_at": self.clock(),
                    }
                    review = self._store_snapshot(
                        aid, context=context, gifts=gifts,
                        inventory_revision=inventory["revision"],
                        complete=remote["complete"] is True,
                        max_snapshots=snap_cap,
                    )
                    review_tokens[aid] = review
                    scanned[aid] = {
                        "review_token": review,
                        "complete": remote["complete"] is True,
                        "gift_count": len(gifts),
                        "username": username,
                        "hosted_addresses": list(gifts),
                    }
                return {
                    "catalog": self.decorate(scope_ids, ui_context),
                    "accounts": scanned,
                    "review_tokens": review_tokens,
                    "errors": errors,
                    "scanned_count": len(scanned),
                    "error_count": len(errors),
                }

    async def set_account_visibility_all(self, account_id, nft_addresses=None, *, visible, confirmed=False,
                                         review_token=None, scope_ids, ui_context, scope_check=None, stop_event=None):
        """Show or hide all (or chosen) active hosted rental gifts on one account."""
        if confirmed is not True or type(visible) is not bool:
            raise CatalogError("Confirm batch gift visibility change")
        if not isinstance(account_id, str) or account_id not in scope_ids:
            raise CatalogError("Select exactly one account within the current group/filter")
        if self.lock.locked():
            raise CatalogError("Another rental operation is running")

        def guard_scope():
            if scope_check is not None and not scope_check():
                raise CatalogError("Group/filter scope changed; reopen and scan again")

        async with self.lock:
            async with asyncio.timeout(180):
                guard_scope()
                account, context = self.account(account_id, ui_context)
                inventory = self.current_inventory()
                snap = None
                if review_token is not None:
                    snap = self._validate_snapshot(
                        review_token, account_id=account_id, context=context,
                        inventory_revision=inventory["revision"],
                    )
                fresh_page = await self.reader.read_rented(
                    token=self.token_source(), bypass_cooldown=True, **inventory["params"],
                )
                active_by_addr = {
                    row["nft_address"]: row
                    for row in fresh_page["items"]
                    if row["end_time"] > self.clock()
                }
                if review_token is not None:
                    self.snapshots.pop(review_token, None)
                self.observations.pop(account_id, None)

                updated, skipped, failed = [], {}, {}
                async with self.client(account) as client:
                    before = await client.list_own_hosted_gifts()
                    if before.get("complete") is not True:
                        raise CatalogError("Account gift scan incomplete; cannot verify hosting safely")
                    hosted_by_addr = {row["nft_address"]: row for row in before["gifts"]}
                    if snap is not None and any(hosted_by_addr.get(addr) != snap["gifts"].get(addr) for addr in (nft_addresses or snap["gifts"])):
                        if nft_addresses is not None:
                            raise CatalogError("Gift hosting changed since scan; refresh before toggling")
                    targets = list(dict.fromkeys(nft_addresses)) if nft_addresses is not None else [
                        addr for addr in hosted_by_addr if addr in active_by_addr
                    ]
                    if not targets:
                        raise CatalogError("No active rented gifts are hosted on this account")

                    try:
                        for addr in targets:
                            if _is_stopped(stop_event):
                                skipped[addr] = "Cancelled by stop request"
                                continue
                            rental = active_by_addr.get(addr)
                            cached_rental = next((r for r in inventory["page"]["items"] if r["nft_address"] == addr), None)
                            hosted = hosted_by_addr.get(addr)
                            if not rental or (cached_rental and not self._rental_unchanged(cached_rental, rental)):
                                skipped[addr] = "Rental not active or changed"
                                continue
                            if not hosted or hosted["host_user_id"] != account.user_id:
                                skipped[addr] = "Gift is not hosted by this account"
                                continue
                            if hosted["visible"] is visible:
                                skipped[addr] = "Already in requested visibility state"
                                continue
                            guard_scope()
                            if self.account(account_id, ui_context)[1] != context:
                                raise CatalogError("Account changed during batch visibility update")
                            await client.set_hosted_gift_visibility(hosted["slug"], visible)
                            updated.append(addr)
                        after = await client.list_own_hosted_gifts()
                    except BaseException:
                        self.snapshots.clear()
                        raise

                after_by_addr = {row["nft_address"]: row for row in after["gifts"]}
                for addr in list(updated):
                    actual = after_by_addr.get(addr)
                    if not actual or actual["visible"] != visible or actual["host_user_id"] != account.user_id:
                        updated.remove(addr)
                        failed[addr] = "Visibility outcome requires fresh verification"

                if self.account(account_id, ui_context)[1] != context:
                    raise CatalogError("Account changed after batch visibility update; scan again")
                guard_scope()
                self.observations[account_id] = {
                    "gifts": after_by_addr,
                    "username": self._last_usernames.get(account_id),
                    "context": context,
                    "ui_context": ui_context,
                    "checked_at": self.clock(),
                }
                new_review = self._store_snapshot(
                    account_id, context=context, gifts=after_by_addr,
                    inventory_revision=inventory["revision"],
                    complete=after.get("complete") is True,
                )
                return {
                    "catalog": self.decorate(scope_ids, ui_context),
                    "review_token": new_review,
                    "account_id": account_id,
                    "complete": after.get("complete") is True,
                    "updated": updated,
                    "skipped": skipped,
                    "failed": failed,
                    "detail": f"Verified visibility={visible} for {len(updated)} hosted rental gift(s).",
                }
