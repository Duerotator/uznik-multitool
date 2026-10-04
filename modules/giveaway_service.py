from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.telegram_client import create_client
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from modules.raffle_common import configure_raffle_client, is_long_raffle_flood_wait, raffle_flood_skip_message
from modules.raffle_random import extract_channel_targets, looks_like_post_link, normalize_post_link, unique_channels

PROVIDER_LABELS: dict[str, str] = {
    "auto": "Auto detect",
    "random": "@random / JoinLot",
    "bestrandom": "@BestRandom_bot",
    "fastgiveaway": "@FastGiveawaysBot",
    "randombeast": "@randombeast_bot",
}

_ALIASES = {
    "auto": "auto",
    "random": "random",
    "joinlot": "random",
    "bestrandom": "bestrandom",
    "best-random": "bestrandom",
    "fastgiveaway": "fastgiveaway",
    "fast-giveaway": "fastgiveaway",
    "randombeast": "randombeast",
    "random-beast": "randombeast",
}

log = logging.getLogger("giveaway")


@dataclass
class GiveawayInspection:
    provider: str
    source: str
    post_link: str | None = None
    channels: list[str] = field(default_factory=list)
    button_text: str = ""
    probe_account_id: str | None = None

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS.get(self.provider, self.provider)

    def summary(self) -> str:
        source_kind = "post" if self.post_link else "direct link"
        channels = ", ".join(self.channels) if self.channels else "auto / none in post text"
        return f"{self.provider_label}; source={source_kind}; channels={channels}"


def normalize_provider(value: str | None) -> str:
    key = str(value or "auto").strip().lower().replace("_", "-")
    provider = _ALIASES.get(key)
    if not provider:
        raise ValueError(f"Unknown giveaway provider: {value}")
    return provider


def detect_giveaway_provider(source: str, detail: dict[str, Any] | None = None) -> tuple[str | None, str]:
    """Detect a supported giveaway engine from a URL or post buttons."""
    detail = detail or {}
    candidates: list[tuple[str, str]] = [("", str(source or ""))]
    for link in detail.get("hidden_links") or []:
        candidates.append(("", str(link)))
    for button in detail.get("buttons") or []:
        label = str(button.get("text") or "")
        for field_name in ("web_app_url", "url", "callback_data"):
            value = button.get(field_name)
            if value:
                candidates.append((label, str(value)))

    for label, value in candidates:
        folded = value.lower()
        if "fastgiveawaysbot" in folded or "contest_join*captcha*" in folded:
            return "fastgiveaway", label
        if "bestrandom_bot" in folded or "bestrandombot" in folded:
            return "bestrandom", label
        if "randombeast_bot" in folded or "randombeast" in folded:
            return "randombeast", label
        if "/random/joinlot" in folded or "/randomized/joinlot" in folded or "randomgodbot" in folded or folded.startswith("lot_join "):
            return "random", label
    return None, ""


class GiveawayService:
    """Single entry point for giveaway inspection and participation."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)

    async def inspect(
        self,
        target: str,
        *,
        provider: str = "auto",
        extra_channels: list[str] | None = None,
        probe_account: AccountRecord | None = None,
        probe_accounts: list[AccountRecord] | None = None,
    ) -> GiveawayInspection:
        raw = str(target or "").strip()
        if not raw:
            raise ValueError("Giveaway link is empty")
        requested = normalize_provider(provider)
        detail: dict[str, Any] = {}
        post_link: str | None = None
        probe_account_id: str | None = None
        aggregator_channels: list[str] = []
        if looks_like_post_link(raw):
            post_link = normalize_post_link(raw)
            candidates = self._probe_candidates(probe_account, probe_accounts)
            if not candidates:
                raise RuntimeError("No enabled account is available to inspect the giveaway post")
            for account in candidates:
                try:
                    async with create_client(self.config, account) as client:
                        configure_raffle_client(client)
                        detail = await client.get_message_detail(post_link)
                        
                        # Extract aggregator channels before following forward
                        aggregator_channels = extract_channel_targets(str(detail.get("text") or ""))
                        aggregator_chat = str(detail.get("chat_username") or detail.get("chat") or "")
                        if aggregator_chat:
                            aggregator_channels.append(aggregator_chat)
                            
                        forward_post_link = str(detail.get("forward_post_link") or "").strip()
                        if forward_post_link and forward_post_link != post_link:
                            forward_detail = await client.get_message_detail(forward_post_link)
                            if detect_giveaway_provider(forward_post_link, forward_detail)[0]:
                                detail = forward_detail
                                post_link = forward_post_link
                    probe_account_id = account.id
                    break
                except Exception as exc:
                    if not is_long_raffle_flood_wait(exc) and "database is locked" not in str(exc):
                        raise
                    log.warning("%s skipped while inspecting %s: %s", account.id, post_link, str(exc))
            else:
                raise RuntimeError(
                    f"All {len(candidates)} available inspection account(s) were skipped for FloodWait over 30s"
                )

        detected, button_text = detect_giveaway_provider(raw, detail)
        if requested == "auto":
            if not detected:
                raise ValueError(
                    "Could not detect a supported giveaway button in this post. "
                    "Choose the provider manually or provide the bot/startapp link."
                )
            selected = detected
        else:
            selected = requested
            if detected and detected != selected:
                raise ValueError(
                    f"Selected {PROVIDER_LABELS[selected]}, but the link looks like "
                    f"{PROVIDER_LABELS[detected]}."
                )

        channels = extract_channel_targets(str(detail.get("text") or ""))
        for hidden_link in detail.get("hidden_links") or []:
            channels.extend(extract_channel_targets(str(hidden_link)))
        for button in detail.get("buttons") or []:
            for field_name in ("web_app_url", "url"):
                channels.extend(extract_channel_targets(str(button.get(field_name) or "")))
        channels.extend(extract_channel_targets(" ".join(extra_channels or [])))
        chat = str(detail.get("chat_username") or detail.get("chat") or "")
        if chat:
            channels.append(chat)
        channels.extend(aggregator_channels)
        channels = unique_channels(channels)
        return GiveawayInspection(selected, raw, post_link, channels, button_text, probe_account_id)

    async def participate(
        self,
        accounts: list[AccountRecord],
        target: str,
        *,
        provider: str = "auto",
        extra_channels: list[str] | None = None,
        progress: OperationProgress | None = None,
    ) -> ActionResult:
        enabled = [account for account in accounts if account.enabled]
        if not enabled:
            raise ValueError("No enabled accounts selected")
        inspection = await self.inspect(
            target,
            provider=provider,
            extra_channels=extra_channels,
            probe_account=enabled[0],
            probe_accounts=enabled,
        )
        log.info("Resolved giveaway: %s", inspection.summary())
        probe_account = next(
            (account for account in enabled if account.id == inspection.probe_account_id),
            enabled[0],
        )
        return await self._service(inspection.provider).participate(
            enabled,
            target,
            extra_channels=list(extra_channels or []),
            progress=progress,
            probe_account=probe_account,
        )

    def _service(self, provider: str):
        if provider == "random":
            from modules.raffle_random import RandomRaffleService
            return RandomRaffleService(self.config)
        if provider == "bestrandom":
            from modules.raffle_bestrandom import BestRandomService
            return BestRandomService(self.config)
        if provider == "fastgiveaway":
            from modules.raffle_fastgiveaway import FastGiveawayService
            return FastGiveawayService(self.config)
        if provider == "randombeast":
            from modules.raffle_randombeast import RandomBeastService
            return RandomBeastService(self.config)
        raise ValueError(f"Unsupported giveaway provider: {provider}")

    def _first_account(self) -> AccountRecord | None:
        return next(iter(self.accounts.list_accounts(enabled_only=True)), None)

    def _probe_candidates(
        self,
        probe_account: AccountRecord | None,
        probe_accounts: list[AccountRecord] | None,
    ) -> list[AccountRecord]:
        candidates = [account for account in (probe_accounts or []) if account.enabled]
        if probe_account is not None and probe_account.enabled:
            candidates.insert(0, probe_account)
        if not candidates:
            candidates = list(self.accounts.list_accounts(enabled_only=True))
        unique: dict[str, AccountRecord] = {}
        for account in candidates:
            unique.setdefault(account.id, account)
        return list(unique.values())
