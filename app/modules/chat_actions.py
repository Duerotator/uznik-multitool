from __future__ import annotations

import asyncio
import logging

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.telegram_client import ReactionChoice, create_client
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from utils.rate_limit import human_delay
from utils.telegram_errors import is_invalid_auth_error, short_error


class ChatActionService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("chat-actions")

    async def join(self, accounts: list[AccountRecord], target: str, *, progress: OperationProgress | None = None) -> ActionResult:
        return await self._run(accounts, "join", lambda client: client.join_chat(target), progress=progress)

    async def leave(self, accounts: list[AccountRecord], target: str, *, progress: OperationProgress | None = None) -> ActionResult:
        return await self._run(accounts, "leave", lambda client: client.leave_chat(target), progress=progress)

    async def open_link(self, accounts: list[AccountRecord], link: str, *, progress: OperationProgress | None = None) -> ActionResult:
        return await self._run(accounts, "open-link", lambda client: client.open_link(link), progress=progress)

    async def view_post(self, accounts: list[AccountRecord], link: str, *, progress: OperationProgress | None = None) -> ActionResult:
        return await self._run(accounts, "view-post", lambda client: client.view_post(link), progress=progress)

    async def random_reaction(self, accounts: list[AccountRecord], link: str, *, progress: OperationProgress | None = None) -> ActionResult:
        return await self._run(accounts, "random-reaction", lambda client: client.random_reaction(link), progress=progress)

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
    ) -> ActionResult:
        return await self._run(
            accounts,
            "set-reaction",
            lambda client: client.set_reaction(link, reaction),
            progress=progress,
        )

    async def _run(self, accounts: list[AccountRecord], action: str, operation, *, progress: OperationProgress | None = None) -> ActionResult:
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
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
                except asyncio.TimeoutError:
                    error = f"timeout after 90s"
                    self.log.error("%s timed out for %s", action, account.id)
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                except Exception as exc:
                    error = short_error(exc)
                    if action == "join" and "USER_ALREADY_PARTICIPANT" in error:
                        self.log.info("%s already done for %s", action, account.id)
                        result.add_ok(account.id)
                        if progress:
                            progress.mark_ok(account.id)
                        return
                    if action == "leave" and "USER_NOT_PARTICIPANT" in error:
                        self.log.info("%s already done for %s", action, account.id)
                        result.add_ok(account.id)
                        if progress:
                            progress.mark_ok(account.id)
                        return
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc)
                    self.accounts.mark_error(account.id, error, disable=disable)
                    if disable:
                        self.log.error("Disabled invalid session %s: %s", account.id, error)
                    else:
                        self.log.error("%s failed for %s: %s", action, account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result
