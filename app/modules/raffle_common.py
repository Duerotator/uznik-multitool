from __future__ import annotations

import re
from typing import Any

from core.models import ProxyConfig


# Giveaway participation is batch work.  A long Telegram FloodWait must not
# hold the whole batch (or quietly sleep inside AccountClient).
RAFFLE_MAX_FLOOD_WAIT_SECONDS = 30
_FLOOD_WAIT_SECONDS_RE = re.compile(r"\bwait\s+(\d+)\s+seconds?\b", re.IGNORECASE)
_FLOOD_WAIT_SUFFIX_RE = re.compile(r"FLOOD_WAIT_(\d+)", re.IGNORECASE)


def flood_wait_seconds(exc: BaseException) -> int:
    """Return the Telegram FloodWait duration, or zero for another error."""
    text = str(exc)
    if "FLOOD" not in text.upper() or "WAIT" not in text.upper():
        return 0
    for attr in ("value", "seconds", "x"):
        try:
            seconds = int(getattr(exc, attr, 0) or 0)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds
    match = _FLOOD_WAIT_SECONDS_RE.search(text) or _FLOOD_WAIT_SUFFIX_RE.search(text)
    return int(match.group(1)) if match else 0


def is_long_raffle_flood_wait(exc: BaseException) -> bool:
    return flood_wait_seconds(exc) > RAFFLE_MAX_FLOOD_WAIT_SECONDS


def raffle_flood_skip_message(exc: BaseException) -> str:
    seconds = flood_wait_seconds(exc)
    return f"skipped: FloodWait {seconds}s (limit {RAFFLE_MAX_FLOOD_WAIT_SECONDS}s)"


def configure_raffle_client(client: Any) -> None:
    """Make AccountClient raise rather than sleep on a long batch FloodWait."""
    client.flood_wait_raise_after = RAFFLE_MAX_FLOOD_WAIT_SECONDS


def playwright_proxy(proxy_url: str | None) -> dict[str, Any] | None:
    """Convert the account VPN URL to Playwright's authenticated proxy shape."""
    config = ProxyConfig.from_url(proxy_url)
    if config is None:
        return None
    scheme = config.scheme.replace("socks5h", "socks5")
    result: dict[str, Any] = {
        "server": f"{scheme}://{config.hostname}:{config.port}",
    }
    if config.username:
        result["username"] = config.username
    if config.password:
        result["password"] = config.password
    return result
