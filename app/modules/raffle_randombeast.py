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
from modules.captcha_support import CaptchaSupportStore, ManualRequired, manual_notice
from modules.raffle_common import (
    RAFFLE_CHROME_USER_AGENT,
    ReferralCoordinator,
    configure_raffle_client,
    extract_ref_link_and_count,
    is_long_raffle_flood_wait,
    playwright_proxy_context,
    raffle_flood_skip_message,
    raffle_stealth_init_script,
    solve_ddddocr,
    stop_aware_delay,
    stop_aware_sleep,
)
from modules.raffle_random import (
    extract_channel_targets,
    extract_channels,
    looks_like_post_link,
    normalize_post_link,
    unique_channels,
)
from utils.telegram_errors import is_frozen_error, is_invalid_auth_error, short_error

BOT = "randombeast_bot"
APP_SHORT_NAME = "devapp"
START_RE = re.compile(r"startapp=([^&\s]+)", re.IGNORECASE)
APP_PATH_RE = re.compile(r"t\.me/randombeast_bot/([A-Za-z0-9_]+)\?", re.IGNORECASE)

log = logging.getLogger("raffle-randombeast")


@dataclass
class RandomBeastTarget:
    source: str
    start_param: str
    channels: list[str] = field(default_factory=list)
    post_link: str | None = None
    app_short_name: str = APP_SHORT_NAME


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
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        enabled = [account for account in accounts if account.enabled]
        result = ActionResult()
        if not enabled or (stop_event is not None and stop_event.is_set()):
            return result

        parsed = await self._resolve_target(
            target, extra_channels or [], probe_account=probe_account or enabled[0],
        )
        log.info("RandomBeast start=%s app=%s channels=%s", parsed.start_param, parsed.app_short_name, parsed.channels)

        browser_limit = min(
            self.config.max_concurrency or 1,
            getattr(self.config, "giveaway_browser_max_concurrency", None) or 3,
        )
        concurrency = max(1, browser_limit)
        sem = asyncio.Semaphore(concurrency)
        channels_lock = asyncio.Lock()
        referrals = ReferralCoordinator(parsed.start_param)
        referral_mode: bool | None = None

        async def worker(acc: AccountRecord) -> None:
            nonlocal referral_mode
            if stop_event is not None and stop_event.is_set():
                return
            async with sem:
                if await stop_aware_delay(self.config.min_action_delay, self.config.max_action_delay, stop_event):
                    return
                used_start = await referrals.acquire_start_param() or parsed.start_param
                async with channels_lock:
                    target_snapshot = RandomBeastTarget(
                        source=parsed.source,
                        start_param=used_start,
                        channels=list(parsed.channels),
                        post_link=parsed.post_link,
                        app_short_name=parsed.app_short_name,
                    )
                try:
                    outcome = await asyncio.wait_for(
                        self._participate_one(acc, target_snapshot, channels_lock=channels_lock, shared_target=parsed, stop_event=stop_event),
                        timeout=300,
                    )
                    if outcome.status in ("ok", "already"):
                        result.add_ok(acc.id)
                        result.details[acc.id] = outcome.message
                        if progress:
                            progress.mark_ok(acc.id)

                        registered = await referrals.register_ref(outcome.ref_link, outcome.ref_count)
                        if registered:
                            referral_mode = True
                            log.info("%s: queued referral startapp=%s (slots=%s)", acc.id, registered, outcome.ref_count or 1)
                        elif referral_mode is None:
                            referral_mode = False
                    else:
                        result.add_error(acc.id, outcome.message)
                        if progress:
                            progress.mark_error(acc.id)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    result.add_error(acc.id, "timeout")
                    if progress:
                        progress.mark_error(acc.id)
                except ManualRequired as exc:
                    err = short_error(exc)
                    result.add_error(acc.id, err)
                    if progress:
                        progress.mark_error(acc.id)
                    log.warning("%s manual captcha required: %s", acc.id, err)
                except Exception as exc:
                    err = (
                        raffle_flood_skip_message(exc)
                        if is_long_raffle_flood_wait(exc)
                        else short_error(exc)
                    )
                    result.add_error(acc.id, err)
                    if progress:
                        progress.mark_error(acc.id)
                    if is_invalid_auth_error(exc):
                        self.accounts.mark_error(acc.id, err, disable=True)
                    elif is_frozen_error(exc):
                        self.accounts.mark_error(acc.id, f"frozen: {err}", disable=False)

        idx = 0
        while idx < len(enabled):
            if stop_event is not None and stop_event.is_set():
                break
            if referral_mode is None:
                await worker(enabled[idx])
                idx += 1
                continue
            if referral_mode:
                async with referrals._lock:
                    head_slots = referrals._queue[0][1] if referrals._queue else 1
                batch_size = max(1, min(concurrency, head_slots))
            else:
                batch_size = concurrency
            batch = enabled[idx : idx + batch_size]
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
        app_short_name = _extract_app_short_name(raw)
        channels = list(extract_channel_targets(" ".join(extra)))
        return RandomBeastTarget(source=raw, start_param=sp, channels=channels, post_link=None, app_short_name=app_short_name)

    async def _resolve_from_post(
        self, post_link: str, extra: list[str], probe_account: AccountRecord | None = None,
    ) -> RandomBeastTarget:
        account = probe_account or self._first_account()
        if account is None:
            raise RuntimeError("No enabled accounts")
        channels = list(extract_channel_targets(" ".join(extra)))
        start_param = None
        app_short_name = APP_SHORT_NAME
        async with create_client(self.config, account) as client:
            configure_raffle_client(client)
            detail = await client.get_message_detail(post_link)
            forward_post_link = str(detail.get("forward_post_link") or "").strip()
            if forward_post_link and forward_post_link != post_link:
                channels.extend(extract_channel_targets(str(detail.get("text") or "")))
                for hl in detail.get("hidden_links") or []:
                    channels.extend(extract_channel_targets(str(hl)))
                detail = await client.get_message_detail(forward_post_link)
                post_link = forward_post_link

            channels.extend(extract_channel_targets(str(detail.get("text") or "")))
            for hl in detail.get("hidden_links") or []:
                channels.extend(extract_channel_targets(str(hl)))
                if "randombeast" in str(hl).lower() and not start_param:
                    start_param = _extract_start(str(hl))
                    app_short_name = _extract_app_short_name(str(hl))
            for button in detail.get("buttons") or []:
                for field_name in ("web_app_url", "url"):
                    value = str(button.get(field_name) or "")
                    if "randombeast" not in value.lower():
                        continue
                    sp = _extract_start(value)
                    if sp:
                        start_param = sp
                        app_short_name = _extract_app_short_name(value)
                        break
                if start_param:
                    break
        if not start_param:
            raise ValueError(f"Could not find a randombeast startapp link in post {post_link}")
        return RandomBeastTarget(
            source=post_link,
            start_param=start_param,
            channels=unique_channels(channels),
            post_link=post_link,
            app_short_name=app_short_name,
        )

    def _first_account(self) -> AccountRecord | None:
        return next(iter(self.accounts.list_accounts(enabled_only=True)), None)

    async def _participate_one(
        self,
        acc: AccountRecord,
        target: RandomBeastTarget,
        *,
        channels_lock: asyncio.Lock | None = None,
        shared_target: RandomBeastTarget | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> _Outcome:
        async with create_client(self.config, acc) as client:
            configure_raffle_client(client)

            await self._join_channels(client, acc, target.channels, stop_event=stop_event)

            if target.post_link:
                try:
                    await client.view_post(target.post_link)
                except Exception as exc:
                    if is_long_raffle_flood_wait(exc):
                        raise

            await client.send_message(BOT, "/start")
            if await stop_aware_sleep(2.0, stop_event):
                raise asyncio.CancelledError("Giveaway stopped")

            web = await client.request_bot_app_webview(
                BOT, target.app_short_name, target.start_param, peer=BOT,
            )
            web_url = str(web.get("url") or "")
            if not web_url:
                raise RuntimeError("Empty RandomBeast WebApp URL")
            outcome = await self._browser_flow(
                acc.id, web_url, acc.proxy or self.config.global_proxy,
                used_start_param=target.start_param, stop_event=stop_event,
            )
            learned = unique_channels([*target.channels, *outcome.required_channels])
            new_channels = [channel for channel in learned if channel not in target.channels]
            if new_channels:
                target.channels = learned
                if shared_target is not None and channels_lock is not None:
                    async with channels_lock:
                        shared_target.channels = unique_channels([*shared_target.channels, *new_channels])
                log.info("%s: learned %s required channel(s): %s", acc.id, len(new_channels), new_channels)
                await self._join_channels(client, acc, new_channels, stop_event=stop_event)
                outcome = await self._browser_flow(
                    acc.id, web_url, acc.proxy or self.config.global_proxy,
                    used_start_param=target.start_param, stop_event=stop_event,
                )
            return outcome

    async def _join_channels(
        self,
        client,
        acc: AccountRecord,
        channels: list[str],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        for channel in channels:
            if stop_event is not None and stop_event.is_set():
                raise asyncio.CancelledError("Giveaway stopped")
            try:
                await client.join_chat(channel)
            except Exception as exc:
                if is_long_raffle_flood_wait(exc):
                    raise
                error = short_error(exc)
                if "already" not in error.lower() and "participant" not in error.lower():
                    log.warning("%s join %s: %s", acc.id, channel, error)
            if await stop_aware_delay(0.3, 1.0, stop_event):
                raise asyncio.CancelledError("Giveaway stopped")
            try:
                from utils.rate_limit import mute_and_archive
                await mute_and_archive(client.client, channel)
            except Exception:
                pass

    async def _browser_flow(
        self,
        acc_id: str,
        web_url: str,
        proxy_url: str | None,
        *,
        used_start_param: str | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> _Outcome:
        from playwright.async_api import async_playwright

        text = ""
        status = "unknown"
        required_channels: list[str] = []
        ref_link: str | None = None
        ref_count: int | None = None

        async with playwright_proxy_context(proxy_url) as proxy_cfg, async_playwright() as p:
            browser = None
            ctx = None
            page = None
            try:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                    ],
                    proxy=proxy_cfg,
                )
                ctx = await browser.new_context(
                    viewport={"width": 412, "height": 915},
                    user_agent=RAFFLE_CHROME_USER_AGENT,
                    locale="ru-RU",
                )
                page = await ctx.new_page()
                await page.add_init_script(raffle_stealth_init_script(platform="android", language="ru-RU"))

                await page.goto(web_url, wait_until="domcontentloaded", timeout=45_000)
                if await stop_aware_sleep(4.0, stop_event):
                    raise asyncio.CancelledError("Giveaway stopped")
                log.info("%s: page loaded", acc_id)

                last_state = ""
                stagnant_checks = 0
                captcha_attempts = 0
                last_captcha_bytes: bytes | None = None
                for _i in range(60):
                    if await stop_aware_sleep(1.0, stop_event):
                        raise asyncio.CancelledError("Giveaway stopped")
                    try:
                        text = (await page.inner_text("body")).strip()[:800]
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
                        try:
                            content_str = await page.content()
                            hrefs = await page.locator("a[href]").evaluate_all("els => els.map(el => el.href)")
                        except Exception:
                            content_str, hrefs = "", []
                        found_ref, found_cnt = extract_ref_link_and_count(
                            text, [*hrefs, content_str],
                            ignore_start_params={used_start_param} if used_start_param else None,
                        )
                        if found_ref:
                            ref_link, ref_count = found_ref, found_cnt
                            status = "ok"
                            log.info("%s: found referral link %s", acc_id, ref_link)
                            break
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

                    lowered = text.lower()
                    if ("subscribe" in lowered or "подпис" in lowered) and required_channels:
                        status = "need_channels"
                        break

                    if any(w in lowered for w in ("участвуете", "участник", "приняли участие", "congratulations", "successfully", "success!", "you are in the giveaway", "you are in")):
                        status = "ok"
                        break
                    if "уже" in lowered and any(w in lowered for w in ("участ", "приняли")):
                        status = "already"
                        break
                    if "already" in lowered and any(w in lowered for w in ("participat", "joined", "entered")):
                        status = "already"
                        break
                    if "подпис" in lowered and "канал" in lowered:
                        status = "need_channels"
                        break

                    # Step 2: Submit + captcha (EN + RU, single or multi-box inputs)
                    submit_btn = await page.query_selector(
                        "button:has-text('Submit'), button:has-text('Отправить'), "
                        "button:has-text('Подтвердить'), button:has-text('Verify'), button:has-text('Проверить')"
                    )
                    captcha_img = await page.query_selector("img[alt='Captcha'], img[alt*='captcha' i], img[src^='data:image']")
                    if (submit_btn and captcha_img) or (
                        any(w in lowered for w in ("characters", "символ", "captcha", "капч")) and captcha_img
                    ):
                        if captcha_attempts >= 5:
                            status = "error"
                            text = "captcha attempts exhausted"
                            if getattr(self.config, "data_dir", None) and last_captcha_bytes:
                                manual_notice(self.config, acc_id, "randombeast", last_captcha_bytes, reason="unreadable", kind="image")
                            break
                        captcha_attempts += 1
                        try:
                            img_bytes = await captcha_img.screenshot(type="png")
                            last_captcha_bytes = img_bytes
                            if getattr(self.config, "data_dir", None):
                                try:
                                    CaptchaSupportStore(self.config.data_dir).reserve_attempt(acc_id, "randombeast")
                                except ManualRequired:
                                    manual_notice(self.config, acc_id, "randombeast", img_bytes, reason="budget_exhausted", kind="image")
                                    raise
                            answer = await _solve(img_bytes)
                            if answer:
                                log.info("%s: captcha=%s", acc_id, answer)
                                inputs = await page.locator("input:visible:not([type='hidden']):not([type='checkbox']):not([type='radio'])").element_handles()
                                if len(inputs) == 1:
                                    await inputs[0].click()
                                    await inputs[0].fill(answer)
                                elif inputs:
                                    for idx, ch in enumerate(answer[:len(inputs)]):
                                        await inputs[idx].click()
                                        await inputs[idx].fill(ch)
                                if submit_btn:
                                    await submit_btn.click()
                                elif inputs:
                                    await inputs[-1].press("Enter")
                                if await stop_aware_sleep(4.0, stop_event):
                                    raise asyncio.CancelledError("Giveaway stopped")
                                continue
                        except ManualRequired:
                            raise
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            log.warning("%s: captcha err %s", acc_id, short_error(e))

                    # Step 1: Continue (EN + RU)
                    if not submit_btn:
                        btn = await page.query_selector(
                            "button:has-text('Continue'), button:has-text('Продолжить'), "
                            "button:has-text('Далее'), button:has-text('Участвовать'), "
                            "button:has-text('Participate'), button:has-text('Join')"
                        )
                        if btn:
                            try:
                                if await btn.is_visible() and await btn.is_enabled():
                                    await btn.click()
                                    log.info("%s: clicked Continue", acc_id)
                                    stagnant_checks = 0
                                    if await stop_aware_sleep(4.0, stop_event):
                                        raise asyncio.CancelledError("Giveaway stopped")
                                    continue
                            except asyncio.CancelledError:
                                raise
                            except Exception:
                                pass
                else:
                    status = "error"
                    if not text:
                        text = "timeout"

                if status in ("ok", "already") and not ref_link:
                    try:
                        content_str = await page.content()
                        hrefs = await page.locator("a[href]").evaluate_all("els => els.map(el => el.href)")
                        page_text = await page.inner_text("body")
                    except Exception:
                        content_str, hrefs, page_text = "", [], text
                    ref_link, ref_count = extract_ref_link_and_count(
                        page_text, [*hrefs, content_str],
                        ignore_start_params={used_start_param} if used_start_param else None,
                    )
                    if ref_link:
                        log.info("%s: found referral link %s (needed=%s)", acc_id, ref_link, ref_count)

            except asyncio.CancelledError:
                raise
            except ManualRequired:
                raise
            except Exception as exc:
                status = "error"
                text = short_error(exc)
            finally:
                for closer in (page, ctx, browser):
                    if closer is not None:
                        try:
                            await closer.close()
                        except Exception:
                            pass

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
    if m:
        return unquote(m.group(1))
    return None


def _extract_app_short_name(value: str) -> str:
    m = APP_PATH_RE.search(value or "")
    return m.group(1) if m else APP_SHORT_NAME


def _looks_like_post(v: str) -> bool:
    return looks_like_post_link(v)


def _normalize(v: str) -> str:
    return normalize_post_link(v)


async def _solve(img_bytes: bytes) -> str | None:
    def _run():
        result = solve_ddddocr(img_bytes, alphanumeric=True, min_len=3, max_len=5)
        if result:
            return result

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
                if (Path(candidate).is_absolute() and Path(candidate).is_file()) or shutil.which(candidate) is not None
            ),
            None,
        )
        if not tesseract_bin:
            log.warning("No local captcha OCR: install Tesseract or add ddddocr to the active venv")
            return None

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.png"
            p.write_bytes(img_bytes)
            r = None
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
            dg = _re.sub(r"[^a-zA-Z0-9]", "", (r.stdout if r else "") or "")
            return dg[:5] if dg else None

    return await asyncio.to_thread(_run)
