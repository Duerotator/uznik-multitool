"""Review cached Guard evidence; callbacks never revoke sessions."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget
from core.design_tokens import TOKENS


class GuardDialog(QDialog):
    def __init__(self, report, request, parent=None):
        super().__init__(parent)
        self.request = request
        self.setWindowTitle("Security Guard · observation review")
        self.setFont(QFont("Segoe UI", 10))
        self.resize(720, 680)
        palette = self.palette()
        for role, color in ((QPalette.Window, TOKENS.panel), (QPalette.WindowText, TOKENS.text),
                            (QPalette.Base, TOKENS.background), (QPalette.Text, TOKENS.text),
                            (QPalette.Button, TOKENS.panel_secondary), (QPalette.ButtonText, TOKENS.text)):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.heading = self.label("")
        self.heading.setFont(QFont("Segoe UI", 16, QFont.DemiBold))
        layout.addWidget(self.heading)
        self.status = self.label("")
        layout.addWidget(self.status)
        layout.addWidget(self.label("Read-only. Unknown does not mean hostile; Open acc may create a legitimate authorization. Select only sessions you recognize. Device names are not proof of ownership. Nothing here revokes sessions or protects the phone number."))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.setSpacing(12)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        buttons = QHBoxLayout()
        self.refresh = QPushButton("Refresh saved observation")
        self.refresh.clicked.connect(self.reload)
        self.trust = QPushButton("Save reviewed trust list…")
        self.trust.clicked.connect(self.save)
        buttons.addWidget(self.refresh)
        buttons.addWidget(self.trust)
        layout.addLayout(buttons)
        self.message = self.label("Check now / Watch selected are in the main Security section. Saved observations are not a live event stream.")
        layout.addWidget(self.message)
        self.pending = False
        self.choices = {}
        self.render(report)

    @staticmethod
    def label(text):
        result = QLabel(text)
        result.setTextFormat(Qt.PlainText)
        result.setWordWrap(True)
        return result

    def busy(self, value):
        self.pending = bool(value)
        self.refresh.setEnabled(not self.pending)
        snapshot = getattr(self, "report", {}).get("snapshot")
        age = getattr(self, "report", {}).get("age_seconds")
        state = getattr(self, "report", {}).get("status")
        self.trust.setEnabled(not self.pending and bool(snapshot) and state != "stale" and age is not None and age <= 600)
        for box in getattr(self, "choices", {}).values():
            box.setEnabled(not self.pending and not box.property("unconfirmed"))

    def reload(self):
        if self.pending:
            return
        self.busy(True)
        self.request("report", {})

    def fail(self, error):
        self.busy(False)
        self.message.setText(str(error))

    def render(self, report):
        self.report = report
        self.heading.setText("Guard · "+report["account_id"])
        snapshot = report.get("snapshot")
        age = report.get("age_seconds")
        state = report["status"]
        self.status.setText(f"State: {state} · Last successful check: {age if age is not None else 'never'} seconds ago"
                            +(f" · Error: {report['error']}" if report.get("error") else ""))
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.choices = {}
        if snapshot:
            self.rows.addWidget(self.label(f"Telegram owner: {snapshot['owner_id']} · 2FA: {'on' if snapshot['twofa_enabled'] else 'off'} · Recovery: {'set' if snapshot['recovery_email_set'] else 'not observed'}"))
            if snapshot["password_reset_pending"]:
                self.rows.addWidget(self.label(f"ATTENTION: pending 2FA password reset, deadline (Unix): {snapshot['password_reset_deadline']}. This is not an account deletion check."))
            self.rows.addWidget(self.label("Current MTProto authorization is excluded below. This list contains other authorizations only."))
            trusted = set(report.get("baseline") or [])
            for row in snapshot["sessions"]:
                box = QCheckBox()
                # Put untrusted Telegram strings in a plain-text label; avoid
                # native checkbox mnemonic/rich-text interpretation.
                box.setAccessibleName("Trust authorization "+row["fingerprint"][:12])
                box.setChecked(row["fingerprint"] in trusted)
                box.setProperty("unconfirmed", bool(row["unconfirmed"]))
                box.setEnabled(not row["unconfirmed"])
                line = QWidget()
                inner = QHBoxLayout(line)
                inner.setContentsMargins(0, 0, 0, 0)
                inner.addWidget(box)
                inner.addWidget(self.label(row["label"]+" · "+row["fingerprint"][:12]+(" · UNCONFIRMED (not selectable)" if row["unconfirmed"] else "")), 1)
                self.rows.addWidget(line)
                self.choices[row["fingerprint"]] = box
            if not snapshot["sessions"]:
                self.rows.addWidget(self.label("No other authorizations were returned. Saving an empty list explicitly trusts none."))
        else:
            self.rows.addWidget(self.label("No successful Guard snapshot yet. Run Guard check selected first."))
        active = [row for row in report["events"] if row["active"]]
        self.rows.addWidget(self.label("Active conditions: "+(", ".join(sorted({row["kind"] for row in active})) or "none listed — not a safety guarantee")))
        for event in report["events"][:30]:
            self.rows.addWidget(self.label(f"{'Active' if event['active'] else 'Resolved'} · {event['kind']} · first {int(event['first_seen'])}, last {int(event['last_seen'])}"))
        self.rows.addStretch()
        self.busy(False)

    def save(self):
        if self.pending or not self.trust.isEnabled():
            return
        values = [key for key, box in self.choices.items() if box.isChecked() and not box.property("unconfirmed")]
        if QMessageBox.question(self, "Confirm reviewed authorizations",
                f"Replace the trust list with {len(values)} reviewed authorization(s)?\nUnchecked sessions remain unknown. Nothing is revoked. Do not trust unknown sessions just to clear a warning.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.busy(True)
        self.request("trust", {"token": self.report["snapshot_token"], "fingerprints": values, "confirmed": True})
