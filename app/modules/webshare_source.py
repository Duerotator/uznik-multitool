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
        async with httpx.AsyncClient(timeout=15) as client:
            for account in accounts:
                api_key = account.get("api_key", "")
                if not api_key:
                    continue
                try:
                    r = await client.get(
                        f"{BASE}/proxy/list/?mode=direct&page=1&page_size=50",
                        headers={"Authorization": f"Token {api_key}"},
                    )
                    if r.status_code != 200:
                        logger.warning("Webshare API error for %s...: %s", api_key[:10], r.status_code)
                        continue
                    data = r.json()
                    for p in data.get("results", []):
                        if not p.get("valid"):
                            continue
                        proxies.append((
                            p["proxy_address"],
                            p["port"],
                            "http",
                            p["username"],
                            p["password"],
                        ))
                except Exception:
                    logger.exception("Webshare fetch failed for %s...", api_key[:10])

        self._cache = proxies
        logger.info("Webshare source: %d proxies from %d accounts", len(proxies), len(accounts))
        return proxies

    async def refresh(self) -> int:
        """Обновить кэш и вернуть количество."""
        await self.fetch()
        return len(self._cache)

    def get_cached(self) -> list[tuple[str, int, str, str, str]]:
        return self._cache


def get_webshare_source(config: AppConfig | None = None) -> WebshareSource:
    """Singleton для WebshareSource."""
    if not hasattr(get_webshare_source, "_instance"):
        accounts_file = (config.data_dir / "webshare_accounts.json") if config else Path("data/webshare_accounts.json")
        get_webshare_source._instance = WebshareSource(accounts_file)
    return get_webshare_source._instance
