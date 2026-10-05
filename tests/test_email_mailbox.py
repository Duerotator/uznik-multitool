"""Mailbox and account-flow regression tests: no network or real sessions."""
from __future__ import annotations

import asyncio
import imaplib
import json
import logging
import poplib
import ssl
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from modules.email_inbox import EmailInboxClient, create_email_inbox
from modules.email_mailbox import Mailbox, MailboxError, MailboxInboxClient, MailboxPool, load_mailboxes, telegram_code


ADDRESS = "reader@mail.ru"


def letter(code="123456", *, sender="Telegram <noreply@telegram.org>", recipient=ADDRESS,
           stamp=None, html=False, subject="Telegram email verification"):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Date"] = format_datetime(datetime.fromtimestamp(stamp or time.time(), timezone.utc))
    message.set_content(f"<p>Ваш код: <b>{code}</b></p><style>999999</style>" if html else f"Ваш код: {code}",
                        subtype="html" if html else "plain")
    return message.as_bytes()


class MailboxParsingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "accounts.txt"

    def parse(self, source, protocol="imap", **kwargs):
        self.path.write_text(source, encoding="utf-8-sig")
        return load_mailboxes(self.path, protocol, **kwargs)

    def test_list_formats_and_password_redaction(self):
        boxes = self.parse('# comment\n\nUSER@RAMBLER.RU:p:a@ss@imap.rambler.ru:993\n'
                           'reader@mail.ru:app-password;imap.mail.ru;993\n'
                           '{"email":"owner@example.org","password":"p;:@ss","host":"mail.example.org",'
                           '"login":"separate-login","port":1993,"folder":"Codes"}\n')
        self.assertEqual(3, len(boxes))
        self.assertEqual(("user@rambler.ru", "p:a@ss", "imap.rambler.ru", 993),
                         (boxes[0].email, boxes[0].password, boxes[0].host, boxes[0].port))
        self.assertEqual(("separate-login", "Codes", 1993), (boxes[2].login, boxes[2].folder, boxes[2].port))
        self.assertNotIn("app-password", repr(boxes[1]))

    def test_pop3_defaults_and_supplier_format(self):
        boxes = self.parse("reader@mail.ru:secret\nuser:secret@pop3.rambler.ru\n", "pop3")
        self.assertEqual(("pop.mail.ru", 995), (boxes[0].host, boxes[0].port))
        self.assertEqual("user@rambler.ru", boxes[1].email)
        self.assertEqual("pop3.rambler.ru", boxes[1].host)

    def test_custom_server_can_be_configured_globally(self):
        box = self.parse("owner@firstmail.example:secret", host="mail.example.org", port=1993)[0]
        self.assertEqual(("mail.example.org", 1993), (box.host, box.port))

    def test_missing_custom_server_names_line_and_setting_without_credentials(self):
        with self.assertRaises(MailboxError) as caught:
            self.parse("# mailboxes\nowner@unknown.org:private-password")
        self.assertIn("line 2", str(caught.exception))
        self.assertIn("EMAIL_MAILBOX_HOST", str(caught.exception))
        self.assertNotIn("private-password", str(caught.exception))
        self.assertNotIn("owner@", str(caught.exception))

    def test_duplicate_identical_entries_are_deduplicated(self):
        self.assertEqual(1, len(self.parse("reader@mail.ru:secret\nreader@mail.ru:secret")))

    def test_invalid_entries_never_echo_password(self):
        cases = ("reader@mail.ru:sensitive;imap.mail.ru;70000", "reader@mail.ru:sensitive;pop.mail.ru;995",
                 "owner@unknown.org:sensitive", "reader@mail.ru:sensitive;imap.mail.ru;993;extra",
                 '{"email":"reader@mail.ru","password":null}',
                 "reader@mail.ru:sensitive\nreader@mail.ru:another-sensitive")
        for source in cases:
            with self.subTest(source=source), self.assertRaises(MailboxError) as caught:
                self.parse(source)
            self.assertNotIn("sensitive", str(caught.exception))
        with self.assertRaisesRegex(MailboxError, "empty"):
            self.parse("# nothing\n")
        with self.assertRaisesRegex(MailboxError, "host matches"):
            self.parse("reader@mail.ru:secret@imap.mail.ru", "pop3")


class MailboxPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "bindings.json"
        self.boxes = [Mailbox(f"u{i}@mail.ru", "private-password", "imap.mail.ru", login=f"u{i}@mail.ru") for i in range(16)]

    def test_concurrent_reservations_across_instances_are_unique_and_persist(self):
        def reserve(i):
            return MailboxPool(self.boxes, self.path).reserve(str(i)).email
        with ThreadPoolExecutor(max_workers=8) as executor:
            addresses = list(executor.map(reserve, range(16)))
        self.assertEqual(16, len(set(addresses)))
        self.assertEqual(addresses[4], MailboxPool(self.boxes, self.path).reserve("4").email)
        self.assertNotIn("private-password", self.path.read_text())
        with self.assertRaisesRegex(MailboxError, "No free"):
            reserve(17)

    def test_existing_account_metadata_prevents_reusing_mailbox(self):
        pool = MailboxPool(self.boxes, self.path, {self.boxes[0].email: {"other"}})
        self.assertEqual(self.boxes[1].email, pool.reserve("new").email)
        self.assertEqual(self.boxes[0].email, pool.reserve("other").email)

    def test_missing_or_conflicting_reservation_fails_closed(self):
        first = MailboxPool(self.boxes, self.path).reserve("a")
        with self.assertRaisesRegex(MailboxError, "Reserved mailbox"):
            MailboxPool(self.boxes[1:], self.path).reserve("a")
        with self.assertRaisesRegex(MailboxError, "Reserved mailbox"):
            MailboxPool(self.boxes, self.path, {first.email: {"another"}}).reserve("a")

    def test_corrupt_or_duplicate_bindings_are_not_overwritten(self):
        for content in ('{broken', '[1]', '{"bindings":{"a":[]}}',
                        '{"bindings":{"a":"u0@mail.ru","b":"u0@mail.ru"}}'):
            self.path.write_text(content)
            with self.subTest(content=content), self.assertRaises(MailboxError):
                MailboxPool(self.boxes, self.path).reserve("new")
            self.assertEqual(content, self.path.read_text())


class MailMessageTests(unittest.TestCase):
    def test_plain_html_and_length(self):
        for html in (False, True):
            raw = letter(html=html)
            self.assertEqual("123456", telegram_code(raw, ADDRESS, int(time.time()), 6))
            self.assertIsNone(telegram_code(raw, ADDRESS, int(time.time()), 5))

    def test_old_or_wrong_sender_or_recipient_is_rejected(self):
        now = int(time.time())
        for raw in (letter(stamp=now - 120), letter(sender="noreply@not-telegram.org"),
                    letter(sender="telegram.org@attacker.example"), letter(recipient="someone@mail.ru")):
            self.assertIsNone(telegram_code(raw, ADDRESS, now, 6))

    def test_ambiguous_code_rejected_but_subject_preferred(self):
        self.assertIsNone(telegram_code(letter(code="123456 or 999999"), ADDRESS, 0, 6))
        self.assertEqual("234567", telegram_code(letter(code="123456 or 999999", subject="Code: 234567"), ADDRESS, 0, 6))


class FakeIMAP:
    def __init__(self, *, next_uid=101, validity=7, messages=None, error=None):
        self.sock = Mock()
        self.login = Mock(side_effect=error, return_value=("OK", []))
        self.select = Mock(return_value=("OK", []))
        self.logout = Mock()
        self.shutdown = Mock()
        self.uid = Mock(side_effect=self._uid)
        self.next_uid, self.validity, self.messages = next_uid, validity, messages or {}

    def response(self, key):
        return key, [str(self.next_uid if key == "UIDNEXT" else self.validity).encode()]

    def _uid(self, command, *args):
        if command == "search":
            return "OK", [b" ".join(str(uid).encode() for uid in self.messages)]
        return "OK", [(b"BODY", self.messages[int(args[0])]), b")"]


class FakePOP:
    def __init__(self, messages=None, error=None, sizes=None):
        self.messages = messages or {}
        self.sizes = sizes or {}
        self.sock = Mock()
        self.user = Mock()
        self.pass_ = Mock(side_effect=error)
        self.quit = Mock()
        self.close = Mock()
        self.dele = Mock()
        self.uidl = Mock(side_effect=lambda: (b"+OK", [f"{n} uid-{n}".encode() for n in self.messages], 0))
        self.list = Mock(side_effect=lambda n: f"+OK {n} {self.sizes.get(n, len(self.messages[n]))}".encode())
        self.retr = Mock(side_effect=lambda n: (b"+OK", self.messages[n].splitlines(), len(self.messages[n])))


class MailboxProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def client(self, protocol="imap", **kwargs):
        box = Mailbox(ADDRESS, "never-print-this", "imap.mail.ru" if protocol == "imap" else "pop.mail.ru",
                      993 if protocol == "imap" else 995, ADDRESS)
        return MailboxInboxClient(MailboxPool([box], Path(self.temp.name) / "bindings.json"), protocol,
                                  timeout=0.1, poll_interval=0.01, **kwargs)

    async def test_imap_snapshot_before_request_and_read_only_fetch(self):
        inbox = self.client()
        snapshot = FakeIMAP()
        poll = FakeIMAP(messages={100: letter("111111"), 101: letter()})
        with patch("modules.email_mailbox.imaplib.IMAP4_SSL", side_effect=[snapshot, poll]) as connect:
            await inbox.prepare(ADDRESS)
            self.assertEqual("123456", await inbox.wait_code(ADDRESS, int(time.time()), 6))
        self.assertEqual((7, 100), inbox.snapshots[ADDRESS])
        snapshot.select.assert_called_once_with("INBOX", readonly=True)
        poll.select.assert_called_once_with("INBOX", readonly=True)
        self.assertIn(("fetch", "101", "(BODY.PEEK[]<0.512000>)"), [call.args for call in poll.uid.call_args_list])
        self.assertFalse(any(call.args[:2] == ("fetch", "100") for call in poll.uid.call_args_list))
        self.assertEqual(ssl.CERT_REQUIRED, connect.call_args.kwargs["ssl_context"].verify_mode)
        snapshot.logout.assert_called_once()
        poll.logout.assert_called_once()

    async def test_imap_no_new_uid_never_accepts_old_code(self):
        inbox = self.client()
        inbox.snapshots[ADDRESS] = (7, 100)
        poll = FakeIMAP(messages={100: letter()})
        with patch("modules.email_mailbox.imaplib.IMAP4_SSL", return_value=poll):
            self.assertIsNone(inbox._poll(inbox.pool.boxes[ADDRESS], 0, 6, time.monotonic() + 10))
        self.assertFalse(any(call.args[0] == "fetch" for call in poll.uid.call_args_list))

    async def test_uidvalidity_change_fails_without_fetch(self):
        inbox = self.client()
        inbox.snapshots[ADDRESS] = (7, 100)
        poll = FakeIMAP(validity=8)
        with patch("modules.email_mailbox.imaplib.IMAP4_SSL", return_value=poll):
            with self.assertRaisesRegex(MailboxError, "UIDVALIDITY changed"):
                await inbox.wait_code(ADDRESS, 0, 6)
        poll.uid.assert_not_called()
        poll.logout.assert_called_once()

    async def test_pop3_reads_only_new_uidl_and_does_not_delete(self):
        inbox = self.client("pop3")
        snapshot = FakePOP({1: letter("111111")})
        poll = FakePOP({1: letter("111111"), 2: letter()})
        with patch("modules.email_mailbox.poplib.POP3_SSL", side_effect=[snapshot, poll]):
            await inbox.prepare(ADDRESS)
            self.assertEqual("123456", await inbox.wait_code(ADDRESS, 0, 6))
        poll.retr.assert_called_once_with(2)
        poll.dele.assert_not_called()
        poll.quit.assert_called_once()
        snapshot.quit.assert_called_once()

    async def test_pop3_oversize_message_is_not_downloaded(self):
        inbox = self.client("pop3")
        inbox.snapshots[ADDRESS] = frozenset()
        poll = FakePOP({1: letter()}, sizes={1: 600_000})
        with patch("modules.email_mailbox.poplib.POP3_SSL", return_value=poll):
            self.assertIsNone(inbox._poll(inbox.pool.boxes[ADDRESS], 0, 6, time.monotonic() + 10))
        poll.retr.assert_not_called()
        poll.dele.assert_not_called()

    async def test_auth_errors_are_sanitized_and_connection_is_closed(self):
        for protocol, connection, target in (
                ("imap", FakeIMAP(error=imaplib.IMAP4.error("never-print-this")), "imaplib.IMAP4_SSL"),
                ("pop3", FakePOP(error=poplib.error_proto("never-print-this")), "poplib.POP3_SSL")):
            with self.subTest(protocol=protocol), patch("modules.email_mailbox." + target, return_value=connection):
                with self.assertRaises(MailboxError) as caught:
                    await self.client(protocol).prepare(ADDRESS)
                self.assertNotIn("never-print-this", str(caught.exception))
                (connection.logout if protocol == "imap" else connection.quit).assert_called_once()

    async def test_transient_errors_retry_and_timeout_is_bounded(self):
        inbox = self.client()
        inbox.snapshots[ADDRESS] = (7, 100)
        with patch.object(inbox, "_poll", side_effect=[OSError("disconnected"), "123456"]) as poll:
            self.assertEqual("123456", await inbox.wait_code(ADDRESS, 0, 6))
            self.assertEqual(2, poll.call_count)
        started = time.monotonic()
        with patch.object(inbox, "_poll", return_value=None), self.assertRaisesRegex(TimeoutError, "No email code received"):
            await inbox.wait_code(ADDRESS, 0, 6)
        self.assertLess(time.monotonic() - started, 1)

    async def test_missing_snapshot_rejected_and_wait_is_cancellable(self):
        inbox = self.client()
        with self.assertRaisesRegex(MailboxError, "prepared"):
            await inbox.wait_code(ADDRESS, 0, 6)
        inbox.snapshots[ADDRESS] = (7, 100)
        with patch.object(inbox, "_poll", return_value=None):
            task = asyncio.create_task(inbox.wait_code(ADDRESS, 0, 6))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_prepare_bounds_a_blocked_worker_and_keeps_snapshot_unset(self):
        inbox = self.client(socket_timeout=0.01)
        def blocked(*args):
            time.sleep(0.12)
            return (7, 100)
        with patch.object(inbox, "_snapshot", side_effect=blocked):
            started = time.monotonic()
            with self.assertRaisesRegex(MailboxError, "connection failed or timed out"):
                await inbox.prepare(ADDRESS)
            self.assertLess(time.monotonic() - started, 0.1)
            await asyncio.sleep(0.14)
        self.assertNotIn(ADDRESS, inbox.snapshots)

    async def test_poll_wait_is_bounded_even_if_socket_worker_is_blocked(self):
        inbox = self.client()
        inbox.snapshots[ADDRESS] = (7, 100)
        def blocked(*args):
            time.sleep(0.2)
            return "123456"
        with patch.object(inbox, "_poll", side_effect=blocked):
            started = time.monotonic()
            with self.assertRaisesRegex(TimeoutError, "No email code received"):
                await inbox.wait_code(ADDRESS, 0, 6)
            self.assertLess(time.monotonic() - started, 0.18)
            await asyncio.sleep(0.15)


class EmailFactoryTests(unittest.TestCase):
    def test_http_backend_keeps_existing_api_contract(self):
        config = SimpleNamespace(email_inbox_backend="http")
        inbox = create_email_inbox(config, domain="example.org", api_url="https://inbox.example.org/", token="secret", timeout=90)
        self.assertIsInstance(inbox, EmailInboxClient)
        self.assertTrue(inbox.address_for("account").endswith("@example.org"))
        self.assertEqual({"Authorization": "Bearer secret"}, inbox._headers())
        self.assertEqual("https://inbox.example.org", inbox.base_url)

    def test_imap_pop3_do_not_require_http_domain_or_api(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "accounts.txt"
            path.write_text("reader@mail.ru:secret")
            for protocol in ("imap", "pop3"):
                config = SimpleNamespace(email_inbox_backend=protocol, email_mailboxes_file=path,
                                         email_mailbox_host="", email_mailbox_port=0, email_mailbox_folder="INBOX",
                                         email_mailbox_socket_timeout=15, email_mailbox_poll_interval=4, data_dir=Path(temp))
                inbox = create_email_inbox(config, domain="", api_url="", token="", timeout=90)
                self.assertEqual(protocol, inbox.protocol)
                self.assertEqual(ADDRESS, inbox.address_for("a"))


class EmailSecurityFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_stall_times_out_and_marks_account_error_for_both_actions(self):
        from modules.account_security import AccountSecurityService
        for method, telegram_method in (("change_login_emails", "set_login_email"),
                                        ("bind_recovery_emails", "set_recovery_email")):
            with self.subTest(method=method):
                inbox = Mock(healthcheck=AsyncMock(), prepare=AsyncMock())
                inbox.address_for.return_value = ADDRESS
                async def hang(**kwargs):
                    await asyncio.Event().wait()
                native = SimpleNamespace(**{telegram_method: hang})
                closed = Mock()
                @asynccontextmanager
                async def create(*args):
                    try:
                        yield native
                    finally:
                        closed()
                service = AccountSecurityService.__new__(AccountSecurityService)
                service.config = SimpleNamespace(email_inbox_backend="imap", max_concurrency=1, min_action_delay=0, max_action_delay=0)
                service.accounts, service.log = Mock(), logging.getLogger("security-test")
                service._email_inbox = Mock(return_value=inbox)
                progress = Mock()
                with patch("modules.account_security.create_client", create), patch("modules.account_security.human_delay", AsyncMock()), patch("modules.account_security.EMAIL_OPERATION_OVERHEAD", 0):
                    result = await getattr(service, method)([SimpleNamespace(id="a", enabled=True)], "", "", "", code_timeout=0.02, progress=progress)
                self.assertIn("Email operation timed out", result.details["a"])
                progress.mark_error.assert_called_once_with("a")
                service.accounts.update_profile_metadata.assert_not_called()
                closed.assert_called_once()

    async def test_mailbox_timeout_is_terminal_not_retried(self):
        from modules.account_security import _with_network_retries
        operation = AsyncMock(side_effect=MailboxError("IMAP connection failed or timed out"))
        with self.assertRaises(MailboxError):
            await _with_network_retries(operation, base_delay=0)
        operation.assert_awaited_once()

    async def test_mailbox_prepared_before_both_telegram_requests_and_metadata_saved(self):
        from modules.account_security import AccountSecurityService
        for method, telegram_method, metadata_key in (("change_login_emails", "set_login_email", "login_email"),
                                                      ("bind_recovery_emails", "set_recovery_email", "recovery_email")):
            events = []
            inbox = Mock()
            inbox.healthcheck = AsyncMock()
            inbox.address_for.return_value = ADDRESS
            inbox.prepare = AsyncMock(side_effect=lambda _: events.append("prepare"))
            inbox.wait_code = AsyncMock(return_value="123456")

            async def request(**kwargs):
                events.append("Telegram request")
                self.assertEqual("123456", await kwargs["code_provider"](kwargs["email"], 6))

            client = SimpleNamespace(**{telegram_method: request})

            @asynccontextmanager
            async def create(*_):
                yield client

            service = AccountSecurityService.__new__(AccountSecurityService)
            service.config = SimpleNamespace(email_inbox_backend="pop3", max_concurrency=1, min_action_delay=0, max_action_delay=0)
            service.accounts = Mock()
            service.log = logging.getLogger("security-test")
            service._email_inbox = Mock(return_value=inbox)
            progress = Mock()
            with patch("modules.account_security.create_client", create), patch("modules.account_security.human_delay", AsyncMock()):
                result = await getattr(service, method)([SimpleNamespace(id="a", enabled=True)], "", "", "", progress=progress)
            self.assertEqual(["prepare", "Telegram request"], events)
            self.assertEqual(ADDRESS, result.details["a"])
            self.assertEqual(ADDRESS, service.accounts.update_profile_metadata.call_args.args[1][metadata_key])
            progress.mark_ok.assert_called_once_with("a")

    async def test_mail_auth_failure_never_changes_telegram_and_progress_finishes(self):
        from modules.account_security import AccountSecurityService
        inbox = Mock(healthcheck=AsyncMock(), prepare=AsyncMock(side_effect=MailboxError("Authentication failed")))
        inbox.address_for.return_value = ADDRESS
        service = AccountSecurityService.__new__(AccountSecurityService)
        service.config = SimpleNamespace(email_inbox_backend="imap", max_concurrency=1, min_action_delay=0, max_action_delay=0)
        service.accounts, service.log = Mock(), logging.getLogger("security-test")
        service._email_inbox = Mock(return_value=inbox)
        progress = Mock()
        with patch("modules.account_security.create_client") as create, patch("modules.account_security.human_delay", AsyncMock()):
            await service.change_login_emails([SimpleNamespace(id="a", enabled=True)], "", "", "", progress=progress)
        create.assert_not_called()
        service.accounts.update_profile_metadata.assert_not_called()
        progress.mark_error.assert_called_once_with("a")


if __name__ == "__main__":
    unittest.main()
