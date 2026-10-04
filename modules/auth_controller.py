from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.config import AppConfig
from core.session_manager import SessionManager
from core.storage import write_json_atomic
from modules.accounts import AccountService
from modules.fingerprint_generator import FingerprintGenerator
from utils.telegram_errors import short_error

if TYPE_CHECKING:
    from pyrogram import Client

logger = logging.getLogger("auth-controller")

IMPORT_WATCH_DIR = Path("imports/auth_input")
MAX_RETRIES = 5
RETRY_COOLDOWN = 30
PROCESSED_MEMORY = 2000


@dataclass
class AuthSession:
    phone: str
    client: Client
    phone_code_hash: str
    fingerprint: dict[str, str]
    created_at: float = field(default_factory=time.time)
    attempts: int = 0


class AuthManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.sessions: dict[str, AuthSession] = {}
        self.accounts = AccountService(config)
        self.session_mgr = SessionManager(config)
        self.fingerprints = FingerprintGenerator()
        self._lock = asyncio.Lock()
        IMPORT_WATCH_DIR.mkdir(parents=True, exist_ok=True)

    async def verified_proxy_url(self, proxy_url: str | None = None) -> str:
        """Return an MTProto-verified proxy; never permit direct Telegram I/O."""
        from core.models import ProxyConfig
        from modules.proxy_manager import ProxyPool, validate_proxy

        if proxy_url:
            cfg = ProxyConfig.from_url(proxy_url)
            if cfg is not None:
                scheme = cfg.scheme.replace("socks5h", "socks5")
                auth = (cfg.username, cfg.password or "") if cfg.username else None
                ok, _ = await validate_proxy(
                    cfg.hostname, cfg.port, scheme, timeout=8.0, auth=auth
                )
                if ok:
                    return proxy_url
        pool = ProxyPool(self.config.proxy_pool_db)
        entry = await pool.acquire(protocol="socks5") or await pool.acquire(protocol=None)
        if entry is None:
            raise RuntimeError("No MTProto-verified proxy available; direct connection is forbidden")
        return entry.url

    async def send_code(self, phone: str, proxy_url: str | None = None) -> dict[str, Any]:
        from pyrogram import Client
        from pyrogram.types import SentCode

        async with self._lock:
            existing = self.sessions.get(phone)
            if existing:
                try:
                    await existing.client.stop()
                except Exception:
                    pass
            self.sessions.pop(phone, None)

        fp = self.fingerprints.get_params(phone)
        try:
            proxy_url = await self.verified_proxy_url(proxy_url)
            from core.models import ProxyConfig
            proxy_cfg = ProxyConfig.from_url(proxy_url)
            proxy_dict = proxy_cfg.to_pyrogram() if proxy_cfg else None
        except Exception as exc:
            return {"ok": False, "error": short_error(exc)}

        client = Client(
            name=f":memory:_{phone}_{int(time.time())}",
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            device_model=fp["device_model"],
            system_version=fp["system_version"],
            app_version=fp["app_version"],
            lang_code=fp["lang_code"],
            proxy=proxy_dict,
            in_memory=True,
            no_updates=True,
        )

        try:
            await client.connect()
            sent: SentCode = await client.send_code(phone)
            phone_code_hash = sent.phone_code_hash

            async with self._lock:
                self.sessions[phone] = AuthSession(
                    phone=phone,
                    client=client,
                    phone_code_hash=phone_code_hash,
                    fingerprint=fp,
                )

            return {
                "ok": True,
                "phone_code_hash": phone_code_hash,
                "type": str(sent.type),
                "timeout": getattr(sent, "timeout", 120),
                "fingerprint": {"device": fp["device_model"], "os": fp["system_version"]},
            }
        except Exception as exc:
            try:
                await client.stop()
            except Exception:
                pass
            err = short_error(exc)
            logger.error("send_code failed for %s: %s", phone, err)
            return {"ok": False, "error": err}

    async def sign_in(self, phone: str, phone_code_hash: str, code: str) -> dict[str, Any]:
        from pyrogram.errors import BadRequest, FloodWait, PhoneCodeExpired, PhoneCodeInvalid, SessionPasswordNeeded

        async with self._lock:
            auth = self.sessions.get(phone)

        if not auth:
            return {"ok": False, "error": "Session not found. Call send_code first."}

        try:
            user = await auth.client.sign_in(phone, phone_code_hash, code)
            return await self._finalize_auth(phone, auth, user)
        except SessionPasswordNeeded:
            return {"ok": False, "status": "2fa_required", "hint": "Enter cloud password."}
        except (PhoneCodeInvalid, PhoneCodeExpired, BadRequest) as exc:
            return {"ok": False, "error": short_error(exc)}
        except FloodWait as exc:
            return {"ok": False, "error": f"Flood wait {exc.value}s", "wait": exc.value}
        except Exception as exc:
            logger.exception("sign_in failed for %s", phone)
            return {"ok": False, "error": short_error(exc)}

    async def check_password(self, phone: str, password: str) -> dict[str, Any]:
        async with self._lock:
            auth = self.sessions.get(phone)

        if not auth:
            return {"ok": False, "error": "Session not found. Call send_code first."}

        try:
            user = await auth.client.check_password(password)
            return await self._finalize_auth(phone, auth, user)
        except Exception as exc:
            return {"ok": False, "error": short_error(exc)}

    async def _finalize_auth(self, phone: str, auth: AuthSession, user: Any) -> dict[str, Any]:
        user_id = getattr(user, "id", 0) or 0
        first_name = getattr(user, "first_name", "") or ""
        last_name = getattr(user, "last_name", "") or ""
        username = getattr(user, "username", "") or ""

        session_filename = f"{phone}_{self.fingerprints.get_params(phone)['device_model'].replace(' ', '_')[:20]}"
        # The client is in-memory, so nothing is ever written to disk: export the
        # session string and hand SessionManager a json_dump, which it supports.
        dump_path = self.config.sessions_dir / "pyrogram" / f"{session_filename}.json"
        dump_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            session_string = await auth.client.export_session_string()
        except Exception as exc:
            try:
                await auth.client.stop()
            except Exception:
                pass
            return {"ok": False, "error": f"Failed to export session: {short_error(exc)}"}

        try:
            await auth.client.stop()
        except Exception:
            pass

        write_json_atomic(dump_path, {"backend": "pyrogram", "session_string": session_string, "phone": phone})

        try:
            imported = self.session_mgr.import_path(dump_path, group="inbox", backend_hint="pyrogram")
        except Exception as exc:
            logger.warning("Auto-import failed for %s: %s", dump_path, exc)
            imported = []

        async with self._lock:
            self.sessions.pop(phone, None)

        return {
            "ok": True,
            "status": "authorized",
            "user_id": user_id,
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "session_path": str(dump_path),
            "imported": len(imported) > 0,
        }

    async def cleanup_expired(self, max_age_seconds: int = 300) -> int:
        now = time.time()
        removed = 0
        async with self._lock:
            for phone in list(self.sessions):
                auth = self.sessions[phone]
                if now - auth.created_at > max_age_seconds:
                    try:
                        await auth.client.stop()
                    except Exception:
                        pass
                    del self.sessions[phone]
                    removed += 1
        return removed


class AuthImportWatcher:
    def __init__(self, config: AppConfig, auth_mgr: AuthManager):
        self.config = config
        self.auth = auth_mgr
        self.log = logging.getLogger("auth-import")
        IMPORT_WATCH_DIR.mkdir(parents=True, exist_ok=True)
        self._processed: deque[str] = deque(maxlen=PROCESSED_MEMORY)
        self._processed_set: set[str] = set()
        self._attempts: dict[str, tuple[int, float]] = {}

    def _remember(self, key: str) -> None:
        """Bounded memory of handled files — the watcher runs for the process lifetime."""
        if key in self._processed_set:
            return
        if len(self._processed) == self._processed.maxlen:
            self._processed_set.discard(self._processed[0])
        self._processed.append(key)
        self._processed_set.add(key)
        self._attempts.pop(key, None)

    async def watch(self, stop_event: asyncio.Event) -> None:
        self.log.info("Auth import watcher started on %s", IMPORT_WATCH_DIR)
        while not stop_event.is_set():
            for item in sorted(IMPORT_WATCH_DIR.glob("*.session")):
                key = str(item.resolve())
                if key in self._processed_set:
                    continue
                attempts, last_try = self._attempts.get(key, (0, 0))
                now = time.time()
                if attempts >= MAX_RETRIES:
                    self.log.warning("Max retries for %s, deleting.", item.name)
                    item.unlink(missing_ok=True)
                    self._remember(key)
                    continue
                if now - last_try < RETRY_COOLDOWN:
                    continue

                self._attempts[key] = (attempts + 1, now)
                try:
                    result = await self._recreate_session(item)
                    if result.get("ok"):
                        self.log.info("Session recreated from %s => %s", item.name, result.get("session_path"))
                        item.unlink(missing_ok=True)
                        self._remember(key)
                    else:
                        self.log.warning("Attempt %d failed for %s: %s", attempts + 1, item.name, result.get("error"))
                except Exception as exc:
                    self.log.error("Recreate error for %s: %s", item.name, exc)

            await asyncio.sleep(5)

    async def _recreate_session(self, session_path: Path) -> dict[str, Any]:
        from pyrogram import Client

        try:
            proxy_url = await self.auth.verified_proxy_url()
            from core.models import ProxyConfig
            proxy_cfg = ProxyConfig.from_url(proxy_url)
        except Exception as exc:
            return {"ok": False, "error": short_error(exc)}

        # A .session file is a SQLite database, not a session string — open it as
        # the on-disk session Pyrogram expects.
        client = Client(
            name=session_path.stem,
            api_id=self.config.api_id,
            api_hash=self.config.api_hash,
            workdir=str(session_path.parent),
            in_memory=False,
            no_updates=True,
            proxy=proxy_cfg.to_pyrogram() if proxy_cfg else None,
        )
        await client.connect()

        try:
            me = await client.get_me()
            phone = getattr(me, "phone_number", "") or ""
            if not phone:
                await client.stop()
                return {"ok": False, "error": "No phone in session."}

            result = await self.auth.send_code(phone, proxy_url=proxy_url)
            if not result.get("ok"):
                await client.stop()
                return result

            phone_code_hash = result["phone_code_hash"]

            service_msgs = await client.get_messages(777000, limit=5)
            code = ""
            for msg in service_msgs:
                text = str(getattr(msg, "text", ""))
                codes = re.findall(r"\b(\d{5,6})\b", text)
                if codes:
                    code = codes[0]
                    break

            if not code:
                await client.stop()
                return {"ok": False, "error": "No login code found in service messages."}

            sign_result = await self.auth.sign_in(phone, phone_code_hash, code)
            await client.stop()
            return sign_result
        except Exception as exc:
            try:
                await client.stop()
            except Exception:
                pass
            return {"ok": False, "error": short_error(exc)}
