from __future__ import annotations

import re


INVALID_AUTH_MARKERS = (
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "USER_DEACTIVATED",
    "USER_DEACTIVATED_BAN",
    "SESSION_REVOKED",
    "SESSION_EXPIRED",
    "AUTH_KEY_DUPLICATED",
)


def short_error(exc: BaseException) -> str:
    text = str(exc).strip()
    if not text:
        text = exc.__class__.__name__
    return text.replace("\n", " ")[:500]


def is_invalid_auth_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}"
    return any(marker in text for marker in INVALID_AUTH_MARKERS)


def flood_wait_seconds(exc: BaseException) -> int | None:
    text = f"{exc.__class__.__name__}: {exc}"
    if "floodwait" not in text.lower() and "FLOOD_WAIT" not in text.upper():
        return None
    for name in ("value", "seconds"):
        value = getattr(exc, name, None)
        if isinstance(value, (int, float)):
            return max(1, int(value))
    match = re.search(r"(?:wait|FLOOD_WAIT[_ ]*)(?:\s+for)?\s*(\d+)", text, re.I)
    return max(1, int(match.group(1))) if match else 60
