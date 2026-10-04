from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.telegram_client import create_client, parse_post_link
from modules.raffle_random import looks_like_post_link, normalize_post_link, extract_channels
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from modules.raffle_common import (
    configure_raffle_client,
    is_long_raffle_flood_wait,
    raffle_flood_skip_message,
)
from utils.rate_limit import human_delay
from utils.telegram_errors import is_invalid_auth_error, short_error

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
        log.info("BestRandom start_param=%s channels=%s", parsed.start_param, parsed.channels)

        sem = asyncio.Semaphore(self.config.max_concurrency or 1)
        result = ActionResult()

        async def worker(acc: AccountRecord) -> None:
            async with sem:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                try:
                    outcome = await asyncio.wait_for(self._participate_one(acc, parsed), timeout=180)
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
                except asyncio.TimeoutError:
                    result.add_error(acc.id, "timeout")
                    if progress:
                        progress.mark_error(acc.id)
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

        await asyncio.gather(*(worker(a) for a in enabled))
        return result

    async def _resolve_target(
        self, target: str, extra: list[str], probe_account: AccountRecord | None = None,
    ) -> BestRandomTarget:
        raw = target.strip()
        if not raw:
            raise ValueError("Empty target")

        start_param = _extract_start_param(raw)
        channels: list[str] = []
        post_link = None

        if _looks_like_post_link(raw):
            post_link = _normalize(raw)
            return await self._resolve_from_post(post_link, extra, probe_account)

        if not start_param:
            raise ValueError("Could not extract start= param. Provide full t.me/BestRandom_bot?start=... link")

        for item in extra:
            channels.extend(extract_channels(item))
        channels = list(dict.fromkeys(channels))

        return BestRandomTarget(source=raw, start_param=start_param, channels=channels, post_link=post_link)

    async def _resolve_from_post(
        self, post_link: str, extra: list[str], probe_account: AccountRecord | None = None,
    ) -> BestRandomTarget:
        raw = _normalize(post_link)
        start_param = _extract_start_param(raw)
        acc = probe_account or self._first_account()
        if acc is None:
            raise RuntimeError("No enabled accounts")

        channels = list(extract_channels(" ".join(extra)))
        async with create_client(self.config, acc) as client:
            configure_raffle_client(client)
            detail = await client.get_message_detail(raw)
            text = str(detail.get("text") or "")
            channels.extend(extract_channels(text))
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
        return BestRandomTarget(source=raw, start_param=start_param, channels=list(dict.fromkeys(channels)), post_link=raw)

    async def _participate_one(self, acc: AccountRecord, target: BestRandomTarget) -> JoinOutcome:
        async with create_client(self.config, acc) as client:
            configure_raffle_client(client)
            for ch in target.channels:
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
                    if "already" not in err.lower():
                        log.warning("%s join %s: %s", acc.id, ch, err)
                await human_delay(0.3, 1.0)

            await client.send_message(BESTRANDOM_BOT, f"/start {target.start_param}")
            log.info("%s: sent /start", acc.id)

            async for m in client.client.get_chat_history(BESTRANDOM_BOT, limit=2):
                if m.outgoing:
                    sent_id = m.id
                    break
            else:
                sent_id = 0

            captcha_msg = None
            for subscription_attempt in range(2):
                captcha_msg = await self._wait_for_photo(client, after_id=sent_id, timeout=25)
                if captcha_msg is not None:
                    break
                required_msg = await self._subscription_message(client, after_id=sent_id)
                if required_msg is None:
                    break
                joined = await self._join_required_channels(client, acc, required_msg)
                if not joined:
                    return JoinOutcome("error", self._message_text(required_msg)[:150])
                await self._check_subscription(client, required_msg)
                await human_delay(1.0, 2.0)
                await client.send_message(BESTRANDOM_BOT, f"/start {target.start_param}")
                log.info("%s: retrying after joining %s required channel(s)", acc.id, len(joined))
                await asyncio.sleep(2)
                sent_id = await self._last_outgoing_id(client, fallback=sent_id)

            if captcha_msg is None:
                # check for error text
                async for m in client.client.get_chat_history(BESTRANDOM_BOT, limit=3):
                    if m.id > int(sent_id) and not m.outgoing:
                        text = m.text or m.caption or ""
                        if "уже" in text.lower() and any(w in text.lower() for w in ("участ", "участвуете")):
                            return JoinOutcome("already", text[:150])
                        if any(w in text.lower() for w in ("завершен", "закончен", "отменен")):
                            return JoinOutcome("error", text[:150])
                        if text.strip():
                            return JoinOutcome("error", text[:150])
                return JoinOutcome("error", "no captcha photo received")

            photo_bytes = await self._download_photo(client, captcha_msg)
            if photo_bytes is None:
                return JoinOutcome("error", "failed to download captcha")

            answer = await self._solve_captcha(photo_bytes)
            log.info("%s: captcha answer = %s", acc.id, answer)
            await human_delay(0.5, 1.5)
            await client.send_message(BESTRANDOM_BOT, answer)

            for retry in range(3):
                outcome = await self._wait_result(client, after_id=captcha_msg.id, timeout=12)
                if outcome.status != "retry":
                    return outcome
                if retry >= 2:
                    return JoinOutcome("error", "captcha retries exhausted")
                next_captcha = await self._wait_for_photo(client, after_id=captcha_msg.id + 1, timeout=10)
                if next_captcha is None:
                    return JoinOutcome("error", "no new captcha after wrong answer")
                photo_bytes = await self._download_photo(client, next_captcha)
                if photo_bytes is None:
                    return JoinOutcome("error", "failed to download retry captcha")
                answer = await self._solve_captcha(photo_bytes)
                log.info("%s: retry captcha answer = %s", acc.id, answer)
                await human_delay(0.3, 1.0)
                await client.send_message(BESTRANDOM_BOT, answer)
                captcha_msg = next_captcha
            return JoinOutcome("error", "captcha retries exhausted")

    async def _wait_for_photo(self, client, *, after_id, timeout: float) -> Any:
        for _ in range(int(timeout)):
            await asyncio.sleep(1)
            from pyrogram.types import Message
            async for m in client.client.get_chat_history(BESTRANDOM_BOT, limit=3):
                if isinstance(m, Message) and m.id > after_id and not m.outgoing and m.photo:
                    return m
        return None

    async def _subscription_message(self, client, *, after_id: int) -> Any | None:
        async for msg in client.client.get_chat_history(BESTRANDOM_BOT, limit=5):
            if msg.id <= int(after_id) or msg.outgoing:
                continue
            text = self._message_text(msg).lower()
            if "подпис" in text and "канал" in text:
                return msg
        return None

    @staticmethod
    def _message_text(msg: Any) -> str:
        return str(getattr(msg, "text", None) or getattr(msg, "caption", None) or "")

    @staticmethod
    def _required_channels(msg: Any) -> list[str]:
        channels: list[str] = []
        markup = getattr(msg, "reply_markup", None)
        for row in getattr(markup, "inline_keyboard", None) or []:
            for button in row:
                url = str(getattr(button, "url", None) or getattr(button, "web_app", None) or "")
                if not url:
                    continue
                channels.extend(extract_channels(url))
                if "t.me/+" in url.lower() or "joinchat/" in url.lower():
                    channels.append(url)
        return list(dict.fromkeys(channels))

    async def _join_required_channels(self, client, acc: AccountRecord, msg: Any) -> list[str]:
        joined: list[str] = []
        for channel in self._required_channels(msg):
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
            await human_delay(0.4, 1.0)
        return joined

    async def _check_subscription(self, client, msg: Any) -> None:
        markup = getattr(msg, "reply_markup", None)
        for row in getattr(markup, "inline_keyboard", None) or []:
            for button in row:
                label = str(getattr(button, "text", None) or "").lower()
                callback_data = getattr(button, "callback_data", None)
                if not callback_data or not any(word in label for word in ("провер", "check", "готов")):
                    continue
                try:
                    await client.client.request_callback_answer(
                        BESTRANDOM_BOT, msg.id, callback_data, timeout=10,
                    )
                except Exception as exc:
                    if is_long_raffle_flood_wait(exc):
                        raise
                    log.debug("subscription check callback: %s", short_error(exc))
                return

    async def _last_outgoing_id(self, client, *, fallback: int) -> int:
        async for msg in client.client.get_chat_history(BESTRANDOM_BOT, limit=5):
            if msg.outgoing:
                return int(msg.id)
        return fallback

    async def _download_photo(self, client, msg) -> bytes | None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "captcha.jpg"
            try:
                await client.client.download_media(msg.photo, file_name=str(path))
                return path.read_bytes() if path.is_file() else None
            except Exception as exc:
                if is_long_raffle_flood_wait(exc):
                    raise
                log.warning("download captcha: %s", short_error(exc))
                return None

    async def _solve_captcha(self, image: bytes) -> str:
        digits = await asyncio.to_thread(_tesseract_digits, image)
        if digits and len(digits) >= 3:
            log.info("Captcha tesseract: %s", digits)
            return digits[:4]
        log.warning("OCR weak/captcha unreadable, sending best guess")
        return (digits or "0000")[:4].zfill(4)

    async def _wait_result(self, client, *, after_id, timeout: float) -> JoinOutcome:
        for _ in range(int(timeout)):
            await asyncio.sleep(1)
            async for m in client.client.get_chat_history(BESTRANDOM_BOT, limit=3):
                if m.id > int(after_id) and not m.outgoing:
                    text = m.text or m.caption or ""
                    if "участник" in text.lower() and any(w in text.lower() for w in ("теперь", "стали", "вы")):
                        return JoinOutcome("ok", text[:150])
                    if "уже" in text.lower() and any(w in text.lower() for w in ("участ", "участвуете")):
                        return JoinOutcome("already", text[:150])
                    if "цифры" in text.lower() or "какие числа" in text.lower():
                        return JoinOutcome("retry", "wrong answer, retry")
                    if any(w in text.lower() for w in ("неверн", "неправильн", "ошиб", "error", "wrong")):
                        return JoinOutcome("retry", f"wrong captcha: {text[:100]}")
                    if any(w in text.lower() for w in ("завершен", "закончен", "отменен")):
                        return JoinOutcome("error", text[:150])
            await asyncio.sleep(0.5)
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
        try:
            proc = subprocess.run(
                [tesseract_bin, str(path), "stdout", "--psm", "7", "-c", "tessedit_char_whitelist=0123456789"],
                capture_output=True, text=True, timeout=20,
            )
        except Exception:
            return None
        digits = re.sub(r"\D", "", proc.stdout or "")
        log.debug("tesseract raw: %r → digits: %s", proc.stdout, digits)
        if len(digits) >= 3:
            return digits[:4]
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
        if shutil.which(c) or Path(c).is_file():
            return c
    return None
