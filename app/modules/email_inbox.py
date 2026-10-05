from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from pathlib import Path
from dataclasses import dataclass

import httpx


CODE_RE = re.compile(r"\b(\d{4,8})\b")
log = logging.getLogger("email-inbox")


@dataclass
class InboxCode:
    code: str
    received_at: int | None = None


class EmailInboxClient:
    def __init__(self, base_url: str, token: str, timeout: float = 180.0, *, domain: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.domain = domain

    def address_for(self, account_id: str) -> str:
        return generate_recovery_email(self.domain, account_id)

    async def prepare(self, email: str) -> None:
        pass  # HTTP mode generates a unique address for each operation.

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


def email_setup_description(config, *, domain: str | None = None, api_url: str | None = None,
                            validate_list: bool = False) -> str:
    backend = getattr(config, "email_inbox_backend", "http").strip().lower()
    if backend == "http":
        if not (domain if domain is not None else config.email_domain).strip() or not (api_url if api_url is not None else config.email_inbox_api_url).strip():
            raise RuntimeError("Set EMAIL_DOMAIN and EMAIL_INBOX_API_URL for HTTP API mode.")
        return "Generates new email addresses and receives codes through HTTP Inbox API."
    if backend not in {"imap", "pop3"}:
        raise RuntimeError("EMAIL_INBOX_BACKEND must be http, imap or pop3.")
    path = getattr(config, "email_mailboxes_file", None)
    if not path or not Path(path).is_file():
        raise RuntimeError("Select a mailbox list in Security, or set EMAIL_MAILBOXES_FILE.")
    if validate_list:
        from modules.email_mailbox import load_mailboxes
        load_mailboxes(Path(path), backend, host=config.email_mailbox_host,
                       port=config.email_mailbox_port, folder=config.email_mailbox_folder)
    return f"Assigns mailboxes from the selected list and reads fresh codes through {backend.upper()} over TLS."


def create_email_inbox(config, *, domain: str, api_url: str, token: str, timeout: float, owners=None):
    email_setup_description(config, domain=domain, api_url=api_url)
    backend = getattr(config, "email_inbox_backend", "http").strip().lower()
    if backend == "http":
        return EmailInboxClient(api_url, token, timeout, domain=domain)
    from modules.email_mailbox import MailboxInboxClient, MailboxPool, load_mailboxes
    boxes = load_mailboxes(Path(config.email_mailboxes_file), backend,
                           host=config.email_mailbox_host, port=config.email_mailbox_port,
                           folder=config.email_mailbox_folder)
    pool = MailboxPool(boxes, config.data_dir / "email_mailbox_bindings.json", owners)
    return MailboxInboxClient(pool, backend, timeout=timeout,
                              socket_timeout=config.email_mailbox_socket_timeout,
                              poll_interval=config.email_mailbox_poll_interval)
