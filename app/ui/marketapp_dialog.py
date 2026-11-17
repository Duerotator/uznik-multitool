"""Read-only catalog browser. Selection is ephemeral, not a rental/account binding."""
from datetime import datetime, timezone
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
                              QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit,
                              QPushButton, QSpinBox, QVBoxLayout)

from core.design_tokens import TOKENS


class MarketappDialog(QDialog):
    def __init__(self, request, parent=None, selected_accounts=None):
        super().__init__(parent)
        self.request = request
        self.page = None
        self.generation = 0
        self.pending = False
        self.selected_accounts = selected_accounts or (lambda: [])
        self.review_token = None
        self.scan_account = None
        self.setWindowTitle("Gift rental catalog · read-only")
        self.resize(740, 800)
        self.setFont(QFont("Segoe UI", 10))
        palette = self.palette()
        for role, color in ((QPalette.Window, TOKENS.panel), (QPalette.WindowText, TOKENS.text),
                            (QPalette.Base, TOKENS.background), (QPalette.Text, TOKENS.text),
                            (QPalette.Button, TOKENS.panel_secondary), (QPalette.ButtonText, TOKENS.text),
                            (QPalette.PlaceholderText, TOKENS.muted), (QPalette.Link, TOKENS.text)):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        label = QLabel("Marketapp · mainnet\nMy rentals: check one selected account, then show/hide hosted gifts. No payment or transfer.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.mode = QComboBox()
        self.mode.addItem("Available for rent", "catalog")
        self.mode.addItem("My active rentals", "rentals")
        self.mode.currentIndexChanged.connect(self.change_mode)
        layout.addWidget(self.mode)
        form = QFormLayout()
        self.form = form
        self.filters = {}
        for key, title, maximum in (("collection_address", "Collection address", 100), ("model", "Model", 200),
                                     ("symbol", "Symbol", 200), ("backdrop", "Backdrop", 200)):
            field = QLineEdit()
            field.setMaxLength(maximum)
            field.setPlaceholderText("Optional")
            field.textChanged.connect(self.invalidate)
            self.filters[key] = field
            form.addRow(title, field)
        self.limit = QSpinBox()
        self.limit.setRange(1, 100)
        self.limit.setValue(10)
        self.limit.valueChanged.connect(self.invalidate)
        form.addRow("Page size", self.limit)
        self.sort = QComboBox()
        for title, key in (("Daily price", "price_per_day"), ("Minimum price", "min_price"),
                           ("Recently updated", "recently_touch"), ("Duration ascending", "duration_asc"),
                           ("Duration descending", "duration_desc")):
            self.sort.addItem(title, key)
        self.sort.currentIndexChanged.connect(self.invalidate)
        form.addRow("Sort", self.sort)
        layout.addLayout(form)
        self.sellable = QCheckBox("Include gifts sellable during rent (may end early)")
        self.sellable.toggled.connect(self.invalidate)
        layout.addWidget(self.sellable)
        self.message = QLabel("Token is loaded from the dedicated Marketapp vault (or backend environment fallback if no vault exists); never enter it here.")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.PlainText)
        layout.addWidget(self.message)
        row = QHBoxLayout()
        self.fetch = QPushButton("Read catalog…")
        self.next_page = QPushButton("Next page…")
        self.next_page.setEnabled(False)
        self.fetch.clicked.connect(lambda: self.read(False))
        self.next_page.clicked.connect(lambda: self.read(True))
        for button in (self.fetch, self.next_page):
            button.setMinimumHeight(36)
            row.addWidget(button)
        layout.addLayout(row)
        self.items = QListWidget()
        self.items.setWordWrap(True)
        self.items.currentRowChanged.connect(self.choose)
        layout.addWidget(self.items, 1)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(170)
        layout.addWidget(self.detail, 1)
        self.hosting_info = QLabel('First hosting/relink and full detach: <a href="https://fragment.com/">official Fragment</a>.\nHiding is NOT detaching hosting or ending rent.')
        self.hosting_info.setWordWrap(True)
        self.hosting_info.setOpenExternalLinks(True)
        layout.addWidget(self.hosting_info)
        hosting = QHBoxLayout()
        self.scan = QPushButton("Check selected account…")
        self.show_gift = QPushButton("Show on profile…")
        self.hide_gift = QPushButton("Hide from profile…")
        for button, action in ((self.scan, "scan"), (self.show_gift, "show"), (self.hide_gift, "hide")):
            hosting.addWidget(button)
            button.clicked.connect(lambda checked=False, value=action: self.hosting_action(value))
        layout.addLayout(hosting)
        self.hosting_controls()
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        layout.addWidget(close)

    def invalidate(self, *args):
        self.page = None
        self.review_token = None
        self.scan_account = None
        self.items.clear()
        self.detail.setPlainText("No gift selected. Selection is local only.")
        self.next_page.setEnabled(False)
        if hasattr(self, "scan"):
            self.hosting_controls()

    def change_mode(self):
        self.generation += 1
        self.invalidate()
        self.controls(False)
        self.fetch.setText("Read my rentals…" if self.mode.currentData() == "rentals" else "Read catalog…")

    def hosting_controls(self):
        rentals = self.mode.currentData() == "rentals"
        self.hosting_info.setVisible(rentals)
        for button in (self.scan, self.show_gift, self.hide_gift):
            button.setVisible(rentals)
        self.scan.setEnabled(not self.pending and bool(self.page))
        index = self.items.currentRow()
        selected = self.selected_accounts()
        item = self.page["items"][index] if self.page and 0 <= index < len(self.page["items"]) else None
        host = next((row for row in item.get("observed_accounts", []) if row["account_id"] == self.scan_account and row["hosting_verified"] and row["fresh"]), None) if item else None
        allowed = not self.pending and bool(self.review_token and host and not item.get("expired") and selected == [self.scan_account])
        self.show_gift.setEnabled(allowed and not host["visible"])
        self.hide_gift.setEnabled(allowed and host["visible"])

    def controls(self, busy):
        self.pending = busy
        rentals = self.mode.currentData() == "rentals"
        for field in [*self.filters.values(), self.sort, self.sellable]:
            field.setEnabled(not busy and not rentals)
        for field in [*self.filters.values(), self.sort]:
            self.form.setRowVisible(field, not rentals)
        self.sellable.setVisible(not rentals)
        for field in (self.limit, self.mode, self.fetch):
            field.setEnabled(not busy)
        self.next_page.setEnabled(not busy and bool(self.page and self.page.get("cursor")))
        self.hosting_controls()

    def read(self, next_page):
        if self.pending or next_page and not (self.page and self.page.get("cursor")):
            return
        if QMessageBox.question(self, "Confirm catalog read", "Read one page from api.marketapp.org using the backend token?\nNo purchase or Telegram action.",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        payload = {key: field.text().strip() for key, field in self.filters.items() if field.text().strip()}
        payload.update(confirmed=True, limit=self.limit.value(), sort_by=self.sort.currentData(), include_sellable=self.sellable.isChecked())
        if self.mode.currentData() == "rentals":
            payload = {"confirmed": True, "limit": self.limit.value(), "_action": "rentals"}
        if next_page:
            payload["cursor"] = self.page["cursor"]
        self.generation += 1
        self.invalidate()
        self.controls(True)
        self.message.setText("Reading one bounded page… no automatic retries.")
        self.request(payload, self.generation)

    def render(self, result, generation):
        if generation != self.generation or not self.isVisible():
            return
        self.controls(False)
        if not result.get("ok"):
            self.invalidate()
            self.message.setText(result.get("error", "Catalog unavailable; retry explicitly later"))
            return
        self.page = result["catalog"]
        self.review_token = result.get("review_token") if result.get("complete") else None
        self.scan_account = result.get("account_id")
        checked = datetime.fromtimestamp(self.page['checked_at'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        self.message.setText(f"Provider snapshot: {checked}. Availability can change; not a quote.")
        if result.get("complete") is False:
            self.message.setText("Own gift scan is incomplete (bounded to 500). Visibility changes are disabled; absence is not globally unlinked.")
        elif result.get("detail"):
            self.message.setText(result["detail"])
        for item in self.page["items"]:
            self.items.addItem(item["nft_name"] + " · " + item["price_per_day_nanograms"] + " nanograms/day")
        self.items.setCurrentRow(-1)
        self.detail.setPlainText("Choose a gift locally; no purchase or account binding." if self.page["items"] else "No matching gifts on this page.")
        self.controls(False)

    def choose(self, index):
        if not self.page or not 0 <= index < len(self.page["items"]):
            return
        item = self.page["items"][index]
        if item.get("my_rented"):
            hosts = "; ".join(row["account_id"] + (" — host verified" if row["hosting_verified"] else " — hosting unknown") +
                              (", shown" if row["visible"] else ", hidden") + ("" if row["fresh"] else " (stale)") for row in item.get("observed_accounts", []))
            self.detail.setPlainText(item["nft_name"] + "\nNFT: " + item["nft_address"] +
                f"\nRent ends: Unix {item['end_time']} · expired: {item['expired']}\nPrice/day: {item['price_per_day_nanograms']} nanograms (provider unit)" +
                f"\nEarly termination risk: {item['early_termination_risk']}\nObserved accounts: {hosts or 'not checked'}" +
                "\nOnly explicitly checked accounts. Hiding is not detaching hosting.")
            self.hosting_controls()
            return
        self.detail.setPlainText(item["nft_name"] + "\n" + item["price_per_day_nanograms"] + " nanograms/day (provider unit)\n" +
            f"Duration: {item['min_duration']}–{item['max_duration']} seconds · discount/day: {item['discount_per_day']}%\n" +
            ("WARNING: sellable during rent; may end early.\n" if item["early_termination_risk"] else "Provider reports not sellable during rent; not a guarantee.\n") +
            "Total + fees: unknown · provider_reported, not chain proof\nNFT: " + item["nft_address"] + "\nNot saved. No purchase or Telegram account binding.")

    def hosting_action(self, action):
        if self.pending or self.mode.currentData() != "rentals" or not self.page:
            return
        ids = self.selected_accounts()
        if len(ids) != 1:
            self.message.setText("Select exactly one enabled account in the current group/filter.")
            return
        payload = {"confirmed": True, "account_id": ids[0], "_action": "scan" if action == "scan" else "visibility"}
        if action != "scan":
            index = self.items.currentRow()
            if not self.review_token or self.scan_account != ids[0] or not 0 <= index < len(self.page["items"]):
                return
            payload.update(review_token=self.review_token, nft_address=self.page["items"][index]["nft_address"], visible=action == "show")
        text = ("Read own hosted gifts" if action == "scan" else action.upper() + " " + payload["nft_address"]) + "\nAccount: " + ids[0] + "\nNo transfer/payment. Hiding does not detach hosting."
        if QMessageBox.question(self, "Confirm exact rental action", text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.review_token = None
        self.items.clear()
        self.detail.clear()
        self.generation += 1
        self.controls(True)
        self.message.setText("Checking exact rental/account… up to 90 seconds. Closing does not cancel an approved change; scan before retrying.")
        self.request(payload, self.generation)

    def done(self, result):
        self.generation += 1
        self.invalidate()
        super().done(result)
