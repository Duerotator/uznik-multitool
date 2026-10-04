from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from core.config import AppConfig
from core.models import AccountRecord, utc_now_iso
from core.results import ActionResult
from core.telegram_client import create_client
from modules.accounts import AccountService
from utils.rate_limit import human_delay
from utils.telegram_errors import is_invalid_auth_error, short_error


@dataclass(frozen=True)
class SpamBlockStatus:
    status: str
    until: str | None = None
    raw_text: str = ""

    @property
    def display(self) -> str:
        if self.status == "clear":
            return "none"
        if self.status == "forever":
            return "forever"
        if self.status == "temporary" and self.until:
            try:
                parsed = datetime.fromisoformat(self.until.replace("Z", "+00:00"))
                return f"until {parsed.strftime('%Y-%m-%d %H:%M UTC')}"
            except ValueError:
                return f"until {self.until}"
        if self.status == "limited":
            return "limited"
        return "unknown"


class SpamBlockService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("spamblock")

    async def check(self, accounts: list[AccountRecord], progress=None) -> ActionResult:
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                try:
                    async with create_client(self.config, account) as client:
                        started_at = int(time.time())
                        await client.send_message("@SpamBot", "/start")
                        try:
                            text = await self._read_spambot_reply(client, since_ts=started_at)
                        except RuntimeError as exc:
                            self.log.warning("SpamBot reply missing for %s: %s", account.id, exc)
                            text = ""
                    status = parse_spambot_response(text)
                    self.accounts.update_profile_metadata(
                        account.id,
                        {
                            "spamblock_status": status.status,
                            "spamblock_until": status.until,
                            "spamblock_checked_at": utc_now_iso(),
                            "spamblock_raw": status.raw_text[:1000],
                        },
                    )
                    result.add_ok(account.id)
                    if progress: progress.mark_ok(account.id)
                    self.log.info("Spamblock for %s: %s", account.id, status.display)
                except Exception as exc:
                    error = short_error(exc)
                    result.add_error(account.id, error)
                    if progress: progress.mark_error(account.id, error)
                    disable = is_invalid_auth_error(exc)
                    self.accounts.mark_error(account.id, error, disable=disable)
                    self.log.error("Spamblock check failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result

    async def _read_spambot_reply(self, client, since_ts: int) -> str:
        fallback: list[str] = []
        for attempt in range(4):
            await asyncio.sleep(1.0)
            messages = await client.get_recent_chat_messages("@SpamBot", limit=8)
            incoming = [
                str(message.get("text") or "").strip()
                for message in messages
                if message.get("text")
                and not message.get("outgoing")
            ]
            recent = [
                str(message.get("text") or "").strip()
                for message in messages
                if message.get("text")
                and not message.get("outgoing")
                and int(message.get("received_at") or 0) >= since_ts - 2
            ]
            if recent:
                return "\n\n".join(recent[:3])
            if incoming and not fallback:
                fallback = incoming
        if fallback:
            return "\n\n".join(fallback[:3])
        raise RuntimeError("No @SpamBot reply received.")


def parse_spambot_response(text: str) -> SpamBlockStatus:
    normalized = " ".join(text.lower().split())
    until = parse_until_date(text)

    clear_markers = (
        "good news",
        "no limits are currently applied",
        "no limits currently applied",
        "your account is not limited",
        "free as a bird",
        "ваш аккаунт свободен от",
        "свободен от каких-либо ограничений",
        "нет никаких ограничений",
        "ограничений нет",
        "\u0432\u0430\u0448 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0441\u0432\u043e\u0431\u043e\u0434\u0435\u043d \u043e\u0442",
        "\u0441\u0432\u043e\u0431\u043e\u0434\u0435\u043d \u043e\u0442 \u043a\u0430\u043a\u0438\u0445-\u043b\u0438\u0431\u043e \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u0439",
        "\u0644\u0627 \u062a\u0648\u062c\u062f \u0642\u064a\u0648\u062f",
        "\u0644\u0627 \u062a\u0648\u062c\u062f \u0623\u064a \u0642\u064a\u0648\u062f",
        "\u062d\u0633\u0627\u0628\u0643 \u063a\u064a\u0631 \u0645\u0642\u064a\u062f",
    )
    if any(marker in normalized for marker in clear_markers):
        return SpamBlockStatus(status="clear", raw_text=text)

    forever_markers = (
        "limited forever",
        "permanently limited",
        "permanent limitation",
        "limited permanently",
        "forever",
        "permanently",
        "навсегда",
        "постоянно огранич",
    )
    if any(marker in normalized for marker in forever_markers):
        return SpamBlockStatus(status="forever", raw_text=text)

    limited_markers = (
        "your account is limited",
        "account is now limited",
        "currently limited",
        "you will not be able to send messages",
        "cannot send messages",
        "harsh response from our anti-spam systems",
        "submit a complaint to our moderators",
        "less strict limits",
        "ваш аккаунт ограничен",
        "аккаунт ограничен",
        "ограничен в отправке сообщений",
        "ограничена отправка сообщений",
        "не сможете отправлять сообщения",
        "\u0432\u0430\u0448 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d",
        "\u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d",
        "\u0430\u043d\u0442\u0438\u0441\u043f\u0430\u043c-\u0441\u0438\u0441\u0442\u0435\u043c",
        "\u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u044f\u0432\u043a\u0443 \u043c\u043e\u0434\u0435\u0440\u0430\u0442\u043e\u0440\u0430\u043c",
        "\u043c\u0435\u043d\u0435\u0435 \u0441\u0442\u0440\u043e\u0433\u0438\u0435 \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d\u0438\u044f",
        "\u062d\u0633\u0627\u0628\u0643 \u0645\u062d\u062f\u0648\u062f",
        "\u062d\u0633\u0627\u0628\u0643 \u0645\u0642\u064a\u062f",
        "\u0644\u0646 \u062a\u062a\u0645\u0643\u0646 \u0645\u0646 \u0625\u0631\u0633\u0627\u0644",
        "\u0645\u0643\u0627\u0641\u062d\u0629 \u0627\u0644\u0631\u0633\u0627\u0626\u0644 \u0627\u0644\u0645\u0632\u0639\u062c\u0629",
    )
    if until:
        return SpamBlockStatus(status="temporary", until=until, raw_text=text)
    if any(marker in normalized for marker in limited_markers):
        return SpamBlockStatus(status="limited", raw_text=text)
    return SpamBlockStatus(status="unknown", raw_text=text)


def parse_until_date(text: str) -> str | None:
    patterns = (
        r"(?:until|on|lifted on|released on)\s+(\d{1,2}\s+[A-Za-zА-Яа-яёЁ.]+\s+\d{4},?\s+\d{1,2}:\d{2}\s+UTC)",
        r"(?:до|снято|снимется)\s+(\d{1,2}\s+[A-Za-zА-Яа-яёЁ.]+\s+\d{4},?\s+\d{1,2}:\d{2}\s+UTC)",
        r"(\d{1,2}\s+[A-Za-zА-Яа-яёЁ.]+\s+\d{4},?\s+\d{1,2}:\d{2}\s+UTC)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        parsed = parse_spambot_datetime(match.group(1))
        if parsed:
            return parsed
    return None


def parse_spambot_datetime(value: str) -> str | None:
    value = normalize_month_name(value.strip().replace("  ", " "))
    value = re.sub(r"\s+", " ", value)
    value = value.replace(" ,", ",")
    formats = (
        "%d %b %Y, %H:%M UTC",
        "%d %B %Y, %H:%M UTC",
        "%d %b %Y %H:%M UTC",
        "%d %B %Y %H:%M UTC",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def normalize_month_name(value: str) -> str:
    months = {
        "янв": "Jan",
        "января": "Jan",
        "фев": "Feb",
        "февраля": "Feb",
        "мар": "Mar",
        "марта": "Mar",
        "апр": "Apr",
        "апреля": "Apr",
        "мая": "May",
        "май": "May",
        "июн": "Jun",
        "июня": "Jun",
        "июл": "Jul",
        "июля": "Jul",
        "авг": "Aug",
        "августа": "Aug",
        "сен": "Sep",
        "сент": "Sep",
        "сентября": "Sep",
        "окт": "Oct",
        "октября": "Oct",
        "ноя": "Nov",
        "ноября": "Nov",
        "дек": "Dec",
        "декабря": "Dec",
    }
    for ru, en in months.items():
        value = re.sub(rf"\b{ru}\.?\b", en, value, flags=re.IGNORECASE)
    return value
