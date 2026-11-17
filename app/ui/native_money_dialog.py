"""Local Qt-only financial setup. No seed field is served to Web/Mini App."""
import time
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QColor,QFont,QPalette
from PySide6.QtWidgets import (QCheckBox,QComboBox,QDialog,QFormLayout,QInputDialog,QLabel,QLineEdit,
    QMessageBox,QPushButton,QScrollArea,QVBoxLayout,QWidget)

from core.ton_address import format_nano,friendly_address
from core.design_tokens import TOKENS


class NativeMoneyDialog(QDialog):
    def __init__(self,report,targets,request,parent=None):
        super().__init__(parent)
        self.request=request;self.report=report;self.review=None;self.running=False;self.scope_valid=True;self.dirty=False
        self._confirming_dialog=False
        self.setWindowTitle('Money · локальная подпись');self.resize(780,860)
        self.setFont(QFont('Segoe UI',10));self.setMinimumWidth(620)
        palette=self.palette()
        for role,color in ((QPalette.Window,TOKENS.panel),(QPalette.WindowText,TOKENS.text),
            (QPalette.Base,TOKENS.background),(QPalette.Text,TOKENS.text),
            (QPalette.Button,TOKENS.panel_secondary),(QPalette.ButtonText,TOKENS.text),
            (QPalette.PlaceholderText,TOKENS.muted)):
            palette.setColor(role,QColor(color))
        self.setPalette(palette)
        self.setStyleSheet(
            f'QDialog {{ background: {TOKENS.panel}; color: {TOKENS.text}; }}'
            f'QScrollArea#nativeMoneyScroll, QWidget#nativeMoneyBody {{ background: {TOKENS.panel}; border: 0; }}'
            f'QLabel {{ color: {TOKENS.text}; }}'
            f'QCheckBox {{ color: {TOKENS.text}; spacing: 8px; }}'
            f'QCheckBox:disabled {{ color: {TOKENS.muted}; }}'
            f'QLineEdit, QComboBox {{ background: {TOKENS.background}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 8px; padding: 7px 10px; }}'
            f'QComboBox QAbstractItemView {{ background: {TOKENS.panel_secondary}; color: {TOKENS.text}; selection-background-color: #1d4f8c; }}'
            f'QPushButton {{ background: {TOKENS.panel_secondary}; color: {TOKENS.text}; border: 1px solid {TOKENS.border}; border-radius: 8px; padding: 8px 12px; }}'
            f'QPushButton:hover {{ background: #263140; }}'
            f'QPushButton:disabled {{ background: #161b22; color: #64748b; border-color: {TOKENS.border}; }}'
            f'QScrollBar:vertical {{ background: {TOKENS.background}; width: 10px; }}'
            f'QScrollBar::handle:vertical {{ background: {TOKENS.border}; min-height: 24px; border-radius: 4px; }}'
        )
        outer=QVBoxLayout(self);scroll=QScrollArea();scroll.setObjectName('nativeMoneyScroll');scroll.setWidgetResizable(True);outer.addWidget(scroll)
        body=QWidget();body.setObjectName('nativeMoneyBody');scroll.setWidget(body);layout=QVBoxLayout(body);layout.setContentsMargins(24,24,24,24);layout.setSpacing(12)
        layout.addWidget(self.label('Native Money · '+report['account_id']))
        layout.addWidget(self.label('Mainnet, один обычный перевод. Карусель/send-all/сбор не включены. Реальный API ещё не прошёл live-приёмку.'))
        self.status=self.label('');layout.addWidget(self.status)
        self.create=QPushButton('Создать отдельный DPAPI vault…');layout.addWidget(self.create)
        self.create.clicked.connect(lambda:self.local_confirm('create-vault','CREATE FINANCIAL VAULT',{}))
        self.unlock=QPushButton('Разблокировать финансовый vault…');layout.addWidget(self.unlock)
        self.unlock.clicked.connect(lambda:self.local_confirm('unlock','UNLOCK FINANCIAL VAULT',{}))
        self.lock=QPushButton('Заблокировать финансовый vault');layout.addWidget(self.lock)
        self.lock.clicked.connect(lambda:self.request('lock',{}))
        layout.addWidget(self.label('1 · Ключ — только на этом компьютере'))
        form=QFormLayout();form.setSpacing(8)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.seed=QLineEdit();self.seed.setEchoMode(QLineEdit.Password);self.seed.setMaxLength(256)
        self.seed.setPlaceholderText('Raw Ed25519 seed32 (64 hex) ИЛИ 12/24 слова фразы восстановления WalletTg')
        self.anchor=QLineEdit();self.anchor.setMaxLength(64);self.anchor.setPlaceholderText('Исходный публичный anchor; пусто = вычисляется автоматически')
        form.addRow('Seed32 / 12–24 слова',self.seed);form.addRow('Публичный anchor',self.anchor);layout.addLayout(form)
        layout.addWidget(self.label('Поддерживается 64-hex seed32 и 12/24-словная фраза TEP-0003 Rotation Mnemonic. DPAPI защищает файл под вашей Windows-учётной записью.'))
        self.import_button=QPushButton('Сохранить ключ / фразу локально…');layout.addWidget(self.import_button);self.import_button.clicked.connect(self.import_key)
        self.local_provision_button=QPushButton('Создать кошелёк локально (мгновенно, без сети)…');layout.addWidget(self.local_provision_button);self.local_provision_button.clicked.connect(self.local_provision_wallet)
        self.auto_provision_button=QPushButton('Авто-извлечение / создание кошелька в Telegram…');layout.addWidget(self.auto_provision_button);self.auto_provision_button.clicked.connect(self.auto_provision_wallet)
        layout.addWidget(self.label('2 · Получатель и ограничения'))
        policy=QFormLayout();policy.setSpacing(8);self.target=QComboBox()
        policy.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.target.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon);self.target.setMinimumContentsLength(20)
        for account in targets:
            if account['id']!=report['account_id']: self.target.addItem(account['id']+' · @'+(account.get('username') or '?'),account['id'])
        self.address=QLineEdit();self.address.setMaxLength(100);self.address.setPlaceholderText('Публичный адрес выбранного получателя; backend сверит его с Telegram')
        self.debit=QLineEdit('0.2');self.fee=QLineEdit('0.01');self.daily=QLineEdit('0.5');self.reserve=QLineEdit('0')
        for widget in (self.debit,self.fee,self.daily,self.reserve): widget.setMaxLength(18)
        for label,widget in [('Аккаунт получателя',self.target),('Разрешённый адрес',self.address),
            ('Максимум списания с комиссией',self.debit),('Потолок комиссии',self.fee),('Резерв на 24 часа',self.daily),('Оставить на исходном кошельке',self.reserve)]: policy.addRow(label,widget)
        layout.addLayout(policy)
        self.resolve_button=QPushButton('Получить адрес выбранного аккаунта…');layout.addWidget(self.resolve_button)
        self.resolve_button.clicked.connect(self.resolve)
        self.prepare_recipient=QCheckBox('Подготовить адрес в Telegram (force, не чтение)');layout.addWidget(self.prepare_recipient)
        self.prepare_recipient.toggled.connect(self.invalidate)
        self.existing=QCheckBox('Разрешаю расход существующих средств в пределах этой политики');layout.addWidget(self.existing)
        layout.addWidget(self.label('Суммы в native TON (не jetton). Максимум списания — не сумма получателю: preview покажет точную сумму и предел расходов.'))
        self.save=QPushButton('Сохранить политику…');layout.addWidget(self.save);self.save.clicked.connect(self.save_policy)
        layout.addWidget(self.label('3 · Проверить → подтвердить → отправить'))
        self.prepare=QPushButton('Подготовить и эмулировать без подписи…');layout.addWidget(self.prepare);self.prepare.clicked.connect(self.preview)
        self.preview_text=self.label('Просмотр не создаёт платёж и не использует закрытый ключ.');layout.addWidget(self.preview_text)
        self.send_button=QPushButton('Подтвердить точный перевод…');layout.addWidget(self.send_button);self.send_button.clicked.connect(self.send)
        self.send_button.setStyleSheet('QPushButton { background:'+TOKENS.primary+'; color:'+TOKENS.text+'; border:0; border-radius:8px; padding:10px; } QPushButton:disabled { background:'+TOKENS.panel_secondary+'; color:'+TOKENS.muted+'; }')
        self.message=self.label('Открытие читает только локальную конфигурацию.');layout.addWidget(self.message)
        self.journal=self.label('');layout.addWidget(self.journal)
        self.reconcile=QPushButton('Сверить выбранный незавершённый перевод…');self.intent=QComboBox()
        layout.addWidget(self.intent);layout.addWidget(self.reconcile);self.reconcile.clicked.connect(self.reconcile_intent)
        self.recover_button=QPushButton('Проверить истёкший запрос и освободить nonce…');layout.addWidget(self.recover_button)
        self.recover_button.clicked.connect(self.recover_nonce)
        self.intent.currentIndexChanged.connect(lambda *_:self.update_recovery_button())
        self.reload=QPushButton('Обновить локальный статус');layout.addWidget(self.reload);self.reload.clicked.connect(lambda:self.request('report',{}))
        for button in self.findChildren(QPushButton): button.setMinimumHeight(36)
        for widget in (self.anchor,self.address,self.debit,self.fee,self.daily,self.reserve): widget.textEdited.connect(self.invalidate)
        self.target.currentIndexChanged.connect(self.invalidate);self.existing.toggled.connect(self.invalidate)
        self.render(report,initial=True)
        self.timer=QTimer(self);self.timer.timeout.connect(self.expire_review);self.timer.start(1000)

    @staticmethod
    def label(text):
        widget=QLabel(text);widget.setTextFormat(Qt.PlainText);widget.setWordWrap(True)
        widget.setTextInteractionFlags(Qt.TextSelectableByMouse);return widget

    def phrase(self,text):
        self._confirming_dialog=True
        try:
            value,ok=QInputDialog.getText(self,'Подтверждение','Введите точно: '+text)
            return ok and value==text
        finally:
            self._confirming_dialog=False

    def local_confirm(self,action,phrase,values):
        if self.phrase(phrase): self.request(action,values)

    def import_key(self):
        if not self.phrase('IMPORT '+self.report['account_id']): self.seed.clear();return
        text=self.seed.text().strip();self.seed.clear()
        anchor_override=self.anchor.text().strip()
        seed=None
        try:
            if len(text)==64:
                try:
                    seed=bytearray.fromhex(text)
                except ValueError:
                    seed=None
            if seed is None or len(seed)!=32:
                from modules.wallet_tg_custody import parse_rotation_mnemonic, _wipe
                parsed=parse_rotation_mnemonic(text)
                seed=parsed['signing_seed']
                _wipe(parsed.get('anchor_seed'))
                if not anchor_override:
                    anchor_override=parsed['anchor_public_key']
        except Exception:
            self.message.setText('Нужен 64-символьный hex seed32 ИЛИ корректная 12/24-словная фраза восстановления BIP-39.');return
        finally:
            text=None
        try:
            self.request('import-key',{'seed':seed,'anchor':anchor_override,
                'expected_revision':(self.report.get('key') or {}).get('revision')})
        finally:
            if isinstance(seed,bytearray): seed[:]=bytes(len(seed))

    def local_provision_wallet(self):
        phrase_text=self.seed.text().strip();self.seed.clear()
        self.request('local-provision',{'phrase':phrase_text,'replace_existing':False})

    def auto_provision_wallet(self):
        phrase_text=self.seed.text().strip();self.seed.clear()
        if QMessageBox.question(
            self,
            'Авто-настройка WalletTg',
            'Подключить сессию '+self.report['account_id']+', автоматически извлечь облачную фразу (если доступно) '
            'или создать/привязать кошелёк WalletTg в Telegram и сохранить ключ в локальный DPAPI Vault?\n'
            '(При недоступности прокси/региона автоматически создаст локальный WalletTg кошелёк без ошибки.)',
            QMessageBox.Yes|QMessageBox.No,
            QMessageBox.No,
        )!=QMessageBox.Yes:
            return
        self.busy(True)
        self.message.setText('Синхронизирую / создаю кошелёк WalletTg и сохраняю ключ в DPAPI Vault…')
        self.request('auto-provision',{'phrase':phrase_text,'replace_existing':False})

    def save_policy(self):
        if not self.existing.isChecked(): self.message.setText('Нужно отдельно разрешить расход существующих средств.');return
        values={'target_id':self.target.currentData(),'target_address':self.address.text().strip(),
            'debit':self.debit.text(),'fee':self.fee.text(),'daily':self.daily.text(),'reserve':self.reserve.text(),'allow_existing':True}
        self.local_confirm('save-policy','POLICY '+self.report['account_id'],{'values':values,
            'expected_revision':(self.report.get('policy') or {}).get('revision')})

    def invalidate(self,*args):
        self.dirty=True
        self.review=None;self.send_button.setEnabled(False);self.prepare.setEnabled(False)
        self.preview_text.setText('Форма изменена. Сохраните политику и создайте новый preview.')

    def invalidate_scope(self):
        self.scope_valid=False;self.seed.clear();self.invalidate();self.busy(False)
        self.message.setText('Выделение/группа/фильтр изменились. Закройте окно и выберите исходный аккаунт заново.')

    def busy(self,value):
        self.running=value
        for widget in (self.create,self.unlock,self.lock,self.seed,self.anchor,self.import_button,self.local_provision_button,self.auto_provision_button,self.target,self.address,self.debit,self.fee,self.daily,
                       self.reserve,self.existing,self.save,self.prepare,self.reload,self.intent,self.reconcile,self.resolve_button,self.prepare_recipient):
            widget.setEnabled(not value and self.scope_valid)
        self.create.setEnabled(not value and self.scope_valid and not self.report['vault_exists'])
        self.create.setVisible(not self.report['vault_exists'])
        self.unlock.setEnabled(not value and self.scope_valid and self.report['vault_exists'] and self.report['vault_locked'])
        self.unlock.setVisible(self.report['vault_exists'] and self.report['vault_locked'])
        self.lock.setEnabled(not value and self.scope_valid and self.report['vault_exists'] and not self.report['vault_locked'])
        self.lock.setVisible(self.report['vault_exists'] and not self.report['vault_locked'])
        self.import_button.setEnabled(not value and self.scope_valid and self.report['vault_exists'] and not self.report['vault_locked'])
        self.local_provision_button.setEnabled(not value and self.scope_valid)
        self.auto_provision_button.setEnabled(not value and self.scope_valid)
        self.save.setEnabled(not value and self.scope_valid and bool(self.report.get('key')))
        self.prepare.setEnabled(not value and self.scope_valid and not self.dirty and not self.report['vault_locked'] and bool(self.report.get('policy_enabled')))
        self.send_button.setEnabled(not value and self.scope_valid and bool(self.review) and self.report['native_enabled'])
        self.reconcile.setEnabled(not value and self.scope_valid and self.intent.count()>0)
        self.update_recovery_button()

    def update_recovery_button(self):
        ident=self.intent.currentData()
        row=next((r for r in self.report['journal'] if r['id']==ident),{})
        self.recover_button.setEnabled(not self.running and self.scope_valid and row.get('native_seqno') is not None
            and row.get('expires',float('inf'))+60<time.time())

    def render(self,report,initial=False):
        self.report=report;self.review=None
        key=report.get('key');policy=report.get('policy')
        addr_lines=''
        if key and key.get('address'):
            try:
                uq=friendly_address(key['address'],'mainnet',bounceable=False)
                eq=friendly_address(key['address'],'mainnet',bounceable=True)
                addr_lines='\nАдрес (UQ, для 1-го пополнения): '+uq+'\nАдрес (EQ): '+eq+'\nSource (raw): '+key['address']
            except Exception:
                addr_lines='\nSource: '+key['address']
        self.status.setText('Vault: '+('есть' if report['vault_exists'] else 'не создан')+' · Ключ: '+('сохранён' if key else 'нет')+
            '\nNative send: '+('разрешён flags' if report['native_enabled'] else 'выключен: нужны TGB_FINANCE_ENABLE_NATIVE=1 и TGB_FINANCE_ENABLE_MAINNET=1')+
            addr_lines)
        if policy:
            targets=policy.get('targets') or []
            self.target.blockSignals(True)
            self.target.setCurrentIndex(self.target.findData(report.get('target_id')))
            self.target.blockSignals(False)
            self.address.setText(targets[0] if targets else '')
            for widget,name in ((self.debit,'transfer_debit_cap_nano'),(self.fee,'fee_cap_nano'),(self.daily,'daily_reservation_cap_nano'),(self.reserve,'reserve_nano')):
                widget.setText(format_nano(policy[name]))
        self.existing.blockSignals(True)
        self.existing.setChecked(bool(report.get('policy_enabled')))
        self.existing.blockSignals(False)
        self.dirty=False
        self.intent.clear()
        for row in report['journal']:
            if row['state'] in {'uncertain','wallet_prompted','wallet_submitted_unverified'} or row['state']=='operator_not_sent' and (row.get('native_seqno') is not None or row.get('native_stage')=='expired_unused_nonce_observed'):
                self.intent.addItem(row['id']+' · '+row['state'],row['id'])
        self.journal.setText('\n'.join(r['id']+' · '+r['state']+' · '+format_nano(r['amount']) for r in report['journal']) or 'Журнал пуст.')
        self.busy(False)

    def preview(self):
        if QMessageBox.question(self,'Read & emulate','Подключить выбранную сессию, прочитать Telegram/TON Center и выполнить unsigned-эмуляцию? Без подписи и отправки.',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)==QMessageBox.Yes:
            self.review=None;self.busy(True);self.message.setText('Читаю состояние и эмулирую без подписи. Автоматических повторов нет.');self.request('preview',{})

    def resolve(self):
        target=self.target.currentData()
        if target and self.prepare_recipient.isChecked():
            phrase='PREPARE '+self.report['account_id']+' TO '+target
            if QMessageBox.question(self,'Подготовка адреса в Telegram',
                    'Запросить force-подготовку адреса аккаунта '+target+' из '+self.report['account_id']+'? Это может изменить состояние на стороне Telegram. Нужен готовый исходный Money с положительным балансом. Без ключей, подписи и перевода; активация не подтверждается.',
                    QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes or not self.phrase(phrase): return
            if self.target.currentData()!=target or not self.prepare_recipient.isChecked(): return
            self.invalidate();self.busy(True);self.message.setText('Запрашиваю подготовку адреса в Telegram один раз. При таймауте автоматического повтора не будет…')
            self.request('prepare-recipient',{'target_id':target,'confirmation':phrase})
            return
        if target and QMessageBox.question(self,'Адрес получателя','Подключить исходную Telegram-сессию и прочитать адрес выбранного аккаунта '+target+'? Без force, извлечения ключа, подписи и перевода.',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)==QMessageBox.Yes:
            self.busy(True);self.message.setText('Читаю публичный адрес конкретного получателя…');self.request('resolve',{'target_id':target})

    def resolved(self,result):
        if result['target_id']!=self.target.currentData(): return
        self.address.setText(result['address']);self.invalidate();self.busy(False)
        self.prepare_recipient.blockSignals(True)
        self.prepare_recipient.setChecked(False)
        self.prepare_recipient.blockSignals(False)
        prefix='Запрос подготовки выполнен. ' if result.get('prepare_address_requested') else ''
        self.message.setText(prefix+'Адрес получен из Telegram. Активация, deployment и signer НЕ подтверждены. Сохраните новую политику перед отдельными preview/SEND.')

    def show_preview(self,review):
        self.review=review;limits=review['limits']
        self.preview_text.setText('От: '+review['account_id']+'\nКому: '+review['target_id']+' @'+review['target_username']+
            '\nАдрес: '+limits['destination']+'\nПолучателю (nominal): '+format_nano(limits['amount_nano'])+
            '\nМаксимум списания: '+format_nano(limits['max_debit_nano'])+'\nПотолок комиссии: '+format_nano(review['fee_cap_nano'])+
            '\nОценка provider: '+format_nano(review['estimated_fee_nano'])+' (не гарантия). Preview действует до Unix '+str(review['expires']))
        self.busy(False)
        if review.get('deployment_required'):
            self.message.setText('Первый исходящий перевод: будет добавлен StateInit исходного WalletTg. Нужны исходный ключ и особое DEPLOY AND SEND. Эмуляция не доказывает фактическое развёртывание; итог сверяется по журналу.')
        elif review.get('recipient_state')=='uninitialized_credited':
            self.message.setText('Первое пополнение: эмуляция показывает только зачисление на неразвёрнутый адрес. Активация Money и готовность к отправке НЕ подтверждены. Отправка требует отдельного подтверждения.')
        else:
            self.message.setText('Preview готов. Проверьте конкретные параметры; отправка требует отдельного подтверждения.')

    def send(self):
        self.expire_review()
        review=self.review
        if not review: return
        phrase=('DEPLOY AND SEND ' if review.get('deployment_required') else 'SEND ')+self.report['account_id']+' TO '+review['target_id']
        if not self.phrase(phrase): return
        if not self.review or self.review is not review or self.review['expires']<=time.time():
            self.expire_review();return
        token=self.review['review_token'];self.review=None;self.busy(True)
        self.message.setText('Подтверждённая операция выполняется. Не повторяйте отправку; итог потребуется сверить в журнале.')
        self.request('execute',{'token':token,'confirmation':phrase})

    def expire_review(self):
        self.update_recovery_button()
        if getattr(self,'_confirming_dialog',False): return
        if self.review and self.review['expires']<=time.time():
            self.review=None;self.send_button.setEnabled(False);self.message.setText('Preview истёк. Подготовьте новый перед отправкой.')

    def reconcile_intent(self):
        ident=self.intent.currentData()
        if ident and self.phrase('CHECK '+ident):
            self.busy(True);self.message.setText('Читаю provider evidence для выбранного intent. Новый перевод не создаётся.');self.request('reconcile',{'id':ident})

    def recover_nonce(self):
        ident=self.intent.currentData()
        if not ident or not self.recover_button.isEnabled(): return
        warning='Только после expiry + 60s: два advancing masterchain reads должны показать прежний неиспользованный nonce. Это provider report, не chain proof. Резервы сохраняются, новый перевод не запускается. Продолжить проверку?'
        if QMessageBox.question(self,'Проверка истёкшего nonce',warning,QMessageBox.Yes|QMessageBox.No,QMessageBox.No)==QMessageBox.Yes and self.phrase('RECOVER '+ident):
            self.busy(True);self.message.setText('Проверяю истёкший nonce по двум advancing chain observations. Подписи и повторной отправки нет.')
            self.request('recover',{'id':ident,'confirmation':'RECOVER '+ident})

    def reject(self):
        self.seed.clear()
        if self.running and QMessageBox.question(self,'Операция идёт','Закрытие не гарантирует отмену подписанного/отправленного запроса. Закрыть окно и остановить дальнейшие проверки scope?',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes: return
        super().reject()

    def closeEvent(self,event):
        self.seed.clear()
        if self.running and QMessageBox.question(self,'Операция идёт','Закрытие не гарантирует отмену подписанного/отправленного запроса. Закрыть окно и остановить дальнейшие проверки scope?',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:
            event.ignore();return
        super().closeEvent(event)
