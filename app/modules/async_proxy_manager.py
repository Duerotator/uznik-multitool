from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from modules.proxy_manager import (
    DEFAULT_SOURCES,
    TELEGRAM_DCS,
    ProxyEntry,
    ProxyPool,
    harvest_and_validate,
    validate_proxy,
)

logger = logging.getLogger("async-proxy-manager")

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])

CHECK_INTERVAL_SECONDS = 300
RECHECK_DEAD_AFTER = 1800
MIN_ACTIVE_POOL_SIZE = 20


@dataclass
class ActiveProxy:
    entry: ProxyEntry
    active_connections: int = 0
    last_used: float = 0.0
    fail_count: int = 0

    @property
    def url(self) -> str:
        return self.entry.url

    @property
    def key(self) -> str:
        return f"{self.entry.ip}:{self.entry.port}/{self.entry.protocol}"


class AsyncProxyManager:
    def __init__(
        self,
        pool: ProxyPool,
        sources: list[str] | None = None,
        check_interval: float = CHECK_INTERVAL_SECONDS,
        min_active: int = MIN_ACTIVE_POOL_SIZE,
    ):
        self.pool = pool
        self.sources = sources or DEFAULT_SOURCES
        self.check_interval = check_interval
        self.min_active = min_active

        self._active: dict[str, ActiveProxy] = {}
        self._account_assignments: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        await self._populate_active_pool()
        self._task = asyncio.create_task(self._background_loop(), name="async-proxy-manager")
        logger.info(
            "AsyncProxyManager started (active=%d, check_interval=%.0fs)",
            len(self._active),
            self.check_interval,
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._started = False
        logger.info("AsyncProxyManager stopped")

    async def _background_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.check_interval)
            except asyncio.TimeoutError:
                pass
            if self._stop_event.is_set():
                break
            try:
                await self._check_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background proxy check cycle failed")

    async def _populate_active_pool(self) -> None:
        candidates = await self.pool.candidates(limit=200)
        async with self._lock:
            for entry in candidates:
                key = f"{entry.ip}:{entry.port}/{entry.protocol}"
                if key not in self._active:
                    self._active[key] = ActiveProxy(entry=entry)
        logger.info("Active pool populated: %d proxies", len(self._active))

    async def _check_cycle(self) -> None:
        gateway_mode = os.getenv("PROXY_MODE", "").strip().lower() == "vpn_gateway"
        if gateway_mode:
            # The gateway service may have revived recovering rows since the
            # previous cycle. Pull them back into the in-memory scheduler.
            await self._populate_active_pool()
        if len(self._active) < self.min_active and not gateway_mode:
            logger.info("Active pool low (%d), running harvest", len(self._active))
            await harvest_and_validate(self.pool, self.sources, max_concurrency=400)
            await self._populate_active_pool()

        to_check = list(self._active.values())
        dead_keys: list[str] = []
        check_sem = asyncio.Semaphore(50)

        async def check_one(ap: ActiveProxy) -> None:
            async with check_sem:
                ok, latency = await validate_proxy(
                    ap.entry.ip,
                    ap.entry.port,
                    ap.entry.protocol,
                    timeout=8.0,
                    auth=ap.entry.auth,
                )
            if ok:
                await self.pool.record_success(ap.entry.ip, ap.entry.port, ap.entry.protocol, latency)
                ap.entry.latency = latency
                ap.fail_count = 0
            else:
                ap.fail_count += 1
                key = f"{ap.entry.ip}:{ap.entry.port}/{ap.entry.protocol}"
                dead_keys.append(key)
                if gateway_mode:
                    await self.pool.record_gateway_failure(
                        ap.entry.ip, ap.entry.port, ap.entry.protocol,
                    )
                else:
                    await self.pool.add_to_blacklist(ap.entry.ip, ap.entry.port, ap.entry.protocol)
                    await self.pool.remove_dead_from_pool(ap.entry.ip, ap.entry.port, ap.entry.protocol)

        await asyncio.gather(*(check_one(ap) for ap in to_check), return_exceptions=True)

        if dead_keys:
            async with self._lock:
                for key in dead_keys:
                    removed = self._active.pop(key, None)
                    if removed:
                        for acc_id, proxy_key in list(self._account_assignments.items()):
                            if proxy_key == key:
                                del self._account_assignments[acc_id]
            logger.info("Removed %d dead proxies from active pool", len(dead_keys))

        logger.info(
            "Proxy check cycle done: active=%d, dead_removed=%d",
            len(self._active),
            len(dead_keys),
        )

    async def get_proxy_for_account(
        self, account_id: str, *, exclude_url: str | None = None
    ) -> ActiveProxy | None:
        async with self._lock:
            existing_key = self._account_assignments.get(account_id)
            if existing_key and existing_key in self._active:
                ap = self._active[existing_key]
                if not exclude_url or ap.url != exclude_url:
                    ap.last_used = time.monotonic()
                    return ap

        candidates = sorted(
            (ap for ap in self._active.values() if not exclude_url or ap.url != exclude_url),
            key=lambda ap: (ap.active_connections, ap.last_used),
        )
        if not candidates:
            await self._populate_active_pool()
            async with self._lock:
                candidates = sorted(
                    (ap for ap in self._active.values() if not exclude_url or ap.url != exclude_url),
                    key=lambda ap: (ap.active_connections, ap.last_used),
                )
        if not candidates:
            return None

        best = candidates[0]
        async with self._lock:
            self._account_assignments[account_id] = best.key
        best.last_used = time.monotonic()
        return best

    def release_proxy(self, account_id: str) -> None:
        pass

    async def mark_proxy_failed(self, account_id: str) -> None:
        failed: ActiveProxy | None = None
        async with self._lock:
            key = self._account_assignments.pop(account_id, None)
            if key:
                failed = self._active.pop(key, None)
        if failed:
            if os.getenv("PROXY_MODE", "").strip().lower() == "vpn_gateway":
                await self.pool.record_gateway_failure(
                    failed.entry.ip, failed.entry.port, failed.entry.protocol,
                )
                logger.info("VPN exit %s moved to recovery after runtime failure", failed.key)
            else:
                await self.pool.add_to_blacklist(
                    failed.entry.ip, failed.entry.port, failed.entry.protocol
                )
                await self.pool.remove_dead_from_pool(
                    failed.entry.ip, failed.entry.port, failed.entry.protocol
                )
                logger.info("Proxy %s removed from pool after runtime failure", failed.key)

    async def mark_proxy_success(self, account_id: str, latency: float = 0.0) -> None:
        async with self._lock:
            key = self._account_assignments.get(account_id)
            if key:
                ap = self._active.get(key)
                if ap:
                    ap.fail_count = 0
                    ap.last_used = time.monotonic()
                    if latency > 0:
                        ap.entry.latency = latency

    def active_count(self) -> int:
        return len(self._active)

    def stats(self) -> dict[str, Any]:
        return {
            "active_count": len(self._active),
            "assigned_accounts": len(self._account_assignments),
            "avg_connections": (
                sum(ap.active_connections for ap in self._active.values()) / max(1, len(self._active))
            ),
        }

    async def refresh_pool(self) -> int:
        if os.getenv("PROXY_MODE", "").strip().lower() != "vpn_gateway":
            await harvest_and_validate(self.pool, self.sources, max_concurrency=400)
        before = len(self._active)
        await self._populate_active_pool()
        return len(self._active) - before


_manager_instance: AsyncProxyManager | None = None


def get_async_proxy_manager(pool: ProxyPool | None = None, sources: list[str] | None = None) -> AsyncProxyManager:
    global _manager_instance
    if _manager_instance is None:
        if pool is None:
            from core.config import AppConfig
            config = AppConfig.load()
            pool = ProxyPool(config.proxy_pool_db)
        _manager_instance = AsyncProxyManager(pool, sources)
    return _manager_instance


def retry_with_proxy_swap(max_retries: int = 3):
    def decorator(func: F) -> F:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            from core.telegram_client import AccountClient

            client: AccountClient | None = None
            for arg in args:
                if isinstance(arg, AccountClient):
                    client = arg
                    break
            if client is None:
                for value in kwargs.values():
                    if isinstance(value, AccountClient):
                        client = value
                        break

            last_exc: Exception | None = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except (ConnectionResetError, OSError) as exc:
                    msg = str(exc).lower()
                    is_connection_error = any(
                        kw in msg
                        for kw in ("connection reset", "connection refused", "connection aborted", "broken pipe")
                    )
                    if not is_connection_error:
                        raise
                    last_exc = exc
                    if client is not None:
                        client.log.warning(
                            "Connection error on attempt %d/%d: %s — swapping proxy",
                            attempt + 1,
                            max_retries,
                            exc,
                        )
                        manager = get_async_proxy_manager()
                        await manager.mark_proxy_failed(client.account.id)
                        await client._replace_proxy()
                    else:
                        raise
                except Exception as exc:
                    msg = str(exc).lower()
                    if "connection" in msg and "reset" in msg:
                        last_exc = exc
                        if client is not None:
                            client.log.warning(
                                "Connection reset on attempt %d/%d: %s — swapping proxy",
                                attempt + 1,
                                max_retries,
                                exc,
                            )
                            manager = get_async_proxy_manager()
                            await manager.mark_proxy_failed(client.account.id)
                            await client._replace_proxy()
                            continue
                    raise

            if last_exc is not None:
                raise last_exc
            raise RuntimeError("All retry attempts exhausted")

        return wrapper  # type: ignore[return-value]

    return decorator
