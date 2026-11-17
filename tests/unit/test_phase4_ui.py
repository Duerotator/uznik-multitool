import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / "app", _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Offscreen Phase 4 Qt UI regression tests (no live Telegram/TON/MarketApp requests)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QWidget
import shiboken6

from core.config import AppConfig
from ui.carousel_funding_dialog import CarouselFundingDialog
from ui.marketapp_credentials_dialog import MarketappCredentialsDialog
from ui.marketapp_dialog import MarketappDialog
from ui.qt_app import QtDesktopApp
from ui.vault_dialog import VaultDialog
from ui.wallet_x_dialog import WalletXDialog


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


class Phase4QtUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = _app()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env_patch = patch.dict(os.environ, {
            "TELEGRAM_DATA_DIR": str(Path(self.temp.name) / "data"),
            "TELEGRAM_IMPORT_DIR": str(Path(self.temp.name) / "imports"),
        })
        self.addCleanup(self.env_patch.stop)
        self.env_patch.start()
        self.cfg = AppConfig.load(Path(self.temp.name) / ".env")

    def test_clear_layout_phone_formatting_and_global_qss(self):
        mock_target = MagicMock()
        QtDesktopApp._apply_style(mock_target)
        qss = mock_target.setStyleSheet.call_args[0][0]
        for required in (
            "QDialog {",
            "QDialog QScrollArea",
            "QTabWidget::pane",
            "QCheckBox::indicator",
            "QPushButton:disabled",
            "QPlainTextEdit",
            "QSpinBox",
        ):
            self.assertIn(required, qss)

        # _clear_layout hides & detaches widgets immediately (no orphaned ghost buttons)
        host = QWidget()
        layout = QHBoxLayout(host)
        ghost = QPushButton("inbox", host)
        ghost.show()
        layout.addWidget(ghost)
        cleaner = MagicMock()
        QtDesktopApp._clear_layout(cleaner, layout)
        self.assertEqual(0, layout.count())
        self.assertIsNone(ghost.parent())
        self.assertFalse(ghost.isVisible())
        host.deleteLater()

        # _format_phone_cell emits Segoe-safe bracket tags (never tofu emoji flags)
        self.assertEqual("[RU/KZ] +79161234567", QtDesktopApp._format_phone_cell("+79161234567"))
        self.assertEqual("[TON] +88812345678", QtDesktopApp._format_phone_cell("+88812345678"))
        self.assertEqual("none", QtDesktopApp._format_phone_cell(None))

    def test_vault_and_marketapp_credentials_worker_deleted_safety(self):
        from PySide6.QtWidgets import QTabWidget
        from ui.vault_dialog import _VaultWorker

        vd = VaultDialog(self.cfg, ["acc1"])
        self.addCleanup(vd.close)
        if vd.worker is not None and shiboken6.isValid(vd.worker):
            vd.worker.wait(3000)
        self.qapp.processEvents()
        # Tabs label uses '&&' so Qt renders 'Vault & backups' rather than accelerator underline
        tabs = vd.findChild(QTabWidget)
        self.assertIsNotNone(tabs)
        self.assertEqual("Vault && backups", tabs.tabText(0))

        # Simulate C++ deleteLater deletion while attribute might still point at dead wrapper
        real_dead = _VaultWorker(lambda: {}, vd)
        vd.worker = real_dead
        shiboken6.delete(real_dead)
        self.assertFalse(vd._worker_running())
        self.assertIsNone(vd.worker)
        vd.closeEvent(QCloseEvent())

        mcd = MarketappCredentialsDialog(self.cfg.data_dir)
        self.addCleanup(mcd.close)
        if mcd.worker is not None and shiboken6.isValid(mcd.worker):
            mcd.worker.wait(3000)
        self.qapp.processEvents()
        real_dead2 = _VaultWorker(lambda: {}, mcd)
        mcd.worker = real_dead2
        shiboken6.delete(real_dead2)
        self.assertFalse(mcd._worker_running())
        self.assertIsNone(mcd.worker)
        mcd.closeEvent(QCloseEvent())

    def test_wallet_x_dialog_controls_preview_batch_history_and_lock_on_close(self):
        dispatched = []
        closed_locks = []
        dlg = WalletXDialog(
            {"exists": False, "configured": False, "locked_in_process": False, "has_mnemonic": False},
            lambda act, val: dispatched.append((act, val)),
            selected_accounts=lambda: ["acc1", "acc2"],
            on_close_lock=lambda: closed_locks.append(True),
        )
        self.addCleanup(dlg.close)
        dlg.show()
        self.assertEqual(3, dlg.tabs.count())
        self.assertTrue(dlg.create_btn.isVisible())
        self.assertFalse(dlg.preview_withdraw_btn.isEnabled())
        self.assertFalse(dlg.preview_batch_btn.isEnabled())

        # Render configured unlocked Wallet X
        status = {
            "exists": True,
            "configured": True,
            "locked_in_process": False,
            "protection": "windows-current-user-dpapi",
            "has_mnemonic": True,
            "wallet": {
                "network": "mainnet",
                "revision": "abcdef1234567890" * 2,
                "address": "0:" + "11" * 32,
                "friendly_address": "EQARERERERERERERERERERERERERERERERERERERERERER9R",
                "non_bounceable_address": "UQAREREREREREREREREREREREREREREREREREREREREREdqV",
            },
        }
        dlg.render_status(
            status,
            intents=[{
                "id": "aa" * 16, "kind": "external_send", "seqno": 1, "state": "uncertain",
                "send_all": True, "total_nano": "500000000", "created": 1_700_000_000, "expires": 1_700_000_180,
                "error": "timeout",
            }],
        )
        self.assertFalse(dlg.create_btn.isVisible())
        self.assertTrue(dlg.preview_withdraw_btn.isEnabled())
        self.assertTrue(dlg.export_seed_btn.isEnabled())
        self.assertEqual(1, dlg.intent_combo.count())
        self.assertTrue(dlg.recover_btn.isEnabled())

        # Toggle send_all clears/disables amount input
        dlg.send_all_chk.setChecked(True)
        self.assertFalse(dlg.withdraw_amount.isEnabled())
        self.assertEqual("", dlg.withdraw_amount.text())

        # Render chain + withdraw preview + batch preview/progress/done
        dlg.render_chain({
            "address": "0:" + "11" * 32,
            "network": "mainnet",
            "balance_native": "2.5",
            "chain_state": "active",
            "seqno": 1,
            "provider": "toncenter",
            "checked_at": 1_700_000_500,
            "deploy_needed": False,
            "history": [{
                "utime": 1_700_000_100,
                "hash": "bb" * 32,
                "fee_nano": "3000000",
                "incoming": {"amount_nano": "2500000000", "source": "external/mint"},
                "outgoing": [],
            }],
        })
        self.assertEqual(1, dlg.history_table.rowCount())
        self.assertIn("2.5 TON", dlg.chain_status_label.text())

        dlg.render_withdraw_preview({
            "destination_uq": "UQTest",
            "send_all": True,
            "amount_nano": "2495000000",
            "recipient_state": "uninitialized_credited",
            "balance_nano": "2500000000",
            "seqno": 1,
            "deploy": False,
            "estimated_fee_nano": "5000000",
            "bounce": True,
            "warning": "Destination is uninitialized and bounce=True",
            "expected_confirmation": "DRAIN ALL TO UQTest",
            "expires": 9_999_999_999,
        })
        self.assertTrue(dlg.execute_withdraw_btn.isEnabled())
        self.assertIn("ПРЕДУПРЕЖДЕНИЕ", dlg.withdraw_preview_box.toPlainText())

        dlg.render_batch_preview({
            "items": [{"account_id": "acc1", "current_balance_nano": "100000000", "amount_nano": "400000000", "destination_uq": "UQ1"}],
            "skipped": [{"account_id": "acc2", "balance_nano": "600000000", "reason": "already_funded"}],
            "total_ton": "0.4",
            "estimated_fee_budget_nano": "50000000",
            "expires": 9_999_999_999,
        })
        self.assertEqual(2, dlg.batch_table.rowCount())
        self.assertTrue(dlg.execute_batch_btn.isEnabled())
        dlg.render_batch_progress({"index": 0, "total": 1, "phase": "item_done", "account_id": "acc1"})
        dlg.render_batch_done({"state": "completed", "sent": 1, "total": 1, "confirmed": 1, "failed": 0})
        self.assertFalse(dlg.execute_batch_btn.isEnabled())

        # Close locks vault cleanly
        dlg.close()
        self.assertGreaterEqual(len(closed_locks), 1)

    def test_carousel_end_at_wallet_x_and_marketapp_dialog_modeless_flow(self):
        requests = []
        accounts = [
            {"id": "acc1", "username": "alice_user", "uq_address": "UQ1"},
            {"id": "acc2", "username": "bob_user", "uq_address": "UQ2"},
        ]
        cf = CarouselFundingDialog(
            accounts, ["acc1", "acc2"],
            lambda action, payload: requests.append((action, payload)),
            report={
                "id": "draft1", "revision": "rev1", "state": "funding_observed",
                "fund_to": "@alice_user", "fund_user_id": 101,
                "route": [{"id": "acc1", "username": "alice_user"}, {"id": "acc2", "username": "bob_user"}],
                "address": "0:" + "11" * 32, "checks": 1,
            },
        )
        self.addCleanup(cf.close)
        cf.end_at_wallet_x.setChecked(True)
        cf.start_from_wallet_x.setChecked(True)
        cf.render_review({
            "route": [
                {"account_id": "acc1", "address": "0:" + "11" * 32},
                {"account_id": "acc2", "address": "0:" + "22" * 32},
                {"account_id": "__wallet_x__", "address": "0:" + "33" * 32},
            ],
            "net_nano": "300000000",
            "fee_cap_nano": "10000000",
            "debit_cap_nano": "1000000000",
            "daily_cap_nano": "2000000000",
            "enabled": True,
            "expires": 9_999_999_999,
            "token": "tok1",
            "confirmation": "RUN draft1",
        })
        self.assertEqual(3, cf.route.count())
        self.assertIn("Master Wallet X", cf.route.item(2).text())

        # MarketappDialog modeless workspace + sync + rent quote + handoff
        m_reqs = []
        md = MarketappDialog(
            lambda payload, gen: m_reqs.append((payload, gen)),
            selected_accounts=lambda: ["acc1"],
        )
        self.addCleanup(md.close)
        md.show()
        self.assertEqual(3, md.tabs.count())

        md.generation = 1
        md.render({
            "ok": True,
            "workspace": {
                "gifts_catalog": {
                    "cursor": None,
                    "checked_at": 1_700_000_000,
                    "items": [{
                        "nft_address": "0:" + "44" * 32,
                        "nft_name": "B-DayCandle-42",
                        "price_per_day_nanograms": "500000000",
                        "price_per_day_formatted": "0.5 GRAM",
                        "min_duration": 86400,
                        "max_duration": 604800,
                        "min_duration_text": "1d",
                        "max_duration_text": "7d",
                        "min_total_formatted": "0.5 GRAM",
                        "discount_per_day": 0,
                        "early_termination_risk": False,
                        "asset_kind": "gift",
                    }],
                },
                "account_matrix": [{
                    "account_id": "acc1",
                    "username": "alice_user",
                    "hosted_gift_count": 1,
                    "balance": {"status": "ok", "balance_ton": "0.4"},
                }],
                "jobs": [{
                    "job_id": "job1234567890", "operation": "pay", "state": "handoff_required",
                    "account_id": "acc1", "nft_address": "0:" + "44" * 32,
                }],
                "section_errors": {},
            },
        }, 1, action="sync-workspace")
        self.assertEqual(1, md.items.count())
        md.items.setCurrentRow(0)
        self.assertIn("0.5 GRAM", md.detail.toPlainText())
        self.assertIn("Синхронизация завершена", md.message.text())

        md.generation = 2
        md.render({
            "ok": True,
            "quote": {
                "job_id": "job1234567890",
                "operation": "pay",
                "nft_address": "0:" + "44" * 32,
                "account_id": "acc1",
                "total_gram_formatted": "1.5 GRAM",
                "total_ton": "0.15",
                "duration_text": "3d",
                "expires_in_seconds": 180,
                "confirmation_text": "PAY RENT Candle 3d",
            },
        }, 2, action="rent-quote")
        self.assertIn("1.5 GRAM", md.quote_label.text())
        self.assertTrue(md.execute_rent_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
