"""Real Qt construction in an empty temporary directory, without Telegram calls."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QPushButton
from core.config import AppConfig
from ui.qt_app import QtDesktopApp


def main() -> int:
    previous_dir = Path.cwd()
    app = QApplication.instance() or QApplication([])
    # The Windows offscreen plugin has no native font database. Load the
    # installed UI font so sizing checks use glyphs, not missing-font boxes.
    font_file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/segoeui.ttf"
    if font_file.is_file() and QFontDatabase.addApplicationFont(str(font_file)) >= 0:
        app.setFont(QFont("Segoe UI", 9))
    with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
        "TELEGRAM_DATA_DIR": str(Path(temp) / "data"),
        "TELEGRAM_IMPORT_DIR": str(Path(temp) / "imports"),
        "PROXY_MODE": "manual",
    }, clear=True):
        os.chdir(temp)
        config = AppConfig.load(Path(temp) / ".env")
        # Test preferences stay in this INI file, not in the user's registry.
        with patch("ui.qt_app.QSettings", return_value=QSettings(str(Path(temp) / "ui.ini"), QSettings.IniFormat)):
            window = QtDesktopApp(config)
        try:
            window.show()
            app.processEvents()
            assert window.windowTitle() == "Uznik MultiTool"
            assert window.sidebar_scroll.width() == 420
            assert window.table.verticalHeader().defaultSectionSize() == 46
            assert not hasattr(window, "density_combo")
            assert window.table_model.rowCount() == 0
            assert window.accounts.list_accounts() == []
            assert not window.windowIcon().isNull()
            # Long captions must fit at both the default and minimum window
            # size, without hidden horizontal overflow in the sidebar.
            for section in (window.actions_section, window.giveaway_section,
                            window.profile_section, window.security_section,
                            window.passkeys_section, window.scenarios_section):
                section.set_open(True)
            for width, height in ((1460, 860), (1080, 680)):
                window.resize(width, height)
                app.processEvents()
                assert window.sidebar_scroll.horizontalScrollBar().maximum() == 0
                for backend in ("http", "imap", "pop3"):
                    window.email_backend_combo.setCurrentIndex(window.email_backend_combo.findData(backend))
                    app.processEvents()
                    assert window.mailbox_file_row.isVisible() == (backend != "http")
                    for button in window.sidebar_scroll.findChildren(QPushButton):
                        if not button.isVisible():
                            continue
                        assert button.width() >= button.sizeHint().width(), (
                            f"Caption clipped: {button.text()!r} "
                            f"({button.width()} < {button.sizeHint().width()})"
                        )
                        assert button.height() >= button.sizeHint().height(), button.text()
                        assert button.parentWidget().rect().contains(button.geometry()), button.text()
            assert window.email_task_config().email_inbox_backend == "pop3"
            assert (config.import_dir / "emails/accounts.txt").is_file()
            for selected in ([], ["first"], ["first", "second"]):
                with patch.object(window, "selected_account_ids", return_value=selected):
                    window.update_header_status()
                    assert window.open_account_button.isVisible() == (len(selected) == 1)
            assert not window.external_auto_check.isChecked()
            assert "0 pending" in window.external_queue_label.text()
            assert (config.import_dir / "auth_input/processed").is_dir()
            supplied = config.import_dir / "auth_input" / "queue_fixture.session"
            supplied.write_bytes(b"offline queue fixture")
            os.utime(supplied, (time.time() - 3, time.time() - 3))
            window.refresh_external_session_queue()
            assert "1 pending" in window.external_queue_label.text()
            assert not window.external_sessions_running
            window.process_external_sessions()  # Empty API config must block requests.
            assert not window.external_sessions_running
            assert supplied.exists()
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.worker.loop.close()
            os.chdir(previous_dir)
    print("Desktop smoke OK: empty database, Uznik branding, fixed Comfortable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
