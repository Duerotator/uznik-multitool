"""GET-only Marketapp rental catalog. No accounts, signer or payment API."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time

import httpx

from core.ton_address import normalize_address

ENDPOINT = "https://api.marketapp.org/v1/rent/gifts/"
RENTED_ENDPOINT = "https://api.marketapp.org/v1/rent/my-rented/"
MAX_RESPONSE = 1024 * 1024
SORTS = {"price_per_day", "min_price", "duration_asc", "duration_desc",
         "item_num_asc", "item_num_desc", "recently_touch"}


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


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("Duplicate catalog JSON field")
        result[key] = value
    return result


def parse_catalog(raw, *, limit=10, include_sellable=False):
    """Validate one page, project public fields only; no quote/ownership claim."""
    _integer(limit, 1, 100)
    if type(include_sellable) is not bool:
        raise CatalogError("Choose include_sellable explicitly")
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE:
        raise CatalogError("Catalog response exceeds the size limit")
    try:
        data = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise CatalogError("Invalid catalog JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or "cursor" not in data:
        raise CatalogError("Incomplete catalog page")
    cursor = data["cursor"]
    if cursor is not None:
        _text(cursor, 2048)
    raw_items = data["items"][:limit]
    rows, seen = [], set()
    for item in raw_items:
        if not isinstance(item, dict):
            raise CatalogError("Invalid catalog item")
        try:
            address = normalize_address(item.get("nft_address"), "mainnet").raw
            raw_owner = item.get("owner")
            owner = normalize_address(raw_owner, "mainnet").raw if raw_owner is not None else None
        except ValueError:
            raise CatalogError("Invalid catalog address") from None
        if address in seen:
            raise CatalogError("Duplicate catalog NFT")
        seen.add(address)
        minimum = _integer(item.get("min_duration"), 1, 2**63-1)
        maximum = _integer(item.get("max_duration"), minimum, 2**63-1)
        price = item.get("price_per_day")
        if not isinstance(price, str) or not re.fullmatch(r"[0-9]{1,37}", price):
            raise CatalogError("Invalid rental price")
        discount = item.get("discount_per_day")
        if discount is not None:
            if type(discount) not in (int, float) or not 0 <= discount <= 100 or not math.isfinite(discount):
                raise CatalogError("Invalid rental discount")
        listed = item.get("listed_at")
        if listed is not None:
            _integer(listed, 0, 2**63-1)
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
        rows.append({"nft_address": address, "nft_name": _text(item.get("nft_name")),
                     "owner": owner, "attributes": clean_attrs, "min_duration": minimum,
                     "max_duration": maximum, "price_per_day_nanograms": str(int(price)),
                     "discount_per_day": discount if discount is not None else 0, "listed_at": listed,
                     "is_sellable_during_rent": sellable,
                     "early_termination_risk": sellable, "total_quote": None})
    return {"provider": "marketapp", "verification": "provider_reported",
            "network": "mainnet", "price_unit": "nanograms_as_documented",
            "cursor": cursor, "items": rows, "purchase_available": False}


def parse_rented(raw, *, limit=10):
    _integer(limit, 1, 100)
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE:
        raise CatalogError("Rental response exceeds the size limit")
    try:
        data = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise CatalogError("Invalid rental JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or "cursor" not in data:
        raise CatalogError("Incomplete rental page")
    cursor = data["cursor"]
    if cursor is not None:
        _text(cursor, 2048)
    rows, seen = [], set()
    for item in data["items"][:limit]:
        if not isinstance(item, dict):
            raise CatalogError("Invalid rental item")
        try:
            address = normalize_address(item.get("nft_address"), "mainnet").raw
        except ValueError:
            raise CatalogError("Invalid rental NFT address") from None
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
            _integer(extend_duration, 0, 2**63-1)
        rows.append({"nft_address": address, "nft_name": _text(item.get("nft_name")),
                     "duration": _integer(item.get("duration"), 1, 2**63-1),
                     "end_time": _integer(item.get("end_time"), 0, 2**63-1),
                     "extend_duration": extend_duration,
                     "price_per_day_nanograms": str(int(price)), "early_termination_risk": sellable,
                     "is_sellable_during_rent": sellable, "my_rented": True, "total_quote": None})
    return {"provider": "marketapp", "verification": "provider_reported", "network": "mainnet",
            "items": rows, "cursor": cursor, "purchase_available": False, "inventory_kind": "active_rentals"}


class MarketappCatalogReader:
    def __init__(self, *, client=None, clock=time.monotonic, proxy=None):
        self.client, self.clock = client, clock
        self.proxy = proxy.replace("socks5h://", "socks5://") if isinstance(proxy, str) and proxy else None
        self._lock = asyncio.Lock()
        self._next_read = 0.0
        self._next_rented_read = 0.0

    def _build_client(self):
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5),
            proxy=self.proxy,
            trust_env=False,
            follow_redirects=False,
        )

    async def read(self, *, token, confirmed=False, limit=10, sort_by="price_per_day",
                   cursor=None, collection_address=None, model=None, symbol=None,
                   backdrop=None, include_sellable=False):
        if confirmed is not True:
            raise CatalogError("Explicit provider-read confirmation is required")
        if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{1,2048}", token):
            raise CatalogError("A valid Marketapp API token is required")
        _integer(limit, 1, 100)
        if not isinstance(sort_by, str) or sort_by not in SORTS or type(include_sellable) is not bool:
            raise CatalogError("Invalid catalog filter")
        params = {"limit": limit, "sort_by": sort_by,
                  "include_sellable": str(include_sellable).lower()}
        for key, value in (("cursor", cursor), ("model", model), ("symbol", symbol), ("backdrop", backdrop)):
            if value is not None:
                params[key] = _text(value, 2048 if key == "cursor" else 200)
        if collection_address is not None:
            try:
                params["collection_address"] = normalize_address(collection_address, "mainnet").raw
            except ValueError:
                raise CatalogError("Invalid collection address") from None
        async with self._lock:
            if self.clock() < self._next_read:
                raise CatalogError("Catalog cooldown is active; do not retry automatically")
            self._next_read = self.clock() + 5
            try:
                async with asyncio.timeout(25):
                    if self.client is not None:
                        return await self._request(self.client, token, params, limit, include_sellable)
                    async with self._build_client() as client:
                        return await self._request(client, token, params, limit, include_sellable)
            except (httpx.HTTPError, TimeoutError):
                raise CatalogError("Catalog network request failed or timed out") from None

    async def read_rented(self, *, token, confirmed=False, limit=10, cursor=None, bypass_cooldown=False):
        if confirmed is not True:
            raise CatalogError("Confirm one authenticated rental read")
        if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{1,2048}", token):
            raise CatalogError("A valid Marketapp API token is required")
        _integer(limit, 1, 100)
        params = {"limit": limit}
        if cursor is not None:
            params["cursor"] = _text(cursor, 2048)
        async with self._lock:
            if not bypass_cooldown and self.clock() < self._next_rented_read:
                raise CatalogError("Catalog cooldown is active; retry explicitly later")
            self._next_rented_read = self.clock() + 5
            try:
                async with asyncio.timeout(25):
                    if self.client is not None:
                        return await self._request(self.client, token, params, limit, False, rented=True)
                    async with self._build_client() as client:
                        return await self._request(client, token, params, limit, False, rented=True)
            except (httpx.HTTPError, TimeoutError):
                raise CatalogError("Rental network request failed or timed out") from None

    async def _request(self, client, token, params, limit, include_sellable, *, rented=False):
        async with client.stream("GET", RENTED_ENDPOINT if rented else ENDPOINT, params=params,
                                 headers={"Authorization": token, "Accept-Encoding": "gzip, deflate"},
                                 follow_redirects=False) as response:
            if response.status_code != 200:
                retry = response.headers.get("retry-after", "")
                delay = min(int(retry), 86400) if re.fullmatch(r"[0-9]{1,8}", retry) else 60
                cooldown = self.clock() + max(60, delay)
                if rented:
                    self._next_rented_read = cooldown
                else:
                    self._next_read = cooldown
                raise CatalogError(f"Catalog provider HTTP {response.status_code}; no automatic retry")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise CatalogError("Catalog response exceeds the size limit")
                chunks.append(chunk)
            raw = b"".join(chunks)
            return parse_rented(raw, limit=limit) if rented else parse_catalog(raw, limit=limit, include_sellable=include_sellable)
