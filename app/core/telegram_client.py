from __future__ import annotations

import asyncio
import json
import os
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.config import AppConfig
from core.diagnostics import get_proxy_diagnostics
from core.logging_setup import account_logger
from core.models import AccountRecord, ProxyConfig
from core.telegram_connection import CLEANUP_TIMEOUT, CONNECT_TIMEOUT, connection_error, is_connection_error
from utils.telegram_errors import flood_wait_seconds, is_invalid_auth_error


_SESSION_LOCKS: dict[str, asyncio.Lock] = {}
_PROXY_POOLS: dict[str, Any] = {}


class AccountBusyError(RuntimeError):
    """Another task owns the session; this is not an authentication failure."""


def session_lock(account: AccountRecord) -> asyncio.Lock:
    key = str(Path(account.session_ref).resolve())
    if key not in _SESSION_LOCKS:
        _SESSION_LOCKS[key] = asyncio.Lock()
    return _SESSION_LOCKS[key]


def resolve_session_path(config: AppConfig, session_ref: str) -> Path:
    """Resolve session refs written on Windows after they have been mirrored to VPS."""
    normalized = str(session_ref).replace("\\", "/")
    foreign_windows_path = (
        os.name != "nt"
        and len(normalized) >= 3
        and normalized[0].isalpha()
        and normalized[1:3] == ":/"
    )
    direct = Path(session_ref)
    if direct.is_file() and not foreign_windows_path:
        return direct
    marker = "/data/sessions/"
    if marker in normalized:
        candidate = config.sessions_dir / normalized.split(marker, 1)[1]
        if candidate.is_file():
            return candidate
    candidates = list(config.sessions_dir.rglob(Path(normalized).name))
    if len(candidates) == 1 and candidates[0].is_file():
        return candidates[0]
    return direct


def _proxy_pool(config: AppConfig):
    """One pool per database file — constructing it runs schema DDL."""
    from modules.proxy_manager import ProxyPool

    key = str(config.proxy_pool_db)
    if key not in _PROXY_POOLS:
        _PROXY_POOLS[key] = ProxyPool(config.proxy_pool_db)
    return _PROXY_POOLS[key]


class AccountClient(ABC):
    def __init__(self, config: AppConfig, account: AccountRecord):
        self.config = config
        self.account = account
        self.log = account_logger(account.id)
        self._lock: asyncio.Lock | None = None
        self._lock_acquired = False
        self.lock_wait_timeout: float | None = None
        # High-volume callers may lower this threshold and rotate accounts.
        self.flood_wait_raise_after = 60
        self.flood_wait_count = 0

    async def __aenter__(self) -> "AccountClient":
        self._lock = session_lock(self.account)
        get_proxy_diagnostics().trace_client_lifecycle(self.account.id, "lock_acquire", "waiting")
        try:
            if self.lock_wait_timeout is None:
                await self._lock.acquire()
            else:
                await asyncio.wait_for(self._lock.acquire(), timeout=self.lock_wait_timeout)
        except TimeoutError:
            get_proxy_diagnostics().trace_client_lifecycle(self.account.id, "lock_busy", "wait expired")
            raise AccountBusyError(
                "Account is busy with another task. Stop Warmup / Online or wait for the current task to finish, then retry."
            ) from None
        self._lock_acquired = True
        get_proxy_diagnostics().trace_client_lifecycle(self.account.id, "lock_acquired", "ok")
        try:
            get_proxy_diagnostics().trace_client_lifecycle(self.account.id, "client_start", f"proxy={self.account.proxy or '(none)'}")
            await self.start()
            get_proxy_diagnostics().trace_client_lifecycle(self.account.id, "client_started", "ok")
        except BaseException as exc:
            get_proxy_diagnostics().trace_client_lifecycle(self.account.id, "client_start_failed", str(exc))
            try:
                await asyncio.wait_for(self.stop(), CLEANUP_TIMEOUT)
            except Exception as stop_exc:  # noqa: BLE001 - cleanup must not hide the start error
                self.log.debug("Could not stop client after failed start: %s", stop_exc)
            finally:
                if self._lock_acquired:
                    self._lock.release()
                    self._lock_acquired = False
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        get_proxy_diagnostics().trace_client_lifecycle(self.account.id, "client_exit", f"exc_type={exc_type.__name__ if exc_type else 'None'}")
        try:
            try:
                await self.set_offline()
            except Exception as offline_exc:  # noqa: BLE001 - cleanup must not hide task errors
                self.log.debug("Could not force offline before stop: %s", offline_exc)
            finally:
                try:
                    await asyncio.wait_for(self.stop(), CLEANUP_TIMEOUT)
                except Exception as stop_exc:
                    get_proxy_diagnostics().trace_client_lifecycle(self.account.id, "client_cleanup_failed", type(stop_exc).__name__)
                    if exc is None:
                        raise
                    self.log.warning("Client cleanup failed after task error (%s)", type(stop_exc).__name__)
                else:
                    get_proxy_diagnostics().trace_client_lifecycle(self.account.id, "client_stopped", "ok")
        finally:
            if self._lock and self._lock_acquired:
                self._lock.release()
                self._lock_acquired = False
                get_proxy_diagnostics().trace_client_lifecycle(self.account.id, "lock_released", "ok")

    @staticmethod
    def _flood_caller() -> str:
        import inspect

        frame = inspect.currentframe()
        if frame is None:
            return "?"
        f = frame.f_back
        while f is not None:
            mod = f.f_globals.get("__name__", "")
            if not mod.startswith("core.telegram_client"):
                return f"{mod}.{f.f_code.co_name}"
            f = f.f_back
        return "?"

    @staticmethod
    def _is_network_error(exc: Exception) -> bool:
        if is_connection_error(exc):
            return True
        msg = str(exc).lower()
        return any(
            kw in msg for kw in (
                "timed out", "timeout", "connection", "network", "proxy",
                "socket", "refused", "reset", "broken pipe", "eof",
                "transport", "handshake", "ssl", "tls", "aborted",
                "peer", "unreachable", "closed",
            )
        )

    async def _replace_proxy(self) -> None:
        from urllib.parse import urlparse

        from core.storage import read_json, write_json_atomic

        pool = _proxy_pool(self.config)
        old_proxy = self.account.proxy

        if self.account.proxy:
            old = urlparse(self.account.proxy)
            old_ip = old.hostname or ""
            old_port = old.port or 0
            old_scheme = old.scheme or "http"
            await pool.add_to_blacklist(old_ip, old_port, old_scheme)
            await pool.remove_dead_from_pool(old_ip, old_port, old_scheme)

        try:
            from modules.async_proxy_manager import get_async_proxy_manager
            mgr = get_async_proxy_manager()
            ap = await mgr.get_proxy_for_account(self.account.id, exclude_url=old_proxy)
            if ap:
                self.account.proxy = ap.url
                get_proxy_diagnostics().trace_replace_proxy(self.account.id, old_proxy, self.account.proxy)
                self.log.info("Switched to proxy %s (%.2fs) from async pool", ap.url, ap.entry.latency)
                return
        except Exception:
            pass

        entry = await pool.acquire(protocol="socks5")
        if entry is None:
            entry = await pool.acquire(protocol=None)
        if entry is None:
            get_proxy_diagnostics().trace_replace_proxy(self.account.id, old_proxy, None)
            self.log.error("No MTProto-verified replacement proxy available.")
            raise connection_error(old_proxy or self.config.global_proxy)
        self.account.proxy = entry.url
        get_proxy_diagnostics().trace_replace_proxy(self.account.id, old_proxy, self.account.proxy)
        self.log.info("Switched to proxy %s (%.2fs)", entry.url, entry.latency)

        raw = read_json(self.config.accounts_file, {"accounts": []})
        for item in raw.get("accounts", []):
            if item.get("id") == self.account.id:
                item["proxy"] = self.account.proxy
                break
        write_json_atomic(self.config.accounts_file, raw)

    async def _proxy_usable(self, proxy: ProxyConfig) -> bool:
        from modules.proxy_manager import validate_proxy

        scheme = proxy.scheme.replace("socks5h", "socks5")
        get_proxy_diagnostics().trace_connect(proxy.hostname, proxy.port, scheme, tag=f"account={self.account.id}")
        auth = (proxy.username, proxy.password or "") if proxy.username else None
        ok, latency = await validate_proxy(
            proxy.hostname, proxy.port, scheme, timeout=8.0, auth=auth
        )
        get_proxy_diagnostics().trace_handshake(proxy.hostname, proxy.port, scheme, ok, latency)
        if not ok:
            self.log.warning("Proxy %s:%s failed Telegram MTProto validation", proxy.hostname, proxy.port)
            return False
        self.log.debug("Proxy %s:%s passed Telegram MTProto validation", proxy.hostname, proxy.port)
        return True

    async def _startup_proxy(self) -> ProxyConfig | None:
        url = self.account.proxy or self.config.global_proxy
        if not url:
            # Preserve automatic use of the UI pool, but allow an empty pool.
            try:
                entry = await asyncio.wait_for(
                    _proxy_pool(self.config).acquire(protocol="socks5"), CONNECT_TIMEOUT
                )
            except TimeoutError:
                raise connection_error(True) from None
            if entry:
                self.account.proxy = url = entry.url
        return ProxyConfig.from_url(url)

    async def _with_retry(self, factory, max_retries: int = 3):
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                return await factory()
            except (ConnectionResetError, OSError) as exc:
                msg = str(exc).lower()
                is_conn_err = any(
                    kw in msg
                    for kw in ("connection reset", "connection refused", "connection aborted", "broken pipe")
                )
                if not is_conn_err:
                    raise
                last_exc = exc
                self.log.warning(
                    "Connection error attempt %d/%d: %s — swapping proxy",
                    attempt + 1, max_retries, exc,
                )
                try:
                    from modules.async_proxy_manager import get_async_proxy_manager
                    mgr = get_async_proxy_manager()
                    await mgr.mark_proxy_failed(self.account.id)
                except Exception:
                    pass
                await self._replace_proxy()
            except Exception as exc:
                msg = str(exc).lower()
                if "connection" in msg and ("reset" in msg or "refused" in msg):
                    last_exc = exc
                    self.log.warning(
                        "Connection error attempt %d/%d: %s — swapping proxy",
                        attempt + 1, max_retries, exc,
                    )
                    try:
                        from modules.async_proxy_manager import get_async_proxy_manager
                        mgr = get_async_proxy_manager()
                        await mgr.mark_proxy_failed(self.account.id)
                    except Exception:
                        pass
                    await self._replace_proxy()
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("All retry attempts exhausted")

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    async def set_offline(self) -> None:
        return None

    @abstractmethod
    async def join_chat(self, target: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def resolve_chat_peer(self, target: str) -> Any:
        raise NotImplementedError

    @abstractmethod
    async def leave_chat(self, target: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def open_link(self, link: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def view_post(self, link: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def random_reaction(self, link: str, *, mark_viewed: bool = True) -> str:
        raise NotImplementedError

    @abstractmethod
    async def available_reactions(self, link: str) -> list["ReactionChoice"]:
        raise NotImplementedError

    @abstractmethod
    async def set_reaction(self, link: str, reaction: "ReactionChoice") -> str:
        raise NotImplementedError

    @abstractmethod
    async def update_profile(
        self,
        first_name: str | None = None,
        last_name: str | None = None,
        bio: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def set_username(self, username: str | None) -> None:
        raise NotImplementedError

    @abstractmethod
    async def set_profile_photo(self, image_path: Path) -> None:
        raise NotImplementedError

    @abstractmethod
    async def clear_profile_photos(self) -> None:
        raise NotImplementedError

    async def terminate_other_sessions(self, desktop_only: bool = False) -> int:
        raise NotImplementedError

    async def delete_passkeys(self) -> int:
        raise NotImplementedError

    async def set_cloud_password(
        self,
        new_password: str,
        hint: str = "",
        current_password: str | None = None,
    ) -> None:
        raise NotImplementedError

    async def remove_cloud_password(self, current_password: str) -> None:
        raise NotImplementedError

    async def set_recovery_email(
        self,
        email: str,
        code_provider,
        new_password: str | None = None,
        hint: str = "",
        current_password: str | None = None,
    ) -> None:
        raise NotImplementedError

    async def set_login_email(self, email: str, code_provider) -> None:
        raise NotImplementedError

    async def get_security_state(self) -> dict[str, Any]:
        return {}

    async def get_recent_service_messages(self, since_ts: int, limit: int = 25) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def get_recent_chat_messages(self, chat: str, limit: int = 10, *, include_media: bool = False) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def send_message(self, chat: str, text: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_me(self) -> dict[str, Any]:
        raise NotImplementedError

    async def get_message_detail(self, link: str) -> dict[str, Any]:
        raise NotImplementedError

    async def request_bot_app_webview(
        self,
        bot: str,
        short_name: str,
        start_param: str,
        *,
        peer: str | None = None,
        platform: str = "android",
    ) -> dict[str, str]:
        raise NotImplementedError

    async def request_callback_answer(
        self,
        chat: str,
        message_id: int,
        data: str,
        *,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def get_user_full(
        self,
        target: str,
        *,
        with_photo_count: bool = True,
        with_stories: bool = False,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def download_profile_photos(self, target: str, limit: int = 15) -> list[bytes]:
        raise NotImplementedError

    async def download_stories(
        self,
        target: str,
        limit: int = 18,
        *,
        max_videos: int = 5,
        max_photos: int = 13,
        video_timeout: float = 60.0,
        photo_timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def upload_story(self, story_info: dict[str, Any], caption: str = "") -> None:
        raise NotImplementedError

    async def get_recent_chat_senders(self, target: str, limit: int = 200) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def check_username_available(self, username: str) -> bool:
        raise NotImplementedError

    async def set_profile_photos(self, paths: list[Path]) -> None:
        raise NotImplementedError

    async def set_birthday(self, day: int, month: int, year: int | None = None) -> None:
        raise NotImplementedError

    async def pin_active_stories(self) -> int:
        """Pin all currently active own stories so Telegram shows them as publications."""
        raise NotImplementedError

    async def clear_active_stories(self) -> int:
        """Delete all currently active own stories and return their count."""
        raise NotImplementedError

    async def supports_stories(self) -> bool:
        raise NotImplementedError

    async def supports_profile_music(self) -> bool:
        raise NotImplementedError

    async def download_profile_music(self, target: str) -> dict[str, Any] | None:
        raise NotImplementedError

    async def set_profile_music(self, music_bytes: bytes, meta: dict[str, Any]) -> None:
        raise NotImplementedError

    async def probe_frozen(self) -> None:
        raise NotImplementedError

    async def keep_online(self, stop_event: asyncio.Event, interval: float = 45.0) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


class PyrogramAccountClient(AccountClient):
    def __init__(self, config: AppConfig, account: AccountRecord, *, receive_updates: bool = False):
        super().__init__(config, account)
        self.receive_updates = receive_updates
        self.client: Any | None = None
        self._connected_via_connect = False

    async def start(self) -> None:
        self.config.require_telegram_api()
        from pyrogram import Client

        from modules.fingerprint_generator import FingerprintGenerator

        MAX_RETRIES = 10
        for attempt in range(MAX_RETRIES + 1):
            proxy = await self._startup_proxy()
            get_proxy_diagnostics().trace_client_lifecycle(
                self.account.id, "proxy_check", f"attempt={attempt + 1}/{MAX_RETRIES + 1} proxy={proxy}"
            )
            if proxy is None:
                self.log.info("No proxy configured; connecting to Telegram directly")
            if proxy and not await self._proxy_usable(proxy):
                if attempt < MAX_RETRIES:
                    get_proxy_diagnostics().trace_client_lifecycle(
                        self.account.id, "proxy_rejected", f"attempt={attempt + 1} swapping"
                    )
                    await self._replace_proxy()
                    continue
                raise RuntimeError(f"No working proxy for {self.account.id}")

            session_name, workdir, session_string = self._session_args()
            fingerprint = FingerprintGenerator().params_for_account(self.account)
            self.client = Client(
                name=session_name,
                api_id=self.config.api_id,
                api_hash=self.config.api_hash,
                workdir=str(workdir),
                session_string=session_string,
                proxy=proxy.to_pyrogram() if proxy else None,
                no_updates=not self.receive_updates,
                device_model=fingerprint["device_model"],
                system_version=fingerprint["system_version"],
                app_version=fingerprint["app_version"],
                lang_code=fingerprint["lang_code"],
            )
            async def connect_existing_session() -> None:
                authorized = await self.client.connect()
                if authorized:
                    return
                raise RuntimeError(
                    "Pyrogram session is not authorized; interactive login is disabled"
                )

            try:
                get_proxy_diagnostics().trace_client_lifecycle(
                    self.account.id, "pyrogram_connecting", f"attempt={attempt + 1}"
                )
                await asyncio.wait_for(self._retry_locked(connect_existing_session), CONNECT_TIMEOUT)
                self._connected_via_connect = True
                self.log.info("Pyrogram client connected")
                return
            except BaseException as exc:
                get_proxy_diagnostics().trace_client_lifecycle(
                    self.account.id, "pyrogram_connect_failed", f"attempt={attempt + 1} error={exc}"
                )
                self.log.warning("Pyrogram start attempt %d failed: %s", attempt + 1, exc)
                try:
                    # start() uses connect(), not initialize()/start().
                    await asyncio.wait_for(self.client.disconnect(), CLEANUP_TIMEOUT)
                except Exception as stop_exc:  # noqa: BLE001 - cleanup must not hide the start error
                    self.log.debug("Could not stop failed Pyrogram client: %s", stop_exc)
                self.client = None
                self._connected_via_connect = False
                if not isinstance(exc, Exception):
                    raise
                if proxy is None and self._is_network_error(exc):
                    raise connection_error(None) from exc
                if attempt < MAX_RETRIES and self._is_network_error(exc):
                    await self._replace_proxy()
                    continue
                raise

    async def stop(self) -> None:
        if self.client is not None:
            if self._connected_via_connect:
                await self.client.disconnect()
            else:
                await self.client.stop()
            self.client = None
            self._connected_via_connect = False
            self.log.info("Pyrogram client stopped")

    async def set_offline(self) -> None:
        if self.client is None:
            return
        from pyrogram.raw.functions.account import UpdateStatus

        await asyncio.wait_for(
            self._retry_locked(lambda: self.client.invoke(UpdateStatus(offline=True))),
            timeout=8,
        )

    async def join_chat(self, target: str) -> None:
        from pyrogram import raw

        invite_hash = telegram_invite_hash(target)
        if invite_hash:
            await self._with_flood_wait(
                lambda: self.client.invoke(raw.functions.messages.ImportChatInvite(hash=invite_hash))
            )
            return
        target = normalize_chat_target(target)
        if str(target).startswith("+"):
            await self._with_flood_wait(lambda: self.client.join_chat(str(target)))
            return
        peer = await self.resolve_chat_peer(target)
        if isinstance(peer, raw.types.InputPeerChat):
            await self._with_flood_wait(
                lambda: self.client.invoke(
                    raw.functions.messages.AddChatUser(
                        chat_id=peer.chat_id, user_id=raw.types.InputUserSelf()
                    )
                )
            )
        else:
            await self._with_flood_wait(
                lambda: self.client.invoke(raw.functions.channels.JoinChannel(channel=peer))
            )

    async def leave_chat(self, target: str) -> None:
        from pyrogram import raw

        target = normalize_chat_target(target)
        peer = await self.resolve_chat_peer(target)
        if isinstance(peer, raw.types.InputPeerChat):
            await self._with_flood_wait(
                lambda: self.client.invoke(
                    raw.functions.messages.DeleteChatUser(
                        chat_id=peer.chat_id, user_id=raw.types.InputUserSelf()
                    )
                )
            )
        else:
            await self._with_flood_wait(
                lambda: self.client.invoke(raw.functions.channels.LeaveChannel(channel=peer))
            )

    async def resolve_chat_peer(self, target: str) -> Any:
        from pyrogram import raw

        resolved = coerce_chat_id(target)
        try:
            return await self.client.resolve_peer(resolved)
        except Exception as exc:
            s = str(target).strip()
            if s.startswith("-100") and s.lstrip("-").isdigit():
                raw_id = int(s[4:])
                if raw_id > 0:
                    self.log.debug(
                        "resolve %s failed (%s) — retrying as basic chat %s", target, exc, raw_id
                    )
                    return raw.types.InputPeerChat(chat_id=raw_id)
            raise

    async def open_link(self, link: str) -> None:
        parsed = parse_telegram_link(link)
        if parsed["kind"] == "post":
            await self.view_post(link)
            return
        if parsed["kind"] == "bot_start":
            payload = parsed.get("payload")
            command = f"/start {payload}" if payload else "/start"
            await self.send_message(str(parsed["target"]), command)
            return
        await self.join_chat(str(parsed["target"]))

    async def view_post(self, link: str) -> None:
        from pyrogram import raw

        target, message_id = parse_post_link(link)
        peer = await self.client.resolve_peer(coerce_chat_id(target))
        await self._with_flood_wait(
            lambda: self.client.invoke(
                raw.functions.messages.GetMessagesViews(
                    peer=peer,
                    id=[message_id],
                    increment=True,
                )
            )
        )

    async def random_reaction(self, link: str, *, mark_viewed: bool = True) -> str:
        target, message_id = parse_post_link(link)
        if mark_viewed:
            await self.view_post(link)
        reactions = await self._allowed_reactions(target)
        if not reactions:
            raise RuntimeError("No reactions are available for this post.")

        candidates = unique_reactions(reactions)[:5]
        random.shuffle(candidates)
        last_error: Exception | None = None
        for reaction in candidates:
            try:
                await self._send_reaction(target, message_id, reaction)
                self.log.info(
                    "Random reaction set for %s/%s: %s (available=%s)",
                    target,
                    message_id,
                    reaction.display,
                    len(reactions),
                )
                return f"reaction={reaction.display}; available={len(reactions)}"
            except Exception as exc:
                last_error = exc
                if flood_wait_seconds(exc) is not None or is_invalid_auth_error(exc):
                    raise
                self.log.warning(
                    "Reaction candidate failed for %s/%s: %s (%s)",
                    target,
                    message_id,
                    reaction.display,
                    exc,
                )
        if last_error:
            raise last_error
        raise RuntimeError("No reaction candidates generated.")

    async def available_reactions(self, link: str) -> list["ReactionChoice"]:
        target, _message_id = parse_post_link(link)
        return await self._allowed_reactions(target)

    async def set_reaction(self, link: str, reaction: "ReactionChoice") -> str:
        target, message_id = parse_post_link(link)
        await self.view_post(link)
        await self._send_reaction(target, message_id, reaction)
        self.log.info("Reaction set for %s/%s: %s", target, message_id, reaction.display)
        return f"reaction={reaction.display}"

    async def _send_reaction(self, target: str, message_id: int, reaction: "ReactionChoice") -> None:
        from pyrogram import raw

        peer = await self.client.resolve_peer(coerce_chat_id(target))
        await self._with_flood_wait(
            lambda: self.client.invoke(
                raw.functions.messages.SendReaction(
                    peer=peer,
                    msg_id=message_id,
                    reaction=[reaction.to_pyrogram_raw(raw)],
                )
            )
        )

    async def _allowed_reactions(self, target: str) -> list["ReactionChoice"]:
        chat = await self.client.get_chat(coerce_chat_id(target))
        available = getattr(chat, "available_reactions", None)
        reactions = reactions_from_pyrogram_chat_reactions(available)
        if getattr(available, "all_are_enabled", False) or not reactions:
            reactions = reactions or await self._global_reactions()
        reactions = unique_reactions(reactions or default_reaction_choices())
        return await self._with_custom_reaction_labels(reactions)

    async def _global_reactions(self) -> list["ReactionChoice"]:
        from pyrogram import raw

        result = await self._with_flood_wait(
            lambda: self.client.invoke(raw.functions.messages.GetAvailableReactions(hash=0))
        )
        return [
            ReactionChoice.emoji(
                str(getattr(reaction, "reaction", "") or ""),
                str(getattr(reaction, "title", "") or ""),
            )
            for reaction in getattr(result, "reactions", []) or []
            if getattr(reaction, "reaction", None)
            and not getattr(reaction, "inactive", False)
            and not getattr(reaction, "premium", False)
        ]

    async def _with_custom_reaction_labels(self, reactions: list["ReactionChoice"]) -> list["ReactionChoice"]:
        from pyrogram import raw

        custom_ids = [reaction.custom_emoji_id for reaction in reactions if reaction.custom_emoji_id]
        if not custom_ids:
            return reactions
        try:
            docs = await self._with_flood_wait(
                lambda: self.client.invoke(
                    raw.functions.messages.GetCustomEmojiDocuments(document_id=custom_ids)
                )
            )
        except Exception as exc:
            self.log.debug("Could not load custom reaction labels: %s", exc)
            return reactions
        labels = custom_emoji_labels_from_documents(docs)
        for reaction in reactions:
            if reaction.custom_emoji_id and reaction.custom_emoji_id in labels:
                reaction.title = labels[reaction.custom_emoji_id]
        return reactions

    async def update_profile(
        self,
        first_name: str | None = None,
        last_name: str | None = None,
        bio: str | None = None,
    ) -> None:
        kwargs = {
            key: value
            for key, value in {
                "first_name": first_name,
                "last_name": last_name,
                "bio": bio,
            }.items()
            if value is not None
        }
        if kwargs:
            await self._with_flood_wait(lambda: self.client.update_profile(**kwargs))

    async def set_username(self, username: str | None) -> None:
        await self._with_flood_wait(lambda: self.client.set_username(username or ""))

    async def set_profile_photo(self, image_path: Path) -> None:
        await self._upload_profile_photo_raw(image_path)

    async def clear_profile_photos(self) -> None:
        photos: list[str] = []
        async for photo in self.client.get_chat_photos("me"):
            photos.append(photo.file_id)
        for index in range(0, len(photos), 100):
            chunk = photos[index:index + 100]
            try:
                await self._with_flood_wait(lambda chunk=chunk: self.client.delete_profile_photos(chunk))
            except Exception as exc:
                self.log.warning("Could not delete profile photo chunk for %s: %s", self.account.id, exc)

    async def _upload_profile_photo_raw(self, image_path: Path) -> None:
        from pyrogram import raw

        file_id = random.randint(1, 2 ** 63 - 1)
        data = image_path.read_bytes()
        chunk_size = 512 * 1024
        parts = 0
        for i in range(0, len(data), chunk_size):
            await self.client.invoke(
                raw.functions.upload.SaveFilePart(
                    file_id=file_id,
                    file_part=parts,
                    bytes=data[i:i + chunk_size],
                )
            )
            parts += 1
        input_file = raw.types.InputFile(
            id=file_id, parts=parts, name=image_path.name, md5_checksum="",
        )
        is_video = data[:4] in (b"\x00\x00\x00\x1cftyp",) or data[:3] == b"\x01\x00\x00" or data[:4] == b"RIFF"
        kwargs = {}
        if is_video:
            kwargs["video"] = input_file
            kwargs["video_start_ts"] = 0.0
        else:
            kwargs["file"] = input_file
        await self._with_flood_wait(
            lambda: self.client.invoke(raw.functions.photos.UploadProfilePhoto(**kwargs))
        )

    async def set_profile_photos(self, paths: list[Path]) -> None:
        for path in paths:
            try:
                await self._upload_profile_photo_raw(path)
                await asyncio.sleep(0.5)
            except Exception as exc:
                msg = str(exc)
                if "FROZEN" in msg.upper():
                    raise
                self.log.warning("set_profile_photos %s: %s", self.account.id, exc)

    async def terminate_other_sessions(self, desktop_only: bool = False) -> int:
        from pyrogram.raw.functions.account import GetAuthorizations, ResetAuthorization

        result = await self._with_flood_wait(lambda: self.client.invoke(GetAuthorizations()))
        removed = 0
        for authorization in getattr(result, "authorizations", []):
            if getattr(authorization, "current", False):
                continue
            if desktop_only and not is_desktop_authorization(authorization):
                continue
            await self._with_flood_wait(
                lambda authorization=authorization: self.client.invoke(
                    ResetAuthorization(hash=authorization.hash)
                )
            )
            removed += 1
        return removed

    async def delete_passkeys(self) -> int:
        _register_pyrogram_passkey_raw_types()
        passkeys_result = await self._with_flood_wait(
            lambda: self.client.invoke(PyrogramGetPasskeys())
        )
        removed = 0
        for passkey in getattr(passkeys_result, "passkeys", []) or []:
            passkey_id = getattr(passkey, "id", "")
            if not passkey_id:
                continue
            await self._with_flood_wait(
                lambda passkey_id=passkey_id: self.client.invoke(
                    PyrogramDeletePasskey(id=passkey_id)
                )
            )
            removed += 1
        return removed

    async def set_cloud_password(
        self,
        new_password: str,
        hint: str = "",
        current_password: str | None = None,
    ) -> None:
        from pyrogram import raw
        
        password_info = await self.client.invoke(raw.functions.account.GetPassword())
        has_password = getattr(password_info, "has_password", False)
        
        if has_password:
            if not current_password:
                raise RuntimeError("Current cloud password is required to change it.")
            await self._with_flood_wait(
                lambda: self.client.change_cloud_password(
                    current_password=current_password,
                    new_password=new_password,
                    new_hint=hint,
                )
            )
            return
        
        await self._with_flood_wait(
            lambda: self.client.enable_cloud_password(
                password=new_password,
                hint=hint,
            )
        )

    async def remove_cloud_password(self, current_password: str) -> None:
        await self._with_flood_wait(lambda: self.client.remove_cloud_password(current_password))

    async def set_recovery_email(
        self,
        email: str,
        code_provider,
        new_password: str | None = None,
        hint: str = "",
        current_password: str | None = None,
    ) -> None:
        from pyrogram import raw
        from pyrogram.errors import EmailUnconfirmed
        from pyrogram.utils import btoi, compute_password_check, compute_password_hash, itob

        password_info = await self.client.invoke(raw.functions.account.GetPassword())

        async def update_settings() -> None:
            if getattr(password_info, "has_password", False):
                if not current_password:
                    raise RuntimeError("Current cloud password is required to change recovery email.")
                settings_kwargs: dict[str, Any] = {"email": email}
                if hint:
                    settings_kwargs["hint"] = hint
                if new_password:
                    password_info.new_algo.salt1 += os.urandom(32)
                    new_hash = btoi(compute_password_hash(password_info.new_algo, new_password))
                    settings_kwargs["new_algo"] = password_info.new_algo
                    settings_kwargs["new_password_hash"] = itob(
                        pow(password_info.new_algo.g, new_hash, btoi(password_info.new_algo.p))
                    )
                await self.client.invoke(
                    raw.functions.account.UpdatePasswordSettings(
                        password=compute_password_check(password_info, current_password),
                        new_settings=raw.types.account.PasswordInputSettings(**settings_kwargs),
                    )
                )
                return

            if not new_password:
                raise RuntimeError("New cloud password is required when account has no cloud password.")
            password_info.new_algo.salt1 += os.urandom(32)
            new_hash = btoi(compute_password_hash(password_info.new_algo, new_password))
            await self.client.invoke(
                raw.functions.account.UpdatePasswordSettings(
                    password=raw.types.InputCheckPasswordEmpty(),
                    new_settings=raw.types.account.PasswordInputSettings(
                        new_algo=password_info.new_algo,
                        new_password_hash=itob(
                            pow(password_info.new_algo.g, new_hash, btoi(password_info.new_algo.p))
                        ),
                        hint=hint,
                        email=email,
                    ),
                )
            )

        try:
            await self._with_flood_wait(update_settings)
        except EmailUnconfirmed as exc:
            code = await code_provider(email, getattr(exc, "value", None))
            await self._with_flood_wait(
                lambda: self.client.invoke(raw.functions.account.ConfirmPasswordEmail(code=code))
            )
        password_for_check = new_password or current_password
        if password_for_check:
            fresh_password_info = await self.client.invoke(raw.functions.account.GetPassword())
            settings = await self._with_flood_wait(
                lambda: self.client.invoke(
                    raw.functions.account.GetPasswordSettings(
                        password=compute_password_check(fresh_password_info, password_for_check)
                    )
                )
            )
            if not getattr(settings, "email", None):
                raise RuntimeError("Recovery email was not visible in Telegram password settings after confirmation.")

    async def set_login_email(self, email: str, code_provider) -> None:
        from pyrogram import raw

        purpose = raw.types.EmailVerifyPurposeLoginChange()
        sent = await self._with_flood_wait(
            lambda: self.client.invoke(
                raw.functions.account.SendVerifyEmailCode(purpose=purpose, email=email)
            )
        )
        length = getattr(sent, "length", None)
        code = await code_provider(email, length)
        await self._with_flood_wait(
            lambda: self.client.invoke(
                raw.functions.account.VerifyEmail(
                    purpose=purpose,
                    verification=raw.types.EmailVerificationCode(code=code),
                )
            )
        )

    async def get_security_state(self) -> dict[str, Any]:
        from pyrogram import raw

        password_info = await self._with_flood_wait(
            lambda: self.client.invoke(raw.functions.account.GetPassword())
        )
        return {
            "cloud_password": bool(getattr(password_info, "has_password", False)),
            "recovery_email_hint": getattr(password_info, "email_unconfirmed_pattern", None),
        }

    async def get_recent_service_messages(self, since_ts: int, limit: int = 25) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        async for message in self.client.get_chat_history(777000, limit=limit):
            created_at = _datetime_timestamp(getattr(message, "date", None))
            if created_at and created_at < since_ts:
                break
            text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
            if text:
                messages.append({"received_at": created_at, "text": text})
        return messages

    async def get_recent_chat_messages(self, chat: str, limit: int = 10, *, include_media: bool = False) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        async for message in self.client.get_chat_history(chat, limit=limit):
            created_at = _datetime_timestamp(getattr(message, "date", None))
            text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
            if text or include_media and getattr(message, "media", None):
                messages.append(
                    {
                        "id": getattr(message, "id", 0),
                        "received_at": created_at,
                        "text": text,
                        "outgoing": bool(getattr(message, "outgoing", False)),
                    }
                )
        return messages

    async def send_message(self, chat: str, text: str) -> None:
        await self._with_flood_wait(lambda: self.client.send_message(chat, text))

    async def get_me(self) -> dict[str, Any]:
        user = await self.client.get_me()
        return {
            "user_id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "phone": getattr(user, "phone_number", None),
            "language_code": getattr(user, "language_code", None) or getattr(user, "lang_code", None),
        }

    async def get_message_detail(self, link: str) -> dict[str, Any]:
        target, message_id = parse_post_link(link)
        message = await self._with_flood_wait(
            lambda: self.client.get_messages(coerce_chat_id(target), message_id)
        )
        if message is None or getattr(message, "empty", False):
            raise RuntimeError(f"Message not found: {link}")
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        buttons: list[dict[str, Any]] = []
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "inline_keyboard", None) or []
        for row in rows:
            for button in row:
                web_app = getattr(button, "web_app", None)
                buttons.append(
                    {
                        "text": getattr(button, "text", None),
                        "url": getattr(button, "url", None),
                        "callback_data": getattr(button, "callback_data", None),
                        "web_app_url": getattr(web_app, "url", None) if web_app else None,
                    }
                )
        chat = getattr(message, "chat", None)
        forward_origin = getattr(message, "forward_origin", None)
        forward_chat = getattr(forward_origin, "chat", None)
        forward_message_id = getattr(forward_origin, "message_id", None)
        forward_username = str(getattr(forward_chat, "username", "") or "").lstrip("@")
        forward_post_link = (
            f"https://t.me/{forward_username}/{int(forward_message_id)}"
            if forward_username and forward_message_id
            else None
        )
        entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []
        hidden_links: list[str] = []
        for entity in entities:
            url = getattr(entity, "url", None)
            if url and "t.me/" in str(url):
                hidden_links.append(str(url))
        return {
            "chat": target,
            "chat_id": getattr(chat, "id", None),
            "chat_username": getattr(chat, "username", None),
            "message_id": int(getattr(message, "id", message_id) or message_id),
            "text": text,
            "buttons": buttons,
            "hidden_links": list(dict.fromkeys(hidden_links)),
        }

    async def request_bot_app_webview(
        self,
        bot: str,
        short_name: str,
        start_param: str,
        *,
        peer: str | None = None,
        platform: str = "android",
    ) -> dict[str, str]:
        from pyrogram import raw

        bot_peer = await self.client.resolve_peer(bot)
        peer_ref = await self.client.resolve_peer(peer or bot)
        result = await self._with_flood_wait(
            lambda: self.client.invoke(
                raw.functions.messages.RequestAppWebView(
                    peer=peer_ref,
                    app=raw.types.InputBotAppShortName(bot_id=bot_peer, short_name=short_name),
                    platform=platform,
                    write_allowed=True,
                    start_param=start_param or "",
                )
            )
        )
        url = str(getattr(result, "url", "") or "")
        if not url:
            raise RuntimeError("RequestAppWebView returned empty url")
        return {"url": url, **parse_webapp_url(url)}

    async def request_callback_answer(
        self,
        chat: str,
        message_id: int,
        data: str,
        *,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        from pyrogram import raw

        peer = await self.client.resolve_peer(coerce_chat_id(chat))
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        result = await asyncio.wait_for(
            self._with_flood_wait(
                lambda: self.client.invoke(
                    raw.functions.messages.GetBotCallbackAnswer(
                        peer=peer,
                        msg_id=int(message_id),
                        data=payload,
                    )
                )
            ),
            timeout=timeout,
        )
        return {
            "message": getattr(result, "message", None),
            "url": getattr(result, "url", None),
            "alert": bool(getattr(result, "alert", False)),
            "has_url": bool(getattr(result, "has_url", False)),
        }

    async def get_user_full(
        self,
        target: str,
        *,
        with_photo_count: bool = True,
        with_stories: bool = False,
    ) -> dict[str, Any]:
        from pyrogram import raw

        resolved = coerce_chat_id(target) if isinstance(target, str) and target.lstrip("-").isdigit() else target
        user = await self._with_flood_wait(lambda: self.client.get_users(resolved))
        peer = await self.client.resolve_peer(user.id)
        full = await self._with_flood_wait(
            lambda: self.client.invoke(raw.functions.users.GetFullUser(id=peer))
        )
        fu = full.full_user
        bio = getattr(fu, "about", None) or ""
        has_photo = bool(getattr(user, "photo", None) or getattr(fu, "profile_photo", None))
        photos = 0
        if with_photo_count:
            photos = await self._get_user_photos_count(user.id)
            if photos == 0 and has_photo:
                photos = 1
        elif has_photo:
            photos = 1
        stories = await self._get_stories_count(user.id) if with_stories else 0
        bday = None
        raw_bday = getattr(fu, "birthday", None)
        if raw_bday is not None:
            bday = {
                "day": getattr(raw_bday, "day", 0),
                "month": getattr(raw_bday, "month", 0),
                "year": getattr(raw_bday, "year", None),
            }
        music = None
        saved = getattr(fu, "saved_music", None)
        if saved is not None and hasattr(saved, "__iter__"):
            docs = list(saved)
            if docs:
                d = docs[0]
                music = {
                    "id": getattr(d, "id", 0),
                    "access_hash": getattr(d, "access_hash", 0),
                    "file_reference": getattr(d, "file_reference", b""),
                }
        return {
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "bio": bio,
            "photo_count": photos,
            "stories_count": stories,
            "is_premium": getattr(user, "is_premium", False),
            "birthday": bday,
            "music": music,
        }

    async def download_profile_photos(self, target: str, limit: int = 15) -> list[bytes]:
        result: list[bytes] = []
        count = 0
        try:
            async for photo in self.client.get_chat_photos(coerce_chat_id(target)):
                if count >= limit:
                    break
                try:
                    buf = await asyncio.wait_for(
                        self.client.download_media(photo, in_memory=True),
                        timeout=15,
                    )
                    data = buf.getvalue() if hasattr(buf, "getvalue") else buf
                    if data:
                        result.append(data if isinstance(data, bytes) else bytes(data))
                        count += 1
                except Exception:
                    continue
        except Exception as exc:
            self.log.warning("download_profile_photos %s: %s", target, exc)
        return result

    async def get_channel_members(self, target: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        from pyrogram import raw

        target = normalize_chat_target(target)
        peer = await self.resolve_chat_peer(target)
        if isinstance(peer, raw.types.InputPeerChat):
            return await self._get_basic_chat_participants(peer, limit)
        result: list[dict[str, Any]] = []
        try:
            r = await self._with_flood_wait(
                lambda: self.client.invoke(
                    raw.functions.channels.GetParticipants(
                        channel=peer,
                        filter=raw.types.ChannelParticipantsRecent(),
                        offset=offset,
                        limit=min(200, limit),
                        hash=0,
                    )
                )
            )
        except Exception as exc:
            if "CHANNEL_PRIVATE" in str(exc).upper() or "CHAT_ADMIN" in str(exc).upper():
                return []
            raise
        for user in r.users:
            result.append({
                "user_id": user.id,
                "username": getattr(user, "username", None) or "",
                "first_name": getattr(user, "first_name", None) or "",
                "last_name": getattr(user, "last_name", None) or "",
                "is_premium": getattr(user, "is_premium", False),
            })
        return result[:limit]

    async def _get_basic_chat_participants(self, peer: Any, limit: int) -> list[dict[str, Any]]:
        from pyrogram import raw

        try:
            r = await self._with_flood_wait(
                lambda: self.client.invoke(
                    raw.functions.messages.GetFullChat(chat_id=peer.chat_id)
                )
            )
        except Exception as exc:
            if "CHAT_ADMIN" in str(exc).upper() or "CHAT_NOT_EXIST" in str(exc).upper():
                return []
            raise
        users = {u.id: u for u in getattr(r, "users", []) or []}
        participants = getattr(getattr(r, "full_chat", None), "participants", None)
        if not isinstance(participants, list):
            return []
        result: list[dict[str, Any]] = []
        for cp in participants:
            uid = getattr(cp, "user_id", 0)
            u = users.get(uid)
            if u is None:
                continue
            result.append({
                "user_id": uid,
                "username": getattr(u, "username", None) or "",
                "first_name": getattr(u, "first_name", None) or "",
                "last_name": getattr(u, "last_name", None) or "",
                "is_premium": getattr(u, "is_premium", False),
            })
        return result[:limit]

    async def get_recent_chat_senders(self, target: str, limit: int = 200) -> list[dict[str, Any]]:
        from pyrogram import raw

        target = normalize_chat_target(target)
        peer = await self.resolve_chat_peer(target)
        seen: set[int] = set()
        senders: list[dict[str, Any]] = []
        try:
            r = await self._with_flood_wait(
                lambda: self.client.invoke(
                    raw.functions.messages.GetHistory(
                        peer=peer,
                        offset_id=0,
                        offset_date=0,
                        add_offset=0,
                        limit=min(200, limit),
                        max_id=0,
                        min_id=0,
                        hash=0,
                    )
                )
            )
            if hasattr(r, "users"):
                for u in r.users:
                    uid = getattr(u, "id", 0)
                    uname = (getattr(u, "username", None) or "").strip()
                    if not uname or uid in seen:
                        continue
                    seen.add(uid)
                    senders.append({
                        "user_id": uid,
                        "username": uname,
                        "first_name": getattr(u, "first_name", None) or "",
                        "last_name": getattr(u, "last_name", None) or "",
                        "is_premium": getattr(u, "is_premium", False),
                    })
        except Exception as exc:
            self.log.warning("get_recent_chat_senders %s: %s", target, exc)
        return senders

    async def download_stories(
        self,
        target: str,
        limit: int = 18,
        *,
        max_videos: int = 5,
        max_photos: int = 13,
        video_timeout: float = 60.0,
        photo_timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        from pyrogram import raw, types
        from pyrogram.raw.types import MessageMediaDocument, MessageMediaPhoto

        target = normalize_chat_target(target)
        peer = await self.client.resolve_peer(coerce_chat_id(target))
        try:
            r = await self._with_flood_wait(
                lambda: self.client.invoke(raw.functions.stories.GetPeerStories(peer=peer))
            )
        except Exception as exc:
            self.log.debug("download_stories failed for %s: %s", target, exc)
            return []
        stories_raw = getattr(r, "stories", None)
        if stories_raw is None:
            return []
        try:
            stories_list = list(stories_raw)
        except TypeError:
            stories_list = list(getattr(stories_raw, "stories", []))
        users = {user.id: user for user in getattr(r, "users", [])}
        chats = {chat.id: chat for chat in getattr(r, "chats", [])}
        result: list[dict[str, Any]] = []
        videos = 0
        photos = 0
        for story in stories_list:
            if len(result) >= limit or (videos >= max_videos and photos >= max_photos):
                break
            media = getattr(story, "media", None)
            document = getattr(media, "document", None)
            mime = str(getattr(document, "mime_type", "") or "")
            is_video = (
                isinstance(media, MessageMediaDocument)
                and (bool(getattr(media, "video", False)) or mime.startswith("video/"))
            ) or bool(isinstance(media, MessageMediaPhoto) and getattr(media, "video", False))
            if is_video and videos >= max_videos:
                continue
            if not is_video and photos >= max_photos:
                continue
            try:
                parsed_story = await types.Story._parse(self.client, story, peer, users, chats)
                downloadable = parsed_story.video if is_video else parsed_story.photo
                if downloadable is None:
                    continue
                data = await asyncio.wait_for(
                    self.client.download_media(downloadable, in_memory=True),
                    timeout=video_timeout if is_video else photo_timeout,
                )
                data = data.getvalue() if hasattr(data, "getvalue") else data
                if not data:
                    continue
                data = data if isinstance(data, bytes) else bytes(data)
                if is_video:
                    videos += 1
                else:
                    photos += 1
                result.append({
                    "bytes": data,
                    "is_video": is_video,
                    "mime_type": mime if is_video and mime else ("video/mp4" if is_video else "image/jpeg"),
                })
            except Exception as exc:
                self.log.debug("story media download failed for %s: %s", target, exc)
                continue
        return result

    async def check_username_available(self, username: str) -> bool:
        from pyrogram import raw

        try:
            r = await self._with_flood_wait(
                lambda: self.client.invoke(
                    raw.functions.account.CheckUsername(username=username.strip().lstrip("@"))
                )
            )
            return bool(r) if not isinstance(r, bool) else r
        except Exception:
            return False

    async def supports_stories(self) -> bool:
        try:
            from pyrogram import raw
            return hasattr(raw.functions, "stories")
        except Exception:
            return False

    async def supports_profile_music(self) -> bool:
        try:
            from pyrogram import raw
            return hasattr(raw.functions.account, "SaveMusic")
        except Exception:
            return False

    async def download_profile_music(self, target: str) -> dict[str, Any] | None:
        # Extracted from UserFull.saved_music in get_user_full — return from cache
        return None

    async def set_profile_music(self, music_meta: dict[str, Any]) -> None:
        from pyrogram import raw

        doc_id = int(music_meta.get("id") or 0)
        if not doc_id:
            return
        fr = music_meta.get("file_reference", b"")
        input_doc = raw.types.InputDocument(
            id=doc_id,
            access_hash=int(music_meta.get("access_hash") or 0),
            file_reference=fr if isinstance(fr, bytes) else bytes(fr) if fr else b"",
        )
        await self._with_flood_wait(
            lambda: self.client.invoke(
                raw.functions.account.SaveMusic(id=input_doc, unsave=False)
            )
        )

    async def set_birthday(self, day: int, month: int, year: int | None = None) -> None:
        from pyrogram import raw
        bday = raw.types.Birthday(day=day, month=month, year=year)
        await self._with_flood_wait(
            lambda: self.client.invoke(raw.functions.account.UpdateBirthday(birthday=bday))
        )
        await self._with_flood_wait(
            lambda: self.client.invoke(
                raw.functions.account.SetPrivacy(
                    key=raw.types.InputPrivacyKeyBirthday(),
                    rules=[raw.types.InputPrivacyValueAllowAll()],
                )
            )
        )

    async def upload_story(self, story_info: dict[str, Any], caption: str = "") -> None:
        from pyrogram import raw

        media_bytes = story_info.get("bytes", b"")
        is_video = story_info.get("is_video", False)
        mime = story_info.get("mime_type", "image/jpeg")
        if not media_bytes:
            return
        file_id = random.randint(1, 2 ** 63 - 1)
        chunk_size = 512 * 1024
        parts = 0
        for i in range(0, len(media_bytes), chunk_size):
            await self.client.invoke(
                raw.functions.upload.SaveFilePart(
                    file_id=file_id, file_part=parts,
                    bytes=media_bytes[i:i + chunk_size],
                )
            )
            parts += 1
        ext = ".mp4" if is_video else ".jpg"
        input_file = raw.types.InputFile(
            id=file_id, parts=parts, name=f"story{ext}", md5_checksum="",
        )
        if is_video:
            input_media = raw.types.InputMediaUploadedDocument(
                file=input_file,
                mime_type=mime,
                attributes=[raw.types.DocumentAttributeVideo(
                    duration=0, w=0, h=0,
                    supports_streaming=True,
                )],
            )
        else:
            input_media = raw.types.InputMediaUploadedPhoto(file=input_file)
        peer = await self.client.resolve_peer("me")
        await self._with_flood_wait(
            lambda: self.client.invoke(
                raw.functions.stories.SendStory(
                    peer=peer,
                    media=input_media,
                    privacy_rules=[raw.types.InputPrivacyValueAllowAll()],
                    random_id=random.randint(1, 2 ** 63 - 1),
                    pinned=True,
                    caption=caption,
                )
            )
        )

    async def pin_active_stories(self) -> int:
        """Pin active own stories, including ones uploaded before pinned=True was used."""
        from pyrogram import raw

        peer = await self.client.resolve_peer("me")
        response = await self._with_flood_wait(
            lambda: self.client.invoke(raw.functions.stories.GetPeerStories(peer=peer))
        )
        raw_stories = getattr(response, "stories", None)
        try:
            stories = list(raw_stories or [])
        except TypeError:
            stories = list(getattr(raw_stories, "stories", []) or [])
        story_ids = [int(story.id) for story in stories if getattr(story, "id", None)]
        if not story_ids:
            return 0
        await self._with_flood_wait(
            lambda: self.client.invoke(
                raw.functions.stories.TogglePinned(peer=peer, id=story_ids, pinned=True)
            )
        )
        return len(story_ids)

    async def clear_active_stories(self) -> int:
        from pyrogram import raw

        peer = await self.client.resolve_peer("me")
        response = await self._with_flood_wait(
            lambda: self.client.invoke(raw.functions.stories.GetPeerStories(peer=peer))
        )
        raw_stories = getattr(response, "stories", None)
        try:
            stories = list(raw_stories or [])
        except TypeError:
            stories = list(getattr(raw_stories, "stories", []) or [])
        story_ids = [int(story.id) for story in stories if getattr(story, "id", None)]
        if not story_ids:
            return 0
        await self._with_flood_wait(
            lambda: self.client.invoke(raw.functions.stories.DeleteStories(peer=peer, id=story_ids))
        )
        return len(story_ids)

    async def set_profile_photos(self, paths: list[Path]) -> None:
        for path in paths:
            try:
                await self._upload_profile_photo_raw(path)
                await asyncio.sleep(0.5)
            except Exception as exc:
                msg = str(exc)
                if "FROZEN" in msg.upper():
                    raise
                self.log.warning("set_profile_photos %s: %s", self.account.id, exc)
            await asyncio.sleep(0.5)

    async def _get_user_photos_count(self, user_id: int) -> int:
        try:
            count = await self._with_flood_wait(
                lambda: self.client.get_chat_photos_count(user_id)
            )
            return int(count or 0)
        except Exception:
            return 0

    async def _get_stories_count(self, user_id: int) -> int:
        try:
            from pyrogram import raw
            peer = await self.client.resolve_peer(user_id)
            r = await self._with_flood_wait(
                lambda: self.client.invoke(raw.functions.stories.GetPeerStories(peer=peer))
            )
            stories_raw = getattr(r, "stories", None)
            if stories_raw is None:
                return 0
            try:
                stories_list = list(stories_raw)
            except TypeError:
                stories_list = list(getattr(stories_raw, "stories", []))
            count = len(stories_list)
            if count > 0:
                self.log.debug("stories count %d for user %d", count, user_id)
            return count
        except Exception as exc:
            self.log.debug("_get_stories_count user %d: %s", user_id, exc)
            return 0

    async def probe_frozen(self) -> None:
        from pyrogram.raw.functions.account import UpdateStatus
        await self._with_flood_wait(lambda: self.client.invoke(UpdateStatus(offline=False)))

    async def keep_online(self, stop_event: asyncio.Event, interval: float = 45.0) -> None:
        from pyrogram.raw.functions.account import UpdateStatus

        fails = 0
        while not stop_event.is_set():
            try:
                await self._with_flood_wait(lambda: self.client.invoke(UpdateStatus(offline=False)))
                fails = 0
            except Exception as exc:
                fails += 1
                msg = str(exc).lower()
                if fails >= 3 and ("timed out" in msg or "timeout" in msg):
                    self.log.warning("keep_online timeout ×%d, replacing proxy", fails)
                    try:
                        await self._replace_proxy()
                        fails = 0
                    except Exception:
                        pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def _session_args(self) -> tuple[str, Path, str | None]:
        path = resolve_session_path(self.config, self.account.session_ref)
        if self.account.session_kind == "session_string":
            payload = json.loads(path.read_text(encoding="utf-8"))
            session_string = (
                payload.get("session_string")
                or payload.get("pyrogram_session")
                or payload.get("string_session")
            )
            return (self.account.id, self.config.sessions_dir / "runtime", session_string)
        return (path.stem, path.parent, None)

    async def _with_flood_wait(self, factory):
        from pyrogram.errors import FloodWait

        try:
            return await self._with_retry(lambda: self._retry_locked(factory))
        except FloodWait as exc:
            wait_for = int(getattr(exc, "value", 0)) + 2
            self.flood_wait_count += 1
            if wait_for > self.flood_wait_raise_after:
                self.log.warning(
                    "FloodWait %s seconds — too long, raising for account swap (caller: %s)",
                    wait_for, self._flood_caller(),
                )
                raise
            self.log.warning("FloodWait for %s seconds", wait_for)
            await asyncio.sleep(wait_for)
            return await self._with_retry(lambda: self._retry_locked(factory))

    async def _retry_locked(self, factory):
        last_exc = None
        for attempt in range(3):
            try:
                return await factory()
            except Exception as exc:
                if "database is locked" not in str(exc).lower():
                    raise
                last_exc = exc
                await asyncio.sleep(1.5 * (attempt + 1))
        raise last_exc


class TelethonAccountClient(AccountClient):
    def __init__(self, config: AppConfig, account: AccountRecord, *, receive_updates: bool = False):
        super().__init__(config, account)
        self.receive_updates = receive_updates
        self.client: Any | None = None

    async def start(self) -> None:
        self.config.require_telegram_api()
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        from modules.fingerprint_generator import FingerprintGenerator

        MAX_RETRIES = 10
        for attempt in range(MAX_RETRIES + 1):
            proxy = await self._startup_proxy()
            get_proxy_diagnostics().trace_client_lifecycle(
                self.account.id, "proxy_check", f"attempt={attempt + 1}/{MAX_RETRIES + 1} proxy={proxy} backend=telethon"
            )
            if proxy is None:
                self.log.info("No proxy configured; connecting to Telegram directly")
            if proxy and not await self._proxy_usable(proxy):
                if attempt < MAX_RETRIES:
                    get_proxy_diagnostics().trace_client_lifecycle(
                        self.account.id, "proxy_rejected", f"attempt={attempt + 1} swapping backend=telethon"
                    )
                    await self._replace_proxy()
                    continue
                raise RuntimeError(f"No working proxy for {self.account.id}")

            session = self._session_ref(StringSession)
            fingerprint = FingerprintGenerator().params_for_account(self.account)
            self.client = TelegramClient(
                session,
                self.config.api_id,
                self.config.api_hash,
                proxy=proxy.to_telethon() if proxy else None,
                device_model=fingerprint["device_model"],
                system_version=fingerprint["system_version"],
                app_version=fingerprint["app_version"],
                lang_code=fingerprint["lang_code"],
            )
            try:
                get_proxy_diagnostics().trace_client_lifecycle(
                    self.account.id, "telethon_connecting", f"attempt={attempt + 1}"
                )
                async def connect_existing_session() -> None:
                    await self.client.connect()
                    if not await self.client.is_user_authorized():
                        raise RuntimeError(f"Telethon session is not authorized: {self.account.id}")
                await asyncio.wait_for(connect_existing_session(), CONNECT_TIMEOUT)
                self.log.info("Telethon client started")
                return
            except BaseException as exc:
                get_proxy_diagnostics().trace_client_lifecycle(
                    self.account.id, "telethon_connect_failed", f"attempt={attempt + 1} error={exc}"
                )
                self.log.warning("Telethon start attempt %d failed: %s", attempt + 1, exc)
                try:
                    await asyncio.wait_for(self.client.disconnect(), CLEANUP_TIMEOUT)
                except Exception as disconnect_exc:  # noqa: BLE001 - cleanup must not hide the start error
                    self.log.debug("Could not disconnect failed Telethon client: %s", disconnect_exc)
                self.client = None
                if not isinstance(exc, Exception):
                    raise
                if proxy is None and self._is_network_error(exc):
                    raise connection_error(None) from exc
                if attempt < MAX_RETRIES and self._is_network_error(exc):
                    await self._replace_proxy()
                    continue
                raise

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None
            self.log.info("Telethon client stopped")

    async def set_offline(self) -> None:
        if self.client is None:
            return
        from telethon.tl.functions.account import UpdateStatusRequest

        await asyncio.wait_for(
            self.client(UpdateStatusRequest(offline=True)),
            timeout=8,
        )

    async def resolve_chat_peer(self, target: str) -> Any:
        resolved = coerce_chat_id(target)
        try:
            return await self.client.get_entity(resolved)
        except Exception as exc:
            s = str(target).strip()
            if s.startswith("-100") and s.lstrip("-").isdigit():
                raw_id = int(s[4:])
                if raw_id > 0:
                    from telethon.tl.types import PeerChat
                    from telethon.utils import get_input_peer

                    self.log.debug(
                        "resolve %s failed (%s) — retrying as basic chat %s", target, exc, raw_id
                    )
                    return get_input_peer(PeerChat(chat_id=raw_id))
            raise

    async def join_chat(self, target: str) -> None:
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import AddChatUserRequest, ImportChatInviteRequest
        from telethon.tl.types import InputPeerChat, InputUserSelf

        invite_hash = telegram_invite_hash(target)
        if invite_hash:
            await self._with_flood_wait(
                lambda: self.client(ImportChatInviteRequest(hash=invite_hash))
            )
            return
        target = normalize_chat_target(target)
        if str(target).startswith("+"):
            await self._with_flood_wait(
                lambda: self.client(ImportChatInviteRequest(hash=str(target).lstrip("+")))
            )
            return
        peer = await self.resolve_chat_peer(target)
        if isinstance(peer, InputPeerChat):
            await self._with_flood_wait(
                lambda: self.client(AddChatUserRequest(chat_id=peer.chat_id, user_id=InputUserSelf()))
            )
        else:
            await self._with_flood_wait(lambda: self.client(JoinChannelRequest(channel=peer)))

    async def leave_chat(self, target: str) -> None:
        from telethon.tl.functions.channels import LeaveChannelRequest
        from telethon.tl.functions.messages import DeleteChatUserRequest
        from telethon.tl.types import InputPeerChat, InputUserSelf

        target = normalize_chat_target(target)
        peer = await self.resolve_chat_peer(target)
        if isinstance(peer, InputPeerChat):
            await self._with_flood_wait(
                lambda: self.client(DeleteChatUserRequest(chat_id=peer.chat_id, user_id=InputUserSelf()))
            )
        else:
            await self._with_flood_wait(lambda: self.client(LeaveChannelRequest(channel=peer)))

    async def open_link(self, link: str) -> None:
        parsed = parse_telegram_link(link)
        if parsed["kind"] == "post":
            await self.view_post(link)
            return
        if parsed["kind"] == "bot_start":
            payload = parsed.get("payload")
            command = f"/start {payload}" if payload else "/start"
            await self.send_message(str(parsed["target"]), command)
            return
        await self.join_chat(str(parsed["target"]))

    async def view_post(self, link: str) -> None:
        from telethon.tl.functions.messages import GetMessagesViewsRequest

        target, message_id = parse_post_link(link)
        peer = await self.client.get_input_entity(coerce_chat_id(target))
        await self._with_flood_wait(
            lambda: self.client(
                GetMessagesViewsRequest(
                    peer=peer,
                    id=[message_id],
                    increment=True,
                )
            )
        )

    async def random_reaction(self, link: str, *, mark_viewed: bool = True) -> str:
        target, message_id = parse_post_link(link)
        if mark_viewed:
            await self.view_post(link)
        reactions = await self._allowed_reactions(target)
        if not reactions:
            raise RuntimeError("No reactions are available for this post.")

        candidates = unique_reactions(reactions)[:5]
        random.shuffle(candidates)
        peer = await self.client.get_input_entity(coerce_chat_id(target))
        last_error: Exception | None = None
        for reaction in candidates:
            try:
                await self._send_reaction_to_peer(peer, message_id, reaction)
                self.log.info(
                    "Random reaction set for %s/%s: %s (available=%s)",
                    target,
                    message_id,
                    reaction.display,
                    len(reactions),
                )
                return f"reaction={reaction.display}; available={len(reactions)}"
            except Exception as exc:
                last_error = exc
                if flood_wait_seconds(exc) is not None or is_invalid_auth_error(exc):
                    raise
                self.log.warning(
                    "Reaction candidate failed for %s/%s: %s (%s)",
                    target,
                    message_id,
                    reaction.display,
                    exc,
                )
        if last_error:
            raise last_error
        raise RuntimeError("No reaction candidates generated.")

    async def available_reactions(self, link: str) -> list["ReactionChoice"]:
        target, _message_id = parse_post_link(link)
        return await self._allowed_reactions(target)

    async def set_reaction(self, link: str, reaction: "ReactionChoice") -> str:
        target, message_id = parse_post_link(link)
        await self.view_post(link)
        peer = await self.client.get_input_entity(coerce_chat_id(target))
        await self._send_reaction_to_peer(peer, message_id, reaction)
        self.log.info("Reaction set for %s/%s: %s", target, message_id, reaction.display)
        return f"reaction={reaction.display}"

    async def _send_reaction_to_peer(self, peer: Any, message_id: int, reaction: "ReactionChoice") -> None:
        from telethon.tl.functions.messages import SendReactionRequest

        await self._with_flood_wait(
            lambda: self.client(
                SendReactionRequest(
                    peer=peer,
                    msg_id=message_id,
                    reaction=[reaction.to_telethon_raw()],
                )
            )
        )

    async def _allowed_reactions(self, target: str) -> list["ReactionChoice"]:
        from telethon.tl import functions, types

        entity = await self.client.get_entity(coerce_chat_id(target))
        try:
            full = await self._with_flood_wait(
                lambda: self.client(functions.channels.GetFullChannelRequest(entity))
            )
        except Exception:
            full = await self._with_flood_wait(
                lambda: self.client(functions.messages.GetFullChatRequest(entity.id))
            )
        available = getattr(getattr(full, "full_chat", None), "available_reactions", None)
        reactions = reactions_from_telethon_chat_reactions(available)
        if isinstance(available, types.ChatReactionsAll) or not reactions:
            reactions = reactions or await self._global_reactions()
        reactions = unique_reactions(reactions or default_reaction_choices())
        return await self._with_custom_reaction_labels(reactions)

    async def _global_reactions(self) -> list["ReactionChoice"]:
        from telethon.tl.functions.messages import GetAvailableReactionsRequest

        result = await self._with_flood_wait(
            lambda: self.client(GetAvailableReactionsRequest(hash=0))
        )
        return [
            ReactionChoice.emoji(
                str(getattr(reaction, "reaction", "") or ""),
                str(getattr(reaction, "title", "") or ""),
            )
            for reaction in getattr(result, "reactions", []) or []
            if getattr(reaction, "reaction", None)
            and not getattr(reaction, "inactive", False)
            and not getattr(reaction, "premium", False)
        ]

    async def _with_custom_reaction_labels(self, reactions: list["ReactionChoice"]) -> list["ReactionChoice"]:
        from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest

        custom_ids = [reaction.custom_emoji_id for reaction in reactions if reaction.custom_emoji_id]
        if not custom_ids:
            return reactions
        try:
            docs = await self._with_flood_wait(
                lambda: self.client(GetCustomEmojiDocumentsRequest(document_id=custom_ids))
            )
        except Exception as exc:
            self.log.debug("Could not load custom reaction labels: %s", exc)
            return reactions
        labels = custom_emoji_labels_from_documents(docs)
        for reaction in reactions:
            if reaction.custom_emoji_id and reaction.custom_emoji_id in labels:
                reaction.title = labels[reaction.custom_emoji_id]
        return reactions

    async def update_profile(
        self,
        first_name: str | None = None,
        last_name: str | None = None,
        bio: str | None = None,
    ) -> None:
        from telethon.tl.functions.account import UpdateProfileRequest

        kwargs = {
            key: value
            for key, value in {
                "first_name": first_name,
                "last_name": last_name,
                "about": bio,
            }.items()
            if value is not None
        }
        if kwargs:
            await self._with_flood_wait(lambda: self.client(UpdateProfileRequest(**kwargs)))

    async def set_username(self, username: str | None) -> None:
        from telethon.tl.functions.account import UpdateUsernameRequest

        await self._with_flood_wait(lambda: self.client(UpdateUsernameRequest(username or "")))

    async def set_profile_photo(self, image_path: Path) -> None:
        from telethon.tl.functions.photos import UploadProfilePhotoRequest

        uploaded = await self.client.upload_file(str(image_path))
        await self._with_flood_wait(lambda: self.client(UploadProfilePhotoRequest(file=uploaded)))

    async def clear_profile_photos(self) -> None:
        from telethon.tl.functions.photos import DeletePhotosRequest, GetUserPhotosRequest

        photos = []
        offset = 0
        while True:
            result = await self.client(GetUserPhotosRequest(user_id="me", offset=offset, max_id=0, limit=100))
            if not result.photos:
                break
            photos.extend(result.photos)
            offset += len(result.photos)
        for index in range(0, len(photos), 100):
            chunk = photos[index:index + 100]
            try:
                await self._with_flood_wait(lambda chunk=chunk: self.client(DeletePhotosRequest(id=chunk)))
            except Exception as exc:
                self.log.warning("Could not delete profile photo chunk for %s: %s", self.account.id, exc)

    async def terminate_other_sessions(self, desktop_only: bool = False) -> int:
        from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest

        result = await self._with_flood_wait(lambda: self.client(GetAuthorizationsRequest()))
        removed = 0
        for authorization in getattr(result, "authorizations", []):
            if getattr(authorization, "current", False):
                continue
            if desktop_only and not is_desktop_authorization(authorization):
                continue
            await self._with_flood_wait(
                lambda authorization=authorization: self.client(
                    ResetAuthorizationRequest(hash=authorization.hash)
                )
            )
            removed += 1
        return removed

    async def delete_passkeys(self) -> int:
        from telethon.tl.functions.account import DeletePasskeyRequest, GetPasskeysRequest

        passkeys_result = await self._with_flood_wait(lambda: self.client(GetPasskeysRequest()))
        removed = 0
        for passkey in getattr(passkeys_result, "passkeys", []) or []:
            passkey_id = getattr(passkey, "id", "")
            if not passkey_id:
                continue
            await self._with_flood_wait(
                lambda passkey_id=passkey_id: self.client(DeletePasskeyRequest(id=passkey_id))
            )
            removed += 1
        return removed

    async def set_cloud_password(
        self,
        new_password: str,
        hint: str = "",
        current_password: str | None = None,
    ) -> None:
        raise RuntimeError("Cloud password setup is currently supported only for Pyrogram accounts.")

    async def remove_cloud_password(self, current_password: str) -> None:
        raise RuntimeError("Cloud password removal is currently supported only for Pyrogram accounts.")

    async def set_recovery_email(
        self,
        email: str,
        code_provider,
        new_password: str | None = None,
        hint: str = "",
        current_password: str | None = None,
    ) -> None:
        raise RuntimeError("Recovery email setup is currently supported only for Pyrogram accounts.")

    async def set_login_email(self, email: str, code_provider) -> None:
        raise RuntimeError("Login email setup is currently supported only for Pyrogram accounts.")

    async def get_security_state(self) -> dict[str, Any]:
        from telethon.tl.functions.account import GetPasswordRequest

        password_info = await self._with_flood_wait(lambda: self.client(GetPasswordRequest()))
        bday = None
        raw_bday = getattr(fu, "birthday", None)
        if raw_bday is not None:
            bday = {
                "day": getattr(raw_bday, "day", 0),
                "month": getattr(raw_bday, "month", 0),
                "year": getattr(raw_bday, "year", None),
            }
        return {
            "cloud_password": bool(getattr(password_info, "has_password", False)),
            "recovery_email_hint": getattr(password_info, "email_unconfirmed_pattern", None),
        }

    async def get_recent_service_messages(self, since_ts: int, limit: int = 25) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        async for message in self.client.iter_messages(777000, limit=limit):
            created_at = _datetime_timestamp(getattr(message, "date", None))
            if created_at and created_at < since_ts:
                break
            text = getattr(message, "message", None) or getattr(message, "text", None) or ""
            if text:
                messages.append({"received_at": created_at, "text": text})
        return messages

    async def get_recent_chat_messages(self, chat: str, limit: int = 10, *, include_media: bool = False) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        async for message in self.client.iter_messages(chat, limit=limit):
            created_at = _datetime_timestamp(getattr(message, "date", None))
            text = getattr(message, "message", None) or getattr(message, "text", None) or ""
            if text or include_media and getattr(message, "media", None):
                messages.append(
                    {
                        "id": getattr(message, "id", 0),
                        "received_at": created_at,
                        "text": text,
                        "outgoing": bool(getattr(message, "out", False)),
                    }
                )
        return messages

    async def send_message(self, chat: str, text: str) -> None:
        await self._with_flood_wait(lambda: self.client.send_message(chat, text))

    async def get_me(self) -> dict[str, Any]:
        user = await self.client.get_me()
        return {
            "user_id": getattr(user, "id", None),
            "username": getattr(user, "username", None),
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "phone": getattr(user, "phone", None),
            "language_code": getattr(user, "lang_code", None) or getattr(user, "language_code", None),
        }

    async def get_message_detail(self, link: str) -> dict[str, Any]:
        target, message_id = parse_post_link(link)
        entity = await self.client.get_entity(coerce_chat_id(target))
        message = await self._with_flood_wait(lambda: self.client.get_messages(entity, ids=message_id))
        if message is None:
            raise RuntimeError(f"Message not found: {link}")
        text = getattr(message, "message", None) or getattr(message, "text", None) or ""
        buttons: list[dict[str, Any]] = []
        markup = getattr(message, "reply_markup", None)
        rows = getattr(markup, "rows", None) or []
        for row in rows:
            for button in getattr(row, "buttons", None) or []:
                url = getattr(button, "url", None)
                data = getattr(button, "data", None)
                if isinstance(data, (bytes, bytearray)):
                    data = data.decode("utf-8", errors="replace")
                web_app_url = None
                web_app = getattr(button, "web_app", None) or getattr(button, "webViewUrl", None)
                if web_app is not None:
                    web_app_url = getattr(web_app, "url", None) or str(web_app)
                buttons.append(
                    {
                        "text": getattr(button, "text", None),
                        "url": url,
                        "callback_data": data,
                        "web_app_url": web_app_url,
                    }
                )
        return {
            "chat": target,
            "chat_id": getattr(entity, "id", None),
            "chat_username": getattr(entity, "username", None),
            "message_id": int(getattr(message, "id", message_id) or message_id),
            "text": text,
            "buttons": buttons,
        }

    async def request_bot_app_webview(
        self,
        bot: str,
        short_name: str,
        start_param: str,
        *,
        peer: str | None = None,
        platform: str = "android",
    ) -> dict[str, str]:
        from telethon.tl.functions.messages import RequestAppWebViewRequest
        from telethon.tl.types import InputBotAppShortName

        bot_entity = await self.client.get_input_entity(bot)
        peer_entity = await self.client.get_input_entity(peer or bot)
        result = await self._with_flood_wait(
            lambda: self.client(
                RequestAppWebViewRequest(
                    peer=peer_entity,
                    app=InputBotAppShortName(bot_id=bot_entity, short_name=short_name),
                    platform=platform,
                    write_allowed=True,
                    start_param=start_param or "",
                )
            )
        )
        url = str(getattr(result, "url", "") or "")
        if not url:
            raise RuntimeError("RequestAppWebView returned empty url")
        return {"url": url, **parse_webapp_url(url)}

    async def request_callback_answer(
        self,
        chat: str,
        message_id: int,
        data: str,
        *,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        from telethon.tl.functions.messages import GetBotCallbackAnswerRequest

        entity = await self.client.get_input_entity(coerce_chat_id(chat))
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        result = await asyncio.wait_for(
            self._with_flood_wait(
                lambda: self.client(
                    GetBotCallbackAnswerRequest(
                        peer=entity,
                        msg_id=int(message_id),
                        data=payload,
                    )
                )
            ),
            timeout=timeout,
        )
        return {
            "message": getattr(result, "message", None),
            "url": getattr(result, "url", None),
            "alert": bool(getattr(result, "alert", False)),
            "has_url": bool(getattr(result, "has_url", False)),
        }

    async def get_user_full(
        self,
        target: str,
        *,
        with_photo_count: bool = True,
        with_stories: bool = False,
    ) -> dict[str, Any]:
        from telethon.tl.functions.users import GetFullUserRequest

        entity = await self.client.get_entity(target)
        full = await self._with_flood_wait(lambda: self.client(GetFullUserRequest(id=entity)))
        fu = full.full_user
        has_photo = bool(getattr(entity, "photo", None) or getattr(fu, "profile_photo", None))
        photos = 0
        if with_photo_count:
            photos = await self._get_user_photos_count(entity.id)
            if photos == 0 and has_photo:
                photos = 1
        elif has_photo:
            photos = 1
        stories = 0
        if with_stories:
            try:
                from telethon.tl.functions.stories import GetPeerStoriesRequest
                r = await self._with_flood_wait(
                    lambda: self.client(GetPeerStoriesRequest(peer=entity))
                )
                peer_stories = getattr(r, "stories", None)
                items = getattr(peer_stories, "stories", None) if peer_stories is not None else None
                if items is None:
                    items = peer_stories if isinstance(peer_stories, list) else []
                stories = len(items)
            except Exception:
                stories = 0
        return {
            "user_id": entity.id,
            "username": getattr(entity, "username", None) or "",
            "first_name": getattr(entity, "first_name", None) or "",
            "last_name": getattr(entity, "last_name", None) or "",
            "bio": getattr(fu, "about", None) or "",
            "photo_count": photos,
            "stories_count": stories,
            "is_premium": getattr(entity, "premium", False),
            "birthday": bday,
        }

    async def download_profile_photos(self, target: str, limit: int = 20) -> list[bytes]:
        from telethon.tl.functions.photos import GetUserPhotosRequest

        entity = await self.client.get_entity(target)
        result: list[bytes] = []
        offset = 0
        while len(result) < limit:
            r = await self._with_flood_wait(
                lambda: self.client(GetUserPhotosRequest(
                    user_id=entity,
                    offset=offset, max_id=0, limit=min(100, limit - len(result)),
                ))
            )
            for photo in r.photos:
                buf = await self.client.download_media(photo, file=bytes)
                if buf:
                    result.append(buf)
            if len(r.photos) < min(100, limit - len(result)):
                break
            offset += len(r.photos)
        return result

    async def get_channel_members(self, target: str, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        from telethon.tl.functions.channels import GetParticipantsRequest
        from telethon.tl.types import ChannelParticipantsRecent

        entity = await self.client.get_entity(target)
        try:
            r = await self._with_flood_wait(
                lambda: self.client(GetParticipantsRequest(
                    channel=entity,
                    filter=ChannelParticipantsRecent(),
                    offset=offset,
                    limit=min(200, limit),
                    hash=0,
                ))
            )
        except Exception as exc:
            if "CHANNEL_PRIVATE" in str(exc).upper() or "CHAT_ADMIN" in str(exc).upper():
                return []
            raise
        result: list[dict[str, Any]] = []
        for user in r.users:
            result.append({
                "user_id": user.id,
                "username": getattr(user, "username", None) or "",
                "first_name": getattr(user, "first_name", None) or "",
                "last_name": getattr(user, "last_name", None) or "",
                "is_premium": getattr(user, "premium", False),
            })
        return result[:limit]

    async def get_recent_chat_senders(self, target: str, limit: int = 200) -> list[dict[str, Any]]:
        from telethon.tl.functions.messages import GetHistoryRequest

        entity = await self.client.get_entity(target)
        seen: set[int] = set()
        senders: list[dict[str, Any]] = []
        try:
            r = await self._with_flood_wait(
                lambda: self.client(GetHistoryRequest(
                    peer=entity, offset_id=0, offset_date=None, add_offset=0,
                    limit=min(200, limit), max_id=0, min_id=0, hash=0,
                ))
            )
            if hasattr(r, "users"):
                for u in r.users:
                    uid = getattr(u, "id", 0)
                    uname = (getattr(u, "username", None) or "").strip()
                    if not uname or uid in seen:
                        continue
                    seen.add(uid)
                    senders.append({
                        "user_id": uid,
                        "username": uname,
                        "first_name": getattr(u, "first_name", None) or "",
                        "last_name": getattr(u, "last_name", None) or "",
                        "is_premium": getattr(u, "premium", False),
                    })
        except Exception:
            pass
        return senders

    async def check_username_available(self, username: str) -> bool:
        from telethon.tl.functions.account import CheckUsernameRequest

        try:
            r = await self._with_flood_wait(
                lambda: self.client(CheckUsernameRequest(username=username.strip().lstrip("@")))
            )
            return bool(r)
        except Exception:
            return False

    async def supports_stories(self) -> bool:
        return False

    async def supports_profile_music(self) -> bool:
        return False

    async def set_birthday(self, day: int, month: int, year: int | None = None) -> None:
        try:
            from telethon.tl.functions.account import UpdateBirthdayRequest
            from telethon.tl.types import Birthday
            await self._with_flood_wait(
                lambda: self.client(UpdateBirthdayRequest(birthday=Birthday(day=day, month=month, year=year)))
            )
        except Exception:
            pass

    async def pin_active_stories(self) -> int:
        try:
            from telethon.tl.functions.stories import GetPeerStoriesRequest, TogglePinnedRequest

            peer = await self.client.get_input_entity("me")
            response = await self._with_flood_wait(
                lambda: self.client(GetPeerStoriesRequest(peer=peer))
            )
            raw_stories = getattr(response, "stories", None)
            stories = list(getattr(raw_stories, "stories", raw_stories or []) or [])
            story_ids = [int(story.id) for story in stories if getattr(story, "id", None)]
            if not story_ids:
                return 0
            await self._with_flood_wait(
                lambda: self.client(TogglePinnedRequest(peer=peer, id=story_ids, pinned=True))
            )
            return len(story_ids)
        except Exception:
            return 0

    async def clear_active_stories(self) -> int:
        try:
            from telethon.tl.functions.stories import DeleteStoriesRequest, GetPeerStoriesRequest

            peer = await self.client.get_input_entity("me")
            response = await self._with_flood_wait(
                lambda: self.client(GetPeerStoriesRequest(peer=peer))
            )
            raw_stories = getattr(response, "stories", None)
            stories = list(getattr(raw_stories, "stories", raw_stories or []) or [])
            story_ids = [int(story.id) for story in stories if getattr(story, "id", None)]
            if not story_ids:
                return 0
            await self._with_flood_wait(
                lambda: self.client(DeleteStoriesRequest(peer=peer, id=story_ids))
            )
            return len(story_ids)
        except Exception:
            return 0

    async def upload_story(self, story_info: dict[str, Any], caption: str = "") -> None:
        pass

    async def download_profile_music(self, target: str) -> dict[str, Any] | None:
        return None

    async def set_profile_music(self, music_bytes: bytes, meta: dict[str, Any]) -> None:
        pass

    async def set_profile_photos(self, paths: list[Path]) -> None:
        from telethon.tl.functions.photos import UploadProfilePhotoRequest

        for path in paths:
            uploaded = await self.client.upload_file(str(path))
            await self._with_flood_wait(
                lambda file=uploaded: self.client(UploadProfilePhotoRequest(file=file))
            )
            await asyncio.sleep(0.5)

    async def _get_user_photos_count(self, user_id: int) -> int:
        try:
            from telethon.tl.functions.photos import GetUserPhotosRequest
            entity = await self.client.get_entity(user_id)
            r = await self.client(GetUserPhotosRequest(user_id=entity, offset=0, max_id=0, limit=1))
            return r.count if hasattr(r, "count") else len(r.photos)
        except Exception:
            return 0

    async def probe_frozen(self) -> None:
        from telethon.tl.functions.account import UpdateStatusRequest
        await self._with_flood_wait(lambda: self.client(UpdateStatusRequest(offline=False)))

    async def keep_online(self, stop_event: asyncio.Event, interval: float = 45.0) -> None:
        from telethon.tl.functions.account import UpdateStatusRequest

        while not stop_event.is_set():
            await self._with_flood_wait(lambda: self.client(UpdateStatusRequest(offline=False)))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def _session_ref(self, string_session_cls):
        path = resolve_session_path(self.config, self.account.session_ref)
        if self.account.session_kind == "session_string":
            payload = json.loads(path.read_text(encoding="utf-8"))
            session_string = (
                payload.get("session_string")
                or payload.get("telethon_session")
                or payload.get("string_session")
            )
            return string_session_cls(session_string)
        return str(path.with_suffix(""))

    async def _with_flood_wait(self, factory):
        from telethon.errors import FloodWaitError

        try:
            return await self._with_retry(factory)
        except FloodWaitError as exc:
            wait_for = int(getattr(exc, "seconds", 0)) + 2
            self.flood_wait_count += 1
            if wait_for > self.flood_wait_raise_after:
                self.log.warning(
                    "FloodWait %s seconds — too long, raising for account swap (caller: %s)",
                    wait_for, self._flood_caller(),
                )
                raise
            self.log.warning("FloodWait for %s seconds", wait_for)
            await asyncio.sleep(wait_for)
            return await self._with_retry(factory)


class PyrogramGetPasskeys:
    __slots__ = []
    ID = 0xea1f0c52
    QUALNAME = "functions.account.GetPasskeys"

    def write(self, *args) -> bytes:
        from pyrogram.raw.core.primitives import Int

        return Int(self.ID, False)


class PyrogramDeletePasskey:
    __slots__ = ["id"]
    ID = 0xf5b5563f
    QUALNAME = "functions.account.DeletePasskey"

    def __init__(self, *, id: str) -> None:
        self.id = id

    def write(self, *args) -> bytes:
        from pyrogram.raw.core.primitives import Int, String

        b = BytesIO()
        b.write(Int(self.ID, False))
        b.write(String(self.id))
        return b.getvalue()


class PyrogramPasskey:
    __slots__ = ["id", "name", "date", "software_emoji_id", "last_usage_date"]
    ID = 0x98613ebf
    QUALNAME = "types.Passkey"

    def __init__(
        self,
        *,
        id: str,
        name: str,
        date: int,
        software_emoji_id: int | None = None,
        last_usage_date: int | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.date = date
        self.software_emoji_id = software_emoji_id
        self.last_usage_date = last_usage_date

    @staticmethod
    def read(b: BytesIO, *args: Any) -> "PyrogramPasskey":
        from pyrogram.raw.core.primitives import Int, Long, String

        flags = Int.read(b)
        passkey_id = String.read(b)
        name = String.read(b)
        date = Int.read(b)
        software_emoji_id = Long.read(b) if flags & 1 else None
        last_usage_date = Int.read(b) if flags & 2 else None
        return PyrogramPasskey(
            id=passkey_id,
            name=name,
            date=date,
            software_emoji_id=software_emoji_id,
            last_usage_date=last_usage_date,
        )


class PyrogramPasskeys:
    __slots__ = ["passkeys"]
    ID = 0xf8e0aa1c
    QUALNAME = "types.account.Passkeys"

    def __init__(self, *, passkeys: list[Any]) -> None:
        self.passkeys = passkeys

    @staticmethod
    def read(b: BytesIO, *args: Any) -> "PyrogramPasskeys":
        from pyrogram.raw.core import TLObject

        return PyrogramPasskeys(passkeys=TLObject.read(b))


def _register_pyrogram_passkey_raw_types() -> None:
    from pyrogram.raw.all import objects

    objects[PyrogramPasskey.ID] = PyrogramPasskey
    objects[PyrogramPasskeys.ID] = PyrogramPasskeys


def create_client(
    config: AppConfig, account: AccountRecord, *, lock_wait_timeout: float | None = None,
) -> AccountClient:
    if account.backend == "pyrogram":
        client = PyrogramAccountClient(config, account)
    elif account.backend == "telethon":
        client = TelethonAccountClient(config, account)
    else:
        raise ValueError(f"Unsupported backend: {account.backend}")
    client.lock_wait_timeout = lock_wait_timeout
    return client


DEFAULT_REACTION_EMOJIS = [
    "\U0001f44d",
    "\u2764",
    "\U0001f525",
    "\U0001f44f",
    "\U0001f601",
    "\U0001f389",
    "\U0001f929",
    "\U0001f64f",
    "\U0001f44c",
]


@dataclass
class ReactionChoice:
    kind: str
    value: str
    title: str = ""

    @classmethod
    def emoji(cls, emoji: str, title: str = "") -> "ReactionChoice":
        return cls(kind="emoji", value=str(emoji), title=str(title or ""))

    @classmethod
    def custom(cls, custom_emoji_id: int | str, title: str = "") -> "ReactionChoice":
        return cls(kind="custom", value=str(custom_emoji_id), title=str(title or ""))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReactionChoice":
        return cls(
            kind=str(data.get("kind") or "emoji"),
            value=str(data.get("value") or ""),
            title=str(data.get("title") or ""),
        )

    @property
    def custom_emoji_id(self) -> int | None:
        if self.kind != "custom":
            return None
        try:
            return int(self.value)
        except (TypeError, ValueError):
            return None

    @property
    def display(self) -> str:
        if self.kind == "custom":
            prefix = f"{self.title} " if self.title else ""
            return f"{prefix}custom:{self.value}"
        return f"{self.value} {self.title}".strip()

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "value": self.value, "title": self.title}

    def to_pyrogram_raw(self, raw: Any) -> Any:
        if self.kind == "custom":
            custom_id = self.custom_emoji_id
            if custom_id is None:
                raise ValueError(f"Invalid custom emoji id: {self.value}")
            return raw.types.ReactionCustomEmoji(document_id=custom_id)
        return raw.types.ReactionEmoji(emoticon=self.value)

    def to_telethon_raw(self) -> Any:
        from telethon.tl.types import ReactionCustomEmoji, ReactionEmoji

        if self.kind == "custom":
            custom_id = self.custom_emoji_id
            if custom_id is None:
                raise ValueError(f"Invalid custom emoji id: {self.value}")
            return ReactionCustomEmoji(document_id=custom_id)
        return ReactionEmoji(emoticon=self.value)


def parse_webapp_url(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    fragment = parse_qs(parsed.fragment)
    query = parse_qs(parsed.query)
    init_data = ""
    if fragment.get("tgWebAppData"):
        init_data = fragment["tgWebAppData"][0]
    elif query.get("tgWebAppData"):
        init_data = query["tgWebAppData"][0]
    start_param = ""
    if query.get("tgWebAppStartParam"):
        start_param = query["tgWebAppStartParam"][0]
    elif fragment.get("tgWebAppStartParam"):
        start_param = fragment["tgWebAppStartParam"][0]
    if init_data and "%" in init_data[:12]:
        from urllib.parse import unquote

        init_data = unquote(init_data)
    return {
        "init_data": init_data,
        "start_param": start_param,
        "base_url": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
    }


def parse_telegram_link(link: str) -> dict[str, str | None]:
    link = link.strip()
    if link.startswith("@"):
        return {"kind": "chat", "target": link, "payload": None, "message_id": None}
    if link.lower().startswith(("t.me/", "telegram.me/")):
        link = f"https://{link}"
    parsed = urlparse(link)
    if parsed.scheme and parsed.netloc.lower() not in {"t.me", "telegram.me"}:
        raise ValueError("Only Telegram links are supported.")
    if not parsed.scheme:
        return {"kind": "chat", "target": link, "payload": None, "message_id": None}

    path = parsed.path.strip("/")
    query = parse_qs(parsed.query)
    if path.startswith("+") or path.startswith("joinchat/"):
        return {"kind": "chat", "target": link, "payload": None, "message_id": None}

    parts = [part for part in path.split("/") if part]
    if len(parts) >= 3 and parts[0].lower() == "c" and parts[1].isdigit() and parts[2].isdigit():
        return {
            "kind": "post",
            "target": f"-100{parts[1]}",
            "payload": None,
            "message_id": parts[2],
        }
    if len(parts) >= 3 and parts[0].lower() == "s" and parts[2].isdigit():
        return {
            "kind": "post",
            "target": parts[1],
            "payload": None,
            "message_id": parts[2],
        }
    if len(parts) >= 2 and parts[1].isdigit():
        return {
            "kind": "post",
            "target": parts[0],
            "payload": None,
            "message_id": parts[1],
        }

    username = path.split("/", 1)[0]
    payload = (query.get("start") or query.get("startgroup") or [None])[0]
    if payload is not None:
        return {"kind": "bot_start", "target": username, "payload": payload, "message_id": None}
    if username.lower().endswith("bot"):
        return {"kind": "bot_start", "target": username, "payload": None, "message_id": None}
    return {"kind": "chat", "target": username, "payload": None, "message_id": None}


def parse_post_link(link: str) -> tuple[str, int]:
    parsed = parse_telegram_link(link)
    if parsed["kind"] != "post":
        raise ValueError("Post link is required, for example https://t.me/channel/123.")
    target = str(parsed["target"] or "").strip()
    message_id = int(str(parsed["message_id"] or "0"))
    if not target or message_id <= 0:
        raise ValueError("Telegram post link is incomplete.")
    return target, message_id


def normalize_chat_target(target: str) -> str:
    parsed = parse_telegram_link(target)
    return str(parsed["target"])


def telegram_invite_hash(target: str) -> str | None:
    """Return an invite hash from a t.me/+... or t.me/joinchat/... link."""
    value = str(target or "").strip()
    if value.lower().startswith(("t.me/", "telegram.me/")):
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.netloc.lower() not in {"t.me", "telegram.me"}:
        return None
    path = parsed.path.strip("/")
    if path.startswith("+"):
        return path[1:] or None
    if path.lower().startswith("joinchat/"):
        return path.split("/", 1)[1] or None
    return None


def coerce_chat_id(target: str) -> int | str:
    target = str(target).strip()
    if target.lstrip("-").isdigit():
        return int(target)
    return target


def unique_reactions(values: list[ReactionChoice]) -> list[ReactionChoice]:
    seen: set[tuple[str, str]] = set()
    result: list[ReactionChoice] = []
    for value in values:
        if not value.value:
            continue
        key = (value.kind, value.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def default_reaction_choices() -> list[ReactionChoice]:
    return [ReactionChoice.emoji(emoji) for emoji in DEFAULT_REACTION_EMOJIS]


def reactions_from_pyrogram_chat_reactions(available: Any) -> list[ReactionChoice]:
    reactions = getattr(available, "reactions", None) or []
    result: list[ReactionChoice] = []
    for reaction in reactions:
        emoji = getattr(reaction, "emoji", None)
        if emoji:
            result.append(ReactionChoice.emoji(str(emoji)))
            continue
        custom_emoji_id = getattr(reaction, "custom_emoji_id", None)
        if custom_emoji_id:
            result.append(ReactionChoice.custom(custom_emoji_id))
    return result


def reactions_from_telethon_chat_reactions(available: Any) -> list[ReactionChoice]:
    reactions = getattr(available, "reactions", None) or []
    result: list[ReactionChoice] = []
    for reaction in reactions:
        emoticon = getattr(reaction, "emoticon", None)
        if emoticon:
            result.append(ReactionChoice.emoji(str(emoticon)))
            continue
        document_id = getattr(reaction, "document_id", None)
        if document_id:
            result.append(ReactionChoice.custom(document_id))
    return result


def custom_emoji_labels_from_documents(documents: Any) -> dict[int, str]:
    labels: dict[int, str] = {}
    for document in documents or []:
        document_id = getattr(document, "id", None)
        if document_id is None:
            continue
        for attribute in getattr(document, "attributes", []) or []:
            alt = str(getattr(attribute, "alt", "") or "").strip()
            if alt:
                labels[int(document_id)] = alt
                break
    return labels


def _datetime_timestamp(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value.timestamp())
    except (AttributeError, OSError, OverflowError, ValueError):
        return 0


def is_desktop_authorization(authorization: Any) -> bool:
    text = " ".join(
        str(getattr(authorization, attr, "") or "").lower()
        for attr in ("app_name", "device_model", "platform", "system_version")
    )
    return any(
        marker in text
        for marker in (
            "desktop",
            "tdesktop",
            "windows",
            "mac",
            "macos",
            "linux",
            "web",
            "browser",
        )
    )
