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
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from modules.raffle_common import (
    configure_raffle_client,
    is_long_raffle_flood_wait,
    raffle_flood_skip_message,
)
from modules.raffle_random import extract_channels, looks_like_post_link, normalize_post_link
from utils.rate_limit import human_delay
from utils.telegram_errors import is_invalid_auth_error, short_error

BOT = "FastGiveawaysBot"
START_RE = re.compile(r"start=(.+?)(?:&|$)", re.IGNORECASE)
CAPTCHA_CB_RE = re.compile(r"contest_join\*captcha\*(\d+)")

_EMOJI_MAP: dict[str, str] = {}

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
    ) -> ActionResult:
        enabled = [account for account in accounts if account.enabled]
        parsed = await self._resolve_target(
            target, extra_channels or [], probe_account=probe_account or (enabled[0] if enabled else None),
        )
        log.info("FastGiveaway start=%s channels=%s", parsed.start_param, parsed.channels)

        sem = asyncio.Semaphore(max(1, self.config.max_concurrency or 1))
        result = ActionResult()

        async def worker(acc: AccountRecord) -> None:
            nonlocal current_batch_size
            async with sem:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                try:
                    outcome = await asyncio.wait_for(self._participate_one(acc, parsed), timeout=180)
                    if outcome.status in ("ok", "already"):
                        result.add_ok(acc.id)
                        result.details[acc.id] = outcome.message
                        if progress:
                            progress.mark_ok(acc.id)
                        if getattr(outcome, "ref_link", None) and ("start=" in str(outcome.ref_link) or "startapp=" in str(outcome.ref_link)):
                            new_start = re.split(r"start(?:app)?=", str(outcome.ref_link))[-1]
                            parsed.start_param = new_start
                            
                            new_count = getattr(outcome, "ref_count", None)
                            if new_count:
                                current_batch_size = max(1, int(new_count))
                                log.info("%s: updated batch_size to %s", acc.id, current_batch_size)
                            else:
                                current_batch_size = 1
                            
                            log.info("%s: updated start_param to %s for next accounts", acc.id, new_start)

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
    ) -> FastGiveawayTarget:
        raw = target.strip()
        if not raw:
            raise ValueError("Empty target")
        start_param = _extract_start(raw)
        if _looks_like_post(raw):
            return await self._resolve_from_post(_normalize(raw), extra, start_param, probe_account)
        if not start_param:
            raise ValueError("Could not extract start= param. Provide t.me/FastGiveawaysBot?start=... link")
        channels: list[str] = []
        for item in extra:
            channels.extend(extract_channels(item))
        channels = list(dict.fromkeys(channels))
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
        channels = list(extract_channels(" ".join(extra)))
        async with create_client(self.config, acc) as client:
            configure_raffle_client(client)
            detail = await client.get_message_detail(raw)
            text = str(detail.get("text") or "")
            channels.extend(extract_channels(text))
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
            channels=list(dict.fromkeys(channels)),
            post_link=raw,
        )

    async def _participate_one(self, acc: AccountRecord, target: FastGiveawayTarget) -> _Outcome:
        async with create_client(self.config, acc) as client:
            configure_raffle_client(client)
            c = client.client
            await self._ensure_lang(c)

            for ch in target.channels:
                try:
                    await client.join_chat(ch)
                    try:
                        from utils.rate_limit import mute_and_archive
                        await mute_and_archive(c, ch)
                    except Exception:
                        pass
                except Exception as exc:
                    if is_long_raffle_flood_wait(exc):
                        raise
                    err = short_error(exc)
                    if "already" not in err.lower():
                        log.warning("%s join %s: %s", acc.id, ch, err)
                await human_delay(0.3, 1.0)

            await client.send_message(BOT, f"/start {target.start_param}")
            await asyncio.sleep(2)

            last_id = await self._last_outgoing_id(c)
            seen_buttons: set[str] = set()
            subscription_retries = 0

            for attempt in range(10):
                # check result first
                bot_message = await self._last_bot_message(c, after_id=last_id)
                text = self._message_text(bot_message)
                if any(w in text.lower() for w in ("успешно", "приняли участие", "участвуете", "приглашайте", "invite", "скопировать")):
                    ref_link = None
                    ref_count = None
                    import re as re_mod
                    
                    fraction_match = re_mod.search(r'\d+\s*/\s*(\d+)', text)
                    if fraction_match:
                        ref_count = int(fraction_match.group(1))
                    else:
                        cnt_match = re_mod.search(r'(?:invite|пригласи(?:ть|те)?)\D*?(\d+)', text.lower())
                        if cnt_match:
                            ref_count = int(cnt_match.group(1))
                            
                    if hasattr(bot_message, "reply_markup") and bot_message.reply_markup:
                        for row in getattr(bot_message.reply_markup, "inline_keyboard", []) or []:
                            for btn in row:
                                val = str(getattr(btn, "url", ""))
                                copy_text = getattr(btn, "copy_text", None)
                                if copy_text:
                                    val += " " + str(getattr(copy_text, "text", copy_text))
                                switch_inline_query = getattr(btn, "switch_inline_query", None)
                                if switch_inline_query:
                                    val += " " + str(switch_inline_query)
                                
                                match = re_mod.search(r'https?://(?:t\.me|telegram\.me)/[a-zA-Z0-9_]+bot\?(?:start|startapp)=[a-zA-Z0-9_-]+', val)
                                if match:
                                    ref_link = match.group(0)
                                    break
                            if ref_link:
                                break
                    return _Outcome("ok", text[:200], ref_link, ref_count)
                if "уже" in text.lower() and any(w in text.lower() for w in ("приняли", "участ")):
                    return _Outcome("already", text[:200])
                if "подписк" in text.lower():
                    joined = await self._join_required_channels(client, acc, bot_message)
                    if joined and subscription_retries < 2:
                        subscription_retries += 1
                        await self._check_subscription(c, bot_message)
                        await human_delay(1.0, 2.0)
                        await client.send_message(BOT, f"/start {target.start_param}")
                        log.info("%s: retrying after joining %s bot-required channel(s)", acc.id, len(joined))
                        await asyncio.sleep(2)
                        last_id = await self._last_outgoing_id(c)
                        continue
                    return _Outcome("error", text[:150])
                if "отказано" in text.lower():
                    return _Outcome("error", text[:150])

                captcha = await self._find_captcha_msg(c, after_id=last_id, timeout=5)
                if captcha is None:
                    if attempt == 0 and not text.strip():
                        return _Outcome("error", "no bot response")
                    await asyncio.sleep(2)
                    continue

                question = (captcha.text or captcha.caption or "").strip()
                log.info("%s: captcha attempt %d: %s", acc.id, attempt + 1, question[:100])

                emoji_ids, cb_map = self._parse_buttons(captcha)
                target_name = _extract_target_emoji(question)
                alt_map = await self._resolve_emoji_alts(c, emoji_ids)
                log.info("%s: target=%s alts=%s", acc.id, target_name, {
                    eid: alt_map.get(eid, "?") for eid in emoji_ids
                })
                chosen_cb = _pick_callback(cb_map, alt_map, target_name, question)

                if chosen_cb in seen_buttons:
                    log.info("%s: already tried this captcha, skipping", acc.id)
                    await asyncio.sleep(1)
                    last_id = captcha.id
                    continue
                seen_buttons.add(chosen_cb)

                if chosen_cb is None and cb_map:
                    chosen_cb = next(iter(cb_map.values()))
                if chosen_cb is None:
                    return _Outcome("error", "no captcha buttons")

                await human_delay(0.5, 1.5)
                try:
                    await c.request_callback_answer(BOT, captcha.id, chosen_cb, timeout=10)
                except Exception as exc:
                    if is_long_raffle_flood_wait(exc):
                        raise
                    log.warning("%s: btn err %s", acc.id, short_error(exc))

                await asyncio.sleep(2)
                last_id = captcha.id

            text = await self._last_bot_text(c, after_id=last_id)
            if any(w in text.lower() for w in ("успешно", "приняли", "участвуете")):
                return _Outcome("ok", text[:200])
            if "уже" in text.lower() and any(w in text.lower() for w in ("приняли", "участ")):
                return _Outcome("already", text[:200])
            return _Outcome("error", "captcha attempts exhausted")

    async def _ensure_lang(self, c) -> None:
        async for m in c.get_chat_history(BOT, limit=8):
            if m.text and "выберите язык" in m.text.lower() and m.reply_markup and m.reply_markup.inline_keyboard:
                ru_btn = m.reply_markup.inline_keyboard[0][0]
                try:
                    await c.request_callback_answer(BOT, m.id, ru_btn.callback_data, timeout=8)
                except Exception:
                    pass
                return
        # No language menu in history — this is a new user, send /start to trigger it
        try:
            await c.send_message(BOT, "/start")
            await asyncio.sleep(2)
            async for m in c.get_chat_history(BOT, limit=5):
                if m.text and "выберите язык" in m.text.lower() and m.reply_markup and m.reply_markup.inline_keyboard:
                    ru_btn = m.reply_markup.inline_keyboard[0][0]
                    await c.request_callback_answer(BOT, m.id, ru_btn.callback_data, timeout=8)
                    await asyncio.sleep(1)
                    return
        except Exception:
            pass

    async def _find_captcha_msg(self, c, *, after_id, timeout: float):
        for _ in range(int(timeout)):
            await asyncio.sleep(1)
            async for m in c.get_chat_history(BOT, limit=8):
                if not m.outgoing and m.reply_markup and m.reply_markup.inline_keyboard:
                    if m.id > int(after_id) or m.edit_date is not None:
                        for row in m.reply_markup.inline_keyboard:
                            for btn in row:
                                if "captcha" in (btn.callback_data or ""):
                                    return m
        return None

    def _parse_buttons(self, msg) -> tuple[list[int], dict[int, str]]:
        emoji_ids: list[int] = []
        cb_map: dict[int, str] = {}
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                cb = btn.callback_data or ""
                m = CAPTCHA_CB_RE.search(cb)
                if m:
                    eid = int(m.group(1))
                    emoji_ids.append(eid)
                    cb_map[eid] = cb
        return emoji_ids, cb_map

    async def _resolve_emoji_alts(self, c, emoji_ids: list[int]) -> dict[int, str]:
        try:
            from pyrogram import raw
            r = await c.invoke(raw.functions.messages.GetCustomEmojiDocuments(document_id=emoji_ids))
            alt_map: dict[int, str] = {}
            for doc in r:
                for attr in getattr(doc, "attributes", []) or []:
                    alt = getattr(attr, "alt", None)
                    if alt:
                        alt_map[doc.id] = alt
                        break
            return alt_map
        except Exception as exc:
            if is_long_raffle_flood_wait(exc):
                raise
            log.warning("GetCustomEmojiDocuments failed: %s", short_error(exc))
            return {}

    async def _last_outgoing_id(self, c) -> int:
        async for m in c.get_chat_history(BOT, limit=3):
            if m.outgoing:
                return m.id
        return 0

    async def _last_bot_text(self, c, *, after_id: int) -> str:
        return self._message_text(await self._last_bot_message(c, after_id=after_id))

    async def _last_bot_message(self, c, *, after_id: int):
        async for m in c.get_chat_history(BOT, limit=5):
            if m.id > int(after_id) and not m.outgoing:
                return m
        return None

    @staticmethod
    def _message_text(message) -> str:
        return str(getattr(message, "text", None) or getattr(message, "caption", None) or "")

    @staticmethod
    def _required_channels(message) -> list[str]:
        channels: list[str] = []
        markup = getattr(message, "reply_markup", None)
        for row in getattr(markup, "inline_keyboard", None) or []:
            for button in row:
                url = str(getattr(button, "url", None) or "")
                if not url:
                    continue
                channels.extend(extract_channels(url))
                if "t.me/+" in url.lower() or "joinchat/" in url.lower():
                    channels.append(url)
        return list(dict.fromkeys(channels))

    async def _join_required_channels(self, client, acc: AccountRecord, message) -> list[str]:
        joined: list[str] = []
        for channel in self._required_channels(message):
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

    async def _check_subscription(self, c, message) -> None:
        markup = getattr(message, "reply_markup", None)
        for row in getattr(markup, "inline_keyboard", None) or []:
            for button in row:
                label = str(getattr(button, "text", None) or "").lower()
                callback_data = getattr(button, "callback_data", None)
                if not callback_data or not any(word in label for word in ("провер", "check", "готов")):
                    continue
                try:
                    await c.request_callback_answer(BOT, message.id, callback_data, timeout=10)
                except Exception as exc:
                    if is_long_raffle_flood_wait(exc):
                        raise
                    log.debug("subscription check callback: %s", short_error(exc))
                return

    async def _wait_captcha_msg_after(self, c, *, after_id, timeout: float):
        for _ in range(int(timeout)):
            await asyncio.sleep(1)
            async for m in c.get_chat_history(BOT, limit=3):
                if m.id > int(after_id) and not m.outgoing:
                    if m.reply_markup and m.reply_markup.inline_keyboard:
                        for row in m.reply_markup.inline_keyboard:
                            for btn in row:
                                if "captcha" in (btn.callback_data or ""):
                                    return m
        return None

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
    "осьминог": "🐙",
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
    # "Выберите ПЧЕЛУ:" → пчелу
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
) -> str | None:
    """Pick the callback_data for the emoji matching target_name."""
    if target_name and target_name in alt_map.values():
        for emoji_id, alt in alt_map.items():
            if alt == target_name:
                return cb_map.get(emoji_id)
    # fallback: try partial match
    for emoji_id, alt in alt_map.items():
        if target_name and alt and target_name in alt:
            return cb_map.get(emoji_id)
    # brute: pick first unseen
    return next(iter(cb_map.values()), None)
