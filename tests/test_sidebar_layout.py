"""Button-row sizing and callbacks, without opening accounts or services."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


if __name__ == "__main__":
    unittest.main()
