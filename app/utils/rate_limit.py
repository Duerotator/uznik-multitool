from __future__ import annotations

import asyncio
import logging
import random

log = logging.getLogger("rate-limit")


async def human_delay(min_seconds: float, max_seconds: float) -> None:
    if max_seconds <= 0:
        return
    await asyncio.sleep(random.uniform(max(0, min_seconds), max(min_seconds, max_seconds)))


async def mute_and_archive(client, chat: str) -> None:
    try:
        await client.mute_chat(chat, until=2**31 - 1)
        await client.archive_chats([chat])
    except Exception as exc:
        log.debug("mute+archive %s: %s", chat, exc)


class AsyncLimiter:
    def __init__(self, concurrency: int):
        self.semaphore = asyncio.Semaphore(max(1, concurrency))

    async def __aenter__(self):
        await self.semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.semaphore.release()
