"""Metadata-only diagnostics/manual queue. No answer entry or challenge URLs."""
from datetime import datetime, timezone
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QComboBox, QDialog, QLabel, QMessageBox, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget
from core.design_tokens import TOKENS


class CaptchaSupportDialog(QDialog):
    def __init__(self, report, request, parent=None):
        super().__init__(parent)
        self.request = request
        self.setWindowTitle("CAPTCHA support · local diagnostics")
        self.setFont(QFont("Segoe UI", 10))
        self.resize(720, 640)
        palette = self.palette()
        for role, color in ((QPalette.Window, TOKENS.panel), (QPalette.WindowText, TOKENS.text),
                            (QPalette.Base, TOKENS.background), (QPalette.Text, TOKENS.text),
                            (QPalette.Button, TOKENS.panel_secondary), (QPalette.ButtonText, TOKENS.text)):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.setStyleSheet(
            f"QDialog {{ background: {TOKENS.panel}; color: {TOKENS.text}; }}"
            f"QTabWidget::pane {{ background: {TOKENS.panel}; border: 1px solid {TOKENS.border}; border-radius: 8px; }}"
            f"QWidget#captchaTabPage {{ background: {TOKENS.panel}; color: {TOKENS.text}; }}"
            f"QTabBar::tab {{ background: {TOKENS.panel_secondary}; color: {TOKENS.muted}; border: 1px solid {TOKENS.border}; border-bottom: 0; padding: 7px 14px; border-top-left-radius: 7px; border-top-right-radius: 7px; margin-right: 4px; }}"
            f"QTabBar::tab:selected {{ background: #1d4f8c; color: #ffffff; border-color: #4b8fd8; }}"
            f"QLabel {{ color: {TOKENS.text}; }}"
            f"QComboBox {{ background: {TOKENS.background}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 8px; padding: 8px 10px; }}"
            f"QComboBox QAbstractItemView {{ background: {TOKENS.panel_secondary}; color: {TOKENS.text}; selection-background-color: #1d4f8c; }}"
            f"QPushButton {{ background: {TOKENS.primary}; color: #f8fbff; border: 0; border-radius: 8px; padding: 8px 12px; }}"
            f"QPushButton:hover {{ background: #3a92e8; }}"
            f"QPushButton:disabled {{ background: #1c222b; color: #64748b; border: 1px solid {TOKENS.border}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(self.label("CAPTCHA support · no solving, installs or paid requests"))
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        diagnostics = QWidget()
        diagnostics.setObjectName("captchaTabPage")
        local = QVBoxLayout(diagnostics)
        local.setContentsMargins(16, 16, 16, 16)
        self.dependencies = self.label("")
        local.addWidget(self.dependencies)
        local.addWidget(self.label("Present does not mean usable. Models were not loaded; Chromium installation and OCR inference were not checked. No keys were inspected, binaries executed or network requests made."))
        local.addStretch()
        tabs.addTab(diagnostics, "Dependencies")
        queue = QWidget()
        queue.setObjectName("captchaTabPage")
        manual = QVBoxLayout(queue)
        manual.setContentsMargins(16, 16, 16, 16)
        manual.setSpacing(16)
        self.scope = self.label("")
        manual.addWidget(self.scope)
        manual.addWidget(self.label("No image, answer, token or third-party URL is stored here. Use the dashboard's Open acc and review in the official interface. Queue expiry is local, not a promise that the provider challenge is still valid."))
        self.items = QComboBox()
        self.items.setMinimumContentsLength(25)
        self.items.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        manual.addWidget(self.items)
        self.detail = self.label("")
        manual.addWidget(self.detail)
        self.ack = QPushButton("Mark selected item reviewed locally…")
        self.ack.setMinimumHeight(36)
        self.ack.clicked.connect(self.acknowledge)
        manual.addWidget(self.ack)
        manual.addStretch()
        tabs.addTab(queue, "Manual queue")
        self.message = self.label("Local review only. This does not submit an answer, resume a task or confirm giveaway participation.")
        layout.addWidget(self.message)
        self.reload = QPushButton("Refresh local diagnostics / queue")
        self.reload.setMinimumHeight(36)
        self.reload.clicked.connect(lambda: self.send("report", {}))
        layout.addWidget(self.reload)
        self.items.currentIndexChanged.connect(self.select)
        self.render(report)

    @staticmethod
    def label(text):
        widget = QLabel(text)
        widget.setTextFormat(Qt.PlainText)
        widget.setWordWrap(True)
        return widget

    def render(self, report):
        self.report = report
        lines = []
        for package in report["diagnostics"]["packages"]:
            lines.append(f"{package['name']}: {package['status']}"+(f" · {package['version']}" if package.get("version") else "")+
                         (f" · model files: {package['model_files']} (not loaded)" if package.get("model_files") is not None else ""))
        lines.extend(f"{binary['name']}: {binary['status']}" for binary in report["diagnostics"]["binaries"])
        self.dependencies.setText("\n".join(lines))
        self.scope.setText("Account: "+report["account_id"] if report["account_id"] else "Diagnostics only. Select exactly one enabled account in the current group/filters to inspect its queue.")
        self.items.blockSignals(True)
        self.items.clear()
        for item in report["items"]:
            self.items.addItem(f"{item['state']} · {item['provider']} · {item['reason']} · {item['id'][:8]}", item)
        self.items.blockSignals(False)
        self.busy(False)
        self.select()
        self.message.setText("Local review only. No answer submission, task resume or participation success is inferred.")

    @staticmethod
    def _format_time(value):
        if isinstance(value, (int, float)) and value > 0:
            return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return str(value or "unknown")

    def select(self):
        item = self.items.currentData()
        self.ack.setEnabled(bool(item and item["state"] == "pending" and self.report["account_id"]))
        self.detail.setText(f"Kind: {item['kind']}\nCreated: {self._format_time(item['created'])}\nLocal queue expires: {self._format_time(item['expires'])}\nProvider validity is unknown. Refresh before acknowledging an old item." if item else "No manual items in this explicit scope.")

    def busy(self, value):
        self.items.setEnabled(not value)
        self.reload.setEnabled(not value)
        if value:
            self.ack.setEnabled(False)
        else:
            self.select()

    def send(self, action, payload):
        self.busy(True)
        self.request(action, payload)

    def acknowledge(self):
        item = self.items.currentData()
        if not item or item["state"] != "pending" or not self.ack.isEnabled():
            return
        prompt = QMessageBox(QMessageBox.Question, "Confirm local review", "Mark this selected metadata entry reviewed locally?\nNo answer is sent; no task is resumed; this is not proof of successful participation.", QMessageBox.Yes | QMessageBox.No, self)
        prompt.setTextFormat(Qt.PlainText)
        prompt.setDefaultButton(QMessageBox.No)
        if prompt.exec() != QMessageBox.Yes:
            return
        self.send("ack", {"id": item["id"], "revision": item["revision"], "expected_context": self.report["account_context"], "confirmed": True})
