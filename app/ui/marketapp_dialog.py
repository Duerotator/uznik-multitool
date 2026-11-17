"""Modeless MarketApp catalog, minimal-click Rent automation & visibility workspace."""
from __future__ import annotations

from datetime import datetime, timezone
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.design_tokens import TOKENS
from core.ton_address import format_nano


class MarketappDialog(QDialog):
    """MarketApp Rent catalog, 1-click Rent pipeline, batch hosting scan & Wallet X funding."""

    def __init__(self, request, parent=None, selected_accounts=None):
        super().__init__(parent)
        self.request = request
        self.page = None
        self.generation = 0
        self.pending = False
        self.selected_accounts = selected_accounts or (lambda: [])
        self.review_token = None
        self.scan_account = None
        self.rent_quote: dict | None = None
        self.fund_quote: dict | None = None
        self.jobs_data: list[dict] = []
        self.account_matrix: list[dict] = []
        self._confirming_dialog = False

        self.setWindowTitle("MarketApp · Аренда подарков и юзернеймов")
        self.resize(900, 840)
        self.setMinimumSize(700, 600)
        self.setFont(QFont("Segoe UI", 10))
        palette = self.palette()
        for role, color in (
            (QPalette.Window, TOKENS.panel),
            (QPalette.WindowText, TOKENS.text),
            (QPalette.Base, TOKENS.background),
            (QPalette.Text, TOKENS.text),
            (QPalette.Button, TOKENS.panel_secondary),
            (QPalette.ButtonText, TOKENS.text),
            (QPalette.PlaceholderText, TOKENS.muted),
            (QPalette.Link, QColor("#6cb6ff")),
        ):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.setStyleSheet(
            f"QDialog {{ background: {TOKENS.panel}; color: {TOKENS.text}; }}"
            f"QWidget#marketTabPage {{ background: {TOKENS.panel}; color: {TOKENS.text}; }}"
            f"QFrame#marketTopBar {{ background: {TOKENS.background}; border: 1px solid {TOKENS.border}; border-radius: 8px; }}"
            f"QTabWidget::pane {{ background: {TOKENS.panel}; border: 1px solid {TOKENS.border}; border-radius: 8px; padding: 6px; }}"
            f"QTabBar::tab {{ background: {TOKENS.panel_secondary}; color: {TOKENS.muted}; border: 1px solid {TOKENS.border}; border-bottom: 0; padding: 7px 14px; border-top-left-radius: 7px; border-top-right-radius: 7px; margin-right: 4px; }}"
            f"QTabBar::tab:selected {{ background: #1d4f8c; color: #ffffff; border-color: #4b8fd8; }}"
            f"QLabel {{ color: {TOKENS.text}; }}"
            f"QCheckBox {{ color: {TOKENS.text}; spacing: 8px; }}"
            f"QCheckBox:disabled {{ color: {TOKENS.muted}; }}"
            f"QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {TOKENS.border}; border-radius: 4px; background: {TOKENS.background}; }}"
            f"QCheckBox::indicator:checked {{ background: {TOKENS.primary}; border-color: #4b8fd8; }}"
            f"QLineEdit, QComboBox, QSpinBox, QListWidget, QPlainTextEdit {{ background: {TOKENS.background}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 8px; padding: 6px 8px; }}"
            f"QComboBox QAbstractItemView {{ background: {TOKENS.panel_secondary}; color: {TOKENS.text}; selection-background-color: #1d4f8c; }}"
            f"QListWidget::item {{ padding: 5px 6px; border-radius: 4px; }}"
            f"QListWidget::item:selected {{ background: #1d4f8c; color: #ffffff; }}"
            f"QTableWidget {{ background: {TOKENS.background}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 8px; gridline-color: {TOKENS.border}; }}"
            f"QHeaderView::section {{ background: {TOKENS.panel_secondary}; color: {TOKENS.muted}; border: 0; border-right: 1px solid {TOKENS.border}; border-bottom: 1px solid {TOKENS.border}; padding: 6px 8px; font-weight: 600; }}"
            f"QProgressBar {{ background: {TOKENS.background}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 6px; text-align: center; }}"
            f"QProgressBar::chunk {{ background: {TOKENS.primary}; border-radius: 5px; }}"
            f"QPushButton {{ background: {TOKENS.primary}; color: #f8fbff; border: 0; border-radius: 8px; padding: 7px 12px; }}"
            f"QPushButton:hover {{ background: #3a92e8; }}"
            f"QPushButton[class='secondary'] {{ background: #24415e; }}"
            f"QPushButton[class='secondary']:hover {{ background: #2d557d; }}"
            f"QPushButton[class='danger'] {{ background: #8d3a31; }}"
            f"QPushButton[class='danger']:hover {{ background: #a8453a; }}"
            f"QPushButton:disabled, QPushButton[class='secondary']:disabled, QPushButton[class='danger']:disabled {{ background: #1c222b; color: #64748b; border: 1px solid {TOKENS.border}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        # Top workspace bar
        top_bar = QFrame()
        top_bar.setObjectName("marketTopBar")
        top_row = QHBoxLayout(top_bar)
        top_row.setContentsMargins(10, 8, 10, 8)
        top_row.setSpacing(8)
        self.scope_label = QLabel("Выбрано аккаунтов: 0")
        self.scope_label.setWordWrap(True)
        top_row.addWidget(self.scope_label, 1)
        self.refresh_selection_btn = QPushButton("Обновить выбор")
        self.refresh_selection_btn.setProperty("class", "secondary")
        self.refresh_selection_btn.clicked.connect(self.refresh_selection_scope)
        self.sync_all_btn = QPushButton("1-клик Синхронизировать всё…")
        self.sync_all_btn.setMinimumWidth(220)
        self.sync_all_btn.clicked.connect(self.sync_workspace)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setProperty("class", "danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(lambda: self.request({"_action": "stop"}, self.generation))
        for btn in (self.refresh_selection_btn, self.sync_all_btn, self.stop_btn):
            btn.setMinimumHeight(32)
            top_row.addWidget(btn)
        layout.addWidget(top_bar)

        # Mode + Filters
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.mode = QComboBox()
        self.mode.addItem("Available gifts for rent", "catalog")
        self.mode.addItem("Available usernames for rent", "usernames")
        self.mode.addItem("My active rentals", "rentals")
        self.mode.currentIndexChanged.connect(self.change_mode)
        mode_row.addWidget(self.mode, 1)

        self.limit = QSpinBox()
        self.limit.setRange(1, 100)
        self.limit.setValue(20)
        self.limit.setPrefix("Limit: ")
        self.limit.valueChanged.connect(self.invalidate)
        mode_row.addWidget(self.limit)

        self.sort = QComboBox()
        for title, key in (
            ("Daily price", "price_per_day"),
            ("Minimum price", "min_price"),
            ("Recently updated", "recently_touch"),
            ("Duration ascending", "duration_asc"),
            ("Duration descending", "duration_desc"),
        ):
            self.sort.addItem(title, key)
        self.sort.currentIndexChanged.connect(self.invalidate)
        mode_row.addWidget(self.sort)
        layout.addLayout(mode_row)

        form = QFormLayout()
        form.setSpacing(6)
        self.form = form
        self.filters = {}
        filter_row1 = QHBoxLayout()
        filter_row2 = QHBoxLayout()
        for idx, (key, title, maximum) in enumerate((
            ("collection_address", "Collection", 100),
            ("model", "Model", 200),
            ("symbol", "Symbol", 200),
            ("backdrop", "Backdrop", 200),
        )):
            field = QLineEdit()
            field.setMaxLength(maximum)
            field.setPlaceholderText(title + " (optional)")
            field.textChanged.connect(self.invalidate)
            self.filters[key] = field
            (filter_row1 if idx < 2 else filter_row2).addWidget(field)
        form.addRow("Gift filters", filter_row1)
        form.addRow("Traits", filter_row2)
        layout.addLayout(form)

        fetch_row = QHBoxLayout()
        fetch_row.setSpacing(8)
        self.sellable = QCheckBox("Include sellable during rent (may end early)")
        self.sellable.toggled.connect(self.invalidate)
        fetch_row.addWidget(self.sellable, 1)
        self.fetch = QPushButton("Read catalog")
        self.next_page = QPushButton("Next page")
        self.next_page.setProperty("class", "secondary")
        self.next_page.setEnabled(False)
        self.fetch.clicked.connect(lambda: self.read(False))
        self.next_page.clicked.connect(lambda: self.read(True))
        for button in (self.fetch, self.next_page):
            button.setMinimumHeight(34)
            fetch_row.addWidget(button)
        layout.addLayout(fetch_row)

        # Split pane: Items (left) + Detail (right)
        split = QHBoxLayout()
        split.setSpacing(10)
        self.items = QListWidget()
        self.items.setWordWrap(True)
        self.items.setMinimumHeight(180)
        self.items.currentRowChanged.connect(self.choose)
        split.addWidget(self.items, 1)

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(180)
        self.detail.setPlainText("Выберите элемент в списке слева или нажмите «1-клик Синхронизировать всё…».")
        split.addWidget(self.detail, 1)
        layout.addLayout(split, 1)

        # Bottom Action Tabs
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Tab 0: 1-Click Rent via Wallet X
        rent_tab = QWidget()
        rent_tab.setObjectName("marketTabPage")
        r_layout = QVBoxLayout(rent_tab)
        r_layout.setContentsMargins(10, 10, 10, 10)
        r_layout.setSpacing(8)

        r_row1 = QHBoxLayout()
        r_row1.setSpacing(8)
        r_row1.addWidget(QLabel("Целевой аккаунт:"))
        self.rent_account = QComboBox()
        self.rent_account.setMinimumContentsLength(20)
        self.rent_account.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.rent_account.currentIndexChanged.connect(self._invalidate_quote)
        r_row1.addWidget(self.rent_account, 1)

        r_row1.addWidget(QLabel("Срок (дней):"))
        self.rent_days = QSpinBox()
        self.rent_days.setRange(1, 365)
        self.rent_days.setValue(1)
        self.rent_days.valueChanged.connect(self._invalidate_quote)
        r_row1.addWidget(self.rent_days)

        self.auto_visible_chk = QCheckBox("Авто-показать в профиле")
        self.auto_visible_chk.setChecked(True)
        r_row1.addWidget(self.auto_visible_chk)
        r_layout.addLayout(r_row1)

        self.quote_label = QLabel("Выберите подарок/юзернейм в каталоге → нажмите «Рассчитать аренду (Pay)».")
        self.quote_label.setTextFormat(Qt.PlainText)
        self.quote_label.setWordWrap(True)
        r_layout.addWidget(self.quote_label)

        r_btns = QHBoxLayout()
        r_btns.setSpacing(8)
        self.quote_rent_btn = QPushButton("Рассчитать и эмулировать (Pay)")
        self.quote_rent_btn.setProperty("class", "secondary")
        self.execute_rent_btn = QPushButton("Подтвердить и арендовать (Wallet X)…")
        self.quote_rent_btn.clicked.connect(lambda: self.prepare_rent("pay"))
        self.execute_rent_btn.clicked.connect(self.execute_rent)
        self.execute_rent_btn.setEnabled(False)
        for btn in (self.quote_rent_btn, self.execute_rent_btn):
            btn.setMinimumHeight(36)
            r_btns.addWidget(btn)
        r_layout.addLayout(r_btns)

        self.rent_progress = QProgressBar()
        self.rent_progress.setRange(0, 5)
        self.rent_progress.setValue(0)
        r_layout.addWidget(self.rent_progress)
        self.tabs.addTab(rent_tab, "Быстрая аренда (1-клик)")

        # Tab 1: My Rentals & Hosting Visibility
        rentals_tab = QWidget()
        rentals_tab.setObjectName("marketTabPage")
        v_layout = QVBoxLayout(rentals_tab)
        v_layout.setContentsMargins(10, 10, 10, 10)
        v_layout.setSpacing(8)

        self.hosting_info = QLabel(
            'First hosting/relink and full detach: <a href="https://fragment.com/" style="color:#6cb6ff;">official Fragment</a>. '
            'Hiding is NOT detaching hosting or ending rent.'
        )
        self.hosting_info.setWordWrap(True)
        self.hosting_info.setOpenExternalLinks(True)
        v_layout.addWidget(self.hosting_info)

        hosting = QHBoxLayout()
        hosting.setSpacing(8)
        self.scan = QPushButton("Check selected account…")
        self.batch_scan_btn = QPushButton("Batch Scan выбранных")
        self.show_gift = QPushButton("Show on profile")
        self.hide_gift = QPushButton("Hide from profile")
        for btn, act in (
            (self.scan, "scan"),
            (self.show_gift, "show"),
            (self.hide_gift, "hide"),
        ):
            btn.setMinimumHeight(34)
            btn.clicked.connect(lambda checked=False, value=act: self.hosting_action(value))
            hosting.addWidget(btn)
        self.batch_scan_btn.setMinimumHeight(34)
        self.batch_scan_btn.setProperty("class", "secondary")
        self.batch_scan_btn.clicked.connect(self.run_batch_scan)
        hosting.addWidget(self.batch_scan_btn)
        v_layout.addLayout(hosting)

        batch_vis_row = QHBoxLayout()
        batch_vis_row.setSpacing(8)
        self.show_all_btn = QPushButton("Показать ВСЕ (акк)")
        self.hide_all_btn = QPushButton("Скрыть ВСЕ (акк)")
        self.extend_btn = QPushButton("Продлить выбранную…")
        self.cancel_btn = QPushButton("Отменить аренду…")
        self.tonconnect_btn = QPushButton("Привязать TonConnect…")
        for btn in (self.show_all_btn, self.hide_all_btn, self.extend_btn, self.tonconnect_btn):
            btn.setProperty("class", "secondary")
        self.cancel_btn.setProperty("class", "danger")
        self.show_all_btn.clicked.connect(lambda: self.run_batch_visibility(True))
        self.hide_all_btn.clicked.connect(lambda: self.run_batch_visibility(False))
        self.extend_btn.clicked.connect(lambda: self.prepare_rent("extend"))
        self.cancel_btn.clicked.connect(lambda: self.prepare_rent("cancel"))
        self.tonconnect_btn.clicked.connect(self.submit_tonconnect_uri)
        for btn in (self.show_all_btn, self.hide_all_btn, self.extend_btn, self.cancel_btn, self.tonconnect_btn):
            btn.setMinimumHeight(34)
            batch_vis_row.addWidget(btn)
        v_layout.addLayout(batch_vis_row)
        self.tabs.addTab(rentals_tab, "Мои аренды и Видимость")

        # Tab 2: Account Matrix, Batch Rent Funding & Resume Jobs
        ops_tab = QWidget()
        ops_tab.setObjectName("marketTabPage")
        o_layout = QVBoxLayout(ops_tab)
        o_layout.setContentsMargins(10, 10, 10, 10)
        o_layout.setSpacing(8)

        self.matrix_table = QTableWidget(0, 5)
        self.matrix_table.verticalHeader().setVisible(False)
        self.matrix_table.setHorizontalHeaderLabels(["Аккаунт", "@username", "TON баланс", "Хостинг", "Статус / Ошибка"])
        self.matrix_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.matrix_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.matrix_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.matrix_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.matrix_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.matrix_table.setMinimumHeight(110)
        self.matrix_table.setEditTriggers(QTableWidget.NoEditTriggers)
        o_layout.addWidget(self.matrix_table, 1)

        fund_row = QHBoxLayout()
        fund_row.setSpacing(8)
        self.fund_mode = QComboBox()
        self.fund_mode.addItem("Fixed TON/акк", "fixed")
        self.fund_mode.addItem("Top-up до TON", "top_up_to")
        self.fund_amount = QLineEdit("0.15")
        self.fund_amount.setFixedWidth(84)
        self.fund_batch_btn = QPushButton("Пополнить выбранные из Wallet X…")
        self.fund_batch_btn.clicked.connect(self.start_batch_rent_funding)
        self.fund_batch_btn.setMinimumHeight(34)
        fund_row.addWidget(QLabel("Грамы на аренду:"))
        fund_row.addWidget(self.fund_mode)
        fund_row.addWidget(self.fund_amount)
        fund_row.addWidget(self.fund_batch_btn, 1)
        o_layout.addLayout(fund_row)

        jobs_row = QHBoxLayout()
        jobs_row.setSpacing(8)
        self.jobs_combo = QComboBox()
        self.jobs_combo.setMinimumContentsLength(24)
        self.jobs_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.jobs_combo.currentIndexChanged.connect(lambda *_: self.hosting_controls())
        self.resume_job_btn = QPushButton("Продолжить / Сверить задачу")
        self.resume_job_btn.setProperty("class", "secondary")
        self.resume_job_btn.setMinimumHeight(34)
        self.resume_job_btn.setMinimumWidth(212)
        self.resume_job_btn.clicked.connect(self.resume_selected_job)
        jobs_row.addWidget(self.jobs_combo, 1)
        jobs_row.addWidget(self.resume_job_btn)
        o_layout.addLayout(jobs_row)
        self.tabs.addTab(ops_tab, "Аккаунты, Грамы и Задачи")

        # Footer
        footer = QHBoxLayout()
        self.message = QLabel(
            "Token is loaded from the dedicated Marketapp vault (or backend environment fallback if no vault exists)."
        )
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.PlainText)
        footer.addWidget(self.message, 1)
        close = QPushButton("Close")
        close.setProperty("class", "secondary")
        close.setMinimumHeight(34)
        close.clicked.connect(self.close)
        footer.addWidget(close)
        layout.addLayout(footer)

        self._quote_timer = QTimer(self)
        self._quote_timer.timeout.connect(self._tick_quote)
        self._quote_timer.start(1000)

        self.refresh_selection_scope()
        self.hosting_controls()

    def _current_selection(self) -> list[str]:
        raw = self.selected_accounts() or []
        ids: list[str] = []
        for item in raw:
            aid = item.get("id") if isinstance(item, dict) else str(item)
            if aid and aid not in ids:
                ids.append(aid)
        return ids

    def refresh_selection_scope(self) -> None:
        ids = self._current_selection()
        preview = ", ".join(ids[:4]) + (f" (+{len(ids) - 4})" if len(ids) > 4 else "")
        self.scope_label.setText(f"Выбрано аккаунтов в таблице: {len(ids)}" + (f" · {preview}" if preview else " (отметьте аккаунты в таблице)"))

        prev_acc = self.rent_account.currentData()
        self.rent_account.blockSignals(True)
        self.rent_account.clear()
        matrix_map = {row.get("account_id"): row for row in self.account_matrix if isinstance(row, dict)}
        ordered_ids = list(dict.fromkeys([*ids, *matrix_map.keys()]))
        for aid in ordered_ids:
            info = matrix_map.get(aid, {})
            uname = f"@{info['username']}" if info.get("username") else ""
            bal = info.get("balance")
            bal_txt = f" · {bal.get('balance_native')} TON" if isinstance(bal, dict) and bal.get("balance_native") is not None else ""
            self.rent_account.addItem(f"{aid} {uname}{bal_txt}".strip(), aid)
        idx = self.rent_account.findData(prev_acc) if prev_acc else 0
        self.rent_account.setCurrentIndex(max(0, idx))
        self.rent_account.blockSignals(False)
        self.hosting_controls()

    def _invalidate_quote(self, *_args) -> None:
        if self.rent_quote is not None and not self.pending:
            self.rent_quote = None
            self.quote_label.setText("Параметры изменены — нажмите «Рассчитать и эмулировать» заново.")
            self.hosting_controls()

    def _tick_quote(self) -> None:
        if self._confirming_dialog or self.pending or self.rent_quote is None:
            return
        valid_until = float(self.rent_quote.get("valid_until") or 0)
        rem = int(valid_until - time.time()) if valid_until > 0 else 0
        if valid_until > 0 and rem <= 0:
            self.rent_quote = None
            self.quote_label.setText("Котировка истекла. Нажмите «Рассчитать и эмулировать» заново.")
            self.hosting_controls()

    def invalidate(self, *args):
        self.page = None
        self.review_token = None
        self.scan_account = None
        self.rent_quote = None
        self.items.clear()
        self.detail.setPlainText("No item selected. Selection is local only.")
        self.next_page.setEnabled(False)
        if hasattr(self, "scan"):
            self.hosting_controls()

    def change_mode(self):
        self.generation += 1
        self.invalidate()
        self.controls(False)
        mode = self.mode.currentData()
        if mode == "rentals":
            self.fetch.setText("Read my rentals")
        elif mode == "usernames":
            self.fetch.setText("Read usernames")
        else:
            self.fetch.setText("Read catalog")

    def hosting_controls(self):
        rentals = self.mode.currentData() == "rentals"
        selected = self._current_selection()
        self.scan.setEnabled(not self.pending and rentals and len(selected) == 1)
        self.batch_scan_btn.setEnabled(not self.pending and bool(selected))
        index = self.items.currentRow()
        item = self.page["items"][index] if self.page and 0 <= index < len(self.page.get("items", [])) else None
        host = (
            next(
                (
                    row
                    for row in item.get("observed_accounts", [])
                    if row["account_id"] == self.scan_account and row["hosting_verified"] and row["fresh"]
                ),
                None,
            )
            if item
            else None
        )
        allowed = not self.pending and rentals and bool(self.review_token and host and not item.get("expired") and selected == [self.scan_account])
        self.show_gift.setEnabled(allowed and not host["visible"])
        self.hide_gift.setEnabled(allowed and host["visible"])

        has_single_acc = len(selected) == 1
        self.show_all_btn.setEnabled(not self.pending and has_single_acc)
        self.hide_all_btn.setEnabled(not self.pending and has_single_acc)
        self.extend_btn.setEnabled(not self.pending and rentals and bool(item))
        self.cancel_btn.setEnabled(not self.pending and rentals and bool(item))
        self.tonconnect_btn.setEnabled(not self.pending and bool(item))

        can_quote = not self.pending and not rentals and bool(item)
        self.quote_rent_btn.setEnabled(can_quote)
        self.execute_rent_btn.setEnabled(not self.pending and bool(self.rent_quote))
        self.sync_all_btn.setEnabled(not self.pending)
        self.fund_batch_btn.setEnabled(not self.pending and bool(selected))
        self.stop_btn.setEnabled(self.pending)
        job = self.jobs_combo.currentData() if hasattr(self, "jobs_combo") else None
        self.resume_job_btn.setEnabled(
            not self.pending and isinstance(job, dict) and job.get("state") not in {"completed", "cancelled", "failed", None}
        )

    def controls(self, busy):
        self.pending = bool(busy)
        mode = self.mode.currentData()
        rentals = mode == "rentals"
        usernames = mode == "usernames"
        for key, field in self.filters.items():
            visible = not rentals and not usernames
            field.setEnabled(not self.pending and visible)
        self.form.setRowVisible(0, not rentals and not usernames)
        self.form.setRowVisible(1, not rentals and not usernames)
        self.sort.setEnabled(not self.pending and not rentals)
        self.sellable.setVisible(not rentals)
        self.sellable.setEnabled(not self.pending and not rentals)
        for field in (self.limit, self.mode, self.fetch, self.refresh_selection_btn, self.rent_account, self.rent_days, self.auto_visible_chk):
            field.setEnabled(not self.pending)
        self.next_page.setEnabled(not self.pending and bool(self.page and self.page.get("cursor")))
        self.hosting_controls()

    def read(self, next_page):
        if self.pending or (next_page and not (self.page and self.page.get("cursor"))):
            return
        mode = self.mode.currentData()
        if mode == "rentals":
            payload = {"confirmed": True, "limit": self.limit.value(), "_action": "rentals"}
        elif mode == "usernames":
            payload = {
                "_action": "usernames",
                "confirmed": True,
                "limit": self.limit.value(),
                "sort_by": self.sort.currentData(),
                "include_sellable": self.sellable.isChecked(),
            }
        else:
            payload = {key: field.text().strip() for key, field in self.filters.items() if field.text().strip()}
            payload.update(
                confirmed=True,
                limit=self.limit.value(),
                sort_by=self.sort.currentData(),
                include_sellable=self.sellable.isChecked(),
            )
        if next_page:
            payload["cursor"] = self.page["cursor"]
        self.generation += 1
        self.invalidate()
        self.controls(True)
        self.message.setText("Reading bounded MarketApp page…")
        self.request(payload, self.generation)

    def sync_workspace(self) -> None:
        if self.pending:
            return
        self.refresh_selection_scope()
        self.generation += 1
        self.controls(True)
        self.message.setText("1-клик синхронизация: каталог, мои аренды, хостинг выбранных аккаунтов и балансы…")
        self.request(
            {
                "_action": "sync-workspace",
                "account_ids": self._current_selection(),
                "limit": self.limit.value(),
                "sort_by": self.sort.currentData(),
                "include_sellable": self.sellable.isChecked(),
            },
            self.generation,
        )

    def _populate_items(self, page: dict) -> None:
        self.page = page
        self.items.clear()
        for item in self.page.get("items", []):
            rate = item.get("price_per_day_formatted") or self._format_rate(item.get("price_per_day_nanograms"))
            extra = f" · {item['countdown_text']}" if item.get("countdown_text") else ""
            self.items.addItem(f"{item['nft_name']} · {rate}{extra}")
        self.items.setCurrentRow(0 if self.page.get("items") else -1)

    def _render_matrix_and_jobs(self, matrix: list[dict] | None, jobs: list[dict] | None) -> None:
        if matrix is not None:
            self.account_matrix = list(matrix)
            self.matrix_table.setRowCount(len(self.account_matrix))
            for r_idx, row in enumerate(self.account_matrix):
                uname = f"@{row['username']}" if row.get("username") else "—"
                bal = row.get("balance") or {}
                bal_str = f"{bal['balance_native']} TON" if bal.get("balance_native") is not None else "—"
                hosted_cnt = str(row.get("hosted_gift_count", 0))
                err = row.get("scan_error") or (f"rent @{row['matched_username_rental']['nft_name']}" if row.get("matched_username_rental") else "ok")
                for c_idx, val in enumerate((row["account_id"], uname, bal_str, hosted_cnt, str(err))):
                    self.matrix_table.setItem(r_idx, c_idx, QTableWidgetItem(val))
            self.refresh_selection_scope()
        if jobs is not None:
            self.jobs_data = list(jobs)
            self.jobs_combo.blockSignals(True)
            self.jobs_combo.clear()
            for j in self.jobs_data:
                self.jobs_combo.addItem(
                    f"{j['operation']} · {j['state']} · {j.get('account_id') or '-'} · {j['job_id'][:8]}",
                    j,
                )
            self.jobs_combo.blockSignals(False)

    def render(self, result, generation, action=None):
        if generation != self.generation or not self.isVisible():
            return
        if isinstance(result, dict) and result.get("progress"):
            ev = result["progress"]
            stage = str(ev.get("stage") or "")
            stages = {"tx_sending": 1, "marketapp_polling": 2, "tonconnect": 3, "telegram_hosting": 4, "auto_visible": 5, "completed": 5}
            self.rent_progress.setValue(stages.get(stage, self.rent_progress.value()))
            self.message.setText(f"[{stage}] {ev.get('detail', '')}")
            return

        self.controls(False)
        if not result.get("ok"):
            if action in {"catalog", "usernames", "rentals"}:
                self.invalidate()
            self.message.setText(result.get("error", "Operation unavailable; retry explicitly later"))
            return

        if action == "sync-workspace":
            ws = result.get("workspace") or {}
            self._render_matrix_and_jobs(ws.get("account_matrix"), ws.get("jobs"))
            mode = self.mode.currentData()
            chosen_page = ws.get("rentals") if mode == "rentals" else (ws.get("usernames_catalog") if mode == "usernames" else ws.get("gifts_catalog"))
            if chosen_page:
                self._populate_items(chosen_page)
            errs = ws.get("section_errors") or {}
            err_note = f" · Предупреждения: {', '.join(errs.keys())}" if errs else ""
            self.message.setText(
                f"Синхронизация завершена: {len(self.account_matrix)} акк. · Задач: {len(self.jobs_data)}{err_note}"
            )
            self.controls(False)
            return

        if action == "rent-quote":
            q = result["quote"]
            self.rent_quote = q
            gram_str = str(q.get("total_gram_formatted") or "-")
            if gram_str != "-" and "GRAM" not in gram_str.upper():
                gram_str += " GRAM"
            self.quote_label.setText(
                f"Котировка [{q['operation'].upper()}]: {gram_str} + {q.get('total_ton')} TON газ "
                f"· срок {q.get('duration_text') or '-'} · TTL {q.get('expires_in_seconds')}с"
            )
            self.message.setText("Расчёт готов. Нажмите «Подтвердить и арендовать (Wallet X)…» (или выполните Extend/Cancel).")
            if q["operation"] in {"extend", "cancel"}:
                self.tabs.setCurrentIndex(0)
                self._confirm_and_execute_quote(q)
            self.controls(False)
            return

        if action in {"rent-execute", "rent-resume"}:
            self.rent_quote = None
            res = result.get("pipeline") or {}
            job = res.get("job") or {}
            self._render_matrix_and_jobs(None, result.get("jobs"))
            self.rent_progress.setValue(5 if res.get("ok") else 0)
            if res.get("state") == "handoff_required":
                self.quote_label.setText(
                    f"Wallet X недоступен — задача {job.get('job_id', '')[:8]} ожидает внешней подписи.\n"
                    f"Отправьте транзакцию вручную и нажмите Resume во вкладке Задачи."
                )
                self.message.setText("Требуется внешняя подпись (handoff_required); затем нажмите «Продолжить / Сверить задачу».")
            else:
                self.quote_label.setText(f"Задача {job.get('job_id', '')[:8]}: статус {res.get('state')}")
                self.message.setText(f"Аренда обработана: статус {res.get('state')}.")
            self.controls(False)
            return

        if action == "batch-fund-quote":
            self._confirm_and_execute_batch_fund(result["preview"])
            return

        if action == "batch-fund-execute":
            res = result.get("batch") or {}
            self.message.setText(
                f"Пополнение завершено ({res.get('state')}): подтверждено {res.get('confirmed', 0)}/{res.get('total', 0)}, ошибок {res.get('failed', 0)}."
            )
            self.controls(False)
            return

        if action == "batch-scan":
            scan = result.get("batch_scan") or {}
            tokens = scan.get("review_tokens") or {}
            sel = self._current_selection()
            if len(sel) == 1 and sel[0] in tokens:
                self.scan_account = sel[0]
                self.review_token = tokens[sel[0]]
            if scan.get("catalog"):
                self._populate_items(scan["catalog"])
            self.message.setText(
                f"Batch Scan завершён: проверено {scan.get('scanned_count', 0)} акк., ошибок {scan.get('error_count', 0)}."
            )
            self.controls(False)
            return

        if action == "tonconnect":
            self.message.setText("TonConnect привязка отправлена на MarketApp.")
            self.controls(False)
            return

        self.review_token = result.get("review_token") if result.get("complete") is not False else None
        self.scan_account = result.get("account_id") or self.scan_account
        if result.get("catalog"):
            self._populate_items(result["catalog"])
            checked = datetime.fromtimestamp(self.page["checked_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            self.message.setText(f"Provider snapshot: {checked}. Availability can change; not a quote.")
        if result.get("complete") is False:
            self.message.setText("Own gift scan is incomplete (bounded to 500). Visibility changes disabled.")
        elif result.get("detail"):
            self.message.setText(result["detail"])
        self.controls(False)

    @staticmethod
    def _format_rate(nanograms):
        try:
            return f"{format_nano(str(nanograms))} GRAM/day"
        except Exception:
            return f"{nanograms} nanograms/day"

    @staticmethod
    def _format_end_time(end_time):
        if isinstance(end_time, (int, float)) and end_time > 0:
            end_dt = datetime.fromtimestamp(float(end_time), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            remaining = int(float(end_time) - time.time())
            if remaining <= 0:
                return f"{end_dt} (expired)"
            days, rem = divmod(remaining, 86400)
            hours = rem // 3600
            return f"{end_dt} ({days}d {hours:02d}h left)"
        return str(end_time)

    def choose(self, index):
        if not self.page or not 0 <= index < len(self.page.get("items", [])):
            self.hosting_controls()
            return
        self._invalidate_quote()
        item = self.page["items"][index]
        rate = item.get("price_per_day_formatted") or self._format_rate(item.get("price_per_day_nanograms"))
        if item.get("my_rented"):
            hosts = "; ".join(
                row["account_id"]
                + (" — host verified" if row["hosting_verified"] else " — hosting unknown")
                + (", shown" if row["visible"] else ", hidden")
                + ("" if row["fresh"] else " (stale)")
                for row in item.get("observed_accounts", [])
            )
            end_str = item.get("end_time_utc") or self._format_end_time(item.get("end_time"))
            if item.get("countdown_text"):
                end_str = f"{end_str} ({item['countdown_text']})"
            self.detail.setPlainText(
                f"{item['nft_name']}\nNFT: {item['nft_address']}\n"
                f"Rent ends: {end_str} · expired: {item['expired']}\nPrice/day: {rate}\n"
                f"Early termination risk: {item['early_termination_risk']}\nObserved accounts: {hosts or 'not checked'}\n"
                "Only explicitly checked accounts. Hiding is not detaching hosting."
            )
            self.hosting_controls()
            return
        dur_min = item.get("min_duration_text") or f"{item.get('min_duration')}s"
        dur_max = item.get("max_duration_text") or f"{item.get('max_duration')}s"
        min_tot = f" · Min total: {item['min_total_formatted']} GRAM" if item.get("min_total_formatted") else ""
        self.detail.setPlainText(
            f"{item['nft_name']}\n{rate}{min_tot}\n"
            f"Duration: {dur_min} – {dur_max} · discount/day: {item.get('discount_per_day', 0)}%\n"
            + ("WARNING: sellable during rent; may end early.\n" if item.get("early_termination_risk") else "Provider reports not sellable during rent; not a guarantee.\n")
            + f"NFT: {item['nft_address']}\nВыберите целевой аккаунт и нажмите «Рассчитать и эмулировать (Pay)»."
        )
        self.hosting_controls()

    def prepare_rent(self, operation: str) -> None:
        index = self.items.currentRow()
        if self.pending or not self.page or not 0 <= index < len(self.page.get("items", [])):
            return
        item = self.page["items"][index]
        target_acc = self.rent_account.currentData() if operation == "pay" else None
        if operation == "pay" and not target_acc:
            self.message.setText("Выберите целевой аккаунт для аренды во вкладке «Быстрая аренда».")
            return
        self.generation += 1
        self.controls(True)
        self.message.setText(f"Запрашиваю расчёт MarketApp ({operation})…")
        asset_kind = "username" if (item.get("asset_kind") == "username" or self.mode.currentData() == "usernames") else "gift"
        self.request(
            {
                "_action": "rent-quote",
                "operation": operation,
                "nft_address": item["nft_address"],
                "nft_addresses": [item["nft_address"]],
                "nft_name": item.get("nft_name"),
                "asset_kind": asset_kind,
                "duration_days": self.rent_days.value(),
                "price_per_day": item.get("price_per_day_nanograms"),
                "sale_during_rent": bool(item.get("early_termination_risk")),
                "account_id": target_acc,
                "auto_visible": self.auto_visible_chk.isChecked(),
            },
            self.generation,
        )

    def execute_rent(self) -> None:
        if self.rent_quote and not self.pending:
            self._confirm_and_execute_quote(self.rent_quote)

    def _confirm_and_execute_quote(self, q: dict) -> None:
        expected = q["confirmation_text"]
        gram_str = str(q.get("total_gram_formatted") or "-")
        if gram_str != "-" and "GRAM" not in gram_str.upper():
            gram_str += " GRAM"
        self._confirming_dialog = True
        try:
            phrase, ok = QInputDialog.getText(
                self,
                f"Подтвердить MarketApp {q['operation'].upper()}",
                f"Позиция: {q['nft_address']}\n"
                f"Аккаунт: {q.get('account_id') or '-'}\n"
                f"Срок: {q.get('duration_text') or '-'}\n"
                f"Стоимость: {gram_str} + {q.get('total_ton')} TON (газ/депозит)\n"
                f"Авто-показ в профиле: {q.get('auto_visible')}\n"
                f"Для оплаты через Wallet X введите точно:\n{expected}",
                text=expected,
            )
        finally:
            self._confirming_dialog = False
        if not ok or (phrase or "").strip() != expected:
            self.message.setText("Аренда отменена пользователем.")
            return
        self.generation += 1
        self.controls(True)
        self.rent_progress.setValue(0)
        self.message.setText("Выполняется автоматический пайплайн аренды (Wallet X -> MarketApp -> Профиль)…")
        self.request(
            {
                "_action": "rent-execute",
                "job_id": q["job_id"],
                "review_token": q["review_token"],
                "confirmation": expected,
                "auto_visible": self.auto_visible_chk.isChecked(),
            },
            self.generation,
        )

    def hosting_action(self, action):
        if self.pending or self.mode.currentData() != "rentals" or not self.page:
            return
        ids = self._current_selection()
        if len(ids) != 1:
            self.message.setText("Select exactly one enabled account in the current group/filter.")
            return
        payload = {"confirmed": True, "account_id": ids[0], "_action": "scan" if action == "scan" else "visibility"}
        if action != "scan":
            index = self.items.currentRow()
            if not self.review_token or self.scan_account != ids[0] or not 0 <= index < len(self.page["items"]):
                return
            payload.update(review_token=self.review_token, nft_address=self.page["items"][index]["nft_address"], visible=action == "show")
        if action == "scan":
            self.review_token = None
            self.items.clear()
            self.detail.clear()
        self.generation += 1
        self.controls(True)
        self.message.setText("Checking exact rental/account visibility…")
        self.request(payload, self.generation)

    def run_batch_scan(self) -> None:
        ids = self._current_selection()
        if self.pending or not ids:
            return
        self.generation += 1
        self.controls(True)
        self.message.setText(f"Сканирую хостинг для {len(ids)} выбранных аккаунтов…")
        self.request({"_action": "batch-scan", "account_ids": ids}, self.generation)

    def run_batch_visibility(self, visible: bool) -> None:
        ids = self._current_selection()
        if self.pending or len(ids) != 1:
            self.message.setText("Выберите ровно 1 аккаунт для массового показа/скрытия подарков.")
            return
        self.generation += 1
        self.controls(True)
        self.message.setText(f"{'Показываю' if visible else 'Скрываю'} все активные арендованные подарки на {ids[0]}…")
        self.request(
            {
                "_action": "batch-visibility",
                "account_id": ids[0],
                "visible": visible,
                "review_token": self.review_token if self.scan_account == ids[0] else None,
            },
            self.generation,
        )

    def submit_tonconnect_uri(self) -> None:
        index = self.items.currentRow()
        if self.pending or not self.page or not 0 <= index < len(self.page.get("items", [])):
            return
        item = self.page["items"][index]
        self._confirming_dialog = True
        try:
            uri, ok = QInputDialog.getText(self, "TonConnect URI", f"Вставьте tonconnect URL для {item['nft_address']}:")
        finally:
            self._confirming_dialog = False
        if not ok or not (uri or "").strip():
            return
        self.generation += 1
        self.controls(True)
        self.message.setText("Отправляю TonConnect привязку…")
        self.request({"_action": "tonconnect", "nft_address": item["nft_address"], "tonconnect_url": uri.strip()}, self.generation)

    def start_batch_rent_funding(self) -> None:
        ids = self._current_selection()
        if self.pending or not ids:
            return
        amt = self.fund_amount.text().strip()
        mode = self.fund_mode.currentData()
        self.generation += 1
        self.controls(True)
        self.message.setText("Готовлю предпросмотр пополнения выбранных аккаунтов из Wallet X…")
        self.request({"_action": "batch-fund-quote", "account_ids": ids, "mode": mode, "amount_ton": amt}, self.generation)

    def _confirm_and_execute_batch_fund(self, preview: dict) -> None:
        expected = preview["expected_confirmation"]
        self._confirming_dialog = True
        try:
            phrase, ok = QInputDialog.getText(
                self,
                "Пополнить аккаунты из Wallet X",
                f"Аккаунтов к пополнению: {len(preview.get('items', []))} (пропущено {len(preview.get('skipped', []))})\n"
                f"Итого: {preview.get('total_ton')} TON\nВведите точно:\n{expected}",
                text=expected,
            )
        finally:
            self._confirming_dialog = False
        if not ok or (phrase or "").strip() != expected:
            self.message.setText("Пополнение отменено.")
            return
        self.generation += 1
        self.controls(True)
        self.message.setText("Выполняется пополнение балансов из Wallet X…")
        self.request({"_action": "batch-fund-execute", "review_token": preview["review_token"], "confirmation": expected}, self.generation)

    def resume_selected_job(self) -> None:
        job = self.jobs_combo.currentData()
        if not isinstance(job, dict) or self.pending:
            return
        external_ok = False
        if job.get("state") == "handoff_required":
            if (
                QMessageBox.question(
                    self,
                    "Подтверждение внешней транзакции",
                    f"Задача {job['job_id'][:8]} ожидает внешней отправки TX.\nВы уже отправили транзакцию вручную и хотите продолжить?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                != QMessageBox.Yes
            ):
                return
            external_ok = True
        self.generation += 1
        self.controls(True)
        self.message.setText(f"Возобновляю задачу {job['job_id'][:8]} ({job['state']})…")
        self.request(
            {
                "_action": "rent-resume",
                "job_id": job["job_id"],
                "external_tx_submitted": external_ok,
                "auto_visible": self.auto_visible_chk.isChecked(),
            },
            self.generation,
        )

    def done(self, result):
        self.generation += 1
        self.invalidate()
        super().done(result)
