from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.models import AccountRecord
from core.storage import read_json
from core.telegram_client import create_client
from modules.accounts import AccountService
from utils.rate_limit import human_delay
from utils.telegram_errors import is_invalid_auth_error, short_error

logger = logging.getLogger("warmup")

PERSONAS_FILE = Path("data/personas.json")

DEFAULT_PERSONAS: list[dict[str, Any]] = [
    {
        "id": "crypto_trader",
        "name": "Crypto Trader",
        "interests": ["Crypto", "Trading", "DeFi", "NFT"],
        "channels": [
            "crypto_news", "forklog", "bitcoininfo", "DeFiEducation",
            "tradingview", "CryptoMarkets", "coinmarketcap", "whale_alert",
            "cryptomemology", "NFT_NEWS", "metaversenews", "DAO_Talks",
            "web3_academy", "airdrop_today", "crypto_signals_pro",
        ],
        "join_new": True,
    },
    {
        "id": "tech_geek",
        "name": "Tech Geek",
        "interests": ["Tech", "AI", "Startups", "Programming"],
        "channels": [
            "d_code", "tproger_official", "ai_newz", "habr_com",
            "productstar", "devopstalk", "android_google", "apple_insider",
            "tesla_motors", "github_trends", "python_academy", "datascience_ru",
            "machinelearning_ai", "startup_of_the_day", "venture_capital",
        ],
        "join_new": True,
    },
    {
        "id": "news_junkie",
        "name": "News Junkie",
        "interests": ["News", "Politics", "World", "Economy"],
        "channels": [
            "meduza_news", "breakingmash", "rbc_news", "kommersant_news",
            "rt_russian", "bbcnews", "reuters_world", "bloomberg_economics",
            "sport_express", "culture_news", "science_daily", "techcrunch",
            "theeconomist", "forbes_russia", "prime_economics",
        ],
        "join_new": True,
    },
    {
        "id": "design_creative",
        "name": "Design & Creative",
        "interests": ["Design", "Art", "Photography", "Fashion"],
        "channels": [
            "design_tg", "uiux_design", "typography_daily", "behance",
            "art_news", "streetart_tg", "photography_tips", "fashion_week",
            "architecture_daily", "motion_design", "color_palette", "graphic_design",
            "illustration_tg", "brand_identity", "creative_boom",
        ],
        "join_new": True,
    },
    {
        "id": "gaming_community",
        "name": "Gaming Community",
        "interests": ["Gaming", "Esports", "Reviews", "Streaming"],
        "channels": [
            "games_tg", "esports_news", "steam_sales", "ps5_community",
            "xbox_games", "nintendo_switch", "mobile_gaming", "pc_gaming",
            "twitch_streams", "gaming_memes", "retro_games", "indie_dev",
            "game_dev_ru", "cs2_updates", "dota2_news",
        ],
        "join_new": True,
    },
    {
        "id": "business_mind",
        "name": "Business Mind",
        "interests": ["Business", "Marketing", "Finance", "Management"],
        "channels": [
            "business_secrets", "marketing_tg", "smm_planner", "finance_ru",
            "investments_tg", "startups_ru", "hr_management", "sales_methods",
            "ecommerce_news", "logistics_tg", "realestate_market", "crypto_business",
            "product_management", "analytics_tg", "strategy_talks",
        ],
        "join_new": True,
    },
    {
        "id": "health_fitness",
        "name": "Health & Fitness",
        "interests": ["Health", "Fitness", "Nutrition", "Wellness"],
        "channels": [
            "health_tips_tg", "fitness_pro", "yoga_daily", "nutrition_facts",
            "mental_health_tg", "running_community", "crossfit_news", "weightlifting",
            "meditation_guide", "healthy_recipes", "supplements_info", "sleep_science",
            "sport_nutrition", "biohacking_ru", "wellness_hub",
        ],
        "join_new": True,
    },
    {
        "id": "travel_explorer",
        "name": "Travel Explorer",
        "interests": ["Travel", "Culture", "Food", "Adventure"],
        "channels": [
            "travel_tg_ru", "backpacker_stories", "foodie_world", "adventure_trips",
            "cheap_flights", "hotel_reviews", "road_trip_ideas", "expat_life",
            "cultural_events", "festival_guide", "travel_photography", "local_food",
            "nature_lovers", "digital_nomad", "travel_hacks",
        ],
        "join_new": True,
    },
]

REACTION_CHANCE = 0.25
SAVE_TO_FAVORITES_CHANCE = 0.08
JOIN_NEW_CHANCE = 0.10
VIEW_POST_COUNT = (2, 6)
MAX_ACTIONS_PER_HOUR = 20
SESSION_COOLDOWN_MINUTES = 8
BROWSE_DELAY_MIN = 5
BROWSE_DELAY_MAX = 20
CYCLE_PAUSE_MIN = 60
CYCLE_PAUSE_MAX = 180
# A session holds one of the max_concurrency connection slots, so it has to end
# for the remaining accounts to ever get a turn.
SESSION_MAX_MINUTES = 20
ROTATION_PAUSE_MIN = 90
ROTATION_PAUSE_MAX = 300


@dataclass
class Persona:
    persona_id: str
    name: str
    interests: list[str]
    channels: list[str]
    join_new: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "Persona":
        return cls(
            persona_id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            interests=list(data.get("interests", [])),
            channels=list(data.get("channels", [])),
            join_new=bool(data.get("join_new", True)),
        )


class PersonaStore:
    def __init__(self, file_path: Path = PERSONAS_FILE):
        self.file_path = file_path
        self.personas: dict[str, Persona] = {}
        self._load()

    def _load(self) -> None:
        data = read_json(self.file_path, DEFAULT_PERSONAS)
        if isinstance(data, list):
            for item in data:
                persona = Persona.from_dict(item)
                self.personas[persona.persona_id] = persona
        elif isinstance(data, dict):
            for item in data.get("personas", []):
                persona = Persona.from_dict(item)
                self.personas[persona.persona_id] = persona

    def get(self, persona_id: str | None) -> Persona | None:
        if not persona_id:
            return None
        return self.personas.get(persona_id)

    def random(self) -> Persona:
        return random.choice(list(self.personas.values()))


class WarmupRateLimiter:
    def __init__(self, max_per_hour: int = MAX_ACTIONS_PER_HOUR):
        self.max_per_hour = max_per_hour
        self.accounts: dict[str, list[float]] = {}

    def _cleanup(self, account_id: str) -> None:
        now = time.monotonic()
        if account_id in self.accounts:
            self.accounts[account_id] = [t for t in self.accounts[account_id] if now - t < 3600]

    def can_act(self, account_id: str) -> bool:
        return self.remaining(account_id) > 0

    def record(self, account_id: str) -> None:
        if account_id not in self.accounts:
            self.accounts[account_id] = []
        self.accounts[account_id].append(time.monotonic())

    def remaining(self, account_id: str) -> int:
        self._cleanup(account_id)
        return self.max_per_hour - len(self.accounts.get(account_id, []))


class WarmupEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.personas = PersonaStore()
        self.limiter = WarmupRateLimiter()
        from modules.sleep_scheduler import SleepScheduler
        self.scheduler = SleepScheduler()
        self.log = logging.getLogger("warmup")

    async def run_session(self, account: AccountRecord, stop_event: asyncio.Event) -> dict[str, int]:
        stats = {"viewed": 0, "reacted": 0, "saved": 0, "joined": 0, "errors": 0}

        if stop_event.is_set() or not account.enabled:
            return stats

        self.scheduler.assign_timezone(account.id, account.phone)
        persona = self.personas.get(account.persona) or self.personas.random()
        channels = list(persona.channels)

        deadline = time.monotonic() + SESSION_MAX_MINUTES * 60
        try:
            async with create_client(self.config, account) as client:
                self.log.info("Warmup connected %s (%s)", account.id, persona.name)

                while not stop_event.is_set() and time.monotonic() < deadline:
                    if self.scheduler.is_sleeping(account.id):
                        wait = self.scheduler.wake_at(account.id) or 600
                        self.log.info("%s sleeping, waiting %.0fs", account.id, wait)
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=min(wait, 600))
                        except asyncio.TimeoutError:
                            pass
                        continue

                    if self.limiter.remaining(account.id) <= 0:
                        self.log.info("Rate limit reached for %s, cooling down.", account.id)
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=SESSION_COOLDOWN_MINUTES * 60)
                        except asyncio.TimeoutError:
                            pass
                        continue

                    if not channels:
                        channels = list(persona.channels)

                    channel = random.choice(channels)
                    channels.remove(channel)

                    try:
                        await self._simulate_browser(client, account, channel, stats)
                    except Exception as exc:
                        err = short_error(exc)
                        if is_invalid_auth_error(exc):
                            self.accounts.mark_error(account.id, err, disable=True)
                            raise
                        self.log.warning("Warmup step failed for %s on %s: %s", account.id, channel, err)
                        stats["errors"] += 1

                    self.limiter.record(account.id)

                    pause = random.randint(CYCLE_PAUSE_MIN, CYCLE_PAUSE_MAX)
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=pause)
                    except asyncio.TimeoutError:
                        pass

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            err = short_error(exc)
            if is_invalid_auth_error(exc):
                self.accounts.mark_error(account.id, err, disable=True)
            self.log.error("Warmup failed for %s: %s", account.id, err)
            stats["errors"] += 1

        return stats

    async def _recent_post_links(self, client, channel: str, limit: int) -> list[str]:
        """Turn a channel username into real post links.

        view_post() and set_reaction() both go through parse_post_link(), which
        rejects a bare username — the message id is what makes the link valid.
        """
        messages = await client.get_recent_chat_messages(channel, limit=limit)
        target = channel.lstrip("@")
        return [
            f"https://t.me/{target}/{message['id']}"
            for message in messages
            if message.get("id")
        ]

    async def _simulate_browser(
        self, client, account: AccountRecord, channel: str, stats: dict[str, int]
    ) -> None:
        delay = random.uniform(BROWSE_DELAY_MIN, BROWSE_DELAY_MAX)
        self.log.debug("%s browsing %s (%.1fs)...", account.id, channel, delay)
        await asyncio.sleep(delay)

        view_count = random.randint(*VIEW_POST_COUNT)
        links = await self._recent_post_links(client, channel, limit=max(view_count, 8))
        if not links:
            self.log.info("%s: %s has no readable posts, skipping.", account.id, channel)
            return

        random.shuffle(links)
        viewed: list[str] = []
        for link in links[:view_count]:
            try:
                await client.view_post(link)
            except Exception as exc:
                self.log.warning("%s view failed on %s: %s", account.id, link, short_error(exc))
                continue
            viewed.append(link)
            stats["viewed"] += 1
            await human_delay(0.5, 2.0)

        if viewed and random.random() < REACTION_CHANCE:
            link = random.choice(viewed)
            try:
                # random_reaction picks from what the post actually allows and
                # builds the ReactionChoice itself.
                detail = await client.random_reaction(link)
                stats["reacted"] += 1
                self.log.info("%s reacted on %s (%s)", account.id, link, detail)
            except Exception as exc:
                self.log.warning("%s reaction failed on %s: %s", account.id, link, short_error(exc))

        if random.random() < SAVE_TO_FAVORITES_CHANCE:
            try:
                await client.send_message("me", random.choice(viewed) if viewed else f"https://t.me/{channel}")
                stats["saved"] += 1
            except Exception as exc:
                self.log.warning("%s save to favorites failed for %s: %s", account.id, channel, short_error(exc))

        if random.random() < JOIN_NEW_CHANCE:
            persona = self.personas.get(account.persona) or self.personas.random()
            if persona.join_new and persona.channels:
                new_ch = random.choice(persona.channels)
                try:
                    await client.join_chat(new_ch)
                    stats["joined"] += 1
                    self.log.info("%s joined %s", account.id, new_ch)
                except Exception as exc:
                    self.log.warning("%s join failed for %s: %s", account.id, new_ch, short_error(exc))

    async def run_for_group(self, group: str, stop_event: asyncio.Event) -> dict[str, dict[str, int]]:
        accounts = self.accounts.list_accounts(group=group, enabled_only=True)
        if not accounts:
            self.log.info("Warmup skipped: no enabled accounts in %s", group)
            return {}

        limit = max(1, self.config.max_concurrency)
        self.log.info(
            "Warmup starting for %d accounts in %s (%d at a time)", len(accounts), group, limit
        )
        semaphore = asyncio.Semaphore(limit)
        results: dict[str, dict[str, int]] = {}

        async def worker(account: AccountRecord) -> dict[str, int]:
            totals = {"viewed": 0, "reacted": 0, "saved": 0, "joined": 0, "errors": 0}
            while not stop_event.is_set():
                async with semaphore:
                    stats = await self.run_session(account, stop_event)
                for key, value in stats.items():
                    totals[key] = totals.get(key, 0) + value
                if stop_event.is_set() or not account.enabled:
                    break
                # Release the slot for a while so every account gets a turn.
                pause = random.randint(ROTATION_PAUSE_MIN, ROTATION_PAUSE_MAX)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=pause)
                except asyncio.TimeoutError:
                    continue
            return totals

        tasks = {account.id: asyncio.create_task(worker(account)) for account in accounts}

        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass

        for task in list(tasks.values()):
            task.cancel()

        for account_id, task in tasks.items():
            try:
                results[account_id] = await task
            except asyncio.CancelledError:
                results[account_id] = {"viewed": 0, "reacted": 0, "saved": 0, "joined": 0, "errors": 0}
            except Exception as exc:
                self.log.warning("Warmup worker for %s ended badly: %s", account_id, short_error(exc))
                results[account_id] = {"viewed": 0, "reacted": 0, "saved": 0, "joined": 0, "errors": 1}

        total = {
            "viewed": sum(r.get("viewed", 0) for r in results.values()),
            "reacted": sum(r.get("reacted", 0) for r in results.values()),
            "saved": sum(r.get("saved", 0) for r in results.values()),
            "joined": sum(r.get("joined", 0) for r in results.values()),
            "errors": sum(r.get("errors", 0) for r in results.values()),
        }
        self.log.info("Warmup finished: %s", total)
        return results
