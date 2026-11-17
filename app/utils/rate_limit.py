from __future__ import annotations

import asyncio
import logging
import random

log = logging.getLogger("rate-limit")


async def human_delay(
    min_seconds: float,
    max_seconds: float,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    if max_seconds <= 0:
        return
    if stop_event is not None and stop_event.is_set():
        return
    delay = random.uniform(max(0, min_seconds), max(min_seconds, max_seconds))
    if stop_event is None:
        await asyncio.sleep(delay)
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        pass


def _coerce_chat_target(chat: str | int) -> str | int:
    if isinstance(chat, int):
        return chat
    text = str(chat).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return text


async def _mute_chat(target_client, chat: str | int) -> None:
    target = _coerce_chat_target(chat)
    if callable(getattr(target_client, "mute_chat", None)):
        await target_client.mute_chat(target, until=2**31 - 1)
        return
    if callable(getattr(target_client, "invoke", None)) and callable(getattr(target_client, "resolve_peer", None)):
        from pyrogram import raw

        peer = await target_client.resolve_peer(target)
        await target_client.invoke(
            raw.functions.account.UpdateNotifySettings(
                peer=raw.types.InputNotifyPeer(peer=peer),
                settings=raw.types.InputPeerNotifySettings(mute_until=2**31 - 1, silent=True),
            )
        )
        return
    if callable(getattr(target_client, "get_input_entity", None)) and callable(target_client):
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings

        entity = await target_client.get_input_entity(target)
        await target_client(
            UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer=entity),
                settings=InputPeerNotifySettings(mute_until=2**31 - 1, silent=True),
            )
        )


async def _archive_chat(target_client, chat: str | int) -> None:
    target = _coerce_chat_target(chat)
    if callable(getattr(target_client, "archive_chats", None)):
        await target_client.archive_chats([target])
        return
    if callable(getattr(target_client, "edit_folder", None)):
        await target_client.edit_folder(target, 1)


async def mute_and_archive(client, chat: str | int) -> None:
    target_client = getattr(client, "client", None) or client
    try:
        await _mute_chat(target_client, chat)
    except Exception as exc:
        log.debug("mute %s: %s", chat, exc)
    try:
        await _archive_chat(target_client, chat)
    except Exception as exc:
        log.debug("archive %s: %s", chat, exc)


class AsyncLimiter:
    def __init__(self, concurrency: int):
        self.semaphore = asyncio.Semaphore(max(1, concurrency))

    async def __aenter__(self):
        await self.semaphore.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.semaphore.release()
