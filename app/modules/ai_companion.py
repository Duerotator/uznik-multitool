from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.telegram_client import create_client
from modules.accounts import AccountService
from utils.rate_limit import human_delay
from utils.telegram_errors import is_invalid_auth_error, short_error


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
        self.provider = run_config.provider.lower()
        self.api_key = run_config.api_key or app_config.llm_api_key
        self.base_url = (run_config.base_url or app_config.llm_base_url).rstrip("/")

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
        httpx = self._httpx()
        payload = {
            "model": self.run_config.model,
            "messages": messages,
            "temperature": self.run_config.temperature,
            "max_tokens": 180,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                )
            except httpx.ConnectError as exc:
                raise RuntimeError(
                    f"Cannot connect to LLM endpoint {self.base_url}. "
                    "Check internet/VPN/DNS and LLM_BASE_URL."
                ) from exc
            self._raise_for_status(response, "OpenAI-compatible")
            data = response.json()
        return data["choices"][0]["message"]["content"].strip()

    async def _anthropic(self, messages: list[dict[str, str]]) -> str:
        httpx = self._httpx()
        system = "\n".join(item["content"] for item in messages if item["role"] == "system")
        user_messages = [
            {"role": "assistant" if item["role"] == "assistant" else "user", "content": item["content"]}
            for item in messages
            if item["role"] != "system"
        ]
        payload = {
            "model": self.run_config.model,
            "max_tokens": 180,
            "temperature": self.run_config.temperature,
            "system": system,
            "messages": user_messages or [{"role": "user", "content": "Start."}],
        }
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
            except httpx.ConnectError as exc:
                raise RuntimeError(
                    "Cannot connect to Anthropic endpoint. Check internet/VPN/DNS."
                ) from exc
            self._raise_for_status(response, "Anthropic")
            data = response.json()
        return "".join(part.get("text", "") for part in data.get("content", [])).strip()

    async def _gemini(self, messages: list[dict[str, str]]) -> str:
        httpx = self._httpx()
        text = "\n".join(f"{item['role']}: {item['content']}" for item in messages)
        payload = {"contents": [{"parts": [{"text": text}]}]}
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.run_config.model}:generateContent?key={self.api_key}"
        )
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                response = await client.post(url, json=payload)
            except httpx.ConnectError as exc:
                raise RuntimeError(
                    "Cannot connect to Google Gemini endpoint. Check internet/VPN/DNS."
                ) from exc
            self._raise_for_status(response, "Google Gemini")
            data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ""
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(part.get("text", "") for part in parts).strip()

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

        enabled_accounts = [account for account in accounts if account.enabled]
        if len(enabled_accounts) < 2:
            raise RuntimeError("AI companion needs at least two enabled accounts.")

        llm = LLMClient(self.config, run_config)
        history: list[dict[str, str]] = []
        stop_event = stop_event or asyncio.Event()
        result = ActionResult()
        clients: dict[str, Any] = {}

        async def account_client(account: AccountRecord):
            client = clients.get(account.id)
            if client is not None:
                return client
            client = create_client(self.config, account)
            await client.__aenter__()
            clients[account.id] = client
            return client

        try:
            for index in range(run_config.message_count):
                if stop_event.is_set():
                    break
                account = enabled_accounts[index % len(enabled_accounts)]
                persona = self._persona(account, run_config)
                text = await self._next_message(
                    llm, run_config, history, persona, account, enabled_accounts, index
                )
                if not text:
                    continue
                await human_delay(run_config.min_delay, run_config.max_delay)
                try:
                    client = await account_client(account)
                    await client.send_message(run_config.chat, text)
                    history.append({"speaker": self._display_name(account), "content": text})
                    history = history[-30:]
                    self.log.info("AI message sent by %s", account.id)
                    result.add_ok(account.id)
                except Exception as exc:
                    client = clients.pop(account.id, None)
                    if client is not None:
                        try:
                            await client.__aexit__(None, None, None)
                        except Exception:
                            pass
                    error = short_error(exc)
                    result.add_error(account.id, error)
                    disable = is_invalid_auth_error(exc)
                    self.accounts.mark_error(account.id, error, disable=disable)
                    if disable:
                        self.log.error("Disabled invalid session %s: %s", account.id, error)
                    else:
                        self.log.error("AI message failed for %s: %s", account.id, error)
        finally:
            for client in reversed(list(clients.values())):
                try:
                    await client.__aexit__(None, None, None)
                except Exception as exc:
                    self.log.debug("AI client cleanup failed: %s", exc)
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
        return defaults[hash(account.id) % len(defaults)]

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
        if account.user_id:
            return f"[{name}](tg://user?id={account.user_id})"
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
