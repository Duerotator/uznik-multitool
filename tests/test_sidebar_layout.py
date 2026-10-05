"""Button-row sizing and callbacks, without opening accounts or services."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from PySide6.QtWidgets import QApplication, QPushButton
from ui.qt_app import ButtonRow, QtDesktopApp


class ButtonRowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_buttons_reflow_and_return_to_one_row_without_losing_callbacks(self):
        buttons = [QPushButton(text) for text in ("View post", "Random reaction", "Choose reaction")]
        callback = Mock()
        buttons[1].clicked.connect(callback)
        row = ButtonRow(buttons)
        try:
            wide = sum(button.sizeHint().width() for button in buttons) + 32
            row.resize(wide, row.heightForWidth(wide))
            row.show()
            self.app.processEvents()
            self.assertEqual(3, row.columns)
            narrow = max(button.sizeHint().width() for button in buttons)
            row.resize(narrow, row.heightForWidth(narrow))
            self.app.processEvents()
            self.assertEqual(1, row.columns)
            for button in buttons:
                self.assertGreaterEqual(button.width(), button.sizeHint().width())
                self.assertTrue(row.rect().contains(button.geometry()), button.text())
            self.assertLess(buttons[0].y(), buttons[1].y())
            self.assertLess(buttons[1].y(), buttons[2].y())
            row.resize(wide, row.heightForWidth(wide))
            self.app.processEvents()
            self.assertEqual(3, row.columns)
            buttons[1].click()
            callback.assert_called_once()
            self.assertEqual(3, row.grid.count())
        finally:
            row.close()

    def test_last_button_spans_full_row_when_two_columns_fit(self):
        buttons = [QPushButton("View post"), QPushButton("Random reaction"), QPushButton("Choose reaction")]
        row = ButtonRow(buttons)
        try:
            width = buttons[0].sizeHint().width() + buttons[1].sizeHint().width() + 8
            row.resize(width, row.heightForWidth(width))
            row.show()
            self.app.processEvents()
            self.assertEqual(2, row.columns)
            self.assertEqual((1, 0, 1, 2), row.grid.getItemPosition(2))
            self.assertGreaterEqual(buttons[2].width(), width)
        finally:
            row.close()

    def test_open_account_only_passes_single_selected_account_to_browser(self):
        window = Mock()
        window.direct_access.open_account = AsyncMock(return_value="opened")
        for selected in ([], ["first", "second"]):
            window.selected_account_ids.return_value = selected
            QtDesktopApp.open_selected_account(window)
        window.start_managed_task.assert_not_called()
        account = SimpleNamespace(id="selected", label="Selected account")
        window.selected_account_ids.return_value = [account.id]
        window.accounts.get_account.return_value = account
        QtDesktopApp.open_selected_account(window)
        name, runner = window.start_managed_task.call_args.args
        self.assertEqual("open-account", name)
        self.assertEqual("opened", asyncio.run(runner(None)))
        window.direct_access.open_account.assert_awaited_once_with(account)

    def test_bad_mailbox_configuration_is_shown_before_starting_a_task(self):
        from core.config import AppConfig
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / "mailboxes.txt"
            path.write_text("# list\nowner@unknown.org:private-password", encoding="utf-8")
            window = Mock()
            window.config = AppConfig.load(Path(folder) / "empty.env")
            window.mailbox_file_entry.text.return_value = str(path)
            window.email_backend_combo.currentData.return_value = "imap"
            with patch("ui.qt_app.QMessageBox.warning") as warning:
                self.assertIsNone(QtDesktopApp.email_task_config(window))
            self.assertIn("EMAIL_MAILBOX_HOST", warning.call_args.args[2])
            self.assertNotIn("private-password", warning.call_args.args[2])
            window.start_managed_task.assert_not_called()

    def test_email_task_failure_clears_progress_and_shows_error(self):
        window = Mock()
        with patch("ui.qt_app.QMessageBox.warning") as warning:
            QtDesktopApp.task_failed(window, "change-login-email", "Traceback...\nMailboxError: missing mail host", "inbox")
        self.assertEqual("", window.current_task_id)
        window.clear_progress.assert_called_once()
        warning.assert_called_once_with(window, "Email operation failed", "MailboxError: missing mail host")


class ManagedTaskOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_fast_failure_is_never_overwritten_by_late_started_event(self):
        window = Mock()
        window.current_progress = Mock()
        window.active_group.return_value = "inbox"
        events = []
        failed = asyncio.get_running_loop().create_future()
        tasks = {}
        def start(name, factory):
            tasks["fixture"] = asyncio.create_task(factory(asyncio.Event()))
            return "fixture"
        window.tasks = SimpleNamespace(stop_by_name_prefix=AsyncMock(return_value=0), start=start, tasks=tasks)
        window.signals.task_started.emit.side_effect = lambda *args: events.append("started")
        def failure(*args):
            events.append("failed")
            failed.set_result(None)
        window.signals.task_failed.emit.side_effect = failure
        submitted = []
        def submit(coroutine, callback):
            async def run():
                result = await coroutine
                await failed  # Deliberately delay the submit callback until failure.
                callback(result, None)
            submitted.append(asyncio.create_task(run()))
        window.worker.submit.side_effect = submit
        async def invalid_settings(stop):
            raise RuntimeError("Invalid mailbox settings")
        QtDesktopApp.start_managed_task(window, "change-login-email", invalid_settings)
        await asyncio.wait_for(asyncio.gather(*submitted), 1)
        self.assertEqual(["started", "failed"], events)


if __name__ == "__main__":
    unittest.main()
