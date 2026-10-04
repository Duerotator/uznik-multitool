from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from core.config import AppConfig
from core.models import AccountRecord, utc_now_iso
from core.results import ActionResult
from core.telegram_client import create_client
from modules.accounts import AccountService
from utils.telegram_errors import is_invalid_auth_error, short_error

logger = logging.getLogger("account-age")

DATERE_BOT = "dateregbot"

MONTHS_MAP = {
    "январь": "01", "февраль": "02", "март": "03", "апрель": "04",
    "май": "05", "июнь": "06", "июль": "07",
    "август": "08", "сентябрь": "09", "октябрь": "10", "ноябрь": "11", "декабрь": "12",
    "января": "01", "февраля": "02", "марта": "03", "апреля": "04",
    "мая": "05", "июня": "06", "июля": "07",
    "августа": "08", "сентября": "09", "октября": "10", "ноября": "11", "декабря": "12",
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07",
    "august": "08", "september": "09", "october": "10", "november": "11", "december": "12",
}
MONTHS_MAP.update({k[:3]: v for k, v in list(MONTHS_MAP.items()) if len(k) > 3})

REG_LINE = re.compile(
    r"(?:Регистрация|Registration|регистрация|registration)\s*:?\s*"
    r"([A-ZА-ЯЁa-zа-яё]+)\s+(\d{4})",
)


def parse_registration_date(text: str) -> str | None:
    match = REG_LINE.search(text)
    if not match:
        return None
    month_name = match.group(1).lower().strip()
    year = match.group(2)
    for key, num in sorted(MONTHS_MAP.items(), key=lambda x: -len(x[0])):
        if month_name.startswith(key):
            return f"{num}/{year[2:]}"
    return None


class AccountAgeService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("account-age")

    @staticmethod
    def _incoming_text(messages: list[dict[str, Any]]) -> str:
        return "\n".join(
            str(message.get("text") or "")
            for message in messages
            if not message.get("outgoing", False) and message.get("text")
        )

    @staticmethod
    def _is_russian_button(text: object) -> bool:
        normalized = str(text or "").strip().lower()
        return "russian" in normalized or "русск" in normalized

    async def _select_datereg_language(self, client, known_message_ids: set[int]) -> bool:
        """Click Russian only on the fresh response to this run's /start."""
        raw_client = getattr(client, "client", None)
        if raw_client is None:
            return False
        try:
            if hasattr(raw_client, "get_chat_history"):
                async for message in raw_client.get_chat_history(DATERE_BOT, limit=8):
                    if int(getattr(message, "id", 0) or 0) in known_message_ids:
                        continue
                    markup = getattr(message, "reply_markup", None)
                    buttons = (
                        getattr(markup, "inline_keyboard", None)
                        or getattr(markup, "keyboard", None)
                    ) if markup else None
                    if not buttons:
                        continue
                    for row_index, row in enumerate(buttons):
                        for column_index, button in enumerate(row):
                            if not self._is_russian_button(getattr(button, "text", "")):
                                continue
                            await asyncio.wait_for(
                                message.click(x=column_index, y=row_index, timeout=6),
                                timeout=8,
                            )
                            return True
            elif hasattr(raw_client, "iter_messages"):
                async for message in raw_client.iter_messages(DATERE_BOT, limit=8):
                    if int(getattr(message, "id", 0) or 0) in known_message_ids:
                        continue
                    if not getattr(message, "buttons", None):
                        continue
                    await asyncio.wait_for(
                        message.click(text=lambda text: self._is_russian_button(text)),
                        timeout=8,
                    )
                    return True
        except Exception as exc:
            self.log.debug("Could not choose @dateregbot language: %s", short_error(exc))
        return False

    async def check_one(self, account: AccountRecord) -> str | None:
        if account.metadata.get("registration"):
            self.log.info("%s already checked: %s", account.id, account.metadata["registration"])
            return account.metadata["registration"]

        try:
            async with create_client(self.config, account) as client:
                me = await client.get_me()
                user_id = int(me.get("user_id") or account.user_id or 0)
                if not user_id:
                    self.log.warning("%s has no Telegram user ID", account.id)
                    return None
                if user_id != account.user_id:
                    self.accounts.update_account_runtime_identity(
                        account.id,
                        user_id=user_id,
                        username=me.get("username"),
                        first_name=me.get("first_name"),
                        last_name=me.get("last_name"),
                        phone=me.get("phone"),
                    )

                before_start = await client.get_recent_chat_messages(DATERE_BOT, limit=12)
                before_start_ids = {int(message.get("id") or 0) for message in before_start}
                await client.send_message(DATERE_BOT, "/start")
                await asyncio.sleep(3)
                if await self._select_datereg_language(client, before_start_ids):
                    await asyncio.sleep(2)
                username = str(me.get("username") or account.username or "").strip().lstrip("@")
                # Exactly one lookup per account/run: username has priority;
                # otherwise DateReg accepts the bare numeric ID. This avoids
                # a burst of username-and-ID retries when the bot is waiting
                # for its language setup.
                target = f"@{username}" if username else str(user_id)
                previous = await client.get_recent_chat_messages(DATERE_BOT, limit=12)
                previous_ids = {int(message.get("id") or 0) for message in previous}
                self.log.info("%s: checking @dateregbot target %s", account.id, target)
                await client.send_message(DATERE_BOT, target)
                return await self._wait_registration_reply(client, account, previous_ids)
        except Exception as exc:
            error = short_error(exc)
            if is_invalid_auth_error(exc):
                self.accounts.mark_error(account.id, error, disable=True)
            self.log.error("%s age check failed: %s", account.id, error)
            return None

    async def _wait_registration_reply(
        self,
        client,
        account: AccountRecord,
        previous_ids: set[int],
        *,
        warn: bool = True,
    ) -> str | None:
        reply_text = ""
        for _ in range(4):
            await asyncio.sleep(2)
            messages = await client.get_recent_chat_messages(DATERE_BOT, limit=12)
            fresh = [
                message for message in messages
                if int(message.get("id") or 0) not in previous_ids
            ]
            reply_text = self._incoming_text(fresh)
            registration = parse_registration_date(reply_text)
            if registration:
                self.log.info("%s registered: %s", account.id, registration)
                return registration

        if warn:
            preview = reply_text.replace("\n", " ")[:200] or "no fresh reply"
            self.log.warning("%s: @dateregbot returned no registration date: %s", account.id, preview)
        return None

    async def check_batch(self, accounts: list[AccountRecord], progress=None) -> ActionResult:
        result = ActionResult()
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))

        async def worker(account: AccountRecord) -> None:
            if not account.enabled:
                return
            async with semaphore:
                registration = await self.check_one(account)
                if registration:
                    self.accounts.update_profile_metadata(account.id, {
                        "registration": registration,
                        "reg_checked_at": utc_now_iso(),
                    })
                    result.add_ok(account.id)
                    if progress: progress.mark_ok(account.id)
                else:
                    result.add_error(account.id, "no_reg_date_found")
                    if progress: progress.mark_error(account.id)

        await asyncio.gather(*(worker(account) for account in accounts), return_exceptions=True)
        return result
