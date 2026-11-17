"""Standalone Master Wallet X management dialog (DPAPI vault, live balance, withdraw, batch rent funding)."""
from __future__ import annotations

from datetime import datetime, timezone
import re
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
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
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.design_tokens import TOKENS
from core.ton_address import format_nano, friendly_address


def _format_utc(ts: object) -> str:
    if isinstance(ts, (int, float)) and ts > 0:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(ts or "never")


def _safe_nano_ton(value: object) -> str:
    try:
        return format_nano(str(value))
    except Exception:
        return str(value or "0")


class WalletXDialog(QDialog):
    """Manage standalone Master Wallet X without binding to any Telegram account."""

    def __init__(
        self,
        status: dict,
        request,
        parent=None,
        *,
        selected_accounts=None,
        on_close_lock=None,
    ):
        super().__init__(parent)
        self.request = request
        self.selected_accounts = selected_accounts or (lambda: [])
        self.on_close_lock = on_close_lock
        self.status_data = dict(status or {})
        self.chain_data: dict | None = None
        self.intents_data: list[dict] = []
        self.review: dict | None = None
        self.batch_review: dict | None = None
        self.running = False
        self._confirming_dialog = False
        self._closing_confirmed = False

        self.setWindowTitle("Master Wallet X · Standalone TON Vault")
        self.resize(900, 820)
        self.setMinimumSize(680, 580)
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
        ):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.setStyleSheet(
            f"QDialog {{ background: {TOKENS.panel}; color: {TOKENS.text}; }}"
            f"QScrollArea#walletXScroll, QWidget#walletXBody, QWidget#walletXTabPage {{ background: {TOKENS.panel}; border: 0; }}"
            f"QFrame#walletXCard {{ background: {TOKENS.background}; border: 1px solid {TOKENS.border}; border-radius: 10px; }}"
            f"QTabWidget::pane {{ background: {TOKENS.panel}; border: 1px solid {TOKENS.border}; border-radius: 8px; padding: 6px; }}"
            f"QTabBar::tab {{ background: {TOKENS.panel_secondary}; color: {TOKENS.muted}; border: 1px solid {TOKENS.border}; border-bottom: 0; padding: 7px 14px; border-top-left-radius: 7px; border-top-right-radius: 7px; margin-right: 4px; }}"
            f"QTabBar::tab:selected {{ background: #1d4f8c; color: #ffffff; border-color: #4b8fd8; }}"
            f"QLabel {{ color: {TOKENS.text}; }}"
            f"QCheckBox {{ color: {TOKENS.text}; spacing: 8px; }}"
            f"QCheckBox:disabled {{ color: {TOKENS.muted}; }}"
            f"QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {TOKENS.border}; border-radius: 4px; background: {TOKENS.background}; }}"
            f"QCheckBox::indicator:checked {{ background: {TOKENS.primary}; border-color: #4b8fd8; }}"
            f"QLineEdit, QComboBox, QPlainTextEdit {{ background: {TOKENS.background}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 8px; padding: 7px 10px; }}"
            f"QComboBox QAbstractItemView {{ background: {TOKENS.panel_secondary}; color: {TOKENS.text}; selection-background-color: #1d4f8c; }}"
            f"QTableWidget {{ background: {TOKENS.background}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 8px; gridline-color: {TOKENS.border}; }}"
            f"QHeaderView::section {{ background: {TOKENS.panel_secondary}; color: {TOKENS.muted}; border: 0; border-right: 1px solid {TOKENS.border}; border-bottom: 1px solid {TOKENS.border}; padding: 6px 8px; font-weight: 600; }}"
            f"QProgressBar {{ background: {TOKENS.background}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 6px; text-align: center; }}"
            f"QProgressBar::chunk {{ background: {TOKENS.primary}; border-radius: 5px; }}"
            f"QPushButton {{ background: {TOKENS.primary}; color: #f8fbff; border: 0; border-radius: 8px; padding: 8px 12px; }}"
            f"QPushButton:hover {{ background: #3a92e8; }}"
            f"QPushButton[class='secondary'] {{ background: #24415e; }}"
            f"QPushButton[class='secondary']:hover {{ background: #2d557d; }}"
            f"QPushButton[class='danger'] {{ background: #8d3a31; }}"
            f"QPushButton[class='danger']:hover {{ background: #a8453a; }}"
            f"QPushButton:disabled, QPushButton[class='secondary']:disabled, QPushButton[class='danger']:disabled {{ background: #1c222b; color: #64748b; border: 1px solid {TOKENS.border}; }}"
            f"QScrollBar:vertical {{ background: {TOKENS.background}; width: 10px; }}"
            f"QScrollBar::handle:vertical {{ background: {TOKENS.border}; min-height: 24px; border-radius: 4px; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        header = self.label("Master Wallet X · Независимый мастер-кошелёк (DPAPI Vault, не привязан к Telegram-аккаунту)")
        header.setFont(QFont("Segoe UI", 12, QFont.DemiBold))
        outer.addWidget(header)

        # Vault & Address Header Card
        card = QFrame()
        card.setObjectName("walletXCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(8)

        self.vault_status_label = self.label("")
        card_layout.addWidget(self.vault_status_label)

        self.address_label = self.label("")
        self.address_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card_layout.addWidget(self.address_label)

        self.chain_status_label = self.label("Баланс: не обновлялся · Нажмите «Обновить баланс и историю».")
        self.chain_status_label.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
        card_layout.addWidget(self.chain_status_label)

        setup_row = QHBoxLayout()
        setup_row.setSpacing(8)
        self.create_btn = QPushButton("Создать Wallet X (24 слова)…")
        self.import_btn = QPushButton("Импорт Seed / 24 слова…")
        self.unlock_btn = QPushButton("Разблокировать")
        self.lock_btn = QPushButton("Заблокировать")
        self.export_seed_btn = QPushButton("Показать Seed…")
        self.delete_btn = QPushButton("Удалить Wallet X…")
        self.delete_btn.setProperty("class", "danger")
        for btn in (self.import_btn, self.unlock_btn, self.lock_btn, self.export_seed_btn):
            btn.setProperty("class", "secondary")
        self.create_btn.clicked.connect(self._create_wallet)
        self.import_btn.clicked.connect(self._import_wallet)
        self.unlock_btn.clicked.connect(lambda: self._dispatch("unlock", {"confirmed": True}))
        self.lock_btn.clicked.connect(lambda: self._dispatch("lock", {}))
        self.export_seed_btn.clicked.connect(self._export_mnemonic)
        self.delete_btn.clicked.connect(self._delete_wallet)
        for btn in (self.create_btn, self.import_btn, self.unlock_btn, self.lock_btn, self.export_seed_btn, self.delete_btn):
            btn.setMinimumHeight(34)
            setup_row.addWidget(btn)
        card_layout.addLayout(setup_row)

        addr_row = QHBoxLayout()
        addr_row.setSpacing(8)
        self.refresh_chain_btn = QPushButton("Обновить баланс и историю")
        self.copy_uq_btn = QPushButton("Копировать UQ")
        self.copy_eq_btn = QPushButton("Копировать EQ")
        for btn in (self.copy_uq_btn, self.copy_eq_btn):
            btn.setProperty("class", "secondary")
        self.refresh_chain_btn.clicked.connect(lambda: self._dispatch("refresh-chain", {"include_history": True}))
        self.copy_uq_btn.clicked.connect(lambda: self._copy_address("non_bounceable_address", "UQ"))
        self.copy_eq_btn.clicked.connect(lambda: self._copy_address("friendly_address", "EQ"))
        for btn in (self.refresh_chain_btn, self.copy_uq_btn, self.copy_eq_btn):
            btn.setMinimumHeight(34)
            addr_row.addWidget(btn)
        card_layout.addLayout(addr_row)
        outer.addWidget(card)

        # Tabs
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        # Tab 0: Withdraw / Send-All
        withdraw_tab = QWidget()
        withdraw_tab.setObjectName("walletXTabPage")
        w_layout = QVBoxLayout(withdraw_tab)
        w_layout.setContentsMargins(12, 12, 12, 12)
        w_layout.setSpacing(10)

        w_form = QFormLayout()
        w_form.setSpacing(8)
        dest_row = QHBoxLayout()
        dest_row.setSpacing(6)
        self.withdraw_dest = QLineEdit()
        self.withdraw_dest.setMaxLength(100)
        self.withdraw_dest.setPlaceholderText("Любой TON адрес получателя (UQ / EQ / 0:...)")
        self.withdraw_dest.textChanged.connect(self.invalidate_withdraw_review)
        paste_btn = QPushButton("Paste")
        paste_btn.setProperty("class", "secondary")
        paste_btn.setMinimumHeight(34)
        paste_btn.clicked.connect(lambda: self.withdraw_dest.setText(QApplication.clipboard().text().strip()))
        dest_row.addWidget(self.withdraw_dest, 1)
        dest_row.addWidget(paste_btn)
        w_form.addRow("Адрес получателя", dest_row)

        self.send_all_chk = QCheckBox("Отправить весь остаток (send_all, mode=130)")
        self.send_all_chk.toggled.connect(self._toggle_send_all)
        w_form.addRow(self.send_all_chk)

        self.withdraw_amount = QLineEdit("0.1")
        self.withdraw_amount.setMaxLength(24)
        self.withdraw_amount.setPlaceholderText("TON (например 0.5)")
        self.withdraw_amount.textChanged.connect(self.invalidate_withdraw_review)
        w_form.addRow("Сумма (TON)", self.withdraw_amount)

        self.withdraw_fee_cap = QLineEdit("0.2")
        self.withdraw_fee_cap.setMaxLength(16)
        self.withdraw_fee_cap.textChanged.connect(self.invalidate_withdraw_review)
        w_form.addRow("Потолок комиссии (TON)", self.withdraw_fee_cap)

        self.withdraw_comment = QLineEdit()
        self.withdraw_comment.setMaxLength(120)
        self.withdraw_comment.setPlaceholderText("Необязательно (префикс accmanager: запрещён)")
        self.withdraw_comment.textChanged.connect(self.invalidate_withdraw_review)
        w_form.addRow("Комментарий", self.withdraw_comment)

        self.bounce_chk = QCheckBox("Bounce=True (ОПАСНО для uninit-кошельков; по умолчанию выкл.)")
        self.bounce_chk.toggled.connect(self.invalidate_withdraw_review)
        w_form.addRow(self.bounce_chk)
        w_layout.addLayout(w_form)

        w_btns = QHBoxLayout()
        w_btns.setSpacing(8)
        self.preview_withdraw_btn = QPushButton("Эмулировать и проверить…")
        self.preview_withdraw_btn.setProperty("class", "secondary")
        self.execute_withdraw_btn = QPushButton("Подтвердить и отправить вывод…")
        self.preview_withdraw_btn.clicked.connect(self._preview_withdraw)
        self.execute_withdraw_btn.clicked.connect(self._execute_withdraw)
        for btn in (self.preview_withdraw_btn, self.execute_withdraw_btn):
            btn.setMinimumHeight(38)
            w_btns.addWidget(btn)
        w_layout.addLayout(w_btns)

        self.withdraw_preview_box = QPlainTextEdit()
        self.withdraw_preview_box.setReadOnly(True)
        self.withdraw_preview_box.setMinimumHeight(130)
        self.withdraw_preview_box.setPlainText("Нажмите «Эмулировать и проверить…» для расчёта комиссии, состояния получателя и seqno без подписи.")
        w_layout.addWidget(self.withdraw_preview_box, 1)
        self.tabs.addTab(withdraw_tab, "Вывод / Send-All")

        # Tab 1: Batch Rent Funding ("Грамы на аренду")
        batch_tab = QWidget()
        batch_tab.setObjectName("walletXTabPage")
        b_layout = QVBoxLayout(batch_tab)
        b_layout.setContentsMargins(12, 12, 12, 12)
        b_layout.setSpacing(10)

        scope_row = QHBoxLayout()
        self.batch_scope_label = self.label("Выбранные аккаунты в таблице: 0")
        scope_row.addWidget(self.batch_scope_label, 1)
        self.refresh_selection_btn = QPushButton("Обновить выбор")
        self.refresh_selection_btn.setProperty("class", "secondary")
        self.refresh_selection_btn.setMinimumHeight(32)
        self.refresh_selection_btn.setMinimumWidth(148)
        self.refresh_selection_btn.clicked.connect(self.refresh_selected_scope)
        scope_row.addWidget(self.refresh_selection_btn)
        b_layout.addLayout(scope_row)

        b_form = QFormLayout()
        b_form.setSpacing(8)
        self.batch_mode = QComboBox()
        self.batch_mode.addItem("Фиксированная сумма на каждый аккаунт (fixed)", "fixed")
        self.batch_mode.addItem("Пополнить до целевого баланса (top_up_to)", "top_up_to")
        self.batch_mode.currentIndexChanged.connect(self.invalidate_batch_review)
        b_form.addRow("Режим", self.batch_mode)

        self.batch_amount = QLineEdit("0.15")
        self.batch_amount.setMaxLength(24)
        self.batch_amount.setPlaceholderText("TON на аккаунт / целевой баланс")
        self.batch_amount.textChanged.connect(self.invalidate_batch_review)
        b_form.addRow("Сумма / цель (TON)", self.batch_amount)

        self.batch_fee_cap = QLineEdit("0.05")
        self.batch_fee_cap.setMaxLength(16)
        self.batch_fee_cap.textChanged.connect(self.invalidate_batch_review)
        b_form.addRow("Резерв комиссии/шаг", self.batch_fee_cap)

        self.batch_comment = QLineEdit("rent")
        self.batch_comment.setMaxLength(120)
        self.batch_comment.textChanged.connect(self.invalidate_batch_review)
        b_form.addRow("Комментарий", self.batch_comment)
        b_layout.addLayout(b_form)

        b_btns = QHBoxLayout()
        b_btns.setSpacing(8)
        self.preview_batch_btn = QPushButton("Предпросмотр батча…")
        self.preview_batch_btn.setProperty("class", "secondary")
        self.execute_batch_btn = QPushButton("Отправить батч…")
        self.stop_batch_btn = QPushButton("Stop батч")
        self.stop_batch_btn.setProperty("class", "danger")
        self.preview_batch_btn.clicked.connect(self._preview_batch)
        self.execute_batch_btn.clicked.connect(self._execute_batch)
        self.stop_batch_btn.clicked.connect(lambda: self.request("stop-batch", {}))
        for btn in (self.preview_batch_btn, self.execute_batch_btn, self.stop_batch_btn):
            btn.setMinimumHeight(36)
            b_btns.addWidget(btn)
        b_layout.addLayout(b_btns)

        self.batch_progress = QProgressBar()
        self.batch_progress.setRange(0, 1)
        self.batch_progress.setValue(0)
        b_layout.addWidget(self.batch_progress)

        self.batch_table = QTableWidget(0, 4)
        self.batch_table.verticalHeader().setVisible(False)
        self.batch_table.setHorizontalHeaderLabels(["Аккаунт", "Тек. баланс", "Отправка TON", "Статус / UQ"])
        self.batch_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.batch_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.batch_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.batch_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.batch_table.setEditTriggers(QTableWidget.NoEditTriggers)
        b_layout.addWidget(self.batch_table, 1)
        self.tabs.addTab(batch_tab, "Массовое пополнение (Аренда)")

        # Tab 2: On-chain history & Local Intents
        hist_tab = QWidget()
        hist_tab.setObjectName("walletXTabPage")
        h_layout = QVBoxLayout(hist_tab)
        h_layout.setContentsMargins(12, 12, 12, 12)
        h_layout.setSpacing(8)

        h_layout.addWidget(self.label("История блокчейна (TonCenter / TonAPI):"))
        self.history_table = QTableWidget(0, 5)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setHorizontalHeaderLabels(["Время UTC", "Тип", "Сумма TON", "Комиссия", "Адрес / Хеш"])
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        h_layout.addWidget(self.history_table, 1)

        h_layout.addWidget(self.label("Локальный журнал интентов Wallet X (сверка / освобождение seqno):"))
        intent_row = QHBoxLayout()
        self.intent_combo = QComboBox()
        self.intent_combo.setMinimumContentsLength(28)
        self.intent_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.intent_combo.currentIndexChanged.connect(lambda *_: self._sync_controls())
        intent_row.addWidget(self.intent_combo, 1)
        self.reconcile_btn = QPushButton("Сверить intent")
        self.reconcile_btn.setProperty("class", "secondary")
        self.recover_btn = QPushButton("Освободить зависший…")
        self.recover_btn.setProperty("class", "danger")
        self.recover_btn.setMinimumWidth(174)
        self.reconcile_btn.clicked.connect(self._reconcile_selected)
        self.recover_btn.clicked.connect(self._recover_selected)
        for btn in (self.reconcile_btn, self.recover_btn):
            btn.setMinimumHeight(34)
            intent_row.addWidget(btn)
        h_layout.addLayout(intent_row)

        self.intents_log = QPlainTextEdit()
        self.intents_log.setReadOnly(True)
        self.intents_log.setMaximumHeight(130)
        h_layout.addWidget(self.intents_log)
        self.tabs.addTab(hist_tab, "История и Интенты")

        # Footer status & Close
        footer = QHBoxLayout()
        self.message = self.label("Локальное состояние загружено. Для проверки баланса нажмите «Обновить баланс и историю».")
        footer.addWidget(self.message, 1)
        close_btn = QPushButton("Закрыть")
        close_btn.setProperty("class", "secondary")
        close_btn.setMinimumHeight(36)
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn)
        outer.addLayout(footer)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick_expiry)
        self._timer.start(1000)

        self.refresh_selected_scope()
        self.render_status(self.status_data)

    @staticmethod
    def label(text: str) -> QLabel:
        widget = QLabel(text)
        widget.setTextFormat(Qt.PlainText)
        widget.setWordWrap(True)
        return widget

    def _current_selection(self) -> list[str]:
        raw = self.selected_accounts() or []
        ids: list[str] = []
        for item in raw:
            aid = item.get("id") if isinstance(item, dict) else str(item)
            if aid and aid not in ids:
                ids.append(aid)
        return ids

    def refresh_selected_scope(self) -> None:
        ids = self._current_selection()
        preview = ", ".join(ids[:6]) + (f" (+{len(ids) - 6})" if len(ids) > 6 else "")
        self.batch_scope_label.setText(f"Выбранные аккаунты в таблице: {len(ids)}" + (f" · {preview}" if preview else " (отметьте строки в основной таблице)"))

    def _toggle_send_all(self, checked: bool) -> None:
        self.withdraw_amount.setEnabled(not checked and not self.running)
        if checked:
            self.withdraw_amount.clear()
        elif not self.withdraw_amount.text().strip():
            self.withdraw_amount.setText("0.1")
        self.invalidate_withdraw_review()

    def invalidate_withdraw_review(self, *_args) -> None:
        if self.review is not None and not self.running:
            self.review = None
            self.withdraw_preview_box.setPlainText("Параметры вывода изменены — выполните новую эмуляцию перед отправкой.")
            self._sync_controls()

    def invalidate_batch_review(self, *_args) -> None:
        if self.batch_review is not None and not self.running:
            self.batch_review = None
            self._sync_controls()

    def _tick_expiry(self) -> None:
        if self._confirming_dialog or self.running:
            return
        now = time.time()
        changed = False
        if self.review is not None and now >= float(self.review.get("expires", 0)):
            self.review = None
            self.withdraw_preview_box.setPlainText("Review вывода истёк. Запустите «Эмулировать и проверить…» заново.")
            changed = True
        if self.batch_review is not None and now >= float(self.batch_review.get("expires", 0)):
            self.batch_review = None
            changed = True
        if self.review is not None and not changed:
            rem = max(0, int(float(self.review["expires"]) - now))
            base = str(self.review.get("_summary_text") or "")
            if base:
                self.withdraw_preview_box.setPlainText(f"{base}\nReview истекает через: {rem} сек.")
        if changed:
            self._sync_controls()

    def _dispatch(self, action: str, values: dict) -> None:
        self.busy(True)
        self.request(action, values)

    def busy(self, value: bool, message: str | None = None) -> None:
        self.running = bool(value)
        if message:
            self.message.setText(message)
        self._sync_controls()

    def _sync_controls(self) -> None:
        configured = bool(self.status_data.get("configured"))
        locked = bool(self.status_data.get("locked_in_process"))
        ready = configured and not locked
        idle = not self.running

        self.create_btn.setVisible(not configured)
        self.create_btn.setEnabled(idle and not configured)
        self.import_btn.setEnabled(idle and not locked)
        self.unlock_btn.setVisible(configured and locked)
        self.unlock_btn.setEnabled(idle and configured and locked)
        self.lock_btn.setVisible(configured and not locked)
        self.lock_btn.setEnabled(idle and ready)
        self.export_seed_btn.setEnabled(idle and ready and bool(self.status_data.get("has_mnemonic")))
        self.delete_btn.setEnabled(idle and configured)

        self.refresh_chain_btn.setEnabled(idle and configured)
        self.copy_uq_btn.setEnabled(configured)
        self.copy_eq_btn.setEnabled(configured)

        for widget in (self.withdraw_dest, self.send_all_chk, self.withdraw_fee_cap, self.withdraw_comment, self.bounce_chk):
            widget.setEnabled(idle and ready)
        self.withdraw_amount.setEnabled(idle and ready and not self.send_all_chk.isChecked())
        self.preview_withdraw_btn.setEnabled(idle and ready)
        self.execute_withdraw_btn.setEnabled(idle and ready and bool(self.review))

        for widget in (self.batch_mode, self.batch_amount, self.batch_fee_cap, self.batch_comment, self.refresh_selection_btn):
            widget.setEnabled(idle)
        self.preview_batch_btn.setEnabled(idle and ready)
        self.execute_batch_btn.setEnabled(idle and ready and bool(self.batch_review))
        self.stop_batch_btn.setEnabled(self.running)

        cur_intent = self.intent_combo.currentData()
        unresolved = bool(isinstance(cur_intent, dict) and cur_intent.get("state") in {"uncertain", "submitted_unverified"})
        self.intent_combo.setEnabled(idle and self.intent_combo.count() > 0)
        self.reconcile_btn.setEnabled(idle and cur_intent is not None)
        stale_recover = bool(isinstance(cur_intent, dict) and cur_intent.get("seqno") is not None and time.time() >= float(cur_intent.get("expires") or 0) + 60 and cur_intent.get("state") in {"uncertain", "submitted_unverified"})
        self.recover_btn.setEnabled(idle and (stale_recover or unresolved))

    def render_status(self, status: dict, *, intents: list[dict] | None = None, created_mnemonic: str | None = None) -> None:
        self.status_data = dict(status or {})
        configured = bool(self.status_data.get("configured"))
        locked = bool(self.status_data.get("locked_in_process"))
        wallet = self.status_data.get("wallet") or {}
        if configured and wallet:
            lock_txt = "Заблокирован в памяти" if locked else "Разблокирован (готов к подписи)"
            self.vault_status_label.setText(
                f"Vault: {lock_txt} · Защита: {self.status_data.get('protection', 'dpapi')} · Сеть: {wallet.get('network', 'mainnet')} · Ревизия: {str(wallet.get('revision', ''))[:8]}"
            )
            self.address_label.setText(
                f"UQ (для пополнения): {wallet.get('non_bounceable_address', '?')}\n"
                f"EQ (bounceable): {wallet.get('friendly_address', '?')}\n"
                f"Raw: {wallet.get('address', '?')}"
            )
        elif self.status_data.get("exists") and locked:
            self.vault_status_label.setText("Vault существует, но заблокирован в этом процессе. Нажмите «Разблокировать».")
            self.address_label.setText("Адрес скрыт до разблокировки.")
        else:
            self.vault_status_label.setText("Wallet X ещё не создан. Нажмите «Создать Wallet X» или импортируйте существующую фразу.")
            self.address_label.setText("Кошелёк не настроен.")

        if intents is not None:
            self.render_intents(intents)
        self.busy(False)
        if created_mnemonic:
            self._show_mnemonic_modal("Новая seed-фраза Wallet X (сохраните в безопасном месте)", created_mnemonic)

    def render_chain(self, payload: dict) -> None:
        self.chain_data = dict(payload or {})
        self.chain_status_label.setText(
            f"Баланс: {payload.get('balance_native', '0')} TON · Состояние: {payload.get('chain_state', 'unknown')} "
            f"· Seqno: {payload.get('seqno', '?')} · Провайдер: {payload.get('provider', '?')} · Проверено: {_format_utc(payload.get('checked_at'))}"
            + (" · Требуется deploy при 1-м исходе" if payload.get("deploy_needed") else "")
        )
        history = payload.get("history") or []
        src = str(payload.get("address") or "")
        self.history_table.setRowCount(len(history))
        for row_idx, tx in enumerate(history):
            outgoing = tx.get("outgoing") or []
            incoming = tx.get("incoming") or {}
            if outgoing:
                kind = f"OUT ({len(outgoing)})"
                total_out = sum(int(o.get("amount_nano") or 0) for o in outgoing)
                amt = _safe_nano_ton(total_out)
                dest = outgoing[0].get("destination") or "?"
                try:
                    dest = friendly_address(dest, self.chain_data.get("network", "mainnet"), bounceable=False)[:18] + "…"
                except Exception:
                    dest = str(dest)[:20]
                target_info = f"→ {dest} · tx {str(tx.get('hash', ''))[:10]}"
            else:
                kind = "IN"
                amt = _safe_nano_ton(incoming.get("amount_nano") or "0")
                sender = incoming.get("source") or "external/mint"
                if sender not in {"external/mint", src}:
                    try:
                        sender = friendly_address(sender, self.chain_data.get("network", "mainnet"), bounceable=False)[:18] + "…"
                    except Exception:
                        sender = str(sender)[:20]
                target_info = f"← {sender} · tx {str(tx.get('hash', ''))[:10]}"
            fee = _safe_nano_ton(tx.get("fee_nano") or "0")
            for col_idx, val in enumerate((_format_utc(tx.get("utime")), kind, amt, fee, target_info)):
                self.history_table.setItem(row_idx, col_idx, QTableWidgetItem(val))
        self.busy(False, f"Баланс обновлён: {payload.get('balance_native', '0')} TON ({len(history)} транз.)")

    def render_intents(self, rows: list[dict]) -> None:
        self.intents_data = list(rows or [])
        self.intent_combo.blockSignals(True)
        self.intent_combo.clear()
        lines = []
        for r in self.intents_data:
            amt = "ALL (mode 130)" if r.get("send_all") else f"{_safe_nano_ton(r.get('total_nano') or 0)} TON"
            label = f"{r['kind']} · seqno={r.get('seqno')} · {r['state']} · {amt} · {str(r['id'])[:8]}"
            self.intent_combo.addItem(label, r)
            err = f" · err={r['error']}" if r.get("error") else ""
            lines.append(f"{_format_utc(r.get('created'))} | {label}{err}")
        self.intent_combo.blockSignals(False)
        self.intents_log.setPlainText("\n".join(lines) or "Локальных интентов Wallet X пока нет.")
        self._sync_controls()

    def render_withdraw_preview(self, preview: dict) -> None:
        self.review = dict(preview or {})
        amt_label = "ВЕСЬ ОСТАТОК (mode 130)" if self.review.get("send_all") else f"{_safe_nano_ton(self.review.get('amount_nano'))} TON"
        lines = [
            f"Получатель (UQ): {self.review.get('destination_uq')}",
            f"Сумма: {amt_label} · Состояние получателя: {self.review.get('recipient_state')}",
            f"Баланс Wallet X: {_safe_nano_ton(self.review.get('balance_nano'))} TON · Seqno: {self.review.get('seqno')} · Deploy: {self.review.get('deploy')}",
            f"Оценка комиссии (эмуляция): ~{_safe_nano_ton(self.review.get('estimated_fee_nano'))} TON · Bounce: {self.review.get('bounce')}",
        ]
        if self.review.get("warning"):
            lines.append(f"ПРЕДУПРЕЖДЕНИЕ: {self.review['warning']}")
        lines.append(f"Фраза подтверждения: {self.review.get('expected_confirmation')}")
        summary = "\n".join(lines)
        self.review["_summary_text"] = summary
        rem = max(0, int(float(self.review.get("expires", 0)) - time.time()))
        self.withdraw_preview_box.setPlainText(f"{summary}\nReview истекает через: {rem} сек.")
        self.busy(False, "Эмуляция выполнена. Проверьте детали и подтвердите вывод.")

    def render_batch_preview(self, preview: dict) -> None:
        self.batch_review = dict(preview or {})
        items = self.batch_review.get("items") or []
        skipped = self.batch_review.get("skipped") or []
        self.batch_table.setRowCount(len(items) + len(skipped))
        row_idx = 0
        for item in items:
            cur = _safe_nano_ton(item["current_balance_nano"]) if item.get("current_balance_nano") is not None else "—"
            send_amt = _safe_nano_ton(item["amount_nano"])
            info = f"готово · {item.get('destination_uq', '')}"
            for col_idx, val in enumerate((item["account_id"], cur, send_amt, info)):
                self.batch_table.setItem(row_idx, col_idx, QTableWidgetItem(str(val)))
            row_idx += 1
        for skip in skipped:
            bal = _safe_nano_ton(skip["balance_nano"]) if skip.get("balance_nano") is not None else "—"
            for col_idx, val in enumerate((skip["account_id"], bal, "0", f"пропущен: {skip.get('reason')}")):
                self.batch_table.setItem(row_idx, col_idx, QTableWidgetItem(str(val)))
            row_idx += 1
        self.batch_progress.setRange(0, max(1, len(items)))
        self.batch_progress.setValue(0)
        self.busy(
            False,
            f"Готово к отправке: {len(items)} акк. · Итого {self.batch_review.get('total_ton')} TON · "
            f"Резерв комиссий {_safe_nano_ton(self.batch_review.get('estimated_fee_budget_nano'))} TON (пропущено: {len(skipped)}).",
        )

    def render_batch_progress(self, event: dict) -> None:
        total = max(1, int(event.get("total") or self.batch_progress.maximum() or 1))
        idx = int(event.get("index") or 0)
        phase = str(event.get("phase") or "")
        done = idx + (1 if phase == "item_done" else 0)
        self.batch_progress.setRange(0, total)
        self.batch_progress.setValue(min(done, total))
        self.message.setText(f"Батч [{done}/{total}] · {phase} · аккаунт {event.get('account_id', '')}")

    def render_batch_done(self, result: dict) -> None:
        self.batch_review = None
        self.batch_progress.setValue(self.batch_progress.maximum())
        self.busy(
            False,
            f"Батч завершён ({result.get('state')}): отправлено {result.get('sent', 0)}/{result.get('total', 0)}, "
            f"подтверждено {result.get('confirmed', 0)}, ошибок {result.get('failed', 0)}.",
        )

    def _copy_address(self, field: str, label: str) -> None:
        wallet = self.status_data.get("wallet") or {}
        addr = wallet.get(field)
        if addr:
            QApplication.clipboard().setText(str(addr))
            self.message.setText(f"Адрес {label} скопирован: {addr}")

    def _create_wallet(self) -> None:
        if QMessageBox.question(
            self,
            "Создать Master Wallet X",
            "Создать новый изолированный кошелёк Wallet X (24 слова) и сохранить ключ в защищённом DPAPI Vault на этом компьютере?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self._dispatch("create-wallet", {"confirmed": True, "network": "mainnet"})

    def _import_wallet(self) -> None:
        replacing = bool(self.status_data.get("configured"))
        if replacing and QMessageBox.warning(
            self,
            "Заменить существующий Wallet X?",
            "Внимание: существующий Wallet X будет заменён новым! Убедитесь, что старый seed сохранён.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self._confirming_dialog = True
        try:
            phrase, ok = QInputDialog.getText(
                self,
                "Импорт Wallet X",
                "Введите 12/24 слова мнемоники (TEP-0003) ИЛИ 64-символьный hex Seed32:",
                QLineEdit.Password,
            )
        finally:
            self._confirming_dialog = False
        text = (phrase or "").strip()
        if not ok or not text:
            return
        buf = bytearray(text.encode("utf-8"))
        text = ""
        try:
            self._dispatch("import-wallet", {"phrase_buf": buf, "confirmed": True, "replace_existing": replacing})
        finally:
            buf[:] = bytes(len(buf))

    def _export_mnemonic(self) -> None:
        self._confirming_dialog = True
        try:
            phrase, ok = QInputDialog.getText(
                self,
                "Экспорт Seed-фразы Wallet X",
                "Введите точно: SHOW WALLET X SEED",
            )
        finally:
            self._confirming_dialog = False
        if not ok or (phrase or "").strip() != "SHOW WALLET X SEED":
            self.message.setText("Экспорт seed отменён.")
            return
        self._dispatch("export-mnemonic", {"confirmed": True})

    def show_exported_mnemonic(self, phrase: str) -> None:
        self.busy(False, "Seed-фраза прочитана локально. Не передавайте её никому.")
        self._show_mnemonic_modal("Seed-фраза Wallet X (секретно!)", phrase)

    def _show_mnemonic_modal(self, title: str, phrase: str) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(560, 260)
        dlg.setStyleSheet(self.styleSheet())
        v = QVBoxLayout(dlg)
        v.addWidget(self.label("Сохраните эти слова в надёжном офлайн-месте. Потеря фразы = потеря доступа к средствам:"))
        box = QPlainTextEdit(phrase)
        box.setReadOnly(True)
        v.addWidget(box, 1)
        row = QHBoxLayout()
        copy_btn = QPushButton("Копировать фразу")
        copy_btn.setProperty("class", "secondary")
        copy_btn.clicked.connect(lambda: (QApplication.clipboard().setText(phrase), self.message.setText("Seed скопирован в буфер (очистите буфер после сохранения!).")))
        ok_btn = QPushButton("Готово, скрыть")
        ok_btn.clicked.connect(dlg.accept)
        row.addWidget(copy_btn)
        row.addWidget(ok_btn)
        v.addLayout(row)
        self._confirming_dialog = True
        try:
            dlg.exec()
        finally:
            self._confirming_dialog = False

    def _delete_wallet(self) -> None:
        rev = str((self.status_data.get("wallet") or {}).get("revision") or "")[:8]
        expected = f"DELETE WALLET X {rev}"
        self._confirming_dialog = True
        try:
            phrase, ok = QInputDialog.getText(
                self,
                "Удалить Wallet X навсегда",
                f"УБЕДИТЕСЬ, ЧТО SEED СОХРАНЁН И БАЛАНС ПУСТ!\nВведите точно для удаления: {expected}",
            )
        finally:
            self._confirming_dialog = False
        if not ok or (phrase or "").strip() != expected:
            self.message.setText("Удаление Wallet X отменено.")
            return
        self._dispatch("delete-wallet", {"confirmation": expected, "confirmed": True})

    def _preview_withdraw(self) -> None:
        dest = self.withdraw_dest.text().strip()
        if not dest:
            self.message.setText("Укажите адрес получателя (UQ / EQ / 0:...).")
            return
        comment = self.withdraw_comment.text().strip()
        if comment.startswith("accmanager:"):
            self.message.setText("Префикс комментария accmanager: зарезервирован для внутренних шагов карусели.")
            return
        send_all = self.send_all_chk.isChecked()
        payload = {
            "destination": dest,
            "send_all": send_all,
            "amount_ton": None if send_all else self.withdraw_amount.text().strip(),
            "fee_cap_ton": self.withdraw_fee_cap.text().strip() or "0.2",
            "comment": comment,
            "bounce": self.bounce_chk.isChecked(),
            "confirmed": True,
        }
        self.review = None
        self._dispatch("preview-withdraw", payload)

    def _execute_withdraw(self) -> None:
        reviewed = self.review
        if not reviewed:
            return
        expected = reviewed["expected_confirmation"]
        self._confirming_dialog = True
        try:
            phrase, ok = QInputDialog.getText(
                self,
                "Подтвердить вывод TON из Wallet X",
                f"Получатель: {reviewed.get('destination_uq')}\n"
                f"Сумма: {'ВЕСЬ ОСТАТОК' if reviewed.get('send_all') else _safe_nano_ton(reviewed.get('amount_nano')) + ' TON'}\n"
                f"Оценка комиссии: ~{_safe_nano_ton(reviewed.get('estimated_fee_nano'))} TON\n"
                + (f"\n⚠ {reviewed['warning']}\n" if reviewed.get("warning") else "")
                + f"\nДля подтверждения введите точно:\n{expected}",
            )
        finally:
            self._confirming_dialog = False
        if not ok or (phrase or "").strip() != expected:
            self.message.setText("Неверная фраза подтверждения — перевод отменён.")
            return
        if self.review is None or self.review.get("review_token") != reviewed["review_token"] or time.time() >= float(self.review.get("expires", 0)):
            self._tick_expiry()
            return
        token = reviewed["review_token"]
        self.review = None
        self._dispatch("execute-withdraw", {"review_token": token, "confirmation": expected})

    def _preview_batch(self) -> None:
        self.refresh_selected_scope()
        ids = self._current_selection()
        if not ids:
            self.message.setText("Выделите от 1 до 200 аккаунтов в основной таблице слева/сверху.")
            return
        mode = self.batch_mode.currentData()
        amt = self.batch_amount.text().strip()
        if not amt:
            self.message.setText("Укажите сумму пополнения в TON.")
            return
        comment = self.batch_comment.text().strip()
        if comment.startswith("accmanager:"):
            self.message.setText("Префикс комментария accmanager: запрещён.")
            return
        payload = {
            "account_ids": ids,
            "mode": mode,
            "amount_ton": amt if mode == "fixed" else None,
            "target_balance_ton": amt if mode == "top_up_to" else None,
            "fee_cap_ton": self.batch_fee_cap.text().strip() or "0.05",
            "comment": comment,
            "confirmed": True,
        }
        self.batch_review = None
        self._dispatch("preview-batch", payload)

    def _execute_batch(self) -> None:
        reviewed = self.batch_review
        if not reviewed:
            return
        expected = reviewed["expected_confirmation"]
        self._confirming_dialog = True
        try:
            phrase, ok = QInputDialog.getText(
                self,
                "Подтвердить массовое пополнение",
                f"Будет отправлено {len(reviewed.get('items', []))} переводов на сумму {reviewed.get('total_ton')} TON.\n"
                f"Для запуска введите точно:\n{expected}",
                text=expected,
            )
        finally:
            self._confirming_dialog = False
        if not ok or (phrase or "").strip() != expected:
            self.message.setText("Массовое пополнение отменено.")
            return
        if self.batch_review is None or self.batch_review.get("review_token") != reviewed["review_token"] or time.time() >= float(self.batch_review.get("expires", 0)):
            self._tick_expiry()
            return
        token = reviewed["review_token"]
        self.batch_review = None
        self._dispatch("execute-batch", {"review_token": token, "confirmation": expected})

    def _reconcile_selected(self) -> None:
        row = self.intent_combo.currentData()
        if isinstance(row, dict):
            self._dispatch("reconcile", {"intent_id": row["id"]})

    def _recover_selected(self) -> None:
        row = self.intent_combo.currentData()
        if not isinstance(row, dict):
            return
        expected = f"RECOVER {row['id']}"
        self._confirming_dialog = True
        try:
            phrase, ok = QInputDialog.getText(
                self,
                "Освободить зависший intent Wallet X",
                f"Будет проверен блокчейн по seqno={row.get('seqno')}. Введите точно:\n{expected}",
                text=expected,
            )
        finally:
            self._confirming_dialog = False
        if not ok or (phrase or "").strip() != expected:
            return
        self._dispatch("recover", {"intent_id": row["id"], "confirmation": expected})

    def fail(self, error: str) -> None:
        self.busy(False, str(error))

    def _confirm_stop_on_close(self) -> bool:
        if not self.running or self._closing_confirmed:
            return True
        if (
            QMessageBox.question(
                self,
                "Операция выполняется",
                "Выполняется операция Wallet X. Остановить батч и закрыть окно?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return False
        self._closing_confirmed = True
        self.request("stop-batch", {})
        return True

    def done(self, result):
        if not self._confirm_stop_on_close():
            return
        if callable(self.on_close_lock):
            try:
                self.on_close_lock()
            except Exception:
                pass
        super().done(result)

    def closeEvent(self, event):
        if not self._confirm_stop_on_close():
            event.ignore()
            return
        if callable(self.on_close_lock):
            try:
                self.on_close_lock()
            except Exception:
                pass
        super().closeEvent(event)
