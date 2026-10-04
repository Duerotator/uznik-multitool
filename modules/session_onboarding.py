"""Safely recreate local sessions from sessions supplied by another client.

The original file is never used as the application's working session and is
never deleted.  A temporary copy is used only to read the number and, when
possible, the Telegram login code from 777000.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.models import ProxyConfig
from core.session_manager import SessionManager
from modules.auth_controller import AuthManager

log = logging.getLogger("session-onboarding")

PHONE_RE = re.compile(r"(?<!\d)(\d{10,15})(?!\d)")
CODE_RE = re.compile(r"(?<!\d)(\d{5,6})(?!\d)")
SUPPORTED_SUFFIXES = {".session", ".json"}


def phone_hint_from_name(path: Path) -> str:
    """Return a conservative phone hint from a supplier filename, if present."""
    match = PHONE_RE.search(path.stem)
    return match.group(1) if match else ""


def session_sources(paths: Iterable[Path]) -> list[Path]:
    """Expand dropped files/directories without treating unrelated files as sessions."""
    result: list[Path] = []
    for path in paths:
        path = path.resolve()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            result.append(path)
        elif path.is_dir():
            result.extend(item for item in sorted(path.rglob("*")) if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES)
    return result


def queue_external_sessions(config: AppConfig, paths: Iterable[Path]) -> list[Path]:
    """Copy dropped session files to a dedicated queue, preserving their source."""
    queue = config.import_dir / "auth_input"
    queue.mkdir(parents=True, exist_ok=True)
    queued: list[Path] = []
    for source in session_sources(paths):
        target = queue / source.name
        if target.exists() and target.read_bytes() != source.read_bytes():
            target = queue / f"{source.stem}_{abs(hash(str(source.resolve()))) & 0xFFFFFFFF:08x}{source.suffix}"
        if not target.exists():
            shutil.copy2(source, target)
        queued.append(target)
    return queued


@dataclass
class OnboardingResult:
    source: Path
    phone: str = ""
    status: str = "error"
    error: str = ""
    imported: bool = False


CodeProvider = Callable[[str, Path], Awaitable[str | None]]
PasswordProvider = Callable[[str, Path], Awaitable[str | None]]


class ForeignSessionOnboarding:
    def __init__(self, config: AppConfig, auth: AuthManager | None = None) -> None:
        self.config = config
        self.auth = auth or AuthManager(config)
        self.sessions = SessionManager(config)

    async def process(
        self,
        paths: Iterable[Path],
        code_provider: CodeProvider | None = None,
        password_provider: PasswordProvider | None = None,
    ) -> list[OnboardingResult]:
        return [await self.process_one(path, code_provider, password_provider) for path in paths]

    async def process_one(
        self,
        source: Path,
        code_provider: CodeProvider | None = None,
        password_provider: PasswordProvider | None = None,
    ) -> OnboardingResult:
        source = source.resolve()
        result = OnboardingResult(source=source, phone=phone_hint_from_name(source))
        try:
            backend, kind = self.sessions.detect_session(source)
        except Exception as exc:
            result.error = f"Unsupported source: {exc}"
            return result
        if kind != "session_file":
            result.error = "Only .session files can read a Telegram login code; import JSON sessions normally."
            return result

        legacy: Any = None
        try:
            log.info("external session %s: opening supplied session", source.name)
            legacy = await self._open_legacy_copy(source, backend)
            me = await legacy["get_me"]()
            result.phone = str(getattr(me, "phone_number", "") or result.phone)
        except Exception as exc:
            # A filename phone can still be used. Keep processing the queue,
            # but the code cannot be read automatically without this session.
            log.warning("external session %s: could not open supplied session: %s", source.name, exc)
            await self._close_legacy(legacy)
            legacy = None
        if not result.phone:
            result.status = "needs_phone"
            result.error = "Phone number is not in the filename and the supplied session could not be opened."
            return result

        known_message_ids = await self._recent_service_message_ids(legacy)
        log.info("external session %s: requesting login code for %s", source.name, result.phone)
        sent = await self.auth.send_code(result.phone)
        if not sent.get("ok"):
            result.error = str(sent.get("error", "Could not send login code."))
            await self._close_legacy(legacy)
            return result

        log.info("external session %s: waiting for a fresh 777000 code", source.name)
        code = await self._read_recent_code(legacy, known_message_ids)
        if not code and code_provider:
            code = await code_provider(result.phone, source)
        await self._close_legacy(legacy)
        if not code:
            result.status = "needs_code"
            result.error = "Login code was not found in 777000. Enter it and retry this session."
            return result

        signed = await self.auth.sign_in(result.phone, str(sent["phone_code_hash"]), code)
        if signed.get("status") == "2fa_required":
            password = await password_provider(result.phone, source) if password_provider else ""
            if password:
                signed = await self.auth.check_password(result.phone, password)
            else:
                result.status = "needs_2fa"
                result.error = "Cloud password is required."
        elif signed.get("ok"):
            result.status = "authorized"
            result.imported = bool(signed.get("imported"))
            self._archive_source(source)
        if signed.get("ok") and result.status != "authorized":
            result.status = "authorized"
            result.imported = bool(signed.get("imported"))
            self._archive_source(source)
        elif result.status != "needs_2fa" and not signed.get("ok"):
            result.error = str(signed.get("error", "Sign-in failed."))
        return result

    @staticmethod
    def _archive_source(source: Path) -> None:
        """Keep the purchased source for audit, but do not process it twice."""
        archive = source.parent / "processed"
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / source.name
        if target.exists():
            target = archive / f"{source.stem}_{int(source.stat().st_mtime_ns)}{source.suffix}"
        shutil.move(str(source), str(target))

    async def _open_legacy_copy(self, source: Path, backend: str) -> dict[str, Any]:
        """Open a disposable copy so Pyrogram/Telethon cannot mutate the supplied file."""
        tmp_dir = Path(tempfile.mkdtemp(prefix="tgbmt_auth_"))
        copy = tmp_dir / source.name
        shutil.copy2(source, copy)
        proxy_url = await self.auth.verified_proxy_url()
        proxy = ProxyConfig.from_url(proxy_url)
        if backend == "telethon":
            from telethon import TelegramClient
            client = TelegramClient(str(copy.with_suffix("")), self.config.api_id, self.config.api_hash, proxy=proxy.to_telethon() if proxy else None)
            await client.connect()
            return {
                "get_me": client.get_me,
                "get_service_messages": lambda: self._telethon_service_messages(client),
                "close": client.disconnect,
                "tmp": tmp_dir,
            }
        from pyrogram import Client
        client = Client(name=copy.stem, api_id=self.config.api_id, api_hash=self.config.api_hash, workdir=str(copy.parent), no_updates=True, proxy=proxy.to_pyrogram() if proxy else None)
        await client.connect()
        return {
            "get_me": client.get_me,
            "get_service_messages": lambda: self._pyrogram_service_messages(client),
            "close": client.stop,
            "tmp": tmp_dir,
        }

    async def _pyrogram_service_messages(self, client: Any) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        async for message in client.get_chat_history(777000, limit=8):
            messages.append({
                "id": int(getattr(message, "id", 0) or 0),
                "text": str(getattr(message, "text", "") or ""),
            })
        return messages

    async def _telethon_service_messages(self, client: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": int(getattr(message, "id", 0) or 0),
                "text": str(getattr(message, "message", "") or ""),
            }
            for message in await client.get_messages(777000, limit=8)
        ]

    async def _recent_service_message_ids(self, legacy: dict[str, Any] | None) -> set[int]:
        if not legacy:
            return set()
        try:
            return {
                int(message.get("id") or 0)
                for message in await legacy["get_service_messages"]()
                if int(message.get("id") or 0)
            }
        except Exception as exc:
            log.debug("Could not read baseline 777000 messages: %s", exc)
            return set()

    async def _read_recent_code(self, legacy: dict[str, Any] | None, known_message_ids: set[int]) -> str:
        if not legacy:
            return ""
        # Login messages may arrive later than send_code() returns. Only
        # accept a message created after the request, never an old code.
        for attempt in range(15):
            try:
                messages = await legacy["get_service_messages"]()
                for message in messages:
                    message_id = int(message.get("id") or 0)
                    if not message_id or message_id in known_message_ids:
                        continue
                    codes = CODE_RE.findall(str(message.get("text") or ""))
                    if codes:
                        return codes[0]
            except Exception as exc:
                log.warning(
                    "external session 777000 read failed (attempt %d/%d): %s",
                    attempt + 1,
                    15,
                    exc,
                )
            if attempt < 14:
                await asyncio.sleep(2)
        return ""

    async def _close_legacy(self, legacy: dict[str, Any] | None) -> None:
        if not legacy:
            return
        try:
            await legacy["close"]()
        except Exception:
            # Pyrogram can terminate its temporary client while handling an
            # authorization transition.  It is already closed in that case.
            pass
        finally:
            shutil.rmtree(legacy["tmp"], ignore_errors=True)
