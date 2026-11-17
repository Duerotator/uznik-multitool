"""Public addresses only; displaying a record never fetches a live balance."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout
from core.design_tokens import TOKENS
from core.ton_address import normalize_address, AddressError


class AssetDialog(QDialog):
    def __init__(self, report, request, parent=None):
        super().__init__(parent)
        self.request = request
        self.setWindowTitle("Public assets · read-only")
        self.setFont(QFont("Segoe UI", 10))
        self.resize(700, 680)
        palette = self.palette()
        for role, color in ((QPalette.Window, TOKENS.panel), (QPalette.WindowText, TOKENS.text),
                            (QPalette.Base, TOKENS.background), (QPalette.Text, TOKENS.text),
                            (QPalette.Button, TOKENS.panel_secondary), (QPalette.ButtonText, TOKENS.text)):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.heading = self.label("Public assets · "+report["account_id"])
        self.heading.setFont(QFont("Segoe UI", 16, QFont.DemiBold))
        layout.addWidget(self.heading)
        layout.addWidget(self.label("Public address declarations and native TON only. No wallet activation, seed, signing or transfers. A balance does not verify Telegram Money, a Gram jetton, ownership or a reward."))
        form = QFormLayout()
        form.setSpacing(12)
        self.network = QComboBox()
        self.network.addItem("Testnet", "testnet")
        self.network.addItem("Mainnet", "mainnet")
        self.network.setCurrentIndex(self.network.findData(report["network"]))
        self.address = QLineEdit()
        self.address.setMaxLength(100)
        self.address.setPlaceholderText("Public TON address (raw or friendly), never a seed")
        self.source = QComboBox()
        for name, value in (("Manual entry (unverified)", "manual"), ("Seen in Telegram (operator declaration)", "telegram_view"), ("Provider export (operator declaration)", "provider_export")):
            self.source.addItem(name, value)
        form.addRow("Network", self.network)
        form.addRow("Public address", self.address)
        form.addRow("Declared source", self.source)
        layout.addLayout(form)
        self.state, self.observation = self.label(""), self.label("")
        layout.addWidget(self.state)
        layout.addWidget(self.observation)
        layout.addStretch()
        self.message = self.label("Opening/refreshing this screen reads the local database only.")
        layout.addWidget(self.message)
        self.save_button = QPushButton("Save public declaration…")
        self.fetch_button = QPushButton("Read public TON balance…")
        self.reload_button = QPushButton("Reload local record")
        for button in (self.save_button, self.fetch_button, self.reload_button):
            button.setMinimumHeight(36)
            layout.addWidget(button)
        self.save_button.clicked.connect(self.save)
        self.fetch_button.clicked.connect(self.fetch)
        self.reload_button.clicked.connect(self.reload)
        self.network.currentIndexChanged.connect(self.reload)
        self.render(report)

    @staticmethod
    def label(text):
        result = QLabel(text)
        result.setTextFormat(Qt.PlainText)
        result.setWordWrap(True)
        result.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return result

    def render(self, report):
        if report["network"] != self.network.currentData():
            return
        self.report = report
        binding, observation = report["binding"], report["observation"]
        self.address.setText(binding["friendly"] if binding else "")
        self.source.setCurrentIndex(self.source.findData(binding["source"]) if binding else 0)
        self.state.setText(f"Balance status: {report['status']} · Chain: {report['chain_state']}\n"
                           f"Account link: {report['account_link']} · Money: unknown · Signer: unknown · Contract type: unknown")
        tx_lt = (observation.get("last_transaction") or {}).get("lt", "unknown") if observation else None
        self.observation.setText((f"Last observed native TON: {observation['balance_native']} TON\n"
            f"Provider: {observation['provider']} · Verification: provider_reported, not a chain proof\n"
            f"Read time (Unix): {report['checked_at']} · Provider sync: {observation['source_sync_time']}\n"
            f"Block seqno: {observation['block_seqno']} · Last transaction LT: {tx_lt}"
            if observation else "Balance: unknown — no successful provider observation for this binding.")+
            (f"\nLast error: {report['error']} — previous amount is not current." if report.get("error") else ""))
        self.message.setText("Displaying local saved data. A provider read requires a separate confirmation.")
        self.busy(False)

    def fail(self, error):
        self.busy(False)
        if getattr(self, "report", {}).get("network") != self.network.currentData():
            self.state.setText(f"Could not load local record for {self.network.currentData()}. Retry Reload.")
            self.observation.clear()
        self.message.setText(str(error))

    def busy(self, value):
        for widget in (self.network, self.address, self.source, self.save_button, self.reload_button):
            widget.setEnabled(not value)
        self.fetch_button.setEnabled(not value and bool(getattr(self, "report", {}).get("binding")))
        matching = getattr(self, "report", {}).get("network") == self.network.currentData()
        self.save_button.setEnabled(not value and matching)
        self.fetch_button.setEnabled(self.fetch_button.isEnabled() and matching)

    def reload(self):
        if self.report["network"] != self.network.currentData():
            self.address.clear()
            self.state.setText("Loading local record for "+self.network.currentData()+"…")
            self.observation.clear()
        self.busy(True)
        self.request("report", self.network.currentData(), {})

    def confirm(self, title, text):
        message = QMessageBox(QMessageBox.Question, title, text, QMessageBox.Yes | QMessageBox.No, self)
        message.setTextFormat(Qt.PlainText)
        message.setDefaultButton(QMessageBox.No)
        return message.exec() == QMessageBox.Yes

    def save(self):
        if self.report["network"] != self.network.currentData():
            return
        try:
            canonical = normalize_address(self.address.text(), self.network.currentData()).raw
        except AddressError as exc:
            self.message.setText(str(exc))
            return
        if not self.confirm("Confirm local address declaration", f"Account: {self.report['account_id']}\nNetwork: {self.network.currentData()}\nAddress: {canonical}\nStore this unverified public declaration locally? No Telegram or provider request is made."):
            return
        binding = self.report["binding"]
        self.busy(True)
        self.request("register", self.network.currentData(), {"address": canonical, "source": self.source.currentData(),
            "expected_revision": binding["revision"] if binding else None, "expected_context": self.report["account_context"], "confirmed": True})

    def fetch(self):
        binding = self.report["binding"]
        if not binding or self.report["network"] != self.network.currentData():
            return
        if not self.confirm("Read public chain provider", f"Send this public address to TON Center?\n{self.report['network']}: {binding['address']}\nNative TON only. No Telegram session is opened and no transfer is sent. Rate limits may skip the read."):
            return
        self.busy(True)
        self.request("refresh", self.network.currentData(), {"expected_revision": binding["revision"],
            "expected_context": self.report["account_context"], "confirmed": True})
