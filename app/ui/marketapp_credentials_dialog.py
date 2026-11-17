"""Local-only token settings; no secret crosses the web/action/task boundary."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout

from core.design_tokens import TOKENS
from modules.marketapp_credentials import MarketappCredentials
from ui.vault_dialog import _VaultWorker


class MarketappCredentialsDialog(QDialog):
    def __init__(self, data_dir, parent=None):
        super().__init__(parent)
        self.credentials = MarketappCredentials(data_dir)
        self.worker = None
        self.setWindowTitle("Marketapp credentials · local only")
        self.resize(620, 560)
        self.setFont(QFont("Segoe UI", 10))
        palette = self.palette()
        for role, color in ((QPalette.Window, TOKENS.panel), (QPalette.WindowText, TOKENS.text),
                            (QPalette.Base, TOKENS.background), (QPalette.Text, TOKENS.text),
                            (QPalette.Button, TOKENS.panel_secondary), (QPalette.ButtonText, TOKENS.text)):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        intro = QLabel("Dedicated encrypted API-token vault, separate from Telegram 2FA.\nCreating it opts out of environment fallback, even when locked/empty. No automatic token import or provider request.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.buttons = {}
        for key, title, callback in (("unlock", "Create / unlock dedicated vault…", self.unlock),
                                     ("store", "Save / replace API token…", self.store),
                                     ("lock", "Lock in this process", lambda: self.run(self.credentials.vault.lock)),
                                     ("remove", "Remove local token…", self.remove),
                                     ("backup", "Save encrypted backup…", self.backup),
                                     ("restore", "Restore backup into empty vault…", self.restore),
                                     ("refresh", "Refresh local status", lambda: self.run(lambda: None))):
            button = QPushButton(title)
            button.setMinimumHeight(36)
            button.clicked.connect(callback)
            self.buttons[key] = button
            layout.addWidget(button)
        self.message = QLabel("Never enter a seed phrase. Locking does not cancel an already submitted request. Other processes have independent unlock state.")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.PlainText)
        layout.addWidget(self.message)
        self.run(lambda: None)

    def confirm(self, text):
        return QMessageBox.question(self, "Confirm local credential action", text,
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def password(self, title, prompt):
        value, accepted = QInputDialog.getText(self, title, prompt, QLineEdit.Password)
        return value if accepted and value else None

    def run(self, operation):
        if self.worker and self.worker.isRunning():
            return
        for button in self.buttons.values():
            button.setEnabled(False)
        self.message.setText("Working locally… no provider request.")
        def work():
            operation()
            return self.credentials.status()
        self.worker = _VaultWorker(work, self)
        self.worker.completed.connect(self.complete)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def complete(self, state, error):
        for button in self.buttons.values():
            button.setEnabled(True)
        if error:
            self.status.setText("Status unavailable; refresh or unlock locally.")
            self.message.setText(error)
            for key in ("store", "remove", "backup"):
                self.buttons[key].setEnabled(False)
            return
        self.status.setText("Source: " + state["source"] + " · " +
            ("unlocked" if state["unlocked"] else "locked / not created") +
            " · token configured: " + str(state["configured"]) + " · idle timeout: 10 minutes")
        for key in ("store", "remove", "backup"):
            self.buttons[key].setEnabled(state["unlocked"])
        self.buttons["restore"].setEnabled(not state["initialized"])
        self.message.setText("Local operation completed. No token value displayed; no network validation performed. Keep encrypted backups and master passphrase separately.")

    def unlock(self):
        creating = not self.credentials.vault.path.exists()
        if creating and not self.confirm("Create a separate Marketapp vault? Environment fallback stops immediately, even before a token is saved. 2FA storage is unchanged."):
            return
        password = self.password("Create / unlock Marketapp vault", "Master passphrase (12+ characters for creation):")
        if not password:
            return
        if creating and self.password("Confirm passphrase", "Enter master passphrase again:") != password:
            self.message.setText("Passphrases do not match; nothing created.")
            return
        self.run(lambda: self.credentials.vault.create(password) if creating else self.credentials.vault.unlock(password))

    def store(self):
        if not self.confirm("Save/replace the encrypted Marketapp API token? Existing rental reviews may need refreshing. No provider request or revocation."):
            return
        token = self.password("Save Marketapp token", "API token only (never a wallet seed):")
        if token:
            self.run(lambda: self.credentials.store(token, confirmed=True))

    def remove(self):
        if self.confirm("Remove the local encrypted token? No environment fallback. Existing backups and provider-side token remain valid; this is NOT provider revocation."):
            self.run(lambda: self.credentials.remove(confirmed=True))

    def backup(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save NEW encrypted Marketapp backup", "", "Encrypted backup (*.enc)")
        if path:
            self.run(lambda: self.credentials.vault.backup(path))

    def restore(self):
        path, _ = QFileDialog.getOpenFileName(self, "Restore Marketapp backup into EMPTY vault", "", "Encrypted backup (*.enc)")
        if not path or not self.confirm("Restore this encrypted backup into an empty dedicated Marketapp vault? Existing storage is never overwritten. Use only a backup from this dedicated vault."):
            return
        password = self.password("Restore backup", "Master passphrase for this dedicated backup:")
        if password:
            self.run(lambda: self.credentials.restore(path, password))

    def done(self, result):
        if self.worker and self.worker.isRunning():
            self.message.setText("Wait for the local operation to finish before closing.")
            return
        super().done(result)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.message.setText("Wait for the local operation to finish before closing.")
            event.ignore()
        else:
            super().closeEvent(event)
