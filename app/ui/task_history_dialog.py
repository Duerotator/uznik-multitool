"""Metadata history; explicit resume of supported read-only jobs only."""
from datetime import datetime, timezone
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPalette, QColor
from core.design_tokens import TOKENS
from PySide6.QtWidgets import QComboBox, QDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout


class TaskHistoryDialog(QDialog):
    def __init__(self, request, selection, parent=None):
        super().__init__(parent)
        self.request, self.selection = request, selection
        self.setFont(QFont("Segoe UI", 10))
        palette = self.palette()
        for role, color in ((QPalette.Window,TOKENS.panel), (QPalette.WindowText,TOKENS.text),
                (QPalette.Base,TOKENS.background), (QPalette.Text,TOKENS.text),
                (QPalette.Button,TOKENS.panel_secondary), (QPalette.ButtonText,TOKENS.text)):
            palette.setColor(role,QColor(color))
        self.setPalette(palette)
        self.setStyleSheet(
            f"QDialog {{ background: {TOKENS.panel}; color: {TOKENS.text}; }}"
            f"QLabel {{ color: {TOKENS.text}; }}"
            f"QComboBox {{ background: {TOKENS.background}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 8px; padding: 8px 10px; }}"
            f"QComboBox QAbstractItemView {{ background: {TOKENS.panel_secondary}; color: {TOKENS.text}; selection-background-color: #1d4f8c; }}"
            f"QPushButton {{ background: {TOKENS.primary}; color: #f8fbff; border: 0; border-radius: 8px; padding: 8px 12px; }}"
            f"QPushButton:hover {{ background: #3a92e8; }}"
            f"QPushButton:disabled {{ background: #1c222b; color: #64748b; border: 1px solid {TOKENS.border}; }}"
        )
        self.setWindowTitle("Task history / safe resume")
        self.resize(700, 460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.items = QComboBox()
        self.items.setMinimumContentsLength(30)
        self.items.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        layout.addWidget(self.items)
        self.detail = QLabel()
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.message = QLabel("History contains metadata only. No automatic replay after restart.")
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.resume = QPushButton("Resume unfinished read-only Guard…")
        self.reload = QPushButton("Refresh history")
        self.resume.setMinimumHeight(36)
        self.reload.setMinimumHeight(36)
        layout.addWidget(self.resume)
        layout.addWidget(self.reload)
        layout.addStretch()
        self.items.currentIndexChanged.connect(self.choose)
        self.resume.clicked.connect(self.resume_selected)
        self.reload.clicked.connect(lambda: self.send("list", {}))
        self.choose()

    def render(self, rows):
        self.items.blockSignals(True)
        self.items.clear()
        for row in rows:
            self.items.addItem(f"{row['operation']} · {row['status']} · {row['id'][:8]}", row)
        self.items.blockSignals(False)
        self.busy(False)
        self.choose()

    @staticmethod
    def _format_time(value):
        if isinstance(value, (int, float)) and value > 0:
            return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return str(value or "unknown")

    def choose(self):
        row = self.items.currentData()
        self.resume.setEnabled(bool(row and row["resumable"]))
        self.detail.setText((f"Source: {row['source']}\nStatus: {row['status']}\nProgress: {row['completed']}/{row['total']} · errors {row['errors']}\n"
            f"Error kind: {row.get('error_kind') or 'none recorded'}\nUpdated: {self._format_time(row['updated'])}\n{row['resume_reason']}\nCompletion is not independent verification of external state.") if row else "No recorded tasks. Legacy tasks.json is not imported or replayed.")

    def busy(self, value):
        self.items.setEnabled(not value)
        self.reload.setEnabled(not value)
        if value:
            self.resume.setEnabled(False)
        else:
            self.choose()

    def send(self, action, values):
        self.busy(True)
        self.request(action, values)

    def resume_selected(self):
        row = self.items.currentData()
        if not row or not row["resumable"] or not self.resume.isEnabled():
            return
        ids = self.selection()
        prompt = QMessageBox(QMessageBox.Question, "Confirm read-only resume", "Resume only unfinished Guard observations for these exact selected IDs?\n"+", ".join(ids)+"\nNo revocation, login codes or profile publishing. Group/filter/account changes will block resume.", QMessageBox.Yes | QMessageBox.No, self)
        prompt.setTextFormat(Qt.PlainText)
        prompt.setDefaultButton(QMessageBox.No)
        if prompt.exec() == QMessageBox.Yes:
            self.send("resume", {"id": row["id"], "revision": row["revision"], "account_ids": ids, "confirmed": True})
