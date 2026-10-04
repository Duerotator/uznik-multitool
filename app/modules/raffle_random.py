from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.telegram_client import create_client, parse_post_link
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from modules.raffle_common import (
    configure_raffle_client,
    is_long_raffle_flood_wait,
    playwright_proxy,
    raffle_flood_skip_message,
)
from utils.rate_limit import human_delay
from utils.telegram_errors import is_invalid_auth_error, short_error

RANDOM_BOT = "random"
JOIN_APP_SHORT_NAME = "JoinLot"
JOIN_API = "https://randomgodbot.com/lot_join"
BOT_USERNAMES = {"random", "randomgodbot"}

STARTAPP_RE = re.compile(
    r"(?:startapp|startApp)=([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
JOINLOT_RE = re.compile(
    r"(?:t\.me|telegram\.me)/(?:Random|random)/JoinLot\?[^\s\"']+",
    re.IGNORECASE,
)
CHANNEL_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})|@([A-Za-z0-9_]{4,})",
    re.IGNORECASE,
)
LOT_CALLBACK_RE = re.compile(r"^lot_join\s+(\d+)$", re.IGNORECASE)
SKIP_USERNAMES = {
    "random",
    "randomgod",
    "randomgodbot",
    "telegram",
    "share",
    "boost",
    "iv",
    "addstickers",
    "proxy",
    "socks",
    "setlanguage",
}


@dataclass
class RaffleTarget:
    source: str
    start_param: str | None = None
    post_link: str | None = None
    chat: str | None = None
    message_id: int | None = None
    channels: list[str] = field(default_factory=list)
    text: str = ""
    button_text: str | None = None
    callback_data: str | None = None


@dataclass
class JoinOutcome:
    status: str
    message: str
    raw: dict[str, Any] = field(default_factory=dict)


class RandomRaffleService:
    """Participate in @random (RandomGodBot) giveaways via Mini App JoinLot API."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("raffle-random")
        self.solvecaptcha_key = (
            os.getenv("SOLVECAPTCHA_KEY")
            or os.getenv("SOLVE_CAPTCHA_KEY")
            or os.getenv("CAPTCHA_API_KEY")
            or ""
        ).strip()

    async def participate(
        self,
        accounts: list[AccountRecord],
        target: str,
        *,
        extra_channels: list[str] | None = None,
        progress: OperationProgress | None = None,
        probe_account: AccountRecord | None = None,
    ) -> ActionResult:
        enabled = [account for account in accounts if account.enabled]
        parsed = await self.resolve_target(
            target,
            extra_channels=extra_channels or [],
            probe_account=probe_account or (enabled[0] if enabled else None),
        )
        self.log.info(
            "Raffle target start_param=%s post=%s channels=%s",
            parsed.start_param,
            parsed.post_link,
            parsed.channels,
        )
        # Playwright Chromium is substantially heavier than ordinary MTProto
        # work. Keep its provider-specific ceiling low on the VPS even when
        # TELEGRAM_MAX_CONCURRENCY is higher for lightweight operations.
        browser_limit = max(1, self.config.giveaway_browser_max_concurrency)
        semaphore = asyncio.Semaphore(max(1, min(self.config.max_concurrency, browser_limit)))

        result = ActionResult()

        async def worker(account: AccountRecord) -> None:
            nonlocal current_batch_size
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                try:
                    outcome = await asyncio.wait_for(
                        self._participate_one(account, parsed),
                        timeout=180,
                    )
                    detail = outcome.message or outcome.status
                    if outcome.status in {"ok", "already"}:
                        result.add_ok(account.id)
                        result.details[account.id] = detail
                        if progress:
                            progress.mark_ok(account.id)
                        self.log.info("%s: %s", account.id, detail)
                        
                        ref_link = outcome.raw.get("ref_link")
                        if ref_link and ("start=" in str(ref_link) or "startapp=" in str(ref_link)):
                            new_start = re.split(r"start(?:app)?=", str(ref_link))[-1]
                            parsed.start_param = new_start
                            
                            new_count = outcome.raw.get("ref_count")
                            if new_count:
                                current_batch_size = max(1, int(new_count))
                                self.log.info("%s: updated batch_size to %s", account.id, current_batch_size)
                            else:
                                current_batch_size = 1
                            
                            self.log.info("%s: updated start_param to %s for next accounts", account.id, new_start)


                    else:
                        result.add_error(account.id, detail)
                        if progress:
                            progress.mark_error(account.id)
                        self.log.error("%s failed: %s", account.id, detail)
                except asyncio.TimeoutError:
                    error = "timeout after 180s"
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    self.log.error("%s timed out", account.id)
                except Exception as exc:
                    long_flood_wait = is_long_raffle_flood_wait(exc)
                    error = raffle_flood_skip_message(exc) if long_flood_wait else short_error(exc)
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    if long_flood_wait:
                        self.log.warning("%s %s", account.id, error)
                    else:
                        disable = is_invalid_auth_error(exc)
                        self.accounts.mark_error(account.id, error, disable=disable)
                        self.log.error("%s failed: %s", account.id, error)

        current_batch_size = len(enabled)
        scout, *remaining = enabled
        await worker(scout)

        idx = 0
        while idx < len(remaining):
            batch = remaining[idx : idx + current_batch_size]
            await asyncio.gather(*(worker(account) for account in batch))
            idx += len(batch)


        return result

    async def resolve_target(
        self,
        target: str,
        *,
        extra_channels: list[str] | None = None,
        probe_account: AccountRecord | None = None,
    ) -> RaffleTarget:
        raw = (target or "").strip()
        if not raw:
            raise ValueError("Empty raffle target")
        extra = extra_channels or []

        # (1) Bare start_app token
        if re.fullmatch(r"[A-Za-z0-9_\-.+-]{6,64}", raw) and not raw.startswith("@"):
            channels = self._collect_channels(raw, extra)
            post_link = _derive_post_link(raw)
            return RaffleTarget(
                source=raw,
                start_param=raw,
                channels=channels,
                post_link=post_link,
            )

        # (2) JoinLot URL (t.me/Random/JoinLot?startapp=...) — resolve offline
        start_param = extract_start_param(raw)
        if start_param and "JoinLot" in raw:
            channels = self._collect_channels(raw, extra)
            post_link = _derive_post_link(raw)
            self.log.info("Resolved via JoinLot URL: startapp=%s channels=%s", start_param, channels)
            return RaffleTarget(
                source=raw,
                start_param=start_param,
                post_link=post_link,
                channels=channels,
                text=raw,
            )

        # (3) Post link — need a client to read post text
        if looks_like_post_link(raw):
            return await self._resolve_post_link(raw, extra, probe_account)

        # (4) Maybe a bare lot_join callback text
        cb_match = LOT_CALLBACK_RE.fullmatch(raw)
        if cb_match:
            lot_id = cb_match.group(1)
            sp = f"{lot_id}G"
            channels = self._collect_channels(raw, extra)
            self.log.warning("Bare lot_join callback, trying startapp=%s", sp)
            return RaffleTarget(source=raw, start_param=sp, channels=channels)

        raise ValueError(
            "Could not resolve raffle target. Provide one of:\n"
            "  - t.me/Random/JoinLot?startapp=... link\n"
            "  - Post link (e.g. t.me/channel/123) with JoinLot button\n"
            "  - Bare startapp token"
        )

    async def _resolve_post_link(
        self,
        raw: str,
        extra_channels: list[str],
        probe_account: AccountRecord | None,
    ) -> RaffleTarget:
        post_link = normalize_post_link(raw)
        chat, message_id = parse_post_link(post_link)
        account = probe_account or self._first_enabled_account()
        if account is None:
            raise RuntimeError("No enabled accounts to resolve raffle post")

        async with create_client(self.config, account) as client:
            configure_raffle_client(client)
            detail = await client.get_message_detail(post_link)
            text = str(detail.get("text") or "")
            channels = extract_channel_targets(text)
            for hidden_link in detail.get("hidden_links") or []:
                channels.extend(extract_channel_targets(str(hidden_link)))
            for button in detail.get("buttons") or []:
                for field_name in ("web_app_url", "url"):
                    channels.extend(extract_channel_targets(str(button.get(field_name) or "")))
            chat = str(detail.get("chat_username") or detail.get("chat") or chat)
            message_id = int(detail.get("message_id") or message_id or 0) or message_id

            # buttons: prefer direct URL/webapp startapp over callback
            start_param = extract_start_param(raw)
            if not start_param:
                for button in detail.get("buttons") or []:
                    for blob in (button.get("web_app_url"), button.get("url")):
                        if not blob:
                            continue
                        found = extract_start_param(str(blob))
                        if found:
                            start_param = found
                            break
                    if start_param:
                        break

            # fallback: try callback briefly
            if not start_param:
                for button in detail.get("buttons") or []:
                    cb = button.get("callback_data") or ""
                    if LOT_CALLBACK_RE.match(str(cb)):
                        try:
                            answer = await client.request_callback_answer(
                                str(detail["chat"]),
                                int(detail["message_id"]),
                                cb,
                                timeout=25.0,
                            )
                            url = answer.get("url") or ""
                            start_param = extract_start_param(str(url)) if url else None
                            if start_param:
                                self.log.info("Resolved startapp via callback: %s", start_param)
                        except Exception as exc:
                            self.log.warning("Callback failed: %s", short_error(exc))
                        break

            if not start_param and chat and message_id:
                if not post_link.startswith("http"):
                    post_link = f"https://t.me/{str(chat).lstrip('@')}/{message_id}"
                raise ValueError(
                    f"Could not extract startapp from post. "
                    f"Provide the t.me/Random/JoinLot?startapp=... URL from the participate button. "
                    f"Post: {post_link}"
                )

            channels.extend(extract_channel_targets(" ".join(extra_channels or [])))
            if chat:
                channels.append(chat)
            channels = unique_channels(channels)
            return RaffleTarget(
                source=raw,
                start_param=start_param,
                post_link=post_link,
                chat=chat,
                message_id=message_id,
                channels=channels,
                text=text,
            )

    def _collect_channels(self, raw: str, extra: list[str]) -> list[str]:
        channels = extract_channels(raw)
        for item in extra:
            channels.extend(extract_channels(item))
        return unique_channels(channels)

    async def _participate_one(self, account: AccountRecord, target: RaffleTarget) -> JoinOutcome:
        async with create_client(self.config, account) as client:
            configure_raffle_client(client)
            if target.post_link:
                try:
                    await client.view_post(target.post_link)
                except Exception as exc:
                    if is_long_raffle_flood_wait(exc):
                        raise
                    self.log.debug("view_post failed for %s: %s", account.id, short_error(exc))

            await self._ensure_channels(client, target.channels, view_from=target.post_link)

            web = await client.request_bot_app_webview(
                RANDOM_BOT,
                JOIN_APP_SHORT_NAME,
                target.start_param or "",
                # JoinLot is a bot Mini App.  Using the source-post channel as
                # peer fails with CHANNEL_PRIVATE for accounts that can read
                # a public post but are not members of that channel.
                peer=RANDOM_BOT,
            )
            web_url = web.get("url") or ""
            if not web_url:
                raise RuntimeError("Empty WebApp URL")

            proxy_url = account.proxy or self.config.global_proxy
            outcome = await self._browser_join_flow(account.id, web_url, proxy_url)
            if outcome.status == "need_channels":
                dynamic_channels = [str(value) for value in outcome.raw.get("channels", [])]
                retry_channels = unique_channels([*target.channels, *dynamic_channels])
                if dynamic_channels:
                    self.log.info(
                        "%s: retrying after Mini App required channels: %s",
                        account.id,
                        retry_channels,
                    )
                await self._ensure_channels(client, retry_channels, view_from=target.post_link)
                outcome = await self._browser_join_flow(account.id, web_url, proxy_url)
            if outcome.status == "captcha_failed":
                self.log.info("%s: retrying Random Mini App after captcha failure", account.id)
                outcome = await self._browser_join_flow(account.id, web_url, proxy_url)
            return outcome

    async def _ensure_channels(self, client, channels: list[str], *, view_from: str | None) -> None:
        joined_now = 0
        for channel in channels:
            try:
                await client.join_chat(channel)
                joined_now += 1
                self.log.info("Joined %s", channel)
                try:
                    from utils.rate_limit import mute_and_archive
                    await mute_and_archive(client.client, channel)
                except Exception:
                    pass
            except Exception as exc:
                if is_long_raffle_flood_wait(exc):
                    raise
                err = short_error(exc)
                if "USER_ALREADY_PARTICIPANT" in err or "already" in err.lower():
                    self.log.debug("Already in %s", channel)
                else:
                    self.log.warning("Join %s failed: %s", channel, err)
            await human_delay(0.4, 1.2)
        if joined_now:
            # Telegram may acknowledge ImportChatInvite before the giveaway
            # bot's membership check sees the updated participant list.
            await human_delay(1.5, 3.0)
        if view_from:
            try:
                await client.view_post(view_from)
            except Exception as exc:
                if is_long_raffle_flood_wait(exc):
                    raise

    async def _browser_join_flow(
        self, account_id: str, web_url: str, proxy_url: str | None = None,
    ) -> JoinOutcome:
        from playwright.async_api import async_playwright

        result_text = ""
        result_status = "unknown"
        required_channels: list[str] = []
        ref_link: str | None = None
        ref_count: int | None = None
        captcha_attempts = 0

        max_captcha_rounds = 5

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
                proxy=playwright_proxy(proxy_url),
            )
            context = await browser.new_context(
                viewport={"width": 420, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Mobile Safari/537.36"
                ),
            )
            page = await context.new_page()

            # Mock Telegram WebApp platform and hide webdriver
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.Telegram = window.Telegram || {};
                window.Telegram.WebApp = window.Telegram.WebApp || {};
                window.Telegram.WebApp.platform = 'android';
            """)

            try:
                await page.goto(web_url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    await page.wait_for_selector("#descriptionText", timeout=20_000)
                except Exception:
                    body = await page.content()
                    self.log.warning("%s: page content short: %s", account_id, body[:500])
                    raise
                self.log.info("%s: page loaded", account_id)

                last_text = ""
                turnstile_clicked = False
                for _ in range(120):
                    await asyncio.sleep(1)
                    try:
                        desc_el = await page.query_selector("#descriptionText")
                        text = (await desc_el.inner_text()).strip() if desc_el else ""
                    except Exception:
                        text = ""

                    if text and text != last_text:
                        self.log.info("%s: status = %s", account_id, text[:120])
                        last_text = text

                    # Success
                    if any(
                        phrase in text.lower()
                        for phrase in (
                            "участвуете в розыгрыше",
                            "вы участвуете",
                            "now participating",
                        )
                    ):
                        result_status = "ok"
                        result_text = text
                        break

                    # Already joined
                    if any(
                        phrase in text.lower()
                        for phrase in (
                            "уже участвуете",
                            "уже принимаете",
                            "already joined",
                            "already participating",
                        )
                    ):
                        result_status = "already"
                        result_text = text
                        break

                    # Need channels
                    if any(
                        phrase in text.lower()
                        for phrase in (
                            "не подписаны на все каналы",
                            "not all channels subscribed",
                        )
                    ):
                        result_status = "need_channels"
                        result_text = text
                        required_channels = await self._page_required_channels(page)
                        break

                    # Banned / VPN

                    if any(
                        phrase in text.lower()
                        for phrase in ("vpn", "wi-fi", "отключите", "используете", "anti-bot")
                    ):
                        result_status = "banned"
                        result_text = text
                        break

                    # Hard errors
                    if any(
                        phrase in text.lower()
                        for phrase in (
                            "розыгрыш уже завершен",
                            "розыгрыш был удален",
                            "розыгрыш еще не начат",
                            "lot already end",
                            "lot was deleted",
                            "lot still waiting",
                        )
                    ):
                        result_status = "error"
                        result_text = text
                        break

                    # === Captcha flow ===
                    # Step 1: click turnstile
                    tc = await page.query_selector("#turnstile_check")
                    if tc and await tc.is_visible():
                        if not turnstile_clicked:
                            self.log.info("%s: clicking turnstile", account_id)
                            try:
                                await tc.click()
                            except Exception:
                                pass
                            turnstile_clicked = True
                            await asyncio.sleep(2)
                            continue

                    # Step 2: solve captcha digits
                    inp = await page.query_selector("#turnstile__answer")
                    if inp and await inp.is_visible():
                        if captcha_attempts < max_captcha_rounds:
                            captcha_attempts += 1
                            self.log.info(
                                "%s: captcha round %d / %d",
                                account_id,
                                captcha_attempts,
                                max_captcha_rounds,
                            )
                            # wait for image with real src
                            for _img_wait in range(5):
                                img_el = await page.query_selector(".turnstile__question img")
                                if img_el:
                                    src = await img_el.get_attribute("src") or ""
                                    if src.startswith("data:image"):
                                        break
                                await asyncio.sleep(0.5)
                            else:
                                img_el = await page.query_selector(".turnstile__question img")

                            if img_el:
                                img_bytes = await img_el.screenshot(type="png")
                                answer = await self._solve_captcha_image(img_bytes)
                                if answer:
                                    self.log.info("%s: captcha answer = %s", account_id, answer)
                                    await inp.fill(answer)
                                    btn = await page.query_selector(".turnstile__answer_button")
                                    if btn:
                                        cls = await btn.get_attribute("class") or ""
                                        if "disabled" not in cls:
                                            await btn.click()
                                            await asyncio.sleep(2)
                                            continue
                                    await asyncio.sleep(1)
                                    continue
                                else:
                                    self.log.warning("%s: captcha OCR failed", account_id)
                            else:
                                self.log.warning("%s: captcha image not found", account_id)
                            # retry after delay
                            await asyncio.sleep(2)
                            continue

                    if inp and await inp.is_visible() and captcha_attempts >= max_captcha_rounds:
                        result_status = "captcha_failed"
                        result_text = f"captcha retries exhausted after {max_captcha_rounds} attempts"
                        break

                    # 5xx / refresh error
                    if "обновить страницу" in text.lower() or "504" in text:

                        result_status = "error"
                        result_text = text
                        break

                else:
                    result_text = last_text or "timeout"
                    result_status = "error"
                    if not result_text or result_text == "timeout":
                        try:
                            body = await page.content()
                            self.log.error("%s: page HTML: %s", account_id, body[:600])
                        except Exception:
                            pass
                        result_text = f"browser flow timeout after 120s, last: {last_text or 'N/A'}"
                

                if result_status in ("ok", "already"):
                    try:
                        content = await page.content()
                        import re
                        match = re.search(r'https?://(?:t\.me|telegram\.me)/[a-zA-Z0-9_]+bot\?(?:start|startapp)=[a-zA-Z0-9_-]+', content)
                        if match:
                            ref_link = match.group(0)
                            self.log.info("%s: found referral link %s", account_id, ref_link)
                            
                            # extract referral count
                            try:
                                page_text = (await page.inner_text("body")).lower()
                                fraction_match = re.search(r'\d+\s*/\s*(\d+)', page_text)
                                if fraction_match:
                                    ref_count = int(fraction_match.group(1))
                                else:
                                    cnt_match = re.search(r'(?:invite|пригласи(?:ть)?)\D*?(\d+)', page_text)
                                    if cnt_match:
                                        ref_count = int(cnt_match.group(1))
                            except Exception:
                                pass
                    except Exception:
                        pass



            except Exception as exc:
                result_status = "error"
                result_text = short_error(exc)
                self.log.error("%s: browser error %s", account_id, result_text)
            finally:
                await browser.close()

        mapping = {
            "ok": "joined",
            "already": "already joined",
            "need_channels": "NOT_ALL_CHANNELS_SUBSCRIBED",
            "banned": "banned (VPN/anti-bot)",
            "captcha_failed": "captcha solve failed",
        }
        raw_dict: dict[str, object] = {}
        if result_status == "need_channels":
            raw_dict["channels"] = required_channels
        if ref_link:
            raw_dict["ref_link"] = ref_link
        if ref_count:
            raw_dict["ref_count"] = ref_count

        return JoinOutcome(
            status=result_status,
            message=mapping.get(result_status, result_text),
            raw=raw_dict,
        )

    @staticmethod
    async def _page_required_channels(page: Any) -> list[str]:
        """Read Telegram channel links rendered by the Random Mini App."""
        try:
            hrefs = await page.locator("a[href]").evaluate_all(
                "elements => elements.map(item => item.href)"
            )
        except Exception:
            hrefs = []
        try:
            content = await page.content()
        except Exception:
            content = ""
        
        combined_text = " ".join(str(href or "") for href in hrefs) + " " + str(content)
        return extract_channel_targets(combined_text)

    async def _solve_captcha_image(self, image: bytes) -> str | None:

        result = await asyncio.to_thread(_ddddocr_digits, image)
        if result:
            self.log.info("Captcha ddddocr: %s", result)
            return result
        digits = await asyncio.to_thread(_tesseract_digits, image)
        if digits:
            self.log.info("Captcha tesseract: %s", digits)
            return digits
        if self.solvecaptcha_key:
            digits = await _solvecaptcha_digits(self.solvecaptcha_key, image)
            if digits:
                self.log.info("Captcha solvecaptcha: %s", digits)
                return digits
        self.log.error("No captcha solver. Install tesseract or set SOLVECAPTCHA_KEY.")
        return None

    def _first_enabled_account(self) -> AccountRecord | None:
        for account in self.accounts.list_accounts(enabled_only=True):
            return account
        return None


def extract_start_param(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    match = STARTAPP_RE.search(text)
    if match:
        return unquote(match.group(1))
    joinlot = JOINLOT_RE.search(text)
    if joinlot:
        match = STARTAPP_RE.search(joinlot.group(0))
        if match:
            return unquote(match.group(1))
    parsed = urlparse(text if "://" in text else f"https://{text}")
    if parsed.netloc.lower() in {"t.me", "telegram.me"} and "joinlot" in parsed.path.lower():
        qs = parse_qs(parsed.query)
        for key in ("startapp", "startApp"):
            if qs.get(key):
                return unquote(qs[key][0])
    return None


def extract_channels(text: str) -> list[str]:
    found: list[str] = []
    for match in CHANNEL_RE.finditer(text or ""):
        name = match.group(1) or match.group(2)
        if not name:
            continue
        lower = name.lower()
        if lower in SKIP_USERNAMES or lower.endswith("bot"):
            continue
        if lower in {"c", "s", "joinchat"}:
            continue
        found.append(f"@{name}" if not name.startswith("@") else name)
    return unique_channels(found)


def extract_channel_targets(text: str) -> list[str]:
    """Extract public usernames and Telegram invite URLs from a condition link."""
    channels = extract_channels(text)
    for raw_url in re.findall(r"https?://[^\s<>\"']+", text or ""):
        parsed = urlparse(raw_url.rstrip(".,!?:;)}]"))
        if parsed.hostname not in {"t.me", "telegram.me"}:
            continue
        path = parsed.path.lower()
        if path.startswith("/+") or path.startswith("/joinchat/"):
            channels.append(raw_url)
    return unique_channels(channels)


def unique_channels(channels: list[str], exclude_chat: str | None = None) -> list[str]:
    exclude = (exclude_chat or "").lstrip("@").lower()
    seen: set[str] = set()
    result: list[str] = []
    for channel in channels:
        name = channel.strip()
        if not name:
            continue
        if not name.startswith("@") and not name.lstrip("-").isdigit() and "://" not in name:
            name = f"@{name}"
        key = name.lower()
        if key in seen:
            continue
        if name.lstrip("@").lower() == exclude:
            continue
        if name.lstrip("@").lower() in SKIP_USERNAMES:
            continue
        seen.add(key)
        result.append(name)
    return result


def looks_like_post_link(value: str) -> bool:
    text = value.strip().lower()
    if "t.me/" not in text and "telegram.me/" not in text:
        return False
    if "joinlot" in text or "startapp=" in text:
        # could still be a post if path has /channel/id
        pass
    try:
        parse_post_link(normalize_post_link(value))
        return True
    except Exception:
        return False


def normalize_post_link(value: str) -> str:
    text = value.strip()
    if text.startswith("@"):
        return text
    if text.lower().startswith(("t.me/", "telegram.me/")):
        return f"https://{text}"
    return text


def _derive_post_link(raw: str) -> str | None:
    """If input looks like a post link, return it; else None."""
    try:
        text = normalize_post_link(raw)
        parse_post_link(text)
        return text
    except Exception:
        return None


def _user_id_from_init(init_data: str) -> int | None:
    try:
        qs = parse_qs(init_data)
        user_raw = qs.get("user", [""])[0]
        if not user_raw:
            return None
        import json

        user = json.loads(user_raw)
        return int(user.get("id"))
    except Exception:
        return None


def _ddddocr_digits(image: bytes) -> str | None:
    try:
        import ddddocr
        ocr = ddddocr.DdddOcr(show_ad=False)
        result = ocr.classification(image)
        return re.sub(r"\D", "", result or "")[:5] if result else None
    except Exception:
        return None


def _tesseract_digits(image: bytes) -> str | None:
    import subprocess
    import tempfile
    from pathlib import Path

    tesseract_bin = _find_tesseract()
    if not tesseract_bin:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "captcha.png"
        path.write_bytes(image)
        try:
            proc = subprocess.run(
                [
                    tesseract_bin,
                    str(path),
                    "stdout",
                    "--psm",
                    "7",
                    "-c",
                    "tessedit_char_whitelist=0123456789",
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
                env={**__import__("os").environ, "PATH": "/usr/bin:/usr/local/bin:" + __import__("os").environ.get("PATH", "")},
            )
        except Exception:
            return None
        digits = re.sub(r"\D", "", proc.stdout or "")
        if len(digits) >= 4:
            return digits[:5]
    return None


def _find_tesseract() -> str | None:
    import shutil
    from pathlib import Path

    for candidate in (
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "tesseract",
    ):
        if shutil.which(candidate) is not None or Path(candidate).is_file():
            return candidate
    return None


async def _solvecaptcha_digits(api_key: str, image: bytes) -> str | None:
    payload = {
        "key": api_key,
        "method": "base64",
        "body": base64.b64encode(image).decode("ascii"),
        "json": 1,
        "numeric": 1,
        "min_len": 4,
        "max_len": 6,
        "regsense": 0,
    }
    async with httpx.AsyncClient(timeout=30.0) as http:
        created = await http.post("https://api.solvecaptcha.com/in.php", data=payload)
        try:
            body = created.json()
        except Exception:
            return None
        if body.get("status") != 1:
            return None
        request_id = body.get("request")
        if not request_id:
            return None
        for _ in range(24):
            await asyncio.sleep(3)
            poll = await http.get(
                "https://api.solvecaptcha.com/res.php",
                params={"key": api_key, "action": "get", "id": request_id, "json": 1},
            )
            try:
                data = poll.json()
            except Exception:
                continue
            if data.get("status") == 1:
                digits = re.sub(r"\D", "", str(data.get("request") or ""))
                return digits[:5] if digits else None
            if str(data.get("request")) not in {"CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"}:
                return None
    return None
