from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from modules.giveaway_service import GiveawayService, detect_giveaway_provider, normalize_provider
from core.telegram_client import telegram_invite_hash
from modules.raffle_common import (
    RAFFLE_MAX_FLOOD_WAIT_SECONDS,
    configure_raffle_client,
    flood_wait_seconds,
    is_long_raffle_flood_wait,
    playwright_proxy,
    raffle_flood_skip_message,
)
from modules.raffle_random import JoinOutcome, RaffleTarget, RandomRaffleService, extract_channel_targets

from modules.raffle_bestrandom import BestRandomService, BestRandomTarget
from modules.raffle_fastgiveaway import FastGiveawayService, FastGiveawayTarget
from modules.raffle_randombeast import RandomBeastService, RandomBeastTarget


class GiveawayDetectionTests(unittest.TestCase):
    def test_extracts_public_and_invite_channel_targets(self) -> None:
        self.assertEqual(
            ["@golub_podarki", "https://t.me/+InviteCode"],
            extract_channel_targets("https://t.me/golub_podarki https://t.me/+InviteCode"),
        )

    def test_extracts_telegram_invite_hash_without_resolving_a_username(self) -> None:
        self.assertEqual("MB5ybv3ZypszNGJi", telegram_invite_hash("https://t.me/+MB5ybv3ZypszNGJi"))
        self.assertEqual("OldInvite", telegram_invite_hash("https://t.me/joinchat/OldInvite"))

    def test_inspection_retries_with_next_account_after_long_flood_wait(self) -> None:
        class FloodError(RuntimeError):
            value = 33441

            def __str__(self) -> str:
                return "Telegram says: [420 FLOOD_WAIT_X] - Please wait 33441 seconds"

        class Client:
            flood_wait_raise_after = 60

            def __init__(self, account_id: str):
                self.account_id = account_id

            async def get_message_detail(self, _link: str) -> dict[str, object]:
                if self.account_id == "flooded":
                    raise FloodError()
                return {"text": "", "buttons": []}

        class ClientContext:
            def __init__(self, account_id: str):
                self.client = Client(account_id)

            async def __aenter__(self):
                return self.client

            async def __aexit__(self, *_args):
                return None

        service = object.__new__(GiveawayService)
        service.config = object()
        flooded = types.SimpleNamespace(id="flooded", enabled=True)
        usable = types.SimpleNamespace(id="usable", enabled=True)
        with patch("modules.giveaway_service.create_client", side_effect=lambda _config, account: ClientContext(account.id)):
            inspection = asyncio.run(
                service.inspect(
                    "https://t.me/channel/10",
                    provider="random",
                    probe_accounts=[flooded, usable],
                )
            )
        self.assertEqual("usable", inspection.probe_account_id)

    def test_long_flood_wait_is_detected_and_reported_as_a_skip(self) -> None:
        exc = RuntimeError(
            "Telegram says: [420 FLOOD_WAIT_X] - Please wait 33809 seconds before repeating the action."
        )
        self.assertEqual(33809, flood_wait_seconds(exc))
        self.assertTrue(is_long_raffle_flood_wait(exc))
        self.assertEqual("skipped: FloodWait 33809s (limit 30s)", raffle_flood_skip_message(exc))

    def test_raffle_client_raises_instead_of_sleeping_after_30_seconds(self) -> None:
        client = types.SimpleNamespace(flood_wait_raise_after=60)
        configure_raffle_client(client)
        self.assertEqual(RAFFLE_MAX_FLOOD_WAIT_SECONDS, client.flood_wait_raise_after)

    def test_detects_supported_engines_from_post_buttons(self) -> None:
        cases = {
            "random": {"url": "https://t.me/Random/JoinLot?startapp=123G"},
            "bestrandom": {"url": "https://t.me/BestRandom_bot?start=abc"},
            "fastgiveaway": {"url": "https://t.me/FastGiveawaysBot?start=abc"},
            "randombeast": {"web_app_url": "https://t.me/randombeast_bot/devapp?startapp=abc"},
        }
        for expected, button in cases.items():
            with self.subTest(expected=expected):
                provider, _label = detect_giveaway_provider(
                    "https://t.me/source/10",
                    {"buttons": [{"text": "Participate", **button}]},
                )
                self.assertEqual(expected, provider)

    def test_provider_aliases_are_normalized(self) -> None:
        self.assertEqual("fastgiveaway", normalize_provider("fast_giveaway"))
        self.assertEqual("randombeast", normalize_provider("random-beast"))
        with self.assertRaises(ValueError):
            normalize_provider("unknown")

    def test_direct_link_can_be_inspected_without_telegram(self) -> None:
        service = object.__new__(GiveawayService)
        inspected = asyncio.run(
            service.inspect("https://t.me/BestRandom_bot?start=abc", provider="auto")
        )
        self.assertEqual("bestrandom", inspected.provider)
        self.assertIsNone(inspected.post_link)

    def test_playwright_uses_account_proxy_credentials(self) -> None:
        self.assertEqual(
            {
                "server": "socks5://127.0.0.1:21102",
                "username": "name",
                "password": "secret",
            },
            playwright_proxy("socks5h://name:secret@127.0.0.1:21102"),
        )


    def test_detects_randomized_joinlot_button(self) -> None:
        provider, label = detect_giveaway_provider(
            "https://t.me/simbatgk/115",
            {
                "buttons": [
                    {
                        "text": "Я в деле!",
                        "url": "https://t.me/Randomized/JoinLot?startapp=4270416Aa977d4b_4226406694",
                    }
                ]
            },
        )
        self.assertEqual("random", provider)
        self.assertEqual("Я в деле!", label)

class PostResolverOrderTests(unittest.TestCase):
    def test_fast_giveaway_extracts_required_channels_from_bot_buttons(self) -> None:
        button = types.SimpleNamespace(url="https://t.me/golub_podarki")
        message = types.SimpleNamespace(
            reply_markup=types.SimpleNamespace(inline_keyboard=[[button]]),
        )
        self.assertEqual(["@golub_podarki"], FastGiveawayService._required_channels(message))

    def test_best_random_extracts_required_channels_from_bot_buttons(self) -> None:
        button = types.SimpleNamespace(url="https://t.me/golub_podarki")
        message = types.SimpleNamespace(
            reply_markup=types.SimpleNamespace(inline_keyboard=[[button]]),
        )
        self.assertEqual(["@golub_podarki"], BestRandomService._required_channels(message))

    def test_best_random_reads_post_before_requiring_start_param(self) -> None:
        service = object.__new__(BestRandomService)

        async def fake(_self, link: str, extra: list[str], probe=None) -> BestRandomTarget:
            return BestRandomTarget(link, "best", extra, link)

        service._resolve_from_post = types.MethodType(fake, service)
        target = asyncio.run(service._resolve_target("https://t.me/channel/123", ["@required"]))
        self.assertEqual("best", target.start_param)

    def test_fast_giveaway_reads_post_before_requiring_start_param(self) -> None:
        service = object.__new__(FastGiveawayService)

        async def fake(
            _self, link: str, extra: list[str], existing: str | None, probe=None,
        ) -> FastGiveawayTarget:
            self.assertIsNone(existing)
            return FastGiveawayTarget(link, "fast", extra, link)

        service._resolve_from_post = types.MethodType(fake, service)
        target = asyncio.run(service._resolve_target("https://t.me/channel/123", ["@required"]))
        self.assertEqual("fast", target.start_param)

    def test_random_beast_reads_startapp_from_post_resolver(self) -> None:
        service = object.__new__(RandomBeastService)

        async def fake(_self, link: str, extra: list[str], probe=None) -> RandomBeastTarget:
            return RandomBeastTarget(link, "beast", extra, link)

        service._resolve_from_post = types.MethodType(fake, service)
        target = asyncio.run(service._resolve_target("https://t.me/channel/123", ["@required"]))
        self.assertEqual("beast", target.start_param)

    def test_random_beast_reads_required_channels_from_mini_app_links(self) -> None:
        class Locator:
            async def evaluate_all(self, _script: str):
                return ["https://t.me/golub_podarki", "https://t.me/randombeast_bot"]

        class Page:
            def locator(self, selector: str):
                if selector != "a[href]":
                    raise AssertionError(selector)
                return Locator()

        channels = asyncio.run(RandomBeastService._page_required_channels(Page()))
        self.assertEqual(["@golub_podarki"], channels)

    def test_random_reads_required_channels_from_mini_app_links(self) -> None:
        class Locator:
            async def evaluate_all(self, _script: str):
                return ["https://t.me/golub_podarki", "https://t.me/random"]

        class Page:
            def locator(self, selector: str):
                if selector != "a[href]":
                    raise AssertionError(selector)
                return Locator()

        channels = asyncio.run(RandomRaffleService._page_required_channels(Page()))
        self.assertEqual(["@golub_podarki"], channels)

    def test_random_reads_html_when_link_extraction_fails(self) -> None:
        class Page:
            def locator(self, _selector):
                raise RuntimeError("fixture link extraction failed")

            async def content(self):
                return '<a href="https://t.me/+InviteCode">Required channel</a>'

        channels = asyncio.run(RandomRaffleService._page_required_channels(Page()))
        self.assertEqual(["https://t.me/+InviteCode"], channels)

    def test_random_retries_with_channels_discovered_by_mini_app(self) -> None:
        class Client:
            async def request_bot_app_webview(self, *_args, **_kwargs):
                return {"url": "https://example.test/join"}

        class ClientContext:
            async def __aenter__(self):
                return Client()

            async def __aexit__(self, *_args):
                return None

        service = object.__new__(RandomRaffleService)
        service.config = types.SimpleNamespace(global_proxy=None)
        service.log = types.SimpleNamespace(info=lambda *_args, **_kwargs: None)
        joined: list[list[str]] = []
        outcomes = iter([
            JoinOutcome("need_channels", "NOT_ALL_CHANNELS_SUBSCRIBED", {"channels": ["@dynamic"]}),
            JoinOutcome("ok", "joined"),
        ])

        async def ensure(_client, channels: list[str], *, view_from: str | None) -> None:
            joined.append(channels)

        async def browser(*_args, **_kwargs) -> JoinOutcome:
            return next(outcomes)

        service._ensure_channels = ensure
        service._browser_join_flow = browser
        account = types.SimpleNamespace(id="probe", proxy=None)
        target = RaffleTarget(source="test", start_param="demo", channels=["@static"])
        with patch("modules.raffle_random.create_client", return_value=ClientContext()), patch(
            "modules.raffle_random.configure_raffle_client"
        ):
            outcome = asyncio.run(service._participate_one(account, target))

        self.assertEqual("ok", outcome.status)
        self.assertEqual([["@static"], ["@static", "@dynamic"]], joined)

    def test_random_retries_once_after_captcha_failure(self) -> None:
        class Client:
            async def request_bot_app_webview(self, *_args, **_kwargs):
                return {"url": "https://example.test/join"}

        class ClientContext:
            async def __aenter__(self):
                return Client()

            async def __aexit__(self, *_args):
                return None

        service = object.__new__(RandomRaffleService)
        service.config = types.SimpleNamespace(global_proxy=None)
        service.log = types.SimpleNamespace(info=lambda *_args, **_kwargs: None)
        outcomes = iter([
            JoinOutcome("captcha_failed", "captcha solve failed"),
            JoinOutcome("ok", "joined"),
        ])
        browser_calls = 0

        async def ensure(*_args, **_kwargs) -> None:
            return None

        async def browser(*_args, **_kwargs) -> JoinOutcome:
            nonlocal browser_calls
            browser_calls += 1
            return next(outcomes)

        service._ensure_channels = ensure
        service._browser_join_flow = browser
        account = types.SimpleNamespace(id="probe", proxy=None)
        target = RaffleTarget(source="test", start_param="demo")
        with patch("modules.raffle_random.create_client", return_value=ClientContext()), patch(
            "modules.raffle_random.configure_raffle_client"
        ):
            outcome = asyncio.run(service._participate_one(account, target))

        self.assertEqual("ok", outcome.status)
        self.assertEqual(2, browser_calls)


class ForwardedPostInspectionTests(unittest.TestCase):

    def test_inspect_uses_supported_public_forward_origin(self) -> None:
        service = object.__new__(GiveawayService)
        service.config = object()
        account = types.SimpleNamespace(id="probe", enabled=True)
        service.accounts = types.SimpleNamespace(list_accounts=lambda **_kwargs: [account])
        aggregator = "https://t.me/tgbpump/101821"
        original = "https://t.me/source_channel/77"
        details = {
            aggregator: {"chat": "tgbpump", "text": "forwarded", "buttons": [], "hidden_links": [], "forward_post_link": original},
            original: {"chat": "source_channel", "text": "giveaway", "buttons": [{"text": "Participate", "url": "https://t.me/FastGiveawaysBot?start=demo"}], "hidden_links": []},
        }
        class Client:
            async def get_message_detail(self, link: str) -> dict[str, object]:
                return details[link]
        class ClientContext:
            async def __aenter__(self):
                return Client()
            async def __aexit__(self, _type, _value, _traceback):
                return False
        with patch("modules.giveaway_service.create_client", return_value=ClientContext()):
            inspection = asyncio.run(service.inspect(aggregator, probe_account=account))
        self.assertEqual("fastgiveaway", inspection.provider)
        self.assertEqual(original, inspection.post_link)
        self.assertEqual("probe", inspection.probe_account_id)
if __name__ == "__main__":
    unittest.main()
