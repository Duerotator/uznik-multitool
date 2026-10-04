"""Real Qt construction in an empty temporary directory, without Telegram calls."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from core.config import AppConfig
from ui.qt_app import QtDesktopApp


def main() -> int:
    previous_dir = Path.cwd()
    app = QApplication.instance() or QApplication([])
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
            assert window.sidebar_scroll.width() == 350
            assert window.table.verticalHeader().defaultSectionSize() == 46
            assert not hasattr(window, "density_combo")
            assert window.table_model.rowCount() == 0
            assert window.accounts.list_accounts() == []
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.worker.loop.close()
            os.chdir(previous_dir)
    print("Desktop smoke OK: empty database, Uznik branding, fixed Comfortable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
