from __future__ import annotations

import logging
from pathlib import Path

import httpx

from core.config import AppConfig

logger = logging.getLogger("webshare-source")

BASE = "https://proxy.webshare.io/api/v2"


class WebshareSource:
    """Загрузка прокси из Webshare API по сохранённым API ключам."""

    def __init__(self, accounts_file: Path | None = None):
        self.accounts_file = accounts_file or Path("data/webshare_accounts.json")
        self._cache: list[tuple[str, int, str, str, str]] = []

    def _load_accounts(self) -> list[dict]:
        if not self.accounts_file.exists():
            return []
        import json
        return json.loads(self.accounts_file.read_text(encoding="utf-8")).get("accounts", [])

    async def fetch(self) -> list[tuple[str, int, str, str, str]]:
        """Загрузить прокси из всех аккаунтов.

        Returns:
            [(ip, port, protocol, username, password), ...]
        """
        accounts = self._load_accounts()
        if not accounts:
            return []

        proxies: list[tuple[str, int, str, str, str]] = []
        max_pages = 50
        async with httpx.AsyncClient(timeout=15) as client:
            for account_index, account in enumerate(accounts, start=1):
                api_key = account.get("api_key", "")
                if not api_key:
                    continue
                next_url: str | None = f"{BASE}/proxy/list/?mode=direct&page=1&page_size=100"
                pages = 0
                try:
                    while next_url and pages < max_pages:
                        pages += 1
                        r = await client.get(
                            next_url,
                            headers={"Authorization": f"Token {api_key}"},
                        )
                        if r.status_code != 200:
                            logger.warning("Webshare API error for account #%d: HTTP %d", account_index, r.status_code)
                            break
                        data = r.json()
                        results = data.get("results", []) or []
                        for p in results:
                            if not p.get("valid"):
                                continue
                            proxies.append((
                                p["proxy_address"],
                                p["port"],
                                "http",
                                p["username"],
                                p["password"],
                            ))
                        next_url = data.get("next") or None
                        if not results:
                            break
                except Exception as exc:
                    # Response errors may contain credentials; log only the type.
                    logger.warning("Webshare fetch failed for account #%d (%s)", account_index, type(exc).__name__)

        self._cache = proxies
        logger.info("Webshare source: %d proxies from %d accounts", len(proxies), len(accounts))
        return proxies

    async def refresh(self) -> int:
        """Обновить кэш и вернуть количество."""
        await self.fetch()
        return len(self._cache)

    def get_cached(self) -> list[tuple[str, int, str, str, str]]:
        return self._cache


_INSTANCES: dict[str, WebshareSource] = {}


def get_webshare_source(config: AppConfig | None = None) -> WebshareSource:
    """Per-data-dir instance of WebshareSource."""
    accounts_file = (config.data_dir / "webshare_accounts.json") if config else Path("data/webshare_accounts.json")
    key = str(accounts_file.resolve())
    instance = _INSTANCES.get(key)
    if instance is None:
        instance = WebshareSource(accounts_file)
        _INSTANCES[key] = instance
    return instance
