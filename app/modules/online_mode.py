from __future__ import annotations

import asyncio
import logging

from core.config import AppConfig
from core.models import AccountRecord
from core.telegram_client import create_client
from modules.accounts import AccountService
from utils.telegram_errors import is_invalid_auth_error, short_error


def _is_transient_network_error(exc: BaseException) -> bool:
    text = f"{exc.__class__.__name__}: {exc}".lower()
    markers = (
        "connecttimeout",
        "readtimeout",
        "timeout",
        "connection lost",
        "connectionreseterror",
        "connection aborted",
        "network",
    )
    return any(marker in text for marker in markers)


class OnlineModeService:
    def __init__(self, config: AppConfig, *, scheduler=None):
        self.config = config
        self.accounts = AccountService(config)
        from modules.sleep_scheduler import SleepScheduler
        self.scheduler = scheduler or SleepScheduler(str(config.data_dir / "sleep_zones.json"))
        self.log = logging.getLogger("online-mode")

    async def run(self, accounts: list[AccountRecord], stop_event: asyncio.Event) -> None:
        enabled_accounts = [account for account in accounts if account.enabled]
        if not enabled_accounts:
            self.log.info("Online mode skipped: no enabled accounts.")
            return

        for account in enabled_accounts:
            self.scheduler.assign_timezone(account.id, account.phone)

        wave_size = max(1, self.config.max_concurrency)
        online_seconds = max(35.0, self.config.max_action_delay * 4)
        wave_pause_seconds = max(3.0, self.config.min_action_delay)
        index = 0

        self.log.info(
            "Starting safer online mode: accounts=%s, wave_size=%s, online_seconds=%.1f",
            len(enabled_accounts),
            wave_size,
            online_seconds,
        )

        while not stop_event.is_set():
            enabled_accounts = [account for account in enabled_accounts if account.enabled]
            if not enabled_accounts:
                self.log.info("Online mode stopped: no enabled accounts left.")
                return
            if index >= len(enabled_accounts):
                index = 0

            awake = [a for a in enabled_accounts if not self.scheduler.is_sleeping(a.id)]
            if not awake:
                self.log.info("All accounts sleeping, waiting...")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=600)
                except asyncio.TimeoutError:
                    pass
                continue

            count = min(wave_size, len(awake))
            wave = [awake[(index + offset) % len(awake)] for offset in range(count)]
            index = (index + count) % len(awake)

            self.log.info(
                "Online wave starting: %s",
                ", ".join(account.id for account in wave),
            )
            await asyncio.gather(
                *(self._run_wave_account(account, stop_event, online_seconds) for account in wave),
                return_exceptions=True,
            )

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wave_pause_seconds)
            except asyncio.TimeoutError:
                pass

    async def _run_wave_account(
        self,
        account: AccountRecord,
        stop_event: asyncio.Event,
        online_seconds: float,
    ) -> None:
        if stop_event.is_set() or not account.enabled:
            return

        try:
            async with create_client(self.config, account) as client:
                self.log.info("Online wave connected %s", account.id)
                keep_online_task = asyncio.create_task(client.keep_online(stop_event))
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=online_seconds)
                except asyncio.TimeoutError:
                    pass
                finally:
                    keep_online_task.cancel()
                    try:
                        await keep_online_task
                    except asyncio.CancelledError:
                        pass
                    self.log.info("Online wave finished %s", account.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = short_error(exc)
            disable = is_invalid_auth_error(exc)
            if disable:
                self.accounts.mark_error(account.id, error, disable=True)
                account.enabled = False
                self.log.error("Disabled invalid session %s: %s", account.id, error)
                return
            if not _is_transient_network_error(exc):
                self.accounts.mark_error(account.id, error, disable=False)
            self.log.error("Online wave failed for %s: %s", account.id, error)
