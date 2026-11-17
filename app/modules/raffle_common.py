from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
import ipaddress
import random
import re
import threading
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from core.models import ProxyConfig
from utils.telegram_errors import flood_wait_seconds as _tg_flood_wait_seconds


# Giveaway participation is batch work. A long Telegram FloodWait must not
# hold the whole batch (or quietly sleep inside AccountClient).
RAFFLE_MAX_FLOOD_WAIT_SECONDS = 30

# Updated late-2026 Chrome Android mobile user agent for Mini App Playwright flows.
RAFFLE_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Mobile Safari/537.36"
)

_REF_LINK_RE = re.compile(
    r"https?://(?:t\.me|telegram\.me)/([A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)?)\?([^\s\"'<>]+)",
    re.IGNORECASE,
)
_REF_FRACTION_RE = re.compile(
    r"(?:invite|пригласи(?:ть|те)?|реферал|друз|friends?)\D{0,40}?(\d+)\s*/\s*(\d+)",
    re.IGNORECASE,
)
_REF_COUNT_RE = re.compile(
    r"(?:invite|пригласи(?:ть|те)?|друзей)\D{0,24}?(\d+)",
    re.IGNORECASE,
)

_OCR_LOCK = threading.Lock()
_DDDDOCR_INSTANCE: Any | None = None
_DDDDOCR_FAILED = False


def flood_wait_seconds(exc: BaseException) -> int:
    """Return the Telegram FloodWait duration, or zero for another error."""
    seconds = _tg_flood_wait_seconds(exc)
    if seconds is not None:
        return int(seconds)
    # Fallback for custom/mock exceptions containing only FLOOD_WAIT_N in text.
    text = str(exc)
    if "FLOOD" in text.upper() and "WAIT" in text.upper():
        for attr in ("value", "seconds", "x"):
            try:
                val = int(getattr(exc, attr, 0) or 0)
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val
    return 0


def is_long_raffle_flood_wait(exc: BaseException) -> bool:
    return flood_wait_seconds(exc) > RAFFLE_MAX_FLOOD_WAIT_SECONDS


def raffle_flood_skip_message(exc: BaseException) -> str:
    seconds = flood_wait_seconds(exc)
    return f"skipped: FloodWait {seconds}s (limit {RAFFLE_MAX_FLOOD_WAIT_SECONDS}s)"


def configure_raffle_client(client: Any) -> None:
    """Make AccountClient raise rather than sleep on a long batch FloodWait."""
    client.flood_wait_raise_after = RAFFLE_MAX_FLOOD_WAIT_SECONDS


def playwright_proxy(proxy_url: str | None) -> dict[str, Any] | None:
    """Convert the account VPN URL to Playwright's authenticated proxy shape."""
    config = ProxyConfig.from_url(proxy_url)
    if config is None:
        return None
    scheme = config.scheme.replace("socks5h", "socks5")
    result: dict[str, Any] = {
        "server": f"{scheme}://{config.hostname}:{config.port}",
    }
    if config.username:
        result["username"] = config.username
    if config.password:
        result["password"] = config.password
    return result


class LocalAuthProxyBridge:
    """Local unauthenticated HTTP CONNECT bridge to an authenticated SOCKS5 upstream.

    Chromium does not support username/password auth natively on SOCKS5 proxies.
    """

    def __init__(self, proxy: ProxyConfig) -> None:
        self._proxy = proxy
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.port: int = 0

    @property
    def playwright_config(self) -> dict[str, Any]:
        return {"server": f"http://127.0.0.1:{self.port}"}

    async def start(self) -> dict[str, Any]:
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        sock = self._server.sockets[0]
        self.port = int(sock.getsockname()[1])
        return self.playwright_config

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except Exception:
                pass
            self._server = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        remote_writer: asyncio.StreamWriter | None = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15.0)
            lines = head.decode("latin-1").split("\r\n")
            if not lines or not lines[0]:
                return
            parts = lines[0].split()
            if len(parts) < 3:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                return
            method, target, _proto = parts[0].upper(), parts[1], parts[2]
            if method == "CONNECT":
                host, port = self._split_host_port(target, 443)
                remote_reader, remote_writer = await self._connect_socks5(host, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                parsed = urlparse(target)
                host = parsed.hostname or ""
                if not host:
                    for hdr in lines[1:]:
                        if hdr.lower().startswith("host:"):
                            host, port = self._split_host_port(hdr.split(":", 1)[1].strip(), 80)
                            break
                    else:
                        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                        await writer.drain()
                        return
                else:
                    port = parsed.port or (443 if parsed.scheme == "https" else 80)
                remote_reader, remote_writer = await self._connect_socks5(host, port)
                req_path = parsed.path or "/"
                if parsed.query:
                    req_path += "?" + parsed.query
                rebuilt = "\r\n".join([f"{method} {req_path} HTTP/1.1", *lines[1:]])
                remote_writer.write(rebuilt.encode("latin-1"))
                await remote_writer.drain()

            await asyncio.gather(
                self._pipe(reader, remote_writer),
                self._pipe(remote_reader, writer),
                return_exceptions=True,
            )
        except Exception:
            try:
                if not writer.is_closing():
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                    await writer.drain()
            except Exception:
                pass
        finally:
            if task is not None:
                self._tasks.discard(task)
            for w in (remote_writer, writer):
                if w is not None and not w.is_closing():
                    try:
                        w.close()
                        await asyncio.wait_for(w.wait_closed(), timeout=1.0)
                    except Exception:
                        pass

    @staticmethod
    def _split_host_port(target: str, default_port: int) -> tuple[str, int]:
        if target.startswith("[") and "]" in target:
            host, rest = target[1:].split("]", 1)
            port = int(rest[1:]) if rest.startswith(":") else default_port
            return host, port
        if ":" in target:
            host, port_str = target.rsplit(":", 1)
            return host, int(port_str)
        return target, default_port

    async def _connect_socks5(self, dest_host: str, dest_port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._proxy.hostname, self._proxy.port),
            timeout=15.0,
        )
        try:
            has_auth = bool(self._proxy.username or self._proxy.password)
            methods = bytes([0x05, 0x02, 0x00, 0x02]) if has_auth else bytes([0x05, 0x01, 0x00])
            writer.write(methods)
            await writer.drain()
            greeting = await asyncio.wait_for(reader.readexactly(2), timeout=10.0)
            if greeting[0] != 0x05 or greeting[1] == 0xFF:
                raise ConnectionError("SOCKS5 greeting rejected")
            if greeting[1] == 0x02:
                user_b = (self._proxy.username or "").encode("utf-8")
                pass_b = (self._proxy.password or "").encode("utf-8")
                if len(user_b) > 255 or len(pass_b) > 255:
                    raise ValueError("SOCKS5 credentials too long")
                writer.write(bytes([0x01, len(user_b)]) + user_b + bytes([len(pass_b)]) + pass_b)
                await writer.drain()
                auth_resp = await asyncio.wait_for(reader.readexactly(2), timeout=10.0)
                if auth_resp[1] != 0x00:
                    raise ConnectionError("SOCKS5 authentication failed")

            addr_payload: bytes
            try:
                ip_obj = ipaddress.ip_address(dest_host)
                if isinstance(ip_obj, ipaddress.IPv4Address):
                    addr_payload = bytes([0x01]) + ip_obj.packed
                else:
                    addr_payload = bytes([0x04]) + ip_obj.packed
            except ValueError:
                host_b = dest_host.encode("idna")
                if not host_b or len(host_b) > 255:
                    raise ValueError("Invalid SOCKS5 destination host")
                addr_payload = bytes([0x03, len(host_b)]) + host_b

            req = bytes([0x05, 0x01, 0x00]) + addr_payload + int(dest_port).to_bytes(2, "big")
            writer.write(req)
            await writer.drain()
            resp_hdr = await asyncio.wait_for(reader.readexactly(4), timeout=15.0)
            if resp_hdr[0] != 0x05 or resp_hdr[1] != 0x00:
                raise ConnectionError(f"SOCKS5 connect failed (rep={resp_hdr[1]})")
            atyp = resp_hdr[3]
            if atyp == 0x01:
                await asyncio.wait_for(reader.readexactly(6), timeout=10.0)
            elif atyp == 0x04:
                await asyncio.wait_for(reader.readexactly(18), timeout=10.0)
            elif atyp == 0x03:
                dlen = (await asyncio.wait_for(reader.readexactly(1), timeout=10.0))[0]
                await asyncio.wait_for(reader.readexactly(dlen + 2), timeout=10.0)
            return reader, writer
        except BaseException:
            writer.close()
            raise

    @staticmethod
    async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await src.read(32768)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        finally:
            if not dst.is_closing():
                try:
                    dst.write_eof()
                except Exception:
                    pass


@asynccontextmanager
async def playwright_proxy_context(proxy_url: str | None):
    """Yield Playwright proxy settings, bridging authenticated SOCKS5 via local HTTP CONNECT."""
    config = ProxyConfig.from_url(proxy_url)
    if config is None:
        yield None
        return
    scheme = config.scheme.lower().replace("socks5h", "socks5")
    if scheme.startswith("socks5") and (config.username or config.password):
        bridge = LocalAuthProxyBridge(config)
        try:
            yield await bridge.start()
        finally:
            await bridge.close()
        return
    yield playwright_proxy(proxy_url)


def solve_ddddocr(image: bytes, *, alphanumeric: bool = False, min_len: int = 3, max_len: int = 5) -> str | None:
    """Thread-safe cached ddddocr inference (avoids reloading ONNX weights per captcha)."""
    global _DDDDOCR_INSTANCE, _DDDDOCR_FAILED
    if _DDDDOCR_FAILED:
        return None
    with _OCR_LOCK:
        if _DDDDOCR_FAILED:
            return None
        if _DDDDOCR_INSTANCE is None:
            try:
                import ddddocr
                _DDDDOCR_INSTANCE = ddddocr.DdddOcr(show_ad=False)
            except Exception:
                _DDDDOCR_FAILED = True
                return None
        try:
            raw = _DDDDOCR_INSTANCE.classification(image)
        except Exception:
            return None
    if not raw:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]" if alphanumeric else r"\D", "", str(raw))
    if len(cleaned) < min_len:
        return None
    return cleaned[:max_len]


def extract_query_start_param(url: str) -> str | None:
    """Extract start/startapp query value cleanly without trailing & parameters."""
    text = (url or "").strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"https://{text}")
    qs = parse_qs(parsed.query)
    for key in ("startapp", "startApp", "start"):
        vals = qs.get(key)
        if vals and vals[0]:
            return unquote(vals[0].strip())
    return None


def extract_ref_link_and_count(
    text: str,
    candidate_links: list[str] | None = None,
    *,
    ignore_start_params: set[str] | None = None,
) -> tuple[str | None, int | None]:
    """Extract a bot/MiniApp referral link (including /AppName paths) and needed invite count."""
    ignored = {item for item in (ignore_start_params or set()) if item}
    blobs: list[str] = list(candidate_links or [])
    if text:
        blobs.append(text)

    ref_link: str | None = None
    for blob in blobs:
        for match in _REF_LINK_RE.finditer(str(blob)):
            full_url = match.group(0).rstrip(".,!?:;)}\"'")
            sp = extract_query_start_param(full_url)
            if not sp or sp in ignored:
                continue
            ref_link = full_url
            break
        if ref_link:
            break

    ref_count: int | None = None
    if text:
        lowered = text.lower()
        frac = _REF_FRACTION_RE.search(lowered)
        if frac:
            current_val, target_val = int(frac.group(1)), int(frac.group(2))
            if target_val > 0:
                ref_count = max(1, target_val - current_val)
        else:
            cnt = _REF_COUNT_RE.search(lowered)
            if cnt:
                val = int(cnt.group(1))
                if val > 0:
                    ref_count = val
    return ref_link, ref_count


class ReferralCoordinator:
    """Concurrency-safe referral FIFO queue for multi-account giveaway waves."""

    def __init__(self, initial_start_param: str | None) -> None:
        self.initial_start_param = initial_start_param
        self._lock = asyncio.Lock()
        self._queue: deque[list[Any]] = deque()  # [start_param, remaining_slots]
        self._seen: set[str] = {initial_start_param} if initial_start_param else set()

    async def acquire_start_param(self) -> str | None:
        async with self._lock:
            while self._queue:
                head = self._queue[0]
                if head[1] > 0:
                    head[1] -= 1
                    sp = head[0]
                    if head[1] <= 0:
                        self._queue.popleft()
                    return sp
                self._queue.popleft()
            return self.initial_start_param

    async def register_ref(self, ref_link: str | None, ref_count: int | None) -> str | None:
        sp = extract_query_start_param(ref_link or "")
        if not sp:
            return None
        slots = max(1, int(ref_count or 1))
        async with self._lock:
            if sp in self._seen:
                return None
            self._seen.add(sp)
            self._queue.append([sp, slots])
            return sp


async def stop_aware_sleep(seconds: float, stop_event: asyncio.Event | None = None) -> bool:
    """Sleep up to ``seconds``, waking immediately if ``stop_event`` is set. Returns True if stopped."""
    if stop_event is not None and stop_event.is_set():
        return True
    if seconds <= 0:
        return bool(stop_event is not None and stop_event.is_set())
    if stop_event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return stop_event.is_set()


async def stop_aware_delay(
    min_seconds: float,
    max_seconds: float,
    stop_event: asyncio.Event | None = None,
) -> bool:
    if max_seconds <= 0:
        return bool(stop_event is not None and stop_event.is_set())
    delay = random.uniform(max(0.0, min_seconds), max(min_seconds, max_seconds))
    return await stop_aware_sleep(delay, stop_event)


def raffle_stealth_init_script(*, platform: str = "android", language: str = "ru-RU") -> str:
    return f"""
        Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
        Object.defineProperty(navigator, 'languages', {{get: () => [{language!r}, 'en-US', 'en']}});
        if (!navigator.userAgentData) {{
            Object.defineProperty(navigator, 'userAgentData', {{
                get: () => ({{
                    brands: [
                        {{brand: 'Chromium', version: '142'}},
                        {{brand: 'Google Chrome', version: '142'}},
                        {{brand: 'Not_A Brand', version: '24'}}
                    ],
                    mobile: true,
                    platform: 'Android'
                }})
            }});
        }}
        window.Telegram = window.Telegram || {{}};
        window.Telegram.WebApp = window.Telegram.WebApp || {{}};
        window.Telegram.WebApp.platform = {platform!r};
    """
