"""Shared, bounded UI catalog reads; no account binding or payment capability."""
import asyncio
import os
import time

from core.secret_vault import VaultError
from modules.marketapp_catalog import CatalogError, MarketappCatalogReader
from modules.marketapp_credentials import validate_token

FILTERS = {"limit", "sort_by", "cursor", "collection_address", "model", "symbol", "backdrop", "include_sellable"}


class MarketappCatalogSession:
    def __init__(self, config=None, *, reader=None, token_source=None, clock=time.time):
        proxy = getattr(config, "global_proxy", None) if config is not None else None
        self.reader = reader if reader is not None else MarketappCatalogReader(proxy=proxy)
        if token_source is None and config is not None:
            from modules.marketapp_credentials import MarketappCredentials
            token_source = MarketappCredentials(config.data_dir).get
        self.token_source = token_source or self._env_token
        self.clock = clock
        self._busy = asyncio.Lock()

    @staticmethod
    def _env_token():
        try:
            return validate_token(os.environ.get("MARKETAPP_API_TOKEN"))
        except VaultError as exc:
            raise CatalogError(str(exc)) from None

    async def fetch(self, payload):
        if not isinstance(payload, dict) or set(payload) - FILTERS - {"confirmed"}:
            raise CatalogError("Unsupported catalog request; no token/account/payment fields accepted")
        if payload.get("confirmed") is not True:
            raise CatalogError("Confirm one provider catalog read first")
        if self._busy.locked():
            raise CatalogError("A catalog request is already running")
        async with self._busy:
            report = await self.reader.read(token=self.token_source(), confirmed=True,
                                            **{key: value for key, value in payload.items() if key in FILTERS})
            return {**report, "checked_at": int(self.clock()), "selection_persisted": False}
