"""Minimal-click MarketApp rent orchestration with durable WAL job state."""
from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
import hashlib
import inspect
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from core.ton_address import format_nano, friendly_address, normalize_address
from modules.accounts import AccountService
from modules.asset_registry import AssetStore
from modules.marketapp_catalog import (
    CatalogError,
    MarketappCatalogReader,
    SECONDS_PER_DAY,
    annotate_rent_item,
    compute_rent_total_nanograms,
    format_duration_human,
    format_marketapp_amount,
)
from modules.marketapp_credentials import MarketappCredentials
from modules.marketapp_rentals import MarketappRentalService, _is_stopped
from modules.ton_readonly import ReadError, TonCenterReader
from utils.telegram_errors import short_error

IN_FLIGHT_STATES = ("tx_claiming", "tx_submitted", "tx_uncertain")
HOSTING_IN_PROGRESS_STATES = ("tonconnect_submitted", "hosted_verified")
TERMINAL_STATES = ("visible_verified", "extended_verified", "cancelled_verified", "failed")


def _configure_wal(db: sqlite3.Connection) -> None:
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    mode = db.execute("PRAGMA journal_mode").fetchone()
    if not mode or str(mode[0]).lower() != "wal":
        db.execute("PRAGMA journal_mode=WAL")


def _quote_hash(operation: str, nft_addresses: list[str], duration: int | None, price_per_day: str | None, tx: dict) -> str:
    payload = {
        "operation": operation,
        "nft_addresses": nft_addresses,
        "duration": duration,
        "price_per_day": price_per_day,
        "messages": [
            {"address": m["raw_address"], "amount": m["amount"], "payload": m["payload"], "state_init": m["state_init"]}
            for m in tx.get("messages", [])
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _notify(progress, stage: str, detail: str, **extra) -> None:
    if progress is None:
        return
    fn = getattr(progress, "emit", None) or progress
    if callable(fn):
        fn({"stage": stage, "detail": detail, **extra})


class RentJobStore:
    """WAL SQLite durability + idempotency guard for MarketApp rental jobs."""

    def __init__(self, data_dir, *, clock=time.time):
        self.path = Path(data_dir) / "rent_jobs.sqlite3"
        self.clock = clock

    @contextmanager
    def db(self, *, write=False):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            _configure_wal(conn)
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS rent_jobs (
                        job_id TEXT PRIMARY KEY,
                        operation TEXT NOT NULL,
                        nft_address TEXT NOT NULL,
                        nft_addresses_json TEXT NOT NULL,
                        asset_kind TEXT NOT NULL,
                        account_id TEXT,
                        duration INTEGER,
                        price_per_day TEXT,
                        sale_during_rent INTEGER NOT NULL DEFAULT 0,
                        total_ton_nano TEXT NOT NULL,
                        valid_until INTEGER NOT NULL,
                        tx_json TEXT NOT NULL,
                        quote_hash TEXT NOT NULL,
                        review_token TEXT NOT NULL,
                        confirmation_text TEXT NOT NULL,
                        state TEXT NOT NULL,
                        wallet_tx_id TEXT,
                        tonconnect_url_hash TEXT,
                        auto_visible INTEGER NOT NULL DEFAULT 1,
                        last_error TEXT,
                        detail_json TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_rent_jobs_state ON rent_jobs(state, updated_at DESC)"
                )
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        tx = json.loads(row["tx_json"])
        detail = json.loads(row["detail_json"])
        return {
            "job_id": row["job_id"],
            "operation": row["operation"],
            "nft_address": row["nft_address"],
            "nft_addresses": json.loads(row["nft_addresses_json"]),
            "asset_kind": row["asset_kind"],
            "account_id": row["account_id"],
            "duration": row["duration"],
            "price_per_day": row["price_per_day"],
            "sale_during_rent": bool(row["sale_during_rent"]),
            "total_ton_nano": row["total_ton_nano"],
            "total_ton": format_nano(row["total_ton_nano"]),
            "valid_until": row["valid_until"],
            "tx": tx,
            "quote_hash": row["quote_hash"],
            "review_token": row["review_token"],
            "confirmation_text": row["confirmation_text"],
            "state": row["state"],
            "wallet_tx_id": row["wallet_tx_id"],
            "tonconnect_linked": bool(row["tonconnect_url_hash"]),
            "auto_visible": bool(row["auto_visible"]),
            "last_error": row["last_error"],
            "detail": detail,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_quoted_job(
        self,
        *,
        operation: str,
        nft_addresses: list[str],
        asset_kind: str,
        account_id: str | None,
        duration: int | None,
        price_per_day: str | None,
        sale_during_rent: bool,
        tx: dict,
        confirmation_text: str,
        auto_visible: bool,
        detail: dict | None = None,
    ) -> dict:
        now = float(self.clock())
        job_id = uuid.uuid4().hex
        review_token = secrets.token_hex(24)
        primary_nft = nft_addresses[0]
        qhash = _quote_hash(operation, nft_addresses, duration, price_per_day, tx)
        with self.db(write=True) as conn:
            conn.execute(
                """
                INSERT INTO rent_jobs(
                    job_id, operation, nft_address, nft_addresses_json, asset_kind, account_id,
                    duration, price_per_day, sale_during_rent, total_ton_nano, valid_until,
                    tx_json, quote_hash, review_token, confirmation_text, state,
                    wallet_tx_id, tonconnect_url_hash, auto_visible, last_error, detail_json,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    operation,
                    primary_nft,
                    json.dumps(nft_addresses),
                    asset_kind,
                    account_id,
                    duration,
                    price_per_day,
                    1 if sale_during_rent else 0,
                    tx["total_ton_nano"],
                    int(tx["valid_until"]),
                    json.dumps(tx, sort_keys=True),
                    qhash,
                    review_token,
                    confirmation_text,
                    "quoted",
                    None,
                    None,
                    1 if auto_visible else 0,
                    None,
                    json.dumps(detail or {}, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM rent_jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._row_to_dict(row)

    def get_job(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not job_id:
            raise CatalogError("Unknown rent job ID")
        with self.db(write=False) as conn:
            row = conn.execute("SELECT * FROM rent_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise CatalogError("Rent job not found")
            return self._row_to_dict(row)

    def list_jobs(self, *, state: str | None = None, limit: int = 50) -> list[dict]:
        lim = max(1, min(200, int(limit)))
        with self.db(write=False) as conn:
            if state is not None:
                rows = conn.execute(
                    "SELECT * FROM rent_jobs WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                    (state, lim),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM rent_jobs ORDER BY updated_at DESC LIMIT ?",
                    (lim,),
                ).fetchall()
            return [self._row_to_dict(row) for row in rows]

    def claim_tx_execution(self, job_id: str, *, review_token: str, confirmation: str) -> dict:
        now = float(self.clock())
        with self.db(write=True) as conn:
            row = conn.execute("SELECT * FROM rent_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise CatalogError("Rent job not found")
            if row["state"] != "quoted":
                raise CatalogError(f"Job is already in state '{row['state']}'; use resume instead of re-paying")
            if row["review_token"] != review_token:
                raise CatalogError("Rent quote review token mismatch; prepare a fresh quote")
            if not isinstance(confirmation, str) or confirmation.strip() != row["confirmation_text"]:
                raise CatalogError("Confirmation phrase does not match the reviewed rent quote")
            if now >= float(row["valid_until"]):
                conn.execute(
                    "UPDATE rent_jobs SET state='failed', last_error=?, updated_at=? WHERE job_id=?",
                    ("Quote validUntil expired before submission", now, job_id),
                )
                raise CatalogError("Marketapp payment quote expired; prepare a fresh quote")

            target_set = set(json.loads(row["nft_addresses_json"]))
            placeholders = ",".join("?" for _ in IN_FLIGHT_STATES)
            conflicts = conn.execute(
                f"SELECT job_id, operation, nft_addresses_json, state FROM rent_jobs WHERE job_id<>? AND state IN ({placeholders})",
                (job_id, *IN_FLIGHT_STATES),
            ).fetchall()
            for item in conflicts:
                if target_set & set(json.loads(item["nft_addresses_json"])):
                    raise CatalogError(
                        f"Another active rent job ({item['job_id'][:8]}:{item['state']}) already covers this NFT"
                    )
            if row["operation"] == "pay":
                hp = ",".join("?" for _ in HOSTING_IN_PROGRESS_STATES)
                hosting = conn.execute(
                    f"SELECT job_id, nft_addresses_json, state FROM rent_jobs WHERE job_id<>? AND operation='pay' AND state IN ({hp})",
                    (job_id, *HOSTING_IN_PROGRESS_STATES),
                ).fetchall()
                for item in hosting:
                    if target_set & set(json.loads(item["nft_addresses_json"])):
                        raise CatalogError(
                            f"Another pay job ({item['job_id'][:8]}:{item['state']}) is still finishing hosting for this NFT"
                        )

            conn.execute(
                "UPDATE rent_jobs SET state='tx_claiming', last_error=NULL, updated_at=? WHERE job_id=?",
                (now, job_id),
            )
            updated = conn.execute("SELECT * FROM rent_jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._row_to_dict(updated)

    def transition(
        self,
        job_id: str,
        state: str,
        *,
        wallet_tx_id: str | None = None,
        tonconnect_url_hash: str | None = None,
        last_error: str | None = None,
        detail_patch: dict | None = None,
    ) -> dict:
        now = float(self.clock())
        with self.db(write=True) as conn:
            row = conn.execute("SELECT * FROM rent_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise CatalogError("Rent job not found")
            detail = json.loads(row["detail_json"])
            if detail_patch:
                detail.update(detail_patch)
            conn.execute(
                """
                UPDATE rent_jobs
                   SET state=?,
                       wallet_tx_id=COALESCE(?, wallet_tx_id),
                       tonconnect_url_hash=COALESCE(?, tonconnect_url_hash),
                       last_error=?,
                       detail_json=?,
                       updated_at=?
                 WHERE job_id=?
                """,
                (
                    state,
                    wallet_tx_id,
                    tonconnect_url_hash,
                    last_error,
                    json.dumps(detail, sort_keys=True),
                    now,
                    job_id,
                ),
            )
            updated = conn.execute("SELECT * FROM rent_jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._row_to_dict(updated)


class RentOrchestratorService:
    """Coordinates MarketApp discovery, Wallet X funding/payment, Fragment link & auto-visibility."""

    def __init__(
        self,
        config,
        *,
        rentals=None,
        reader=None,
        accounts=None,
        ton_reader=None,
        asset_store=None,
        job_store=None,
        token_source=None,
        clock=time.time,
        sleep=asyncio.sleep,
    ):
        self.config = config
        self.clock = clock
        self.sleep = sleep
        self.accounts = accounts if accounts is not None else AccountService(config)
        self.token_source = token_source or MarketappCredentials(config.data_dir).get
        self.reader = reader if reader is not None else MarketappCatalogReader(proxy=getattr(config, "global_proxy", None))
        self.rentals = rentals if rentals is not None else MarketappRentalService(
            config, reader=self.reader, accounts=self.accounts, token_source=self.token_source, clock=clock
        )
        self.ton_reader = ton_reader if ton_reader is not None else TonCenterReader(clock=clock)
        self.asset_store = asset_store if asset_store is not None else AssetStore(config.data_dir, clock=clock)
        self.job_store = job_store if job_store is not None else RentJobStore(config.data_dir, clock=clock)

    def _resolve_account_address(self, account_id: str, addresses_by_account: dict | None) -> str | None:
        if isinstance(addresses_by_account, dict) and account_id in addresses_by_account:
            raw = addresses_by_account[account_id]
            if isinstance(raw, str) and raw.strip():
                try:
                    return normalize_address(raw.strip(), "mainnet").raw
                except ValueError:
                    return None
        try:
            report = self.asset_store.report(account_id, "mainnet")
        except Exception:
            return None
        binding = report.get("binding") if isinstance(report, dict) else None
        if isinstance(binding, dict) and isinstance(binding.get("address"), str):
            return binding["address"]
        return None

    async def _read_balances(self, account_ids: list[str], *, addresses_by_account=None, max_concurrency=3, stop_event=None) -> dict:
        sem = asyncio.Semaphore(max(1, min(5, int(max_concurrency))))
        results = {}

        async def _one(aid: str):
            if _is_stopped(stop_event):
                results[aid] = {"status": "skipped", "error": "Cancelled by stop request"}
                return
            addr = self._resolve_account_address(aid, addresses_by_account)
            if not addr:
                results[aid] = {"status": "unbound", "address": None, "friendly": None, "balance_nano": None, "balance_ton": None, "chain_state": "unknown"}
                return
            async with sem:
                if _is_stopped(stop_event):
                    results[aid] = {"status": "skipped", "address": addr, "error": "Cancelled by stop request"}
                    return
                try:
                    obs = await self.ton_reader.read(addr, "mainnet")
                    results[aid] = {
                        "status": "ok",
                        "address": addr,
                        "friendly": friendly_address(addr, "mainnet", bounceable=False),
                        "balance_nano": obs["balance_nano"],
                        "balance_ton": obs["balance_native"],
                        "chain_state": obs["chain_state"],
                        "provider": obs["provider"],
                        "checked_at": obs["source_sync_time"],
                    }
                except (ReadError, ValueError) as exc:
                    results[aid] = {
                        "status": "error",
                        "address": addr,
                        "friendly": friendly_address(addr, "mainnet", bounceable=False),
                        "balance_nano": None,
                        "balance_ton": None,
                        "chain_state": "unknown",
                        "error": str(exc),
                    }

        await asyncio.gather(*(_one(aid) for aid in account_ids))
        return results

    async def sync_workspace(
        self,
        account_ids=None,
        *,
        scope_ids,
        ui_context,
        include_gifts=True,
        include_usernames=True,
        include_rented=True,
        scan_accounts=True,
        check_balances=True,
        addresses_by_account=None,
        catalog_limit=20,
        usernames_limit=20,
        rented_limit=50,
        sort_by="price_per_day",
        include_sellable=False,
        cursor=None,
        usernames_cursor=None,
        rented_cursor=None,
        collection_address=None,
        model=None,
        symbol=None,
        backdrop=None,
        max_concurrency=3,
        scope_check=None,
        stop_event=None,
    ) -> dict:
        """1-click read-only sync of gifts, usernames, active rentals, hosting & TON balances."""
        now = float(self.clock())
        token = None
        if include_gifts or include_usernames or include_rented:
            token = self.token_source()

        section_errors = {}
        gifts_page = None
        usernames_page = None
        rentals_result = None

        if include_gifts:
            page = await self.reader.read(
                token=token,
                confirmed=True,
                limit=catalog_limit,
                sort_by=sort_by,
                cursor=cursor,
                collection_address=collection_address,
                model=model,
                symbol=symbol,
                backdrop=backdrop,
                include_sellable=include_sellable,
            )
            page["items"] = [annotate_rent_item(item, now=now, asset_kind="gift") for item in page["items"]]
            gifts_page = page

        if include_usernames:
            try:
                usernames_page = await self.reader.read_usernames(
                    token=token,
                    confirmed=True,
                    limit=usernames_limit,
                    sort_by=sort_by,
                    cursor=usernames_cursor,
                    include_sellable=include_sellable,
                )
            except CatalogError as exc:
                section_errors["usernames_catalog"] = str(exc)

        if include_rented:
            rentals_payload = {"confirmed": True, "limit": rented_limit}
            if rented_cursor is not None:
                rentals_payload["cursor"] = rented_cursor
            rentals_result = await self.rentals.execute(
                "rentals", rentals_payload, scope_ids=scope_ids, ui_context=ui_context, scope_check=scope_check
            )

        selected = list(dict.fromkeys(aid for aid in (account_ids or []) if isinstance(aid, str) and aid))
        batch_scan = None
        if scan_accounts and selected:
            batch_scan = await self.rentals.sync_batch(
                selected,
                scope_ids=scope_ids,
                ui_context=ui_context,
                confirmed=True,
                max_concurrency=max_concurrency,
                refresh_rentals=False,
                scope_check=scope_check,
                stop_event=stop_event,
            )
            rentals_result = {"catalog": batch_scan["catalog"]}

        balances = {}
        if check_balances and selected:
            balances = await self._read_balances(
                selected,
                addresses_by_account=addresses_by_account,
                max_concurrency=max_concurrency,
                stop_event=stop_event,
            )

        rented_items = rentals_result["catalog"]["items"] if rentals_result else []
        active_username_names = {
            row["nft_name"].lstrip("@").lower(): row
            for row in rented_items
            if isinstance(row.get("nft_name"), str) and not row.get("expired")
        }

        account_matrix = []
        scan_accounts_map = (batch_scan or {}).get("accounts", {})
        scan_errors_map = (batch_scan or {}).get("errors", {})
        for aid in selected:
            obs = self.rentals.observations.get(aid, {})
            hosted_gifts = list((obs.get("gifts") or {}).values())
            uname = (scan_accounts_map.get(aid) or {}).get("username") or obs.get("username")
            matched_username_rental = active_username_names.get(uname.lower()) if isinstance(uname, str) else None
            account_matrix.append({
                "account_id": aid,
                "username": uname,
                "review_token": (scan_accounts_map.get(aid) or {}).get("review_token"),
                "complete": (scan_accounts_map.get(aid) or {}).get("complete"),
                "hosted_gifts": hosted_gifts,
                "hosted_gift_count": len(hosted_gifts),
                "matched_username_rental": matched_username_rental,
                "scan_error": scan_errors_map.get(aid),
                "balance": balances.get(aid),
            })

        return {
            "checked_at": now,
            "gifts_catalog": gifts_page,
            "usernames_catalog": usernames_page,
            "rentals": rentals_result["catalog"] if rentals_result else None,
            "batch_scan": batch_scan,
            "balances": balances,
            "account_matrix": account_matrix,
            "jobs": self.job_store.list_jobs(limit=50),
            "section_errors": section_errors,
        }

    async def prepare_batch_rent_funding(
        self,
        wallet_x,
        account_ids,
        *,
        mode="fixed",
        amount_ton=None,
        target_balance_ton=None,
        confirmed=True,
        scope_ids=None,
        ui_context=None,
        **kwargs,
    ):
        """Delegate Wallet X preflight batch funding preview for managed accounts."""
        preview_fn = getattr(wallet_x, "preview_batch_fund", None)
        if not callable(preview_fn):
            raise CatalogError("Master Wallet X service with preview_batch_fund is required")
        return await _maybe_await(
            preview_fn(
                account_ids,
                mode=mode,
                amount_ton=amount_ton,
                target_balance_ton=target_balance_ton,
                scope_ids=scope_ids,
                ui_context=ui_context,
                confirmed=confirmed,
                **kwargs,
            )
        )

    async def execute_batch_rent_funding(self, wallet_x, review_token, *, confirmation, stop_event=None, progress=None):
        """Delegate confirmed Wallet X batch funding execution."""
        exec_fn = getattr(wallet_x, "execute_batch_fund", None)
        if not callable(exec_fn):
            raise CatalogError("Master Wallet X service with execute_batch_fund is required")
        return await _maybe_await(
            exec_fn(review_token, confirmation=confirmation, stop_event=stop_event, progress=progress)
        )

    @staticmethod
    def _resolve_duration_seconds(*, duration=None, duration_seconds=None, duration_days=None) -> int:
        if duration is not None and duration_seconds is not None and duration != duration_seconds:
            raise CatalogError("Conflicting duration and duration_seconds values")
        explicit_secs = duration_seconds if duration_seconds is not None else duration
        if explicit_secs is not None:
            if type(explicit_secs) is not int or not 1 <= explicit_secs <= 2**63 - 1:
                raise CatalogError("Duration must be a positive integer number of seconds")
            if duration_days is not None:
                if type(duration_days) is not int or duration_days * SECONDS_PER_DAY != explicit_secs:
                    raise CatalogError("Conflicting duration seconds and duration_days values")
            return explicit_secs
        if duration_days is not None:
            if type(duration_days) is not int or not 1 <= duration_days <= 36500:
                raise CatalogError("Duration days must be a positive integer")
            return duration_days * SECONDS_PER_DAY
        raise CatalogError("Choose rental duration in seconds (or duration_days)")

    async def prepare_rent_pipeline(
        self,
        *,
        operation="pay",
        nft_address=None,
        nft_addresses=None,
        asset_kind="gift",
        duration=None,
        duration_seconds=None,
        duration_days=None,
        price_per_day=None,
        sale_during_rent=False,
        account_id=None,
        nft_name=None,
        auto_visible=True,
        scope_ids=None,
        ui_context=None,
    ) -> dict:
        """Call MarketApp pay/extend/cancel to quote & store an idempotent resumable job."""
        if operation not in {"pay", "extend", "cancel"}:
            raise CatalogError("Unsupported rental pipeline operation")
        if asset_kind not in {"gift", "username"}:
            raise CatalogError("Unsupported rental asset kind")
        token = self.token_source()

        if operation == "cancel":
            targets = list(nft_addresses or ([nft_address] if nft_address else []))
            tx = await self.reader.prepare_rent_cancel(token=token, nft_addresses=targets, confirmed=True)
            norm_list = [normalize_address(addr, "mainnet").raw for addr in targets]
            label = f"CANCEL {len(norm_list)} RENTAL(S)"
            job = self.job_store.create_quoted_job(
                operation="cancel",
                nft_addresses=norm_list,
                asset_kind=asset_kind,
                account_id=None,
                duration=None,
                price_per_day=None,
                sale_during_rent=False,
                tx=tx,
                confirmation_text=f"{label} + {tx['total_ton']} TON GAS",
                auto_visible=False,
                detail={"nft_name": nft_name},
            )
            return self._quote_summary(job, total_gram_nano=None)

        if not nft_address:
            raise CatalogError("Select a target NFT address")
        secs = self._resolve_duration_seconds(
            duration=duration, duration_seconds=duration_seconds, duration_days=duration_days
        )
        norm_nft = normalize_address(nft_address, "mainnet").raw
        if account_id is not None:
            if not scope_ids or account_id not in scope_ids:
                raise CatalogError("Select a target account within the current group/filter")
            self.rentals.account(account_id, ui_context)

        detail = {"nft_name": nft_name}
        if operation == "pay":
            tx = await self.reader.prepare_rent_pay(
                token=token,
                nft_address=norm_nft,
                duration=secs,
                price_per_day=price_per_day,
                sale_during_rent=sale_during_rent,
                confirmed=True,
            )
            verb = "PAY RENT"
        else:
            tx = await self.reader.prepare_rent_extend(
                token=token,
                nft_address=norm_nft,
                duration=secs,
                price_per_day=price_per_day,
                confirmed=True,
            )
            verb = "EXTEND RENT"
            if self.rentals.inventory:
                existing = next(
                    (row for row in self.rentals.inventory["page"]["items"] if row["nft_address"] == norm_nft),
                    None,
                )
                if existing:
                    detail["baseline_end_time"] = existing["end_time"]

        total_gram_nano = compute_rent_total_nanograms(price_per_day, secs)
        total_gram_str = format_marketapp_amount(total_gram_nano, unit="GRAM")
        human_dur = format_duration_human(secs)
        display_name = (nft_name or norm_nft[:14]).strip()
        confirm = f"{verb} {display_name} {human_dur} ({secs}s) {total_gram_str} + {tx['total_ton']} TON"
        job = self.job_store.create_quoted_job(
            operation=operation,
            nft_addresses=[norm_nft],
            asset_kind=asset_kind,
            account_id=account_id,
            duration=secs,
            price_per_day=str(int(price_per_day)),
            sale_during_rent=sale_during_rent,
            tx=tx,
            confirmation_text=confirm,
            auto_visible=bool(auto_visible),
            detail=detail,
        )
        return self._quote_summary(job, total_gram_nano=total_gram_nano)

    def _quote_summary(self, job: dict, *, total_gram_nano: str | None) -> dict:
        now = float(self.clock())
        secs = job["duration"]
        return {
            "job_id": job["job_id"],
            "review_token": job["review_token"],
            "confirmation_text": job["confirmation_text"],
            "operation": job["operation"],
            "asset_kind": job["asset_kind"],
            "nft_address": job["nft_address"],
            "nft_addresses": job["nft_addresses"],
            "account_id": job["account_id"],
            "duration": secs,
            "duration_seconds": secs,
            "duration_text": format_duration_human(secs) if secs is not None else None,
            "price_per_day_nanograms": job["price_per_day"],
            "price_per_day_formatted": format_marketapp_amount(job["price_per_day"]) if job["price_per_day"] else None,
            "total_gram_nanograms": total_gram_nano,
            "total_gram_formatted": format_marketapp_amount(total_gram_nano) if total_gram_nano else None,
            "total_ton_nano": job["total_ton_nano"],
            "total_ton": job["total_ton"],
            "valid_until": job["valid_until"],
            "expires_in_seconds": max(0, int(job["valid_until"] - now)),
            "tx": job["tx"],
            "auto_visible": job["auto_visible"],
            "state": job["state"],
        }

    def list_jobs(self, *, state: str | None = None, limit: int = 50) -> list[dict]:
        return self.job_store.list_jobs(state=state, limit=limit)

    def get_job(self, job_id: str) -> dict:
        return self.job_store.get_job(job_id)

    async def execute_rent_pipeline(
        self,
        job_id: str,
        *,
        review_token: str,
        confirmation: str,
        wallet_x=None,
        tonconnect_url: str | None = None,
        scope_ids=None,
        ui_context=None,
        auto_visible: bool | None = None,
        poll_timeout: float = 60.0,
        poll_interval: float = 3.0,
        stop_event=None,
        progress=None,
    ) -> dict:
        """Execute a confirmed quoted rental job end-to-end without double-spend risk."""
        job = self.job_store.claim_tx_execution(job_id, review_token=review_token, confirmation=confirmation)
        if auto_visible is not None:
            job = self.job_store.transition(job_id, job["state"], detail_patch={"auto_visible_override": bool(auto_visible)})

        preview_raw = getattr(wallet_x, "preview_raw_messages", None) if wallet_x is not None else None
        execute_raw = getattr(wallet_x, "execute_raw_messages", None) if wallet_x is not None else None
        if not callable(preview_raw) or not callable(execute_raw):
            handoff = self.job_store.transition(
                job_id,
                "handoff_required",
                last_error="Wallet X raw-message execution is unavailable; submit tx externally and call resume_rent_job",
            )
            _notify(progress, "handoff_required", "External wallet signature required", job_id=job_id)
            return {"ok": False, "state": "handoff_required", "job": handoff, "tx": job["tx"]}

        _notify(progress, "tx_sending", "Simulating and broadcasting MarketApp transaction via Wallet X", job_id=job_id)
        raw_msgs = [
            {
                "address": m["address"],
                "raw_address": m["raw_address"],
                "amount": m["amount"],
                "payload": m["payload"],
                "payload_b64": m["payload"],
                "stateInit": m["state_init"],
                "state_init_b64": m["state_init"],
                "bounce": m["bounce"],
            }
            for m in job["tx"]["messages"]
        ]
        try:
            tx_preview = await _maybe_await(
                preview_raw(raw_msgs, valid_until=job["valid_until"], confirmed=True)
            )
        except Exception as exc:
            self.job_store.transition(
                job_id,
                "quoted",
                last_error=f"Preflight failed before signing: {short_error(exc)}",
            )
            raise CatalogError(f"Wallet X preflight failed before signing: {short_error(exc)}") from None

        wx_token = tx_preview.get("review_token") if isinstance(tx_preview, dict) else None
        wx_confirm = (
            (tx_preview.get("expected_confirmation") or tx_preview.get("confirmation_text"))
            if isinstance(tx_preview, dict)
            else None
        ) or confirmation
        try:
            tx_result = await _maybe_await(
                execute_raw(wx_token, confirmation=wx_confirm)
            )
        except Exception as exc:
            self.job_store.transition(
                job_id,
                "tx_uncertain",
                last_error=f"Broadcast outcome uncertain: {short_error(exc)}",
            )
            raise CatalogError(
                f"Transaction outcome is uncertain ({short_error(exc)}); check status / resume job {job_id[:8]} without re-paying"
            ) from None

        tx_id = None
        if isinstance(tx_result, dict):
            tx_id = tx_result.get("boc_hash") or tx_result.get("tx_hash") or tx_result.get("intent_id") or tx_result.get("id")
        detail_patch = {}
        if isinstance(tx_result, dict) and tx_result.get("id"):
            detail_patch["wallet_intent_id"] = tx_result["id"]
        job = self.job_store.transition(
            job_id,
            "tx_submitted",
            wallet_tx_id=str(tx_id) if tx_id else "submitted",
            detail_patch=detail_patch or None,
        )
        return await self._continue_from_state(
            job,
            tonconnect_url=tonconnect_url,
            scope_ids=scope_ids,
            ui_context=ui_context,
            auto_visible=bool(job["auto_visible"] if auto_visible is None else auto_visible),
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
            stop_event=stop_event,
            progress=progress,
        )

    async def resume_rent_job(
        self,
        job_id: str,
        *,
        external_tx_submitted: bool = False,
        tonconnect_url: str | None = None,
        scope_ids=None,
        ui_context=None,
        auto_visible: bool | None = None,
        poll_timeout: float = 60.0,
        poll_interval: float = 3.0,
        stop_event=None,
        progress=None,
    ) -> dict:
        """Safely resume an existing job from its last durable state without re-paying."""
        job = self.job_store.get_job(job_id)
        if job["state"] == "quoted":
            raise CatalogError("Job has not been submitted yet; execute it with confirmation first")
        if job["state"] == "handoff_required":
            if external_tx_submitted is not True:
                raise CatalogError("Confirm external_tx_submitted=True after sending the external wallet transaction")
            job = self.job_store.transition(job_id, "tx_submitted", wallet_tx_id="external_handoff")
        elif job["state"] == "tx_claiming":
            job = self.job_store.transition(
                job_id, "tx_uncertain", last_error="Previous execution interrupted while claiming broadcast"
            )
        if job["state"] in TERMINAL_STATES:
            return {"ok": job["state"] != "failed", "state": job["state"], "job": job}

        use_auto_visible = job["auto_visible"] if auto_visible is None else bool(auto_visible)
        return await self._continue_from_state(
            job,
            tonconnect_url=tonconnect_url,
            scope_ids=scope_ids,
            ui_context=ui_context,
            auto_visible=use_auto_visible,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
            stop_event=stop_event,
            progress=progress,
        )

    async def _continue_from_state(
        self,
        job: dict,
        *,
        tonconnect_url: str | None,
        scope_ids,
        ui_context,
        auto_visible: bool,
        poll_timeout: float,
        poll_interval: float,
        stop_event,
        progress,
    ) -> dict:
        job_id = job["job_id"]
        op = job["operation"]
        token = self.token_source()
        deadline = float(self.clock()) + max(1.0, float(poll_timeout))
        step_sleep = max(0.0, float(poll_interval))
        latest_page = None

        if job["state"] in {"tx_submitted", "tx_uncertain"}:
            _notify(progress, "polling_marketapp", "Waiting for MarketApp my-rented confirmation", job_id=job_id)
            confirmed_rental, latest_page = await self._poll_marketapp_confirmation(
                job, token=token, deadline=deadline, step_sleep=step_sleep, stop_event=stop_event
            )
            if not confirmed_rental:
                pending = self.job_store.get_job(job_id)
                return {
                    "ok": False,
                    "state": pending["state"],
                    "job": pending,
                    "detail": "Transaction not confirmed on MarketApp before timeout; resume later without re-paying.",
                }
            if op == "cancel":
                done_job = self.job_store.transition(
                    job_id, "cancelled_verified", last_error=None, detail_patch={"cancelled_at": self.clock()}
                )
                _notify(progress, "cancelled_verified", "Rental cancellation verified", job_id=job_id)
                return {"ok": True, "state": "cancelled_verified", "job": done_job}
            if op == "extend":
                ext_job = self.job_store.transition(
                    job_id, "extended_verified", last_error=None, detail_patch={"rental": confirmed_rental}
                )
                _notify(progress, "extended_verified", "Rental extension confirmed on MarketApp", job_id=job_id)
                return {"ok": True, "state": "extended_verified", "job": ext_job, "rental": confirmed_rental}
            job = self.job_store.transition(
                job_id, "rented_confirmed", last_error=None, detail_patch={"rental": confirmed_rental}
            )

        if op == "pay" and job["state"] == "rented_confirmed" and tonconnect_url:
            _notify(progress, "tonconnect_binding", "Submitting Fragment TonConnect URI to MarketApp", job_id=job_id)
            tc_res = await self.reader.submit_tonconnect(
                token=token,
                nft_address=job["nft_address"],
                tonconnect_url=tonconnect_url,
                confirmed=True,
            )
            tc_hash = hashlib.sha256(tonconnect_url.strip().encode("utf-8")).hexdigest()
            job = self.job_store.transition(
                job_id,
                "tonconnect_submitted",
                tonconnect_url_hash=tc_hash,
                last_error=None,
                detail_patch={"tonconnect": tc_res},
            )

        if op == "pay" and job["account_id"] and job["state"] in {"rented_confirmed", "tonconnect_submitted"}:
            if not scope_ids or job["account_id"] not in scope_ids:
                return {
                    "ok": False,
                    "state": job["state"],
                    "job": job,
                    "detail": "Target account is not in active scope; select account and resume job to finish hosting/visibility.",
                }
            _notify(progress, "polling_hosting", "Waiting for rented collectible to appear on Telegram account", job_id=job_id)
            hosted = await self._poll_telegram_hosting(
                job, deadline=deadline, step_sleep=step_sleep, stop_event=stop_event
            )
            if not hosted:
                return {
                    "ok": False,
                    "state": job["state"],
                    "job": self.job_store.get_job(job_id),
                    "detail": "Rental is active on MarketApp, waiting for Fragment hosting on Telegram; complete TonConnect if needed and resume.",
                }
            job = self.job_store.transition(job_id, "hosted_verified", last_error=None, detail_patch={"hosted": hosted})

        if op == "pay" and job["account_id"] and job["state"] == "hosted_verified":
            if job["asset_kind"] == "gift" and auto_visible:
                if not scope_ids or job["account_id"] not in scope_ids:
                    return {"ok": False, "state": job["state"], "job": job, "detail": "Target account out of scope"}
                _notify(progress, "setting_visibility", "Enabling profile visibility and verifying", job_id=job_id)
                vis_res = await self._enable_and_verify_visibility(
                    job, token=token, latest_page=latest_page, scope_ids=scope_ids, ui_context=ui_context
                )
                job = self.job_store.transition(
                    job_id,
                    "visible_verified",
                    last_error=None,
                    detail_patch={"visibility_verified": True, "visibility_detail": vis_res.get("detail")},
                )
                _notify(progress, "visible_verified", "Rental hosted and visible on profile", job_id=job_id)
                return {
                    "ok": True,
                    "state": "visible_verified",
                    "job": job,
                    "catalog": vis_res.get("catalog"),
                    "review_token": vis_res.get("review_token"),
                }
            if job["asset_kind"] == "username":
                job = self.job_store.transition(
                    job_id, "visible_verified", last_error=None, detail_patch={"username_hosted_verified": True}
                )
                return {"ok": True, "state": "visible_verified", "job": job}

        return {"ok": True, "state": job["state"], "job": job}

    async def _poll_marketapp_confirmation(self, job: dict, *, token: str, deadline: float, step_sleep: float, stop_event):
        op = job["operation"]
        targets = set(job["nft_addresses"])
        primary = job["nft_address"]
        baseline_end = job["detail"].get("baseline_end_time")

        while not _is_stopped(stop_event):
            page = await self.reader.read_rented(token=token, confirmed=True, limit=100, bypass_cooldown=True)
            by_addr = {row["nft_address"]: row for row in page["items"]}
            if op == "cancel":
                if not (targets & set(by_addr)):
                    return {"cancelled": list(targets)}, page
            elif op == "extend":
                row = by_addr.get(primary)
                if row and row["end_time"] > self.clock() and (baseline_end is None or row["end_time"] > baseline_end):
                    return row, page
            else:
                row = by_addr.get(primary)
                if row and row["end_time"] > self.clock():
                    return row, page
            if float(self.clock()) >= deadline:
                return None, page
            if step_sleep > 0:
                await self.sleep(min(step_sleep, max(0.05, deadline - float(self.clock()))))
        return None, None

    async def _poll_telegram_hosting(self, job: dict, *, deadline: float, step_sleep: float, stop_event):
        account = self.accounts.get_account(job["account_id"])
        if not account.enabled or type(account.user_id) is not int or account.user_id <= 0:
            raise CatalogError("Selected target account is not enabled or identity-verified")

        expected_username = (job["detail"].get("nft_name") or "").lstrip("@").lower()
        while not _is_stopped(stop_event):
            async with self.rentals.client(account) as client:
                if job["asset_kind"] == "username":
                    me = await client.get_me()
                    actual = (me.get("username") or "").lstrip("@").lower()
                    if actual and (not expected_username or actual == expected_username):
                        return {"asset_kind": "username", "username": actual, "user_id": account.user_id}
                else:
                    remote = await client.list_own_hosted_gifts()
                    match = next((g for g in remote["gifts"] if g["nft_address"] == job["nft_address"]), None)
                    if match and match["host_user_id"] == account.user_id:
                        return match
            if float(self.clock()) >= deadline:
                return None
            if step_sleep > 0:
                await self.sleep(min(step_sleep, max(0.05, deadline - float(self.clock()))))
        return None

    async def _enable_and_verify_visibility(self, job: dict, *, token: str, latest_page: dict | None, scope_ids, ui_context) -> dict:
        params = {"confirmed": True, "limit": 100}
        page = latest_page or await self.reader.read_rented(token=token, bypass_cooldown=True, **params)
        async with self.rentals.lock:
            self.rentals.inventory = {
                "page": page,
                "params": dict(params),
                "checked_at": self.clock(),
                "token_tag": hashlib.sha256(json.dumps(token, sort_keys=True, ensure_ascii=True).encode()).hexdigest(),
                "revision": secrets.token_hex(16),
            }
            self.rentals.snapshots.clear()
        scan_res = await self.rentals.execute(
            "scan",
            {"confirmed": True, "account_id": job["account_id"]},
            scope_ids=scope_ids,
            ui_context=ui_context,
        )
        return await self.rentals.execute(
            "visibility",
            {
                "confirmed": True,
                "account_id": job["account_id"],
                "review_token": scan_res["review_token"],
                "nft_address": job["nft_address"],
                "visible": True,
            },
            scope_ids=scope_ids,
            ui_context=ui_context,
        )
