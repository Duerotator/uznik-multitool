from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Any, Collection, Iterable, Mapping

from core.models import AccountRecord
from utils.phone_region import detect_phone_region, display_region


@dataclass(frozen=True)
class AccountFilter:
    query: str = ""
    created_from: str = ""
    created_to: str = ""
    country: str = "any"
    spamblock: str = "any"
    login_mail: str = "any"
    two_fa: str = "any"
    username: str = "any"
    validity: str = "any"
    proxy: str = "any"
    sleep: str = "any"
    enabled: str = "any"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "AccountFilter":
        raw = raw or {}
        defaults = {
            "query": "", "created_from": "", "created_to": "",
            "country": "any", "spamblock": "any", "login_mail": "any",
            "two_fa": "any", "username": "any", "validity": "any",
            "proxy": "any", "sleep": "any", "enabled": "any",
        }
        return cls(**{
            field: str(raw.get(field) or default).strip()
            for field, default in defaults.items()
        })

    def to_dict(self) -> dict[str, str]:
        return {
            "query": self.query, "created_from": self.created_from,
            "created_to": self.created_to, "country": self.country,
            "spamblock": self.spamblock, "login_mail": self.login_mail,
            "two_fa": self.two_fa, "username": self.username,
            "validity": self.validity, "proxy": self.proxy,
            "sleep": self.sleep, "enabled": self.enabled,
        }

    @property
    def active(self) -> bool:
        return any((
            self.query, self.created_from, self.created_to,
            self.country != "any", self.spamblock != "any",
            self.login_mail != "any", self.two_fa != "any",
            self.username != "any", self.validity != "any",
            self.proxy != "any", self.sleep != "any", self.enabled != "any",
        ))


def filter_accounts(
    accounts: Iterable[AccountRecord],
    spec: AccountFilter,
    *,
    sleeping_ids: Collection[str] | None = None,
    sleep_states: Mapping[str, str] | None = None,
    global_proxy: str | None = None,
) -> list[AccountRecord]:
    start = _date_bound(spec.created_from, end=False)
    end = _date_bound(spec.created_to, end=True)
    query = spec.query.casefold()
    result: list[AccountRecord] = []
    for account in accounts:
        metadata = account.metadata or {}
        created = _parse_datetime(account.created_at)
        if start and (not created or created < start):
            continue
        if end and (not created or created > end):
            continue
        if query and query not in " ".join(str(value or "") for value in (
            account.id, account.label, account.phone, account.username,
            account.first_name, account.last_name,
        )).casefold():
            continue
        region = detect_phone_region(account.phone or account.label)
        country = region.country if region else "unknown"
        if spec.country != "any" and country != spec.country:
            continue
        spamblock = str(metadata.get("spamblock_status") or "unknown").lower()
        if spec.spamblock != "any" and spamblock != spec.spamblock:
            continue
        login_mail = login_mail_state(account)
        if spec.login_mail != "any" and login_mail != spec.login_mail:
            continue
        if not _matches_presence(spec.two_fa, metadata.get("cloud_password")):
            continue
        has_username = bool(account.username or metadata.get("applied_username"))
        if not _matches_presence(spec.username, has_username):
            continue
        if spec.validity != "any" and _validity(account) != spec.validity:
            continue
        if not _matches_presence(spec.proxy, bool(effective_proxy(account, global_proxy))):
            continue
        sleeping = account.id in sleeping_ids if sleeping_ids is not None else False
        state = sleep_states.get(account.id, "unknown") if sleep_states is not None else ("sleeping" if sleeping else "awake")
        if spec.sleep != "any" and state != spec.sleep:
            continue
        if spec.enabled == "enabled" and not account.enabled:
            continue
        if spec.enabled == "disabled" and account.enabled:
            continue
        result.append(account)
    return result


def login_mail_state(account: AccountRecord) -> str:
    if account.metadata.get("login_email"):
        return "custom"
    return str(account.metadata.get("login_email_status") or "unknown").lower()


def effective_proxy(account: AccountRecord, global_proxy: str | None = None) -> str | None:
    return account.proxy or account.metadata.get("proxy") or global_proxy


def country_options(accounts: Iterable[AccountRecord]) -> list[dict[str, Any]]:
    counts = Counter(
        (region.country if (region := detect_phone_region(a.phone or a.label)) else "unknown")
        for a in accounts
    )
    return [
        {"value": country, "label": display_region(country), "count": count}
        for country, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _matches_presence(expected: str, value: Any) -> bool:
    if expected == "any":
        return True
    if expected == "with":
        return value is True
    if expected == "without":
        return value is False
    return value is not True and value is not False


def _validity(account: AccountRecord) -> str:
    metadata = account.metadata or {}
    status = str(metadata.get("health_status") or "").lower()
    if status == "valid":
        return "valid"
    if status in {"invalid", "error"} or metadata.get("last_error"):
        return "invalid"
    return "unknown"


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _date_bound(value: str, *, end: bool) -> datetime | None:
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    return datetime.combine(day, time.max if end else time.min, tzinfo=timezone.utc)
