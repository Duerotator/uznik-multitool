from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

from core.config import AppConfig
from core.models import AccountRecord
from core.storage import read_json, update_json
from core.telegram_client import AccountBusyError, create_client
from modules.accounts import AccountService
from modules.warmup_settings import WarmupHistory, WarmupOptions, normalize_channels
from utils.telegram_errors import flood_wait_seconds, is_invalid_auth_error, short_error

PERSONAS_FILE = Path("data/personas.json")
MAX_ACTIONS_PER_HOUR = 20
BROWSE_DELAY_MIN, BROWSE_DELAY_MAX = 5, 20
CYCLE_PAUSE_MIN, CYCLE_PAUSE_MAX = 60, 180
SESSION_MAX_MINUTES = 20
ROTATION_PAUSE_MIN, ROTATION_PAUSE_MAX = 90, 300
REQUEST_TIMEOUT = 45


@dataclass
class Persona:
    persona_id: str
    name: str
    interests: list[str]
    channels: list[str]
    join_new: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "Persona":
        return cls(
            persona_id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            interests=list(data.get("interests", [])),
            channels=normalize_channels(data.get("channels", [])),
            join_new=data.get("join_new") is True,
        )


class PersonaStore:
    """Optional existing user templates; never fall back to invented channels."""
    def __init__(self, file_path: Path = PERSONAS_FILE):
        self.file_path = file_path
        self.personas: dict[str, Persona] = {}
        data = read_json(file_path, [])
        items = data if isinstance(data, list) else data.get("personas", []) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Each Warmup persona must be a JSON object.")
            persona = Persona.from_dict(item)
            if persona.persona_id and persona.channels:
                self.personas[persona.persona_id] = persona

    def get(self, persona_id: str | None) -> Persona | None:
        return self.personas.get(persona_id or "")


class WarmupBudgetExhausted(RuntimeError):
    pass


class WarmupRateLimiter:
    """Logical read/action attempts, not an exact count of underlying MTProto RPCs."""
    def __init__(self, max_per_hour: int = MAX_ACTIONS_PER_HOUR, storage_path: Path | None = None,
                 *, max_per_day: int = 80):
        self.max_per_hour, self.max_per_day = max_per_hour, max_per_day
        self.storage_path = storage_path
        data = read_json(storage_path, {}) if storage_path else {}
        self.accounts: dict[str, list[float]] = data.get("accounts", {})
        self.cooldowns: dict[str, float] = data.get("cooldowns", {})

    def _cleanup(self, account_id: str) -> None:
        now = time.time()
        if account_id in self.accounts:
            self.accounts[account_id] = sorted(t for t in self.accounts[account_id] if now - t < 86400)

    def can_act(self, account_id: str) -> bool:
        return self.remaining(account_id) > 0

    def record(self, account_id: str) -> None:
        if self.remaining(account_id) <= 0:
            raise WarmupBudgetExhausted("Warmup hourly/daily budget exhausted or account cooling down")
        self.accounts.setdefault(account_id, []).append(time.time())
        self._save()

    def cooldown(self, account_id: str, seconds: float) -> None:
        self.cooldowns[account_id] = max(self.cooldowns.get(account_id, 0), time.time() + seconds)
        self._save()

    def _save(self) -> None:
        if self.storage_path:
            update_json(self.storage_path, {}, lambda _: {"accounts": self.accounts, "cooldowns": self.cooldowns})

    def remaining(self, account_id: str) -> int:
        self._cleanup(account_id)
        now = time.time()
        if self.cooldowns.get(account_id, 0) > now:
            return 0
        attempts = self.accounts.get(account_id, [])
        return max(0, min(self.max_per_hour - sum(now - t < 3600 for t in attempts),
                          self.max_per_day - len(attempts)))

    def wait_seconds(self, account_id: str) -> float:
        self._cleanup(account_id)
        now = time.time()
        attempts = self.accounts.get(account_id, [])
        hourly = [t for t in attempts if now - t < 3600]
        ready = [now, self.cooldowns.get(account_id, 0)]
        if len(hourly) >= self.max_per_hour:
            ready.append(hourly[-self.max_per_hour] + 3600 if self.max_per_hour > 0 else now + 3600)
        if len(attempts) >= self.max_per_day:
            ready.append(attempts[-self.max_per_day] + 86400 if self.max_per_day > 0 else now + 86400)
        return max(0, max(ready) - now)


class _BudgetedClient:
    def __init__(self, client, limiter: WarmupRateLimiter, account_id: str, stop_event: asyncio.Event):
        self.client, self.limiter, self.account_id, self.stop_event = client, limiter, account_id, stop_event

    def __getattr__(self, name):
        method = getattr(self.client, name)
        async def call(*args, **kwargs):
            if self.stop_event.is_set():
                raise asyncio.CancelledError
            self.limiter.record(self.account_id)
            return await asyncio.wait_for(method(*args, **kwargs), timeout=REQUEST_TIMEOUT)
        return call


class WarmupEngine:
    def __init__(self, config: AppConfig, *, scheduler=None, options: WarmupOptions | None = None, on_status=None):
        self.config = config
        self.accounts = AccountService(config)
        self.options = (options or WarmupOptions.load(config.data_dir / "warmup_settings.json")).validate()
        self.personas = None if self.options.channels else PersonaStore(config.data_dir / "personas.json")
        self.history = WarmupHistory(config.data_dir / "warmup_history.json")
        self.limiter = WarmupRateLimiter(self.options.hourly_budget, config.data_dir / "warmup_limits.json",
                                        max_per_day=self.options.daily_budget)
        from modules.sleep_scheduler import SleepScheduler
        self.scheduler = scheduler or SleepScheduler(str(config.data_dir / "sleep_zones.json"))
        self.on_status = on_status
        self._offsets = {}
        self._statuses = {}
        self.log = logging.getLogger("warmup")

    def channels_for(self, account: AccountRecord) -> list[str]:
        # Explicit UI list wins. Otherwise only the account's assigned template is used.
        if self.options.channels:
            return list(self.options.channels)
        persona = self.personas.get(account.persona)
        return list(persona.channels) if persona else []

    def validate_accounts(self, accounts: list[AccountRecord]) -> None:
        missing = sum(not self.channels_for(account) for account in accounts if account.enabled)
        if missing:
            raise ValueError(f"Warmup: {missing} account(s) have no channels. Set public channels in Warmup / Sleep → Warmup settings.")

    def _report(self, account_id: str, status: str, stats: dict[str, int], *, terminal: bool = False) -> None:
        if not hasattr(self, "_statuses"):
            self._statuses = {}
        self._statuses[account_id] = status
        if self.on_status:
            offset = getattr(self, "_offsets", {}).get(account_id, {})
            counts = {key: value + offset.get(key, 0) for key, value in stats.items()}
            self.on_status({"account_id": account_id, "status": status, "stats": counts, "terminal": terminal})

    async def run_session(self, account: AccountRecord, stop_event: asyncio.Event, *, semaphore=None) -> dict[str, int]:
        stats = {"viewed": 0, "reacted": 0, "saved": 0, "joined": 0, "errors": 0, "cycles": 0, "skipped": 0}
        if stop_event.is_set() or not account.enabled:
            return stats
        self.scheduler.assign_timezone(account.id, account.phone)
        channels = self.channels_for(account)
        if not channels:
            raise ValueError("Warmup has no configured channels.")
        deadline = time.monotonic() + SESSION_MAX_MINUTES * 60
        active_slots = semaphore or asyncio.Semaphore(1)
        failures, cursor = 0, 0
        try:
            while not stop_event.is_set() and account.enabled and time.monotonic() < deadline:
                if self.options.cycles and stats["cycles"] >= self.options.cycles:
                    break
                if self.scheduler.is_sleeping(account.id):
                    wait = self.scheduler.wake_at(account.id) or 600
                    self._report(account.id, "Waiting for awake schedule", stats)
                    await self._wait(stop_event, min(wait, 600, deadline - time.monotonic()))
                    continue
                if self.limiter.remaining(account.id) <= 0:
                    wait = self.limiter.wait_seconds(account.id)
                    self._report(account.id, f"Cooldown/budget: {wait:.0f}s remaining", stats)
                    self.log.info("%s: Warmup budget/cooldown wait %.0fs", account.id, wait)
                    # A bounded run reports deferred work rather than spending 20 minutes on a day-long cooldown.
                    if self.options.cycles and wait > 120:
                        stats["skipped"] += 1
                        break
                    await self._wait(stop_event, min(max(wait, 1), 600, deadline - time.monotonic()))
                    continue
                available = [ch for ch in channels if self.history.available(account.id, ch)]
                if not available:
                    stats["skipped"] += 1
                    self._report(account.id, "All channels temporarily unavailable; deferred", stats)
                    break
                channel = available[cursor % len(available)]
                cursor += 1
                self._report(account.id, f"Queued: @{channel}", stats)
                await self._wait(stop_event, min(random.uniform(BROWSE_DELAY_MIN, BROWSE_DELAY_MAX), deadline - time.monotonic()))
                if stop_event.is_set() or time.monotonic() >= deadline:
                    break
                if self.scheduler.is_sleeping(account.id):
                    continue
                try:
                    async with active_slots:
                        if stop_event.is_set() or time.monotonic() >= deadline:
                            break
                        async with create_client(self.config, account, lock_wait_timeout=5.0) as client:
                            client.flood_wait_raise_after = 0
                            native = getattr(client, "client", None)
                            for attr in ("sleep_threshold", "flood_sleep_threshold"):
                                if native is not None and hasattr(native, attr):
                                    setattr(native, attr, 0)
                            self._report(account.id, f"Reading @{channel}", stats)
                            errors_before = stats["errors"]
                            await self._simulate_browser(_BudgetedClient(client, self.limiter, account.id, stop_event),
                                                         account, channel, stats)
                            failures = failures + 1 if stats["errors"] > errors_before else 0
                except AccountBusyError:
                    self._report(account.id, "Session busy; deferred", stats)
                    stats["skipped"] += 1
                except WarmupBudgetExhausted:
                    self._report(account.id, "Budget exhausted; deferred", stats)
                    stats["skipped"] += 1
                except Exception as exc:
                    if is_invalid_auth_error(exc):
                        raise
                    stats["errors"] += 1
                    failures += 1
                    seconds = flood_wait_seconds(exc)
                    if seconds is not None:
                        self.limiter.cooldown(account.id, seconds + 30)
                        self.log.warning("%s: FloodWait %ss; saved cooldown, no further requests", account.id, seconds)
                        self._report(account.id, f"FloodWait: {seconds}s; deferred", stats)
                        break
                    if self._unavailable_channel(exc):
                        self.history.block(account.id, channel)
                        self.log.warning("%s: @%s unavailable; excluded for 6h: %s", account.id, channel, short_error(exc))
                    else:
                        self.log.warning("%s: Warmup step on @%s failed: %s", account.id, channel, short_error(exc))
                stats["cycles"] += 1
                if failures >= 3:
                    self.limiter.cooldown(account.id, 600)
                    self._report(account.id, "Three consecutive failed cycles; paused for 10m", stats)
                    break
                self._report(account.id, f"Cycle {stats['cycles']} finished", stats)
                if self.options.cycles and stats["cycles"] >= self.options.cycles:
                    break
                await self._wait(stop_event, min(random.randint(CYCLE_PAUSE_MIN, CYCLE_PAUSE_MAX), deadline - time.monotonic()))
        except asyncio.CancelledError:
            pass  # Return partial successful counts when Stop cancels active work.
        except Exception as exc:
            if is_invalid_auth_error(exc):
                self.accounts.mark_error(account.id, short_error(exc), disable=True)
                account.enabled = False
            stats["errors"] += 1
            self.log.error("%s: Warmup failed: %s", account.id, short_error(exc))
        if not stop_event.is_set() and self.options.cycles and stats["cycles"] < self.options.cycles:
            stats["skipped"] += 1
        return stats

    @staticmethod
    async def _wait(stop_event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(0, seconds))
        except TimeoutError:
            pass

    @staticmethod
    def _unavailable_channel(exc: Exception) -> bool:
        text = (type(exc).__name__ + " " + str(exc)).replace("_", "").lower()
        return any(code in text for code in ("usernameinvalid", "usernamenotoccupied", "channelprivate", "peeridinvalid"))

    async def _recent_post_links(self, client, channel: str, limit: int) -> list[str]:
        messages = await client.get_recent_chat_messages(channel, limit=limit, include_media=True)
        return list(dict.fromkeys(f"https://t.me/{channel}/{message['id']}"
                                  for message in messages if message.get("id")))

    async def _simulate_browser(self, client, account: AccountRecord, channel: str, stats: dict[str, int]) -> None:
        # Historical method name retained for compatibility; these are MTProto reads, not browser emulation.
        if self.options.join_channels and not self.history.joined(account.id, channel):
            await client.join_chat(channel)
            self.history.record_join(account.id, channel)
            stats["joined"] += 1
        links = await self._recent_post_links(client, channel, limit=20)
        fresh = [link for link in links if not self.history.done(account.id, link, "viewed")]
        viewed = []
        for link in fresh[:self.options.posts_per_cycle]:
            try:
                await client.view_post(link)
                self.history.record(account.id, link, "viewed")
                viewed.append(link)
                stats["viewed"] += 1
                self._report(account.id, f"Viewed {link}", stats)
            except Exception as exc:
                if self._must_stop_cycle(exc) or self._unavailable_channel(exc):
                    raise
                stats["errors"] += 1
                self.log.warning("%s: view failed: %s", account.id, short_error(exc))
        if not fresh:
            stats["skipped"] += 1
            self.log.info("%s: @%s has no new readable posts", account.id, channel)
        # Optional writes are deterministic and only applied once per retained post.
        eligible = viewed + [link for link in links if self.history.done(account.id, link, "viewed") and link not in viewed]
        for action, enabled in (("reacted", self.options.reactions), ("saved", self.options.save_posts)):
            if not enabled:
                continue
            link = next((link for link in eligible if not self.history.done(account.id, link, action)), None)
            if link is None:
                continue
            try:
                if action == "reacted":
                    await client.random_reaction(link, mark_viewed=False)
                else:
                    await client.send_message("me", link)
                self.history.record(account.id, link, action)
                stats[action] += 1
            except Exception as exc:
                if self._must_stop_cycle(exc):
                    raise
                stats["errors"] += 1
                self.log.warning("%s: %s failed: %s", account.id, action, short_error(exc))

    @staticmethod
    def _must_stop_cycle(exc: Exception) -> bool:
        return isinstance(exc, WarmupBudgetExhausted) or flood_wait_seconds(exc) is not None or is_invalid_auth_error(exc)

    async def run_for_group(self, group: str, stop_event: asyncio.Event, *, accounts=None) -> dict[str, dict[str, int]]:
        accounts = self.accounts.list_accounts(group=group, enabled_only=True) if accounts is None else [a for a in accounts if a.enabled]
        if not accounts:
            return {}
        self.validate_accounts(accounts)
        self._offsets = {}
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        results = {}
        self.log.info("Warmup: %d filtered accounts, cycles=%s, budgets=%d/hour %d/day; actions=%s",
                      len(accounts), self.options.cycles or "continuous", self.options.hourly_budget,
                      self.options.daily_budget,
                      [name for name in ("reactions", "save_posts", "join_channels") if getattr(self.options, name)])

        async def worker(account):
            totals = dict.fromkeys(("viewed", "reacted", "saved", "joined", "errors", "cycles", "skipped"), 0)
            results[account.id] = totals
            self._offsets[account.id] = {}
            self._report(account.id, "Queued", totals)
            while not stop_event.is_set():
                try:
                    fresh = self.accounts.get_account(account.id)
                except KeyError:
                    totals["skipped"] += 1
                    self._report(account.id, "Account removed; skipped", totals)
                    break
                if not fresh.enabled:
                    totals["skipped"] += 1
                    self._report(account.id, "Account disabled; skipped", totals)
                    break
                self._offsets[account.id] = dict(totals)
                stats = await self.run_session(fresh, stop_event, semaphore=semaphore)
                for key, value in stats.items():
                    totals[key] = totals.get(key, 0) + value
                self._offsets[account.id] = {}
                if self.options.cycles or stop_event.is_set() or not fresh.enabled:
                    break
                self._report(account.id, "Rotation pause", totals)
                await self._wait(stop_event, random.randint(ROTATION_PAUSE_MIN, ROTATION_PAUSE_MAX))
            return totals

        tasks = {account.id: asyncio.create_task(worker(account)) for account in accounts}
        stopped = asyncio.create_task(stop_event.wait())
        finished = asyncio.gather(*tasks.values(), return_exceptions=True)
        try:
            await asyncio.wait((stopped, finished), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            stop_event.set()
        finally:
            interrupted = stop_event.is_set()
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            values = await finished
            for (account_id, _), value in zip(tasks.items(), values):
                results.setdefault(account_id, dict.fromkeys(("viewed", "reacted", "saved", "joined", "errors", "cycles", "skipped"), 0))
                if isinstance(value, dict):
                    results[account_id] = value
                elif isinstance(value, Exception):
                    results[account_id]["errors"] += 1
                    self.log.warning("%s: Warmup worker failed: %s", account_id, short_error(value))
                totals = results[account_id]
                self._offsets[account_id] = {}
                if interrupted:
                    status = "Stopped"
                elif totals["errors"]:
                    status = "Finished with errors: " + self._statuses.get(account_id, "")
                elif totals["skipped"] and not totals["viewed"]:
                    status = "Deferred / no new posts: " + self._statuses.get(account_id, "")
                else:
                    status = "Finished"
                self._report(account_id, status, totals, terminal=True)
        self.log.info("Warmup finished: %s", {key: sum(row.get(key, 0) for row in results.values())
                                            for key in ("viewed", "reacted", "saved", "joined", "errors", "skipped")})
        return results
