from __future__ import annotations

import asyncio
import logging
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
    ReferralCoordinator,
    configure_raffle_client,
    extract_ref_link_and_count,
    is_long_raffle_flood_wait,
    raffle_flood_skip_message,
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

BOT = "FastGiveawaysBot"
START_RE = re.compile(r"start=(.+?)(?:&|$)", re.IGNORECASE)
CAPTCHA_CB_RE = re.compile(r"contest_join\*captcha\*(\d+)")

log = logging.getLogger("raffle-fastgiveaway")


@dataclass
class FastGiveawayTarget:
    source: str
    start_param: str
    channels: list[str] = field(default_factory=list)
    post_link: str | None = None


class FastGiveawayService:

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
        log.info("FastGiveaway start=%s channels=%s", parsed.start_param, parsed.channels)

        concurrency = max(1, self.config.max_concurrency or 1)
        sem = asyncio.Semaphore(concurrency)
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
                target_snapshot = FastGiveawayTarget(
                    source=parsed.source,
                    start_param=used_start,
                    channels=list(parsed.channels),
                    post_link=parsed.post_link,
                )
                try:
                    outcome = await asyncio.wait_for(
                        self._participate_one(acc, target_snapshot, stop_event=stop_event),
                        timeout=180,
                    )
                    if outcome.status in ("ok", "already"):
                        result.add_ok(acc.id)
                        result.details[acc.id] = outcome.message
                        if progress:
                            progress.mark_ok(acc.id)
                        registered = await referrals.register_ref(outcome.ref_link, outcome.ref_count)
                        if registered:
                            referral_mode = True
                            log.info("%s: queued referral start=%s (slots=%s)", acc.id, registered, outcome.ref_count or 1)
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
    ) -> FastGiveawayTarget:
        raw = target.strip()
        if not raw:
            raise ValueError("Empty target")
        start_param = _extract_start(raw)
        if _looks_like_post(raw):
            return await self._resolve_from_post(_normalize(raw), extra, start_param, probe_account)
        if not start_param:
            raise ValueError("Could not extract start= param. Provide t.me/FastGiveawaysBot?start=... link")
        channels = unique_channels(extract_channel_targets(" ".join(extra)))
        return FastGiveawayTarget(source=raw, start_param=start_param, channels=channels)

    async def _resolve_from_post(
        self,
        post_link: str,
        extra: list[str],
        existing_sp: str | None,
        probe_account: AccountRecord | None = None,
    ) -> FastGiveawayTarget:
        raw = _normalize(post_link)
        start_param = existing_sp or _extract_start(raw)
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
                    start_param = _extract_start(str(hl))
            if not start_param:
                for btn in detail.get("buttons") or []:
                    for field_name in ("web_app_url", "url"):
                        sp = _extract_start(str(btn.get(field_name) or ""))
                        if sp:
                            start_param = sp
                            break
                    if start_param:
                        break
        if not start_param:
            raise ValueError(f"Could not find a FastGiveawaysBot start= link in post {post_link}")
        return FastGiveawayTarget(
            source=raw,
            start_param=start_param,
            channels=unique_channels(channels),
            post_link=raw,
        )

    async def _participate_one(
        self,
        acc: AccountRecord,
        target: FastGiveawayTarget,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> _Outcome:
        async with create_client(self.config, acc) as client:
            configure_raffle_client(client)
            await self._ensure_lang(client, stop_event=stop_event)

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

            await client.send_message(BOT, f"/start {target.start_param}")
            if await stop_aware_sleep(2.0, stop_event):
                raise asyncio.CancelledError("Giveaway stopped")

            last_id = await self._last_outgoing_id(client)
            seen_buttons: set[str] = set()
            subscription_retries = 0
            captcha_msg_id: int | None = None

            for attempt in range(10):
                if stop_event is not None and stop_event.is_set():
                    raise asyncio.CancelledError("Giveaway stopped")
                bot_message = await self._last_bot_message(client, after_id=last_id)
                text = self._message_text(bot_message)
                outcome = self._classify_outcome(bot_message, text, target.start_param)
                if outcome is not None:
                    return outcome

                lowered = text.lower()
                if "подписк" in lowered:
                    joined = await self._join_required_channels(client, acc, bot_message, stop_event=stop_event)
                    if joined and subscription_retries < 2:
                        subscription_retries += 1
                        await self._check_subscription(client, bot_message)
                        if await stop_aware_delay(1.0, 2.0, stop_event):
                            raise asyncio.CancelledError("Giveaway stopped")
                        await client.send_message(BOT, f"/start {target.start_param}")
                        log.info("%s: retrying after joining %s bot-required channel(s)", acc.id, len(joined))
                        if await stop_aware_sleep(2.0, stop_event):
                            raise asyncio.CancelledError("Giveaway stopped")
                        last_id = await self._last_outgoing_id(client)
                        continue
                    return _Outcome("error", text[:150])
                if "отказано" in lowered:
                    return _Outcome("error", text[:150])

                captcha = await self._find_captcha_msg(
                    client, after_id=last_id, known_captcha_id=captcha_msg_id, timeout=5, stop_event=stop_event,
                )
                if captcha is None:
                    if attempt == 0 and not text.strip():
                        return _Outcome("error", "no bot response")
                    if await stop_aware_sleep(2.0, stop_event):
                        raise asyncio.CancelledError("Giveaway stopped")
                    continue

                captcha_msg_id = int(captcha["id"])
                question = self._message_text(captcha).strip()
                log.info("%s: captcha attempt %d: %s", acc.id, attempt + 1, question[:100])

                if getattr(getattr(self, "config", None), "data_dir", None):
                    try:
                        CaptchaSupportStore(self.config.data_dir).reserve_attempt(acc.id, "fastgiveaway")
                    except ManualRequired:
                        manual_notice(self.config, acc.id, "fastgiveaway", question.encode("utf-8")[:256] or b"emoji-captcha", reason="budget_exhausted", kind="unknown")
                        raise

                emoji_ids, cb_map = self._parse_buttons(captcha)
                target_name = _extract_target_emoji(question)
                alt_map = await client.get_custom_emoji_documents(emoji_ids) if emoji_ids else {}
                log.info("%s: target=%s alts=%s", acc.id, target_name, {
                    eid: alt_map.get(eid, "?") for eid in emoji_ids
                })
                chosen_cb = _pick_callback(cb_map, alt_map, target_name, question, exclude=seen_buttons)
                if chosen_cb is None:
                    chosen_cb = next((cb for cb in cb_map.values() if cb not in seen_buttons), None)
                if chosen_cb is None:
                    if getattr(getattr(self, "config", None), "data_dir", None):
                        manual_notice(self.config, acc.id, "fastgiveaway", question.encode("utf-8")[:256] or b"emoji-captcha", reason="unreadable", kind="unknown")
                    return _Outcome("error", "no unseen captcha buttons")

                seen_buttons.add(chosen_cb)
                if await stop_aware_delay(0.5, 1.5, stop_event):
                    raise asyncio.CancelledError("Giveaway stopped")
                try:
                    await client.request_callback_answer(BOT, captcha_msg_id, chosen_cb, timeout=10)
                except Exception as exc:
                    if is_long_raffle_flood_wait(exc):
                        raise
                    log.warning("%s: btn err %s", acc.id, short_error(exc))

                if await stop_aware_sleep(2.0, stop_event):
                    raise asyncio.CancelledError("Giveaway stopped")
                last_id = captcha_msg_id

            final_msg = await self._last_bot_message(client, after_id=last_id)
            final_text = self._message_text(final_msg)
            final_outcome = self._classify_outcome(final_msg, final_text, target.start_param)
            if final_outcome is not None:
                return final_outcome
            if getattr(getattr(self, "config", None), "data_dir", None):
                manual_notice(self.config, acc.id, "fastgiveaway", final_text.encode("utf-8")[:256] or b"emoji-captcha", reason="unreadable", kind="unknown")
            return _Outcome("error", "captcha attempts exhausted")

    def _classify_outcome(self, bot_message: Any, text: str, used_start_param: str) -> _Outcome | None:
        lowered = text.lower()
        if any(w in lowered for w in ("успешно", "приняли участие", "участвуете", "приглашайте", "invite", "скопировать")):
            links = self._message_links(bot_message)
            ref_link, ref_count = extract_ref_link_and_count(
                text, links, ignore_start_params={used_start_param},
            )
            return _Outcome("ok", text[:200], ref_link, ref_count)
        if "уже" in lowered and any(w in lowered for w in ("приняли", "участ")):
            return _Outcome("already", text[:200])
        return None

    async def _ensure_lang(self, client, *, stop_event: asyncio.Event | None = None) -> None:
        history = await client.get_chat_history(BOT, limit=8)
        for m in history:
            text = self._message_text(m).lower()
            buttons = m.get("buttons") or []
            if "выберите язык" in text and buttons:
                cb = buttons[0].get("callback_data")
                if cb:
                    try:
                        await client.request_callback_answer(BOT, int(m["id"]), cb, timeout=8)
                    except Exception:
                        pass
                return
        if history:
            # Existing chat already configured; do not pollute history with bare /start.
            return
        try:
            await client.send_message(BOT, "/start")
            if await stop_aware_sleep(2.0, stop_event):
                raise asyncio.CancelledError("Giveaway stopped")
            for m in await client.get_chat_history(BOT, limit=5):
                text = self._message_text(m).lower()
                buttons = m.get("buttons") or []
                if "выберите язык" in text and buttons:
                    cb = buttons[0].get("callback_data")
                    if cb:
                        await client.request_callback_answer(BOT, int(m["id"]), cb, timeout=8)
                        await stop_aware_sleep(1.0, stop_event)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _find_captcha_msg(
        self,
        client,
        *,
        after_id: int,
        known_captcha_id: int | None,
        timeout: float,
        stop_event: asyncio.Event | None = None,
    ) -> dict[str, Any] | None:
        for _ in range(int(timeout)):
            if await stop_aware_sleep(1.0, stop_event):
                raise asyncio.CancelledError("Giveaway stopped")
            for m in await client.get_chat_history(BOT, limit=8):
                if m.get("outgoing"):
                    continue
                msg_id = int(m["id"])
                is_newer = msg_id > int(after_id)
                is_edited_current = (
                    known_captcha_id is not None
                    and msg_id == int(known_captcha_id)
                    and m.get("edit_date") is not None
                )
                if not (is_newer or is_edited_current):
                    continue
                for btn in m.get("buttons") or []:
                    if "captcha" in str(btn.get("callback_data") or ""):
                        return m
        return None

    @staticmethod
    def _iter_buttons(message: Any) -> list[dict[str, Any]]:
        if isinstance(message, dict):
            return [btn for btn in (message.get("buttons") or []) if isinstance(btn, dict)]
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None) or []
        out: list[dict[str, Any]] = []
        for row in rows:
            for btn in row:
                copy_text = getattr(btn, "copy_text", None)
                web_app = getattr(btn, "web_app", None)
                out.append({
                    "text": getattr(btn, "text", None),
                    "url": getattr(btn, "url", None),
                    "callback_data": getattr(btn, "callback_data", None),
                    "web_app_url": getattr(web_app, "url", None) if web_app else None,
                    "copy_text": getattr(copy_text, "text", copy_text) if copy_text else None,
                    "switch_inline_query": getattr(btn, "switch_inline_query", None),
                })
        return out

    @classmethod
    def _message_links(cls, message: Any) -> list[str]:
        links: list[str] = []
        for btn in cls._iter_buttons(message):
            for key in ("url", "web_app_url", "copy_text", "switch_inline_query"):
                val = btn.get(key)
                if val:
                    links.append(str(val))
        return links

    def _parse_buttons(self, msg: Any) -> tuple[list[int], dict[int, str]]:
        emoji_ids: list[int] = []
        cb_map: dict[int, str] = {}
        for btn in self._iter_buttons(msg):
            cb = str(btn.get("callback_data") or "")
            m = CAPTCHA_CB_RE.search(cb)
            if m:
                eid = int(m.group(1))
                emoji_ids.append(eid)
                cb_map[eid] = cb
        return emoji_ids, cb_map

    async def _last_outgoing_id(self, client) -> int:
        for m in await client.get_chat_history(BOT, limit=3):
            if m.get("outgoing"):
                return int(m["id"])
        return 0

    async def _last_bot_message(self, client, *, after_id: int) -> dict[str, Any] | None:
        for m in await client.get_chat_history(BOT, limit=5):
            if int(m["id"]) > int(after_id) and not m.get("outgoing"):
                return m
        return None

    @staticmethod
    def _message_text(message: Any) -> str:
        if isinstance(message, dict):
            return str(message.get("text") or message.get("caption") or "")
        return str(getattr(message, "text", None) or getattr(message, "caption", None) or "")

    @classmethod
    def _required_channels(cls, message: Any) -> list[str]:
        channels: list[str] = []
        for button in cls._iter_buttons(message):
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
        message: Any,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> list[str]:
        joined: list[str] = []
        for channel in self._required_channels(message):
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

    async def _check_subscription(self, client, message: Any) -> None:
        msg_id = int(message["id"]) if isinstance(message, dict) else int(message.id)
        for button in self._iter_buttons(message):
            label = str(button.get("text") or "").lower()
            callback_data = button.get("callback_data")
            if not callback_data or not any(word in label for word in ("провер", "check", "готов")):
                continue
            try:
                await client.request_callback_answer(BOT, msg_id, callback_data, timeout=10)
            except Exception as exc:
                if is_long_raffle_flood_wait(exc):
                    raise
                log.debug("subscription check callback: %s", short_error(exc))
            return

    def _first_account(self) -> AccountRecord | None:
        for a in self.accounts.list_accounts(enabled_only=True):
            return a
        return None


@dataclass
class _Outcome:
    status: str
    message: str
    ref_link: str | None = None
    ref_count: int | None = None


def _extract_start(value: str) -> str | None:
    text = (value or "").strip()
    m = START_RE.search(text)
    if m:
        return unquote(m.group(1))
    parsed = urlparse(text if "://" in text else f"https://{text}")
    if parsed.query:
        from urllib.parse import parse_qs
        for key in ("start",):
            val = parse_qs(parsed.query).get(key)
            if val:
                return unquote(val[0])
    return None


def _looks_like_post(value: str) -> bool:
    return looks_like_post_link(value)


def _normalize(value: str) -> str:
    return normalize_post_link(value)


_RU_EMOJI: dict[str, str] = {
    "пчела": "🐝", "пчелу": "🐝",
    "роза": "🌹", "розу": "🌹", "цветок": "🌸", "цветочек": "🌸",
    "бабочка": "🦋", "бабочку": "🦋",
    "лиса": "🦊", "лису": "🦊",
    "кит": "🐳", "кита": "🐳",
    "собака": "🐶", "собаку": "🐶", "пёс": "🐶", "пес": "🐶", "щенок": "🐶",
    "кошка": "🐱", "кошку": "🐱", "кот": "🐱", "котенок": "🐱", "котёнок": "🐱",
    "заяц": "🐰", "зайца": "🐰", "зайчик": "🐰", "кролик": "🐰",
    "медведь": "🐻", "медведя": "🐻", "мишка": "🐻",
    "сова": "🦉", "сову": "🦉",
    "ёж": "🦔", "ежа": "🦔", "ежик": "🦔", "ёжик": "🦔",
    "рыба": "🐟", "рыбу": "🐟", "рыбка": "🐟",
    "слон": "🐘", "слона": "🐘",
    "обезьяна": "🐵", "обезьяну": "🐵",
    "тигр": "🐯", "тигра": "🐯",
    "лев": "🦁", "льва": "🦁",
    "свинья": "🐷", "свинью": "🐷", "поросёнок": "🐷", "хрюшка": "🐷",
    "корова": "🐮", "корову": "🐮",
    "лошадь": "🐴", "лошадку": "🐴", "конь": "🐴",
    "мышь": "🐭", "мышку": "🐭", "мышка": "🐭",
    "черепаха": "🐢", "черепаху": "🐢",
    "попугай": "🦜", "попугая": "🦜",
    "осьминог": "🐙", "осьминога": "🐙",
    "краб": "🦀", "краба": "🦀",
    "дельфин": "🐬", "дельфина": "🐬",
    "паук": "🕷", "паука": "🕷",
    "змея": "🐍", "змею": "🐍",
    "лягушка": "🐸", "лягушку": "🐸",
    "курица": "🐔", "цыпленок": "🐤", "цыплёнок": "🐤",
    "петух": "🐓",
    "утка": "🦆", "утку": "🦆",
    "голубь": "🕊",
    "орел": "🦅", "орёл": "🦅",
    "дракон": "🐲",
    "динозавр": "🦕",
    "акула": "🦈", "акулу": "🦈",
    "волк": "🐺", "волка": "🐺",
    "панда": "🐼", "панду": "🐼",
    "коала": "🐨", "коалу": "🐨",
    "хомяк": "🐹",
    "носорог": "🦏",
    "жираф": "🦒",
    "верблюд": "🐪",
    "крокодил": "🐊",
    "ящерица": "🦎",
    "скорпион": "🦂",
    "жук": "🐞",
    "муравей": "🐜",
    "гусеница": "🐛",
    "медуза": "🪼",
    "пингвин": "🐧",
    "тюлень": "🦭",
    "бобр": "🦫", "бобёр": "🦫",
    "енот": "🦝",
    "скунс": "🦨",
    "выдра": "🦦",
    "зубр": "🦬",
    "мамонт": "🦣",
    "дронт": "🦤",
    "единорог": "🦄",
    "пегас": "🦄",
    "звезда": "⭐", "звезду": "⭐",
    "сердце": "❤", "сердечко": "❤",
    "огонь": "🔥",
    "солнце": "☀️", "солнышко": "☀️",
    "луна": "🌙",
    "машина": "🚗", "авто": "🚗", "автомобиль": "🚗",
    "самолет": "✈️", "самолёт": "✈️",
    "ракета": "🚀",
    "велосипед": "🚲",
    "кораблик": "🚢", "корабль": "🚢",
    "дом": "🏠", "домик": "🏠",
    "дерево": "🌳",
    "гриб": "🍄",
    "яблоко": "🍎",
    "банан": "🍌",
    "вишня": "🍒",
    "клубника": "🍓",
    "арбуз": "🍉",
    "пицца": "🍕",
    "торт": "🎂",
    "мороженое": "🍦",
    "конфета": "🍬",
    "подарок": "🎁",
    "мяч": "⚽",
    "корона": "👑",
    "ключ": "🔑",
    "замок": "🔒", "замочек": "🔒",
    "часы": "⌚",
    "телефон": "📱",
    "книга": "📖",
    "карандаш": "✏️",
    "ножницы": "✂️",
    "глаз": "👁", "глазик": "👁",
    "рот": "👄",
    "рука": "✋",
    "нога": "🦶",
    "мозг": "🧠",
}


def _extract_target_emoji(text: str) -> str | None:
    t = text.lower().strip()
    m = re.search(r"выберит[ее]\s+([а-яё]+)", t)
    if not m:
        return None
    word = m.group(1).strip().rstrip(":,.!?")
    return _RU_EMOJI.get(word, word)


def _pick_callback(
    cb_map: dict[int, str],
    alt_map: dict[int, str],
    target_name: str | None,
    question: str,
    *,
    exclude: set[str] | None = None,
) -> str | None:
    """Pick an unseen callback_data for the emoji matching target_name."""
    seen = exclude or set()
    if target_name:
        for emoji_id, alt in alt_map.items():
            cb = cb_map.get(emoji_id)
            if cb and cb not in seen and alt == target_name:
                return cb
        for emoji_id, alt in alt_map.items():
            cb = cb_map.get(emoji_id)
            if cb and cb not in seen and alt and target_name in alt:
                return cb
    return next((cb for cb in cb_map.values() if cb not in seen), None)
