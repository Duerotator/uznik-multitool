from __future__ import annotations

"""Open a persistent Telegram Web window authenticated by an account session."""

import asyncio
import base64
import hashlib
from io import BytesIO
import logging
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse

from core.config import AppConfig
from core.models import AccountRecord, ProxyConfig
from core.secret_vault import VaultConflict, VaultError
from core.telegram_client import create_client
from modules.account_secrets import get_account_password, managed_2fa
from modules.raffle_common import LocalAuthProxyBridge, playwright_proxy
from utils.telegram_errors import is_frozen_error, is_invalid_auth_error

TELEGRAM_WEB_URL = "https://web.telegram.org/a/"
QR_TIMEOUT_SECONDS = 120
DIRECT_ACCESS_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
WEB_ERROR_MARKERS = (
    "this site can't be reached",
    "this site can’t be reached",
    "err_aborted",
    "err_connection",
    "err_tunnel",
    "err_proxy",
    "proxy error",
)


from dataclasses import dataclass


@dataclass(frozen=True)
class DirectAccessResult:
    account_id: str
    detail: str


def decode_login_token(login_url: str) -> bytes:
    """Decode the short-lived token embedded in Telegram Web's QR code."""
    parsed = urlparse(login_url)
    if parsed.scheme != "tg" or parsed.netloc != "login":
        raise ValueError("Telegram Web did not provide a login QR token")
    encoded = parse_qs(parsed.query).get("token", [""])[0].strip()
    if not encoded:
        raise ValueError("Telegram Web QR token is empty")
    encoded += "=" * (-len(encoded) % 4)
    try:
        return base64.b64decode(encoded, altchars=b"-_", validate=True)
    except Exception as exc:  # noqa: BLE001 - report a concise UI error
        raise ValueError("Telegram Web QR token is invalid") from exc


def _qr_image_to_rgb(raw_png: bytes):
    """Composite transparent QR screenshots onto white so dark/transparent canvases stay readable."""
    from PIL import Image

    img = Image.open(BytesIO(raw_png)).convert("RGBA")
    bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
    return Image.alpha_composite(bg, img).convert("RGB")


class DirectAccessService:
    """Keeps visible per-account Telegram Web windows alive for manual work."""

    def __init__(self, config: AppConfig, *, logger: logging.Logger | None = None):
        self.config = config
        self.log = logger or logging.getLogger("direct-access")
        self._playwright = None
        self._playwright_lock = asyncio.Lock()
        self._contexts: dict[str, object] = {}
        self._proxy_bridges: dict[str, LocalAuthProxyBridge] = {}
        self._authenticated: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._loop = None
        self._removed: set[str] = set()
        self._open_tasks: dict[str, asyncio.Task] = {}
        from modules.account_cleanup import register_browser_service
        register_browser_service(config.data_dir, self)

    def _lock_for(self, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    def _profile_dir(self, account_id: str) -> Path:
        key = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:24]
        path = self.config.data_dir / "browser_profiles" / key
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _ensure_playwright(self):
        async with self._playwright_lock:
            if self._playwright is None:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
            return self._playwright

    async def _proxy_for_account(self, account: AccountRecord):
        raw_proxy = account.proxy or self.config.global_proxy
        config = ProxyConfig.from_url(raw_proxy)
        if config is None:
            return None
        scheme = config.scheme.lower().replace("socks5h", "socks5")
        if scheme.startswith("socks5") and (config.username or config.password):
            await self._close_proxy_bridge(account.id)
            bridge = LocalAuthProxyBridge(config)
            settings = await bridge.start()
            self._proxy_bridges[account.id] = bridge
            return settings
        return playwright_proxy(raw_proxy)

    async def _close_proxy_bridge(self, account_id: str) -> None:
        bridge = self._proxy_bridges.pop(account_id, None)
        if bridge is not None:
            try:
                await bridge.close()
            except Exception:
                pass

    async def _is_logged_in(self, page) -> bool:
        """Check if Telegram Web is currently displaying the authorized chat list."""
        try:
            chat_count = await page.locator(
                "#MiddleColumn, #column-middle, #column-left, #LeftColumn, "
                ".chat-list, .chatlist, #folders-tabs, .folders-tabs, .LeftMain, div.ChatFolders"
            ).count()
            if chat_count > 0:
                auth_count = await page.locator(
                    "#auth-qr-form, #auth-pages, #auth-passcode-form, .auth-form, .auth-image, .auth-passcode"
                ).count()
                return auth_count == 0
        except Exception:
            pass
        return False

    async def _wait_for_chats(self, page, timeout_seconds: int = 30) -> bool:
        """Wait until Telegram Web loads the chat interface after authorization."""
        for _ in range(timeout_seconds):
            if await self._is_logged_in(page):
                return True
            await page.wait_for_timeout(1_000)
        return False

    async def _authenticate_page(self, account: AccountRecord, context, page) -> DirectAccessResult:
        """Wait for QR code, accept login token, fill 2FA and verify chats."""
        if await self._is_logged_in(page):
            self._authenticated.add(account.id)
            return DirectAccessResult(account.id, "Opened existing Telegram Web session")

        two_factor_filled = False
        last_exc: Exception | None = None
        for qr_attempt in range(3):
            login_url = await self._wait_for_login_qr(page)
            if login_url is None:
                if await self._is_logged_in(page):
                    self._authenticated.add(account.id)
                    return DirectAccessResult(account.id, "Opened existing Telegram Web session")
                raise RuntimeError("Telegram Web is not logged in and did not show a QR code")

            token = decode_login_token(login_url)
            try:
                await self._accept_login_token(account, token)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                upper = f"{exc.__class__.__name__} {exc}".upper()
                if "AUTH_TOKEN_EXPIRED" in upper or "AUTH_TOKEN_INVALID" in upper:
                    self.log.info("QR token expired/rotated for %s (attempt %d/3); rescanning", account.id, qr_attempt + 1)
                    await page.wait_for_timeout(1_500)
                    continue
                raise
        if last_exc is not None:
            raise last_exc

        two_factor_filled = await self._fill_two_factor_if_requested(page, account)

        reached_chats = await self._wait_for_chats(page, timeout_seconds=30)
        if reached_chats:
            self._authenticated.add(account.id)
        else:
            self._authenticated.discard(account.id)
            raise TimeoutError("Telegram Web authorization was not confirmed: chat list did not appear")

        self.log.info("Telegram Web authorized for %s", account.id)
        detail = "Telegram Web opened and authorized"
        if two_factor_filled:
            detail += " (2FA filled)"
        return DirectAccessResult(account.id, detail)

    async def open_account(self, account: AccountRecord) -> DirectAccessResult:
        task = asyncio.current_task()
        self._open_tasks[account.id] = task
        try:
            return await self._open_account(account)
        finally:
            if self._open_tasks.get(account.id) is task:
                self._open_tasks.pop(account.id, None)

    async def _open_account(self, account: AccountRecord) -> DirectAccessResult:
        """Show Telegram Web and authorize it with ``account`` when needed."""
        self._loop = asyncio.get_running_loop()
        if account.id in self._removed:
            raise RuntimeError("Account has been deleted")
        async with self._lock_for(account.id):
            if account.id in self._removed:
                raise RuntimeError("Account has been deleted")
            context = self._contexts.get(account.id)
            if context is not None:
                try:
                    pages = getattr(context, "pages", [])
                    if pages:
                        page = pages[0]
                        if await self._is_logged_in(page):
                            self._authenticated.add(account.id)
                            await page.bring_to_front()
                            return DirectAccessResult(account.id, "Telegram Web is already open and authenticated")
                        else:
                            self.log.info("Existing window for %s is not logged in. Authorizing...", account.id)
                            await page.bring_to_front()
                            return await self._authenticate_page(account, context, page)
                except Exception as exc:
                    self.log.debug("Existing context check failed for %s: %s", account.id, exc)
                await self._discard_context(account.id, context)

            pw = await self._ensure_playwright()
            proxy_settings = await self._proxy_for_account(account)
            try:
                context = await pw.chromium.launch_persistent_context(
                    user_data_dir=str(self._profile_dir(account.id)),
                    headless=False,
                    viewport={"width": 1280, "height": 900},
                    user_agent=DIRECT_ACCESS_CHROME_UA,
                    proxy=proxy_settings,
                    args=[
                        "--disable-notifications",
                        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                    ],
                )
            except BaseException:
                await self._close_proxy_bridge(account.id)
                raise
            self._contexts[account.id] = context
            context.on("close", lambda: self._context_closed(account.id))
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                page = await self._navigate_to_web(context, page)
                await page.bring_to_front()
                return await self._authenticate_page(account, context, page)
            except BaseException:
                await self._discard_context(account.id, context)
                raise

    async def _discard_context(self, account_id: str, context) -> None:
        self._contexts.pop(account_id, None)
        self._authenticated.discard(account_id)
        try:
            await context.close()
        except Exception:
            pass
        await self._close_proxy_bridge(account_id)

    def account_removed(self, account_id: str) -> bool:
        self._removed.add(account_id)
        if len(self._removed) > 2000:
            self._removed = set(list(self._removed)[-1000:])
        context = self._contexts.get(account_id)
        task = self._open_tasks.get(account_id)
        active_task = task is not None and not task.done()
        if context is None and not active_task:
            self._locks.pop(account_id, None)
            return False
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("Close the Telegram Web window before deleting this account")

        async def close_removed():
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            async with self._lock_for(account_id):
                ctx = self._contexts.pop(account_id, None)
                self._authenticated.discard(account_id)
                if ctx is not None:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                await self._close_proxy_bridge(account_id)
                from modules.account_cleanup import remove_browser_profile
                for attempt in range(5):
                    try:
                        remove_browser_profile(self.config.data_dir, account_id)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        await asyncio.sleep(0.15)
            self._locks.pop(account_id, None)

        future = asyncio.run_coroutine_threadsafe(close_removed(), self._loop)

        def completed(result):
            try:
                result.result()
            except Exception:
                self.log.exception("Could not close/remove deleted account browser profile")

        future.add_done_callback(completed)
        return True

    def _context_closed(self, account_id: str) -> None:
        self._contexts.pop(account_id, None)
        self._authenticated.discard(account_id)
        bridge = self._proxy_bridges.pop(account_id, None)
        if bridge is not None and self._loop is not None and self._loop.is_running():
            self._loop.create_task(bridge.close())

    async def _navigate_to_web(self, context, page):
        """Navigate with one fresh-page retry for Chromium's detached-frame race."""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                await page.goto(TELEGRAM_WEB_URL, wait_until="domcontentloaded", timeout=60_000)
                return page
            except Exception as exc:
                last_error = exc
                self.log.warning("Telegram Web navigation attempt %d failed: %s", attempt + 1, exc)
                if attempt == 1:
                    break
                try:
                    await page.close()
                except Exception:
                    pass
                page = await context.new_page()
        raise RuntimeError(f"Telegram Web could not be opened: {last_error}")

    async def _wait_for_login_qr(self, page) -> str | None:
        """Return the current QR URL, or None for an already signed-in profile."""
        import numpy as np
        import zxingcpp

        for _sec in range(QR_TIMEOUT_SECONDS):
            # 1. Check if already signed in
            if await self._is_logged_in(page):
                return None

            # 2. Check for browser error page
            try:
                body = (await page.locator("body").inner_text(timeout=1_500)).lower()
                if any(marker in body for marker in WEB_ERROR_MARKERS):
                    raise RuntimeError("Telegram Web showed a network/proxy error page")
            except Exception as exc:
                if "network/proxy error" in str(exc):
                    raise

            # 3. Check if phone login is shown instead of QR, and click QR switch button (EN + RU)
            try:
                qr_btn = page.locator(
                    "button:has-text('Log in by QR code'), button:has-text('Log in with QR'), "
                    "button:has-text('QR'), button:has-text('Вход по QR'), button:has-text('Войти по QR')"
                ).first
                if await qr_btn.is_visible(timeout=300):
                    await qr_btn.click(timeout=1_000)
            except Exception:
                pass

            # 4. Check if QR reload/expired button is shown
            try:
                reload_btn = page.locator(
                    "button:has-text('Reload'), button:has-text('Refresh'), "
                    "button:has-text('Обновить'), button:has-text('Повторить')"
                ).first
                if await reload_btn.is_visible(timeout=300):
                    await reload_btn.click(timeout=1_000)
            except Exception:
                pass

            # 5. Look for QR code element and screenshot it
            qr = page.locator(
                "#auth-qr-form svg, #auth-qr-form canvas, .auth-image svg, .auth-image canvas, "
                ".qr-container svg, .qr-container canvas, .qr-outer canvas, #auth-qr-form, .auth-image"
            ).first
            try:
                if await qr.is_visible(timeout=500):
                    screenshot = await qr.screenshot(timeout=2_000)
                    image = _qr_image_to_rgb(screenshot)
                    barcode = zxingcpp.read_barcode(np.asarray(image))
                    text = str(barcode.text or "") if barcode else ""
                    if text.startswith("tg://login?token="):
                        return text
            except Exception as exc:
                self.log.debug("Could not read QR from element screenshot: %s", exc)

            # 6. Fallback: screenshot the entire page and scan for QR
            try:
                page_ss = await page.screenshot(timeout=2_000)
                image = _qr_image_to_rgb(page_ss)
                barcode = zxingcpp.read_barcode(np.asarray(image))
                text = str(barcode.text or "") if barcode else ""
                if text.startswith("tg://login?token="):
                    return text
            except Exception as exc:
                self.log.debug("Could not read QR from page screenshot: %s", exc)

            await page.wait_for_timeout(1_000)

        raise TimeoutError(f"Telegram Web QR was not ready within {QR_TIMEOUT_SECONDS} seconds")

    def _resolve_two_factor_password(self, account: AccountRecord | None) -> str | None:
        if account is not None:
            try:
                pwd = get_account_password(self.config, account)
                if pwd:
                    return pwd
                if managed_2fa(account):
                    return None
            except (VaultError, VaultConflict, KeyError, OSError) as exc:
                self.log.warning("Could not read 2FA password from vault for %s (%s); enter manually", account.id, type(exc).__name__)
                return None
        return os.getenv("TELEGRAM_DIRECT_ACCESS_2FA_PASSWORD") or None

    async def _fill_two_factor_if_requested(self, page, account: AccountRecord | None = None) -> bool:
        """Fill Telegram Web's password form when QR authorization needs it."""
        password = self._resolve_two_factor_password(account)
        if not password:
            return False

        for _ in range(25):
            if await self._is_logged_in(page):
                return False

            field = page.locator("input[type='password']").first
            try:
                if await field.is_visible(timeout=1_000):
                    await field.fill(password)
                    next_button = page.get_by_role(
                        "button",
                        name=re.compile(r"^(next|continue|далее|продолжить|log in|войти)$", re.IGNORECASE),
                    ).first
                    try:
                        await next_button.click(timeout=2_000)
                    except Exception:
                        await field.press("Enter")
                    return True
            except Exception:
                pass
            await page.wait_for_timeout(1_000)
        return False

    async def _accept_login_token(self, account: AccountRecord, token: bytes) -> None:
        initial_proxy = account.proxy
        async with create_client(self.config, account, lock_wait_timeout=15.0) as client:
            client.flood_wait_raise_after = 30
            if account.proxy != initial_proxy:
                self.log.info("Account proxy rotated during QR acceptance for %s; Web context uses initial proxy", account.id)
            try:
                if account.backend == "pyrogram":
                    from pyrogram import raw
                    await client._with_flood_wait(lambda: client.client.invoke(raw.functions.auth.AcceptLoginToken(token=token)))  # type: ignore[attr-defined]
                    return
                if account.backend == "telethon":
                    from telethon.tl.functions.auth import AcceptLoginTokenRequest
                    await client._with_flood_wait(lambda: client.client(AcceptLoginTokenRequest(token=token)))  # type: ignore[attr-defined]
                    return
                raise ValueError(f"Unsupported backend: {account.backend}")
            except Exception as exc:
                if is_frozen_error(exc):
                    raise RuntimeError("Account is frozen on Telegram; cannot accept QR login") from exc
                if is_invalid_auth_error(exc):
                    raise RuntimeError("Account Telegram session is invalid/revoked") from exc
                raise

    async def close_all(self) -> None:
        contexts = list(self._contexts.values())
        self._contexts.clear()
        self._authenticated.clear()
        for context in contexts:
            try:
                await context.close()
            except Exception:
                pass
        bridges = list(self._proxy_bridges.values())
        self._proxy_bridges.clear()
        for bridge in bridges:
            try:
                await bridge.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None
