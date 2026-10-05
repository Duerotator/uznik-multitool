"""TLS mailbox pool and read-only IMAP/POP3 polling for fresh Telegram codes."""
from __future__ import annotations

import asyncio
import imaplib
import json
import logging
import poplib
import re
import ssl
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from core.storage import update_json

log = logging.getLogger("email-inbox")
CODE_RE = re.compile(r"\b(\d{4,8})\b")


class MailboxError(RuntimeError):
    """A safe error message that never contains server responses/passwords."""


@dataclass(frozen=True)
class Mailbox:
    email: str
    password: str = field(repr=False)
    host: str = ""
    port: int = 993
    login: str = ""
    folder: str = "INBOX"


def load_mailboxes(path: Path, protocol: str, *, host: str = "", port: int = 0,
                   folder: str = "INBOX") -> list[Mailbox]:
    if protocol not in {"imap", "pop3"}:
        raise MailboxError("Mailbox protocol must be imap or pop3.")
    boxes: dict[str, Mailbox] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise MailboxError("Cannot read mailbox list; select a UTF-8 file in Security.") from None
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if line.startswith("{"):
                data = json.loads(line)
            else:
                parts = line.split(";")
                address, password = parts[0].split(":", 1)
                server = parts[1].strip() if len(parts) > 1 else ""
                server_port = parts[2].strip() if len(parts) > 2 else ""
                # Legacy supplier format: email:password@imap.host:993.
                if len(parts) == 1 and re.search(r"@(?:imap|pop3?)\.[A-Za-z0-9.-]+(?::\d+)?$", password, re.I):
                    password, _, server = password.rpartition("@")
                if len(parts) > 3:
                    raise ValueError
                if ":" in server and not server_port:
                    server, server_port = server.rsplit(":", 1)
                data = {"email": address, "password": password, "host": server, "port": server_port}
            if not isinstance(data["email"], str) or not isinstance(data["password"], str):
                raise ValueError
            address = data["email"].strip().lower()
            server = str(data.get("host") or host).strip().lower()
            if "@" not in address and server in {"imap.rambler.ru", "pop.rambler.ru", "pop3.rambler.ru", "imap.mail.ru", "pop.mail.ru"}:
                address += "@" + server.split(".", 1)[1]
            if not re.fullmatch(r"[^\s:@]+@[^\s:@]+\.[^\s:@]+", address):
                raise ValueError
            domain = address.rsplit("@", 1)[1]
            provider = {"rambler.ru": "rambler.ru", "lenta.ru": "rambler.ru", "ro.ru": "rambler.ru",
                        "mail.ru": "mail.ru"}.get(domain)
            if not server and provider:
                server = ("imap." if protocol == "imap" else "pop.") + provider
            if not server:
                raise MailboxError(
                    f"Mailbox line {number}: mail server is not specified for this provider. "
                    "Set EMAIL_MAILBOX_HOST in .env, or use email:password;host;port in the selected list. "
                    "Entry contents are hidden."
                )
            if not re.fullmatch(r"[A-Za-z0-9.-]+", server) or not data["password"]:
                raise ValueError
            if (protocol == "pop3" and server.startswith("imap.")) or (protocol == "imap" and server.startswith(("pop.", "pop3."))):
                raise ValueError
            server_port = int(data.get("port") or port or (993 if protocol == "imap" else 995))
            if not 1 <= server_port <= 65535:
                raise ValueError
            login = str(data.get("login") or address)
            mailbox_folder = str(data.get("folder") or folder)
            password = str(data["password"])
            if any("\r" in value or "\n" in value for value in (login, password, mailbox_folder)):
                raise ValueError
            box = Mailbox(address, password, server, server_port, login, mailbox_folder)
            if address in boxes and boxes[address] != box:
                raise ValueError
            boxes[address] = box
        except (ValueError, KeyError, TypeError, AttributeError):
            raise MailboxError(f"Invalid mailbox entry on line {number}. Use email:password;host;port, or JSON. "
                               "Check that the host matches IMAP/POP3. Entry contents are hidden.") from None
    if not boxes:
        raise MailboxError("Mailbox list is empty. Add your mailboxes before starting.")
    return list(boxes.values())


class MailboxPool:
    def __init__(self, boxes: list[Mailbox], state_file: Path, owners: dict[str, set[str]] | None = None):
        self.boxes = {box.email: box for box in boxes}
        self.state_file = state_file
        self.owners = owners or {}

    @staticmethod
    def _validate_state(state):
        bindings = state.get("bindings") if isinstance(state, dict) else None
        if (not isinstance(bindings, dict)
                or any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
                       for key, value in bindings.items())
                or len(set(bindings.values())) != len(bindings)):
            raise MailboxError("Mailbox bindings are invalid; restore data/email_mailbox_bindings.json from backup.")

    def reserve(self, account_id: str) -> Mailbox:
        # Fail closed on malformed state; do not lose pending reservations.
        if self.state_file.exists():
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8-sig"))
                self._validate_state(state)
            except (ValueError, UnicodeError, OSError):
                raise MailboxError("Mailbox bindings are unreadable; restore data/email_mailbox_bindings.json from backup.") from None

        def assign(state):
            self._validate_state(state)
            bindings = state["bindings"]
            address = bindings.get(account_id)
            if address:
                if address not in self.boxes or self.owners.get(address, set()) - {account_id}:
                    raise MailboxError("Reserved mailbox is missing or belongs to another account; check the list/bindings.")
            else:
                used = set(bindings.values())
                address = next((email for email in self.boxes if email not in used
                                and not self.owners.get(email, set()) - {account_id}), None)
                if not address:
                    raise MailboxError("No free mailboxes left in the selected list.")
                bindings[account_id] = address
            return state

        state = update_json(self.state_file, {"bindings": {}}, assign)
        return self.boxes[state["bindings"][account_id]]


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def telegram_code(raw: bytes, email: str, after: int, length: int | None) -> str | None:
    message = message_from_bytes(raw, policy=policy.default)
    senders = getaddresses(message.get_all("From", []))
    if not any(address.rsplit("@", 1)[-1].lower() == "telegram.org" for _, address in senders):
        return None
    recipients = getaddresses(message.get_all("To", []) + message.get_all("Delivered-To", []))
    if recipients and email.lower() not in {address.lower() for _, address in recipients}:
        return None
    if message.get("Date"):
        try:
            stamp = parsedate_to_datetime(str(message["Date"]))
            if stamp.tzinfo is None or stamp.timestamp() < after - 60:
                return None
        except (ValueError, TypeError, OverflowError):
            return None
    texts = [str(message.get("Subject", ""))]
    for part in message.walk():
        if part.get_content_type() not in {"text/plain", "text/html"} or part.get_content_disposition() == "attachment":
            continue
        try:
            text = part.get_content()
        except (LookupError, UnicodeError):
            continue
        if part.get_content_type() == "text/html":
            parser = _HTMLText()
            parser.feed(text)
            text = "\n".join(parser.parts)
        texts.append(text)
    for text in texts:
        codes = list(dict.fromkeys(code for code in CODE_RE.findall(text) if not length or len(code) == length))
        if len(codes) == 1:
            return codes[0]
    return None


class MailboxInboxClient:
    def __init__(self, pool: MailboxPool, protocol: str, *, timeout: float = 90,
                 socket_timeout: float = 15, poll_interval: float = 4):
        if protocol not in {"imap", "pop3"}:
            raise MailboxError("Mailbox protocol must be imap or pop3.")
        if socket_timeout <= 0 or poll_interval <= 0 or timeout <= 0:
            raise MailboxError("Mail timeouts/poll interval must be positive.")
        self.pool, self.protocol = pool, protocol
        self.timeout, self.socket_timeout, self.poll_interval = timeout, socket_timeout, poll_interval
        self.snapshots: dict[str, tuple | frozenset] = {}

    def address_for(self, account_id: str) -> str:
        return self.pool.reserve(account_id).email

    async def healthcheck(self) -> None:
        # Parse/validate pool locally; each assigned mailbox is authenticated
        # in prepare(), before any Telegram email change is requested.
        return None

    def _call(self, connection, deadline, operation, *args, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Mailbox operation timed out")
        connection.sock.settimeout(min(self.socket_timeout, remaining))
        return operation(*args, **kwargs)

    @contextmanager
    def _connection(self, box: Mailbox, deadline: float):
        connection = None
        try:
            timeout = min(self.socket_timeout, max(0.1, deadline - time.monotonic()))
            cls = imaplib.IMAP4_SSL if self.protocol == "imap" else poplib.POP3_SSL
            connection = cls(box.host, box.port, timeout=timeout, **{
                "ssl_context" if self.protocol == "imap" else "context": ssl.create_default_context()})
            try:
                if self.protocol == "imap":
                    self._call(connection, deadline, connection.login, box.login, box.password)
                else:
                    self._call(connection, deadline, connection.user, box.login)
                    self._call(connection, deadline, connection.pass_, box.password)
            except imaplib.IMAP4.abort:
                raise OSError("IMAP connection lost") from None
            except (imaplib.IMAP4.error, poplib.error_proto):
                raise MailboxError(f"{self.protocol.upper()} authentication failed. Check mail access and the app password.") from None
            if self.protocol == "imap":
                status, _ = self._call(connection, deadline, connection.select, box.folder, readonly=True)
                if status != "OK":
                    raise MailboxError("Cannot open the configured IMAP folder.")
            yield connection
        except ssl.SSLCertVerificationError:
            raise MailboxError("Mailbox TLS certificate verification failed.") from None
        except imaplib.IMAP4.abort:
            raise OSError("IMAP connection lost") from None
        except (imaplib.IMAP4.error, poplib.error_proto):
            raise MailboxError("Mailbox protocol error; check server settings and UID/UIDL support.") from None
        finally:
            if connection is not None:
                try:
                    connection.sock.settimeout(1)
                    connection.logout() if self.protocol == "imap" else connection.quit()
                except Exception:
                    try:
                        connection.shutdown() if self.protocol == "imap" else connection.close()
                    except Exception:
                        pass

    def _snapshot(self, box: Mailbox, deadline: float):
        with self._connection(box, deadline) as connection:
            if self.protocol == "pop3":
                _, lines, _ = self._call(connection, deadline, connection.uidl)
                return frozenset(line.split()[1] for line in lines)
            _, validity = connection.response("UIDVALIDITY")
            _, next_uid = connection.response("UIDNEXT")
            if not validity or not next_uid or not validity[0] or not next_uid[0]:
                raise MailboxError("IMAP server did not provide UIDVALIDITY/UIDNEXT; cannot safely exclude old codes.")
            return (int(validity[0]), int(next_uid[0]) - 1)

    async def prepare(self, email: str) -> None:
        box = self.pool.boxes[email]
        budget = self.socket_timeout * 3
        log.info("Checking %s mailbox at %s:%s (up to %.0fs)", self.protocol.upper(), box.host, box.port, budget)
        try:
            snapshot = await asyncio.wait_for(asyncio.to_thread(
                self._snapshot, box, time.monotonic() + budget), timeout=budget)
        except (TimeoutError, OSError):
            raise MailboxError(
                f"{self.protocol.upper()} mailbox connection failed or timed out. "
                "Check EMAIL_MAILBOX_HOST/PORT, network access and mail client permissions."
            ) from None
        self.snapshots[email] = snapshot
        log.info("%s mailbox authenticated; fresh-message snapshot ready", self.protocol.upper())

    def _poll(self, box: Mailbox, after: int, length: int | None, deadline: float) -> str | None:
        baseline = self.snapshots[box.email]
        with self._connection(box, deadline) as connection:
            if self.protocol == "pop3":
                _, lines, _ = self._call(connection, deadline, connection.uidl)
                fresh = [(int(line.split()[0]), line.split()[1]) for line in lines if line.split()[1] not in baseline]
                for number, _uid in sorted(fresh, reverse=True)[:25]:
                    size_response = self._call(connection, deadline, connection.list, number)
                    if int(size_response.split()[-1]) > 512_000:
                        continue
                    _, content, _ = self._call(connection, deadline, connection.retr, number)
                    raw = b"\r\n".join(content)
                    if len(raw) > 512_000:
                        continue
                    code = telegram_code(raw, box.email, after, length)
                    if code:
                        return code
            else:
                _, validity = connection.response("UIDVALIDITY")
                if not validity or int(validity[0]) != baseline[0]:
                    raise MailboxError("Mailbox UIDVALIDITY changed; restart the operation before requesting another code.")
                status, data = self._call(connection, deadline, connection.uid, "search", None, f"UID {baseline[1] + 1}:*")
                if status != "OK":
                    raise MailboxError("IMAP UID search failed.")
                uids = sorted((int(uid) for uid in (data[0] or b"").split() if int(uid) > baseline[1]), reverse=True)
                for uid in uids[:25]:
                    status, parts = self._call(connection, deadline, connection.uid, "fetch", str(uid), "(BODY.PEEK[]<0.512000>)")
                    if status != "OK":
                        continue
                    for part in parts:
                        if isinstance(part, tuple) and isinstance(part[1], bytes):
                            code = telegram_code(part[1], box.email, after, length)
                            if code:
                                return code
        return None

    async def wait_code(self, email: str, after: int, length: int | None = None) -> str:
        if email not in self.snapshots:
            raise MailboxError("Mailbox must be prepared before requesting the Telegram code.")
        deadline = time.monotonic() + self.timeout
        attempts = 0
        log.info("Waiting for a fresh %s email code (up to %.0fs)", self.protocol.upper(), self.timeout)
        while time.monotonic() < deadline:
            attempts += 1
            try:
                code = await asyncio.wait_for(
                    asyncio.to_thread(self._poll, self.pool.boxes[email], after, length, deadline),
                    timeout=max(0.01, deadline - time.monotonic()),
                )
                if code:
                    return code
            except OSError as exc:
                log.warning("%s mail connection failed (%s); retrying", self.protocol.upper(), type(exc).__name__)
            if attempts == 1 or attempts % 5 == 0:
                log.info("Waiting for a fresh %s email code (%ss remaining)", self.protocol.upper(), max(0, int(deadline - time.monotonic())))
            await asyncio.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        raise TimeoutError(f"No email code received within {int(self.timeout)} seconds.")
