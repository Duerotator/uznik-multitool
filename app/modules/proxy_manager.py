from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import logging
import os
import random
import re
import secrets
import socket
import sqlite3
import struct
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from core.diagnostics import get_proxy_diagnostics

logger = logging.getLogger("proxy-manager")

PROXY_REGEX = re.compile(
    r"(?:(?:socks[45]|socks5h|http|https)://)?"
    r"(?:\S+:\S+@)?"
    r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
    r":(\d{2,5})"
)

GATEWAY_PORT_MIN = 21000
GATEWAY_PORT_MAX = 21999


def is_gateway_endpoint(ip: str, port: int) -> bool:
    """Return whether an endpoint is one of the fixed local Xray exits."""
    return (
        os.getenv("PROXY_MODE", "").strip().lower() == "vpn_gateway"
        and ip in {"127.0.0.1", "::1"}
        and GATEWAY_PORT_MIN <= port <= GATEWAY_PORT_MAX
    )


def _normalize_ipv4(value: str) -> str | None:
    parts = value.split(".")
    if len(parts) != 4:
        return None
    try:
        numbers = [int(part, 10) for part in parts]
    except ValueError:
        return None
    if any(number < 0 or number > 255 for number in numbers):
        return None
    return ".".join(str(number) for number in numbers)

# Proxy list sources are opt-in; no proxy/provider data ships with the app.
DEFAULT_SOURCES = list(dict.fromkeys(
    url.strip() for url in os.getenv("PROXY_SOURCE_URLS", "").split(";") if url.strip()
))

# Telegram production datacenters. Reaching one of these is the only capability
# this tool needs from a proxy, so it is what validation actually measures.
TELEGRAM_DCS: tuple[tuple[str, int], ...] = (
    ("149.154.167.51", 443),   # DC2 Amsterdam
    ("149.154.175.50", 443),   # DC1 Miami
    ("149.154.175.100", 443),  # DC3 Miami
    ("91.108.56.130", 443),    # DC5 Singapore
)

VALIDATE_TIMEOUT = 6.0
HANDSHAKE_TIMEOUT = 5.0
# Bulk screening is dominated by timeouts, and a proxy worth keeping answers well
# under this, so a tighter bound here buys throughput at almost no cost in yield.
SCREEN_TIMEOUT = 4.0
SCREEN_CONCURRENCY = 600
# A proxy is retired only after this many consecutive runtime failures, so one
# flaky moment does not burn an otherwise good entry.
MAX_FAILS = 3
BLACKLIST_TTL_HOURS = 12
HANDSHAKE_BLACKLIST_DAYS = 30
SOURCE_MIN_REFRESH_MINUTES = 15
SOURCE_MAX_REFRESH_HOURS = 12
DEAD_RETRY_HOURS = 6
# Expanded for SOCKS5/4 only (no HTTP): higher budget for better coverage
VALIDATE_BUDGET = 60000
TARGET_ACTIVE = 500
SLOW_LATENCY = 2.5


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class ProxyEntry:
    ip: str
    port: int
    protocol: str
    latency: float
    last_checked: str
    status: str
    username: str | None = None
    password: str | None = None

    @property
    def url(self) -> str:
        auth = ""
        if self.username:
            user = quote(self.username, safe="")
            password = quote(self.password or "", safe="")
            auth = f"{user}:{password}@"
        return f"{self.protocol}://{auth}{self.ip}:{self.port}"

    @property
    def auth(self) -> tuple[str, str] | None:
        return (self.username, self.password or "") if self.username else None


# RFC 5737 documentation ranges: nothing may ever accept a connection here.
BLACKHOLE_ADDRESSES = (("192.0.2.1", 443), ("198.51.100.1", 12345), ("203.0.113.1", 9999))


async def network_is_intercepted(timeout: float = 4.0) -> bool:
    """Detect a transparent proxy that accepts every outbound TCP connection.

    Corporate networks, some VPNs and most sandboxes answer any connect()
    immediately. Every proxy then looks reachable and the whole probe stage
    produces meaningless results, so it is worth saying so out loud.
    """
    for host, port in BLACKHOLE_ADDRESSES:
        writer = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        except (OSError, asyncio.TimeoutError):
            return False
        finally:
            if writer is not None:
                try:
                    writer.close()
                except OSError:
                    pass
    return True


# ---------------------------------------------------------------------------
# Protocol probes
# ---------------------------------------------------------------------------

async def _socks5_connect(
    reader, writer, dst: tuple[str, int], timeout: float,
    auth: tuple[str, str] | None = None,
) -> bool:
    methods = b"\x00\x02" if auth and auth[0] else b"\x00"
    writer.write(b"\x05" + bytes((len(methods),)) + methods)
    await writer.drain()
    greeting = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
    if greeting[0] != 0x05 or greeting[1] == 0xFF:
        return False
    if greeting[1] == 0x02:
        if not auth or not auth[0]:
            return False
        username = auth[0].encode()
        password = auth[1].encode()
        if len(username) > 255 or len(password) > 255:
            return False
        writer.write(b"\x01" + bytes((len(username),)) + username + bytes((len(password),)) + password)
        await writer.drain()
        auth_reply = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if auth_reply != b"\x01\x00":
            return False
    elif greeting[1] != 0x00:
        return False
    writer.write(b"\x05\x01\x00\x01" + socket.inet_aton(dst[0]) + struct.pack(">H", dst[1]))
    await writer.drain()
    reply = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    if reply[0] != 0x05 or reply[1] != 0x00:
        return False
    if reply[3] == 0x01:
        address_size = 4
    elif reply[3] == 0x04:
        address_size = 16
    elif reply[3] == 0x03:
        address_size = (await asyncio.wait_for(reader.readexactly(1), timeout=timeout))[0]
    else:
        return False
    await asyncio.wait_for(reader.readexactly(address_size + 2), timeout=timeout)
    return True


async def _socks4_connect(reader, writer, dst: tuple[str, int], timeout: float) -> bool:
    writer.write(b"\x04\x01" + struct.pack(">H", dst[1]) + socket.inet_aton(dst[0]) + b"\x00")
    await writer.drain()
    reply = await asyncio.wait_for(reader.readexactly(8), timeout=timeout)
    return reply[1] == 0x5A


async def _http_connect(reader, writer, dst: tuple[str, int], timeout: float, auth: tuple[str, str] | None = None) -> bool:
    host_header = f"Host: {dst[0]}:{dst[1]}"
    if auth and auth[0]:
        import base64
        credentials = f"{auth[0]}:{auth[1]}"
        encoded = base64.b64encode(credentials.encode()).decode()
        auth_header = f"\r\nProxy-Authorization: Basic {encoded}"
    else:
        auth_header = ""
    request = f"CONNECT {dst[0]}:{dst[1]} HTTP/1.1\r\n{host_header}{auth_header}\r\n\r\n".encode()
    writer.write(request)
    await writer.drain()
    status = await asyncio.wait_for(reader.readline(), timeout=timeout)
    accepted = b" 200 " in status or status.startswith(b"HTTP/1.1 200") or status.startswith(b"HTTP/1.0 200")
    # Consume the complete proxy response so its CRLF/header bytes are not
    # mistaken for the first Telegram transport byte by MTProto validation.
    while status:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if line in (b"\r\n", b"\n", b""):
            break
    return accepted


_HANDSHAKES: dict[str, Callable[..., Awaitable[bool]]] = {
    "socks5": _socks5_connect,
    "socks4": _socks4_connect,
    "http": _http_connect,
}


async def probe_proxy(
    ip: str,
    port: int,
    protocol: str,
    target: tuple[str, int] | None = None,
    timeout: float = HANDSHAKE_TIMEOUT,
    auth: tuple[str, str] | None = None,
) -> tuple[bool, float]:
    """Open a real tunnel through the proxy to a Telegram DC.

    Speaks SOCKS5/SOCKS4/HTTP CONNECT directly instead of going through httpx,
    which cannot do SOCKS4 at all and would need a full TLS+HTTP round trip.
    """
    handshake = _HANDSHAKES.get(protocol)
    if handshake is None:
        return (False, 0.0)
    # Use one stable DC for screening. Picking a different random DC on every
    # pass made a proxy succeed during harvest and get deleted during assign.
    dst = target or TELEGRAM_DCS[0]
    diag = get_proxy_diagnostics()
    diag.trace_connect(ip, port, protocol)
    started = time.monotonic()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        import inspect
        if "auth" in inspect.signature(handshake).parameters:
            ok = await handshake(reader, writer, dst, timeout, auth)
        else:
            ok = await handshake(reader, writer, dst, timeout)
        latency = max(round(time.monotonic() - started, 3), 0.0) if ok else 0.0
        if not ok:
            diag.trace_handshake(ip, port, protocol, False, latency)
            return (False, 0.0)
        # Never report 0.0 on success: callers use latency as a quality signal and
        # a zero there is indistinguishable from a failure.
        actual_latency = max(round(time.monotonic() - started, 3), 0.001)
        diag.trace_handshake(ip, port, protocol, True, actual_latency)
        return (True, actual_latency)
    except asyncio.TimeoutError:
        diag.trace_timeout(ip, port, protocol, timeout)
        return (False, 0.0)
    except (OSError, asyncio.IncompleteReadError, struct.error) as exc:
        diag.trace_error(ip, port, protocol, exc)
        return (False, 0.0)
    except Exception as exc:
        diag.trace_error(ip, port, protocol, exc)
        logger.debug("Probe error %s:%s (%s): %s", ip, port, protocol, exc)
        return (False, 0.0)
    finally:
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass


async def validate_proxy(
    ip: str,
    port: int,
    protocol: str,
    timeout: float = VALIDATE_TIMEOUT,
    auth: tuple[str, str] | None = None,
) -> tuple[bool, float]:
    """Verify a proxy with a real, unencrypted Telegram MTProto exchange.

    A successful CONNECT/SOCKS reply is not enough: many public proxies accept
    the tunnel and then discard its payload.  req_pq_multi is the first genuine
    Telegram authorization-key request and needs no account credentials.
    """
    handshake = _HANDSHAKES.get(protocol)
    if handshake is None:
        return (False, 0.0)

    dst = TELEGRAM_DCS[0]
    started = time.monotonic()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        if "auth" in inspect.signature(handshake).parameters:
            connected = await handshake(reader, writer, dst, timeout, auth)
        else:
            connected = await handshake(reader, writer, dst, timeout)
        if not connected:
            return (False, 0.0)

        # MTProto abridged transport + unencrypted req_pq_multi.
        nonce = secrets.token_bytes(16)
        body = struct.pack("<I", 0xBE7E8EF1) + nonce
        message_id = (int(time.time() * (1 << 32)) // 4) * 4
        envelope = b"\x00" * 8 + struct.pack("<Q", message_id) + struct.pack("<I", len(body)) + body
        writer.write(b"\xef" + bytes((len(envelope) // 4,)) + envelope)
        await writer.drain()

        length_byte = await asyncio.wait_for(reader.readexactly(1), timeout=timeout)
        if length_byte[0] == 0x7F:
            words = int.from_bytes(
                await asyncio.wait_for(reader.readexactly(3), timeout=timeout), "little"
            )
        else:
            words = length_byte[0]
        if words <= 0 or words > 1024:
            return (False, 0.0)
        response = await asyncio.wait_for(reader.readexactly(words * 4), timeout=timeout)
        if len(response) < 40 or response[:8] != b"\x00" * 8:
            return (False, 0.0)
        body_len = struct.unpack_from("<I", response, 16)[0]
        response_body = response[20:20 + body_len]
        if len(response_body) < 20:
            return (False, 0.0)
        constructor = struct.unpack_from("<I", response_body, 0)[0]
        if constructor != 0x05162463 or response_body[4:20] != nonce:
            return (False, 0.0)
        return (True, max(round(time.monotonic() - started, 3), 0.001))
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, struct.error):
        return (False, 0.0)
    finally:
        if writer is not None:
            writer.close()


async def tcp_check(ip: str, port: int, timeout: float = 3.0) -> tuple[bool, float]:
    started = time.monotonic()
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
        return (True, round(time.monotonic() - started, 3))
    except (OSError, asyncio.TimeoutError):
        return (False, 0.0)
    finally:
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

class ProxyPool:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = asyncio.Lock()
        self._init_db()

    # -- sync core, always called through asyncio.to_thread from async methods --

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proxies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    protocol TEXT NOT NULL DEFAULT 'socks5',
                    latency REAL DEFAULT 0,
                    last_checked TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    UNIQUE(ip, port, protocol)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proxy_blacklist (
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    protocol TEXT NOT NULL,
                    added_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(ip, port, protocol)
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(proxies)")}
            if "fails" not in columns:
                conn.execute("ALTER TABLE proxies ADD COLUMN fails INTEGER NOT NULL DEFAULT 0")
            if "slow_pings" not in columns:
                conn.execute("ALTER TABLE proxies ADD COLUMN slow_pings INTEGER NOT NULL DEFAULT 0")
            if "normal_pings" not in columns:
                conn.execute("ALTER TABLE proxies ADD COLUMN normal_pings INTEGER NOT NULL DEFAULT 0")
            if "username" not in columns:
                conn.execute("ALTER TABLE proxies ADD COLUMN username TEXT")
            if "password" not in columns:
                conn.execute("ALTER TABLE proxies ADD COLUMN password TEXT")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS proxy_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS archived_proxies (
                    ip TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    protocol TEXT NOT NULL,
                    latency REAL DEFAULT 0,
                    last_checked TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    username TEXT,
                    password TEXT,
                    archived_at TEXT NOT NULL,
                    archive_reason TEXT NOT NULL
                )
            """)
            blacklist_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(proxy_blacklist)")
            }
            if "reason" not in blacklist_columns:
                conn.execute("ALTER TABLE proxy_blacklist ADD COLUMN reason TEXT NOT NULL DEFAULT 'runtime'")
            if "expires_at" not in blacklist_columns:
                conn.execute("ALTER TABLE proxy_blacklist ADD COLUMN expires_at TEXT NOT NULL DEFAULT ''")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS proxy_sources (
                    url TEXT PRIMARY KEY,
                    etag TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    last_checked TEXT NOT NULL DEFAULT '',
                    last_changed TEXT NOT NULL DEFAULT '',
                    next_check TEXT NOT NULL DEFAULT '',
                    status INTEGER NOT NULL DEFAULT 0,
                    unchanged_runs INTEGER NOT NULL DEFAULT 0,
                    error_runs INTEGER NOT NULL DEFAULT 0,
                    fetched_total INTEGER NOT NULL DEFAULT 0,
                    live_total INTEGER NOT NULL DEFAULT 0,
                    dead_total INTEGER NOT NULL DEFAULT 0
                )
            """)
            long_dead_marker = conn.execute(
                "SELECT value FROM proxy_meta WHERE key='long_handshake_dead_v1'"
            ).fetchone()
            if long_dead_marker is None:
                migrated = []
                for ip, port, proto, added_at in conn.execute(
                    "SELECT ip,port,protocol,added_at FROM proxy_blacklist"
                ):
                    added = _parse_ts(added_at) or datetime.now(timezone.utc)
                    if added.tzinfo is None:
                        added = added.replace(tzinfo=timezone.utc)
                    migrated.append((
                        "legacy_dead", (added + timedelta(days=HANDSHAKE_BLACKLIST_DAYS)).isoformat(),
                        ip, port, proto,
                    ))
                if migrated:
                    conn.executemany(
                        "UPDATE proxy_blacklist SET reason=?,expires_at=?"
                        " WHERE ip=? AND port=? AND protocol=?",
                        migrated,
                    )
                conn.execute(
                    "INSERT INTO proxy_meta (key,value) VALUES ('long_handshake_dead_v1', ?)",
                    (utc_now(),),
                )
            mtproto_marker = conn.execute(
                "SELECT value FROM proxy_meta WHERE key='mtproto_validation_v1'"
            ).fetchone()
            if mtproto_marker is None:
                # Entries created by older builds only passed CONNECT/SOCKS
                # negotiation and must never be handed to an account as trusted.
                conn.execute("DELETE FROM proxies")
                conn.execute(
                    "INSERT INTO proxy_meta (key, value) VALUES ('mtproto_validation_v1', ?)",
                    (utc_now(),),
                )
            stable_dc_marker = conn.execute(
                "SELECT value FROM proxy_meta WHERE key='stable_dc2_validation_v1'"
            ).fetchone()
            if stable_dc_marker is None:
                # Clear false negatives produced by the former random-DC
                # validator so they can be harvested again immediately.
                conn.execute("DELETE FROM proxies")
                conn.execute("DELETE FROM proxy_blacklist")
                conn.execute(
                    "INSERT INTO proxy_meta (key, value) VALUES ('stable_dc2_validation_v1', ?)",
                    (utc_now(),),
                )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON proxies(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_protocol ON proxies(protocol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pick ON proxies(status, protocol, latency)")
            conn.commit()

    def _run(self, sql: str, params: Any = (), many: bool = False) -> None:
        try:
            with closing(self._connect()) as conn:
                if many:
                    conn.executemany(sql, params)
                else:
                    conn.execute(sql, params)
                conn.commit()
        except sqlite3.Error as exc:
            logger.debug("DB write error: %s", exc)

    def _query(self, sql: str, params: Any = ()) -> list[tuple]:
        try:
            with closing(self._connect()) as conn:
                return conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            logger.debug("DB read error: %s", exc)
            return []

    # -- async API --

    async def add(
        self,
        ip: str,
        port: int,
        protocol: str,
        username: str | None = None,
        password: str | None = None,
    ) -> bool:
        await asyncio.to_thread(
            self._run,
            "INSERT INTO proxies (ip, port, protocol, status, last_checked, username, password)"
            " VALUES (?, ?, ?, 'active', ?, ?, ?)"
            " ON CONFLICT(ip, port, protocol) DO UPDATE SET"
            " status='active', last_checked=excluded.last_checked, fails=0,"
            " username=excluded.username, password=excluded.password",
            (ip, port, protocol, utc_now(), username, password),
        )
        return True

    async def store_results(self, rows: list[tuple[str, int, str, bool, float]]) -> None:
        """Persist a whole validation batch in one transaction."""
        if not rows:
            return
        stamp = utc_now()
        good = [
            (ip, port, proto, lat, stamp, 1 if lat > SLOW_LATENCY else 0, 0 if lat > SLOW_LATENCY else 1)
            for ip, port, proto, ok, lat in rows
            if ok
        ]
        # Failed candidates belong in the temporary blacklist, not in the usable
        # proxy table. Keeping thousands of dead rows made pool totals misleading.
        bad = [(ip, port, proto, stamp) for ip, port, proto, ok, _ in rows if not ok]
        async with self._lock:
            if good:
                await asyncio.to_thread(
                    self._run,
                    "INSERT INTO proxies (ip, port, protocol, latency, last_checked, slow_pings, normal_pings, status, fails)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0)"
                    " ON CONFLICT(ip, port, protocol) DO UPDATE SET"
                    " latency=excluded.latency, last_checked=excluded.last_checked,"
                    " slow_pings = CASE WHEN excluded.slow_pings > 0 THEN proxies.slow_pings + 1 ELSE 0 END,"
                    " normal_pings = CASE WHEN excluded.normal_pings > 0 THEN proxies.normal_pings + 1 ELSE 0 END,"
                    " status = CASE "
                    "   WHEN proxies.status = 'active' AND (CASE WHEN excluded.slow_pings > 0 THEN proxies.slow_pings + 1 ELSE 0 END) >= 3 THEN 'secondary'"
                    "   WHEN proxies.status = 'secondary' AND (CASE WHEN excluded.normal_pings > 0 THEN proxies.normal_pings + 1 ELSE 0 END) >= 3 THEN 'active'"
                    "   WHEN proxies.status NOT IN ('active', 'secondary') THEN 'active'"
                    "   ELSE proxies.status"
                    " END,"
                    " fails=0",
                    good,
                    True,
                )
            if bad:
                await asyncio.to_thread(
                    self._run,
                    "INSERT INTO proxy_blacklist (ip, port, protocol, added_at) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(ip, port, protocol) DO UPDATE SET added_at=excluded.added_at",
                    bad,
                    True,
                )
                await asyncio.to_thread(
                    self._run,
                    "DELETE FROM proxies WHERE ip = ? AND port = ? AND protocol = ?",
                    [(ip, port, proto) for ip, port, proto, _stamp in bad],
                    True,
                )

    async def store_gateway_results(
        self, rows: list[tuple[str, int, str, bool, float]],
    ) -> None:
        """Persist every configured Xray exit, including temporarily failed ones.

        Gateway endpoints are a fixed inventory supplied by Xray. A failed
        health sample makes an exit unavailable for new assignments, but does
        not remove or blacklist it; the gateway monitor keeps probing it and a
        later successful sample revives the same row.
        """
        if not rows:
            return
        stamp = utc_now()
        good = [
            (ip, port, proto, lat, stamp, 1 if lat > SLOW_LATENCY else 0, 0 if lat > SLOW_LATENCY else 1)
            for ip, port, proto, ok, lat in rows
            if ok
        ]
        recovering = [
            (ip, port, proto, stamp)
            for ip, port, proto, ok, _latency in rows
            if not ok
        ]
        endpoints = [(ip, port, proto) for ip, port, proto, _ok, _latency in rows]
        async with self._lock:
            existing_gateway = await asyncio.to_thread(
                self._query,
                "SELECT ip, port, protocol FROM proxies"
                " WHERE ip IN ('127.0.0.1', '::1') AND port BETWEEN ? AND ?",
                (GATEWAY_PORT_MIN, GATEWAY_PORT_MAX),
            )
            current = set(endpoints)
            retired = [entry for entry in existing_gateway if entry not in current]
            if retired:
                await asyncio.to_thread(
                    self._run,
                    "UPDATE proxies SET status='retired'"
                    " WHERE ip = ? AND port = ? AND protocol = ?",
                    retired,
                    True,
                )
            if good:
                await asyncio.to_thread(
                    self._run,
                    "INSERT INTO proxies (ip, port, protocol, latency, last_checked, slow_pings, normal_pings, status, fails)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0)"
                    " ON CONFLICT(ip, port, protocol) DO UPDATE SET"
                    " latency=excluded.latency, last_checked=excluded.last_checked,"
                    " slow_pings = CASE WHEN excluded.slow_pings > 0 THEN proxies.slow_pings + 1 ELSE 0 END,"
                    " normal_pings = CASE WHEN excluded.normal_pings > 0 THEN proxies.normal_pings + 1 ELSE 0 END,"
                    " status = CASE "
                    "   WHEN proxies.status = 'active' AND (CASE WHEN excluded.slow_pings > 0 THEN proxies.slow_pings + 1 ELSE 0 END) >= 3 THEN 'secondary'"
                    "   WHEN proxies.status = 'secondary' AND (CASE WHEN excluded.normal_pings > 0 THEN proxies.normal_pings + 1 ELSE 0 END) >= 3 THEN 'active'"
                    "   WHEN proxies.status NOT IN ('active', 'secondary') THEN 'active'"
                    "   ELSE proxies.status"
                    " END,"
                    " fails=0",
                    good,
                    True,
                )
            if recovering:
                await asyncio.to_thread(
                    self._run,
                    "INSERT INTO proxies (ip, port, protocol, latency, last_checked, status, fails)"
                    " VALUES (?, ?, ?, 0, ?, 'recovering', 1)"
                    " ON CONFLICT(ip, port, protocol) DO UPDATE SET"
                    " last_checked=excluded.last_checked, status='recovering',"
                    " fails=proxies.fails+1",
                    recovering,
                    True,
                )
            await asyncio.to_thread(
                self._run,
                "DELETE FROM proxy_blacklist WHERE ip = ? AND port = ? AND protocol = ?",
                endpoints,
                True,
            )

    async def archive_non_gateway_entries(self) -> int:
        """Move historical public-proxy rows out of the live pool.

        VPN mode must never silently fall back to an old harvested endpoint.
        Keeping the rows in SQLite makes the switch reversible without keeping
        the legacy system active.
        """
        stamp = utc_now()
        async with self._lock:
            before = await asyncio.to_thread(
                self._query,
                "SELECT COUNT(*) FROM proxies WHERE NOT (ip IN ('127.0.0.1','::1') AND port BETWEEN 21000 AND 21999)",
            )
            await asyncio.to_thread(
                self._run,
                "INSERT INTO archived_proxies (ip,port,protocol,latency,last_checked,status,username,password,archived_at,archive_reason) "
                "SELECT ip,port,protocol,latency,last_checked,status,username,password,?, 'vpn_gateway_migration' "
                "FROM proxies WHERE NOT (ip IN ('127.0.0.1','::1') AND port BETWEEN 21000 AND 21999)",
                (stamp,),
            )
            await asyncio.to_thread(
                self._run,
                "DELETE FROM proxies WHERE NOT (ip IN ('127.0.0.1','::1') AND port BETWEEN 21000 AND 21999)",
            )
        return int(before[0][0]) if before else 0

    async def update_status(self, ip: str, port: int, protocol: str, status: str, latency: float) -> None:
        await asyncio.to_thread(
            self._run,
            "UPDATE proxies SET status = ?, latency = ?, last_checked = ? WHERE ip = ? AND port = ? AND protocol = ?",
            (status, latency, utc_now(), ip, port, protocol),
        )

    async def record_failure(self, ip: str, port: int, protocol: str) -> None:
        """Count a runtime failure; retire the proxy only once it keeps failing."""
        await asyncio.to_thread(
            self._run,
            "UPDATE proxies SET fails = fails + 1, last_checked = ?,"
            " status = CASE WHEN fails + 1 >= ? THEN 'dead' ELSE status END"
            " WHERE ip = ? AND port = ? AND protocol = ?",
            (utc_now(), MAX_FAILS, ip, port, protocol),
        )

    async def record_gateway_failure(self, ip: str, port: int, protocol: str) -> None:
        """Keep a fixed Xray exit in the inventory while it recovers."""
        await asyncio.to_thread(
            self._run,
            "UPDATE proxies SET fails = fails + 1, last_checked = ?, status = 'recovering'"
            " WHERE ip = ? AND port = ? AND protocol = ?",
            (utc_now(), ip, port, protocol),
        )

    async def record_success(self, ip: str, port: int, protocol: str, latency: float) -> None:
        is_slow = 1 if latency > SLOW_LATENCY else 0
        is_normal = 0 if latency > SLOW_LATENCY else 1
        
        sql = """
            UPDATE proxies SET 
                fails = 0, 
                latency = ?, 
                last_checked = ?,
                slow_pings = CASE WHEN ? THEN slow_pings + 1 ELSE 0 END,
                normal_pings = CASE WHEN ? THEN normal_pings + 1 ELSE 0 END,
                status = CASE 
                    WHEN status = 'active' AND (CASE WHEN ? THEN slow_pings + 1 ELSE 0 END) >= 3 THEN 'secondary'
                    WHEN status = 'secondary' AND (CASE WHEN ? THEN normal_pings + 1 ELSE 0 END) >= 3 THEN 'active'
                    WHEN status NOT IN ('active', 'secondary') THEN 'active'
                    ELSE status
                END
            WHERE ip = ? AND port = ? AND protocol = ?
        """
        await asyncio.to_thread(
            self._run, sql,
            (latency, utc_now(), is_slow, is_normal, is_slow, is_normal, ip, port, protocol)
        )

    async def is_blacklisted(self, ip: str, port: int, protocol: str) -> bool:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT added_at,expires_at FROM proxy_blacklist WHERE ip = ? AND port = ? AND protocol = ?",
            (ip, port, protocol),
        )
        if not rows:
            return False
        expires = _parse_ts(rows[0][1])
        if expires is not None:
            return expires >= datetime.now(timezone.utc)
        added = _parse_ts(rows[0][0])
        if added is None:
            return True
        return datetime.now(timezone.utc) - added < timedelta(hours=BLACKLIST_TTL_HOURS)

    async def add_to_blacklist(
        self, ip: str, port: int, protocol: str, *, reason: str = "runtime",
        hours: float = BLACKLIST_TTL_HOURS,
    ) -> None:
        if is_gateway_endpoint(ip, port):
            return
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(hours=hours)).isoformat()
        await asyncio.to_thread(
            self._run,
            "INSERT INTO proxy_blacklist (ip, port, protocol, added_at, reason, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(ip, port, protocol) DO UPDATE SET"
            " added_at=excluded.added_at, reason=excluded.reason, expires_at=excluded.expires_at",
            (ip, port, protocol, now.isoformat(), reason, expires),
        )

    async def add_many_to_blacklist(
        self, entries: list[tuple[str, int, str]], *, reason: str, hours: float,
    ) -> None:
        if not entries:
            return
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(hours=hours)).isoformat()
        rows = [(ip, port, proto, now.isoformat(), reason, expires) for ip, port, proto in entries]
        await asyncio.to_thread(
            self._run,
            "INSERT INTO proxy_blacklist (ip, port, protocol, added_at, reason, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(ip, port, protocol) DO UPDATE SET"
            " added_at=excluded.added_at, reason=excluded.reason, expires_at=excluded.expires_at",
            rows, True,
        )

    async def remove_dead_from_pool(self, ip: str, port: int, protocol: str) -> None:
        if is_gateway_endpoint(ip, port):
            await self.record_gateway_failure(ip, port, protocol)
            return
        async with self._lock:
            await asyncio.to_thread(
                self._run,
                "DELETE FROM proxies WHERE ip = ? AND port = ? AND protocol = ?",
                (ip, port, protocol),
            )

    async def mark_dead(self, proxy: ProxyEntry) -> None:
        await self.update_status(proxy.ip, proxy.port, proxy.protocol, "dead", proxy.latency)

    async def candidates(self, protocol: str | None = None, limit: int = 12) -> list[ProxyEntry]:
        """Usable proxies, best tier first, randomised only within a tier.

        Shuffling matters: without it every account grabs the same top entry and
        they all end up behind one IP.
        """
        query = (
            "SELECT ip, port, protocol, latency, last_checked, status, username, password FROM proxies"
            " WHERE status IN ('active', 'secondary')"
        )
        params: list[Any] = []
        if os.getenv("PROXY_MODE", "").strip().lower() == "vpn_gateway":
            # Test mode: only locally bound Xray exits are eligible.  This also
            # guarantees that a gateway outage can never fall back to the PC's
            # own network or to the harvested public-proxy pool.
            query += " AND ip IN ('127.0.0.1', '::1') AND port BETWEEN 21000 AND 21999"
        if protocol:
            query += " AND protocol = ?"
            params.append(protocol)
        query += (
            " ORDER BY CASE WHEN status='active' THEN 0 ELSE 1 END,"
            " CASE WHEN protocol='socks5' THEN 0 ELSE 1 END, latency ASC LIMIT ?"
        )
        params.append(max(limit, 1) * 4)
        rows = await asyncio.to_thread(self._query, query, tuple(params))
        active, secondary = [], []
        for r in rows:
            entry = ProxyEntry(
                ip=r[0], port=r[1], protocol=r[2], latency=r[3], last_checked=r[4],
                status=r[5], username=r[6], password=r[7],
            )
            (active if entry.status == "active" else secondary).append(entry)
        random.shuffle(active)
        random.shuffle(secondary)
        return (active + secondary)[:limit]

    async def get_random_proxy(self, protocol: str | None = None) -> ProxyEntry | None:
        entries = await self.candidates(protocol=protocol, limit=1)
        return entries[0] if entries else None

    async def acquire(
        self,
        protocol: str | None = None,
        attempts: int = 12,
        verify: bool = True,
        exclude_urls: set[str] | None = None,
    ) -> ProxyEntry | None:
        """Hand out a proxy that has just been confirmed to reach Telegram.

        Anything that fails is counted against the entry, so a pool full of stale
        junk drains itself instead of handing the same corpse to the next caller.
        """
        pool = await self.candidates(protocol=protocol, limit=max(attempts * 2, attempts))
        if not pool and protocol:
            pool = await self.candidates(protocol=None, limit=max(attempts * 2, attempts))
        excluded = exclude_urls or set()
        pool = [entry for entry in pool if entry.url not in excluded][:attempts]
        diag = get_proxy_diagnostics()
        for entry in pool:
            if not verify:
                return entry
            diag.trace_connect(entry.ip, entry.port, entry.protocol, tag="pool_acquire")
            ok, latency = await validate_proxy(
                entry.ip, entry.port, entry.protocol, auth=entry.auth
            )
            if ok:
                await self.record_success(entry.ip, entry.port, entry.protocol, latency)
                entry.latency = latency
                entry.status = "active" if latency <= SLOW_LATENCY else "secondary"
                return entry
            await self.add_to_blacklist(entry.ip, entry.port, entry.protocol)
            await self.remove_dead_from_pool(entry.ip, entry.port, entry.protocol)
        logger.warning("Pool acquire exhausted %d candidates (protocol=%s)", len(pool), protocol)
        return None

    async def recently_dead(self) -> set[tuple[str, int, str]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=DEAD_RETRY_HOURS)).isoformat()
        rows = await asyncio.to_thread(
            self._query,
            "SELECT ip, port, protocol FROM proxies WHERE status='dead' AND last_checked >= ?",
            (cutoff,),
        )
        return {(r[0], r[1], r[2]) for r in rows}

    async def count(self, status: str = "active") -> int:
        rows = await asyncio.to_thread(
            self._query, "SELECT COUNT(*) FROM proxies WHERE status = ?", (status,)
        )
        return rows[0][0] if rows else 0

    async def count_all(self) -> int:
        """Count the current inventory, excluding preserved obsolete gateway rows."""
        rows = await asyncio.to_thread(
            self._query, "SELECT COUNT(*) FROM proxies WHERE status != 'retired'",
        )
        return rows[0][0] if rows else 0

    async def clear_all(self) -> int:
        async with self._lock:
            await asyncio.to_thread(self._run, "DELETE FROM proxies", ())
        return 0

    async def stats(self) -> dict[str, int]:
        rows = await asyncio.to_thread(
            self._query, "SELECT protocol, status, COUNT(*) FROM proxies GROUP BY protocol, status"
        )
        return {f"{r[0]}_{r[1]}": r[2] for r in rows}

    async def prune(self) -> None:
        """Drop stale junk so the DB does not grow without bound."""
        now = utc_now()
        black_cutoff = (datetime.now(timezone.utc) - timedelta(hours=BLACKLIST_TTL_HOURS)).isoformat()
        async with self._lock:
            await asyncio.to_thread(
                self._run, "DELETE FROM proxies WHERE status='dead'", ()
            )
            await asyncio.to_thread(
                self._run,
                "DELETE FROM proxy_blacklist WHERE (expires_at != '' AND expires_at < ?)"
                " OR (expires_at = '' AND added_at < ?)",
                (now, black_cutoff),
            )

    async def blacklist_set(self) -> set[tuple[str, int, str]]:
        now = utc_now()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=BLACKLIST_TTL_HOURS)).isoformat()
        rows = await asyncio.to_thread(
            self._query,
            "SELECT ip, port, protocol FROM proxy_blacklist WHERE"
            " (expires_at != '' AND expires_at >= ?) OR (expires_at = '' AND added_at >= ?)",
            (now, cutoff),
        )
        return {(r[0], r[1], r[2]) for r in rows}

    async def source_state(self, url: str) -> dict[str, Any]:
        rows = await asyncio.to_thread(
            self._query,
            "SELECT etag,last_modified,content_hash,last_checked,last_changed,next_check,status,"
            " unchanged_runs,error_runs,fetched_total,live_total,dead_total"
            " FROM proxy_sources WHERE url=?", (url,),
        )
        keys = ("etag", "last_modified", "content_hash", "last_checked", "last_changed",
                "next_check", "status", "unchanged_runs", "error_runs", "fetched_total",
                "live_total", "dead_total")
        return dict(zip(keys, rows[0])) if rows else {}

    async def save_source_state(self, url: str, state: dict[str, Any]) -> None:
        await asyncio.to_thread(
            self._run,
            "INSERT INTO proxy_sources (url,etag,last_modified,content_hash,last_checked,last_changed,"
            " next_check,status,unchanged_runs,error_runs,fetched_total,live_total,dead_total)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET"
            " etag=excluded.etag,last_modified=excluded.last_modified,content_hash=excluded.content_hash,"
            " last_checked=excluded.last_checked,last_changed=excluded.last_changed,"
            " next_check=excluded.next_check,status=excluded.status,unchanged_runs=excluded.unchanged_runs,"
            " error_runs=excluded.error_runs,fetched_total=excluded.fetched_total,"
            " live_total=excluded.live_total,dead_total=excluded.dead_total",
            (url, state.get("etag", ""), state.get("last_modified", ""), state.get("content_hash", ""),
             state.get("last_checked", ""), state.get("last_changed", ""), state.get("next_check", ""),
             int(state.get("status", 0)), int(state.get("unchanged_runs", 0)),
             int(state.get("error_runs", 0)), int(state.get("fetched_total", 0)),
             int(state.get("live_total", 0)), int(state.get("dead_total", 0))),
        )

    async def validate_and_store(self, ip: str, port: int, protocol: str, max_concurrency: int = 50) -> bool:
        ok, latency = await validate_proxy(ip, port, protocol)
        await self.store_results([(ip, port, protocol, ok, latency)])
        return ok


# ---------------------------------------------------------------------------
# Harvesting
# ---------------------------------------------------------------------------

def parse_proxy_line(line: str, default_protocol: str) -> tuple[str, int, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    line_lower = line.lower()
    protocol = default_protocol
    if line_lower.startswith("tg://proxy"):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(line)
        params = parse_qs(parsed.query)
        server = params.get("server", [None])[0]
        port_str = params.get("port", [None])[0]
        if server and port_str:
            try:
                port = int(port_str)
                if 0 < port < 65536:
                    return (server, port, "socks5")
            except (ValueError, TypeError):
                pass
        return None
    if line_lower.startswith("mtproto://"):
        protocol = "socks5"
        line = line.split("://", 1)[1]
        if "@" in line:
            line = line.split("@", 1)[1]
    elif line_lower.startswith("socks5h://"):
        protocol = "socks5"
        line = line.split("://", 1)[1]
    elif line_lower.startswith("socks5://"):
        protocol = "socks5"
        line = line.split("://", 1)[1]
    elif line_lower.startswith(("socks4://", "http://", "https://")):
        protocol = line_lower.split("://")[0]
        if protocol == "https":
            protocol = "http"
        line = line.split("://", 1)[1]
    if "@" in line:
        line = line.split("@", 1)[1]
    match = PROXY_REGEX.search(line)
    if not match:
        return None
    ip = _normalize_ipv4(match.group(1))
    if ip is None:
        return None
    try:
        port = int(match.group(2))
    except (ValueError, IndexError):
        return None
    if port < 1 or port > 65535:
        return None
    if protocol == default_protocol and "://" not in line_lower:
        socks_ports = {1080, 1081, 4145, 4153, 9050, 9150, 7890, 7891, 5678, 65432}
        http_ports = {80, 443, 3128, 3129, 8080, 8000, 8888, 9090, 8118, 8081, 8889}
        if port in socks_ports:
            protocol = "socks5"
        elif port in http_ports:
            protocol = "http"
    return (ip, port, protocol)


def _entry_protocol(entry: dict, default_proto: str) -> str:
    """Resolve the protocol of one JSON proxy entry.

    geonode reports `protocols: ["socks5"]` (a list), other APIs use a plain
    `protocol` string. Reading only the singular key made every geonode entry
    fall back to the protocol guessed from the URL, which silently mislabels
    any list that mixes protocols.
    """
    raw = entry.get("protocols")
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    elif raw is None:
        raw = entry.get("protocol")
    value = str(raw or "").strip().lower()
    if value.startswith("socks"):
        return "socks5" if "5" in value else "socks4"
    if value in ("http", "https"):
        return "http"
    return default_proto


def _entry_protocols(entry: dict, default_proto: str) -> list[str]:
    raw = entry.get("protocols")
    values = raw if isinstance(raw, (list, tuple)) else [entry.get("protocol") or default_proto]
    protocols: list[str] = []
    for value in values:
        normalized = str(value or "").strip().lower()
        if normalized.startswith("socks"):
            protocol = "socks5" if "5" in normalized else "socks4"
        elif normalized in {"http", "https"}:
            protocol = "http"
        else:
            continue
        if protocol not in protocols:
            protocols.append(protocol)
    return protocols or [default_proto]


def _parse_json_response(
    text: str,
    default_proto: str,
    limits: dict,
    seen: set,
    results: list,
    counts: dict[str, int],
    blacklist: set | None = None,
) -> None:
    import json

    data = json.loads(text)
    entries = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ip = _normalize_ipv4(str(entry.get("ip") or entry.get("host") or entry.get("hostname") or ""))
        port = entry.get("port", 0)
        if not ip or not port:
            continue
        try:
            port = int(port)
        except (ValueError, TypeError):
            continue
        if port < 1 or port > 65535:
            continue
        for protocol in _entry_protocols(entry, default_proto):
            key = (ip, port, protocol)
            if key in seen:
                continue
            current = counts.get(protocol, 0)
            if current >= limits.get(protocol, 1000):
                continue
            if blacklist and key in blacklist:
                continue
            seen.add(key)
            results.append(key)
            counts[protocol] = current + 1


PER_SOURCE_CAP = 3000


def _is_geonode_source(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host == "geonode.com" or host.endswith(".geonode.com")


async def harvest_sources(
    sources: list[str], pool: ProxyPool | None = None, timeout: float = 12.0
) -> tuple[list[tuple[str, int, str]], dict[str, dict], dict[tuple[str, int, str], set[str]]]:
    results: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, str]] = set()
    counts: dict[str, int] = {}
    limits = {"http": 20000, "socks4": 20000, "socks5": 20000}
    source_stats: dict[str, dict] = {}
    provenance: dict[tuple[str, int, str], set[str]] = {}
    blacklist: set[tuple[str, int, str]] = set()
    if pool:
        blacklist = await pool.blacklist_set() | await pool.recently_dead()

    async def fetch_one(client: httpx.AsyncClient, url: str) -> None:
        stat = source_stats.setdefault(
            url, {"harvested": 0, "valid": 0, "dead": 0, "status": 0, "skipped": False}
        )
        state = await pool.source_state(url) if pool else {}
        stat["_state"] = state
        now = datetime.now(timezone.utc)
        next_check = _parse_ts(state.get("next_check", ""))
        if next_check and next_check > now:
            stat["status"] = 304
            stat["skipped"] = True
            return
        headers: dict[str, str] = {}
        if state.get("etag"):
            headers["If-None-Match"] = str(state["etag"])
        if state.get("last_modified"):
            headers["If-Modified-Since"] = str(state["last_modified"])
        try:
            checked = utc_now()
            if url.startswith("file://"):
                path = Path(url[7:])
                if not path.is_file():
                    stat["status"] = 404
                    state.update({
                        "last_checked": checked,
                        "next_check": (now + timedelta(minutes=60)).isoformat(),
                        "status": 404, "error_runs": int(state.get("error_runs", 0)) + 1,
                    })
                    return
                # Get modified time
                mtime = path.stat().st_mtime
                mtime_str = str(mtime)
                if state.get("etag") == mtime_str:
                    # Not modified
                    unchanged = int(state.get("unchanged_runs", 0)) + 1
                    delay = min(SOURCE_MIN_REFRESH_MINUTES * (2 ** min(unchanged, 6)), SOURCE_MAX_REFRESH_HOURS * 60)
                    state.update({
                        "last_checked": checked,
                        "next_check": (now + timedelta(minutes=delay)).isoformat(),
                        "status": 304, "unchanged_runs": unchanged, "error_runs": 0,
                    })
                    stat["status"] = 304
                    stat["skipped"] = True
                    return
                text = path.read_text(encoding="utf-8", errors="replace")
                response_content = text.encode("utf-8")
                response_headers = {"etag": mtime_str}
                stat["status"] = 200
            else:
                response = await client.get(url, timeout=timeout, headers=headers)
                stat["status"] = response.status_code
                if response.status_code == 304:
                    unchanged = int(state.get("unchanged_runs", 0)) + 1
                    delay = min(
                        SOURCE_MIN_REFRESH_MINUTES * (2 ** min(unchanged, 6)),
                        SOURCE_MAX_REFRESH_HOURS * 60,
                    )
                    state.update({
                        "last_checked": checked,
                        "next_check": (now + timedelta(minutes=delay)).isoformat(),
                        "status": 304, "unchanged_runs": unchanged, "error_runs": 0,
                    })
                    stat["skipped"] = True
                    return
                if response.status_code != 200:
                    errors = int(state.get("error_runs", 0)) + 1
                    delay = min(30 * (2 ** min(errors, 5)), SOURCE_MAX_REFRESH_HOURS * 60)
                    state.update({
                        "last_checked": checked,
                        "next_check": (now + timedelta(minutes=delay)).isoformat(),
                        "status": response.status_code, "error_runs": errors,
                    })
                    return
                text = response.text
                response_content = response.content
                response_headers = response.headers

            content_hash = hashlib.sha256(response_content).hexdigest()
            unchanged_content = content_hash == state.get("content_hash")
            unchanged = int(state.get("unchanged_runs", 0)) + 1 if unchanged_content else 0
            delay = min(
                SOURCE_MIN_REFRESH_MINUTES * (2 ** min(unchanged, 6)),
                SOURCE_MAX_REFRESH_HOURS * 60,
            )
            state.update({
                "etag": response_headers.get("etag", state.get("etag", "")),                "last_modified": response.headers.get("last-modified", state.get("last_modified", "")),
                "content_hash": content_hash, "last_checked": checked,
                "last_changed": state.get("last_changed", "") if unchanged_content else checked,
                "next_check": (now + timedelta(minutes=delay)).isoformat(),
                "status": 200, "unchanged_runs": unchanged, "error_runs": 0,
            })
            if unchanged_content:
                stat["status"] = 304
                stat["skipped"] = True
                return
            url_lower = url.lower()
            if "socks5" in url_lower or "socks5h" in url_lower:
                default_proto = "socks5"
            elif "socks4" in url_lower:
                default_proto = "socks4"
            elif "mtproto" in url_lower:
                default_proto = "socks5"
            else:
                default_proto = "http"
            before = len(results)
            content_type = response.headers.get("content-type", "")
            if _is_geonode_source(url) or content_type.startswith("application/json"):
                _parse_json_response(text, default_proto, limits, seen, results, counts, blacklist)
                for item in results[before:]:
                    provenance.setdefault(item, set()).add(url)
            else:
                lines = text.splitlines()
                # Big aggregator files are stale at the head; sample across the whole
                # file so a cycle is not spent re-probing the same dead prefix.
                if len(lines) > PER_SOURCE_CAP:
                    lines = random.sample(lines, PER_SOURCE_CAP)
                for line in lines:
                    parsed = parse_proxy_line(line, default_proto)
                    if parsed is None or parsed in seen or parsed in blacklist:
                        continue
                    if counts.get(parsed[2], 0) >= limits.get(parsed[2], 1000):
                        continue
                    seen.add(parsed)
                    results.append(parsed)
                    provenance.setdefault(parsed, set()).add(url)
                    counts[parsed[2]] = counts.get(parsed[2], 0) + 1
            stat["harvested"] = len(results) - before
            state["fetched_total"] = int(state.get("fetched_total", 0)) + stat["harvested"]
        except Exception as exc:
            errors = int(state.get("error_runs", 0)) + 1
            state.update({
                "last_checked": utc_now(),
                "next_check": (now + timedelta(minutes=min(30 * (2 ** min(errors, 5)), 720))).isoformat(),
                "status": 0, "error_runs": errors,
            })
            logger.debug("Harvest error %s: %s", url, exc)

    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, headers={"user-agent": "Mozilla/5.0"}
    ) as client:
        for i in range(0, len(sources), 20):
            chunk = sources[i : i + 20]
            await asyncio.gather(*[fetch_one(client, url) for url in chunk], return_exceptions=True)

    return results, source_stats, provenance


def _silence_proactor_reset_noise() -> None:
    """Stop Windows socket resets from being logged as unhandled asyncio errors.

    Probing thousands of dead proxies means thousands of peers resetting the
    connection. On the Proactor loop the reset surfaces inside
    _call_connection_lost, i.e. in a callback we cannot wrap in try/except, and
    every one of them lands in the log as an ERROR with a traceback.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if getattr(loop, "_proxy_noise_filtered", False):
        return
    previous = loop.get_exception_handler()

    def handler(active_loop, context: dict) -> None:
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            logger.debug("Ignored socket reset during probing: %s", exc)
            return
        if previous is None:
            active_loop.default_exception_handler(context)
        else:
            previous(active_loop, context)

    loop.set_exception_handler(handler)
    loop._proxy_noise_filtered = True


async def harvest_and_validate(pool: ProxyPool, sources: list[str], max_concurrency: int = 50) -> dict[str, int]:
    started = time.monotonic()
    _silence_proactor_reset_noise()
    if await network_is_intercepted():
        logger.warning(
            "This network accepts connections to reserved addresses that must be "
            "unreachable, so probe results cannot be trusted: every proxy will look "
            "alive or dead for reasons that have nothing to do with the proxy. "
            "Run the harvest from the VPS instead."
        )
    await pool.prune()
    logger.info("Harvesting %d sources...", len(sources))
    harvested, source_stats, provenance = await harvest_sources(sources, pool)

    # Load webshare proxies (pre-verified, no need to probe)
    webshare_proxies: list[tuple[str, int, str]] = []
    try:
        from modules.webshare_source import get_webshare_source
        ws = get_webshare_source()
        ws_proxies = await ws.fetch()
        for ip, port, proto, user, password in ws_proxies:
            await pool.add(ip, port, proto, username=user, password=password)
            webshare_proxies.append((ip, port, proto))
        if webshare_proxies:
            logger.info("Webshare: loaded %d pre-verified proxies", len(webshare_proxies))
    except Exception:
        logger.debug("Webshare source not available", exc_info=True)

    ok_sources = sum(1 for s in source_stats.values() if s.get("status") == 200 and s["harvested"] > 0)
    bad_sources = [u for u, s in source_stats.items() if s.get("status") not in (200, 0)]
    logger.info(
        "Harvested %d candidates from %d/%d sources (%d returned an error status)",
        len(harvested), ok_sources, len(sources), len(bad_sources),
    )

    random.shuffle(harvested)
    budget = harvested[:VALIDATE_BUDGET]
    if len(harvested) > VALIDATE_BUDGET:
        logger.info("Probing a random %d of %d candidates this cycle", len(budget), len(harvested))

    # Raw socket handshakes are cheap, so run far wider than the account-level limit.
    concurrency = max(max_concurrency, SCREEN_CONCURRENCY)
    probe_timeout = SCREEN_TIMEOUT
    if os.name == "nt":
        # Hundreds of simultaneous failed connects exhaust Windows' ephemeral
        # sockets and turn even working proxies into false timeouts.
        concurrency = min(concurrency, 128)
        probe_timeout = 6.0
        logger.info("Windows proxy screening: concurrency=%d timeout=%.1fs", concurrency, probe_timeout)
    semaphore = asyncio.Semaphore(concurrency)
    stats = {"harvested": len(harvested), "valid": 0, "dead": 0}
    proto_counts: dict[str, int] = {}
    batch: list[tuple[str, int, str, bool, float]] = []
    handshake_dead: list[tuple[str, int, str]] = []
    enough = asyncio.Event()

    async def flush() -> None:
        nonlocal batch
        if batch:
            pending, batch = batch, []
            await pool.store_results(pending)

    async def check_one(ip: str, port: int, protocol: str) -> None:
        if enough.is_set():
            return
        async with semaphore:
            if enough.is_set():
                return
            tunnel_ok, _ = await probe_proxy(ip, port, protocol, timeout=probe_timeout)
            if tunnel_ok:
                ok, latency = await validate_proxy(
                    ip, port, protocol, timeout=VALIDATE_TIMEOUT
                )
            else:
                ok, latency = False, 0.0
                handshake_dead.append((ip, port, protocol))
        for source_url in provenance.get((ip, port, protocol), ()):
            source_stats[source_url]["valid" if ok else "dead"] += 1
        batch.append((ip, port, protocol, ok, latency))
        if ok:
            stats["valid"] += 1
            proto_counts[protocol] = proto_counts.get(protocol, 0) + 1
            if stats["valid"] >= TARGET_ACTIVE:
                enough.set()
        else:
            stats["dead"] += 1
        if len(batch) >= 500:
            await flush()

    await asyncio.gather(*[check_one(*item) for item in budget], return_exceptions=True)
    await flush()
    await pool.add_many_to_blacklist(
        handshake_dead,
        reason="handshake_dead",
        hours=HANDSHAKE_BLACKLIST_DAYS * 24,
    )

    for url, stat in source_stats.items():
        state = stat.get("_state", {})
        state["live_total"] = int(state.get("live_total", 0)) + int(stat.get("valid", 0))
        state["dead_total"] = int(state.get("dead_total", 0)) + int(stat.get("dead", 0))
        await pool.save_source_state(url, state)

    elapsed = time.monotonic() - started
    logger.info(
        "Proxy harvest done: %d harvested, %d probed, %d live, %d dead (%.1fs)",
        stats["harvested"], stats["valid"] + stats["dead"], stats["valid"], stats["dead"], elapsed,
    )
    if proto_counts:
        logger.info("Live by protocol: %s", dict(proto_counts))
    if enough.is_set():
        logger.info("Stopped early: reached the %d live-proxy target", TARGET_ACTIVE)

    ranked = sorted(
        source_stats.items(),
        key=lambda kv: (kv[1].get("valid", 0), kv[1].get("harvested", 0)),
        reverse=True,
    )
    logger.info(
        "Top sources: %s",
        ", ".join(
            f"{u.rsplit('/', 1)[-1]}=new:{s['harvested']}/live:{s['valid']}/dead:{s['dead']}"
            for u, s in ranked[:6]
        ),
    )
    skipped_sources = sum(1 for stat in source_stats.values() if stat.get("skipped"))
    if skipped_sources:
        logger.info("Source cache: %d/%d unchanged or not due; skipped", skipped_sources, len(sources))
    if bad_sources:
        logger.warning("%d source(s) returned an error status, e.g. %s", len(bad_sources), bad_sources[0])

    stats["active_total"] = await pool.count("active")
    stats["secondary_total"] = await pool.count("secondary")
    stats["usable_total"] = stats["active_total"] + stats["secondary_total"]
    return stats


class ProxyManager:
    def __init__(self, db_path: Path, sources: list[str], max_concurrency: int = 50, interval_minutes: int = 30):
        self.pool = ProxyPool(db_path)
        self.sources = sources
        self.max_concurrency = max_concurrency
        self.interval = interval_minutes * 60
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self.last_stats: dict[str, int] = {}

    async def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Proxy cycle error")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _run_cycle(self) -> None:
        if os.getenv("PROXY_MODE", "").strip().lower() == "vpn_gateway":
            from modules.vpn_gateway import gateway_ports, sync_gateway_pool

            config_path = self.pool.db_path.parent / "vpn_gateway" / "xray.json"
            ports = gateway_ports(config_path)
            if not ports:
                await self.pool.archive_non_gateway_entries()
                self.last_stats = {
                    "harvested": 0, "configured": 0, "valid": 0,
                    "dead": 0, "usable_total": 0,
                }
                return
            stats = await sync_gateway_pool(self.pool, ports)
            self.last_stats = {
                "harvested": 0,
                "configured": stats["configured"],
                "valid": stats["valid"],
                "dead": stats["dead"],
                "usable_total": stats["valid"],
            }
            return
        self.last_stats = await harvest_and_validate(self.pool, self.sources, self.max_concurrency)

    async def start_background(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run(), name="proxy-manager")
        logger.info(
            "Proxy manager started (interval=%dm, sources=%d)", self.interval // 60, len(self.sources)
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def run_once(self) -> dict[str, int]:
        await self._run_cycle()
        return self.last_stats
