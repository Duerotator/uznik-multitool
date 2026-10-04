from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass

import httpx


CODE_RE = re.compile(r"\b(\d{4,8})\b")
log = logging.getLogger("email-inbox")


@dataclass
class InboxCode:
    code: str
    received_at: int | None = None


class EmailInboxClient:
    def __init__(self, base_url: str, token: str, timeout: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def healthcheck(self) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                response = await client.get(f"{self.base_url}/health", headers=self._headers())
                response.raise_for_status()
                if not bool(response.json().get("ok")):
                    raise RuntimeError("Inbox API health check returned an invalid response.")
            except (httpx.HTTPError, ValueError) as exc:
                raise RuntimeError(f"Inbox API health check failed: {exc}") from exc

    async def wait_code(
        self,
        email: str,
        after: int,
        length: int | None = None,
    ) -> str:
        deadline = time.time() + self.timeout
        headers = self._headers()
        attempts = 0
        async with httpx.AsyncClient(timeout=20) as client:
            while time.time() < deadline:
                attempts += 1
                try:
                    response = await client.get(
                        f"{self.base_url}/code",
                        params={"to": email.lower(), "after": after, "length": length or ""},
                        headers=headers,
                    )
                except httpx.HTTPError as exc:
                    log.warning("Inbox API request failed for %s (attempt %d): %s", email, attempts, exc)
                    await asyncio.sleep(4)
                    continue
                if response.status_code == 200:
                    payload = response.json()
                    code = str(payload.get("code") or "").strip()
                    if code:
                        return code
                elif response.status_code not in {202, 404}:
                    raise RuntimeError(f"Inbox API failed: HTTP {response.status_code} {response.text[:200]}")
                elif attempts == 1 or attempts % 5 == 0:
                    remaining = max(0, int(deadline - time.time()))
                    log.info("Waiting for email code for %s (%ss remaining)", email, remaining)
                await asyncio.sleep(4)
        raise TimeoutError(f"No email code received for {email} within {int(self.timeout)} seconds.")


def generate_recovery_email(domain: str, account_id: str) -> str:
    safe_id = re.sub(r"[^a-zA-Z0-9]+", "", account_id)[-10:] or "acct"
    return f"tg-{safe_id.lower()}-{secrets.token_hex(3)}@{domain.strip().lower()}"
