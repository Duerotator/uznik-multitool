from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from core.config import AppConfig
from core.models import AccountRecord
from core.telegram_client import create_client
from modules.accounts import AccountService
from utils.telegram_errors import is_invalid_auth_error, short_error


CODE_RE = re.compile(r"\b(\d{5,6})\b")
CONTEXT_WORDS_RE = (
    r"login|code|telegram|"
    r"\u043a\u043e\u0434|"
    r"\u0432\u0445\u043e\u0434|"
    r"\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434"
)
CONTEXT_CODE_RE = re.compile(
    rf"(?:{CONTEXT_WORDS_RE})[^\d]{{0,80}}(\d{{5,6}})"
    rf"|(\d{{5,6}})[^\n\r]{{0,80}}(?:{CONTEXT_WORDS_RE})",
    re.IGNORECASE,
)


@dataclass
class TelegramCode:
    account_id: str
    label: str
    code: str
    received_at: int


@dataclass
class TelegramCodeScanResult:
    ok: int = 0
    errors: int = 0
    codes: list[TelegramCode] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)


class TelegramCodeService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("telegram-codes")

    async def scan_recent(
        self,
        accounts: list[AccountRecord],
        seconds: int = 60,
        limit: int = 25,
    ) -> TelegramCodeScanResult:
        since_ts = int(time.time()) - max(1, seconds)
        result = TelegramCodeScanResult()
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                try:
                    async with create_client(self.config, account) as client:
                        messages = await client.get_recent_service_messages(
                            since_ts=since_ts,
                            limit=limit,
                        )
                    found: list[TelegramCode] = []
                    for message in messages:
                        received_at = int(message.get("received_at") or 0)
                        if received_at and received_at < since_ts:
                            continue
                        for code in extract_codes(str(message.get("text") or "")):
                            found.append(
                                TelegramCode(
                                    account_id=account.id,
                                    label=account.label,
                                    code=code,
                                    received_at=received_at,
                                )
                            )
                    result.codes.extend(found)
                    result.ok += 1
                    result.details[account.id] = f"codes={len(found)}"
                except Exception as exc:
                    error = short_error(exc)
                    result.errors += 1
                    result.details[account.id] = error
                    disable = is_invalid_auth_error(exc)
                    self.accounts.mark_error(account.id, error, disable=disable)
                    self.log.warning("Telegram code scan failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        result.codes.sort(key=lambda item: item.received_at, reverse=True)
        return result


def extract_codes(text: str) -> list[str]:
    contextual: list[str] = []
    for match in CONTEXT_CODE_RE.finditer(text):
        code = match.group(1) or match.group(2)
        if code:
            contextual.append(code)
    candidates = contextual or CODE_RE.findall(text)
    result: list[str] = []
    seen: set[str] = set()
    for code in candidates:
        if code not in seen:
            result.append(code)
            seen.add(code)
    return result
