"""Local recipe editor and receipt plan. Deliberately no Apply action."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget)
from core.design_tokens import TOKENS


class ProfileStudioDialog(QDialog):
    def __init__(self, report, request, parent=None):
        super().__init__(parent)
        self.request, self.report, self.saved, self.plan = request, report, None, None
        self.apply_token = None
        self.loading, self.pending, self.dirty = True, False, True
        self.setWindowTitle("Profile Studio · review / explicit apply")
        self.setFont(QFont("Segoe UI", 10))
        self.resize(800, 760)
        palette = self.palette()
        for role, color in ((QPalette.Window, TOKENS.panel), (QPalette.WindowText, TOKENS.text),
                (QPalette.Base, TOKENS.background), (QPalette.Text, TOKENS.text),
                (QPalette.Button, TOKENS.panel_secondary), (QPalette.ButtonText, TOKENS.text)):
            palette.setColor(role, QColor(color))
        self.setPalette(palette)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        layout.addWidget(self.label("Profile Studio · preview is local. Apply changes selected profile fields; stories are public and pinned. Partial failure is possible."))
        layout.addWidget(self.label("Exact selected IDs: "+", ".join(report["selection"])))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        editor = QWidget()
        form = QFormLayout(editor)
        form.setSpacing(12)
        scroll.setWidget(editor)
        self.tabs.addTab(scroll, "Recipe")
        self.recipes = QComboBox()
        self.recipes.setMinimumContentsLength(24)
        self.recipes.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        form.addRow("Saved recipe", self.recipes)
        self.title = QLineEdit("Own content")
        self.title.setMaxLength(80)
        form.addRow("Title", self.title)
        self.sources = {}
        for account in report["selection"]:
            field = QLineEdit()
            field.setPlaceholderText("local archive key (not a path)")
            field.setMaxLength(64)
            form.addRow(self.label(account), field)
            self.sources[account] = field
        self.fields = {}
        for name in ("name", "bio", "username", "avatars", "music", "birthday", "stories", "channel", "gifts"):
            box = QCheckBox(name+(" · unsupported" if name in {"channel", "gifts"} else ""))
            self.fields[name] = box
            form.addRow(box)
        self.limit = QSpinBox()
        self.limit.setRange(0, 100)
        self.limit.setValue(18)
        form.addRow("Story limit", self.limit)
        self.all_stories = QCheckBox("All bounded archive stories (still subject to runtime quota)")
        form.addRow(self.all_stories)
        self.clear = QCheckBox("Clear existing avatars only when Apply is confirmed")
        form.addRow(self.clear)
        self.rights = QCheckBox("I own or have permission to use this content")
        form.addRow(self.rights)
        form.addRow(self.label("Rights are operator-declared, not independently verified. Username/music/stories need runtime checks; channel/gifts are unsupported."))
        self.save_button = QPushButton("Save local recipe…")
        form.addRow(self.save_button)
        review = QWidget()
        review_layout = QVBoxLayout(review)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        review_layout.addWidget(self.preview_text)
        self.tabs.addTab(review, "Plan / receipts")
        self.message = self.label("Save an explicit recipe, then inspect its local plan.")
        layout.addWidget(self.message)
        row = QHBoxLayout()
        self.preview_button = QPushButton("Build local preview")
        self.review_button = QPushButton("Mark preview reviewed locally…")
        self.reload_button = QPushButton("Reload recipes")
        for button in (self.preview_button, self.review_button, self.reload_button):
            button.setMinimumHeight(36)
            row.addWidget(button)
        layout.addLayout(row)
        self.apply_button = QPushButton("Apply reviewed profile fields…")
        self.apply_button.setMinimumHeight(40)
        self.apply_button.clicked.connect(self.apply)
        layout.addWidget(self.apply_button)
        self.recipes.currentIndexChanged.connect(self.choose)
        self.save_button.clicked.connect(self.save)
        self.preview_button.clicked.connect(self.preview)
        self.review_button.clicked.connect(self.review)
        self.reload_button.clicked.connect(lambda: self.send("report", {}))
        for field in [self.title, *self.sources.values()]:
            field.textChanged.connect(self.changed)
        for field in [*self.fields.values(), self.all_stories, self.clear, self.rights]:
            field.toggled.connect(self.changed)
        self.limit.valueChanged.connect(self.changed)
        self.render({"report": report})

    @staticmethod
    def label(text):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        return label

    def changed(self, *_):
        if not self.loading:
            self.dirty, self.plan = True, None
            self.apply_token = None
            self.preview_text.clear()
            self.controls()

    def choose(self, *_):
        self.loading = True
        self.apply_token = None
        self.saved = self.recipes.currentData()
        spec = self.saved["spec"] if self.saved else {"title": "Own content", "fields": ["name", "bio", "avatars", "stories"],
            "assignments": {}, "rights_declared": False, "max_stories": 18, "clear_avatars": False}
        self.title.setText(spec["title"])
        for account, field in self.sources.items():
            field.setText(spec["assignments"].get(account, ""))
        for key, field in self.fields.items():
            field.setChecked(key in spec["fields"])
        self.all_stories.setChecked(spec["max_stories"] is None)
        self.limit.setValue(spec["max_stories"] if spec["max_stories"] is not None else 18)
        self.clear.setChecked(spec["clear_avatars"])
        self.rights.setChecked(spec["rights_declared"])
        self.dirty, self.plan, self.loading = not bool(self.saved), None, False
        self.preview_text.clear()
        self.controls()

    def controls(self):
        for widget in (self.recipes, self.title, *self.sources.values(), *self.fields.values(),
                self.all_stories, self.clear, self.rights, self.save_button, self.reload_button):
            widget.setEnabled(not self.pending)
        self.limit.setEnabled(not self.pending and not self.all_stories.isChecked())
        self.preview_button.setEnabled(not self.pending and bool(self.saved) and not self.dirty)
        self.review_button.setEnabled(not self.pending and bool(self.plan) and not any(item["blockers"] for item in self.plan["accounts"]))
        self.apply_button.setEnabled(not self.pending and bool(self.apply_token) and not self.dirty)

    def busy(self, value):
        self.pending = value
        self.controls()

    def send(self, action, payload):
        if self.pending:
            return
        self.busy(True)
        self.message.setText("Queueing explicitly confirmed Apply…" if action=="apply" else "Working locally… No Telegram requests.")
        self.request(action, payload)

    def confirmed(self, text, *, title="Confirm local action"):
        prompt = QMessageBox(QMessageBox.Question, title, text, QMessageBox.Yes | QMessageBox.No, self)
        prompt.setTextFormat(Qt.PlainText)
        prompt.setDefaultButton(QMessageBox.No)
        return prompt.exec() == QMessageBox.Yes

    def save(self):
        if self.pending or not self.confirmed("Save the recipe for these exact selected IDs? No account is changed or published to."):
            return
        spec = {"title": self.title.text(), "fields": [key for key, box in self.fields.items() if box.isChecked()],
            "assignments": {key: field.text().strip() for key, field in self.sources.items()},
            "rights_declared": self.rights.isChecked(), "max_stories": None if self.all_stories.isChecked() else self.limit.value(),
            "clear_avatars": self.clear.isChecked()}
        self.plan = None
        self.send("save", {"spec": spec, "recipe_id": self.saved["id"] if self.saved else None,
            "expected_revision": self.saved["revision"] if self.saved else None, "confirmed": True, "expected_context": self.report["context"]})

    def preview(self):
        if not self.preview_button.isEnabled():
            return
        self.plan = None
        self.apply_token = None
        self.send("preview", {"recipe_id": self.saved["id"], "expected_revision": self.saved["revision"], "expected_context": self.report["context"]})

    def review(self):
        if not self.review_button.isEnabled() or not self.confirmed("Record local review of this exact preview? No Telegram step is executed or marked done."):
            return
        self.send("review", {"token": self.plan["token"], "confirmed": True, "expected_context": self.report["context"]})

    def fail(self, message):
        self.plan = None
        self.apply_token = None
        self.busy(False)
        self.message.setText(message+" Reload/rebuild before review.")

    def apply(self):
        if not self.apply_button.isEnabled() or not self.confirmed("APPLY the reviewed fields to these exact IDs?\n"+", ".join(self.report["selection"])+"\nName/bio/username/avatars/music/birthday may change as selected. Stories are public and pinned. Explicit avatar clearing may delete existing avatars. Partial failure is possible; no automatic retry.",title="Confirm Telegram profile changes"):
            return
        token,self.apply_token = self.apply_token,None
        self.send("apply",{"token":token,"confirmed":True,"expected_context":self.report["context"]})

    def render(self, result):
        if "report" in result:
            self.report = result["report"]
            self.recipes.blockSignals(True)
            self.recipes.clear()
            self.recipes.addItem("New recipe", None)
            for saved in self.report["recipes"]:
                self.recipes.addItem(saved["spec"]["title"], saved)
            index = next((n+1 for n, item in enumerate(self.report["recipes"]) if item["id"] == (result.get("saved") or {}).get("id")), 0)
            self.recipes.setCurrentIndex(index)
            self.recipes.blockSignals(False)
            self.choose()
        if "plan" in result:
            self.apply_token = None
            self.plan = result["plan"]
            lines = [self.plan["detail"], "Rights: "+self.plan["rights"]]
            for account in self.plan["accounts"]:
                lines.append("\n"+account["account_id"]+" ← "+account["source"]+" · "+account["status"])
                lines.extend(f"{key}: {value}" for key,value in account.get("summary",{}).items())
                if account.get("clear_avatars"): lines.append("CLEAR EXISTING AVATARS selected")
                lines.extend("BLOCKED: "+value for value in account["blockers"])
                lines.extend(f"{item['step']} · receipt={item['checkpoint']} · {item['capability']} · runtime={item['runtime_preflight']}" for item in account["steps"])
            self.preview_text.setPlainText("\n".join(lines))
            self.tabs.setCurrentIndex(1)
        if "review" in result:
            self.plan = None
            self.apply_token = result["review"].get("apply_token")
        if result.get("apply_queued"):
            self.plan,self.apply_token = None,None
        self.busy(False)
        self.message.setText(result.get("detail",result.get("review", {}).get("detail", "Preview/review is local. Apply needs a separate confirmation. Unsupported/uncertain steps cannot be bypassed.")))

    def closeEvent(self, event):
        if self.pending:
            self.message.setText("Wait for the local Studio operation to finish before closing.")
            event.ignore()
            return
        super().closeEvent(event)

    def done(self, result):
        if self.pending:
            self.message.setText("Wait for the local Studio operation to finish before closing.")
            return
        super().done(result)
