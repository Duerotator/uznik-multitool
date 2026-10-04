from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.telegram_client import create_client
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from modules.raffle_common import (
    configure_raffle_client,
    is_long_raffle_flood_wait,
    playwright_proxy,
    raffle_flood_skip_message,
)
from modules.raffle_random import extract_channels, looks_like_post_link, normalize_post_link, unique_channels
from utils.rate_limit import human_delay
from utils.telegram_errors import is_invalid_auth_error, short_error

BOT = "randombeast_bot"
APP_SHORT_NAME = "devapp"
START_RE = re.compile(r"startapp=([^&\s]+)", re.IGNORECASE)

log = logging.getLogger("raffle-randombeast")


@dataclass
class RandomBeastTarget:
    source: str
    start_param: str
    channels: list[str] = field(default_factory=list)
    post_link: str | None = None


class RandomBeastService:

    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)

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
        parsed = await self._resolve_target(
            target, extra_channels or [], probe_account=probe_account or (enabled[0] if enabled else None),
        )
        log.info("RandomBeast start=%s channels=%s", parsed.start_param, parsed.channels)

        browser_limit = min(
            self.config.max_concurrency or 1,
            getattr(self.config, "giveaway_browser_max_concurrency", None) or 3,
        )
        sem = asyncio.Semaphore(max(1, browser_limit))
        result = ActionResult()

        async def worker(acc: AccountRecord) -> None:
            nonlocal current_batch_size
            async with sem:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                try:
                    outcome = await asyncio.wait_for(self._participate_one(acc, parsed), timeout=300)
                    if outcome.status in ("ok", "already"):
                        result.add_ok(acc.id)
                        result.details[acc.id] = outcome.message
                        if progress: progress.mark_ok(acc.id)
                        
                        if getattr(outcome, "ref_link", None) and ("start=" in str(outcome.ref_link) or "startapp=" in str(outcome.ref_link)):
                            new_start = re.split(r"start(?:app)?=", outcome.ref_link)[-1]
                            parsed.start_param = new_start
                            
                            if outcome.ref_count:
                                current_batch_size = max(1, int(outcome.ref_count))
                                log.info("%s: updated batch_size to %s", acc.id, current_batch_size)
                            else:
                                current_batch_size = 1
                            
                            log.info("%s: updated start_param to %s for next accounts", acc.id, new_start)

                    else:
                        result.add_error(acc.id, outcome.message)
                        if progress: progress.mark_error(acc.id)
                except asyncio.TimeoutError:
                    result.add_error(acc.id, "timeout")
                    if progress: progress.mark_error(acc.id)
                except Exception as exc:
                    err = (
                        raffle_flood_skip_message(exc)
                        if is_long_raffle_flood_wait(exc)
                        else short_error(exc)
                    )
                    result.add_error(acc.id, err)
                    if progress: progress.mark_error(acc.id)
                    if is_invalid_auth_error(exc):
                        self.accounts.mark_error(acc.id, err, disable=True)

        current_batch_size = len(enabled)
        scout, *remaining = enabled
        await worker(scout)
        
        idx = 0
        while idx < len(remaining):
            batch = remaining[idx : idx + current_batch_size]
            await asyncio.gather(*(worker(acc) for acc in batch))
            idx += len(batch)

            
        return result

    async def _resolve_target(
        self, target: str, extra: list[str], probe_account: AccountRecord | None = None,
    ) -> RandomBeastTarget:
        raw = target.strip()
        if not raw:
            raise ValueError("Empty target")
        if _looks_like_post(raw):
            return await self._resolve_from_post(_normalize(raw), extra, probe_account)
        sp = _extract_start(raw)
        if not sp:
            raise ValueError("No startapp in URL. Provide t.me/randombeast_bot/devapp?startapp=...")
        channels = list(extract_channels(" ".join(extra)))
        pl = None
        if _looks_like_post(raw):
            pl = _normalize(raw)
        return RandomBeastTarget(source=raw, start_param=sp, channels=channels, post_link=pl)

    async def _resolve_from_post(
        self, post_link: str, extra: list[str], probe_account: AccountRecord | None = None,
    ) -> RandomBeastTarget:
        account = probe_account or self._first_account()
        if account is None:
            raise RuntimeError("No enabled accounts")
        channels = list(extract_channels(" ".join(extra)))
        start_param = None
        async with create_client(self.config, account) as client:
            configure_raffle_client(client)
            detail = await client.get_message_detail(post_link)
            channels.extend(extract_channels(str(detail.get("text") or "")))
            for button in detail.get("buttons") or []:
                for field_name in ("web_app_url", "url"):
                    value = str(button.get(field_name) or "")
                    if "randombeast" not in value.lower():
                        continue
                    start_param = _extract_start(value)
                    if start_param:
                        break
                if start_param:
                    break
        if not start_param:
            raise ValueError(f"Could not find a randombeast startapp link in post {post_link}")
        return RandomBeastTarget(
            source=post_link,
            start_param=start_param,
            channels=list(dict.fromkeys(channels)),
            post_link=post_link,
        )

    def _first_account(self) -> AccountRecord | None:
        return next(iter(self.accounts.list_accounts(enabled_only=True)), None)

    async def _participate_one(self, acc: AccountRecord, target: RandomBeastTarget) -> _Outcome:
        async with create_client(self.config, acc) as client:
            configure_raffle_client(client)
            c = client.client

            await self._join_channels(client, acc, target.channels)

            if target.post_link:
                try:
                    await client.view_post(target.post_link)
                except Exception as exc:
                    if is_long_raffle_flood_wait(exc):
                        raise

            await client.send_message(BOT, "/start")
            await asyncio.sleep(2)
            async for _ in c.get_chat_history(BOT, limit=2):
                pass

            web = await client.request_bot_app_webview(
                BOT, APP_SHORT_NAME, target.start_param, peer=BOT,
            )
            web_url = str(web.get("url") or "")
            if not web_url:
                raise RuntimeError("Empty RandomBeast WebApp URL")
            outcome = await self._browser_flow(acc.id, web_url, acc.proxy or self.config.global_proxy)
            learned = unique_channels([*target.channels, *outcome.required_channels])
            new_channels = [channel for channel in learned if channel not in target.channels]
            if new_channels:
                target.channels = learned
                log.info("%s: learned %s required channel(s): %s", acc.id, len(new_channels), new_channels)
                await self._join_channels(client, acc, new_channels)
                # The current page was opened before the new subscriptions.
                # Reopen it once so the bot can verify this account.
                outcome = await self._browser_flow(acc.id, web_url, acc.proxy or self.config.global_proxy)
            return outcome

    async def _join_channels(self, client, acc: AccountRecord, channels: list[str]) -> None:
        for channel in channels:
            try:
                await client.join_chat(channel)
            except Exception as exc:
                if is_long_raffle_flood_wait(exc):
                    raise
                error = short_error(exc)
                if "already" not in error.lower() and "participant" not in error.lower():
                    log.warning("%s join %s: %s", acc.id, channel, error)
            await human_delay(0.3, 1.0)
            try:
                from utils.rate_limit import mute_and_archive
                await mute_and_archive(client.client, channel)
            except Exception:
                pass

    async def _browser_flow(self, acc_id: str, web_url: str, proxy_url: str | None) -> _Outcome:
        from playwright.async_api import async_playwright

        text = ""
        status = "unknown"
        required_channels: list[str] = []
        ref_link: str | None = None
        ref_count: int | None = None

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                proxy=playwright_proxy(proxy_url),
            )
            ctx = await browser.new_context(
                viewport={"width": 480, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Mobile Safari/537.36"
                )
            )
            page = await ctx.new_page()

            # Mock Telegram WebApp platform to bypass "unknown" platform blocks
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.Telegram = window.Telegram || {};
                window.Telegram.WebApp = window.Telegram.WebApp || {};
                window.Telegram.WebApp.platform = 'android';
            """)

            try:
                await page.goto(web_url, wait_until="domcontentloaded", timeout=45_000)
                await asyncio.sleep(4)
                log.info("%s: page loaded", acc_id)

                last_state = ""
                stagnant_checks = 0
                continue_clicked = False
                for _i in range(60):
                    await asyncio.sleep(1)
                    try:
                        text = (await page.inner_text("body")).strip()[:600]
                    except Exception:
                        continue

                    state = re.sub(r"\d+", "#", text.lower())
                    if state == last_state:
                        stagnant_checks += 1
                    else:
                        stagnant_checks = 0
                        last_state = state
                        log.info("%s: text=%s", acc_id, text[:120].replace("\n", " "))

                    if stagnant_checks >= 25:
                        # Before failing, let's check if there's a ref link anyway.
                        try:
                            content_str = await page.content()
                            import re as re_mod
                            match = re_mod.search(r'https?://(?:t\.me|telegram\.me)/[a-zA-Z0-9_]+bot\?(?:start|startapp)=[a-zA-Z0-9_-]+', content_str)
                            if match:
                                ref_link = match.group(0)
                                status = "ok"
                                log.info("%s: found referral link %s", acc_id, ref_link)
                                break
                        except Exception:
                            pass
                        status = "error"
                        text = "page did not advance after its last action"
                        try:
                            html = await page.content()
                            log.warning("%s: timed out HTML: %s", acc_id, html[:1000])
                        except Exception:
                            pass
                        break

                    page_channels = await self._page_required_channels(page)
                    if page_channels:
                        required_channels = unique_channels([*required_channels, *page_channels])

                    # subscription required page
                    if ("subscribe" in text.lower() or "подпис" in text.lower()) and required_channels:
                        status = "need_channels"
                        break

                    if any(w in text.lower() for w in ("участвуете", "участник", "приняли участие", "congratulations", "successfully", "success!", "you are in the giveaway", "you are in")):
                        status = "ok"; break
                    if "уже" in text.lower() and any(w in text.lower() for w in ("участ", "приняли")):
                        status = "already"; break
                    if "already" in text.lower() and any(w in text.lower() for w in ("participat", "joined", "entered")):
                        status = "already"; break
                    if "подпис" in text.lower() and "канал" in text.lower():
                        status = "need_channels"; break

                    # Step 2: Submit + captcha (check BEFORE Continue)
                    if "characters" in text.lower() or "символ" in text.lower() or "captcha" in text.lower():
                        submit_btn = await page.query_selector("button:has-text('Submit')")
                        captcha_img = await page.query_selector("img[alt='Captcha']")
                        if submit_btn and captcha_img:
                            try:
                                img_bytes = await captcha_img.screenshot(type="png")
                                answer = await _solve(img_bytes)
                                if answer:
                                    log.info("%s: captcha=%s", acc_id, answer)
                                    inputs = await page.query_selector_all("input")
                                    for idx, ch in enumerate(answer[:5]):
                                        if idx < len(inputs):
                                            await inputs[idx].click(force=True)
                                            await inputs[idx].fill(ch)
                                    await submit_btn.click(force=True)
                                    await asyncio.sleep(4)
                                    continue
                            except Exception as e:
                                log.warning("%s: captcha err %s", acc_id, short_error(e))

                    # Step 1: Continue (only if no Submit)
                    if "continue" in text.lower() and "submit" not in text.lower():
                        btn = await page.query_selector("button:has-text('Continue')")
                        if btn and not continue_clicked:
                            try:
                                if await btn.is_visible():
                                    await btn.click(force=True)
                                    log.info("%s: clicked Continue", acc_id)
                                    continue_clicked = True
                                    stagnant_checks = 0
                                    await asyncio.sleep(4)
                                    continue_clicked = False  # allow re-click on multi-step pages
                                    continue
                            except Exception:
                                pass


                else:
                    status = "error"
                    if not text:
                        text = "timeout"
                        
                if status in ("ok", "already") and not ref_link:
                    try:
                        content_str = await page.content()
                        import re as re_mod
                        match = re_mod.search(r'https?://(?:t\.me|telegram\.me)/[a-zA-Z0-9_]+bot\?(?:start|startapp)=[a-zA-Z0-9_-]+', content_str)
                        if match:
                            ref_link = match.group(0)
                            log.info("%s: found referral link %s", acc_id, ref_link)
                            
                            try:
                                page_text = (await page.inner_text("body")).lower()
                                fraction_match = re_mod.search(r'\d+\s*/\s*(\d+)', page_text)
                                if fraction_match:
                                    ref_count = int(fraction_match.group(1))
                                else:
                                    cnt_match = re_mod.search(r'(?:invite|пригласи(?:ть)?)\D*?(\d+)', page_text)
                                    if cnt_match:
                                        ref_count = int(cnt_match.group(1))
                            except Exception:
                                pass
                    except Exception:
                        pass

            except Exception as exc:
                status = "error"
                text = short_error(exc)
            finally:
                await browser.close()

        m = {"ok": "joined", "already": "already joined", "need_channels": "NOT SUBSCRIBED", "banned": "banned"}
        return _Outcome(status, m.get(status, text or "unknown"), required_channels, ref_link, ref_count)

    @staticmethod
    async def _page_required_channels(page) -> list[str]:
        try:
            hrefs = await page.locator("a[href]").evaluate_all("elements => elements.map(item => item.href)")
        except Exception:
            return []
        channels: list[str] = []
        for href in hrefs:
            value = str(href or "")
            channels.extend(extract_channels(value))
            if "t.me/+" in value.lower() or "joinchat/" in value.lower():
                channels.append(value)
        return unique_channels(channels)


@dataclass
class _Outcome:
    status: str
    message: str
    required_channels: list[str] = field(default_factory=list)
    ref_link: str | None = None
    ref_count: int | None = None


def _extract_start(value: str) -> str | None:
    m = START_RE.search(value or "")
    if m: return unquote(m.group(1))
    return None


def _looks_like_post(v: str) -> bool:
    return looks_like_post_link(v)


def _normalize(v: str) -> str:
    return normalize_post_link(v)


async def _solve(img_bytes: bytes) -> str | None:
    import asyncio as _a

    def _run():
        try:
            import ddddocr
            ocr = ddddocr.DdddOcr(show_ad=False)
            result = ocr.classification(img_bytes)
            if result and len(result) >= 3:
                return result[:5]
        except Exception:
            pass

        import shutil
        import subprocess
        import tempfile
        import re as _re
        from pathlib import Path

        candidates = (
            "/usr/bin/tesseract",
            "/usr/local/bin/tesseract",
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            "tesseract",
        )
        tesseract_bin = next(
            (
                candidate for candidate in candidates
                if shutil.which(candidate) is not None or Path(candidate).is_file()
            ),
            None,
        )
        if not tesseract_bin:
            log.warning("No local captcha OCR: install Tesseract or add ddddocr to the active venv")
            return None

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.png"
            p.write_bytes(img_bytes)
            for psm in ("7", "8", "6"):
                try:
                    r = subprocess.run(
                        [
                            tesseract_bin, str(p), "stdout", "--psm", psm,
                            "-c", "tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
                        ],
                        capture_output=True, text=True, timeout=15,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    log.warning("Tesseract captcha OCR failed: %s", exc)
                    return None
                dg = _re.sub(r"[^a-zA-Z0-9]", "", r.stdout or "")
                if len(dg) >= 4:
                    return dg[:5]
            dg = _re.sub(r"[^a-zA-Z0-9]", "", r.stdout or "")
            return dg[:5] if dg else None

    return await _a.to_thread(_run)
