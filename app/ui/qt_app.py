from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import threading
import traceback
from collections.abc import Awaitable, Callable
from concurrent.futures import CancelledError as FutureCancelledError
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QSettings, QSize, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QCloseEvent, QDesktopServices, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSizePolicy,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from core.config import AppConfig
from core.design_tokens import TOKENS
from core.models import utc_now_iso
from core.results import ActionResult
from core.session_manager import SessionManager
from core.storage import read_json
from core.task_runner import TaskRunner
from core.ui_operations import balance_proxy_assignments, build_bulk_operation_preview
from core.ui_progress import OperationProgress
from core.ui_logs import Severity, UiLogEntry
from core.ui_state import UiStateStore
from core.telegram_client import ReactionChoice
from modules.account_security import AccountSecurityService, passkey_restore_candidates
from modules.account_filters import AccountFilter, country_options, effective_proxy, filter_accounts, login_mail_state
from modules.accounts import AccountService
from modules.ai_companion import AICompanion, AIConversationConfig
from modules.chat_actions import ChatActionService
from modules.direct_access import DirectAccessResult, DirectAccessService
from modules.email_inbox import email_setup_description
from modules.giveaway_service import GiveawayInspection, GiveawayService, PROVIDER_LABELS
from modules.profile_scraper import ProfileScraperService, _norm_ch
from modules.online_mode import OnlineModeService
from modules.profile_customizer import ProfileCustomizer, ProfileUpdatePlan
from modules.profile_snapshots import ProfileSnapshotService
from modules.scenarios import Scenario, ScenarioStep, ScenarioStore
from modules.session_health import SessionHealthService
from modules.sleep_scheduler import SleepScheduler
from modules.spamblock import SpamBlockService
from modules.telegram_codes import TelegramCodeScanResult, TelegramCodeService
from utils.avatar_pack_downloader import download_google_drive_avatar_pack
from utils.phone_region import format_phone_with_region, phone_group_name
from utils.profile_generator import export_plan_text_files, split_unknown_avatars, write_profile_plan


class AsyncWorker:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(
        self,
        coro: Awaitable,
        done: Callable[[object | None, BaseException | None], None] | None = None,
    ):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if done:

            def callback(completed) -> None:
                try:
                    done(completed.result(), None)
                except BaseException as exc:  # noqa: BLE001 - forwarded to UI log
                    done(None, exc)

            future.add_done_callback(callback)
        return future

    def stop(self) -> None:
        async def shutdown() -> None:
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self.loop.shutdown_asyncgens()
            await self.loop.shutdown_default_executor(timeout=2)

        if not self.thread.is_alive():
            return
        future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        try:
            future.result(timeout=3)
        except (TimeoutError, FutureCancelledError):
            pass
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=1)


class UiSignals(QObject):
    task_started = Signal(str, object, object, str)
    task_finished = Signal(str, object, str, str)
    task_failed = Signal(str, str, str, str)
    task_stopped = Signal(str, str, str)
    log_message = Signal(str, str)
    refresh_requested = Signal()
    session_check_done = Signal(int, object, str)
    passkey_restore_done = Signal(int, object, str)
    stop_done = Signal(str, object, object)
    reaction_choices_loaded = Signal(object, str, str)
    reaction_choices_failed = Signal(str, str)
    proxy_count_ready = Signal(int, int)
    progress_updated = Signal(object)
    warmup_status = Signal(object)
    auth_code_requested = Signal(object)


class ButtonRow(QWidget):
    """Keep captions readable; wrap only when a row's labels cannot fit."""

    def __init__(self, buttons: list[QPushButton]) -> None:
        super().__init__()
        self.buttons = buttons
        self.columns = 0
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._reflow()

    def _column_count(self, width: int) -> int:
        for columns in range(len(self.buttons), 1, -1):
            widths = [0] * columns
            for index, button in enumerate(self.buttons):
                # A lone button on the last row spans the whole row.
                if index == len(self.buttons) - 1 and index % columns == 0:
                    continue
                widths[index % columns] = max(widths[index % columns], button.sizeHint().width())
            required = max(sum(widths) + self.grid.spacing() * (columns - 1),
                           max(button.sizeHint().width() for button in self.buttons))
            if required <= width:
                return columns
        return 1

    def minimumSizeHint(self) -> QSize:
        return QSize(max(button.sizeHint().width() for button in self.buttons),
                     max(button.sizeHint().height() for button in self.buttons))

    def sizeHint(self) -> QSize:
        return QSize(self.minimumSizeHint().width(), self.heightForWidth(self.width()))

    def heightForWidth(self, width: int) -> int:
        columns = self._column_count(width)
        heights = [max(button.sizeHint().height() for button in self.buttons[start:start + columns])
                   for start in range(0, len(self.buttons), columns)]
        return sum(heights) + self.grid.spacing() * (len(heights) - 1)

    def _reflow(self) -> None:
        columns = self._column_count(self.width())
        if columns == self.columns:
            return
        while self.grid.count():
            self.grid.takeAt(0)
        for column in range(max(columns, self.columns)):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, button in enumerate(self.buttons):
            span = columns if index == len(self.buttons) - 1 and index % columns == 0 else 1
            self.grid.addWidget(button, index // columns, index % columns, 1, span)
        self.columns = columns
        self.updateGeometry()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()


class Section(QWidget):
    def __init__(self, key: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.title = title
        self.button = QPushButton(f"▸  {title}")
        self.button.setCheckable(True)
        self.button.setProperty("class", "sectionButton")
        self.content = QFrame()
        self.content.setProperty("class", "sectionContent")
        self.content.setVisible(False)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(12, 10, 12, 12)
        self.content_layout.setSpacing(8)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(self.button)
        layout.addWidget(self.content)

    def set_open(self, value: bool) -> None:
        self.button.setChecked(value)
        self.button.setText(f"{'▾' if value else '▸'}  {self.title}")
        self.content.setVisible(value)
        self.button.setProperty("open", value)
        self.button.style().unpolish(self.button)
        self.button.style().polish(self.button)


class AccountTableModel(QAbstractTableModel):
    HEADERS = ["Added", "Phone / Region", "Spamblock", "Login Mail", "Cloud Password", "Username", "VPN", "Sleep", "Reg", "Profile"]
    STATUS_COLUMNS = {2, 3, 4, 5, 6, 7, 8, 9}
    FIXED_WIDTHS = {0: 142, 2: 118, 3: 122, 4: 140, 6: 72, 7: 82, 9: 72}
    STATUS_COLORS = {
        "ok": QColor("#45d483"),
        "bad": QColor("#ff6b6b"),
        "warn": QColor("#f6c65b"),
        "neutral": QColor("#96a0ad"),
        "info": QColor("#8bb7ff"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, list[str]]] = []

    def set_rows(self, rows: list[tuple[str, list[str]]]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        account_id, values = self.rows[index.row()]
        value = values[index.column()]
        if role == Qt.DisplayRole:
            return value
        if role == Qt.UserRole:
            return account_id
        if role == Qt.TextAlignmentRole and index.column() in self.STATUS_COLUMNS:
            return int(Qt.AlignCenter)
        if role == Qt.ForegroundRole and index.column() in self.STATUS_COLUMNS:
            return self.STATUS_COLORS[self._status_tone(value)]
        return None

    @staticmethod
    def _status_tone(value: str) -> str:
        normalized = value.lower()
        if normalized in {"valid", "clear", "custom", "yes", "awake"} or normalized.startswith("active"):
            return "ok"
        if normalized in {"invalid", "error", "forever", "no", "absent"} or "dead" in normalized:
            return "bad"
        if normalized in {"unknown", "disabled", "none"} or normalized.startswith("sleep"):
            return "neutral"
        if "temporary" in normalized or "limited" in normalized or "slow" in normalized:
            return "warn"
        return "info"

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.HEADERS):
            return self.HEADERS[section]
        return None


class QtDesktopApp(QMainWindow):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.accounts = AccountService(config)
        self.sessions = SessionManager(config)
        self.scenarios = ScenarioStore(config)
        self.tasks = TaskRunner(config)
        self.ui_state = UiStateStore(config)
        self.scheduler = SleepScheduler(str(config.data_dir / "sleep_zones.json"))
        self.worker = AsyncWorker()
        self.vpn_gateway_future = None
        self.signals = UiSignals()
        self.ui_log = logging.getLogger("ui")
        self.last_bad_session_ids: list[str] = []
        self.group_logs: dict[str, list[str]] = {}
        self.structured_logs: list[dict[str, str]] = []
        self.group_name = "inbox"
        self.current_task_id = ""
        self.current_task_name = ""
        self._task_start_pending = False
        self.sections: dict[str, Section] = {}
        self.recording_steps: list[ScenarioStep] = []
        self.recording_scenario = False
        self.replaying_scenario = False
        self.account_filter = AccountFilter()
        self.proxy_active_count = 0
        self.proxy_total_count = 0
        self.current_progress: OperationProgress | None = None
        self.direct_access = DirectAccessService(config)
        self.external_sessions_running = False
        self._announced_external_sources: set[str] = set()
        self.settings = QSettings("UznikMultiTool", "Desktop")
        self.group_name = str(self.settings.value("active_group", "inbox"))

        self.usernames_file = Path("templates/usernames.txt")
        self.first_names_file = Path("templates/first_names.txt")
        self.last_names_file = Path("templates/last_names.txt")
        self.bios_file = Path("templates/bios.txt")
        self.avatars_dir = Path("assets/avatar_packs")
        self.avatar_pack_dir = Path("assets/avatar_packs")
        self.profile_plan_file = Path("templates/profile_plan.json")

        self.setWindowTitle("Uznik MultiTool")
        self.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[2] / "assets/branding/uznik-multitool.ico")))
        self.resize(1460, 860)
        self.setMinimumSize(1080, 680)
        self.setAcceptDrops(True)
        self._connect_signals()
        self._build_layout()
        self._apply_style()
        self.restore_ui_settings()
        self.refresh_accounts()
        self.log(f"Import folder: {self.config.import_dir}")
        self.log(f"New local session queue: {self.config.import_dir / 'auth_input'}")
        self.log("Use the left sections. Active group controls which accounts are used.")
        self.start_local_vpn_gateway()

        self.import_timer = QTimer(self)
        self.import_timer.timeout.connect(self.auto_import_tick)
        self.import_timer.start(5000)
        self.sleep_timer = QTimer(self)
        self.sleep_timer.timeout.connect(self.refresh_sleep_states)
        self.sleep_timer.start(30_000)
        self.proxy_dashboard_timer = QTimer(self)
        self.proxy_dashboard_timer.timeout.connect(self.refresh_proxy_dashboard)
        self.proxy_dashboard_timer.start(5000)
        self.refresh_proxy_dashboard()
        self.refresh_external_session_queue()
        QTimer.singleShot(1200, lambda: self.import_now(verbose=False))

    def start_local_vpn_gateway(self) -> None:
        if os.name != "nt" or os.getenv("PROXY_MODE", "").strip().lower() != "vpn_gateway":
            return
        from modules.proxy_manager import ProxyPool
        from modules.vpn_gateway import gateway_ports, run_gateway_service

        config_path = self.config.data_dir / "vpn_gateway" / "xray.json"
        for port in gateway_ports(config_path):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                    self.log("Local Xray VPN gateway is already running.")
                    return
            except OSError:
                continue

        self.log("Starting the local Xray VPN gateway.")

        def done(_result: object | None, error: BaseException | None) -> None:
            if error is not None and not isinstance(error, (asyncio.CancelledError, FutureCancelledError)):
                self.signals.log_message.emit(f"Local VPN gateway stopped: {error}", "inbox")

        self.vpn_gateway_future = self.worker.submit(
            run_gateway_service(
                ProxyPool(self.config.proxy_pool_db),
                self.config.data_dir,
                int(os.getenv("VPN_GATEWAY_REFRESH_MINUTES", "360")),
            ),
            done,
        )

    def _connect_signals(self) -> None:
        self.signals.task_started.connect(self.task_started)
        self.signals.task_finished.connect(self.task_finished)
        self.signals.task_failed.connect(self.task_failed)
        self.signals.task_stopped.connect(self.task_stopped)
        self.signals.log_message.connect(self.log)
        self.signals.refresh_requested.connect(self.refresh_accounts)
        self.signals.session_check_done.connect(self.on_session_check_done)
        self.signals.passkey_restore_done.connect(self.on_passkey_restore_done)
        self.signals.stop_done.connect(self.on_stop_done)
        self.signals.reaction_choices_loaded.connect(self.on_reaction_choices_loaded)
        self.signals.reaction_choices_failed.connect(self.on_reaction_choices_failed)
        self.signals.proxy_count_ready.connect(self.on_proxy_count_ready)
        self.signals.progress_updated.connect(self.on_progress_updated)
        self.signals.warmup_status.connect(self.on_warmup_status)
        self.signals.auth_code_requested.connect(self.on_auth_code_requested)

    def _build_layout(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(0)

        self.sidebar_scroll = QScrollArea()
        self.sidebar_scroll.setObjectName("sidebarScroll")
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        root_layout.addWidget(self.sidebar_scroll)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(300)
        self.sidebar_scroll.setWidget(sidebar)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(14, 14, 14, 14)
        sidebar_layout.setSpacing(11)

        self.actions_section = self._add_section(sidebar_layout, "actions", "Actions")
        self._build_actions_section(self.actions_section.content_layout)

        self.warmup_section = self._add_section(sidebar_layout, "warmup", "Warmup / Sleep")
        self._build_warmup_section(self.warmup_section.content_layout)

        self.giveaway_section = self._add_section(sidebar_layout, "giveaways", "Giveaways")
        self._build_giveaway_section(self.giveaway_section.content_layout)

        self.profile_section = self._add_section(sidebar_layout, "profile", "Profile")
        self._build_profile_section(self.profile_section.content_layout)

        self.security_section = self._add_section(sidebar_layout, "security", "Security")
        self._build_security_section(self.security_section.content_layout)

        self.passkeys_section = self._add_section(sidebar_layout, "passkeys", "Passkeys")
        self._build_passkeys_section(self.passkeys_section.content_layout)

        self.scenarios_section = self._add_section(sidebar_layout, "scenarios", "Scenarios")
        self._build_scenarios_section(self.scenarios_section.content_layout)

        sidebar_layout.addStretch(1)

        main = QFrame()
        main.setObjectName("mainPanel")
        root_layout.addWidget(main, 1)
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        header = QFrame()
        header.setObjectName("topHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 9, 12, 9)
        header_layout.setSpacing(7)
        self.header_title = QLabel("Uznik MultiTool")
        self.header_title.setObjectName("appTitle")
        header_layout.addWidget(self.header_title)
        header_layout.addStretch(1)
        self.accounts_pill = self._add_status_pill(header_layout, "Accounts: 0", "info")
        self.selected_pill = self._add_status_pill(header_layout, "Selected: 0", "neutral")
        self.proxy_pill = self._add_status_pill(header_layout, "VPN exits: 0", "neutral")
        self.task_pill = self._add_status_pill(header_layout, "Task: idle", "ok")
        self.progress_pill = self._add_status_pill(header_layout, "", "warn")
        self.progress_pill.setVisible(False)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(160)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #315777; border-radius: 3px; background: #17232d; }"
            "QProgressBar::chunk { border-radius: 2px; background: #3b9ad9; }"
        )
        self.progress_bar.setVisible(False)
        header_layout.addWidget(self.progress_bar)
        main_layout.addWidget(header)

        self.filters_section = Section("filters", "Filters")
        self.filters_section.button.clicked.connect(self.filters_section.set_open)
        main_layout.addWidget(self.filters_section)
        filters = QFrame()
        filters.setObjectName("filterBar")
        filters_layout = QGridLayout(filters)
        filters_layout.setContentsMargins(10, 7, 10, 7)
        filters_layout.setSpacing(7)
        self.filter_query = QLineEdit()
        self.filter_query.setPlaceholderText("Search phone, name, username…")
        self.filter_from = QLineEdit()
        self.filter_from.setInputMask("0000.00.00;_")
        self.filter_from.setPlaceholderText("YYYY.MM.DD")
        self.filter_to = QLineEdit()
        self.filter_to.setInputMask("0000.00.00;_")
        self.filter_to.setPlaceholderText("YYYY.MM.DD")
        self.filter_country = QComboBox()
        self.filter_country.addItem("All countries", "any")
        for option in country_options(self.accounts.list_accounts()):
            self.filter_country.addItem(f"{option['label']} ({option['count']})", option["value"])
        self.filter_spamblock = QComboBox()
        self.filter_spamblock.addItem("Any spamblock", "any")
        for value, label in (("clear", "Clear"), ("limited", "Limited"), ("forever", "Forever"), ("unknown", "Unknown")):
            self.filter_spamblock.addItem(label, value)
        self.filter_login_mail = QComboBox()
        self.filter_login_mail.addItem("Any login mail", "any")
        for value, label in (("custom", "Custom"), ("absent", "Absent"), ("unknown", "Unknown")):
            self.filter_login_mail.addItem(label, value)
        self.filter_cloud = QComboBox()
        self.filter_username = QComboBox()
        for combo, title in ((self.filter_cloud, "Cloud"), (self.filter_username, "Username")):
            combo.addItem(f"Any {title.lower()}", "any")
            combo.addItem(f"{title} present", "with")
            combo.addItem(f"{title} missing", "without")
            combo.addItem(f"{title} unknown", "unknown")
        self.filter_proxy = QComboBox()
        self.filter_proxy.addItem("Any VPN", "any")
        self.filter_proxy.addItem("With VPN", "with")
        self.filter_proxy.addItem("Without VPN", "without")
        self.filter_sleep = QComboBox()
        self.filter_sleep.addItem("Any sleep state", "any")
        self.filter_sleep.addItem("Awake", "awake")
        self.filter_sleep.addItem("Sleeping", "sleeping")
        self.filter_sleep.addItem("Unknown", "unknown")
        filters_layout.addWidget(self.filter_query, 0, 0, 1, 2)
        filters_layout.addWidget(QLabel("Added from"), 0, 2)
        filters_layout.addWidget(self.filter_from, 0, 3)
        filters_layout.addWidget(QLabel("to"), 0, 4)
        filters_layout.addWidget(self.filter_to, 0, 5)
        filters_layout.addWidget(self.filter_country, 1, 0)
        filters_layout.addWidget(self.filter_spamblock, 1, 1)
        filters_layout.addWidget(self.filter_login_mail, 1, 2)
        filters_layout.addWidget(self.filter_cloud, 1, 3)
        filters_layout.addWidget(self.filter_username, 1, 4)
        clear_filters = QPushButton("Clear")
        clear_filters.setProperty("class", "secondary")
        clear_filters.clicked.connect(self.clear_account_filters)
        filters_layout.addWidget(clear_filters, 1, 5)
        filters_layout.addWidget(self.filter_proxy, 2, 0)
        filters_layout.addWidget(self.filter_sleep, 2, 1)
        self.filter_query.textChanged.connect(self.apply_account_filters)
        self.filter_from.editingFinished.connect(self.apply_account_filters)
        self.filter_to.editingFinished.connect(self.apply_account_filters)
        self.filter_country.currentIndexChanged.connect(self.apply_account_filters)
        self.filter_spamblock.currentIndexChanged.connect(self.apply_account_filters)
        self.filter_login_mail.currentIndexChanged.connect(self.apply_account_filters)
        self.filter_cloud.currentIndexChanged.connect(self.apply_account_filters)
        self.filter_username.currentIndexChanged.connect(self.apply_account_filters)
        self.filter_proxy.currentIndexChanged.connect(self.apply_account_filters)
        self.filter_sleep.currentIndexChanged.connect(self.apply_account_filters)
        self.filters_section.content_layout.addWidget(filters)

        self.table_model = AccountTableModel()
        self.table = QTableView()
        self.table.setObjectName("accountTable")
        self.table.setModel(self.table_model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.MultiSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.verticalHeader().setMinimumSectionSize(46)
        self.table.selectionModel().selectionChanged.connect(lambda *_args: self.update_header_status())
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignCenter)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        header.setSectionResizeMode(6, QHeaderView.Fixed)
        header.setSectionResizeMode(7, QHeaderView.Fixed)
        for column, width in self.table_model.FIXED_WIDTHS.items():
            self.table.setColumnWidth(column, width)
        main_layout.addWidget(self.table, 4)

        self.selection_bar = QFrame()
        self.selection_bar.setObjectName("selectionBar")
        selection_layout = QHBoxLayout(self.selection_bar)
        selection_layout.setContentsMargins(10, 6, 10, 6)
        selection_layout.setSpacing(7)
        self.selection_bar_label = QLabel("Selected: 0")
        selection_layout.addWidget(self.selection_bar_label)
        selection_layout.addStretch(1)
        self.open_account_button = QPushButton("Open acc")
        self.open_account_button.setProperty("class", "primary")
        self.open_account_button.clicked.connect(self.open_selected_account)
        self.open_account_button.setVisible(False)
        selection_layout.addWidget(self.open_account_button)
        for label, handler, style in (
            ("Assign VPN", self.assign_proxy_to_selected, "primary"),
            ("Check", self.check_selected_sessions, "secondary"),
            ("Move", self.move_selected, "secondary"),
        ):
            button = QPushButton(label)
            button.setProperty("class", style)
            button.clicked.connect(handler)
            selection_layout.addWidget(button)
        self.selection_bar.setVisible(False)
        main_layout.addWidget(self.selection_bar)

        logs_panel = QFrame()
        logs_panel.setObjectName("logsPanel")
        logs_layout = QVBoxLayout(logs_panel)
        logs_layout.setContentsMargins(10, 8, 10, 10)
        logs_layout.setSpacing(7)
        self.log_title = QLabel("ACTIVITY  ·  inbox")
        self.log_title.setObjectName("logsTitle")
        logs_layout.addWidget(self.log_title)
        self.log_box = QPlainTextEdit()
        self.log_box.setObjectName("logBox")
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(500)
        logs_layout.addWidget(self.log_box, 1)
        main_layout.addWidget(logs_panel, 2)

    def _add_status_pill(self, layout: QHBoxLayout, text: str, tone: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("class", "statusPill")
        label.setProperty("tone", tone)
        layout.addWidget(label)
        return label

    def _set_status_pill(self, label: QLabel, text: str, tone: str) -> None:
        label.setText(text)
        label.setProperty("tone", tone)
        label.style().unpolish(label)
        label.style().polish(label)

    def _status_tone(self, value: str) -> str:
        normalized = value.lower()
        if normalized in {"valid", "clear", "custom", "yes", "awake"} or normalized.startswith("active"):
            return "ok"
        if normalized in {"invalid", "error", "forever", "no", "absent"} or "dead" in normalized:
            return "bad"
        if normalized in {"unknown", "disabled", "none"} or normalized.startswith("sleep"):
            return "neutral"
        if "temporary" in normalized or "limited" in normalized or "slow" in normalized:
            return "warn"
        return "info"

    def _status_badge(self, value: str) -> QLabel:
        badge = QLabel(value)
        badge.setAlignment(Qt.AlignCenter)
        badge.setProperty("class", "statusBadge")
        badge.setProperty("tone", self._status_tone(value))
        badge.setToolTip(value)
        return badge

    def apply_account_filters(self) -> None:
        self.account_filter = AccountFilter(
            query=self.filter_query.text().strip(),
            created_from=self._desktop_date_value(self.filter_from),
            created_to=self._desktop_date_value(self.filter_to),
            country=str(self.filter_country.currentData()),
            spamblock=str(self.filter_spamblock.currentData()),
            login_mail=str(self.filter_login_mail.currentData()),
            two_fa=str(self.filter_cloud.currentData()),
            username=str(self.filter_username.currentData()),
            proxy=str(self.filter_proxy.currentData()),
            sleep=str(self.filter_sleep.currentData()),
        )
        self.refresh_accounts()

    @staticmethod
    def _desktop_date_value(entry: QLineEdit) -> str:
        parts = entry.text().replace("_", "").split(".")
        if (
            len(parts) == 3
            and tuple(map(len, parts)) == (4, 2, 2)
            and all(part.isdigit() for part in parts)
        ):
            return "-".join(parts)
        return ""

    def clear_account_filters(self) -> None:
        for entry in (self.filter_query, self.filter_from, self.filter_to):
            entry.blockSignals(True)
            entry.clear()
            entry.blockSignals(False)
        for combo in (
            self.filter_country, self.filter_spamblock, self.filter_login_mail,
            self.filter_cloud, self.filter_username, self.filter_proxy, self.filter_sleep,
        ):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.account_filter = AccountFilter()
        self.refresh_accounts()

    def _build_actions_section(self, layout: QVBoxLayout) -> None:
        self._add_title(layout, "Chat / Channel / Link")
        self.target_entry = self._add_line_with_paste(
            layout,
            "id/@ chat or channel / invite/ref/post link",
        )
        self._add_button_row(
            layout,
            [
                ("Join", lambda: self.start_chat_action("join"), "primary"),
                ("Leave", lambda: self.start_chat_action("leave"), "secondary"),
            ],
        )
        self._add_button_row(layout, [("Open Telegram link", self.open_link, "primary")])
        self._add_button_row(
            layout,
            [
                ("View post", self.view_post, "primary"),
                ("Random reaction", self.set_random_reaction, "primary"),
                ("Choose reaction", self.choose_reaction, "primary"),
            ],
        )

        self._add_title(layout, "Tasks")
        self._add_button_row(
            layout,
            [
                ("Online", self.start_online, "primary"),
                ("Stop current", self.stop_task, "secondary"),
            ],
        )
        self._add_button_row(
            layout,
            [
                ("Check sessions", self.check_sessions, "secondary"),
                ("Check registration date", self.check_account_age, "secondary"),
            ],
        )
        self._add_button_row(layout, [("Check spamblock", self.check_spamblock, "secondary")])

        self._add_title(layout, "External sessions")
        self.external_queue_label = QLabel()
        self.external_queue_label.setWordWrap(True)
        self.external_queue_label.setToolTip(str(self.config.import_dir / "auth_input"))
        layout.addWidget(self.external_queue_label)
        self._add_button_row(
            layout,
            [
                ("Process dropped sessions", lambda: self.process_external_sessions(), "primary"),
                ("Open auth_input", self.open_external_session_folder, "secondary"),
            ],
        )
        self.external_auto_check = QCheckBox("Auto-process new auth_input files")
        self.external_auto_check.setToolTip("Creates new Telegram authorizations through your verified proxy. Codes are read from the supplied session; 2FA may be requested.")
        self.external_auto_check.setChecked(False)
        layout.addWidget(self.external_auto_check)
        self.external_queue_label.setText("auth_input: drop your .session files here")

        self._add_title(layout, "VPN exits")
        self._add_button_row(
            layout,
            [
                ("Assign VPN to selected", self.assign_proxy_to_selected, "primary"),
                ("Assign VPN to group", self.assign_proxy_to_group, "primary"),
            ],
        )
        self._add_button_row(
            layout,
            [
                ("Clear VPN", self.clear_proxy_selected, "secondary"),
                ("Check VPN exits", self.check_assigned_proxies, "secondary"),
            ],
        )
        self._add_button_row(
            layout,
            [
                ("Check VPN pool", self.refresh_proxy_pool, "primary"),
            ],
        )

        self._add_title(layout, "AI Companion")
        self.ai_chat_entry = self._add_line_with_paste(layout, "id/@ chat")
        self.ai_topic_entry = self._add_line_with_paste(layout, "Topic")
        self.ai_count_entry = self._add_line_with_paste(layout, "messages")
        self._add_button_row(layout, [("Run AI for group", self.start_ai, "primary")])

    def _build_warmup_section(self, layout: QVBoxLayout) -> None:
        self._add_button_row(
            layout,
            [
                ("Warmup", self.start_warmup, "primary"),
                ("Stop warmup", self.stop_warmup, "secondary"),
            ],
        )
        from modules.warmup_settings import WarmupOptions
        self._add_title(layout, "Warmup settings")
        try:
            options = WarmupOptions.load(self.config.data_dir / "warmup_settings.json")
        except (ValueError, TypeError) as exc:
            options = WarmupOptions()
            self.ui_log.warning("Warmup settings need correction: %s", exc)
        self.warmup_channels = QPlainTextEdit()
        self.warmup_channels.setPlaceholderText("Public channels: @channel or https://t.me/channel\nOne per line; no automatic channel suggestions")
        self.warmup_channels.setPlainText("\n".join(options.channels))
        self.warmup_channels.setFixedHeight(82)
        layout.addWidget(self.warmup_channels)
        self.warmup_controls = {}
        for key, caption, low, high in (("posts_per_cycle", "Posts per cycle", 1, 6),
                                       ("cycles", "Cycles per account (0 = continuous)", 0, 100),
                                       ("hourly_budget", "Read/action attempts per hour", 1, 100),
                                       ("daily_budget", "Read/action attempts per 24 hours", 1, 1000)):
            row = QHBoxLayout()
            label, control = QLabel(caption), QSpinBox()
            label.setWordWrap(True)
            control.setRange(low, high)
            control.setValue(getattr(options, key))
            row.addWidget(label, 1)
            row.addWidget(control)
            layout.addLayout(row)
            self.warmup_controls[key] = control
        for key, caption in (("reactions", "Allow reactions (up to one per cycle)"),
                             ("save_posts", "Save post links to Saved Messages"),
                             ("join_channels", "Join only the configured channels")):
            control = QCheckBox(caption)
            control.setChecked(getattr(options, key))
            layout.addWidget(control)
            self.warmup_controls[key] = control
        self._add_button_row(layout, [("Save warmup settings", self.save_warmup_settings, "secondary")])
        self.warmup_status_label = QLabel("Not running. Default: read-only; writes require opt-in.")
        self.warmup_status_label.setWordWrap(True)
        layout.addWidget(self.warmup_status_label)
        self._add_title(layout, "Sleep schedule")
        self.sleep_enabled = QCheckBox("Enable sleep schedule for background tasks")
        self.sleep_enabled.setChecked(bool(self.scheduler.settings["enabled"]))
        layout.addWidget(self.sleep_enabled)
        self.sleep_zone_entry = self._add_line_with_paste(layout, "Timezone: Europe/Moscow, UTC…")
        self.sleep_zone_entry.setText(str(self.scheduler.settings["timezone"]))
        hours = QHBoxLayout()
        self.sleep_start_hour, self.sleep_end_hour = QSpinBox(), QSpinBox()
        for title, control, key in (("Sleep from", self.sleep_start_hour, "start"), ("to", self.sleep_end_hour, "end")):
            control.setRange(0, 23)
            control.setSuffix(":00")
            control.setValue(int(self.scheduler.settings[key]))
            hours.addWidget(QLabel(title))
            hours.addWidget(control)
        layout.addLayout(hours)
        self._add_button_row(layout, [("Apply sleep schedule", self.apply_sleep_schedule, "secondary")])
        self._add_button_row(layout, [("Assign timezones", self.assign_timezones, "secondary")])


    def _build_giveaway_section(self, layout: QVBoxLayout) -> None:
        self._add_label(layout, "Giveaway engine")
        self.giveaway_provider = QComboBox()
        for key, label in PROVIDER_LABELS.items():
            self.giveaway_provider.addItem(label, key)
        layout.addWidget(self.giveaway_provider)

        self._add_label(layout, "Post link or direct bot/startapp link")
        self.giveaway_target = self._add_line_with_paste(
            layout, "https://t.me/channel/123 or bot start link",
        )
        self._add_label(layout, "Extra required channels (optional)")
        self.giveaway_channels = self._add_line_with_paste(
            layout, "@channel1, @channel2",
        )
        self._add_button_row(
            layout,
            [
                ("Check link", self.inspect_giveaway, "secondary"),
                ("Join giveaway", self.join_giveaway, "primary"),
            ],
        )
        self._add_label(
            layout,
            "Uses selected accounts when rows are selected; otherwise uses the filtered active group.",
        )

    def _build_scenarios_section(self, layout: QVBoxLayout) -> None:
        self._add_label(layout, "Scenario name")
        self.scenario_name_entry = QLineEdit()
        self.scenario_name_entry.setPlaceholderText("example: beta chat warmup")
        palette = self.scenario_name_entry.palette()
        palette.setColor(QPalette.PlaceholderText, QColor("#9aa9bb"))
        self.scenario_name_entry.setPalette(palette)
        layout.addWidget(self.scenario_name_entry)

        self.scenario_select = QComboBox()
        self.scenario_select.currentTextChanged.connect(lambda _value: self.render_scenario_preview())
        layout.addWidget(self.scenario_select)

        self._add_button_row(
            layout,
            [
                ("Start recording", self.start_scenario_recording, "primary"),
                ("Stop & save", self.stop_and_save_scenario, "secondary"),
            ],
        )
        self._add_button_row(
            layout,
            [
                ("Run saved", self.run_selected_scenario, "primary"),
                ("Run active", self.run_selected_scenario_on_active_group, "primary"),
            ],
        )
        self._add_button_row(
            layout,
            [
                ("Delete selected", self.delete_selected_scenario, "secondary"),
                ("Clear draft", self.clear_scenario_draft, "secondary"),
            ],
        )
        self._add_button_row(layout, [("Refresh list", self.refresh_scenarios, "secondary")])

        self.scenario_preview = QPlainTextEdit()
        self.scenario_preview.setReadOnly(True)
        self.scenario_preview.setMaximumHeight(150)
        self.scenario_preview.setPlaceholderText("Recorded steps will appear here.")
        layout.addWidget(self.scenario_preview)
        self.refresh_scenarios()

    def _build_profile_section(self, layout: QVBoxLayout) -> None:
        self._add_label(layout, "Group / Chat (@name, t.me link, or invite)")
        self.scrape_channel_entry = self._add_line_with_paste(layout, "@chaturbator_chat or t.me/+invitehash")
        self._add_label(layout, "Or usernames (comma / space / newline)")
        self.scrape_usernames_entry = QLineEdit()
        self.scrape_usernames_entry.setPlaceholderText("@user1, @user2, ...")
        layout.addWidget(self.scrape_usernames_entry)

        self._add_label(layout, "Profiles to apply (how many accounts get profile)")
        self.scrape_count_entry = QLineEdit()
        self.scrape_count_entry.setText("5")
        self.scrape_count_entry.setMaximumWidth(70)
        row_count = QHBoxLayout()
        row_count.addWidget(self.scrape_count_entry)
        row_count.addStretch()
        layout.addLayout(row_count)

        self._add_label(layout, "Filters (at least one match)")
        f1 = QHBoxLayout()
        self.chk_avatar = QCheckBox("Has avatar")
        self.chk_bio_flt = QCheckBox("Has bio")
        self.chk_story_flt = QCheckBox("Has story")
        for w in [self.chk_avatar, self.chk_bio_flt, self.chk_story_flt]:
            w.setStyleSheet("font-size:11px")
            f1.addWidget(w)
        f1.addStretch()
        layout.addLayout(f1)

        f2 = QHBoxLayout()
        lbl_a = QLabel("Min avatars:")
        lbl_a.setStyleSheet("font-size:11px")
        self.min_avatars = QLineEdit()
        self.min_avatars.setText("0")
        self.min_avatars.setMaximumWidth(40)
        lbl_s = QLabel("Min stories:")
        lbl_s.setStyleSheet("font-size:11px")
        self.min_stories = QLineEdit()
        self.min_stories.setText("0")
        self.min_stories.setMaximumWidth(40)
        for w in [lbl_a, self.min_avatars, lbl_s, self.min_stories]:
            f2.addWidget(w)
        f2.addStretch()
        layout.addLayout(f2)

        self._add_label(layout, "Copy options")
        self.chk_name = QCheckBox("Name")
        self.chk_name.setChecked(True)
        self.chk_bio = QCheckBox("Bio")
        self.chk_bio.setChecked(True)
        self.chk_username = QCheckBox("Username")
        self.chk_username.setChecked(True)
        self.chk_avatars = QCheckBox("Avatars")
        self.chk_avatars.setChecked(True)
        self.chk_music = QCheckBox("Music")
        self.chk_stories_copy = QCheckBox("Stories")
        opts1 = QHBoxLayout()
        for w in [self.chk_name, self.chk_bio, self.chk_username, self.chk_avatars]:
            w.setStyleSheet("font-size:11px")
            opts1.addWidget(w)
        opts1.addStretch()
        layout.addLayout(opts1)
        self.chk_birthday = QCheckBox("Birthday")
        self.chk_skip_profiled = QCheckBox("Skip profiled")
        self.chk_skip_profiled.setChecked(True)
        opts2 = QHBoxLayout()
        for w in [self.chk_music, self.chk_stories_copy, self.chk_birthday, self.chk_skip_profiled]:
            w.setStyleSheet("font-size:11px")
            opts2.addWidget(w)
        opts2.addStretch()
        layout.addLayout(opts2)

        self._add_button_row(layout, [
            ("Scrape && Apply", self._profile_scrape, "primary"),
        ])
        self._add_button_row(layout, [
            ("Premium stories", self.premium_stories, "secondary"),
            ("Apply saved profiles", self.apply_saved_profiles, "secondary"),
        ])
        self._add_button_row(layout, [
            ("Clear selected", self.clear_full_profile, "secondary"),
        ])

    def _build_security_section(self, layout: QVBoxLayout) -> None:
        self._add_label(layout, "2FA current password")
        self.twofa_entry = self._add_line_with_paste(
            layout,
            "2FA password if same for accounts",
            password=True,
        )
        self._add_label(layout, "Email code source")
        self.email_backend_combo = QComboBox()
        for label, backend in (("HTTP Inbox API", "http"), ("IMAP mailboxes", "imap"), ("POP3 mailboxes", "pop3")):
            self.email_backend_combo.addItem(label, backend)
        self.email_backend_combo.setCurrentIndex(max(0, self.email_backend_combo.findData(self.config.email_inbox_backend)))
        layout.addWidget(self.email_backend_combo)
        self.mailbox_file_row = QWidget()
        file_layout = QHBoxLayout(self.mailbox_file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.setSpacing(8)
        self.mailbox_file_entry = QLineEdit(str(self.config.email_mailboxes_file or ""))
        self.mailbox_file_entry.setPlaceholderText("Mailbox list (UTF-8)")
        self.mailbox_file_entry.setToolTip("email:password;host;port — one mailbox per line. Passwords stay in your file.")
        file_layout.addWidget(self.mailbox_file_entry, 1)
        browse = QPushButton("Browse")
        browse.clicked.connect(self.choose_mailbox_file)
        file_layout.addWidget(browse)
        layout.addWidget(self.mailbox_file_row)
        self.email_backend_combo.currentIndexChanged.connect(self.sync_mailbox_options)
        self.sync_mailbox_options()
        self._add_button_row(layout, [("Terminate other sessions", self.terminate_other_sessions, "secondary")])
        self._add_button_row(
            layout,
            [
                ("Terminate PC sessions", self.terminate_pc_sessions, "secondary"),
                ("Bind recovery email", self.bind_recovery_email, "primary"),
            ],
        )
        self._add_button_row(
            layout,
            [
                ("Change login email", self.change_login_email, "primary"),
                ("Set cloud password", self.set_cloud_password, "primary"),
            ],
        )
        self._add_button_row(layout, [("Get Telegram codes", self.get_recent_telegram_codes, "primary")])
        self._add_button_row(
            layout,
            [
                ("Remove cloud password", self.remove_cloud_password, "secondary"),
            ],
        )
        self._add_button_row(layout, [("Delete selected accounts", self.delete_selected_accounts, "secondary")])

    def _build_passkeys_section(self, layout: QVBoxLayout) -> None:
        self._add_button_row(
            layout,
            [
                ("Add passkeys", self.add_passkeys, "primary"),
                ("Delete passkeys", self.delete_passkeys, "secondary"),
            ],
        )
        self._add_button_row(
            layout,
            [
                ("Restore via passkey", self.restore_passkeys, "primary"),
            ],
        )

    def _build_smart_section(self, layout: QVBoxLayout) -> None:
        self._add_title(layout, "Auto Grouping")
        self._add_button_row(
            layout,
            [
                ("By phone", lambda: self.smart_group("phone"), "primary"),
                ("By 2FA", lambda: self.smart_group("cloud_password"), "secondary"),
            ],
        )
        self._add_button_row(
            layout,
            [
                ("By login mail", lambda: self.smart_group("login_mail"), "secondary"),
                ("By username", lambda: self.smart_group("username"), "secondary"),
            ],
        )
        self._add_button_row(
            layout,
            [("By added date", lambda: self.smart_group("added_date"), "primary")],
        )
        self._add_button_row(layout, [("By spamblock", lambda: self.smart_group("spamblock"), "secondary")])

    def _add_section(self, layout: QVBoxLayout, key: str, title: str) -> Section:
        section = Section(key, title)
        section.button.clicked.connect(lambda _checked=False, value=key: self.toggle_section(value))
        self.sections[key] = section
        layout.addWidget(section)
        return section

    def toggle_section(self, key: str) -> None:
        current = self.sections[key].content.isVisible()
        for name, section in self.sections.items():
            section.set_open(name == key and not current)

    def _add_title(self, layout: QVBoxLayout, text: str) -> None:
        label = QLabel(text)
        label.setProperty("class", "sectionTitle")
        layout.addWidget(label)

    def _add_label(self, layout: QVBoxLayout, text: str) -> None:
        label = QLabel(text)
        label.setProperty("class", "fieldLabel")
        layout.addWidget(label)

    def _add_line_with_paste(
        self,
        layout: QVBoxLayout,
        placeholder: str,
        *,
        password: bool = False,
    ) -> QLineEdit:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        entry = QLineEdit()
        entry.setPlaceholderText(placeholder)
        palette = entry.palette()
        palette.setColor(QPalette.PlaceholderText, QColor("#9aa9bb"))
        entry.setPalette(palette)
        if password:
            entry.setEchoMode(QLineEdit.Password)
        row.addWidget(entry, 1)
        paste = QPushButton("Paste")
        paste.setProperty("class", "smallButton")
        paste.clicked.connect(lambda _checked=False, target=entry: self.paste_to_entry(target))
        row.addWidget(paste)
        layout.addLayout(row)
        return entry

    def _add_button_row(self, layout: QVBoxLayout, buttons: list[tuple[str, Callable[[], None], str]]) -> None:
        widgets = []
        for text, command, kind in buttons:
            button = QPushButton(text)
            button.setProperty("class", kind)
            button.clicked.connect(command)
            widgets.append(button)
        layout.addWidget(ButtonRow(widgets))

    def _clear_layout(self, layout: QVBoxLayout | QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child_layout is not None:
                self._clear_layout(child_layout)

    def _apply_style(self) -> None:
        stylesheet = """
            QMainWindow {
                background: #0e1116;
                color: #e9f0f8;
                font-family: "Segoe UI";
                font-size: 13px;
            }
            QScrollArea#sidebarScroll {
                background: #171c24;
                border: 1px solid #2c3542;
                border-radius: 11px;
            }
            QWidget#sidebar {
                background: #171c24;
            }
            QFrame#mainPanel {
                background: #0e1116;
                border: 0;
            }
            QFrame#topHeader {
                background: #171c24;
                border: 1px solid #2c3542;
                border-radius: 11px;
            }
            QFrame#filterBar {
                background: #171c24;
                border: 1px solid #2c3542;
                border-radius: 11px;
            }
            QFrame#selectionBar {
                background: #16232f;
                border: 1px solid #2a5480;
                border-radius: 9px;
            }
            QLabel#appTitle {
                color: #e9f0f8;
                font-size: 16px;
                font-weight: 700;
                padding-right: 8px;
            }
            QLabel[class="statusPill"] {
                background: #1f2630;
                color: #93a1b4;
                border: 1px solid #2c3542;
                border-radius: 10px;
                padding: 3px 9px;
                font-size: 12px;
                font-weight: 600;
            }
            QLabel[class="statusPill"][tone="ok"] {
                background: #16281f; color: #9fe0bd; border-color: #2c6c4c;
            }
            QLabel[class="statusPill"][tone="warn"] {
                background: #2a2114; color: #f0cf90; border-color: #6d5520;
            }
            QLabel[class="statusPill"][tone="bad"] {
                background: #2a1817; color: #f0aaa2; border-color: #6d3630;
            }
            QLabel {
                color: #e9f0f8;
            }
            QLabel[class="sectionTitle"] {
                color: #f7fbff;
                font-size: 16px;
                font-weight: 700;
                padding-top: 6px;
            }
            QLabel[class="fieldLabel"] {
                color: #d8e1ec;
                padding-top: 4px;
            }
            QLineEdit, QComboBox {
                background: #0f151d;
                color: #e9f0f8;
                border: 1px solid #3a4553;
                border-radius: 8px;
                padding: 8px 10px;
                min-height: 23px;
                selection-background-color: #2f73bc;
            }
            QLineEdit:focus {
                border: 1px solid #3a92e8;
                background: #0f151d;
            }
            QComboBox::drop-down {
                border: 0;
                width: 22px;
            }
            QComboBox QAbstractItemView {
                background: #171c24;
                color: #e9f0f8;
                border: 1px solid #3a4553;
                selection-background-color: #1d4f8c;
            }
            QLineEdit::placeholder {
                color: #9aa9bb;
            }
            QPushButton {
                background: #2a7fd4;
                color: #f8fbff;
                border: 0;
                border-radius: 8px;
                padding: 8px 12px;
                min-height: 23px;
            }
            QPushButton:hover {
                background: #3a92e8;
            }
            QPushButton:pressed {
                background: #18598f;
            }
            QPushButton[class="secondary"], QPushButton[class="smallButton"] {
                background: #24415e;
            }
            QPushButton[class="secondary"]:hover, QPushButton[class="smallButton"]:hover {
                background: #2673b6;
            }
            QPushButton[class="primary"] {
                background: #1f6fb2;
            }
            QPushButton[class="danger"] {
                background: #8d3a31;
            }
            QPushButton[class="danger"]:hover {
                background: #a8453a;
            }
            QPushButton[class="sectionButton"] {
                text-align: left;
                background: #171c24;
                border: 1px solid #2c3542;
                border-radius: 11px;
                padding: 10px 12px;
                font-size: 14px;
                font-weight: 600;
                min-height: 30px;
            }
            QPushButton[class="sectionButton"][open="true"] {
                background: #1f2630;
                border: 1px solid #3a4553;
            }
            QFrame[class="sectionContent"] {
                background: #171c24;
                border: 1px solid #2c3542;
                border-radius: 11px;
            }
            QScrollArea#groupTabsScroll {
                background: #0e1116;
                border: 0;
            }
            QWidget#groupsBar {
                background: #0e1116;
            }
            QPushButton[groupTab="true"] {
                background: #1f2630;
                border: 1px solid #3a4553;
                min-width: 130px;
                font-weight: 600;
            }
            QPushButton[groupTab="true"][active="true"] {
                background: #1d4f8c;
                border: 1px solid #4b8fd8;
            }
            QTableView#accountTable {
                background: #171c24;
                alternate-background-color: #171c24;
                color: #e9f0f8;
                border: 1px solid #2c3542;
                border-radius: 11px;
                selection-background-color: #213e5f;
                selection-color: #ffffff;
            }
            QTableView#accountTable::item {
                padding: 6px 8px;
                border-bottom: 1px solid #232c37;
            }
            QHeaderView::section {
                background: #212934;
                color: #93a1b4;
                border: 0;
                border-right: 1px solid #2c3542;
                border-bottom: 1px solid #2c3542;
                padding: 7px 8px;
                font-size: 12px;
                font-weight: 600;
            }
            QLabel[class="statusBadge"] {
                background: #1f2630;
                color: #93a1b4;
                border: 1px solid #3a4553;
                border-radius: 9px;
                padding: 2px 7px;
                margin: 6px 5px;
                font-size: 11px;
                font-weight: 600;
            }
            QLabel[class="statusBadge"][tone="ok"] {
                background: #16281f; color: #8fdcb8; border-color: #2c6c4c;
            }
            QLabel[class="statusBadge"][tone="warn"] {
                background: #2a2114; color: #f0cf90; border-color: #6d5520;
            }
            QLabel[class="statusBadge"][tone="bad"] {
                background: #2a1817; color: #f0aaa2; border-color: #6d3630;
            }
            QLabel[class="statusBadge"][tone="info"] {
                background: #16232f; color: #9fc9ef; border-color: #2a5480;
            }
            QFrame#logsPanel {
                background: #090d12;
                border: 1px solid #212a35;
                border-radius: 11px;
            }
            QLabel#logsTitle {
                color: #93a1b4;
                font-size: 12px;
                font-weight: 600;
            }
            QPlainTextEdit#logBox {
                background: #090d12;
                color: #e9f0f8;
                border: 0;
                padding: 8px;
                font-family: Consolas, "Cascadia Mono", monospace;
                font-size: 12px;
                selection-background-color: #2f73bc;
            }
            QScrollBar:vertical {
                background: #252a31;
                width: 12px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #586575;
                border-radius: 6px;
                min-height: 24px;
            }
            QScrollBar:horizontal {
                background: #252a31;
                height: 10px;
                margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #586575;
                border-radius: 5px;
                min-width: 24px;
            }
            """
        for source, target in {
            "#0e1116": TOKENS.background,
            "#171c24": TOKENS.panel,
            "#1f2630": TOKENS.panel_secondary,
            "#2c3542": TOKENS.border,
            "#e9f0f8": TOKENS.text,
            "#93a1b4": TOKENS.muted,
            "#2f73bc": TOKENS.primary,
            "#2c6c4c": TOKENS.success,
            "#6d5520": TOKENS.warning,
            "#6d3630": TOKENS.danger,
        }.items():
            stylesheet = stylesheet.replace(source, target)
        self.setStyleSheet(stylesheet)

    def active_group(self) -> str:
        return self.group_name or "inbox"

    def entry_value(self, entry: QLineEdit) -> str:
        return entry.text().strip()

    def paste_to_entry(self, entry: QLineEdit) -> None:
        text = QApplication.clipboard().text()
        if not text:
            self.log("Clipboard is empty or unavailable.")
            return
        entry.setText(text)

    def prompt_group_name(self, title: str, label: str) -> str:
        value, accepted = QInputDialog.getText(self, title, label)
        if not accepted:
            return ""
        return value.strip()

    def log(
        self,
        message: str,
        group: str | None = None,
        *,
        severity: Severity = "info",
        account: str = "",
        operation: str = "ui",
    ) -> None:
        target_group = group or self.active_group()
        entry = UiLogEntry.create(
            message,
            severity=severity,
            group=target_group,
            account=account,
            operation=operation,
        )
        self.ui_log.log(
            {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}[severity],
            "[%s] %s", target_group, message,
        )
        entries = self.group_logs.setdefault(target_group, [])
        entries.append(entry.format_text())
        self.structured_logs.append(entry.to_dict())
        if len(entries) > 500:
            del entries[:-500]
        if len(self.structured_logs) > 500:
            del self.structured_logs[:-500]
        if target_group == self.active_group():
            self.log_box.appendPlainText(entry.format_text())

    def render_group_log(self) -> None:
        self.log_title.setText(f"ACTIVITY  ·  {self.active_group()}")
        self.log_box.clear()
        for message in self.group_logs.get(self.active_group(), []):
            self.log_box.appendPlainText(message)




    def switch_group(self, group: str) -> None:
        self.group_name = group or "inbox"
        self.refresh_accounts()
        self.render_group_log()

    def refresh_accounts(self) -> None:
        selected = set(self.selected_account_ids()) if hasattr(self, "table") else set()
        accounts = self.filtered_accounts(
            self.accounts.list_accounts(group=self.active_group())
        )
        rows: list[tuple[str, list[str]]] = []
        for account in accounts:
            values = [
                self.format_added(account.created_at),
                format_phone_with_region(account.phone, account.label),
                self.spamblock_status(account),
                self.login_mail_status(account),
                self.cloud_password_status(account),
                self.username_status(account),
                self.proxy_status(account),
                self.sleep_status(account),
                self.registration_status(account),
                "yes" if account.has_profile() else "no",
            ]
            rows.append((account.id, [str(value) for value in values]))
        self.table.setUpdatesEnabled(False)
        self.table_model.set_rows(rows)
        self.table.clearSelection()
        for row, (account_id, _values) in enumerate(rows):
            if account_id in selected:
                self.table.selectRow(row)
        self.table.setUpdatesEnabled(True)
        self.update_header_status()

    def update_header_status(self) -> None:
        if not hasattr(self, "accounts_pill"):
            return
        all_accounts = self.accounts.list_accounts(group=self.active_group())
        accounts = self.filtered_accounts(all_accounts)
        selected = len(self.selected_account_ids()) if hasattr(self, "table") else 0
        assigned = sum(bool(account.proxy or account.metadata.get("proxy")) for account in accounts)
        self.header_title.setText("Uznik MultiTool")
        self._set_status_pill(
            self.accounts_pill,
            f"Accounts: {len(accounts)}/{len(all_accounts)}" if self.account_filter.active else f"Accounts: {len(accounts)}",
            "ok" if accounts else "neutral",
        )
        self._set_status_pill(
            self.selected_pill,
            f"Selected: {selected}",
            "info" if selected else "neutral",
        )
        if hasattr(self, "selection_bar"):
            self.selection_bar_label.setText(f"Selected: {selected}")
            self.selection_bar.setVisible(selected > 0)
            self.open_account_button.setVisible(selected == 1)
        self._set_status_pill(
            self.proxy_pill,
            (
                f"VPN exits: {self.proxy_active_count}/{self.proxy_total_count} online"
                f"  ·  assigned {assigned}/{len(accounts)}"
            ),
            "ok" if self.proxy_active_count else "warn",
        )
        self.ui_state.update(
            active_group=self.active_group(),
            selected_accounts=self.selected_account_ids(),
            active_task=self.current_task_id,
            proxy_pool={"active": self.proxy_active_count, "total": self.proxy_total_count},
        )

    def refresh_proxy_dashboard(self) -> None:
        from modules.proxy_manager import ProxyPool

        async def read_count() -> tuple[int, int]:
            pool = ProxyPool(self.config.proxy_pool_db)
            active = await pool.count("active") + await pool.count("secondary") + await pool.count("slow")
            return active, await pool.count_all()

        def done(result: object | None, error: BaseException | None) -> None:
            if error is None:
                active, total = result if isinstance(result, tuple) else (0, 0)
                self.signals.proxy_count_ready.emit(int(active), int(total))

        self.worker.submit(read_count(), done)

    def on_proxy_count_ready(self, active: int, total: int) -> None:
        self.proxy_active_count = active
        self.proxy_total_count = total
        self.update_header_status()

    def selected_account_ids(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for index in self.table.selectionModel().selectedRows():
            account_id = str(index.data(Qt.UserRole))
            if account_id and account_id not in seen:
                ids.append(account_id)
                seen.add(account_id)
        return ids

    def selected_account_id(self) -> str | None:
        selected = self.selected_account_ids()
        return selected[0] if selected else None

    def has_selection(self) -> bool:
        return len(self.selected_account_ids()) > 0

    def get_selected_accounts(self) -> list[AccountRecord]:
        ids = set(self.selected_account_ids())
        return [a for a in self.group_accounts() if a.id in ids]

    def group_accounts(self):
        return self.filtered_accounts(
            self.accounts.list_accounts(group=self.active_group(), enabled_only=True)
        )

    def filtered_accounts(self, accounts):
        items = list(accounts)
        states = {account.id: self.scheduler.state(account.id) for account in items} if self.account_filter.sleep != "any" else None
        return filter_accounts(
            items,
            self.account_filter,
            sleep_states=states,
            global_proxy=self.config.global_proxy,
        )

    def format_added(self, value: str) -> str:
        if not value:
            return "unknown"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value[:16]

    def account_validity(self, account) -> str:
        status = str(account.metadata.get("health_status") or "").lower()
        if not account.enabled:
            return "disabled"
        if status == "valid":
            return "valid"
        if status in {"invalid", "error"}:
            return status
        if account.metadata.get("last_error"):
            return "error"
        return "unknown"

    def login_mail_status(self, account) -> str:
        return login_mail_state(account)

    def cloud_password_status(self, account) -> str:
        value = account.metadata.get("cloud_password")
        if value is True:
            return "yes"
        if value is False:
            return "no"
        return "unknown"

    def spamblock_status(self, account) -> str:
        status = str(account.metadata.get("spamblock_status") or "").lower()
        until = str(account.metadata.get("spamblock_until") or "")
        if status == "clear":
            return "none"
        if status == "forever":
            return "forever"
        if status == "temporary" and until:
            try:
                parsed = datetime.fromisoformat(until.replace("Z", "+00:00"))
                return f"until {parsed.strftime('%Y-%m-%d')}"
            except ValueError:
                return f"until {until[:10]}"
        if status == "limited":
            return "limited"
        return "unknown"

    def username_status(self, account) -> str:
        username = account.username or account.metadata.get("applied_username")
        return f"@{username}" if username else "none"

    def proxy_status(self, account) -> str:
        proxy = effective_proxy(account, self.config.global_proxy)
        if not proxy:
            return "none"
        latency = account.metadata.get("proxy_latency")
        checked = account.metadata.get("proxy_checked_at", "")
        try:
            from urllib.parse import urlparse
            parsed = urlparse(proxy)
            base = f"{parsed.scheme or 'socks5'}://{parsed.hostname or '?'}"
            if latency is not None:
                return f"{base} ({latency}s)"
            return base
        except Exception:
            return "unknown"

    def sleep_status(self, account) -> str:
        return self.scheduler.sleep_status(account.id)

    def refresh_sleep_states(self) -> None:
        if self.account_filter.sleep != "any":
            self.refresh_accounts()
            return
        for row, (account_id, values) in enumerate(self.table_model.rows):
            status = self.scheduler.sleep_status(account_id)
            if values[7] != status:
                values[7] = status
                index = self.table_model.index(row, 7)
                self.table_model.dataChanged.emit(index, index)

    def apply_sleep_schedule(self) -> None:
        accounts = self.get_selected_accounts() or self.group_accounts()
        if not self.confirm("Apply sleep schedule?", f"Set timezone for {len(accounts)} filtered/selected account(s)? Sleep hours and enabled state apply to all background tasks."):
            return
        try:
            self.scheduler.configure(accounts, timezone=self.sleep_zone_entry.text().strip(),
                                     start=self.sleep_start_hour.value(), end=self.sleep_end_hour.value(),
                                     enabled=self.sleep_enabled.isChecked())
        except (ValueError, KeyError) as exc:
            QMessageBox.warning(self, "Invalid sleep schedule", f"{exc}\nCheck the IANA timezone and install requirements (tzdata).")
            return
        self.log(f"Sleep schedule saved for {len(accounts)} account(s).")
        self.refresh_accounts()

    def registration_status(self, account) -> str:
        return account.metadata.get("registration") or ""

    def create_group(self) -> None:
        group = self.prompt_group_name("Create group", "New group name:")
        if not group:
            self.log("Enter new group name.")
            return
        group = self.accounts.create_group(group)
        self.group_name = group
        self.log(f"Group ready: {group}")
        self.refresh_accounts()
        self.render_group_log()

    def move_selected(self) -> None:
        account_ids = self.selected_account_ids()
        if not account_ids:
            self.log("Select one or more accounts first.")
            return
        group = self.prompt_group_name("Add selected", "Target group name:")
        if not group:
            self.log("Enter target group name.")
            return
        group = self.accounts.create_group(group)
        self.accounts.set_group(account_ids, group)
        self.log(f"Added {len(account_ids)} account(s) to {group}.")
        self.refresh_accounts()

    def clear_active_group(self) -> None:
        group = self.active_group()
        if group == "inbox":
            self.log("Inbox cannot be cleared this way.")
            return
        if not self.confirm("Clear group members?", f"Remove all accounts from group '{group}'? Accounts stay in inbox and other groups."):
            return
        moved = self.accounts.clear_group(group, target_group="inbox")
        self.group_name = "inbox"
        self.log(f"Cleared group {group}: removed {moved} membership(s).", group=group)
        self.refresh_accounts()
        self.render_group_log()

    def delete_active_group(self) -> None:
        group = self.active_group()
        if group == "inbox":
            self.log("Inbox cannot be deleted.")
            return
        if not self.confirm("Delete group?", f"Delete group '{group}'? Accounts stay in inbox and other groups."):
            return
        moved = self.accounts.delete_group(group, target_group="inbox")
        self.group_name = "inbox"
        self.log(f"Deleted group {group}: removed {moved} membership(s).", group=group)
        self.refresh_accounts()
        self.render_group_log()

    def remove_selected_from_group(self) -> None:
        group = self.active_group()
        if group == "inbox":
            self.log("Select a created group first.")
            return
        account_ids = self.selected_account_ids()
        if not account_ids:
            self.log("Select one or more accounts first.")
            return
        removed = self.accounts.remove_from_group(account_ids, group)
        self.log(f"Removed {removed} account(s) from {group}.")
        self.refresh_accounts()

    def delete_selected_accounts(self) -> None:
        account_ids = self.selected_account_ids()
        if not account_ids:
            self.log("Select one or more accounts first.")
            return
        accounts = [self.accounts.get_account(account_id) for account_id in account_ids]
        preview = "\n".join(
            f"- {account.label or account.phone or account.id} ({account.id})"
            for account in accounts[:8]
        )
        if len(accounts) > 8:
            preview += f"\n- ... and {len(accounts) - 8} more"
        summary = build_bulk_operation_preview(
            "Delete accounts",
            self.active_group(),
            accounts,
            sleeping_ids={account.id for account in accounts if self.scheduler.is_sleeping(account.id)},
        ).format_text()
        if not self.confirm(
            "Delete selected accounts?",
            (
                f"{summary}\n\n"
                "This removes database records, internal session files, and known matching files in imports.\n\n"
                f"{preview}"
            ),
        ):
            return
        removed = self.accounts.delete_accounts(account_ids, delete_sessions=True)
        self.log(f"Deleted {removed} selected account(s).")
        self.refresh_accounts()

    def smart_group(self, mode: str) -> None:
        accounts = self.filtered_accounts(
            self.accounts.list_accounts(group=self.active_group())
        )
        if not accounts:
            self.log("No accounts in selected group.")
            return
        buckets: dict[str, list[str]] = {}
        for account in accounts:
            group = self.smart_group_name(mode, account)
            buckets.setdefault(group, []).append(account.id)
        details = ", ".join(f"{group}={len(ids)}" for group, ids in sorted(buckets.items()))
        if not self.confirm(
            "Smart grouping?",
            f"Add {len(accounts)} account(s) from current view into {len(buckets)} generated group(s)?\n\n{details}",
        ):
            return
        for group, account_ids in buckets.items():
            self.accounts.set_group(account_ids, group)
        self.log(f"Smart grouping by {mode}: {details}")
        self.refresh_accounts()

    def smart_group_name(self, mode: str, account) -> str:
        if mode == "phone":
            return phone_group_name(account.phone, account.label)
        if mode == "cloud_password":
            return f"cloud_password_{self.cloud_password_status(account).replace(' ', '_')}"
        if mode == "login_mail":
            return f"login_mail_{self.login_mail_status(account).replace(' ', '_')}"
        if mode == "username":
            return "username_yes" if self.username_status(account) != "none" else "username_none"
        if mode == "added_date":
            return "added_recent_3d" if self.is_recent_account(account.created_at, days=3) else "added_older"
        if mode == "spamblock":
            status = str(account.metadata.get("spamblock_status") or "unknown").lower()
            if status == "temporary":
                until = str(account.metadata.get("spamblock_until") or "")
                if until:
                    return f"spamblock_until_{until[:10].replace('-', '_')}"
                return "spamblock_limited"
            if status == "forever":
                return "spamblock_forever"
            if status == "clear":
                return "spamblock_none"
            if status == "limited":
                return "spamblock_limited"
            return "spamblock_unknown"
        return "smart_unknown"

    def is_recent_account(self, created_at: str, days: int) -> bool:
        if not created_at:
            return False
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed >= datetime.now(timezone.utc) - timedelta(days=days)

    def start_scenario_recording(self) -> None:
        if self.recording_scenario:
            self.log("Scenario recording is already active.")
            return
        self.recording_steps = []
        self.recording_scenario = True
        self.log("Scenario recording started. Use normal buttons; supported actions will be captured.")
        self.render_scenario_preview()

    def stop_and_save_scenario(self) -> None:
        if not self.recording_scenario and not self.recording_steps:
            self.log("No scenario draft to save.")
            return
        name = self.entry_value(self.scenario_name_entry)
        if not name:
            self.log("Enter scenario name before saving.")
            return
        if not self.recording_steps:
            self.log("Scenario draft has no recorded steps.")
            return
        scenario = Scenario(name=name, steps=list(self.recording_steps))
        path = self.scenarios.save(scenario)
        self.recording_scenario = False
        self.recording_steps = []
        self.log(f"Scenario saved: {scenario.name} ({len(scenario.steps)} step(s)) -> {path}")
        self.refresh_scenarios(select_name=scenario.name)

    def clear_scenario_draft(self) -> None:
        self.recording_scenario = False
        self.recording_steps = []
        self.log("Scenario draft cleared.")
        self.render_scenario_preview()

    def refresh_scenarios(self, select_name: str | None = None) -> None:
        current = select_name or (self.scenario_select.currentText() if hasattr(self, "scenario_select") else "")
        self.scenario_select.blockSignals(True)
        self.scenario_select.clear()
        names = self.scenarios.list_names()
        self.scenario_select.addItems(names)
        if current:
            index = self.scenario_select.findText(current)
            if index >= 0:
                self.scenario_select.setCurrentIndex(index)
        self.scenario_select.blockSignals(False)
        self.render_scenario_preview()

    def render_scenario_preview(self) -> None:
        if not hasattr(self, "scenario_preview"):
            return
        if self.recording_scenario or self.recording_steps:
            title = "Recording draft" if self.recording_scenario else "Draft"
            lines = [f"{title}: {len(self.recording_steps)} step(s)"]
            steps = self.recording_steps
        else:
            name = self.scenario_select.currentText().strip()
            if not name:
                self.scenario_preview.setPlainText("No saved scenarios yet.")
                return
            try:
                scenario = self.scenarios.load(name)
            except Exception as exc:
                self.scenario_preview.setPlainText(f"Cannot load scenario: {exc}")
                return
            lines = [f"{scenario.name}: {len(scenario.steps)} step(s)"]
            steps = scenario.steps
        for index, step in enumerate(steps, start=1):
            group = step.params.get("group", "?")
            lines.append(f"{index}. [{group}] {step.label}")
        self.scenario_preview.setPlainText("\n".join(lines))

    def record_scenario_step(self, action: str, label: str, params: dict[str, object]) -> None:
        if not self.recording_scenario or self.replaying_scenario:
            return
        captured = dict(params)
        captured.setdefault("group", self.active_group())
        captured.setdefault("account_ids", [account.id for account in self.group_accounts()])
        captured.setdefault("account_filter", self.account_filter.to_dict())
        step = ScenarioStep(action=action, label=label, params=captured)
        self.recording_steps.append(step)
        self.log(f"Recorded scenario step #{len(self.recording_steps)}: {label}")
        self.render_scenario_preview()

    def scenario_not_recorded(self, reason: str) -> None:
        if self.recording_scenario:
            self.log(f"Scenario recorder skipped this action: {reason}")

    def selected_scenario(self) -> Scenario | None:
        name = self.scenario_select.currentText().strip()
        if not name:
            self.log("Select a scenario first.")
            return None
        try:
            return self.scenarios.load(name)
        except Exception as exc:
            self.log(f"Cannot load scenario: {exc}")
            return None

    def run_selected_scenario(self) -> None:
        self.run_selected_scenario_with_group_policy(force_active_group=False)

    def run_selected_scenario_on_active_group(self) -> None:
        self.run_selected_scenario_with_group_policy(force_active_group=True)

    def run_selected_scenario_with_group_policy(self, *, force_active_group: bool) -> None:
        scenario = self.selected_scenario()
        if not scenario:
            return
        if not scenario.steps:
            self.log("Selected scenario has no steps.")
            return
        run_group = self.active_group()
        runnable = self.prepare_scenario_for_run(scenario, run_group, force_active_group=force_active_group)
        if not runnable:
            return
        mode = f"active group '{run_group}'" if force_active_group else "saved groups"
        groups = sorted({str(step.params.get("group") or "inbox") for step in runnable.steps})
        group_counts = ", ".join(f"step {index}: {len(self.scenario_accounts(step.params))}" for index, step in enumerate(runnable.steps, 1))
        if not self.confirm(
            "Run scenario?",
            f"Run '{scenario.name}' with {len(runnable.steps)} step(s) using {mode}?\n\nAccounts: {group_counts}",
        ):
            return
        self.start_managed_task(
            f"scenario:{runnable.name}",
            lambda stop: self.execute_scenario(runnable, stop, run_group),
        )

    def prepare_scenario_for_run(
        self,
        scenario: Scenario,
        active_group: str,
        *,
        force_active_group: bool,
    ) -> Scenario | None:
        if active_group not in self.accounts.groups():
            self.log(f"Active group does not exist: {active_group}")
            return None
        active_accounts = self.accounts.list_accounts(group=active_group, enabled_only=True)
        if force_active_group and not active_accounts:
            self.log(f"No enabled accounts in active group '{active_group}'.")
            return None

        prepared_steps: list[ScenarioStep] = []
        replaced: list[str] = []
        for step in scenario.steps:
            params = dict(step.params)
            saved_group = str(params.get("group") or "inbox")
            saved_accounts = self.accounts.list_accounts(group=saved_group, enabled_only=True)
            if force_active_group:
                if saved_group != active_group:
                    replaced.append(saved_group)
                params["group"] = active_group
                params["account_ids"] = [a.id for a in self.group_accounts()]
                params["account_filter"] = self.account_filter.to_dict()
            elif not saved_accounts:
                self.log(
                    f"Scenario '{scenario.name}' refers to missing or empty group '{saved_group}'. "
                    f"Open that group first or use 'Run active'."
                )
                return None
            if not force_active_group:
                params.setdefault("account_filter", self.account_filter.to_dict())
                if "account_ids" not in params:
                    params["account_ids"] = [a.id for a in self.scenario_accounts(params)]
            prepared_steps.append(ScenarioStep(action=step.action, label=step.label, params=params))

        if replaced:
            unique = ", ".join(sorted(set(replaced)))
            self.log(f"Scenario active-group override: {unique} -> {active_group}")
        return Scenario(
            name=scenario.name,
            description=scenario.description,
            created_at=scenario.created_at,
            updated_at=scenario.updated_at,
            steps=prepared_steps,
        )

    def delete_selected_scenario(self) -> None:
        name = self.scenario_select.currentText().strip()
        if not name:
            self.log("Select a scenario first.")
            return
        if not self.confirm("Delete scenario?", f"Delete saved scenario '{name}'?"):
            return
        if self.scenarios.delete(name):
            self.log(f"Scenario deleted: {name}")
        else:
            self.log(f"Scenario was not found: {name}")
        self.refresh_scenarios()

    async def execute_scenario(
        self,
        scenario: Scenario,
        stop: asyncio.Event,
        run_group: str,
    ) -> ActionResult:
        self.replaying_scenario = True
        result = ActionResult()
        try:
            for index, step in enumerate(scenario.steps, start=1):
                if stop.is_set():
                    self.emit_scenario_log(f"Scenario stopped before step {index}.", run_group, run_group)
                    break
                group = str(step.params.get("group") or "inbox")
                self.emit_scenario_log(
                    f"Scenario '{scenario.name}' step {index}/{len(scenario.steps)}: {step.label}",
                    group,
                    run_group,
                )
                try:
                    step_result = await self.execute_scenario_step(step, stop)
                except Exception as exc:
                    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    result.add_error(f"step_{index}:{step.action}", str(exc))
                    self.ui_log.error(
                        "[%s] Scenario step failed (%s): %s\n%s",
                        group,
                        step.action,
                        exc,
                        details,
                    )
                    continue
                if isinstance(step_result, ActionResult):
                    result.ok += step_result.ok
                    result.errors += step_result.errors
                    for account_id, detail in step_result.details.items():
                        result.details[f"step_{index}:{account_id}"] = detail
                elif isinstance(step_result, TelegramCodeScanResult):
                    result.ok += step_result.ok
                    result.errors += step_result.errors
                    self.emit_scenario_log(
                        f"Telegram codes: accounts_ok={step_result.ok}, "
                        f"errors={step_result.errors}, codes={len(step_result.codes)}",
                        group,
                        run_group,
                    )
                    for item in step_result.codes[:50]:
                        stamp = (
                            datetime.fromtimestamp(item.received_at).strftime("%H:%M:%S")
                            if item.received_at
                            else "?"
                        )
                        self.emit_scenario_log(
                            f"[{stamp}] {item.label} ({item.account_id}) | code={item.code}",
                            group,
                            run_group,
                        )
                elif all(hasattr(step_result, attr) for attr in ("copied", "male", "female", "unknown", "skipped")):
                    result.add_ok(f"step_{index}")
                    self.emit_scenario_log(
                        f"Avatar pack: copied={step_result.copied}, male={step_result.male}, "
                        f"female={step_result.female}, unknown={step_result.unknown}, "
                        f"skipped={step_result.skipped}",
                        group,
                        run_group,
                    )
                else:
                    result.add_ok(f"step_{index}")
            return result
        finally:
            self.replaying_scenario = False

    def emit_scenario_log(self, message: str, step_group: str, run_group: str) -> None:
        self.signals.log_message.emit(message, step_group)
        if run_group != step_group:
            self.signals.log_message.emit(message, run_group)

    def scenario_accounts(self, params: dict) -> list:
        accounts = self.accounts.list_accounts(group=str(params.get("group") or "inbox"), enabled_only=True)
        ids = params.get("account_ids")
        if ids is not None:
            allowed = set(ids)
            accounts = [a for a in accounts if a.id in allowed]
        spec = AccountFilter.from_dict(params.get("account_filter", self.account_filter.to_dict()))
        states = {a.id: self.scheduler.state(a.id) for a in accounts} if spec.sleep != "any" else None
        return filter_accounts(accounts, spec, sleep_states=states, global_proxy=self.config.global_proxy)

    async def execute_scenario_step(self, step: ScenarioStep, stop: asyncio.Event) -> object:
        params = step.params
        group = str(params.get("group") or "inbox")
        accounts = self.scenario_accounts(params)
        action = step.action

        if action in {"profile.generate", "profile.split_unknown"}:
            if action == "profile.generate":
                return await asyncio.to_thread(self.generate_profiles_for_group, group)
            return await asyncio.to_thread(split_unknown_avatars, self.avatar_pack_dir, 0.5, True)

        if not accounts:
            raise RuntimeError(f"No enabled accounts in group '{group}'.")

        if action == "chat.join":
            return await ChatActionService(self.config).join(accounts, str(params.get("target") or ""))
        if action == "chat.leave":
            return await ChatActionService(self.config).leave(accounts, str(params.get("target") or ""))
        if action == "chat.open_link":
            return await ChatActionService(self.config).open_link(accounts, str(params.get("target") or ""))
        if action == "chat.view_post":
            return await ChatActionService(self.config).view_post(accounts, str(params.get("target") or ""))
        if action == "chat.random_reaction":
            return await ChatActionService(self.config).random_reaction(accounts, str(params.get("target") or ""))
        if action == "chat.set_reaction":
            raw_reaction = params.get("reaction")
            if not isinstance(raw_reaction, dict):
                raise RuntimeError("Scenario reaction payload is missing.")
            reaction = ReactionChoice.from_dict(raw_reaction)
            return await ChatActionService(self.config).set_reaction(
                accounts,
                str(params.get("target") or ""),
                reaction,
            )
        if action == "ai.run":
            run_config = AIConversationConfig(
                chat=str(params.get("chat") or ""),
                topic=str(params.get("topic") or ""),
                message_count=int(params.get("count") or 10),
                provider=self.config.llm_provider,
                model=self.config.llm_model,
                api_key=self.config.llm_api_key,
                base_url=self.config.llm_base_url,
            )
            return await AICompanion(self.config).run(accounts, run_config, stop_event=stop)
        if action == "session.check":
            return await SessionHealthService(self.config).validate(accounts)
        if action == "session.spamblock":
            return await SpamBlockService(self.config).check(accounts)
        if action == "telegram.codes":
            return await TelegramCodeService(self.config).scan_recent(accounts, seconds=60, limit=25)
        if action == "profile.download_avatars":
            url = str(params.get("avatar_url") or "")
            if not url:
                raise RuntimeError("Avatar Drive URL is empty.")
            return await asyncio.to_thread(
                download_google_drive_avatar_pack,
                url,
                Path("assets/avatar_packs_raw"),
                self.avatar_pack_dir,
            )
        if action.startswith("profile."):
            return await self.execute_profile_scenario_step(action, accounts)
        if action == "security.terminate_pc":
            return await AccountSecurityService(self.config).terminate_other_sessions(accounts, desktop_only=True)
        if action == "security.terminate_other":
            return await AccountSecurityService(self.config).terminate_other_sessions(accounts, desktop_only=False)
        if action in ("security.delete_passkeys", "passkeys.delete"):
            return await AccountSecurityService(self.config).delete_passkeys(accounts)
        if action == "passkeys.add":
            return await AccountSecurityService(self.config).add_passkeys(accounts)
        if action == "passkeys.restore":
            return await AccountSecurityService(self.config).restore_passkeys(accounts)
        raise RuntimeError(f"Unsupported scenario action: {action}")

    async def execute_profile_scenario_step(self, action: str, accounts: list) -> ActionResult:
        customizer = ProfileCustomizer(self.config)
        if action == "profile.set_usernames":
            usernames = [line.lstrip("@") for line in customizer.load_lines(self.usernames_file)]
            plan = ProfileUpdatePlan(usernames=usernames)
        elif action == "profile.clear_usernames":
            plan = ProfileUpdatePlan(usernames=[None] * len(accounts))
        elif action == "profile.set_profile":
            plan = ProfileUpdatePlan(
                first_names=customizer.load_optional_lines(self.first_names_file),
                last_names=customizer.load_optional_lines(self.last_names_file),
                bios=customizer.load_optional_lines(self.bios_file),
            )
        elif action == "profile.apply_plan":
            if not self.profile_plan_file.exists():
                raise RuntimeError("profile_plan.json does not exist.")
            plan = ProfileUpdatePlan(profile_plan=customizer.load_profile_plan(self.profile_plan_file))
        elif action == "profile.clear_bio":
            plan = ProfileUpdatePlan(bios=[""])
        elif action == "profile.set_avatars":
            plan = ProfileUpdatePlan(avatars_dir=self.avatars_dir)
        elif action == "profile.clear_avatars":
            plan = ProfileUpdatePlan(clear_avatars=True)
        elif action == "profile.clear_full":
            plan = ProfileUpdatePlan(
                usernames=[None] * len(accounts),
                last_names=[""],
                bios=[""],
                clear_avatars=True,
            )
        elif action == "profile.rollback":
            profiles = ProfileSnapshotService(self.config).load_latest_profiles({account.id for account in accounts})
            if not profiles:
                raise RuntimeError("No profile snapshot found.")
            plan = ProfileUpdatePlan(profile_plan=profiles)
        else:
            raise RuntimeError(f"Unsupported profile scenario action: {action}")
        ProfileSnapshotService(self.config).save_latest(accounts, action)
        return await customizer.apply(accounts, plan)

    def generate_profiles_for_group(self, group: str) -> None:
        accounts = self.accounts.list_accounts(group=group, enabled_only=True)
        if not accounts:
            raise RuntimeError(f"No enabled accounts in group '{group}'.")
        used_raw = read_json(self.config.data_dir / "avatar_usage.json", {"used": []})
        used_avatars = {str(item) for item in used_raw.get("used", []) if str(item).strip()}
        plan = write_profile_plan(
            accounts,
            self.avatar_pack_dir,
            self.profile_plan_file,
            used_avatars=used_avatars,
        )
        export_plan_text_files(plan, Path("templates"))

    def import_now(self, verbose: bool = True) -> None:
        imported, errors = self.sessions.import_new_from_inbox(group="inbox")
        if verbose:
            for path, error in list(errors.items())[:10]:
                self.log(f"Skipped {path}: {error}", group="inbox")
        if verbose or imported:
            self.refresh_accounts()

    def dragEnterEvent(self, event) -> None:  # Qt event type differs between PySide releases
        mime = event.mimeData()
        if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        from modules.session_onboarding import queue_external_sessions, session_sources

        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        sources = session_sources(paths)
        imported = 0
        for source in sources:
            if source.suffix.lower() != ".json":
                continue
            try:
                imported += len(self.sessions.import_path(source, group="inbox"))
            except Exception as exc:
                self.log(f"JSON import failed for {source.name}: {exc}", severity="warning")
        queued = queue_external_sessions(self.config, paths)
        if not sources:
            self.log("Drop ignored: add .session or JSON session files (or folders containing them).", severity="warning")
            event.ignore()
            return
        event.acceptProposedAction()
        if imported:
            self.log(f"Imported {imported} JSON session(s) locally; no new Telegram authorization requested.")
            self.refresh_accounts()
        if not queued:
            return
        self.log(f"Queued {len(queued)} external session(s) in imports/auth_input. Original files were not changed.")
        if self.confirm(
            "Process external sessions?",
            "The tool will create its own local session for each dropped file. "
            "It sends a Telegram login code through a verified VPN, tries to read it from the supplied session's 777000 chat, "
            "and asks you only when a code cannot be read automatically. Continue?",
        ):
            self.process_external_sessions(queued)

    def process_external_sessions(self, queued: list[Path] | None = None) -> None:
        from modules.session_onboarding import ForeignSessionOnboarding, OnboardingResult, pending_external_sessions

        if self.external_sessions_running:
            self.log("The external session queue is already being processed.")
            return

        if queued is None:
            queued = pending_external_sessions(self.config)
        if not queued:
            self.log("No queued external .session files. Drag files or a folder onto this window first.")
            return
        try:
            self.config.require_telegram_api()
        except RuntimeError as exc:
            self.log(str(exc), severity="warning")
            return
        self.external_sessions_running = True

        async def ask_for_input(phone: str, source: Path, *, password: bool = False) -> str | None:
            request = {"phone": phone, "source": source, "password": password, "event": threading.Event(), "value": ""}
            self.signals.auth_code_requested.emit(request)
            await asyncio.to_thread(request["event"].wait)
            return str(request["value"] or "")

        async def ask_for_code(phone: str, source: Path) -> str | None:
            return await ask_for_input(phone, source)

        async def ask_for_password(phone: str, source: Path) -> str | None:
            return await ask_for_input(phone, source, password=True)

        async def run(_stop: asyncio.Event):
            try:
                return await process_queue(_stop)
            finally:
                self.external_sessions_running = False

        async def process_queue(_stop: asyncio.Event):
            onboarding = ForeignSessionOnboarding(self.config)
            results = []
            for source in queued:
                if _stop.is_set():
                    break
                try:
                    result = await onboarding.process_one(source, ask_for_code, ask_for_password)
                except Exception as exc:
                    result = OnboardingResult(source=source, status="error", error=str(exc))
                results.append(result)
                if result.status == "authorized":
                    progress.mark_ok(result.phone or source.name)
                else:
                    progress.mark_error(result.phone or source.name)
            for result in results:
                severity: Severity = "info" if result.status == "authorized" else "warning"
                detail = f"external session {result.source.name}: {result.status}"
                if result.phone:
                    detail += f" · {result.phone}"
                if result.error:
                    detail += f" · {result.error}"
                self.signals.log_message.emit(detail, self.active_group())
            self.signals.refresh_requested.emit()
            return results

        progress = self.begin_operation_progress("External sessions", queued)
        self.log(f"External session queue: {len(queued)} file(s). Progress is shown in the top bar.")
        try:
            self.start_managed_task("process-external-sessions", run)
        except Exception:
            self.external_sessions_running = False
            raise

    def on_auth_code_requested(self, request: object) -> None:
        if not isinstance(request, dict):
            return
        phone = str(request.get("phone") or "")
        source = Path(str(request.get("source") or "session"))
        password = bool(request.get("password"))
        value, accepted = QInputDialog.getText(
            self,
            "Cloud password" if password else "Telegram login code",
            (f"Enter the cloud password for {phone}.\n\n" if password else f"Enter the 5–6 digit code for {phone}.\n\n")
            + f"Source: {source.name}.\nCancel leaves this source in the queue without deleting it.",
            QLineEdit.Password if password else QLineEdit.Normal,
        )
        request["value"] = (value.strip() if password else "".join(ch for ch in value if ch.isdigit())) if accepted else ""
        event = request.get("event")
        if isinstance(event, threading.Event):
            event.set()

    def auto_import_tick(self) -> None:
        self.import_now(verbose=False)
        self.refresh_external_session_queue()

    def open_external_session_folder(self) -> None:
        queue = self.config.import_dir / "auth_input"
        queue.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(queue)))

    def refresh_external_session_queue(self) -> None:
        from modules.session_onboarding import pending_external_sessions
        pending = pending_external_sessions(self.config)
        self.external_queue_label.setText(f"auth_input: {len(pending)} pending · creates a new local session")
        paths = {str(path.resolve()) for path in pending}
        fresh = paths - self._announced_external_sources
        self._announced_external_sources.intersection_update(paths)
        if not fresh or self.external_sessions_running:
            return
        if self.external_auto_check.isChecked():
            if not self.config.api_id or not self.config.api_hash:
                return
            self._announced_external_sources.update(fresh)
            self.process_external_sessions([path for path in pending if str(path.resolve()) in fresh])
        else:
            self._announced_external_sources.update(fresh)
            self.log(f"Found {len(fresh)} external session(s). Use Actions → External sessions → Process dropped sessions.")

    def start_managed_task(
        self,
        name: str,
        factory: Callable[[asyncio.Event], Awaitable[object]],
        task_group: str | None = None,
    ) -> None:
        if self._task_start_pending is True or QtDesktopApp.foreground_task_busy(self):
            if name == "process-external-sessions":
                self.external_sessions_running = False
            self.log("Another task is running or starting. Use Stop current before starting a new task.")
            return
        if self.current_progress is None:
            self.begin_indeterminate_progress(self._task_progress_label(name))
        self._task_start_pending = True
        task_group = task_group or self.active_group()

        async def starter():
            stopped_warmup = await self.tasks.stop_by_name_prefix("warmup")
            if stopped_warmup:
                self.signals.log_message.emit(
                    f"Stopped warmup before {name}: {stopped_warmup} task(s).",
                    task_group,
                )
            stopped_online = await self.tasks.stop_by_name_prefix("online-mode")
            if stopped_online:
                self.signals.log_message.emit(
                    f"Stopped online-mode before {name}: {stopped_online} task(s).",
                    task_group,
                )
            task_id = self.tasks.start(name, factory)
            task = self.tasks.tasks[task_id]
            # Queue Started before a fast validation failure can queue Failed.
            self.signals.task_started.emit(name, task_id, None, task_group)

            async def watch() -> None:
                try:
                    result = await task
                except asyncio.CancelledError:
                    self.signals.task_stopped.emit(name, task_group, task_id)
                    return
                except Exception as exc:
                    details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    self.signals.task_failed.emit(name, details, task_group, task_id)
                    self.signals.refresh_requested.emit()
                    return
                self.signals.task_finished.emit(name, result, task_group, task_id)
                self.signals.refresh_requested.emit()

            asyncio.create_task(watch())
            return task_id

        self.worker.submit(
            starter(),
            lambda result, error: self.signals.task_started.emit(name, result, error, task_group) if error else None,
        )

    def task_started(
        self,
        name: str,
        result: object | None,
        error: BaseException | None,
        group: str,
    ) -> None:
        self._task_start_pending = False
        if error:
            if name == "process-external-sessions":
                self.external_sessions_running = False
            if not self.current_task_id:
                self.clear_progress()
            self.log(f"{name} failed to start: {error}", group=group)
            self._set_status_pill(self.task_pill, "Task: failed", "bad")
            return
        self.current_task_id = str(result)
        self.current_task_name = name
        self._set_status_pill(self.task_pill, f"Task: {name}", "warn")
        self.log(f"Started {name}: {result}", group=group)

    def on_progress_updated(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            return
        token = snapshot.get("progress_token")
        if token is not None and (self.current_progress is None or token != id(self.current_progress)):
            return
        total = int(snapshot.get("total", 0) or 0)
        completed = int(snapshot.get("completed", 0) or 0)
        operation = str(snapshot.get("operation") or "Working")
        determinate = str(snapshot.get("mode") or "determinate") != "indeterminate" and total > 0
        if determinate:
            percent = min(100, round((completed / total) * 100))
            errors = int(snapshot.get("errors", 0) or 0)
            detail = f"{operation}: {completed}/{total} · {percent}%"
            if errors:
                detail += f" · errors {errors}"
            self._set_status_pill(self.progress_pill, detail, "warn" if errors else "info")
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(min(completed, total))
        else:
            self._set_status_pill(self.progress_pill, f"{operation}: working…", "info")
            self.progress_bar.setRange(0, 0)
        self.progress_pill.setVisible(True)
        self.progress_bar.setVisible(True)
        self.ui_state.update(operation_progress=dict(snapshot))

    def clear_progress(self) -> None:
        self.current_progress = None
        self.progress_pill.setVisible(False)
        self.progress_bar.setVisible(False)
        self.ui_state.update(operation_progress={})

    @staticmethod
    def _task_progress_label(name: str) -> str:
        value = str(name).replace(":", " · ").replace("-", " ").strip()
        return value[:1].upper() + value[1:] if value else "Working"

    def begin_indeterminate_progress(self, operation: str) -> OperationProgress:
        progress = OperationProgress.indeterminate(
            operation,
            on_update=lambda snapshot: self.publish_progress(progress, snapshot),
        )
        if not QtDesktopApp.foreground_task_busy(self) and self._task_start_pending is not True:
            self.current_progress = progress
            self.publish_progress(progress, progress.to_dict())
        return progress

    def begin_operation_progress(self, operation: str, accounts) -> OperationProgress:
        progress = OperationProgress(
            operation,
            len(accounts),
            on_update=lambda snapshot: self.publish_progress(progress, snapshot),
        )
        if not QtDesktopApp.foreground_task_busy(self) and self._task_start_pending is not True:
            self.current_progress = progress
            self.publish_progress(progress, progress.to_dict())
        return progress

    def foreground_task_busy(self) -> bool:
        return isinstance(self.current_task_id, str) and bool(self.current_task_id) and self.current_task_name not in {"warmup", "online-mode"}

    def publish_progress(self, progress: OperationProgress, snapshot: dict) -> None:
        if self.current_progress is progress:
            self.signals.progress_updated.emit({**snapshot, "progress_token": id(progress)})

    def clear_task_state(self, task_id: str, *, failed: bool = False) -> None:
        if (task_id and task_id != self.current_task_id) or self._task_start_pending is True:
            return
        self.current_task_id = ""
        self.current_task_name = ""
        self.clear_progress()
        self._set_status_pill(self.task_pill, "Task: failed" if failed else "Task: idle", "bad" if failed else "ok")

    def task_failed(self, name: str, error: str, group: str, task_id: str = "") -> None:
        QtDesktopApp.clear_task_state(self, task_id, failed=True)
        self.ui_log.error("[%s] %s failed: %s", group, name, error)
        summary = error.strip().splitlines()[-1] if error.strip() else "Unknown error"
        self.log(f"{name} failed: {summary}", group=group, severity="error")
        if name in {"change-login-email", "bind-recovery-email"}:
            QMessageBox.warning(self, "Email operation failed", summary)

    def task_stopped(self, name: str, group: str, task_id: str = "") -> None:
        if name == "process-external-sessions":
            self.external_sessions_running = False
        QtDesktopApp.clear_task_state(self, task_id)
        self.log(f"{name} stopped.", group=group)

    def task_finished(self, name: str, result: object, group: str, task_id: str = "") -> None:
        if name == "process-external-sessions":
            self.external_sessions_running = False
        QtDesktopApp.clear_task_state(self, task_id)
        if isinstance(result, ActionResult):
            self.log(result.summary(name), group=group, operation=name)
            for account_id, detail in result.details.items():
                if detail == "ok":
                    continue
                self.log(
                    detail,
                    group=group,
                    severity="error",
                    account=account_id,
                    operation=name,
                )
        elif isinstance(result, TelegramCodeScanResult):
            self.log(
                f"{name} finished: accounts_ok={result.ok}, errors={result.errors}, "
                f"codes={len(result.codes)}",
                group=group,
            )
            if not result.codes:
                self.log("No Telegram codes found in service messages from the last 60 seconds.", group=group)
            for item in result.codes:
                stamp = datetime.fromtimestamp(item.received_at).strftime("%H:%M:%S") if item.received_at else "?"
                self.log(f"[{stamp}] {item.label} ({item.account_id}) | code={item.code}", group=group)
        elif isinstance(result, GiveawayInspection):
            self.log(f"Giveaway link: {result.summary()}", group=group, operation=name)
        elif isinstance(result, DirectAccessResult):
            self.log(f"{result.account_id}: {result.detail}", group=group, operation=name)
        elif all(hasattr(result, attr) for attr in ("copied", "male", "female", "unknown", "skipped")):
            self.log(
                f"{name} finished: copied={result.copied}, male={result.male}, "
                f"female={result.female}, unknown={result.unknown}, skipped={result.skipped}",
                group=group,
            )
            if getattr(result, "stopped_at_limit", False):
                self.log("Avatar download stopped automatically after reaching the file limit.", group=group)
            if getattr(result, "download_warning", ""):
                self.log(f"Avatar download warning: {result.download_warning}", group=group)
        else:
            self.log(f"{name} finished.", group=group)
        self.refresh_accounts()

    def start_chat_action(self, action: str) -> None:
        target = self.entry_value(self.target_entry)
        if not target:
            self.log("Enter chat/channel username, invite link, or ID.")
            return
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        if action == "leave" and not self.confirm_bulk_operation("Leave chat/channel", accounts):
            return
        service = ChatActionService(self.config)
        progress = self.begin_operation_progress(
            "Join chat/channel" if action == "join" else "Leave chat/channel", accounts,
        )

        async def runner(_: asyncio.Event) -> ActionResult:
            if action == "join":
                return await service.join(accounts, target, progress=progress)
            return await service.leave(accounts, target, progress=progress)

        self.record_scenario_step(
            f"chat.{action}",
            f"{action} {target}",
            {"group": self.active_group(), "target": target},
        )
        self.start_managed_task(f"{action}:{target}", runner)
        self.target_entry.clear()

    def open_link(self) -> None:
        link = self.entry_value(self.target_entry)
        if not link:
            self.log("Enter Telegram link in Chat / Channel field.")
            return
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        service = ChatActionService(self.config)
        progress = self.begin_operation_progress("Open link", accounts)

        async def runner(_: asyncio.Event) -> ActionResult:
            return await service.open_link(accounts, link, progress=progress)

        self.record_scenario_step(
            "chat.open_link",
            f"open link {link}",
            {"group": self.active_group(), "target": link},
        )
        self.start_managed_task(f"open-link:{link}", runner)

    def _giveaway_inputs(self) -> tuple[str, str, list[str]] | None:
        target = self.giveaway_target.text().strip()
        if not target:
            self.log("Enter a giveaway post link or bot/startapp link.")
            return None
        provider = str(self.giveaway_provider.currentData() or "auto")
        channels = [
            item.strip()
            for item in re.split(r"[\s,;]+", self.giveaway_channels.text().strip())
            if item.strip()
        ]
        return target, provider, channels

    def inspect_giveaway(self) -> None:
        values = self._giveaway_inputs()
        if values is None:
            return
        target, provider, channels = values
        accounts = self.get_selected_accounts() if self.has_selection() else self.group_accounts()
        if not accounts:
            self.log("No enabled accounts match the current selection/filter.")
            return
        service = GiveawayService(self.config)
        self.start_managed_task(
            f"giveaway-inspect:{target}",
            lambda _stop: service.inspect(
                target,
                provider=provider,
                extra_channels=channels,
                probe_account=accounts[0],
                probe_accounts=accounts,
            ),
        )

    def join_giveaway(self) -> None:
        values = self._giveaway_inputs()
        if values is None:
            return
        target, provider, channels = values
        accounts = self.get_selected_accounts() if self.has_selection() else self.group_accounts()
        if not accounts:
            self.log("No enabled accounts match the current selection/filter.")
            return
        if not self.confirm(
            "Join giveaway?",
            f"Join {len(accounts)} account(s) to this giveaway?\n\n{target}",
        ):
            return
        progress = self.begin_operation_progress("Join giveaway", accounts)
        service = GiveawayService(self.config)

        async def runner(_stop: asyncio.Event) -> ActionResult:
            return await service.participate(
                accounts,
                target,
                provider=provider,
                extra_channels=channels,
                progress=progress,
            )

        self.start_managed_task(f"giveaway-join:{target}", runner)

    def view_post(self) -> None:
        link = self.entry_value(self.target_entry)
        if not link:
            self.log("Enter Telegram post link in Chat / Channel field.")
            return
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        service = ChatActionService(self.config)
        progress = self.begin_operation_progress("View post", accounts)

        async def runner(_: asyncio.Event) -> ActionResult:
            return await service.view_post(accounts, link, progress=progress)

        self.record_scenario_step(
            "chat.view_post",
            f"view post {link}",
            {"group": self.active_group(), "target": link},
        )
        self.start_managed_task(f"view-post:{link}", runner)
        self.target_entry.clear()

    def set_random_reaction(self) -> None:
        link = self.entry_value(self.target_entry)
        if not link:
            self.log("Enter Telegram post link in Chat / Channel field.")
            return
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        service = ChatActionService(self.config)
        progress = self.begin_operation_progress("Set random reaction", accounts)

        async def runner(_: asyncio.Event) -> ActionResult:
            return await service.random_reaction(accounts, link, progress=progress)

        self.record_scenario_step(
            "chat.random_reaction",
            f"random reaction {link}",
            {"group": self.active_group(), "target": link},
        )
        self.start_managed_task(f"random-reaction:{link}", runner)
        self.target_entry.clear()

    def choose_reaction(self) -> None:
        link = self.entry_value(self.target_entry)
        if not link:
            self.log("Enter Telegram post link in Chat / Channel field.")
            return
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        group = self.active_group()
        service = ChatActionService(self.config)
        self.log("Loading available reactions...", group=group)

        async def loader() -> list[ReactionChoice]:
            return await service.available_reactions(accounts, link)

        def done(result: object | None, error: BaseException | None) -> None:
            if error:
                self.signals.reaction_choices_failed.emit(str(error), group)
                return
            self.signals.reaction_choices_loaded.emit(result or [], link, group)

        self.worker.submit(loader(), done)

    def on_reaction_choices_failed(self, error: str, group: str) -> None:
        self.log(f"Could not load reactions: {error}", group=group)

    def on_reaction_choices_loaded(self, reactions_object: object, link: str, group: str) -> None:
        reactions = [
            reaction for reaction in reactions_object
            if isinstance(reaction, ReactionChoice) and reaction.value
        ]
        if not reactions:
            self.log("No reactions are available for this post.", group=group)
            return

        lines = [f"{index}. {reaction.display}" for index, reaction in enumerate(reactions, start=1)]
        number, accepted = QInputDialog.getInt(
            self,
            "Choose reaction",
            "Available reactions:\n\n" + "\n".join(lines) + "\n\nEnter reaction number:",
            1,
            1,
            len(reactions),
            1,
        )
        if not accepted:
            self.log("Reaction selection cancelled.", group=group)
            return
        reaction = reactions[number - 1]
        accounts = self.accounts.list_accounts(group=group, enabled_only=True)
        if not accounts:
            self.log(f"No enabled accounts in group '{group}'.", group=group)
            return
        service = ChatActionService(self.config)
        progress = self.begin_operation_progress("Set reaction", accounts)

        async def runner(_: asyncio.Event) -> ActionResult:
            return await service.set_reaction(accounts, link, reaction, progress=progress)

        self.record_scenario_step(
            "chat.set_reaction",
            f"set reaction {reaction.display} {link}",
            {"group": group, "target": link, "reaction": reaction.to_dict()},
        )
        self.start_managed_task(f"set-reaction:{reaction.display}:{link}", runner, task_group=group)
        if self.active_group() == group:
            self.target_entry.clear()

    def start_ai(self) -> None:
        chat = self.entry_value(self.ai_chat_entry) or self.entry_value(self.target_entry)
        topic = self.entry_value(self.ai_topic_entry)
        if not chat or not topic:
            self.log("Enter AI chat username/ID and topic.")
            return
        try:
            count = int(self.entry_value(self.ai_count_entry) or "10")
        except ValueError:
            count = 10
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        companion = AICompanion(self.config)
        run_config = AIConversationConfig(
            chat=chat,
            topic=topic,
            message_count=count,
            provider=self.config.llm_provider,
            model=self.config.llm_model,
            api_key=self.config.llm_api_key,
            base_url=self.config.llm_base_url,
        )
        self.log(
            f"AI config: provider={run_config.provider}, model={run_config.model}, "
            f"base_url={run_config.base_url}, chat={chat}, messages={count}"
        )
        self.record_scenario_step(
            "ai.run",
            f"AI {chat} / {topic[:48]} / {count}",
            {"group": self.active_group(), "chat": chat, "topic": topic, "count": count},
        )
        self.start_managed_task(
            f"ai:{chat}",
            lambda stop: companion.run(accounts, run_config, stop_event=stop),
        )
        self.ai_chat_entry.clear()
        self.ai_topic_entry.clear()
        self.ai_count_entry.clear()

    def start_online(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        self.scenario_not_recorded("online mode is long-running; start it separately.")
        service = OnlineModeService(self.config, scheduler=self.scheduler)
        self.start_managed_task("online-mode", lambda stop: service.run(accounts, stop))

    def start_warmup(self) -> None:
        from modules.warmup_engine import WarmupEngine

        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        if self.foreground_task_busy() or self._task_start_pending:
            self.log("Finish or stop the current task before starting Warmup.")
            return
        if self.current_task_name == "warmup":
            self.log("Warmup is already running. Stop it before changing its policy.")
            return
        try:
            options = self.warmup_options()
            engine = WarmupEngine(self.config, scheduler=self.scheduler, options=options)
            engine.validate_accounts(accounts)
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Warmup settings", str(exc))
            return
        writes = ", ".join(name for name, enabled in (("reactions", options.reactions), ("Saved Messages", options.save_posts), ("subscriptions", options.join_channels)) if enabled) or "none (read-only)"
        if not self.confirm("Start Warmup?", f"Accounts: {len(accounts)} (current filters).\nCycles per account: {options.cycles or 'continuous until Stop'}.\nBudgets: {options.hourly_budget}/hour, {options.daily_budget}/24h.\nWrites: {writes}.\nSleep and persisted cooldowns apply; this does not guarantee protection from Telegram limits."):
            return
        options.save(self.config.data_dir / "warmup_settings.json")
        self.scenario_not_recorded("warmup uses background settings; start it separately.")
        progress = self.begin_operation_progress("Warmup", accounts) if options.cycles else self.begin_indeterminate_progress("Warmup")
        states = {}
        def report(snapshot):
            states[snapshot["account_id"]] = snapshot
            if snapshot["terminal"] and snapshot["status"] != "Stopped" and options.cycles:
                if snapshot["stats"]["errors"] or snapshot["stats"]["skipped"] and not snapshot["stats"]["viewed"]:
                    progress.mark_error(snapshot["account_id"])
                else:
                    progress.mark_ok(snapshot["account_id"])
            self.signals.warmup_status.emit({"token": id(progress), "states": list(states.values()), "total": len(accounts), "status": f"{snapshot['account_id']}: {snapshot['status']}"})
        engine.on_status = report
        group = self.active_group()
        self.start_managed_task("warmup", lambda stop: engine.run_for_group(group, stop, accounts=accounts))

    def warmup_options(self):
        from modules.warmup_settings import WarmupOptions
        values = {key: control.isChecked() if isinstance(control, QCheckBox) else control.value()
                  for key, control in self.warmup_controls.items()}
        return WarmupOptions(channels=self.warmup_channels.toPlainText(), **values).validate()

    def save_warmup_settings(self) -> None:
        try:
            self.warmup_options().save(self.config.data_dir / "warmup_settings.json")
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Warmup settings", str(exc))
            return
        self.log("Warmup settings saved. Changes apply on the next start.")

    def on_warmup_status(self, snapshot) -> None:
        if self.current_progress is None or snapshot.get("token") != id(self.current_progress):
            return
        states = snapshot["states"]
        done = sum(state["terminal"] and state["status"] != "Stopped" for state in states)
        counts = {key: sum(state["stats"][key] for state in states) for key in ("viewed", "reacted", "saved", "joined", "errors")}
        detail = snapshot["status"]
        self.warmup_status_label.setText(f"Accounts finished: {done}/{snapshot['total']} · viewed: {counts['viewed']} · errors: {counts['errors']}\nReactions: {counts['reacted']} · saved: {counts['saved']} · joined: {counts['joined']}\n{detail}")

    def stop_warmup(self) -> None:
        self.worker.submit(self.tasks.stop_by_name_prefix("warmup"))
        self.log("Warmup stop requested.")

    def refresh_proxy_pool(self) -> None:
        from modules.proxy_manager import DEFAULT_SOURCES, ProxyManager

        manager = ProxyManager(
            self.config.proxy_pool_db,
            DEFAULT_SOURCES,
            self.config.proxy_max_concurrency,
            self.config.proxy_interval_minutes,
        )

        group = self.active_group()

        async def runner(_stop: asyncio.Event) -> None:
            stats = await manager.run_once()
            self.signals.log_message.emit(
                f"VPN pool: {stats.get('valid', 0)} MTProto-valid, "
                f"{stats.get('usable_total', 0)} usable local exits.",
                group,
            )
            self.signals.refresh_requested.emit()
            self.refresh_proxy_dashboard()

        self.log("Checking the local VPN gateway exits.")
        self.start_managed_task("proxy-refresh", runner)

    def check_selected_sessions(self) -> None:
        account_ids = self.selected_account_ids()
        if not account_ids:
            self.log("Select accounts first.")
            return
        self.check_sessions(account_ids)

    def open_selected_account(self) -> None:
        account_ids = self.selected_account_ids()
        if len(account_ids) != 1:
            self.log("Select exactly one account to open Telegram Web.")
            return
        account = self.accounts.get_account(account_ids[0])
        if account is None:
            self.log("Selected account no longer exists.")
            return

        async def runner(_stop: asyncio.Event) -> DirectAccessResult:
            return await self.direct_access.open_account(account)

        self.log(f"Opening Telegram Web for {account.label or account.id}…")
        self.start_managed_task("open-account", runner)

    def check_sessions(self, account_ids: list[str] | None = None) -> None:
        if account_ids:
            wanted = set(account_ids)
            accounts = [
                account for account in self.accounts.list_accounts()
                if account.id in wanted
            ]
        else:
            accounts = self.filtered_accounts(self.accounts.list_accounts(group=self.active_group()))
        if not accounts:
            self.log("No accounts in selected group.")
            return
        check_group = self.active_group()
        service = SessionHealthService(self.config)

        progress = self.begin_operation_progress("Check sessions", accounts)
        async def runner(_stop: asyncio.Event) -> ActionResult:
            result = await service.validate(accounts, progress=progress)
            bad_ids = sorted(service.invalid_session_ids)
            frozen_ids = sorted(service.frozen_session_ids)
            self.signals.session_check_done.emit(result.ok, bad_ids, check_group)
            if frozen_ids:
                deleted = self.accounts.delete_accounts(frozen_ids, delete_sessions=True)
                self.log(f"Deleting {deleted} frozen account(s): {frozen_ids}")
            self.signals.refresh_requested.emit()
            return result

        self.record_scenario_step(
            "session.check",
            "check sessions",
            {"group": self.active_group()},
        )
        self.start_managed_task("check-sessions", runner)

    def check_spamblock(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        service = SpamBlockService(self.config)

        async def runner(_stop: asyncio.Event) -> ActionResult:
            result = await service.check(accounts)
            self.signals.refresh_requested.emit()
            return result

        self.record_scenario_step(
            "session.spamblock",
            "check spamblock",
            {"group": self.active_group()},
        )
        self.start_managed_task("check-spamblock", runner)

    def check_account_age(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return

        from modules.account_age import AccountAgeService
        service = AccountAgeService(self.config)

        async def runner(_stop: asyncio.Event) -> ActionResult:
            result = await service.check_batch(accounts)
            self.signals.refresh_requested.emit()
            return result

        self.start_managed_task("check-account-age", runner)

    def assign_proxy_to_selected(self) -> None:
        account_ids = self.selected_account_ids()
        if not account_ids:
            self.log("Select accounts first.")
            return
        self._assign_proxies(account_ids)

    def assign_proxy_to_group(self) -> None:
        accounts = self.filtered_accounts(
            self.accounts.list_accounts(group=self.active_group(), enabled_only=False)
        )
        if not accounts:
            self.log("No accounts in group.")
            return
        self._assign_proxies([a.id for a in accounts])

    def _assign_proxies(self, account_ids: list[str]) -> None:
        from modules.proxy_manager import ProxyPool
        pool = ProxyPool(self.config.proxy_pool_db)
        progress = self.begin_operation_progress("Assign VPN exits", account_ids)

        async def runner(_stop: asyncio.Event) -> ActionResult:
            result = ActionResult()
            entries = await pool.candidates(protocol="socks5", limit=200)
            if not entries:
                entries = await pool.candidates(protocol=None, limit=200)
            accounts = self.accounts.list_accounts(enabled_only=False)
            assignments = balance_proxy_assignments(
                account_ids,
                [entry.url for entry in entries],
                {account.id: account.proxy for account in accounts},
            )
            assigned_ids = set()
            for account_id, proxy_url in assignments:
                self.accounts.set_proxy(account_id, proxy_url)
                assigned_ids.add(account_id)
                result.add_ok(account_id)
                progress.mark_ok(account_id)
                await asyncio.sleep(0)
            for account_id in account_ids:
                if account_id not in assigned_ids:
                    result.add_error(account_id, "no VPN exits in pool")
                    progress.mark_error(account_id)
            return result

        self.start_managed_task("assign-proxy", runner)

    def clear_proxy_selected(self) -> None:
        account_ids = self.selected_account_ids()
        if not account_ids:
            self.log("Select accounts first.")
            return
        count = 0
        for aid in account_ids:
            account = self.accounts.get_account(aid)
            if account and account.proxy:
                self.accounts.set_proxy(aid, None)
                count += 1
        self.log(f"Cleared proxy from {count}/{len(account_ids)} account(s).")
        self.refresh_accounts()

    def check_assigned_proxies(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts.")
            return
        from modules.proxy_manager import ProxyPool, validate_proxy
        from urllib.parse import unquote, urlparse
        pool = ProxyPool(self.config.proxy_pool_db)
        progress = self.begin_operation_progress("Check VPN exits", accounts)

        async def runner(_stop: asyncio.Event) -> ActionResult:
            result = ActionResult()

            def mark_ok(account_id: str) -> None:
                result.add_ok(account_id)
                progress.mark_ok(account_id)

            def mark_error(account_id: str, detail: str) -> None:
                result.add_error(account_id, detail)
                progress.mark_error(account_id)

            if os.getenv("PROXY_MODE", "").strip().lower() == "vpn_gateway":
                return await self._check_gateway_proxies(
                    accounts, validate_proxy, pool, result, mark_ok, mark_error,
                )

            for account in accounts:
                proxy_url = account.proxy or account.metadata.get("proxy")
                if not proxy_url:
                    mark_error(account.id, "proxy required")
                    continue
                try:
                    parsed = urlparse(proxy_url)
                    ip, port = parsed.hostname or "", parsed.port or 0
                    proto = parsed.scheme or "socks5"
                    if ip and port:
                        auth = (
                            (unquote(parsed.username), unquote(parsed.password or ""))
                            if parsed.username else None
                        )
                        valid, latency = await validate_proxy(ip, port, proto, auth=auth)
                        if valid:
                            mark_ok(account.id)
                            account.metadata["proxy_latency"] = round(latency, 3)
                            await pool.record_success(ip, port, proto, latency)
                        else:
                            await pool.add_to_blacklist(ip, port, proto)
                            await pool.remove_dead_from_pool(ip, port, proto)
                            replacement = await pool.acquire(exclude_urls={proxy_url})
                            if replacement:
                                self.accounts.set_proxy(account.id, replacement.url)
                                mark_ok(account.id)
                            else:
                                self.accounts.set_proxy(account.id, None)
                                mark_error(account.id, "dead, no verified replacement")
                    else:
                        mark_error(account.id, "bad url")
                except Exception:
                    mark_error(account.id, "parse err")
                await asyncio.sleep(0)
            self.signals.refresh_requested.emit()
            return result

        self.start_managed_task("check-proxies", runner)

    async def _check_gateway_proxies(
        self,
        accounts,
        validate_proxy,
        pool,
        result: ActionResult,
        mark_ok,
        mark_error,
    ) -> ActionResult:
        """Check each shared exit once and move accounts off recovering exits."""
        from urllib.parse import unquote, urlparse

        grouped: dict[str, list] = {}
        for account in accounts:
            proxy_url = account.proxy or account.metadata.get("proxy")
            if proxy_url:
                grouped.setdefault(proxy_url, []).append(account)
            else:
                mark_error(account.id, "proxy required")

        sem = asyncio.Semaphore(20)
        working_urls: list[str] = []
        failed_groups: list[tuple[str, list]] = []

        async def check_exit(proxy_url: str, members: list) -> None:
            parsed = urlparse(proxy_url)
            ip, port = parsed.hostname or "", parsed.port or 0
            protocol = parsed.scheme or "socks5"
            if not ip or not port:
                for account in members:
                    mark_error(account.id, "invalid url")
                return
            auth = (
                (unquote(parsed.username), unquote(parsed.password or ""))
                if parsed.username else None
            )
            try:
                async with sem:
                    valid, latency = await validate_proxy(
                        ip, port, protocol, timeout=8.0, auth=auth,
                    )
            except Exception as exc:
                self.ui_log.warning("VPN exit health check failed for %s:%s: %s", ip, port, exc)
                valid, latency = False, 0.0

            if valid and latency <= 8.0:
                status = "active" if latency <= 5.0 else "slow"
                await pool.update_status(ip, port, protocol, status, latency)
                working_urls.append(proxy_url)
                checked_at = utc_now_iso()
                for account in members:
                    account.metadata["proxy_latency"] = latency
                    account.metadata["proxy_checked_at"] = checked_at
                    self.sessions._upsert_account(account)
                    mark_ok(account.id)
                return

            await pool.record_gateway_failure(ip, port, protocol)
            failed_groups.append((proxy_url, members))

        await asyncio.gather(*(check_exit(url, members) for url, members in grouped.items()))
        failed_accounts = [account for _url, members in failed_groups for account in members]
        if failed_accounts and working_urls:
            current = {
                account.id: account.proxy
                for account in self.accounts.list_accounts(enabled_only=False)
            }
            assignments = balance_proxy_assignments(
                [account.id for account in failed_accounts], working_urls, current,
            )
            assigned_ids = set()
            for account_id, proxy_url in assignments:
                self.accounts.set_proxy(account_id, proxy_url)
                assigned_ids.add(account_id)
                mark_ok(account_id)
            for account in failed_accounts:
                if account.id not in assigned_ids:
                    mark_error(account.id, "no healthy VPN replacement; assignment preserved")
            self.log(
                f"Moved {len(assigned_ids)} account(s) away from "
                f"{len(failed_groups)} recovering VPN exit(s)."
            )
        else:
            for account in failed_accounts:
                mark_error(account.id, "no healthy VPN replacement; assignment preserved")
        self.signals.refresh_requested.emit()
        return result

    def assign_timezones(self) -> None:
        accounts = self.filtered_accounts(
            self.accounts.list_accounts(group=self.active_group(), enabled_only=False)
        )
        count = self.scheduler.assign_all(accounts)
        self.log(f"Timezones assigned to {count} account(s).")
        self.refresh_accounts()

    def on_session_check_done(self, ok: int, bad_ids: object, group: str) -> None:
        bad_list = [str(item) for item in bad_ids] if isinstance(bad_ids, list) else []
        self.last_bad_session_ids = bad_list
        self.log(f"Session check finished: ok={ok}, invalid={len(bad_list)}", group=group)
        if bad_list and self.confirm(
            "Delete broken sessions?",
            (
                f"Found {len(bad_list)} broken session(s).\n\n"
                "Delete them from the account list and remove session files from data/sessions and imports?"
            ),
        ):
            removed = self.accounts.delete_accounts(bad_list, delete_sessions=True)
            self.log(f"Deleted {removed} broken session(s).", group=group)
            self.refresh_accounts()
        self.prompt_duplicate_sessions()

    def on_passkey_restore_done(self, ok: int, bad_ids: object, group: str) -> None:
        bad_list = [str(item) for item in bad_ids] if isinstance(bad_ids, list) else []
        self.last_bad_session_ids = bad_list
        self.log(f"Passkey restore finished: restored={ok}, unrestored={len(bad_list)}", group=group)
        if bad_list and self.confirm(
            "Delete unrestored broken sessions?",
            (
                f"Could not restore {len(bad_list)} broken session(s) via Passkeys (no saved passkeys or restoration failed).\n\n"
                "Delete them from the account list and remove session files from data/sessions and imports?"
            ),
        ):
            removed = self.accounts.delete_accounts(bad_list, delete_sessions=True)
            self.log(f"Deleted {removed} broken session(s).", group=group)
            self.refresh_accounts()

    def prompt_duplicate_sessions(self) -> None:
        duplicates = self.accounts.find_duplicate_user_ids()
        if not duplicates:
            return
        delete_ids: list[str] = []
        lines: list[str] = []
        for user_id, items in duplicates.items():
            keep = items[0]
            remove = items[1:]
            delete_ids.extend(account.id for account in remove)
            lines.append(f"user_id {user_id}: keep {keep.id}")
            for account in remove:
                username = f"@{account.username}" if account.username else "no username"
                lines.append(f"  delete newer {account.id} ({username}, group={account.group})")
        message = "Found sessions that point to the same Telegram account.\n\n" + "\n".join(lines[:24])
        if len(lines) > 24:
            message += f"\n...and {len(lines) - 24} more line(s)."
        message += "\n\nDelete the newer duplicate sessions from accounts, data/sessions, and imports?"
        if self.confirm("Duplicate Telegram accounts", message):
            removed = self.accounts.delete_accounts(delete_ids, delete_sessions=True)
            self.log(f"Deleted {removed} duplicate newer session(s).")
            self.refresh_accounts()

    def stop_task(self) -> None:
        task_id = self.current_task_id.strip()
        if not task_id:
            self.log("No current task is running.")
            return
        self.worker.submit(
            self.tasks.stop(task_id),
            lambda result, error: self.signals.stop_done.emit(f"Stop {task_id}", result, error),
        )

    def stop_all_tasks(self) -> None:
        self.worker.submit(
            self.tasks.stop_all(),
            lambda result, error: self.signals.stop_done.emit("Stop all", result, error),
        )

    def on_stop_done(self, label: str, result: object, error: object) -> None:
        if error:
            self.log(f"{label}: {error}")
        else:
            self.log(f"{label}: {result}")
        self.refresh_accounts()

    def _profile_scrape(self) -> None:
        accounts = self.get_selected_accounts() if self.has_selection() else self.group_accounts()
        if not accounts:
            self.log("No accounts selected.")
            return
        if self.chk_skip_profiled.isChecked():
            accounts = [a for a in accounts if not a.has_profile()]
        if not accounts:
            self.log("All accounts already profiled.")
            return
        count = int(self.scrape_count_entry.text() or "5")
        group_raw = self.scrape_channel_entry.text().strip()
        user_raw = self.scrape_usernames_entry.text().strip()
        if group_raw:
            group = _norm_ch(group_raw)
            self.start_profile_scrape_task(group, count, [], accounts)
        elif user_raw:
            names = [n.strip().lstrip("@") for n in re.split(r"[\s,;]+", user_raw) if n.strip()]
            self.start_profile_scrape_task("", len(names), names, accounts)
        else:
            self.log("Enter a group or usernames first.")

    def start_profile_scrape_task(self, group: str, count: int, names: list[str], accounts: list[AccountRecord]) -> None:
        copy_name = self.chk_name.isChecked()
        copy_bio = self.chk_bio.isChecked()
        copy_username = self.chk_username.isChecked()
        copy_avatars = self.chk_avatars.isChecked()
        copy_music = self.chk_music.isChecked() if hasattr(self, "chk_music") else False
        copy_stories = self.chk_stories_copy.isChecked() if hasattr(self, "chk_stories_copy") else False
        copy_birthday = self.chk_birthday.isChecked() if hasattr(self, "chk_birthday") else False
        min_avatars_val = int(self.min_avatars.text() or "0")
        min_stories_val = int(self.min_stories.text() or "0")
        require_bio_val = self.chk_bio_flt.isChecked()
        require_avatar = self.chk_avatar.isChecked()
        require_stories = self.chk_story_flt.isChecked()
        async def runner(stop_event) -> ActionResult:
            scraper = ProfileScraperService(self.config)
            on_log = self.log
            if names:
                from modules.profile_scraper import ProfileData
                profiles = [ProfileData(0, n, "", "", "", 0, 0, False) for n in names]
                self.log(f"Using {len(profiles)} usernames")
            else:
                sr = await scraper.scrape_from_group(group, on_log=on_log)
                profiles = sr.profiles
                self.log(f"Parsed {len(profiles)} usernames from group")
            parse_progress = self.begin_operation_progress("Profile parsing", profiles)
            need_stories = require_stories or copy_stories
            full = await scraper.fetch_full_profiles(
                profiles,
                download_avatars=copy_avatars,
                download_stories=need_stories,
                download_music=copy_music,
                with_birthday=copy_birthday,
                min_avatars=max(min_avatars_val, 1 if require_avatar else 0),
                min_stories=max(min_stories_val, 1 if require_stories else 0),
                require_bio=require_bio_val,
                require_stories=require_stories,
                max_count=count if not names else len(names),
                progress=parse_progress,
                on_log=on_log,
            )
            if not full:
                self.log("No profiles matched filters — try uncheck Has avatar / Has story")
                return ActionResult()
            apply_progress = self.begin_operation_progress("Apply profiles", accounts[: len(full)])
            return await scraper.apply_to_accounts(
                accounts, full,
                copy_name=copy_name, copy_bio=copy_bio,
                copy_username=copy_username, copy_avatars=copy_avatars,
                copy_music=copy_music, copy_stories=copy_stories, copy_birthday=copy_birthday,
                clear_first=True, progress=apply_progress,
                on_log=on_log,
            )

        name = f"profile-scrape-{group or len(names)}"
        self.start_managed_task(name, runner)

    def set_usernames(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        customizer = ProfileCustomizer(self.config)
        usernames = [line.lstrip("@") for line in customizer.load_lines(self.usernames_file)]
        if not self.confirm_bulk_operation("Set usernames", accounts):
            return
        self.record_scenario_step(
            "profile.set_usernames",
            "set usernames from templates/usernames.txt",
            {"group": self.active_group()},
        )
        self.start_profile_task("set-usernames", ProfileUpdatePlan(usernames=usernames))

    def clear_usernames(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        if not self.confirm_bulk_operation("Clear usernames", accounts):
            return
        self.record_scenario_step(
            "profile.clear_usernames",
            "clear usernames",
            {"group": self.active_group()},
        )
        self.start_profile_task("clear-usernames", ProfileUpdatePlan(usernames=[None] * len(accounts)))

    def set_profile(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        customizer = ProfileCustomizer(self.config)
        if not self.confirm_bulk_operation("Set profile fields", accounts):
            return
        self.record_scenario_step(
            "profile.set_profile",
            "set first/last/bio from templates",
            {"group": self.active_group()},
        )
        self.start_profile_task(
            "set-profile",
            ProfileUpdatePlan(
                first_names=customizer.load_optional_lines(self.first_names_file),
                last_names=customizer.load_optional_lines(self.last_names_file),
                bios=customizer.load_optional_lines(self.bios_file),
            ),
        )

    def generate_profiles(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        used_raw = read_json(self.config.data_dir / "avatar_usage.json", {"used": []})
        used_avatars = {str(item) for item in used_raw.get("used", []) if str(item).strip()}
        plan = write_profile_plan(
            accounts,
            self.avatar_pack_dir,
            self.profile_plan_file,
            used_avatars=used_avatars,
        )
        export_plan_text_files(plan, Path("templates"))
        with_avatar = sum(1 for item in plan if item.get("avatar"))
        male = sum(1 for item in plan if item.get("gender") == "m")
        female = sum(1 for item in plan if item.get("gender") == "f")
        self.log(
            f"Generated profile plan: {len(plan)} accounts, m={male}, f={female}, "
            f"avatars={with_avatar}. File: {self.profile_plan_file}"
        )
        self.record_scenario_step(
            "profile.generate",
            "generate profile plan",
            {"group": self.active_group()},
        )
        from modules.fingerprint_generator import FingerprintGenerator
        fg = FingerprintGenerator()
        for account in accounts:
            fg.regenerate(account.id)
        self.log(f"Fingerprints regenerated for {len(accounts)} account(s).")
        self.refresh_accounts()

    def download_avatar_pack(self) -> None:
        url = self.entry_value(self.avatar_drive_entry)
        if not url:
            self.log("Enter Google Drive folder URL.")
            return
        raw_dir = Path("assets/avatar_packs_raw")
        pack_dir = self.avatar_pack_dir

        async def runner(_stop: asyncio.Event):
            return await asyncio.to_thread(download_google_drive_avatar_pack, url, raw_dir, pack_dir)

        self.record_scenario_step(
            "profile.download_avatars",
            "download avatar pack",
            {"group": self.active_group(), "avatar_url": url},
        )
        self.start_managed_task("download-avatar-pack", runner)

    def apply_profile_plan(self) -> None:
        customizer = ProfileCustomizer(self.config)
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        if not self.profile_plan_file.exists():
            self.log("Generate profiles first or set an existing profile_plan.json.")
            return
        try:
            profiles = customizer.load_profile_plan(self.profile_plan_file)
        except Exception as exc:
            self.log(f"Cannot load profile plan: {exc}")
            return
        if not self.confirm_bulk_operation("Apply profile plan", accounts):
            return
        self.record_scenario_step(
            "profile.apply_plan",
            "apply profile plan",
            {"group": self.active_group()},
        )
        self.start_profile_task("apply-profile-plan", ProfileUpdatePlan(profile_plan=profiles))

    def split_unknown_avatars(self) -> None:
        if not self.confirm(
            "Split unknown avatars?",
            (
                "This will move images from assets/avatar_packs/unknown into male/female randomly. "
                "They will disappear from unknown.\n\nContinue?"
            ),
        ):
            return
        self.record_scenario_step(
            "profile.split_unknown",
            "split unknown avatars",
            {"group": self.active_group()},
        )
        counters = split_unknown_avatars(self.avatar_pack_dir, male_ratio=0.5, move=True)
        self.log(
            f"Split unknown avatars: male={counters['male']}, "
            f"female={counters['female']}, skipped={counters['skipped']}"
        )

    def clear_bio(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        if not self.confirm_bulk_operation("Clear bios", accounts):
            return
        self.record_scenario_step(
            "profile.clear_bio",
            "clear bio",
            {"group": self.active_group()},
        )
        self.start_profile_task("clear-bio", ProfileUpdatePlan(bios=[""]), accounts)

    def set_avatars(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        if not self.confirm_bulk_operation("Set profile photos", accounts):
            return
        self.record_scenario_step(
            "profile.set_avatars",
            "set avatars",
            {"group": self.active_group()},
        )
        self.start_profile_task("set-avatars", ProfileUpdatePlan(avatars_dir=self.avatars_dir), accounts)

    def clear_avatars(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        if not self.confirm_bulk_operation("Clear profile photos", accounts):
            return
        self.record_scenario_step(
            "profile.clear_avatars",
            "clear avatars",
            {"group": self.active_group()},
        )
        self.start_profile_task("clear-avatars", ProfileUpdatePlan(clear_avatars=True), accounts)

    def clear_full_profile(self) -> None:
        accounts = self.get_selected_accounts() if self.has_selection() else self.group_accounts()
        if not accounts:
            self.log("No accounts selected.")
            return
        if not self.confirm_bulk_operation(
            "Clear full profile",
            accounts,
            detail="Clears username, last name, bio, and all profile photos.",
        ):
            return
        snapshot_path = ProfileSnapshotService(self.config).save_latest(accounts, "clear-full-profile")
        self.log(f"Saved profile snapshot: {snapshot_path}")
        customizer = ProfileCustomizer(self.config)
        plan = ProfileUpdatePlan(
            usernames=[None] * len(accounts),
            last_names=[""],
            bios=[""],
            clear_avatars=True,
        )
        progress = self.begin_operation_progress("Clear full profile", accounts)

        async def runner(_stop: asyncio.Event) -> ActionResult:
            result = await customizer.apply(accounts, plan, progress=progress)
            for acc in accounts:
                try:
                    customizer.accounts.update_profile_metadata(acc.id, {"profiled": False})
                except Exception:
                    pass
            try:
                from modules.profile_bindings import ProfileBindings
                bindings = ProfileBindings(self.config)
                for acc in accounts:
                    bindings.unbind(acc.id)
            except Exception:
                pass
            self.signals.refresh_requested.emit()
            return result

        self.record_scenario_step(
            "profile.clear_full_profile",
            "clear full profile",
            {"group": self.active_group()},
        )
        self.start_managed_task("clear-full-profile", runner)

    def premium_stories(self) -> None:
        accounts = self.get_selected_accounts() if self.has_selection() else self.group_accounts()
        if not accounts:
            self.log("No accounts selected.")
            return
        scraper = ProfileScraperService(self.config)
        progress = self.begin_indeterminate_progress("Premium stories")
        self.start_managed_task(
            "premium-stories",
            lambda _stop: scraper.download_stories_for_bound(accounts, progress=progress, on_log=self.log),
        )

    def apply_saved_profiles(self) -> None:
        accounts = self.get_selected_accounts() if self.has_selection() else self.group_accounts()
        if not accounts:
            self.log("No accounts selected.")
            return
        if not self.confirm_bulk_operation(
            "Apply saved profiles",
            accounts,
            detail="Applies locally saved profiles to accounts; accounts already bound to a profile only get missing stories uploaded.",
        ):
            return
        self.record_scenario_step(
            "profile.apply_saved",
            "apply profiles from local archive",
            {"group": self.active_group()},
        )
        scraper = ProfileScraperService(self.config)
        self.start_managed_task(
            "apply-saved-profiles",
            lambda _stop: scraper.apply_saved_profiles(accounts, on_log=self.log),
        )

    def rollback_profile_snapshot(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        snapshot = ProfileSnapshotService(self.config)
        profiles = snapshot.load_latest_profiles({account.id for account in accounts})
        if not profiles:
            self.log("No profile snapshot found for selected group.")
            return
        if not self.confirm_bulk_operation(
            "Rollback profile snapshot",
            accounts,
            detail="Restores the latest known profile values; avatar rollback uses known stored paths.",
        ):
            return
        self.record_scenario_step(
            "profile.rollback",
            "rollback profile snapshot",
            {"group": self.active_group()},
        )
        customizer = ProfileCustomizer(self.config)
        progress = self.begin_operation_progress("Rollback profile snapshot", accounts)
        self.start_managed_task(
            "rollback-profile-snapshot",
            lambda _stop: customizer.apply(
                accounts,
                ProfileUpdatePlan(profile_plan=profiles),
                progress=progress,
            ),
        )

    def terminate_other_sessions(self) -> None:
        self._terminate_sessions(desktop_only=False)

    def terminate_pc_sessions(self) -> None:
        self._terminate_sessions(desktop_only=True)

    def _terminate_sessions(self, desktop_only: bool) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        scope = "desktop/web Telegram sessions" if desktop_only else "all other active Telegram sessions"
        if not self.confirm_bulk_operation(
            "Terminate PC sessions" if desktop_only else "Terminate other sessions",
            accounts,
            detail=f"Terminates {scope}; the current tool session is kept.",
        ):
            return
        self.record_scenario_step(
            "security.terminate_pc" if desktop_only else "security.terminate_other",
            "terminate PC sessions" if desktop_only else "terminate other sessions",
            {"group": self.active_group()},
        )
        service = AccountSecurityService(self.config)
        progress = self.begin_operation_progress(
            "Terminate PC sessions" if desktop_only else "Terminate other sessions", accounts,
        )
        self.start_managed_task(
            "terminate-pc-sessions" if desktop_only else "terminate-other-sessions",
            lambda _stop: service.terminate_other_sessions(
                accounts, desktop_only=desktop_only, progress=progress,
            ),
        )

    def set_cloud_password(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        new_password = self.ask_text("Cloud password", "New cloud password. It will not be saved.", password=True)
        if not new_password:
            self.log("Cloud password was not changed.")
            return
        hint = self.ask_text("Password hint", "Optional password hint.")
        current_password = self.get_shared_2fa_password(
            "Current password",
            "Current password if already enabled. Leave empty if none.",
        )
        if not self.confirm_bulk_operation(
            "Set cloud password",
            accounts,
            detail="The password is used only in memory and is not written to project files.",
        ):
            return
        self.scenario_not_recorded("cloud-password actions contain secrets.")
        service = AccountSecurityService(self.config)
        progress = self.begin_operation_progress("Set cloud password", accounts)
        self.start_managed_task(
            "set-cloud-password",
            lambda _stop: service.set_cloud_password(
                accounts,
                new_password=new_password,
                hint=hint or "",
                current_password=current_password or None,
                progress=progress,
            ),
        )
        self.twofa_entry.clear()

    def remove_cloud_password(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        current_password = self.get_shared_2fa_password(
            "Current password",
            "Current 2FA password is required to remove cloud password.",
        )
        if not current_password:
            self.log("Cloud password was not removed: current 2FA password is required.")
            return
        if not self.confirm_bulk_operation(
            "Remove cloud password",
            accounts,
        ):
            return
        self.scenario_not_recorded("cloud-password actions contain secrets.")
        service = AccountSecurityService(self.config)
        progress = self.begin_operation_progress("Remove cloud password", accounts)
        self.start_managed_task(
            "remove-cloud-password",
            lambda _stop: service.remove_cloud_password(
                accounts, current_password=current_password, progress=progress,
            ),
        )
        self.twofa_entry.clear()

    def delete_passkeys(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        if not self.confirm_bulk_operation(
            "Delete passkeys",
            accounts,
            detail=(
                "Does not delete Telegram sessions or cloud password; passkeys must be recreated manually."
            ),
        ):
            return
        self.record_scenario_step(
            "passkeys.delete",
            "delete passkeys",
            {"group": self.active_group()},
        )
        service = AccountSecurityService(self.config)
        progress = self.begin_operation_progress("Delete passkeys", accounts)
        self.start_managed_task(
            "delete-passkeys",
            lambda _stop: service.delete_passkeys(accounts, progress=progress),
        )

    def add_passkeys(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        if not self.confirm_bulk_operation(
            "Add passkeys",
            accounts,
            detail=(
                "Registers WebAuthn/FIDO2 ECDSA P-256 passkeys on Telegram servers and saves private keys locally."
            ),
        ):
            return
        self.record_scenario_step(
            "passkeys.add",
            "add passkeys",
            {"group": self.active_group()},
        )
        service = AccountSecurityService(self.config)
        progress = self.begin_operation_progress("Add passkeys", accounts)
        self.start_managed_task(
            "add-passkeys",
            lambda _stop: service.add_passkeys(accounts, progress=progress),
        )

    def restore_passkeys(self) -> None:
        selected_ids = set(self.selected_account_ids())
        accounts = self.filtered_accounts(
            self.accounts.list_accounts(group=self.active_group(), enabled_only=False)
        )
        if selected_ids:
            accounts = [a for a in accounts if a.id in selected_ids]
        accounts = passkey_restore_candidates(accounts)

        if not accounts:
            self.log("No invalid or revoked accounts found to restore.")
            return

        if not self.confirm(
            "Restore via passkey",
            (
                f"Found {len(accounts)} invalid/revoked account(s).\n\n"
                "Attempt to restore them via saved Passkeys and recover sessions?"
            ),
        ):
            return

        self.record_scenario_step(
            "passkeys.restore",
            "restore passkeys",
            {"group": self.active_group(), "accounts": [a.id for a in accounts]},
        )
        check_group = self.active_group()
        service = AccountSecurityService(self.config)
        progress = self.begin_operation_progress("Restore via passkey", accounts)

        async def runner(_stop: asyncio.Event) -> ActionResult:
            result = await service.restore_passkeys(accounts, progress=progress)
            failed_ids = [acc_id for acc_id, detail in result.details.items() if detail != "ok"]
            self.signals.passkey_restore_done.emit(result.ok, failed_ids, check_group)
            self.signals.refresh_requested.emit()
            return result

        self.start_managed_task("restore-passkeys", runner)

    def sync_mailbox_options(self) -> None:
        self.mailbox_file_row.setVisible(self.email_backend_combo.currentData() in {"imap", "pop3"})

    def choose_mailbox_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose mailbox list", str(self.config.import_dir / "emails"),
                                             "Mailbox lists (*.txt *.jsonl);;All files (*)")
        if path:
            self.mailbox_file_entry.setText(path)

    def email_task_config(self) -> AppConfig | None:
        path = self.mailbox_file_entry.text().strip()
        config = replace(self.config, email_inbox_backend=str(self.email_backend_combo.currentData()),
                         email_mailboxes_file=Path(path).resolve() if path else None)
        try:
            email_setup_description(config, validate_list=True)
        except RuntimeError as exc:
            self.log(str(exc))
            QMessageBox.warning(self, "Email settings", str(exc))
            return None
        return config

    def bind_recovery_email(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        email_config = self.email_task_config()
        if email_config is None:
            return
        domain = email_config.email_domain.strip()
        inbox_api = email_config.email_inbox_api_url.strip()
        inbox_token = email_config.email_inbox_token.strip()
        new_password = self.ask_text(
            "Cloud password",
            "New cloud password if account has no 2FA, or new password if you want to change it.",
            password=True,
        )
        current_password = self.get_shared_2fa_password(
            "Current password",
            "Current cloud password if already enabled. Leave empty only for accounts without 2FA.",
        )
        hint = self.ask_text("Password hint", "Optional password hint.")
        if not self.confirm_bulk_operation(
            "Bind recovery emails",
            accounts,
            detail=email_setup_description(email_config),
        ):
            return
        self.scenario_not_recorded("recovery-email binding may require 2FA secrets.")
        service = AccountSecurityService(email_config)
        progress = self.begin_operation_progress("Bind recovery emails", accounts)
        self.start_managed_task(
            "bind-recovery-email",
            lambda _stop: service.bind_recovery_emails(
                accounts,
                domain=domain,
                inbox_api_url=inbox_api,
                inbox_token=inbox_token,
                new_password=new_password or None,
                hint=hint or "",
                current_password=current_password or None,
                progress=progress,
            ),
        )
        self.twofa_entry.clear()

    def change_login_email(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        email_config = self.email_task_config()
        if email_config is None:
            return
        domain = email_config.email_domain.strip()
        inbox_api = email_config.email_inbox_api_url.strip()
        inbox_token = email_config.email_inbox_token.strip()
        if not self.confirm_bulk_operation(
            "Change login emails",
            accounts,
            detail=(
                email_setup_description(email_config) + " Accounts without a login email can fail with EMAIL_NOT_SETUP."
            ),
        ):
            return
        self.scenario_not_recorded("login-email changes are kept manual for safety.")
        service = AccountSecurityService(email_config)
        progress = self.begin_operation_progress("Change login emails", accounts)
        self.start_managed_task(
            "change-login-email",
            lambda _stop: service.change_login_emails(
                accounts,
                domain=domain,
                inbox_api_url=inbox_api,
                inbox_token=inbox_token,
                progress=progress,
            ),
        )
        self.twofa_entry.clear()

    def get_recent_telegram_codes(self) -> None:
        accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts in selected group.")
            return
        service = TelegramCodeService(self.config)
        self.record_scenario_step(
            "telegram.codes",
            "get Telegram codes",
            {"group": self.active_group()},
        )
        self.start_managed_task(
            "get-telegram-codes",
            lambda _stop: service.scan_recent(accounts, seconds=60, limit=25),
        )

    def ask_text(self, title: str, text: str, *, password: bool = False) -> str:
        mode = QLineEdit.Password if password else QLineEdit.Normal
        value, ok = QInputDialog.getText(self, title, text, mode)
        return value.strip() if ok else ""

    def get_shared_2fa_password(self, title: str, text: str) -> str:
        shared = self.entry_value(self.twofa_entry)
        if shared:
            self.log("Using 2FA password from Security field for accounts that require it.")
            return shared
        return self.ask_text(title, text, password=True)

    def start_profile_task(self, name: str, plan: ProfileUpdatePlan, accounts: list[AccountRecord] | None = None) -> None:
        if accounts is None:
            accounts = self.group_accounts()
        if not accounts:
            self.log("No enabled accounts.")
            return
        snapshot_path = ProfileSnapshotService(self.config).save_latest(accounts, name)
        self.log(f"Saved profile snapshot: {snapshot_path}")
        customizer = ProfileCustomizer(self.config)
        progress = self.begin_operation_progress(f"Profile: {name}", accounts)
        is_clear = bool(plan.clear_avatars or (plan.usernames and all(u is None for u in plan.usernames)))

        async def runner(_stop: asyncio.Event) -> ActionResult:
            result = await customizer.apply(accounts, plan, progress=progress)
            if is_clear:
                for acc in accounts:
                    try:
                        customizer.accounts.update_profile_metadata(acc.id, {"profiled": False})
                    except Exception:
                        pass
                self.signals.refresh_requested.emit()
            return result

        self.start_managed_task(name, runner)

    def confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.question(
            self,
            title,
            text,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def confirm_bulk_operation(self, action: str, accounts, *, detail: str = "") -> bool:
        items = list(accounts)
        preview = build_bulk_operation_preview(
            action,
            self.active_group(),
            items,
            sleeping_ids={
                account.id for account in items if self.scheduler.is_sleeping(account.id)
            },
        )
        suffix = f"\n\n{detail}" if detail else ""
        return self.confirm(f"{action}?", preview.format_text() + suffix + "\n\nContinue?")

    def restore_ui_settings(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.apply_density()
        widths = self.settings.value("table_widths", [])
        if isinstance(widths, list) and len(widths) == self.table_model.columnCount():
            for column, width in enumerate(widths):
                if column in self.table_model.FIXED_WIDTHS:
                    continue
                try:
                    self.table.setColumnWidth(column, int(width))
                except (TypeError, ValueError):
                    pass

    def apply_density(self) -> None:
        """Use the Comfortable layout for every desktop session."""
        self.sidebar_scroll.setFixedWidth(420)
        self.table.verticalHeader().setDefaultSectionSize(46)
        self.table.verticalHeader().setMinimumSectionSize(46)
        self.log_box.setMaximumHeight(16_777_215)
        self.refresh_accounts()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("active_group", self.active_group())
        self.settings.setValue("table_widths", [self.table.columnWidth(i) for i in range(self.table_model.columnCount())])
        self.import_timer.stop()
        self.sleep_timer.stop()
        self.proxy_dashboard_timer.stop()
        if self.vpn_gateway_future is not None and not self.vpn_gateway_future.done():
            self.vpn_gateway_future.cancel()
        f1 = self.worker.submit(self.direct_access.close_all())
        f2 = self.worker.submit(self.tasks.stop_all())
        
        try:
            import concurrent.futures
            concurrent.futures.wait([f1, f2], timeout=2.0)
        except Exception:
            pass
        self.worker.stop()
        event.accept()


def run_desktop_app(config: AppConfig) -> None:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Uznik MultiTool")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parents[2] / "assets/branding/uznik-multitool.ico")))
    window = QtDesktopApp(config)
    window.show()
    app.exec()
