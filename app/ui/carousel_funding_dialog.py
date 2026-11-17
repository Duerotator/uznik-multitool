"""Finite carousel startup instructions. No wallet access on open or copy."""
import time
import math
import re
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QFont,QColor,QPalette,QTextOption
from PySide6.QtWidgets import QApplication,QComboBox,QDialog,QFormLayout,QInputDialog,QLabel,QLineEdit,QListWidget,QMessageBox,QProgressBar,QPushButton,QScrollArea,QSizePolicy,QTextEdit,QVBoxLayout,QWidget

from core.design_tokens import TOKENS
from core.ton_address import format_nano


class WrappedInstruction(QTextEdit):
    """Selectable plain text: soft-wrap long wallet addresses without altering copy."""
    def __init__(self):
        super().__init__();self.setReadOnly(True);self.setAcceptRichText(False)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(0);self.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Fixed)
        self.setStyleSheet('QTextEdit {background:transparent;color:'+TOKENS.text+';border:0;padding:0;}')
        self.textChanged.connect(self._fit)

    def setText(self,text): self.setPlainText(text)
    def text(self): return self.toPlainText()
    def resizeEvent(self,event):
        super().resizeEvent(event);self._fit()
    def _fit(self):
        self.document().setTextWidth(max(1,self.viewport().width()))
        height=max(self.fontMetrics().lineSpacing()*2,math.ceil(self.document().size().height())+4)
        if self.height()!=height: self.setFixedHeight(height)


class CarouselFundingDialog(QDialog):
    def __init__(self,accounts,selected,request,report=None,parent=None):
        super().__init__(parent)
        self.accounts={a['id']:a for a in accounts};self.selected=list(selected);self.request=request
        self.report=None;self.review=None;self.copy_snapshot=None;self.execution_started=False;self.execution_state=None;self.plan_id=None;self.valid=True;self.running=False
        self.setWindowTitle('Money · старт карусели');self.setMinimumSize(480,480);self.setFont(QFont('Segoe UI',10))
        available=self.screen().availableGeometry();self.resize(min(740,available.width()-48),min(880,available.height()-64))
        palette=self.palette()
        for role,color in ((QPalette.Window,TOKENS.panel),(QPalette.WindowText,TOKENS.text),(QPalette.Base,TOKENS.background),(QPalette.Text,TOKENS.text),
                (QPalette.Button,TOKENS.panel_secondary),(QPalette.ButtonText,TOKENS.text),(QPalette.PlaceholderText,TOKENS.muted)):
            palette.setColor(role,QColor(color))
        self.setPalette(palette)
        self.setStyleSheet(
            'QDialog {background:'+TOKENS.panel+';color:'+TOKENS.text+';}'
            'QWidget#carouselContent,QScrollArea {background:'+TOKENS.panel+';border:0;}'
            'QLabel {color:'+TOKENS.text+';}'
            'QListWidget {background:'+TOKENS.background+';color:'+TOKENS.text+';border:1px solid '+TOKENS.border+';border-radius:8px;}'
            'QListWidget::item {padding:5px;}'
            'QComboBox,QPushButton {background:'+TOKENS.panel_secondary+';color:'+TOKENS.text+';border:1px solid '+TOKENS.border+';border-radius:8px;padding:8px;}'
            'QComboBox QAbstractItemView {background:'+TOKENS.panel_secondary+';color:'+TOKENS.text+';selection-background-color:'+TOKENS.primary+';}'
            'QComboBox:disabled,QPushButton:disabled {color:'+TOKENS.muted+';}'
            'QLineEdit {background:'+TOKENS.background+';color:'+TOKENS.text+';border:1px solid '+TOKENS.border+';border-radius:8px;padding:8px;}'
            'QProgressBar {background:'+TOKENS.background+';color:'+TOKENS.text+';border:1px solid '+TOKENS.border+';border-radius:5px;text-align:center;}'
            'QProgressBar::chunk {background:'+TOKENS.primary+';}'
            'QScrollBar:vertical {background:'+TOKENS.background+';width:10px;}'
            'QScrollBar::handle:vertical {background:'+TOKENS.border+';min-height:24px;border-radius:4px;}'
            'QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}'
            'QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:none;}'
        )
        footer=QVBoxLayout(self);footer.setContentsMargins(16,16,16,16);footer.setSpacing(12)
        self.scroll=QScrollArea();self.scroll.setWidgetResizable(True);self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content=QWidget();content.setObjectName('carouselContent');self.scroll.setWidget(content);footer.addWidget(self.scroll,1)
        layout=QVBoxLayout(content);layout.setContentsMargins(8,8,8,8);layout.setSpacing(12)
        layout.addWidget(self.label('Карусель · маршрут и первое пополнение'))
        layout.addWidget(self.label('Сначала зафиксируйте цепочку. Затем пополните первый аккаунт в Telegram и явно запустите проверку поступления. Само нажатие не подтверждает оплату.'))
        form=QFormLayout();form.setRowWrapPolicy(QFormLayout.WrapLongRows);self.first=QComboBox();self.last=QComboBox()
        for combo in (self.first,self.last):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon);combo.setMinimumContentsLength(20)
        for ident in selected:
            a=self.accounts[ident];self.first.addItem('@'+(a['username'] or '?'),ident)
            self.first.setItemData(self.first.count()-1,ident,Qt.ToolTipRole)
        for a in accounts:
            self.last.addItem('@'+(a['username'] or '?'),a['id']);self.last.setItemData(self.last.count()-1,a['id'],Qt.ToolTipRole)
        self.last.setCurrentIndex(self.last.findData(selected[0]))
        form.addRow('Первый аккаунт A',self.first);form.addRow('Конечный аккаунт X',self.last);layout.addLayout(form)
        self.route=QListWidget();self.route.setWordWrap(True);self.route.setTextElideMode(Qt.ElideNone)
        self.route.setMinimumHeight(140);self.route.setMaximumHeight(260);layout.addWidget(self.route,1)
        self.instruction=WrappedInstruction();layout.addWidget(self.instruction)
        self.copy=QPushButton('Копировать @ для пополнения');self.copy.setEnabled(False);layout.addWidget(self.copy)
        self.copy.clicked.connect(self.copy_username)
        self._has_uq=any(a.get('uq_address') for a in accounts)
        if self._has_uq:
            self.copy_uq=QPushButton('Копировать UQ-адрес 1-го аккаунта (TON)')
            self.copy_uq.clicked.connect(self.copy_uq_address)
            layout.addWidget(self.copy_uq)
        layout.addWidget(self.label('Хеш пополнения — необязательно.\nНужен, если поступлений несколько.'))
        self.tx=QLineEdit();self.tx.setPlaceholderText('Хеш транзакции');self.tx.setToolTip('Необязательный хеш пополнения: 64 символа');self.tx.setMaxLength(64);layout.addWidget(self.tx)
        budgets=QFormLayout();budgets.setRowWrapPolicy(QFormLayout.WrapLongRows);budgets.setLabelAlignment(Qt.AlignLeft|Qt.AlignVCenter);self.limits={};self.limit_labels={}
        budget_rows=(('fee','Потолок комиссий на шаг\n(заполняется автоматически)','0.05'),('debit','Максимальный расход на шаг\n(заполняется автоматически)','3'),('daily','Суточный потолок отправителя\n(заполняется автоматически)','10'))
        label_width=max(160,max(self.fontMetrics().horizontalAdvance(line) for _,title,_ in budget_rows for line in title.splitlines())+8)
        for key,title,value in budget_rows:
            label=self.label(title);label.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Preferred)
            label.setFixedWidth(label_width);self.limit_labels[key]=label
            field=QLineEdit(value);field.setMaxLength(24);self.limits[key]=field;budgets.addRow(label,field)
            field.textChanged.connect(self.clear_review)
        layout.addLayout(budgets)
        self.progress=QProgressBar();self.progress.setRange(0,1);self.progress.setValue(0);layout.addWidget(self.progress)
        self.message=self.label('Лимиты ниже — ваши потолки, не оценка комиссий. После пополнения подготовьте отдельный review; нужны настроенные финансовые ключи отправителей.');layout.addWidget(self.message)
        self.provision_btn=QPushButton('Авто-настройка ключей маршрута в Telegram…');self.provision_btn.setEnabled(False);layout.addWidget(self.provision_btn)
        self.provision_btn.clicked.connect(self.auto_provision_route)
        self.deploy_btn=QPushButton('Развернуть uninit-кошелёк маршрута (StateInit)…');self.deploy_btn.setEnabled(False);layout.addWidget(self.deploy_btn)
        self.deploy_btn.clicked.connect(self.deploy_uninit_route)
        self.resume_btn=QPushButton('Продолжить прерванную карусель (Resume)…');self.resume_btn.setEnabled(False);layout.addWidget(self.resume_btn)
        self.resume_btn.clicked.connect(self.resume_execution)
        self.primary=QPushButton('Зафиксировать маршрут');footer.addWidget(self.primary);self.primary.setMinimumHeight(52)
        self.primary.setStyleSheet('QPushButton {background:'+TOKENS.primary+';color:'+TOKENS.text+';border-radius:8px;padding:10px;} QPushButton:disabled {background:'+TOKENS.panel_secondary+';}')
        self.primary.clicked.connect(self.act)
        self.cancel=QPushButton('Закрыть инструкцию\n(без движения средств)');footer.addWidget(self.cancel);self.cancel.setMinimumHeight(52);self.cancel.clicked.connect(self.cancel_draft)
        self.first.currentIndexChanged.connect(self.render_route);self.last.currentIndexChanged.connect(self.render_route)
        self.render(report) if report else self.render_route()
        self._review_timer=QTimer(self);self._review_timer.timeout.connect(self.expire_review);self._review_timer.start(1000)

    @staticmethod
    def label(text):
        label=QLabel(text);label.setTextFormat(Qt.PlainText);label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred);return label

    def ids(self):
        first,last=self.first.currentData(),self.last.currentData()
        ordered=[first]+[i for i in self.selected if i!=first]
        return ordered+[last] if last==first else [i for i in ordered if i!=last]+[last]

    def render_route(self,*args):
        if self.report: return
        self.route.clear()
        route_ids=self.ids()
        for index,ident in enumerate(route_ids):
            u=self.accounts[ident].get('username') or '?'
            uq=self.accounts[ident].get('uq_address')
            suffix=('\nUQ: '+uq) if uq else ''
            self.route.addItem(str(index+1)+'. @'+u+'\nID: '+ident+suffix)
        if getattr(self,'_has_uq',False) and route_ids:
            first_a=self.accounts[route_ids[0]]
            no_u=sum(1 for ident in self.selected if not self.accounts.get(ident,{}).get('username'))
            extra=f' (пропущено без @username: {no_u} акк.)' if no_u else ''
            self.instruction.setText(
                f'Маршрут: {len(self.selected)} акк. с @username → {len(route_ids)} точек с возвратом на A{extra}.\n'
                f'Сначала нажмите внизу «Зафиксировать маршрут»! После фиксации станет активна кнопка копирования @{first_a.get("username") or "?"}'
                + (f'\nUQ-адрес 1-го аккаунта: {first_a["uq_address"]}' if first_a.get('uq_address') else '')
            )
        else:
            self.instruction.setText('После фиксации появится точный @ первого аккаунта для пополнения.')

    def render(self,report):
        self.report=report;self.review=None;self.copy_snapshot=None;self.route.clear()
        if report.get('linked_intent'): self.execution_started=True
        if report['state']=='cancelled': self.execution_started=False
        for index,node in enumerate(report['route']):
            uq=self.accounts.get(node['id'],{}).get('uq_address')
            suffix=('\nUQ: '+uq) if uq else ''
            self.route.addItem(str(index+1)+'. @'+node['username']+'\nID: '+node['id']+suffix)
        first_uq=self.accounts.get(report['route'][0]['id'],{}).get('uq_address') if report.get('route') else None
        uq_line=('\nЛокальный UQ-адрес 1-го аккаунта: '+first_uq) if first_uq and not report.get('address') else ''
        self.instruction.setText(('Пополнение связано с intent '+report['linked_intent']+'. Не пополняйте повторно; проверьте исходный журнал Money.' if report.get('linked_intent') else
            'Инструкция устарела: аккаунты/группа/фильтр изменились. Не переводите по старому @. Закройте эту инструкцию и создайте новую.' if report.get('stale_context') else
            'После ошибки не пополняйте аккаунт повторно до сверки.' if report.get('stale') else
            'Пополнить в Telegram: '+report['fund_to']+'\nID получателя: '+str(report['fund_user_id']))+
            ('\nАдрес, сообщённый Telegram: '+report['address'] if report.get('address') else uq_line+'\nАдрес будет сверяться после поступления. Проверьте получателя в Telegram.'))
        event=report.get('observation')
        if report['state']=='cancelled':
            self.instruction.setText('Инструкция закрыта. Не пополняйте по ней. Средства не перемещались.')
            self.message.setText('Для нового маршрута откройте окно заново.')
        elif event:
            self._clean_limits()
            self.message.setText('Провайдер сообщает о поступлении: '+format_nano(event['net_credit_nano'])+' native после комиссии.\n'+
                ('Срез устарел, нужна повторная сверка.\n' if report.get('stale') else '')+
                'Нажмите внизу «Подготовить запуск карусели», затем «Запустить карусель» и введите ok.')
        elif report.get('error'): self.message.setText('Проверка не завершена: '+report['error']+'. Автоматического повтора и перевода нет.')
        else: self.message.setText('Ожидается пополнение. Нажмите кнопку после перевода: подключится только первый аккаунт и read-only проверка. Сумма с запасом на комиссии; бюджет цепочки пока не рассчитан.')
        self.busy(False)

    def busy(self,value):
        self.running=value;enabled=self.valid and not value
        self.first.setEnabled(enabled and self.report is None);self.last.setEnabled(enabled and self.report is None)
        self.tx.setEnabled(enabled and self.report is not None and not self.execution_started and not self.review)
        for field in self.limits.values(): field.setEnabled(enabled and not self.execution_started)
        inspection=self.execution_started and self.report is not None and self.report['state']!='cancelled'
        self.primary.setEnabled(enabled and (inspection or not self.execution_started and (self.report is None or self.report['state']!='cancelled' and not self.report.get('stale_context') and not self.report.get('linked_intent')) and (not self.review or self.review['enabled'])))
        self.copy.setEnabled(enabled and (self.copy_snapshot is not None or not self.execution_started and not self.review and self.report is not None and self.report['state']=='awaiting_funding' and not self.report.get('stale')))
        self.copy.setText(
            'Копировать отчёт маршрута' if self.copy_snapshot is not None else
            ('Копировать @ для пополнения ('+str(self.report.get('fund_to') or '')+')' if self.report and self.report.get('fund_to') else
             'Копировать @ для пополнения (сначала нажмите «Зафиксировать маршрут» ↓)' if getattr(self,'_has_uq',False) and self.report is None else
             'Копировать @ для пополнения')
        )
        if hasattr(self,'copy_uq'):
            self.copy_uq.setEnabled(enabled and not self.execution_started)
        self.cancel.setEnabled(self.valid and value or enabled and (not self.execution_started or self.execution_state=='completed') and self.report is not None and self.report['state']!='cancelled')
        self.cancel.setText('Остановить операцию' if value else 'Закрыть инструкцию\n(без движения средств)')
        if hasattr(self,'provision_btn'):
            self.provision_btn.setEnabled(enabled and self.report is not None and self.report['state']!='cancelled' and not self.execution_started)
        if hasattr(self,'deploy_btn'):
            self.deploy_btn.setEnabled(enabled and self.report is not None and self.report['state']!='cancelled')
        if hasattr(self,'resume_btn'):
            self.resume_btn.setEnabled(enabled and self.execution_started and self.execution_state in {'paused','interrupted','needs_review'} and bool(self.plan_id))
        self.primary.setText('Показать состояние\nмаршрута' if inspection else 'Зафиксировать маршрут' if self.report is None else 'Инструкция закрыта' if self.report['state']=='cancelled' else 'Запустить карусель' if self.review else
            'Подготовить запуск\nкарусели' if self.report['state']=='funding_observed' and not self.report.get('stale') else 'Средства переведены\nПроверить поступление')

    def clear_review(self,*args):
        if self.review and not self.running:
            self.review=None;self.message.setText('Лимиты изменены — подготовьте новый review. Старое подтверждение не используется.');self.busy(False)

    def _clean_limits(self):
        net_int=0
        try:
            obs=((self.report or {}).get('observation') or {})
            net_int=int(obs.get('net_credit_nano') or 0)
        except Exception:
            net_int=0
        default_debit=max(3,math.ceil(net_int/10**9)+1) if net_int>0 else 3
        default_daily=max(10,default_debit*2)
        defaults={'fee':'0.05','debit':str(default_debit),'daily':str(default_daily)}
        cleaned={}
        for key,default_val in defaults.items():
            raw=(self.limits[key].text() if key in self.limits else '').strip().replace(',','.')
            m=re.search(r'(0|[1-9][0-9]{0,8})(?:\.([0-9]+))?',raw)
            if m and raw and not re.search(r'[eE\-]',raw):
                whole=m.group(1)
                frac=(m.group(2) or '')[:9].rstrip('0')
                val=f'{whole}.{frac}' if frac else whole
                try:
                    if float(val)<=0: val=default_val
                except Exception:
                    val=default_val
            else:
                val=default_val
            cleaned[key]=val
        try:
            fee_f=float(cleaned['fee'])
            debit_f=float(cleaned['debit'])
            daily_f=float(cleaned['daily'])
            min_debit=(net_int/10**9) if net_int>0 else 0.1
            if fee_f<=0 or fee_f>=1:
                fee_f=0.05;cleaned['fee']='0.05'
            if debit_f<=fee_f or debit_f<min_debit:
                debit_f=float(max(default_debit,math.ceil(fee_f)+1))
                cleaned['debit']=str(int(debit_f))
            if daily_f<debit_f:
                daily_f=float(max(default_daily,math.ceil(debit_f)*2))
                cleaned['daily']=str(int(daily_f))
        except Exception:
            cleaned=dict(defaults)
        for key,val in cleaned.items():
            if key in self.limits and self.limits[key].text()!=val:
                self.limits[key].blockSignals(True)
                self.limits[key].setText(val)
                self.limits[key].blockSignals(False)
        return cleaned

    def expire_review(self):
        if getattr(self,'_confirming_dialog',False): return
        if self.review and not self.running and time.time()>=self.review['expires']:
            self.clear_review();self.message.setText('Review истёк. Подготовьте маршрут заново; деньги не отправлялись.')

    def render_review(self,review):
        self.review=review;self.route.clear()
        self._show_limits({key:review[field] for key,field in {'fee':'fee_cap_nano','debit':'debit_cap_nano','daily':'daily_cap_nano'}.items()})
        for i,node in enumerate(review['route']): self.route.addItem(str(i+1)+'. @'+(self.accounts[node['account_id']]['username'] or '?')+'\nID: '+node['account_id']+'\n'+node['address'])
        self.instruction.setText('Пополнение проверено. Не переводите повторно: перед запуском проверьте адреса всего маршрута и конечный X.')
        self.message.setText('К запуску: '+format_nano(review['net_nano'])+' native; X = '+review['route'][-1]['account_id']+
            '. Потолок комиссий/шаг '+format_nano(review['fee_cap_nano'])+' (НЕ оценка). Review 180 секунд. Будут реальные последовательные переводы; неизвестный исход остановит цепочку.\n'+
            ('' if review['enabled'] else 'Запуск выключен: требуются TGB_FINANCE_ENABLE_NATIVE=1 и TGB_FINANCE_ENABLE_MAINNET=1.'))
        self.progress.setRange(0,len(review['route'])-1);self.progress.setValue(0);self.busy(False)
        self.show_status()

    def show_status(self):
        QTimer.singleShot(0,self,lambda:self.scroll.ensureWidgetVisible(self.message,0,8))

    def _show_limits(self,limits):
        for key,field in self.limits.items():
            field.blockSignals(True);field.setText(format_nano(limits[key]));field.blockSignals(False)

    def render_progress(self,event):
        if event.get('plan_id'): self.plan_id=event['plan_id']
        self.execution_started=True;self.busy(True)
        total=event.get('total',self.progress.maximum());self.progress.setRange(0,total)
        done=event.get('ordinal',0)+(1 if event.get('phase')=='delivered' else 0);self.progress.setValue(done)
        self.instruction.setText('Карусель выполняется. Не пополняйте повторно. Stop/закрытие окна остановят последующие шаги, но не отменят отправленный перевод.')
        self.message.setText(str(done)+' / '+str(total)+' · '+event.get('phase','')+(' · intent '+event['intent_id'] if event.get('intent_id') else ''))

    def render_done(self,result=None,error=None):
        self.execution_state=result.get('state') if result else 'needs_review'
        if result: self.plan_id=result['id']
        self.execution_started=True;self.busy(False)
        if result and result.get('state')=='completed':
            self.progress.setValue(self.progress.maximum());self.message.setText('Маршрут завершён по данным провайдера. Это не chain proof/активация всех кошельков. Plan '+result['id'])
        else: self.message.setText('Карусель остановлена. Не запускайте повторно и не пополняйте вслепую. Проверьте исходные intents. '+(error or '')+(' · plan '+self.plan_id if self.plan_id else ''))

    def render_execution(self,report):
        self.plan_id=report['id'];self.execution_started=True;self.execution_state=report['state'];self.review=None;self.route.clear()
        for hop in report['hops']:
            self.route.addItem(str(hop['ordinal']+1)+'. '+hop['account_id']+' → '+hop['destination_account_id']+
                '\n'+hop['journal_state']+' · intent '+(hop['intent_id'] or 'ещё не создан')+
                ('\nnet '+format_nano(hop['recipient_net_nano'])+' native' if hop.get('recipient_net_nano') else ''))
        self.progress.setRange(0,len(report['hops']));self.progress.setValue(sum(1 for h in report['hops'] if h['delivered']))
        self.instruction.setText('Plan '+report['id']+' · '+report['state']+'. Это чтение исходного журнала, не Resume/повторная отправка.'+
            (' Контекст изменён: показан архив, запуск не разрешён.' if report.get('archived_context') else ''))
        self.message.setText('После неизвестного исхода не пополняйте повторно. ID каждого original intent показан выше; сверка через Money native local. Просмотр не запрашивает сеть/ключи.')
        approval=report.get('approval')
        if approval:
            limits=approval['limits']
            self._show_limits(limits)
            self.message.setText(self.message.text()+'\nУтверждённые потолки native: fee/шаг '+format_nano(limits['fee'])+
                ', debit/шаг '+format_nano(limits['debit'])+', daily/source '+format_nano(limits['daily'])+'. Не оценка комиссии.')
        else:
            for field in self.limits.values():
                field.blockSignals(True);field.setText('неизвестно');field.blockSignals(False)
            self.message.setText(self.message.text()+'\nВ старом review лимиты не сохранены; значения по умолчанию не подставляются.')
        self.copy_snapshot=self.instruction.text()+'\n'+'\n'.join(self.route.item(i).text() for i in range(self.route.count()))+'\n'+self.message.text()
        self.busy(False)

    def invalidate_scope(self):
        self.valid=False;self.busy(False);self.instruction.setText('Выделение/группа/фильтр изменились. Не пополняйте старый @ по этой инструкции. Откройте её заново.')

    def copy_username(self):
        if self.valid and self.report and self.copy.isEnabled():
            target=self.copy_snapshot if self.copy_snapshot is not None else self.report['fund_to']
            QApplication.clipboard().setText(target)
            if self.copy_snapshot is None and target:
                self.copy.setText('✓ Скопировано в буфер: '+str(target))

    def copy_uq_address(self):
        if not self.valid: return
        first_id=self.report['route'][0]['id'] if self.report and self.report.get('route') else (self.ids()[0] if self.ids() else None)
        uq=self.accounts.get(first_id,{}).get('uq_address') if first_id else None
        if uq:
            QApplication.clipboard().setText(uq)
            if hasattr(self,'copy_uq'):
                self.copy_uq.setText('✓ UQ-адрес скопирован: '+uq)

    def act(self):
        if not self.valid or self.running: return
        if self.execution_started:
            self.request('inspect',{'id':self.report['id']});return
        if self.review and time.time()>=self.review['expires']:
            self.expire_review();return
        if self.report is None:
            if QMessageBox.question(self,'Маршрут','Зафиксировать показанный порядок аккаунтов? Сеть, кошельки и деньги не используются.',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes: return
            self.busy(True);self.request('create',{'ids':self.ids()})
        elif self.review:
            reviewed=dict(self.review)
            first,last=reviewed['route'][0]['account_id'],reviewed['route'][-1]['account_id']
            prompt=('Реальные переводы · mainnet · шагов: '+str(len(reviewed['route'])-1)+
                '\nA: @'+(self.accounts[first]['username'] or '?')+'\nX: @'+(self.accounts[last]['username'] or '?')+
                '\nПополнение: '+format_nano(reviewed['net_nano'])+' native'+
                '\nПотолок комиссий на шаг: '+format_nano(reviewed['fee_cap_nano'])+
                '\nРасход на шаг: '+format_nano(reviewed['debit_cap_nano'])+
                '\nСуточный потолок: '+format_nano(reviewed['daily_cap_nano'])+
                '\n\nПроверьте маршрут. Для запуска введите ok:')
            self._confirming_dialog=True
            try:
                phrase,ok=QInputDialog.getText(self,'Реальные переводы',prompt,text='ok')
            finally:
                self._confirming_dialog=False
            if not ok: return
            cleaned_phrase=(phrase or '').strip()
            if cleaned_phrase.lower() not in {'ok','ок','yes','да',reviewed['confirmation'].lower()}:
                QMessageBox.warning(self,'Подтверждение запуска','Для запуска карусели введите ok')
                return
            if not self.valid or self.review is None or self.review['token']!=reviewed['token'] or time.time()>=reviewed['expires']:
                self.expire_review();return
            token=reviewed['token'];self.execution_started=True;self.busy(True)
            self.request('run',{'token':token,'confirmation':reviewed['confirmation']})
        elif self.report['state']=='funding_observed' and not self.report.get('stale'):
            if QMessageBox.question(self,'Подготовить маршрут','Проверить локальные ключи/лимиты и подготовить маршрут к запуску? Без подписи/перевода.',QMessageBox.Yes|QMessageBox.No,QMessageBox.Yes)!=QMessageBox.Yes: return
            cleaned_limits=self._clean_limits()
            self.busy(True);self.message.setText('Проверяю локальные ключи и лимиты. Подписи/перевода нет.')
            self.request('review',{'reference':{k:self.report[k] for k in ('id','revision')},'limits':cleaned_limits})
        else:
            if QMessageBox.question(self,'Проверить поступление','Подключить только первый аккаунт и прочитать Telegram/TON Center? Без подписи, перевода и автоповторов. Нажатие не является подтверждением оплаты.',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes: return
            self.busy(True);self.message.setText('Проверяю первый аккаунт и поступление, без подписи/перевода. Общий таймаут до 65 секунд; Stop прерывает ожидание. Автоматических повторов нет.')
            self.request('check',{'id':self.report['id'],'revision':self.report['revision'],'tx_hash':self.tx.text().strip() or None})

    def cancel_draft(self):
        if self.valid and self.running:
            self.request('stop',{});return
        if not self.valid or self.running or not self.report: return
        if QMessageBox.question(self,'Закрыть инструкцию','Закрыть только эту инструкцию? Внесённые средства останутся на аккаунте. Это не возврат и не отмена Telegram-перевода.',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes: return
        self.busy(True);self.request('cancel',{'id':self.report['id'],'revision':self.report['revision'],'confirmation':'CANCEL '+self.report['id']})

    def auto_provision_route(self):
        if not self.valid or self.running or not self.report: return
        if QMessageBox.question(
            self,
            'Авто-настройка ключей маршрута',
            'Автоматически извлечь или создать кошельки WalletTg в Telegram и сохранить ключи в локальный DPAPI Vault для всех участников маршрута?',
            QMessageBox.Yes|QMessageBox.No,
            QMessageBox.No,
        )!=QMessageBox.Yes:
            return
        self.busy(True)
        self.message.setText('Синхронизирую и создаю кошельки WalletTg в Telegram для участников маршрута…')
        self.request('provision-route',{'reference':{k:self.report[k] for k in ('id','revision')}})

    def deploy_uninit_route(self):
        if not self.valid or self.running or not self.report: return
        if QMessageBox.question(
            self,
            'Развёртывание uninit WalletTg',
            'Отправить стартовую транзакцию с StateInit для активации неразвёрнутого (seqno=0) кошелька маршрута?\n(Примечание: при обычном запуске карусели все uninit-кошельки активируются автоматически на каждом шаге).',
            QMessageBox.Yes|QMessageBox.No,
            QMessageBox.No,
        )!=QMessageBox.Yes:
            return
        cleaned_limits=self._clean_limits()
        self.busy(True)
        self.message.setText('Разворачиваю неактивированный кошелёк WalletTg в блокчейне (StateInit)…')
        self.request('deploy-uninit',{'reference':{k:self.report[k] for k in ('id','revision')},'limits':cleaned_limits})

    def resume_execution(self):
        if not self.valid or self.running or not self.report or not self.plan_id: return
        expected='RESUME '+self.plan_id
        phrase,ok=QInputDialog.getText(self,'Финансовый Resume','Для сверки незавершённого шага и продолжения оставшихся переводов введите ok:',text='ok')
        if not ok: return
        if (phrase or '').strip().lower() not in {'ok','ок','yes','да',expected.lower()}:
            QMessageBox.warning(self,'Подтверждение Resume','Для продолжения маршрута введите ok')
            return
        self.busy(True)
        self.message.setText('Сверяю состояние текущего шага по блокчейну и продолжаю маршрут с контрольной точки…')
        self.request('resume',{'id':self.report['id'],'plan_id':self.plan_id,'confirmation':expected})

    def reject(self):
        if self.running and QMessageBox.question(
            self, 'Операция идёт',
            'Карусель выполняется. Остановить следующие шаги и закрыть окно? Уже отправленный перевод не отменяется.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        if self.running:
            self.request('stop', {})
        super().reject()

    def closeEvent(self, event):
        if self.running and QMessageBox.question(
            self, 'Операция идёт',
            'Карусель выполняется. Остановить следующие шаги и закрыть окно? Уже отправленный перевод не отменяется.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            event.ignore()
            return
        if self.running:
            self.request('stop', {})
        super().closeEvent(event)
