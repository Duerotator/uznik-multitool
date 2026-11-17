from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.telegram_client import create_client
from modules.accounts import AccountService
from modules.sleep_scheduler import SleepScheduler
from utils.rate_limit import human_delay
from utils.telegram_errors import is_frozen_error, is_invalid_auth_error, short_error

_DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openai-compatible": "https://api.openai.com/v1",
    "grok": "https://api.x.ai/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}
_DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"
_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
_MAX_OUTPUT_TOKENS = 400


@dataclass
class AIConversationConfig:
    chat: str
    topic: str
    message_count: int = 20
    min_delay: float = 1.5
    max_delay: float = 4.5
    style: str = "casual beta-test conversation"
    provider: str = "openai"
    model: str = "gpt-4.1-mini"
    api_key: str = ""
    base_url: str | None = None
    temperature: float = 0.8
    personas: dict[str, str] = field(default_factory=dict)


class LLMClient:
    def __init__(self, app_config: AppConfig, run_config: AIConversationConfig):
        self.app_config = app_config
        self.run_config = run_config
        self.provider = run_config.provider.lower().strip()
        self.api_key = run_config.api_key or app_config.llm_api_key
        self.base_url = self._resolve_base_url(run_config.base_url or app_config.llm_base_url)
        self._client = None

    def _resolve_base_url(self, configured: str | None) -> str:
        raw = (configured or "").strip().rstrip("/")
        default = _DEFAULT_BASE_URLS.get(self.provider, _DEFAULT_OPENAI_BASE)
        if not raw:
            return default
        if self.provider not in {"openai", "openai-compatible", "grok"} and raw == _DEFAULT_OPENAI_BASE:
            return default
        return raw

    def _proxy_url(self) -> str | None:
        proxy = str(getattr(self.app_config, "global_proxy", None) or "").strip()
        if proxy.startswith(("http://", "https://")):
            return proxy
        return None

    async def _get_client(self):
        if self._client is not None:
            return self._client
        httpx = self._httpx()
        kwargs: dict[str, Any] = {"timeout": 60}
        proxy = self._proxy_url()
        if proxy:
            kwargs["proxy"] = proxy
        try:
            self._client = httpx.AsyncClient(**kwargs)
        except (TypeError, ImportError, ValueError):
            kwargs.pop("proxy", None)
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def __aenter__(self) -> "LLMClient":
        await self._get_client()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def _post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        provider_name: str,
    ) -> dict[str, Any]:
        httpx = self._httpx()
        client = await self._get_client()
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.ConnectError as exc:
                last_exc = RuntimeError(
                    f"Cannot connect to {provider_name} endpoint {url}. "
                    "Check internet/VPN/DNS and LLM_BASE_URL."
                )
                last_exc.__cause__ = exc
            except httpx.TimeoutException as exc:
                last_exc = RuntimeError(f"{provider_name} request timed out after 60s.")
                last_exc.__cause__ = exc
            except httpx.RequestError as exc:
                last_exc = RuntimeError(f"{provider_name} network request failed: {exc}")
                last_exc.__cause__ = exc
            else:
                if response.status_code < 400:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise RuntimeError(f"{provider_name} returned invalid JSON.") from exc
                    if not isinstance(data, dict):
                        raise RuntimeError(f"{provider_name} returned unexpected JSON shape.")
                    return data
                if response.status_code not in _TRANSIENT_HTTP_CODES or attempt == 2:
                    self._raise_for_status(response, provider_name)
                retry_after = response.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.isdigit() else min(8.0, 1.5 * (2 ** attempt))
                await asyncio.sleep(min(max(delay, 0.5), 10.0))
                continue
            if attempt == 2:
                raise last_exc
            await asyncio.sleep(1.5 * (2 ** attempt))
        assert last_exc is not None
        raise last_exc

    async def complete(self, messages: list[dict[str, str]]) -> str:
        if not self.api_key:
            raise RuntimeError("LLM API key is required for AI companion.")
        if self.provider in {"openai", "grok", "openai-compatible"}:
            return await self._openai_compatible(messages)
        if self.provider == "anthropic":
            return await self._anthropic(messages)
        if self.provider in {"google", "gemini"}:
            return await self._gemini(messages)
        raise ValueError(f"Unsupported LLM provider: {self.provider}")

    async def _openai_compatible(self, messages: list[dict[str, str]]) -> str:
        model = str(self.run_config.model or "").strip()
        low_model = model.lower()
        is_reasoning = low_model.startswith(("o1", "o3", "o4", "gpt-5", "gpt-6"))
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if is_reasoning and self.provider == "openai":
            payload["max_completion_tokens"] = _MAX_OUTPUT_TOKENS
            if not low_model.startswith(("o1", "o3", "o4")):
                payload["temperature"] = self.run_config.temperature
        else:
            payload["temperature"] = self.run_config.temperature
            payload["max_tokens"] = _MAX_OUTPUT_TOKENS
        data = await self._post_json(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            payload=payload,
            provider_name="OpenAI-compatible",
        )
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        return str(message.get("content") or "").strip()

    async def _anthropic(self, messages: list[dict[str, str]]) -> str:
        system = "\n".join(item["content"] for item in messages if item["role"] == "system")
        user_messages = [
            {"role": "assistant" if item["role"] == "assistant" else "user", "content": item["content"]}
            for item in messages
            if item["role"] != "system"
        ]
        payload = {
            "model": self.run_config.model,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "temperature": self.run_config.temperature,
            "system": system,
            "messages": user_messages or [{"role": "user", "content": "Start."}],
        }
        data = await self._post_json(
            f"{self.base_url}/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload=payload,
            provider_name="Anthropic",
        )
        content = data.get("content") or []
        if not isinstance(content, list):
            return ""
        return "".join(part.get("text", "") for part in content if isinstance(part, dict)).strip()

    async def _gemini(self, messages: list[dict[str, str]]) -> str:
        system_parts = [item["content"] for item in messages if item.get("role") == "system" and item.get("content")]
        contents = [
            {
                "role": "model" if item.get("role") == "assistant" else "user",
                "parts": [{"text": str(item.get("content") or "")}],
            }
            for item in messages
            if item.get("role") != "system" and item.get("content")
        ]
        payload: dict[str, Any] = {
            "contents": contents or [{"role": "user", "parts": [{"text": "Start."}]}],
            "generationConfig": {
                "temperature": self.run_config.temperature,
                "maxOutputTokens": _MAX_OUTPUT_TOKENS,
            },
        }
        if system_parts:
            payload["system_instruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
        url = f"{self.base_url}/models/{self.run_config.model}:generateContent"
        data = await self._post_json(
            url,
            headers={
                "x-api-key": self.api_key,
                "x-goog-api-key": self.api_key,
                "content-type": "application/json",
            },
            payload=payload,
            provider_name="Google Gemini",
        )
        candidates = data.get("candidates") or []
        if not isinstance(candidates, list) or not candidates:
            return ""
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        parts = (first.get("content") or {}).get("parts") or []
        if not isinstance(parts, list):
            return ""
        return "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()

    def _httpx(self):
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - depends on runtime environment
            raise RuntimeError("Install httpx to use AI companion: pip install -r config/requirements.txt") from exc
        return httpx

    def _raise_for_status(self, response, provider_name: str) -> None:
        if response.status_code < 400:
            return
        body = response.text.replace("\n", " ")[:300]
        if response.status_code in {401, 403}:
            raise RuntimeError(
                f"{provider_name} API auth failed ({response.status_code}). "
                "Check LLM_PROVIDER, LLM_API_KEY, and model. "
                f"Response: {body}"
            )
        raise RuntimeError(
            f"{provider_name} API request failed ({response.status_code}). Response: {body}"
        )


class AICompanion:
    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("ai-companion")

    async def run(
        self,
        accounts: list[AccountRecord],
        run_config: AIConversationConfig,
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        if self.config.ai_allowed_chats and run_config.chat not in self.config.ai_allowed_chats:
            raise PermissionError(
                f"Chat {run_config.chat} is not listed in AI_ALLOWED_CHATS."
            )
        if run_config.message_count < 1:
            return ActionResult()

        scheduler = SleepScheduler(str(self.config.data_dir / "sleep_zones.json"))
        active_accounts = [
            account
            for account in accounts
            if account.enabled and scheduler.state(account.id) != "sleeping"
        ]
        if len(active_accounts) < 2:
            raise RuntimeError("AI companion needs at least two enabled, awake accounts.")

        history: list[dict[str, str]] = []
        stop_event = stop_event or asyncio.Event()
        result = ActionResult()
        rng = random.Random()
        last_speaker_id: str | None = None
        seeded_chat = False

        async with LLMClient(self.config, run_config) as llm:
            for index in range(run_config.message_count):
                if stop_event.is_set():
                    break
                refreshed: list[AccountRecord] = []
                for candidate in active_accounts:
                    try:
                        fresh = self.accounts.get_account(candidate.id)
                    except KeyError:
                        fresh = candidate
                    if fresh.enabled and scheduler.state(fresh.id) != "sleeping":
                        refreshed.append(fresh)
                active_accounts = refreshed
                if len(active_accounts) < 2:
                    break
                choices = [item for item in active_accounts if item.id != last_speaker_id] or active_accounts
                account = rng.choice(choices)
                last_speaker_id = account.id
                persona = self._persona(account, run_config)

                if not seeded_chat:
                    seeded_chat = True
                    try:
                        async with create_client(self.config, account) as seed_client:
                            recent = await seed_client.get_recent_chat_messages(run_config.chat, limit=6)
                        for item in reversed(recent):
                            body = str(item.get("text") or "").strip()
                            if body:
                                history.append({"speaker": "chat", "content": body})
                        history = history[-30:]
                    except Exception:
                        pass

                text = await self._next_message(
                    llm, run_config, history, persona, account, active_accounts, index
                )
                if not text:
                    continue
                await human_delay(run_config.min_delay, run_config.max_delay, stop_event=stop_event)
                if stop_event.is_set():
                    break
                try:
                    async with create_client(self.config, account) as client:
                        await client.send_message(run_config.chat, text)
                    history.append({"speaker": self._display_name(account), "content": text})
                    history = history[-30:]
                    self.log.info("AI message sent by %s", account.id)
                    result.add_ok(account.id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = short_error(exc)
                    result.add_error(account.id, error)
                    if is_frozen_error(exc):
                        from datetime import datetime, timezone
                        self.accounts.update_profile_metadata(
                            account.id,
                            {"health_status": "frozen", "health_checked_at": datetime.now(timezone.utc).isoformat()},
                        )
                        self.accounts.mark_error(account.id, error, disable=True)
                        account.enabled = False
                        self.log.warning("Disabled frozen session %s: %s", account.id, error)
                    else:
                        disable = is_invalid_auth_error(exc)
                        self.accounts.mark_error(account.id, error, disable=disable)
                        if disable:
                            account.enabled = False
                            self.log.error("Disabled invalid session %s: %s", account.id, error)
                        else:
                            self.log.error("AI message failed for %s: %s", account.id, error)
        return result

    async def _next_message(
        self,
        llm: LLMClient,
        run_config: AIConversationConfig,
        history: list[dict[str, str]],
        persona: str,
        account: AccountRecord,
        participants: list[AccountRecord],
        index: int,
    ) -> str:
        system = (
            "You are writing one short Telegram message for a controlled beta-test chat. "
            "Do not advertise, do not persuade users to join anything, do not impersonate "
            "real people, and do not mention that you are an AI. "
            f"Persona for this account: {persona}. "
            f"Conversation topic: {run_config.topic}. "
            f"Style: {run_config.style}. "
            f"Current speaker: {self._display_name(account)}. "
            f"Participants: {self._participant_roster(participants)}. "
            f"Do not mention the current speaker ({self._display_name(account)}) in the message. "
            "Do not prefix the message with a speaker name, nickname, username, or colon label. "
            "If you directly reply to another participant, use the exact mention token from Participants. "
            "Never tag or mention numeric account IDs, phone-like labels, or internal account IDs. "
            "Return only the message text, under 450 characters."
        )
        recent = self._recent_transcript(history[-12:])
        prompt = (
            f"Message #{index + 1}. Account: {self._display_name(account)}. "
            f"Recent chat:\n{recent}\n\n"
            "Continue naturally, avoid repetition, and keep it plausible for a small test group. "
            "Return the message body only."
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        text = await llm.complete(messages)
        return self._clean_message(text, account, participants)

    def _persona(self, account: AccountRecord, run_config: AIConversationConfig) -> str:
        if account.id in run_config.personas:
            return run_config.personas[account.id]
        if account.persona:
            return account.persona
        defaults = [
            "curious beta tester who asks practical questions",
            "calm power user focused on bugs and UX details",
            "friendly tester who compares behavior across devices",
            "technical tester who notices edge cases",
        ]
        idx = hashlib.sha256(account.id.encode("utf-8")).digest()[0] % len(defaults)
        return defaults[idx]

    def _display_name(self, account: AccountRecord) -> str:
        if account.username:
            return f"@{account.username.lstrip('@')}"
        name = " ".join(part for part in (account.first_name, account.last_name) if part)
        if name:
            return name
        return "participant"

    def _mention_token(self, account: AccountRecord) -> str:
        if account.username:
            return f"@{account.username.lstrip('@')}"
        name = " ".join(part for part in (account.first_name, account.last_name) if part) or "participant"
        return name

    def _participant_roster(self, accounts: list[AccountRecord]) -> str:
        names = [self._mention_token(account) for account in accounts]
        return ", ".join(names) if names else "participants"

    def _recent_transcript(self, history: list[dict[str, str]]) -> str:
        if not history:
            return "No previous messages."
        return "\n".join(
            f"Previous message from {item.get('speaker', 'participant')}: {item.get('content', '')}"
            for item in history
        )

    def _clean_message(
        self,
        text: str,
        account: AccountRecord | None = None,
        participants: list[AccountRecord] | None = None,
    ) -> str:
        text = text.strip().strip('"')
        for prefix in self._forbidden_prefixes(account, participants or []):
            text = re.sub(rf"^\s*{re.escape(prefix)}\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
        if len(text) > 450:
            text = text[:447].rstrip() + "..."
        return text

    def _forbidden_prefixes(
        self,
        account: AccountRecord | None,
        participants: list[AccountRecord],
    ) -> list[str]:
        candidates: list[str] = []
        for item in ([account] if account else []) + participants:
            if not item:
                continue
            candidates.extend(
                value
                for value in (
                    self._display_name(item),
                    item.label,
                    item.id,
                    item.username,
                    " ".join(part for part in (item.first_name, item.last_name) if part),
                )
                if value
            )
        return sorted(set(candidates), key=len, reverse=True)
