"""Local-only vault controls. Master passwords never enter task/log state."""
from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (QDialog, QFileDialog, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget)
try:
    import shiboken6
except ImportError:  # pragma: no cover
    shiboken6 = None

from core.design_tokens import TOKENS
from core.secret_vault import VaultError, get_vault
from modules.account_secrets import AccountSecretManager


class _VaultWorker(QThread):
    completed = Signal(object, str)

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            result = self.operation()
            self.completed.emit(result, "")
        except Exception as exc:
            # No traceback, request parameters, credentials or SQLite payloads.
            message = str(exc) if isinstance(exc, VaultError) else f"Operation failed ({type(exc).__name__}). Check the file and access permissions."
            self.completed.emit(None, message)
        finally:
            self.operation = None


class VaultDialog(QDialog):
    def __init__(self, config, account_ids, parent=None):
        super().__init__(parent)
        self.config = config
        self.account_ids = list(dict.fromkeys(account_ids))
        self.vault = get_vault(config.data_dir)
        self.worker = None
        self.buttons = []
        self.setWindowTitle("Secret vault")
        self.setFont(QFont("Segoe UI", 10))
        self.resize(640, 640)
        palette = self.palette()
        for role, color in ((QPalette.Window, TOKENS.panel), (QPalette.WindowText, TOKENS.text),
                            (QPalette.Base, TOKENS.background), (QPalette.Text, TOKENS.text),
                            (QPalette.Button, TOKENS.panel_secondary), (QPalette.ButtonText, TOKENS.text)):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        self.setStyleSheet(
            f"QDialog {{ background: {TOKENS.panel}; color: {TOKENS.text}; }}"
            f"QTabWidget::pane {{ background: {TOKENS.panel}; border: 1px solid {TOKENS.border}; border-radius: 8px; }}"
            f"QTabWidget QWidget#vaultTabPage {{ background: {TOKENS.panel}; color: {TOKENS.text}; }}"
            f"QTabBar::tab {{ background: {TOKENS.panel_secondary}; color: {TOKENS.muted}; border: 1px solid {TOKENS.border}; border-bottom: 0; padding: 7px 14px; border-top-left-radius: 7px; border-top-right-radius: 7px; margin-right: 4px; }}"
            f"QTabBar::tab:selected {{ background: #1d4f8c; color: #ffffff; border-color: #4b8fd8; }}"
            f"QLabel {{ color: {TOKENS.text}; }}"
            f"QPushButton {{ background: {TOKENS.primary}; color: #f8fbff; border: 0; border-radius: 8px; padding: 8px 12px; }}"
            f"QPushButton:hover {{ background: #3a92e8; }}"
            f"QPushButton:disabled {{ background: #1c222b; color: #64748b; border: 1px solid {TOKENS.border}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        title = QLabel("Secret vault")
        title.setFont(QFont("Segoe UI", 18, QFont.DemiBold))
        layout.addWidget(title)
        self.status = self.label("Checking local vault…")
        layout.addWidget(self.status)
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)
        master = QWidget()
        master.setObjectName("vaultTabPage")
        general = QVBoxLayout(master)
        general.setContentsMargins(16, 16, 16, 16)
        general.setSpacing(12)
        general.addWidget(self.label("The master passphrase stays in this process. Auto-lock after 10 minutes without secret access. Losing it means losing access to encrypted backups."))
        self.button(general, "Create vault / Unlock…", self.unlock)
        self.button(general, "Lock in this process", lambda: self.run(self.vault.lock))
        self.button(general, "Save encrypted backup…", self.backup)
        self.button(general, "Verify backup…", self.verify)
        self.button(general, "Restore into an uninitialized vault…", self.restore)
        general.addStretch()
        tabs.addTab(master, "Vault && backups")
        selected = QWidget()
        selected.setObjectName("vaultTabPage")
        actions = QVBoxLayout(selected)
        actions.setContentsMargins(16, 16, 16, 16)
        actions.setSpacing(12)
        actions.addWidget(self.label(f"Fixed selection: {len(self.account_ids)} account(s). To change it, close this window and select accounts in the dashboard."))
        actions.addWidget(self.label("2FA only. These controls do not change Telegram passwords. Move requires an encrypted copy AND a backup containing it. Old backups, session files and .env are not cleaned or encrypted."))
        self.button(actions, "1. Copy existing plaintext 2FA to vault", self.copy, selected=True)
        self.button(actions, "2. Move 2FA after verified backup…", self.move, selected=True)
        self.button(actions, "Store a known current 2FA password…", self.store, selected=True)
        self.button(actions, "Confirm pending change succeeded…", lambda: self.resolve("promote"), selected=True)
        self.button(actions, "Confirm pending change did NOT happen…", lambda: self.resolve("discard"), selected=True)
        actions.addStretch()
        tabs.addTab(selected, "Selected accounts")
        self.message = self.label("No working credentials are moved automatically.")
        layout.addWidget(self.message)
        self.button(layout, "Refresh status", lambda: self.run(lambda: {}))
        self.run(lambda: {})

    @staticmethod
    def label(text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.PlainText)
        return label

    def button(self, layout, text, callback, *, selected=False):
        button = QPushButton(text)
        button.setMinimumHeight(36)
        button.clicked.connect(callback)
        button.setProperty("needs_selection", selected)
        self.buttons.append(button)
        layout.addWidget(button)

    def password(self, title, prompt):
        value, accepted = QInputDialog.getText(self, title, prompt, QLineEdit.Password)
        return value if accepted and value else None

    def confirm(self, text):
        return QMessageBox.question(self, "Confirm local credential change", text,
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _worker_running(self):
        worker = self.worker
        if worker is None:
            return False
        if shiboken6 is not None and not shiboken6.isValid(worker):
            self.worker = None
            return False
        try:
            return bool(worker.isRunning())
        except RuntimeError:
            self.worker = None
            return False

    def _clear_worker(self, worker):
        if self.worker is worker:
            self.worker = None

    def run(self, operation):
        if self._worker_running():
            return
        for button in self.buttons:
            button.setEnabled(False)
        self.message.setText("Working locally…")
        def work():
            result = operation()
            return {"status": self.vault.status(), "result": result}
        worker = _VaultWorker(work, self)
        self.worker = worker
        worker.completed.connect(self.complete)
        worker.finished.connect(lambda w=worker: self._clear_worker(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def complete(self, payload, error):
        for button in self.buttons:
            button.setEnabled(not button.property("needs_selection") or bool(self.account_ids))
        if error:
            self.message.setText(error)
            self.status.setText("Status unavailable after an error — refresh or unlock.")
            return
        state = payload["status"]
        self.status.setText("Not created" if not state["exists"] else
                            f"Unlocked · {state['entries']} encrypted record(s)" if state["unlocked"] else "Locked")
        result = payload.get("result") or {}
        if isinstance(result, dict):
            detail = result.get("detail")
            counts = ", ".join(f"{key}: {result[key]}" for key in ("copied", "skipped", "moved", "stored", "resolved", "entries") if key in result)
            self.message.setText(" · ".join(filter(None, (counts, detail))) or "Local operation completed.")

    def unlock(self):
        # Existence only; cryptographic IO runs in the worker.
        creating = not self.vault.path.exists()
        password = self.password("Create vault" if creating else "Unlock vault", "Master passphrase (12+ characters for creation):")
        if not password:
            return
        if creating and self.password("Confirm master passphrase", "Enter it again:") != password:
            self.message.setText("Passphrases do not match. Nothing was created.")
            return
        self.run(lambda: self.vault.create(password) if creating else self.vault.unlock(password))

    def backup(self):
        filename, _ = QFileDialog.getSaveFileName(self, "New encrypted backup (never overwrites)", "vault-backup.enc", "Encrypted backup (*.enc)")
        if filename:
            self.run(lambda: self.vault.backup(filename))

    def verify(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Verify encrypted backup", "", "Encrypted backup (*.enc)")
        if filename:
            password = self.password("Verify backup", "Master passphrase for this backup:")
            if password:
                self.run(lambda: self.vault.verify_backup(filename, password))

    def restore(self):
        if self.vault.path.exists():
            self.message.setText("Restore is only allowed before a vault is created; existing data is never replaced.")
            return
        filename, _ = QFileDialog.getOpenFileName(self, "Restore encrypted backup", "", "Encrypted backup (*.enc)")
        if filename:
            password = self.password("Restore backup", "Master passphrase for this backup:")
            if password:
                self.run(lambda: self.vault.restore_new(filename, password))

    def copy(self):
        self.run(lambda: AccountSecretManager(self.config).copy_legacy(self.account_ids))

    def move(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Choose backup containing selected 2FA credentials", "", "Encrypted backup (*.enc)")
        if filename and self.confirm(f"Move current plaintext 2FA values for {len(self.account_ids)} selected account(s) to vault references? They will require an unlocked vault. Old copies remain outside this operation."):
            self.run(lambda: AccountSecretManager(self.config).move_legacy(self.account_ids, filename, confirmed=True))

    def store(self):
        password = self.password("Store current 2FA locally", "Known CURRENT Telegram 2FA password (not the vault master):")
        if password and self.confirm(f"Store this SAME current password for {len(self.account_ids)} selected account(s)? This replaces local vault values, does not change/verify Telegram and is not a recovery mechanism."):
            self.run(lambda: AccountSecretManager(self.config).store_current(self.account_ids, password, confirmed=True))

    def resolve(self, decision):
        text = ("I verified separately that the pending 2FA change succeeded." if decision == "promote" else
                "I verified separately that the pending change did NOT happen; keep the old credential.")
        if self.confirm(f"{text}\nStop all 2FA tasks, including other copies, first.\nApply to {len(self.account_ids)} selected account(s)? Do not guess — a lost response is not proof of failure."):
            self.run(lambda: AccountSecretManager(self.config).resolve_pending(self.account_ids, decision, confirmed=True))

    def closeEvent(self, event):
        if self._worker_running():
            self.message.setText("Wait for the local operation to finish before closing.")
            event.ignore()
        else:
            super().closeEvent(event)

    def done(self, result):
        # Escape/reject does not necessarily pass through closeEvent. Do not
        # destroy a live QThread or abandon a credential migration mid-write.
        if self._worker_running():
            self.message.setText("Wait for the local operation to finish before closing.")
            return
        super().done(result)

    def reject(self):
        self.done(QDialog.Rejected)

    def accept(self):
        self.done(QDialog.Accepted)
