from __future__ import annotations

import re


INVALID_AUTH_MARKERS = (
    "AUTH_KEY_UNREGISTERED",
    "AUTH_KEY_INVALID",
    "AUTH_KEY_PERM_EMPTY",
    "USER_DEACTIVATED",
    "USER_DEACTIVATED_BAN",
    "SESSION_REVOKED",
    "SESSION_EXPIRED",
    "AUTH_KEY_DUPLICATED",
    "AuthKeyUnregisteredError",
    "AuthKeyInvalidError",
    "AuthKeyPermEmptyError",
    "UserDeactivatedError",
    "UserDeactivatedBanError",
    "SessionRevokedError",
    "SessionExpiredError",
    "AuthKeyDuplicatedError",
)

_NORMALIZED_AUTH_MARKERS = (
    "AUTHKEYUNREGISTERED",
    "AUTHKEYINVALID",
    "AUTHKEYPERMEMPTY",
    "AUTHKEYDUPLICATED",
    "USERDEACTIVATED",
    "SESSIONREVOKED",
    "SESSIONEXPIRED",
)

_NORMALIZED_FROZEN_MARKERS = (
    "FROZENMETHODINVALID",
    "FROZENPARTICIPANTMISSING",
)


def short_error(exc: BaseException) -> str:
    text = str(exc).strip()
    if not text:
        text = exc.__class__.__name__
    return text.replace("\n", " ")[:500]


def is_invalid_auth_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}"
    if "session is not authorized" in text.lower():
        return True
    normalized = text.upper().replace("_", "")
    return any(marker in normalized for marker in _NORMALIZED_AUTH_MARKERS)


def is_frozen_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}"
    upper = text.upper()
    if "FROZEN_" in upper:
        return True
    normalized = upper.replace("_", "")
    if any(marker in normalized for marker in _NORMALIZED_FROZEN_MARKERS):
        return True
    lower = text.lower()
    return "frozen account" in lower or "for frozen accounts" in lower


def flood_wait_seconds(exc: BaseException) -> int | None:
    text = f"{exc.__class__.__name__}: {exc}"
    upper = text.upper()
    normalized = upper.replace("_", "")
    is_flood = (
        any(
            marker in normalized
            for marker in ("FLOODWAIT", "FLOODPREMIUMWAIT", "SLOWMODEWAIT", "FLOODTESTPHONEWAIT")
        )
        or bool(re.search(r"\bwait\s+of\s+\d+\s+seconds?\b", text, re.I))
    )
    if not is_flood:
        return None
    for name in ("value", "seconds"):
        value = getattr(exc, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(1, int(value))
    for pattern in (
        r"(?:FLOOD(?:_PREMIUM)?_WAIT|SLOWMODE_WAIT|FLOOD_TEST_PHONE_WAIT)[_ ]+(\d+)",
        r"\bwait\s+(?:of|for)\s+(\d+)\s*seconds?\b",
        r"(?:wait|FLOOD_WAIT[_ ]*)(?:\s+(?:for|of))?\s*(\d+)",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return max(1, int(match.group(1)))
    return 60
