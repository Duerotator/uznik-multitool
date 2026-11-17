"""Fixed-host, unauthenticated read-only TON Center v2 adapter.

Reads native TON, not Telegram Money availability, Gram jettons or rewards.
"""
from __future__ import annotations

import base64
import asyncio
import json
import os
import re
import time

import httpx
from core.ton_address import normalize_address, format_nano

ENDPOINTS = {"mainnet": "https://toncenter.com/api/v2/getAddressInformation",
             "testnet": "https://testnet.toncenter.com/api/v2/getAddressInformation"}
TONAPI_ENDPOINTS = {"mainnet": "https://tonapi.io/v2/accounts",
                    "testnet": "https://testnet.tonapi.io/v2/accounts"}
MAX_RESPONSE = 1024*1024


def toncenter_headers(base=None):
    headers = dict(base or {})
    key = os.getenv("TONCENTER_API_KEY", "").strip()
    if key:
        headers["X-API-Key"] = key
    return headers


class ReadError(RuntimeError):
    def __init__(self, message="Public chain read failed", *, retry_after=60):
        super().__init__(message)
        self.retry_after = retry_after


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ReadError("Provider returned duplicate JSON fields")
        result[key] = value
    return result


def _unsigned(value, *, bits=120):
    if type(value) is int:
        value = str(value)
    if (not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,36}", value)
            or int(value) >= 2**bits):
        raise ReadError("Provider returned an invalid integer amount")
    return value


def parse_account(raw, *, now):
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE:
        raise ReadError("Provider response exceeds the size limit")
    try:
        data = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise ReadError("Provider returned invalid JSON") from exc
    if not isinstance(data, dict) or data.get("ok") is not True or not isinstance(data.get("result"), dict):
        raise ReadError("Provider did not return a successful account read")
    result = data["result"]
    balance = _unsigned(result.get("balance"))
    states = {"active": "active", "uninitialized": "uninit", "frozen": "frozen"}
    raw_state = result.get("state")
    state = states.get(raw_state) if isinstance(raw_state, str) else None
    if state is None:
        raise ReadError("Provider returned an unsupported chain state")
    sync = result.get("sync_utime")
    if type(sync) is not int or not 0 <= sync <= now+30:
        raise ReadError("Provider sync timestamp is missing or invalid")
    if now-sync > 300:
        raise ReadError("Provider state is stale; previous balance was not refreshed")
    tx = result.get("last_transaction_id")
    if not isinstance(tx, dict):
        raise ReadError("Provider transaction reference is missing")
    lt = _unsigned(tx.get("lt"), bits=64)
    value = tx.get("hash")
    if not isinstance(value, str) or len(value) != 44:
        raise ReadError("Provider transaction reference is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ReadError("Provider transaction reference is invalid") from exc
    if len(decoded) != 32 or base64.b64encode(decoded).decode() != value:
        raise ReadError("Provider transaction hash is invalid")
    block = result.get("block_id")
    if not isinstance(block, dict) or type(block.get("seqno")) is not int or not 0 <= block["seqno"] < 2**32:
        raise ReadError("Provider block reference is missing or invalid")
    return {"balance_nano": balance, "balance_native": format_nano(balance), "native_currency": "TON",
            "chain_state": state, "source_sync_time": sync, "block_seqno": block["seqno"],
            "last_transaction": {"lt": lt, "hash": value}, "verification": "provider_reported"}


def parse_tonapi_account(account_raw, head_raw, tx_raw, *, now):
    if any(not isinstance(part, bytes) or len(part) > MAX_RESPONSE for part in (account_raw, head_raw, tx_raw)):
        raise ReadError("Provider response exceeds the size limit")
    try:
        account = json.loads(account_raw, object_pairs_hook=_unique)
        head = json.loads(head_raw, object_pairs_hook=_unique)
        txs = json.loads(tx_raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise ReadError("Provider returned invalid JSON") from exc
    if not isinstance(account, dict) or not isinstance(head, dict) or not isinstance(txs, dict):
        raise ReadError("Provider returned an invalid fallback envelope")
    balance = _unsigned(account.get("balance"))
    states = {"active": "active", "uninit": "uninit", "nonexist": "uninit", "frozen": "frozen"}
    raw_state = account.get("status")
    state = states.get(raw_state) if isinstance(raw_state, str) else None
    if state is None:
        raise ReadError("Provider returned an unsupported chain state")
    seqno = head.get("seqno")
    gen_utime = head.get("gen_utime")
    if type(seqno) is not int or not 0 <= seqno < 2**32 or type(gen_utime) is not int or not 0 <= gen_utime <= now + 30:
        raise ReadError("Provider masterchain sync is missing or invalid")
    if now - gen_utime > 300:
        raise ReadError("Provider state is stale; previous balance was not refreshed")
    tx_list = txs.get("transactions")
    if not isinstance(tx_list, list):
        raise ReadError("Provider transaction history list is invalid")
    if tx_list:
        tx0 = tx_list[0]
        if not isinstance(tx0, dict):
            raise ReadError("Provider transaction entry is invalid")
        lt = _unsigned(tx0.get("lt"), bits=64)
        hx = tx0.get("hash")
        if not isinstance(hx, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", hx):
            raise ReadError("Provider transaction hash is invalid")
        tx_hash = base64.b64encode(bytes.fromhex(hx)).decode()
    else:
        lt = "0"
        tx_hash = base64.b64encode(bytes(32)).decode()
    return {"balance_nano": balance, "balance_native": format_nano(balance), "native_currency": "TON",
            "chain_state": state, "source_sync_time": gen_utime, "block_seqno": seqno,
            "last_transaction": {"lt": lt, "hash": tx_hash}, "verification": "provider_reported"}


def validate_read(value, address, network, *, now):
    fields = {"balance_nano", "balance_native", "native_currency", "chain_state", "source_sync_time", "block_seqno",
              "last_transaction", "verification", "provider", "network", "address"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ReadError("Incomplete or unsupported public balance observation")
    provider = value["provider"]
    if (provider not in {"toncenter-v2", "tonapi-v2"} or value["verification"] != "provider_reported"
            or value["network"] != network or value["address"] != address or value["native_currency"] != "TON"):
        raise ReadError("Balance source, network or address does not match the binding")
    states = {"active": "active", "uninit": "uninitialized", "frozen": "frozen"}
    checked = parse_account(json.dumps({"ok": True, "result": {
        "balance": value["balance_nano"], "state": states.get(value["chain_state"]) if isinstance(value["chain_state"], str) else None,
        "sync_utime": value["source_sync_time"], "block_id": {"seqno": value["block_seqno"]},
        "last_transaction_id": value["last_transaction"],
    }}).encode(), now=now)
    if checked["balance_native"] != value["balance_native"]:
        raise ReadError("Inconsistent native balance display")
    return {**checked, "provider": provider, "network": network, "address": address}


class TonCenterReader:
    def __init__(self, *, client=None, fallback_client=None, clock=time.time):
        self.client, self.fallback_client, self.clock = client, fallback_client, clock

    async def _bounded_get(self, client, url, *, params=None, headers=None):
        try:
            async with client.stream("GET", url, params=params, headers=headers or {}, follow_redirects=False) as response:
                if response.status_code != 200:
                    retry = response.headers.get("retry-after", "")
                    seconds = int(retry) if re.fullmatch(r"[0-9]{1,8}", retry) else 60
                    raise ReadError(f"Public chain provider HTTP {response.status_code}", retry_after=max(60, seconds))
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ReadError("Compressed provider responses are not accepted")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        raise ReadError("Provider response exceeds the size limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            # Never forward response bodies, request objects or auth headers.
            raise ReadError("Public chain request failed or timed out") from exc

    async def read(self, address, network):
        canonical = normalize_address(address, network).raw
        try:
            async with asyncio.timeout(25):
                if self.client is not None:
                    try:
                        return await self._request(self.client, canonical, network)
                    except ReadError:
                        if self.fallback_client is not None:
                            return await self._request_tonapi(self.fallback_client, canonical, network)
                        raise
                async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False) as client:
                    try:
                        return await self._request(client, canonical, network)
                    except ReadError as primary_err:
                        try:
                            return await self._request_tonapi(client, canonical, network)
                        except ReadError:
                            raise primary_err from None
        except TimeoutError as exc:
            raise ReadError("Public chain request exceeded its total deadline") from exc

    async def _request(self, client, address, network):
        raw = await self._bounded_get(
            client, ENDPOINTS[network], params={"address": address},
            headers=toncenter_headers({"Accept-Encoding": "identity"}),
        )
        result = parse_account(raw, now=self.clock())
        return {**result, "provider": "toncenter-v2", "network": network, "address": address}

    async def _request_tonapi(self, client, address, network):
        base = TONAPI_ENDPOINTS[network].rsplit("/accounts", 1)[0]
        hdrs = {"Accept-Encoding": "identity"}
        account_raw = await self._bounded_get(client, f"{base}/accounts/{address}", headers=hdrs)
        head_raw = await self._bounded_get(client, f"{base}/blockchain/masterchain-head", headers=hdrs)
        tx_raw = await self._bounded_get(client, f"{base}/blockchain/accounts/{address}/transactions", params={"limit": 1}, headers=hdrs)
        result = parse_tonapi_account(account_raw, head_raw, tx_raw, now=self.clock())
        return {**result, "provider": "tonapi-v2", "network": network, "address": address}
