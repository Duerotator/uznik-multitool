from __future__ import annotations


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
