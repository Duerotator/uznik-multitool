from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.telegram_client import ReactionChoice, create_client
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from utils.rate_limit import human_delay
from utils.telegram_errors import flood_wait_seconds, is_frozen_error, is_invalid_auth_error, short_error


def _is_already_member_error(action: str, exc: Exception, error: str) -> bool:
    cls_names = {cls.__name__ for cls in type(exc).__mro__}
    upper = error.upper()
    lower = error.lower()
    if action == "join":
        return (
            "UserAlreadyParticipantError" in cls_names
            or "USER_ALREADY_PARTICIPANT" in upper
            or "already a participant" in lower
        )
    if action == "leave":
        return (
            "UserNotParticipantError" in cls_names
            or "USER_NOT_PARTICIPANT" in upper
            or "not a participant" in lower
        )
    return False


class ChatActionService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("chat-actions")

    async def join(
        self,
        accounts: list[AccountRecord],
        target: str,
        *,
        progress: OperationProgress | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        return await self._run(accounts, "join", lambda client: client.join_chat(target), progress=progress, stop_event=stop_event)

    async def leave(
        self,
        accounts: list[AccountRecord],
        target: str,
        *,
        progress: OperationProgress | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        return await self._run(accounts, "leave", lambda client: client.leave_chat(target), progress=progress, stop_event=stop_event)

    async def open_link(
        self,
        accounts: list[AccountRecord],
        link: str,
        *,
        progress: OperationProgress | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        return await self._run(accounts, "open-link", lambda client: client.open_link(link), progress=progress, stop_event=stop_event)

    async def view_post(
        self,
        accounts: list[AccountRecord],
        link: str,
        *,
        progress: OperationProgress | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        return await self._run(accounts, "view-post", lambda client: client.view_post(link), progress=progress, stop_event=stop_event)

    async def random_reaction(
        self,
        accounts: list[AccountRecord],
        link: str,
        *,
        progress: OperationProgress | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        return await self._run(accounts, "random-reaction", lambda client: client.random_reaction(link), progress=progress, stop_event=stop_event)

    async def available_reactions(self, accounts: list[AccountRecord], link: str) -> list[ReactionChoice]:
        errors: dict[str, str] = {}
        for account in accounts:
            if not account.enabled:
                continue
            try:
                async with create_client(self.config, account) as client:
                    reactions = await client.available_reactions(link)
                if reactions:
                    self.log.info("Loaded %s reaction option(s) using %s", len(reactions), account.id)
                    return reactions
            except Exception as exc:
                errors[account.id] = short_error(exc)
                self.log.warning("Could not load reactions with %s: %s", account.id, errors[account.id])
        if errors:
            sample = "; ".join(f"{account_id}: {error}" for account_id, error in list(errors.items())[:3])
            raise RuntimeError(f"Could not load available reactions. {sample}")
        raise RuntimeError("No enabled accounts available to load reactions.")

    async def set_reaction(
        self,
        accounts: list[AccountRecord],
        link: str,
        reaction: ReactionChoice,
        *,
        progress: OperationProgress | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        return await self._run(
            accounts,
            "set-reaction",
            lambda client: client.set_reaction(link, reaction),
            progress=progress,
            stop_event=stop_event,
        )

    async def _run(
        self,
        accounts: list[AccountRecord],
        action: str,
        operation,
        *,
        progress: OperationProgress | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()

        async def _sleep_interruptibly(seconds: float) -> bool:
            if stop_event is None:
                await asyncio.sleep(seconds)
                return False
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=seconds)
                return True
            except asyncio.TimeoutError:
                return False

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                if stop_event and stop_event.is_set():
                    return
                await human_delay(self.config.min_action_delay, self.config.max_action_delay, stop_event=stop_event)
                for attempt in range(2):
                    if stop_event and stop_event.is_set():
                        return
                    try:
                        async with create_client(self.config, account) as client:
                            outcome = await asyncio.wait_for(operation(client), timeout=90)
                        if outcome:
                            self.log.info("%s done for %s: %s", action, account.id, outcome)
                        else:
                            self.log.info("%s done for %s", action, account.id)
                        result.add_ok(account.id)
                        if progress:
                            progress.mark_ok(account.id)
                        return
                    except asyncio.CancelledError:
                        raise
                    except asyncio.TimeoutError:
                        error = "timeout after 90s"
                        self.log.error("%s timed out for %s", action, account.id)
                        result.add_error(account.id, error)
                        if progress:
                            progress.mark_error(account.id)
                        return
                    except Exception as exc:
                        wait = flood_wait_seconds(exc)
                        if wait is not None and attempt == 0 and wait <= 60:
                            self.log.warning("%s flood wait %ss for %s; waiting before retry", action, wait, account.id)
                            if await _sleep_interruptibly(wait + 1.0):
                                return
                            continue
                        error = short_error(exc)
                        if _is_already_member_error(action, exc, error):
                            self.log.info("%s already done for %s", action, account.id)
                            result.add_ok(account.id)
                            if progress:
                                progress.mark_ok(account.id)
                            return
                        if is_frozen_error(exc):
                            self.accounts.update_profile_metadata(
                                account.id,
                                {"health_status": "frozen", "health_checked_at": datetime.now(timezone.utc).isoformat()},
                            )
                            self.accounts.mark_error(account.id, error, disable=True)
                            result.add_error(account.id, error + " [frozen, disabled]")
                            self.log.warning("Disabled frozen session %s: %s", account.id, error)
                        else:
                            result.add_error(account.id, error)
                            disable = is_invalid_auth_error(exc)
                            self.accounts.mark_error(account.id, error, disable=disable)
                            if disable:
                                self.log.error("Disabled invalid session %s: %s", account.id, error)
                            else:
                                self.log.error("%s failed for %s: %s", action, account.id, error)
                        if progress:
                            progress.mark_error(account.id)
                        return

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result
