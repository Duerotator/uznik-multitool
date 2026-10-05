from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.config import AppConfig
from core.session_manager import SessionManager
from core.storage import write_json_atomic
from core.telegram_connection import CLEANUP_TIMEOUT, CONNECT_TIMEOUT, REQUEST_TIMEOUT, connection_error, is_connection_error
from modules.accounts import AccountService
from modules.fingerprint_generator import FingerprintGenerator
from utils.telegram_errors import short_error

if TYPE_CHECKING:
    from pyrogram import Client

logger = logging.getLogger("auth-controller")


@dataclass
class AuthSession:
    phone: str
    client: Client
    phone_code_hash: str
    fingerprint: dict[str, str]
    proxy_url: str | None = None
    created_at: float = field(default_factory=time.time)
    attempts: int = 0


class AuthManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.sessions: dict[str, AuthSession] = {}
        self.accounts = AccountService(config)
        self.session_mgr = SessionManager(config)
        self.fingerprints = FingerprintGenerator(str(config.data_dir / "fingerprints.json"))
        self._lock = asyncio.Lock()
        (config.import_dir / "auth_input").mkdir(parents=True, exist_ok=True)

    async def verified_proxy_url(self, proxy_url: str | None = None) -> str | None:
        """Use the configured proxy or pool; an empty pool permits direct I/O."""
        from core.models import ProxyConfig
        from modules.proxy_manager import ProxyPool, validate_proxy

        proxy_url = proxy_url or self.config.global_proxy
        if proxy_url:
            try:
                cfg = ProxyConfig.from_url(proxy_url)
            except ValueError:
                raise RuntimeError("Некорректный адрес прокси: проверьте TELEGRAM_GLOBAL_PROXY в .env.") from None
            if cfg is not None:
                scheme = cfg.scheme.replace("socks5h", "socks5")
                auth = (cfg.username, cfg.password or "") if cfg.username else None
                try:
                    ok, _ = await asyncio.wait_for(validate_proxy(
                        cfg.hostname, cfg.port, scheme, timeout=8.0, auth=auth
                    ), 9.0)
                except (TimeoutError, OSError):
                    raise connection_error(proxy_url) from None
                if ok:
                    return proxy_url
            # An explicit proxy must never silently expose the local IP.
            raise connection_error(proxy_url)
        pool = ProxyPool(self.config.proxy_pool_db)
        # acquire already falls back to other protocols. Bound stale pool checks.
        try:
            entry = await asyncio.wait_for(pool.acquire(protocol="socks5"), CONNECT_TIMEOUT)
        except TimeoutError:
            raise connection_error(True) from None
        if entry is None:
            logger.info("No proxy configured or available; using direct Telegram connection")
            return None
        return entry.url

    async def send_code(self, phone: str, proxy_url: str | None = None) -> dict[str, Any]:
        self.config.require_telegram_api()
        from pyrogram import Client
        from pyrogram.types import SentCode

        async with self._lock:
            existing = self.sessions.get(phone)
            if existing:
                try:
                    await asyncio.wait_for(existing.client.disconnect(), CLEANUP_TIMEOUT)
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
            logger.info("Connecting to Telegram via %s", "proxy" if proxy_url else "local IP (direct)")
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
            sent: SentCode = await asyncio.wait_for(client.send_code(phone), REQUEST_TIMEOUT)
            phone_code_hash = sent.phone_code_hash

            async with self._lock:
                self.sessions[phone] = AuthSession(
                    phone=phone,
                    client=client,
                    phone_code_hash=phone_code_hash,
                    fingerprint=fp,
                    proxy_url=proxy_url,
                )

            return {
                "ok": True,
                "phone_code_hash": phone_code_hash,
                "type": str(sent.type),
                "timeout": getattr(sent, "timeout", 120),
                "fingerprint": {"device": fp["device_model"], "os": fp["system_version"]},
            }
        except BaseException as exc:
            try:
                await asyncio.wait_for(client.disconnect(), CLEANUP_TIMEOUT)
            except Exception:
                pass
            if not isinstance(exc, Exception):
                raise
            err = str(connection_error(proxy_url)) if is_connection_error(exc) else short_error(exc)
            logger.error("send_code failed for %s: %s", phone, err)
            return {"ok": False, "error": err}

    async def sign_in(self, phone: str, phone_code_hash: str, code: str) -> dict[str, Any]:
        from pyrogram.errors import BadRequest, FloodWait, PhoneCodeExpired, PhoneCodeInvalid, SessionPasswordNeeded

        async with self._lock:
            auth = self.sessions.get(phone)

        if not auth:
            return {"ok": False, "error": "Session not found. Call send_code first."}

        try:
            user = await asyncio.wait_for(auth.client.sign_in(phone, phone_code_hash, code), REQUEST_TIMEOUT)
            return await self._finalize_auth(phone, auth, user)
        except SessionPasswordNeeded:
            return {"ok": False, "status": "2fa_required", "hint": "Enter cloud password."}
        except (PhoneCodeInvalid, PhoneCodeExpired, BadRequest) as exc:
            return {"ok": False, "error": short_error(exc)}
        except FloodWait as exc:
            return {"ok": False, "error": f"Flood wait {exc.value}s", "wait": exc.value}
        except Exception as exc:
            logger.exception("sign_in failed for %s", phone)
            return {"ok": False, "error": str(connection_error(auth.proxy_url)) if is_connection_error(exc) else short_error(exc)}

    async def check_password(self, phone: str, password: str) -> dict[str, Any]:
        async with self._lock:
            auth = self.sessions.get(phone)

        if not auth:
            return {"ok": False, "error": "Session not found. Call send_code first."}

        try:
            user = await asyncio.wait_for(auth.client.check_password(password), REQUEST_TIMEOUT)
            return await self._finalize_auth(phone, auth, user)
        except Exception as exc:
            return {"ok": False, "error": str(connection_error(auth.proxy_url)) if is_connection_error(exc) else short_error(exc)}

    async def _finalize_auth(self, phone: str, auth: AuthSession, user: Any) -> dict[str, Any]:
        user_id = getattr(user, "id", 0) or 0
        first_name = getattr(user, "first_name", "") or ""
        last_name = getattr(user, "last_name", "") or ""
        username = getattr(user, "username", "") or ""

        session_filename = f"{phone}_{self.fingerprints.get_params(phone)['device_model'].replace(' ', '_')[:20]}"
        # The client is in-memory, so nothing is ever written to disk: export the
        # session string and hand SessionManager a json_dump, which it supports.
        dump_path = self.config.sessions_dir / "pyrogram" / f"{session_filename}.json"
        if dump_path.exists():
            dump_path = dump_path.with_name(f"{session_filename}_{time.time_ns()}.json")
        dump_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            session_string = await auth.client.export_session_string()
        except Exception as exc:
            try:
                await auth.client.disconnect()
            except Exception:
                pass
            return {"ok": False, "error": f"Failed to export session: {short_error(exc)}"}

        try:
            await auth.client.disconnect()
        except Exception:
            pass

        write_json_atomic(dump_path, {"backend": "pyrogram", "session_string": session_string, "phone": phone})

        try:
            imported = self.session_mgr.import_path(dump_path, group="inbox", backend_hint="pyrogram")
            for account in imported:
                account.user_id = user_id
                account.phone = phone
                account.username = username or None
                account.first_name = first_name
                account.last_name = last_name
                account.proxy = auth.proxy_url
                account.metadata["fingerprint"] = dict(auth.fingerprint)
                self.session_mgr._upsert_account(account)
        except Exception as exc:
            logger.warning("Auto-import failed for %s: %s", dump_path, exc)
            imported = []

        async with self._lock:
            self.sessions.pop(phone, None)

        return {
            "ok": bool(imported),
            "status": "authorized",
            "user_id": user_id,
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "session_path": str(dump_path),
            "imported": len(imported) > 0,
            "error": "" if imported else "New authorization was saved, but account import failed; source was kept.",
        }

    async def cleanup_expired(self, max_age_seconds: int = 300) -> int:
        now = time.time()
        removed = 0
        async with self._lock:
            for phone in list(self.sessions):
                auth = self.sessions[phone]
                if now - auth.created_at > max_age_seconds:
                    try:
                        await auth.client.disconnect()
                    except Exception:
                        pass
                    del self.sessions[phone]
                    removed += 1
        return removed

    async def close_session(self, phone: str) -> None:
        """Release a cancelled/failed login without expiring other queued logins."""
        async with self._lock:
            auth = self.sessions.pop(phone, None)
        if auth is not None:
            try:
                await auth.client.disconnect()
            except Exception:
                logger.debug("Login client was already disconnected", exc_info=True)
