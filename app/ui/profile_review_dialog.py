"""Native single-account story matching dialog; all IO stays in the worker."""
import base64
import binascii

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette, QPixmap
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout
from core.design_tokens import TOKENS


class ProfileReviewDialog(QDialog):
    def __init__(self, snapshot, request, parent=None):
        super().__init__(parent)
        self.snapshot, self.request = snapshot, request
        self.token = snapshot["token"]
        self.choices = {row["hash"]: row["story_id"] for row in snapshot["local"] if row["status"] == "done"}
        self.setWindowTitle("Review story publications")
        self.setFont(QFont("Segoe UI", 10))
        palette = self.palette()
        for role, color in ((QPalette.Window, TOKENS.panel), (QPalette.WindowText, TOKENS.text),
                            (QPalette.Base, TOKENS.background), (QPalette.Text, TOKENS.text),
                            (QPalette.Button, TOKENS.panel_secondary), (QPalette.ButtonText, TOKENS.text)):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.setStyleSheet(
            f"QDialog {{ background: {TOKENS.panel}; color: {TOKENS.text}; }}"
            f"QLabel {{ color: {TOKENS.text}; }}"
            f"QCheckBox {{ color: {TOKENS.text}; spacing: 8px; }}"
            f"QCheckBox:disabled {{ color: {TOKENS.muted}; }}"
            f"QComboBox {{ background: {TOKENS.background}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 8px; padding: 8px 10px; }}"
            f"QComboBox QAbstractItemView {{ background: {TOKENS.panel_secondary}; color: {TOKENS.text}; selection-background-color: #1d4f8c; }}"
            f"QPushButton {{ background: {TOKENS.primary}; color: #f8fbff; border: 0; border-radius: 8px; padding: 8px 14px; }}"
            f"QPushButton:hover {{ background: #3a92e8; }}"
            f"QPushButton:disabled {{ background: #1c222b; color: #64748b; border: 1px solid {TOKENS.border}; }}"
        )
        self.resize(820, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        title = QLabel(f"Account: {snapshot['account_id']} • Telegram ID: {snapshot['telegram_user_id']}")
        title.setWordWrap(True)
        title.setTextFormat(Qt.PlainText)
        layout.addWidget(title)
        note = QLabel("Compare each archived file with your Telegram publication. No automatic matching.\n"
                      "Not published explicitly permits a later upload. This dialog itself never publishes.\n"
                      "Video previews are one frame; inspect the full story in Telegram if unsure.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.local = QComboBox()
        for row in snapshot["local"]:
            self.local.addItem(f"{row['index']}. {'Video' if row['is_video'] else 'Photo'} • "
                               f"{row['hash'][:12]} • {row['status']}", row["hash"])
        self.remote = QComboBox()
        self.remote.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.remote.setMinimumContentsLength(30)
        self.remote.addItem("Choose a publication…", -1)
        self.remote.addItem("Not published — permit one later upload", 0)
        self.remote.model().item(1).setEnabled(snapshot["complete"])
        for row in snapshot["remote"]:
            visibility = "public / profile" if row["pinned"] and row["public"] else "fix visibility in Telegram"
            self.remote.addItem(f"ID {row['id']} • {row['kind']} • {visibility} • {row['caption'][:70]}", row["id"])
        layout.addWidget(QLabel("Archived file"))
        layout.addWidget(self.local)
        layout.addWidget(QLabel("Your Telegram publication"))
        layout.addWidget(self.remote)
        images = QHBoxLayout()
        self.local_image, self.remote_image = QLabel(), QLabel()
        for label in (self.local_image, self.remote_image):
            label.setMinimumSize(240, 240)
            label.setAlignment(Qt.AlignCenter)
            label.setWordWrap(True)
            images.addWidget(label)
        layout.addLayout(images, 1)
        self.message = QLabel("")
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.confirmed = QCheckBox("I checked matches/absence; all profile tasks (including other copies) are stopped.")
        self.confirmed.setChecked(False)
        layout.addWidget(self.confirmed)
        buttons = QHBoxLayout()
        self.preview_button = QPushButton("Load comparison previews")
        self.preview_button.setMinimumWidth(210)
        self.save_button = QPushButton("Save local receipts")
        self.save_button.setProperty("class", "primary")
        close = QPushButton("Close")
        buttons.addWidget(self.preview_button)
        buttons.addStretch()
        buttons.addWidget(close)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.local.currentIndexChanged.connect(self.select_local)
        self.remote.currentIndexChanged.connect(self.select_remote)
        self.confirmed.toggled.connect(self.update_save)
        self.preview_button.clicked.connect(self.load_previews)
        self.save_button.clicked.connect(self.save)
        close.clicked.connect(self.close)
        for button in (self.preview_button, close):
            button.setProperty("class", "secondary")
        self.select_local()

    def select_local(self):
        digest = self.local.currentData()
        self.remote.blockSignals(True)
        index = self.remote.findData(self.choices.get(digest, -1))
        self.remote.setCurrentIndex(max(0, index))
        self.remote.blockSignals(False)
        row = next((r for r in self.snapshot.get("local", []) if r["hash"] == digest), None)
        locked = bool(row and row.get("status") == "done")
        self.remote.setEnabled(bool(row) and not locked)
        self.preview_button.setEnabled(bool(row))
        self.local_image.clear()
        self.remote_image.clear()
        self.local_image.setText("Archived preview" if row else "No archived media")
        self.remote_image.setText("Telegram preview")
        self.confirmed.setChecked(False)
        self.update_save()

    def select_remote(self):
        digest, value = self.local.currentData(), self.remote.currentData()
        if digest is not None and value is not None and value >= 0:
            self.choices[digest] = value
        elif digest is not None:
            self.choices.pop(digest, None)
        self.remote_image.clear()
        self.remote_image.setText("Load previews to inspect this choice")
        self.confirmed.setChecked(False)
        self.update_save()

    def load_previews(self):
        digest = self.local.currentData()
        if not digest:
            return
        self.request("preview", {"local_hash": digest, "story_id": max(0, self.remote.currentData() or 0)})

    def set_busy(self, busy):
        self.local.setEnabled(not busy)
        self.preview_button.setEnabled(not busy and bool(self.local.currentData()))
        self.confirmed.setEnabled(not busy)
        row = next((r for r in self.snapshot.get("local", []) if r["hash"] == self.local.currentData()), None)
        locked = bool(row and row.get("status") == "done")
        self.remote.setEnabled(not busy and bool(row) and not locked)
        self.save_button.setEnabled(not busy and self.confirmed.isChecked() and len(self.choices) == len(self.snapshot["local"]))

    def update_save(self):
        self.save_button.setEnabled(self.confirmed.isChecked() and len(self.choices) == len(self.snapshot["local"]))
        self.message.setText(f"Resolved: {len(self.choices)}/{len(self.snapshot['local'])}. "
                             + ("" if self.snapshot["complete"] else "Partial listing: absence cannot authorize upload."))

    def previews(self, payload):
        for key, widget in (("local", self.local_image), ("remote", self.remote_image)):
            preview = payload.get(key)
            if not preview:
                continue
            uri = str(preview.get("uri") or "")
            if "," not in uri:
                widget.clear()
                widget.setText("Preview unavailable")
                continue
            try:
                raw = base64.b64decode(uri.split(",", 1)[1], validate=True)
            except (ValueError, binascii.Error):
                widget.clear()
                widget.setText("Preview unavailable")
                continue
            pixmap = QPixmap()
            if not pixmap.loadFromData(raw) or pixmap.isNull():
                widget.clear()
                widget.setText("Preview unavailable")
                continue
            widget.setPixmap(pixmap.scaled(340, 280, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def save(self):
        ready = sum(value == 0 for value in self.choices.values())
        if QMessageBox.question(self, "Confirm story review",
                                f"Record {len(self.choices)-ready} existing publications and authorize {ready} later upload(s)?\n"
                                "Nothing is published by this action.", QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) == QMessageBox.Yes:
            self.request("commit", {"choices": self.choices.copy(), "confirmed": True})
