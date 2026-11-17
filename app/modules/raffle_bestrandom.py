from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.telegram_client import create_client
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from modules.captcha_support import CaptchaSupportStore, ManualRequired, manual_notice
from modules.raffle_common import (
    configure_raffle_client,
    is_long_raffle_flood_wait,
    raffle_flood_skip_message,
    solve_ddddocr,
    stop_aware_delay,
    stop_aware_sleep,
)
from modules.raffle_random import (
    _solvecaptcha_digits,
    extract_channel_targets,
    extract_channels,
    looks_like_post_link,
    normalize_post_link,
    unique_channels,
)
from utils.telegram_errors import is_frozen_error, is_invalid_auth_error, short_error

BESTRANDOM_BOT = "BestRandom_bot"
START_PARAM_RE = re.compile(r"start=(.+?)(?:&|$)", re.IGNORECASE)

log = logging.getLogger("raffle-bestrandom")


@dataclass
class BestRandomTarget:
    source: str
    start_param: str
    channels: list[str] = field(default_factory=list)
    post_link: str | None = None


@dataclass
class JoinOutcome:
    status: str
    message: str


class BestRandomService:

    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
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
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        enabled = [account for account in accounts if account.enabled]
        result = ActionResult()
        if not enabled or (stop_event is not None and stop_event.is_set()):
            return result

        parsed = await self._resolve_target(
            target, extra_channels or [], probe_account=probe_account or enabled[0],
        )
        log.info("BestRandom start_param=%s channels=%s", parsed.start_param, parsed.channels)

        sem = asyncio.Semaphore(max(1, self.config.max_concurrency or 1))

        async def worker(acc: AccountRecord) -> None:
            if stop_event is not None and stop_event.is_set():
                return
            async with sem:
                if await stop_aware_delay(self.config.min_action_delay, self.config.max_action_delay, stop_event):
                    return
                try:
                    outcome = await asyncio.wait_for(
                        self._participate_one(acc, parsed, stop_event=stop_event),
                        timeout=180,
                    )
                    status = outcome.status
                    if status in ("ok", "already"):
                        result.add_ok(acc.id)
                        result.details[acc.id] = outcome.message
                        if progress:
                            progress.mark_ok(acc.id)
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

        await asyncio.gather(*(worker(a) for a in enabled))
        return result

    async def _resolve_target(
        self, target: str, extra: list[str], probe_account: AccountRecord | None = None,
    ) -> BestRandomTarget:
        raw = target.strip()
        if not raw:
            raise ValueError("Empty target")

        start_param = _extract_start_param(raw)
        if _looks_like_post_link(raw):
            return await self._resolve_from_post(_normalize(raw), extra, probe_account)

        if not start_param:
            raise ValueError("Could not extract start= param. Provide full t.me/BestRandom_bot?start=... link")

        channels = unique_channels(extract_channel_targets(" ".join(extra)))
        return BestRandomTarget(source=raw, start_param=start_param, channels=channels, post_link=None)

    async def _resolve_from_post(
        self, post_link: str, extra: list[str], probe_account: AccountRecord | None = None,
    ) -> BestRandomTarget:
        raw = _normalize(post_link)
        start_param = _extract_start_param(raw)
        acc = probe_account or self._first_account()
        if acc is None:
            raise RuntimeError("No enabled accounts")

        channels = list(extract_channel_targets(" ".join(extra)))
        async with create_client(self.config, acc) as client:
            configure_raffle_client(client)
            detail = await client.get_message_detail(raw)
            forward_post_link = str(detail.get("forward_post_link") or "").strip()
            if forward_post_link and forward_post_link != raw:
                channels.extend(extract_channel_targets(str(detail.get("text") or "")))
                for hl in detail.get("hidden_links") or []:
                    channels.extend(extract_channel_targets(str(hl)))
                detail = await client.get_message_detail(forward_post_link)
                raw = forward_post_link

            text = str(detail.get("text") or "")
            channels.extend(extract_channel_targets(text))
            for hl in detail.get("hidden_links") or []:
                channels.extend(extract_channel_targets(str(hl)))
                if not start_param:
                    start_param = _extract_start_param(str(hl))
            if not start_param:
                for btn in detail.get("buttons") or []:
                    for field_name in ("web_app_url", "url"):
                        sp = _extract_start_param(str(btn.get(field_name) or ""))
                        if sp:
                            start_param = sp
                            break
                    if start_param:
                        break
        if not start_param:
            raise ValueError(f"Could not find start= param in post {post_link}")
        return BestRandomTarget(source=raw, start_param=start_param, channels=unique_channels(channels), post_link=raw)

    async def _participate_one(
        self,
        acc: AccountRecord,
        target: BestRandomTarget,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> JoinOutcome:
        async with create_client(self.config, acc) as client:
            configure_raffle_client(client)
            for ch in target.channels:
                if stop_event is not None and stop_event.is_set():
                    raise asyncio.CancelledError("Giveaway stopped")
                try:
                    await client.join_chat(ch)
                    try:
                        from utils.rate_limit import mute_and_archive
                        await mute_and_archive(client.client, ch)
                    except Exception:
                        pass
                except Exception as exc:
                    if is_long_raffle_flood_wait(exc):
                        raise
                    err = short_error(exc)
                    if "already" not in err.lower() and "participant" not in err.lower():
                        log.warning("%s join %s: %s", acc.id, ch, err)
                if await stop_aware_delay(0.3, 1.0, stop_event):
                    raise asyncio.CancelledError("Giveaway stopped")

            await client.send_message(BESTRANDOM_BOT, f"/start {target.start_param}")
            log.info("%s: sent /start", acc.id)
            sent_id = await self._last_outgoing_id(client, fallback=0)

            captcha_msg = None
            for _subscription_attempt in range(2):
                captcha_msg = await self._wait_for_photo(client, after_id=sent_id, timeout=25, stop_event=stop_event)
                if captcha_msg is not None:
                    break
                required_msg = await self._subscription_message(client, after_id=sent_id)
                if required_msg is None:
                    break
                joined = await self._join_required_channels(client, acc, required_msg, stop_event=stop_event)
                if not joined:
                    return JoinOutcome("error", self._message_text(required_msg)[:150])
                await self._check_subscription(client, required_msg)
                if await stop_aware_delay(1.0, 2.0, stop_event):
                    raise asyncio.CancelledError("Giveaway stopped")
                await client.send_message(BESTRANDOM_BOT, f"/start {target.start_param}")
                log.info("%s: retrying after joining %s required channel(s)", acc.id, len(joined))
                if await stop_aware_sleep(2.0, stop_event):
                    raise asyncio.CancelledError("Giveaway stopped")
                sent_id = await self._last_outgoing_id(client, fallback=sent_id)

            if captcha_msg is None:
                for m in await client.get_chat_history(BESTRANDOM_BOT, limit=5):
                    if int(m["id"]) > int(sent_id) and not m.get("outgoing"):
                        text = self._message_text(m)
                        lowered = text.lower()
                        if "уже" in lowered and any(w in lowered for w in ("участ", "участвуете")):
                            return JoinOutcome("already", text[:150])
                        if any(w in lowered for w in ("завершен", "закончен", "отменен")):
                            return JoinOutcome("error", text[:150])
                        if text.strip():
                            return JoinOutcome("error", text[:150])
                return JoinOutcome("error", "no captcha photo received")

            for retry in range(3):
                if stop_event is not None and stop_event.is_set():
                    raise asyncio.CancelledError("Giveaway stopped")
                photo_bytes = await self._download_photo(client, captcha_msg)
                if photo_bytes is None:
                    return JoinOutcome("error", "failed to download captcha")

                if getattr(getattr(self, "config", None), "data_dir", None):
                    try:
                        CaptchaSupportStore(self.config.data_dir).reserve_attempt(acc.id, "bestrandom")
                    except ManualRequired:
                        manual_notice(self.config, acc.id, "bestrandom", photo_bytes, reason="budget_exhausted", kind="image")
                        raise

                answer = await self._solve_captcha(photo_bytes)
                if not answer:
                    if getattr(getattr(self, "config", None), "data_dir", None):
                        manual_notice(self.config, acc.id, "bestrandom", photo_bytes, reason="unreadable", kind="image")
                    return JoinOutcome("error", "captcha OCR unreadable; stopped without guessing")

                log.info("%s: captcha answer = %s", acc.id, answer)
                if await stop_aware_delay(0.5, 1.5, stop_event):
                    raise asyncio.CancelledError("Giveaway stopped")
                await client.send_message(BESTRANDOM_BOT, answer)
                answer_id = await self._last_outgoing_id(client, fallback=int(captcha_msg["id"]))

                outcome = await self._wait_result(client, after_id=answer_id, timeout=14, stop_event=stop_event)
                if outcome.status != "retry":
                    return outcome
                if retry >= 2:
                    if getattr(getattr(self, "config", None), "data_dir", None):
                        manual_notice(self.config, acc.id, "bestrandom", photo_bytes, reason="unreadable", kind="image")
                    return JoinOutcome("error", "captcha retries exhausted")
                next_captcha = await self._wait_for_photo(
                    client, after_id=answer_id, timeout=10, stop_event=stop_event,
                )
                if next_captcha is None:
                    return JoinOutcome("error", "no new captcha after wrong answer")
                captcha_msg = next_captcha
            return JoinOutcome("error", "captcha retries exhausted")

    async def _wait_for_photo(
        self,
        client,
        *,
        after_id: int,
        timeout: float,
        stop_event: asyncio.Event | None = None,
    ) -> dict[str, Any] | None:
        for _ in range(int(timeout)):
            if await stop_aware_sleep(1.0, stop_event):
                raise asyncio.CancelledError("Giveaway stopped")
            for m in await client.get_chat_history(BESTRANDOM_BOT, limit=5):
                if int(m["id"]) > int(after_id) and not m.get("outgoing") and m.get("has_photo"):
                    return m
        return None

    async def _subscription_message(self, client, *, after_id: int) -> dict[str, Any] | None:
        for msg in await client.get_chat_history(BESTRANDOM_BOT, limit=6):
            if int(msg["id"]) <= int(after_id) or msg.get("outgoing"):
                continue
            text = self._message_text(msg).lower()
            if "подпис" in text and "канал" in text:
                return msg
        return None

    @staticmethod
    def _message_text(msg: Any) -> str:
        if isinstance(msg, dict):
            return str(msg.get("text") or msg.get("caption") or "")
        return str(getattr(msg, "text", None) or getattr(msg, "caption", None) or "")

    @staticmethod
    def _iter_buttons(msg: Any) -> list[dict[str, Any]]:
        if isinstance(msg, dict):
            return [btn for btn in (msg.get("buttons") or []) if isinstance(btn, dict)]
        markup = getattr(msg, "reply_markup", None)
        out: list[dict[str, Any]] = []
        for row in getattr(markup, "inline_keyboard", None) or []:
            for button in row:
                web_app = getattr(button, "web_app", None)
                out.append({
                    "text": getattr(button, "text", None),
                    "url": getattr(button, "url", None),
                    "callback_data": getattr(button, "callback_data", None),
                    "web_app_url": getattr(web_app, "url", None) if web_app else str(web_app or "") or None,
                })
        return out

    @classmethod
    def _required_channels(cls, msg: Any) -> list[str]:
        channels: list[str] = []
        for button in cls._iter_buttons(msg):
            url = str(button.get("url") or button.get("web_app_url") or "")
            if not url:
                continue
            channels.extend(extract_channels(url))
            if "t.me/+" in url.lower() or "joinchat/" in url.lower():
                channels.append(url)
        return unique_channels(channels)

    async def _join_required_channels(
        self,
        client,
        acc: AccountRecord,
        msg: Any,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> list[str]:
        joined: list[str] = []
        for channel in self._required_channels(msg):
            if stop_event is not None and stop_event.is_set():
                raise asyncio.CancelledError("Giveaway stopped")
            try:
                await client.join_chat(channel)
                joined.append(channel)
                log.info("%s: joined bot-required channel %s", acc.id, channel)
            except Exception as exc:
                if is_long_raffle_flood_wait(exc):
                    raise
                error = short_error(exc)
                if "already" in error.lower() or "participant" in error.lower():
                    joined.append(channel)
                else:
                    log.warning("%s: could not join required %s: %s", acc.id, channel, error)
            if await stop_aware_delay(0.4, 1.0, stop_event):
                raise asyncio.CancelledError("Giveaway stopped")
        return joined

    async def _check_subscription(self, client, msg: Any) -> None:
        msg_id = int(msg["id"]) if isinstance(msg, dict) else int(msg.id)
        for button in self._iter_buttons(msg):
            label = str(button.get("text") or "").lower()
            callback_data = button.get("callback_data")
            if not callback_data or not any(word in label for word in ("провер", "check", "готов")):
                continue
            try:
                await client.request_callback_answer(BESTRANDOM_BOT, msg_id, callback_data, timeout=10)
            except Exception as exc:
                if is_long_raffle_flood_wait(exc):
                    raise
                log.debug("subscription check callback: %s", short_error(exc))
            return

    async def _last_outgoing_id(self, client, *, fallback: int) -> int:
        for msg in await client.get_chat_history(BESTRANDOM_BOT, limit=5):
            if msg.get("outgoing"):
                return int(msg["id"])
        return fallback

    async def _download_photo(self, client, msg: Any) -> bytes | None:
        try:
            data = await client.download_media(msg)
            return bytes(data) if data else None
        except Exception as exc:
            if is_long_raffle_flood_wait(exc):
                raise
            log.warning("download captcha: %s", short_error(exc))
            return None

    async def _solve_captcha(self, image: bytes) -> str | None:
        digits = await asyncio.to_thread(solve_ddddocr, image, alphanumeric=False, min_len=3, max_len=6)
        if digits:
            log.info("Captcha ddddocr: %s", digits)
            return digits
        digits = await asyncio.to_thread(_tesseract_digits, image)
        if digits:
            log.info("Captcha tesseract: %s", digits)
            return digits
        if self.solvecaptcha_key:
            digits = await _solvecaptcha_digits(self.solvecaptcha_key, image)
            if digits:
                log.info("Captcha solvecaptcha: %s", digits)
                return digits
        log.warning("Captcha OCR failed/unreadable")
        return None

    async def _wait_result(
        self,
        client,
        *,
        after_id: int,
        timeout: float,
        stop_event: asyncio.Event | None = None,
    ) -> JoinOutcome:
        for _ in range(int(timeout)):
            if await stop_aware_sleep(1.0, stop_event):
                raise asyncio.CancelledError("Giveaway stopped")
            for m in await client.get_chat_history(BESTRANDOM_BOT, limit=6):
                if int(m["id"]) > int(after_id) and not m.get("outgoing"):
                    text = self._message_text(m)
                    lowered = text.lower()
                    if any(w in lowered for w in ("не можете", "нельзя", "не стали", "не удалось", "отказано", "завершен", "закончен", "отменен")):
                        return JoinOutcome("error", text[:150])
                    if "уже" in lowered and any(w in lowered for w in ("участ", "участвуете")):
                        return JoinOutcome("already", text[:150])
                    if "участник" in lowered and any(w in lowered for w in ("теперь", "стали", "вы стали", "успешно")):
                        return JoinOutcome("ok", text[:150])
                    if "цифры" in lowered or "какие числа" in lowered or any(
                        w in lowered for w in ("неверн", "неправильн", "ошиб", "error", "wrong")
                    ):
                        return JoinOutcome("retry", f"wrong captcha: {text[:100]}")
        return JoinOutcome("error", "timeout waiting for result")

    def _first_account(self) -> AccountRecord | None:
        for a in self.accounts.list_accounts(enabled_only=True):
            return a
        return None


def _extract_start_param(value: str) -> str | None:
    text = (value or "").strip()
    match = START_PARAM_RE.search(text)
    if match:
        return unquote(match.group(1))
    parsed = urlparse(text if "://" in text else f"https://{text}")
    qs = parsed.query
    if qs:
        from urllib.parse import parse_qs
        for key in ("start",):
            val = parse_qs(qs).get(key)
            if val:
                return unquote(val[0])
    return None


def _looks_like_post_link(value: str) -> bool:
    return looks_like_post_link(value)


def _normalize(value: str) -> str:
    return normalize_post_link(value)


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
        for psm in ("7", "8", "6"):
            try:
                proc = subprocess.run(
                    [tesseract_bin, str(path), "stdout", "--psm", psm, "-c", "tessedit_char_whitelist=0123456789"],
                    capture_output=True, text=True, timeout=20,
                )
            except Exception:
                return None
            digits = re.sub(r"\D", "", proc.stdout or "")
            log.debug("tesseract psm=%s raw: %r -> digits: %s", psm, proc.stdout, digits)
            if len(digits) >= 3:
                return digits[:6]
    return None


def _find_tesseract() -> str | None:
    import shutil
    from pathlib import Path
    for c in (
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "tesseract",
    ):
        p = Path(c)
        if (p.is_absolute() and p.is_file()) or shutil.which(c) is not None:
            return c
    return None
