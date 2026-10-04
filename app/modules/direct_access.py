from __future__ import annotations

"""Open a persistent Telegram Web window authenticated by an account session."""

import asyncio
import base64
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.config import AppConfig
from core.models import AccountRecord
from core.telegram_client import create_client
from modules.raffle_common import playwright_proxy

TELEGRAM_WEB_URL = "https://web.telegram.org/a/"
QR_TIMEOUT_SECONDS = 120
WEB_ERROR_MARKERS = (
    "this site can't be reached",
    "this site can’t be reached",
    "err_aborted",
    "err_connection",
    "err_tunnel",
    "proxy error",
)


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


class DirectAccessService:
    """Keeps visible per-account Telegram Web windows alive for manual work."""

    def __init__(self, config: AppConfig, *, logger: logging.Logger | None = None):
        self.config = config
        self.log = logger or logging.getLogger("direct-access")
        self._playwright = None
        self._contexts: dict[str, object] = {}
        self._authenticated: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, account_id: str) -> asyncio.Lock:
        return self._locks.setdefault(account_id, asyncio.Lock())

    def _profile_dir(self, account_id: str) -> Path:
        key = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:24]
        path = self.config.data_dir / "browser_profiles" / key
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _is_logged_in(self, page) -> bool:
        """Check if Telegram Web is currently displaying the authorized chat list."""
        try:
            chat_count = await page.locator(
                "#MiddleColumn, #column-middle, #column-left, #LeftColumn, "
                ".chat-list, .chatlist, #folders-tabs, .folders-tabs, .LeftMain, div.ChatFolders"
            ).count()
            if chat_count > 0:
                auth_count = await page.locator("#auth-qr-form, #auth-pages, .auth-form, .auth-image").count()
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

        login_url = await self._wait_for_login_qr(page)
        if login_url is None:
            if await self._is_logged_in(page):
                self._authenticated.add(account.id)
                return DirectAccessResult(account.id, "Opened existing Telegram Web session")
            raise RuntimeError("Telegram Web is not logged in and did not show a QR code")

        token = decode_login_token(login_url)
        await self._accept_login_token(account, token)

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
        """Show Telegram Web and authorize it with ``account`` when needed."""
        async with self._lock_for(account.id):
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

            from playwright.async_api import async_playwright

            if self._playwright is None:
                self._playwright = await async_playwright().start()
            context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self._profile_dir(account.id)),
                headless=False,
                viewport={"width": 1280, "height": 900},
                proxy=playwright_proxy(account.proxy or self.config.global_proxy),
                args=["--disable-notifications"],
            )
            self._contexts[account.id] = context
            context.on("close", lambda: self._context_closed(account.id))
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                page = await self._navigate_to_web(context, page)
                await page.bring_to_front()
                return await self._authenticate_page(account, context, page)
            except Exception:
                await self._discard_context(account.id, context)
                raise

    async def _discard_context(self, account_id: str, context) -> None:
        self._contexts.pop(account_id, None)
        self._authenticated.discard(account_id)
        try:
            await context.close()
        except Exception:
            pass

    def _context_closed(self, account_id: str) -> None:
        self._contexts.pop(account_id, None)
        self._authenticated.discard(account_id)

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
        from io import BytesIO
        from PIL import Image
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

            # 3. Check if phone login is shown instead of QR, and click "Log in by QR code"
            try:
                qr_btn = page.locator(
                    "button:has-text('Log in by QR code'), button:has-text('Log in with QR'), button:has-text('QR')"
                ).first
                if await qr_btn.is_visible(timeout=300):
                    await qr_btn.click(timeout=1_000)
            except Exception:
                pass

            # 4. Check if QR reload/expired button is shown
            try:
                reload_btn = page.locator(
                    "button:has-text('Reload'), button:has-text('Refresh'), button:has-text('Обновить')"
                ).first
                if await reload_btn.is_visible(timeout=300):
                    await reload_btn.click(timeout=1_000)
            except Exception:
                pass

            # 5. Look for QR code element and screenshot it
            qr = page.locator(
                "#auth-qr-form svg, #auth-qr-form canvas, .auth-image svg, .auth-image canvas, "
                ".qr-container svg, .qr-container canvas, #auth-qr-form, .auth-image"
            ).first
            try:
                if await qr.is_visible(timeout=500):
                    screenshot = await qr.screenshot(timeout=2_000)
                    image = Image.open(BytesIO(screenshot)).convert("RGB")
                    barcode = zxingcpp.read_barcode(np.asarray(image))
                    text = str(barcode.text or "") if barcode else ""
                    if text.startswith("tg://login?token="):
                        return text
            except Exception as exc:
                self.log.debug("Could not read QR from element screenshot: %s", exc)

            # 6. Fallback: screenshot the entire page and scan for QR
            try:
                page_ss = await page.screenshot(timeout=2_000)
                image = Image.open(BytesIO(page_ss)).convert("RGB")
                barcode = zxingcpp.read_barcode(np.asarray(image))
                text = str(barcode.text or "") if barcode else ""
                if text.startswith("tg://login?token="):
                    return text
            except Exception as exc:
                self.log.debug("Could not read QR from page screenshot: %s", exc)

            await page.wait_for_timeout(1_000)

        raise TimeoutError(f"Telegram Web QR was not ready within {QR_TIMEOUT_SECONDS} seconds")

    async def _fill_two_factor_if_requested(self, page, account: AccountRecord | None = None) -> bool:
        """Fill Telegram Web's password form when QR authorization needs it."""
        password = None
        if account and account.metadata:
            password = account.metadata.get("cloud_password_val")
        if not password:
            password = os.getenv("TELEGRAM_DIRECT_ACCESS_2FA_PASSWORD")
        if not password:
            password = getattr(self.config, "two_fa_password", None)
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
        async with create_client(self.config, account) as client:
            if account.backend == "pyrogram":
                from pyrogram import raw
                await client._with_flood_wait(lambda: client.client.invoke(raw.functions.auth.AcceptLoginToken(token=token)))  # type: ignore[attr-defined]
                return
            if account.backend == "telethon":
                from telethon.tl.functions.auth import AcceptLoginTokenRequest
                await client._with_flood_wait(lambda: client.client(AcceptLoginTokenRequest(token=token)))  # type: ignore[attr-defined]
                return
            raise ValueError(f"Unsupported backend: {account.backend}")

    async def close_all(self) -> None:
        contexts = list(self._contexts.values())
        self._contexts.clear()
        self._authenticated.clear()
        for context in contexts:
            try:
                await context.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None
