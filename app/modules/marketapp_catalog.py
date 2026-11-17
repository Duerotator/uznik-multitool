"""Marketapp rental catalog, active rentals, and validated transaction prep."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
import math
import re
import time

import httpx

from core.ton_address import format_nano, friendly_address, normalize_address

ENDPOINT = "https://api.marketapp.org/v1/rent/gifts/"
USERNAMES_ENDPOINT = "https://api.marketapp.org/v1/rent/usernames/"
RENTED_ENDPOINT = "https://api.marketapp.org/v1/rent/my-rented/"
PAY_ENDPOINT_TMPL = "https://api.marketapp.org/v1/rent/{nft_address}/pay/"
EXTEND_ENDPOINT_TMPL = "https://api.marketapp.org/v1/rent/{nft_address}/extend/"
CANCEL_ENDPOINT = "https://api.marketapp.org/v1/rent/cancel/"
TONCONNECT_ENDPOINT_TMPL = "https://api.marketapp.org/v1/rent/{nft_address}/tonconnect/"
MAX_RESPONSE = 1024 * 1024
SORTS = {"price_per_day", "min_price", "duration_asc", "duration_desc",
         "item_num_asc", "item_num_desc", "recently_touch"}
_TONCONNECT_PREFIXES = (
    "tc://",
    "ton://",
    "https://app.tonkeeper.com/ton-connect",
    "https://wallet.ton.org/ton-connect",
)


class CatalogError(ValueError):
    """Sanitized error: never include provider bodies or authentication."""


def _text(value, limit=200):
    if (not isinstance(value, str) or not 1 <= len(value) <= limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise CatalogError("Invalid catalog text")
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise CatalogError("Invalid catalog integer")
    return value


def _token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,2048}", value):
        raise CatalogError("A valid Marketapp API token is required")
    return value


def _nanograms(value, *, allow_zero=True):
    if isinstance(value, bool):
        raise CatalogError("Invalid rental price")
    raw = str(value) if type(value) is int and value >= 0 else value
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,37}", raw):
        raise CatalogError("Invalid rental price")
    parsed = int(raw)
    if not allow_zero and parsed <= 0:
        raise CatalogError("Rental price must be positive")
    return str(parsed)


def _address(value, label="Invalid catalog address"):
    try:
        return normalize_address(value, "mainnet")
    except ValueError:
        raise CatalogError(label) from None


def _base64_cell(value, *, required=False):
    if value is None:
        if required:
            raise CatalogError("Missing required transaction cell payload")
        return None
    if not isinstance(value, str) or len(value) > 65536 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise CatalogError("Invalid transaction cell encoding")
    try:
        base64.b64decode(value, validate=True)
    except ValueError:
        raise CatalogError("Invalid transaction base64 payload") from None
    return value


SECONDS_PER_DAY = 86400


def format_marketapp_amount(nanograms, *, unit="GRAM"):
    """Format 9-decimal MarketApp nanogram/nanoton integer strings for UI display."""
    cleaned = _nanograms(nanograms, allow_zero=True)
    formatted = format_nano(cleaned)
    return f"{formatted} {unit}" if unit else formatted


def format_duration_human(seconds):
    """Format MarketApp duration in seconds to compact human text (e.g. '1d', '2d 12h')."""
    secs = _integer(seconds, 0, 2**63 - 1)
    if secs == 0:
        return "0s"
    days, rem = divmod(secs, SECONDS_PER_DAY)
    hours, rem = divmod(rem, 3600)
    minutes, rem_s = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    if rem_s and not days and not hours and not minutes:
        parts.append(f"{rem_s}s")
    return " ".join(parts) or f"{secs}s"


def format_expiry_countdown(end_time, *, now=None):
    """Build UTC timestamp and human countdown metadata from Unix seconds."""
    ts = _integer(end_time, 0, 2**63 - 1)
    ref = int(time.time() if now is None else now)
    remaining = ts - ref
    expired = remaining <= 0
    span = abs(remaining)
    days, rem = divmod(span, SECONDS_PER_DAY)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if not days:
        parts.append(f"{minutes}m")
    compact = " ".join(parts) or "0m"
    countdown = f"Expired {compact} ago" if expired else f"{compact} left"
    utc_text = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "end_time": ts,
        "end_time_utc": utc_text,
        "remaining_seconds": remaining,
        "expired": expired,
        "countdown_text": countdown,
    }


def compute_rent_total_nanograms(price_per_day_nanograms, duration_seconds):
    """Compute exact nanogram rent cost from price_per_day and MarketApp duration in seconds."""
    secs = _integer(duration_seconds, 1, 2**63 - 1)
    per_day = int(_nanograms(price_per_day_nanograms, allow_zero=True))
    total = (per_day * secs + SECONDS_PER_DAY - 1) // SECONDS_PER_DAY if per_day else 0
    return str(total)


def annotate_rent_item(item, *, now=None, asset_kind=None):
    """Return a copy of a catalog/rental row enriched with display metadata."""
    enriched = dict(item)
    if asset_kind is not None:
        enriched["asset_kind"] = asset_kind
    elif "asset_kind" not in enriched:
        enriched["asset_kind"] = "gift"
    if "min_duration" in enriched and enriched["min_duration"] is not None:
        enriched["min_duration_text"] = format_duration_human(enriched["min_duration"])
    if "max_duration" in enriched and enriched["max_duration"] is not None:
        enriched["max_duration_text"] = format_duration_human(enriched["max_duration"])
    if "duration" in enriched and enriched["duration"] is not None:
        enriched["duration_text"] = format_duration_human(enriched["duration"])
    if "extend_duration" in enriched and enriched["extend_duration"] is not None:
        enriched["extend_duration_text"] = format_duration_human(enriched["extend_duration"])
    price = enriched.get("price_per_day_nanograms")
    if price is not None:
        enriched["price_per_day_gram"] = format_nano(_nanograms(price, allow_zero=True))
        enriched["price_per_day_formatted"] = format_marketapp_amount(price, unit="GRAM")
        if "min_duration" in enriched:
            min_total = compute_rent_total_nanograms(price, enriched["min_duration"])
            enriched["min_total_nanograms"] = min_total
            enriched["min_total_formatted"] = format_marketapp_amount(min_total, unit="GRAM")
    if "duration" in enriched and price is not None:
        total_nano = compute_rent_total_nanograms(price, enriched["duration"])
        enriched["duration_total_nanograms"] = total_nano
        enriched["duration_total_formatted"] = format_marketapp_amount(total_nano, unit="GRAM")
    if "end_time" in enriched:
        expiry = format_expiry_countdown(enriched["end_time"], now=now)
        enriched["end_time_utc"] = expiry["end_time_utc"]
        enriched["remaining_seconds"] = expiry["remaining_seconds"]
        enriched["expired"] = expiry["expired"]
        enriched["countdown_text"] = expiry["countdown_text"]
    return enriched


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("Duplicate catalog JSON field")
        result[key] = value
    return result


def _parse_json_object(raw, *, size_error, json_error):
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE:
        raise CatalogError(size_error)
    try:
        data = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise CatalogError(json_error) from None
    if not isinstance(data, dict):
        raise CatalogError(json_error)
    return data


def _parse_listing_page(raw, *, limit=10, include_sellable=False, asset_kind="gift"):
    _integer(limit, 1, 100)
    if type(include_sellable) is not bool:
        raise CatalogError("Choose include_sellable explicitly")
    data = _parse_json_object(
        raw,
        size_error="Catalog response exceeds the size limit",
        json_error="Invalid catalog JSON",
    )
    if not isinstance(data.get("items"), list) or "cursor" not in data:
        raise CatalogError("Incomplete catalog page")
    cursor = data["cursor"]
    if cursor is not None:
        _text(cursor, 2048)
    raw_items = data["items"][:limit]
    rows, seen = [], set()
    for item in raw_items:
        if not isinstance(item, dict):
            raise CatalogError("Invalid catalog item")
        address = _address(item.get("nft_address")).raw
        raw_owner = item.get("owner")
        owner = _address(raw_owner).raw if raw_owner is not None else None
        if address in seen:
            raise CatalogError("Duplicate catalog NFT")
        seen.add(address)
        minimum = _integer(item.get("min_duration"), 1, 2**63 - 1)
        maximum = _integer(item.get("max_duration"), minimum, 2**63 - 1)
        price = item.get("price_per_day")
        if not isinstance(price, str) or not re.fullmatch(r"[0-9]{1,37}", price):
            raise CatalogError("Invalid rental price")
        discount = item.get("discount_per_day")
        if discount is not None:
            if type(discount) not in (int, float) or not 0 <= discount <= 100 or not math.isfinite(discount):
                raise CatalogError("Invalid rental discount")
        listed = item.get("listed_at")
        if listed is not None:
            _integer(listed, 0, 2**63 - 1)
        # Missing safety flag is unknown, not an implicit safe/default false.
        sellable = item.get("is_sellable_during_rent")
        if type(sellable) is not bool:
            raise CatalogError("Rental termination condition is unknown")
        attrs = item.get("attributes")
        if attrs is None:
            attrs = []
        elif not isinstance(attrs, list) or len(attrs) > 64:
            raise CatalogError("Invalid rental attributes")
        clean_attrs = []
        for attr in attrs:
            if not isinstance(attr, dict):
                raise CatalogError("Invalid rental attribute")
            value = attr.get("value")
            if value is None:
                pass
            elif type(value) in (int, float):
                if len(str(value)) > 100 or not math.isfinite(value):
                    raise CatalogError("Invalid rental attribute number")
            else:
                _text(value)
            clean_attrs.append({"trait_type": _text(attr.get("trait_type")), "value": value})
        if sellable and not include_sellable:
            continue
        row = {
            "nft_address": address,
            "nft_name": _text(item.get("nft_name")),
            "owner": owner,
            "attributes": clean_attrs,
            "min_duration": minimum,
            "max_duration": maximum,
            "price_per_day_nanograms": str(int(price)),
            "discount_per_day": discount if discount is not None else 0,
            "listed_at": listed,
            "is_sellable_during_rent": sellable,
            "early_termination_risk": sellable,
            "total_quote": None,
        }
        if asset_kind != "gift":
            row = annotate_rent_item(row, asset_kind=asset_kind)
        rows.append(row)
    result = {
        "provider": "marketapp",
        "verification": "provider_reported",
        "network": "mainnet",
        "price_unit": "nanograms_as_documented",
        "cursor": cursor,
        "items": rows,
        "purchase_available": False,
    }
    if asset_kind != "gift":
        result["inventory_kind"] = f"{asset_kind}s"
    return result


def parse_catalog(raw, *, limit=10, include_sellable=False):
    """Validate one gifts catalog page, project public fields only; no quote/ownership claim."""
    return _parse_listing_page(raw, limit=limit, include_sellable=include_sellable, asset_kind="gift")


def parse_usernames(raw, *, limit=10, include_sellable=False):
    """Validate one usernames catalog page with formatted GRAM metadata."""
    return _parse_listing_page(raw, limit=limit, include_sellable=include_sellable, asset_kind="username")


def parse_rented(raw, *, limit=10):
    _integer(limit, 1, 100)
    data = _parse_json_object(
        raw,
        size_error="Rental response exceeds the size limit",
        json_error="Invalid rental JSON",
    )
    if not isinstance(data.get("items"), list) or "cursor" not in data:
        raise CatalogError("Incomplete rental page")
    cursor = data["cursor"]
    if cursor is not None:
        _text(cursor, 2048)
    rows, seen = [], set()
    for item in data["items"][:limit]:
        if not isinstance(item, dict):
            raise CatalogError("Invalid rental item")
        address = _address(item.get("nft_address"), "Invalid rental NFT address").raw
        if address in seen:
            raise CatalogError("Duplicate rental NFT")
        seen.add(address)
        price = item.get("price_per_day")
        if not isinstance(price, str) or not re.fullmatch(r"[0-9]{1,37}", price):
            raise CatalogError("Invalid rental price")
        # OpenAPI default is false; treat an omitted safety field as unknown.
        sellable = item.get("is_sellable_during_rent")
        if sellable is not None and type(sellable) is not bool:
            raise CatalogError("Invalid rental condition")
        extend_duration = item.get("extend_duration")
        if extend_duration is None:
            extend_duration = 0
        else:
            _integer(extend_duration, 0, 2**63 - 1)
        rows.append({"nft_address": address, "nft_name": _text(item.get("nft_name")),
                     "duration": _integer(item.get("duration"), 1, 2**63 - 1),
                     "end_time": _integer(item.get("end_time"), 0, 2**63 - 1),
                     "extend_duration": extend_duration,
                     "price_per_day_nanograms": str(int(price)), "early_termination_risk": sellable,
                     "is_sellable_during_rent": sellable, "my_rented": True, "total_quote": None})
    return {"provider": "marketapp", "verification": "provider_reported", "network": "mainnet",
            "items": rows, "cursor": cursor, "purchase_available": False, "inventory_kind": "active_rentals"}


def parse_send_tx(raw):
    """Validate MarketApp SendTxSchema returned by pay/, extend/, or cancel/."""
    data = _parse_json_object(
        raw,
        size_error="Transaction response exceeds the size limit",
        json_error="Invalid transaction response JSON",
    )
    tx = data.get("transaction")
    if not isinstance(tx, dict):
        raise CatalogError("Incomplete transaction payload from Marketapp")
    valid_until = _integer(tx.get("validUntil"), 1, 2**63 - 1)
    messages = tx.get("messages")
    if not isinstance(messages, list) or not messages:
        raise CatalogError("Transaction response contains no outgoing messages")
    if len(messages) > 4:
        raise CatalogError("Transaction message count exceeds wallet limit (max 4)")
    parsed_msgs = []
    total_nano = 0
    for msg in messages:
        if not isinstance(msg, dict):
            raise CatalogError("Invalid transaction message item")
        norm = _address(msg.get("address"), "Invalid transaction destination address")
        raw_amount = msg.get("amount")
        if not isinstance(raw_amount, str) or not re.fullmatch(r"[0-9]{1,25}", raw_amount):
            raise CatalogError("Invalid transaction TON amount")
        amount_int = int(raw_amount)
        if amount_int <= 0:
            raise CatalogError("Transaction TON amount must be positive")
        payload_b64 = _base64_cell(msg.get("payload"), required=False)
        state_init_b64 = _base64_cell(msg.get("stateInit"), required=False)
        total_nano += amount_int
        parsed_msgs.append({
            "address": friendly_address(norm.raw, "mainnet", bounceable=norm.bounceable),
            "raw_address": norm.raw,
            "bounce": bool(norm.bounceable),
            "amount": str(amount_int),
            "amount_ton": format_nano(str(amount_int)),
            "payload": payload_b64,
            "state_init": state_init_b64,
        })
    return {
        "provider": "marketapp",
        "network": "mainnet",
        "valid_until": valid_until,
        "messages": parsed_msgs,
        "total_ton_nano": str(total_nano),
        "total_ton": format_nano(str(total_nano)),
    }


def parse_tonconnect_result(raw):
    """Validate MarketApp POST /v1/rent/{nft_address}/tonconnect/ JSON response."""
    data = _parse_json_object(
        raw,
        size_error="TonConnect response exceeds the size limit",
        json_error="Invalid TonConnect response JSON",
    )
    ok = data.get("ok")
    if ok is None:
        ok = str(data.get("status", "")).lower() not in {"error", "failed", "rejected"}
    elif type(ok) is not bool:
        raise CatalogError("Invalid TonConnect ok flag")
    detail = None
    for key in ("status", "message", "detail"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            detail = "".join(ch for ch in val if ord(ch) >= 32 and ord(ch) != 127).strip()[:200]
            break
    return {"provider": "marketapp", "ok": ok, "detail": detail}


class MarketappCatalogReader:
    def __init__(self, *, client=None, clock=time.monotonic, proxy=None):
        self.client, self.clock = client, clock
        self.proxy = proxy.replace("socks5h://", "socks5://") if isinstance(proxy, str) and proxy else None
        self._lock = asyncio.Lock()
        self._next_read = 0.0
        self._next_usernames_read = 0.0
        self._next_rented_read = 0.0
        self._next_write_op = 0.0
        self._next_tonconnect_op = 0.0

    def _build_client(self):
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5),
            proxy=self.proxy,
            trust_env=False,
            follow_redirects=False,
        )

    def _cooldown_value(self, kind):
        return {
            "read": self._next_read,
            "usernames": self._next_usernames_read,
            "rented": self._next_rented_read,
            "write": self._next_write_op,
            "tonconnect": self._next_tonconnect_op,
        }[kind]

    def _set_cooldown(self, kind, value):
        if kind == "usernames":
            self._next_usernames_read = value
        elif kind == "rented":
            self._next_rented_read = value
        elif kind == "write":
            self._next_write_op = value
        elif kind == "tonconnect":
            self._next_tonconnect_op = value
        else:
            self._next_read = value

    def _build_listing_params(self, *, limit, sort_by, cursor, collection_address, model, symbol, backdrop, include_sellable):
        _integer(limit, 1, 100)
        if not isinstance(sort_by, str) or sort_by not in SORTS or type(include_sellable) is not bool:
            raise CatalogError("Invalid catalog filter")
        params = {"limit": limit, "sort_by": sort_by,
                  "include_sellable": str(include_sellable).lower()}
        for key, value in (("cursor", cursor), ("model", model), ("symbol", symbol), ("backdrop", backdrop)):
            if value is not None:
                params[key] = _text(value, 2048 if key == "cursor" else 200)
        if collection_address is not None:
            params["collection_address"] = _address(collection_address, "Invalid collection address").raw
        return params

    async def _run_http(self, method, url, *, token, cooldown_kind, params=None, json_body=None,
                        bypass_cooldown=False, parser=None, error_prefix="Catalog"):
        token = _token(token)
        async with self._lock:
            if not bypass_cooldown and self.clock() < self._cooldown_value(cooldown_kind):
                raise CatalogError(
                    "Catalog cooldown is active; retry explicitly later"
                    if cooldown_kind == "rented"
                    else "Catalog cooldown is active; do not retry automatically"
                )
            if cooldown_kind in {"read", "usernames", "rented"}:
                self._set_cooldown(cooldown_kind, self.clock() + 5)
            try:
                async with asyncio.timeout(25):
                    if self.client is not None:
                        return await self._dispatch(
                            self.client, method, url, token=token, params=params,
                            json_body=json_body, cooldown_kind=cooldown_kind, parser=parser,
                        )
                    async with self._build_client() as client:
                        return await self._dispatch(
                            client, method, url, token=token, params=params,
                            json_body=json_body, cooldown_kind=cooldown_kind, parser=parser,
                        )
            except (httpx.HTTPError, TimeoutError):
                raise CatalogError(f"{error_prefix} network request failed or timed out") from None

    async def _dispatch(self, client, method, url, *, token, params, json_body, cooldown_kind, parser):
        headers = {"Authorization": token, "Accept-Encoding": "gzip, deflate"}
        kwargs = {"params": params, "headers": headers, "follow_redirects": False}
        if json_body is not None:
            kwargs["json"] = json_body
        async with client.stream(method, url, **kwargs) as response:
            if response.status_code != 200:
                retry = response.headers.get("retry-after", "")
                delay = min(int(retry), 86400) if re.fullmatch(r"[0-9]{1,8}", retry) else 60
                self._set_cooldown(cooldown_kind, self.clock() + max(60, delay))
                raise CatalogError(f"Catalog provider HTTP {response.status_code}; no automatic retry")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise CatalogError("Catalog response exceeds the size limit")
                chunks.append(chunk)
            raw = b"".join(chunks)
            return parser(raw)

    async def read(self, *, token, confirmed=False, limit=10, sort_by="price_per_day",
                   cursor=None, collection_address=None, model=None, symbol=None,
                   backdrop=None, include_sellable=False):
        if confirmed is not True:
            raise CatalogError("Explicit provider-read confirmation is required")
        params = self._build_listing_params(
            limit=limit, sort_by=sort_by, cursor=cursor, collection_address=collection_address,
            model=model, symbol=symbol, backdrop=backdrop, include_sellable=include_sellable,
        )
        return await self._run_http(
            "GET", ENDPOINT, token=token, cooldown_kind="read", params=params,
            parser=lambda raw: parse_catalog(raw, limit=limit, include_sellable=include_sellable),
            error_prefix="Catalog",
        )

    async def read_usernames(self, *, token, confirmed=False, limit=10, sort_by="price_per_day",
                             cursor=None, include_sellable=False, bypass_cooldown=False):
        if confirmed is not True:
            raise CatalogError("Explicit provider-read confirmation is required")
        params = self._build_listing_params(
            limit=limit, sort_by=sort_by, cursor=cursor, collection_address=None,
            model=None, symbol=None, backdrop=None, include_sellable=include_sellable,
        )
        return await self._run_http(
            "GET", USERNAMES_ENDPOINT, token=token, cooldown_kind="usernames", params=params,
            bypass_cooldown=bypass_cooldown,
            parser=lambda raw: parse_usernames(raw, limit=limit, include_sellable=include_sellable),
            error_prefix="Username catalog",
        )

    async def read_rented(self, *, token, confirmed=False, limit=10, cursor=None, bypass_cooldown=False):
        if confirmed is not True:
            raise CatalogError("Confirm one authenticated rental read")
        _integer(limit, 1, 100)
        params = {"limit": limit}
        if cursor is not None:
            params["cursor"] = _text(cursor, 2048)
        return await self._run_http(
            "GET", RENTED_ENDPOINT, token=token, cooldown_kind="rented", params=params,
            bypass_cooldown=bypass_cooldown,
            parser=lambda raw: parse_rented(raw, limit=limit),
            error_prefix="Rental",
        )

    async def prepare_rent_pay(self, *, token, nft_address, duration, price_per_day,
                               sale_during_rent=False, confirmed=False):
        """Request an unsigned TON transaction payload to initiate a rental (duration in seconds)."""
        if confirmed is not True:
            raise CatalogError("Confirm Marketapp rental payment quote preparation")
        if type(sale_during_rent) is not bool:
            raise CatalogError("Invalid sale_during_rent option")
        address = _address(nft_address, "Invalid rental NFT address").raw
        duration_seconds = _integer(duration, 1, 2**63 - 1)
        price = _nanograms(price_per_day, allow_zero=False)
        return await self._run_http(
            "POST", PAY_ENDPOINT_TMPL.format(nft_address=address),
            token=token, cooldown_kind="write",
            json_body={"duration": duration_seconds, "price_per_day": price, "sale_during_rent": sale_during_rent},
            parser=parse_send_tx, error_prefix="Rental pay",
        )

    async def prepare_rent_extend(self, *, token, nft_address, duration, price_per_day, confirmed=False):
        """Request an unsigned TON transaction payload to extend an active rental (duration in seconds)."""
        if confirmed is not True:
            raise CatalogError("Confirm Marketapp rental extension quote preparation")
        address = _address(nft_address, "Invalid rental NFT address").raw
        duration_seconds = _integer(duration, 1, 2**63 - 1)
        price = _nanograms(price_per_day, allow_zero=False)
        return await self._run_http(
            "POST", EXTEND_ENDPOINT_TMPL.format(nft_address=address),
            token=token, cooldown_kind="write",
            json_body={"duration": duration_seconds, "price_per_day": price},
            parser=parse_send_tx, error_prefix="Rental extend",
        )

    async def prepare_rent_cancel(self, *, token, nft_addresses, confirmed=False):
        """Request an unsigned TON transaction payload to cancel up to 10 rentals."""
        if confirmed is not True:
            raise CatalogError("Confirm Marketapp rental cancellation quote preparation")
        if not isinstance(nft_addresses, (list, tuple)) or not 1 <= len(nft_addresses) <= 10:
            raise CatalogError("Choose 1 to 10 rental NFTs to cancel")
        normalized, seen = [], set()
        for addr in nft_addresses:
            raw = _address(addr, "Invalid rental NFT address").raw
            if raw not in seen:
                seen.add(raw)
                normalized.append(raw)
        return await self._run_http(
            "POST", CANCEL_ENDPOINT,
            token=token, cooldown_kind="write",
            json_body={"nft_addresses": normalized},
            parser=parse_send_tx, error_prefix="Rental cancel",
        )

    async def submit_tonconnect(self, *, token, nft_address, tonconnect_url, confirmed=False):
        """Bind a Fragment rental TonConnect link to MarketApp for hosting."""
        if confirmed is not True:
            raise CatalogError("Confirm submitting Fragment TonConnect URI to Marketapp")
        address = _address(nft_address, "Invalid rental NFT address").raw
        uri = _text(tonconnect_url, 2048).strip()
        if any(c.isspace() for c in uri) or not any(uri.startswith(p) for p in _TONCONNECT_PREFIXES):
            raise CatalogError("Invalid Fragment TonConnect URI")
        return await self._run_http(
            "POST", TONCONNECT_ENDPOINT_TMPL.format(nft_address=address),
            token=token, cooldown_kind="tonconnect",
            json_body={"tonconnect_url": uri},
            parser=parse_tonconnect_result, error_prefix="TonConnect link",
        )
