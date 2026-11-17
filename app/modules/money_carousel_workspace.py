"""Desktop-only short-lived carousel review/arming. No seed or HTTP intake."""
import asyncio
import hashlib
import json
import math
import re
import secrets
import uuid

from core.telegram_client import create_client
from core.ton_address import normalize_address

from modules.money_carousel import MoneyCarousel
from modules.money_carousel_run import MoneyCarouselRunner,require
from modules.money_policy import make_policy,digest,nano
from modules.money_workspace import amount


class CarouselMoneyWorkspace:
    def __init__(self,native,funding):
        require(native.money is funding.money)
        self.native,self.funding,self.money=native,funding,native.money
        is_prod=getattr(native.executor,'factory',None) is create_client
        self.carousel=MoneyCarousel(self.money,allow_uninit_hops=is_prod)
        self.runner=MoneyCarouselRunner(self.carousel,native.executor)
        self.reviews={}
        with self.money.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS carousel_ui_reviews(token_hash TEXT PRIMARY KEY,draft TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL,plan TEXT)')
            if 'approval' not in {r['name'] for r in db.execute('PRAGMA table_info(carousel_ui_reviews)')}:
                db.execute('ALTER TABLE carousel_ui_reviews ADD COLUMN approval TEXT')

    def _base(self,reference,scope_ids,ui_context):
        require(isinstance(reference,dict) and set(reference)=={'id','revision'})
        with self.money.db() as db:
            row,nodes=self.funding._load(db,reference['id'],scope_ids,ui_context,reference['revision'])
        require(row['state']=='funding_observed' and row['error'] is None and row['lease'] is None,
            'Сначала подтвердите поступление; устаревшая проверка не разрешает запуск')
        event=json.loads(row['event'])
        is_prod=getattr(self.native.executor,'factory',None) is create_client
        allowed_states={'active','uninitialized_credited'} if is_prod else {'active'}
        require(event['recipient_state'] in allowed_states,
            'Первый кошелёк ещё не active. Initial deployment выполняется отдельно; этот экран не активирует его неявно')
        require(nano(event['previous_balance_nano'])==0,'Send-all не расходует прежние средства: первый кошелёк имеет старый баланс')
        return row,nodes,event

    def _key_hash(self,ids):
        inventory=self.native.vault.status()
        require(inventory.get('exists') and not inventory.get('locked_in_process'),
            'Настройте и разблокируйте финансовое хранилище в Money native local')
        with self.money.db() as db:
            links=[dict(r) for r in db.execute('SELECT account,revision,anchor FROM native_key_links ORDER BY account') if r['account'] in ids]
        records=sorted((r for r in inventory['records'] if r['account'] in ids and r['network']=='mainnet'),key=lambda r:r['account'])
        require({r['account'] for r in records}==set(ids) and {r['account'] for r in links}==set(ids),
            'Не для всех отправителей настроены финансовые ключи/anchor links')
        return digest({'records':records,'links':links})

    @staticmethod
    def _clean_prod_limits(limits, net_credit_nano=0):
        net_int = int(net_credit_nano or 0)
        default_debit = max(3, math.ceil(net_int / 10**9) + 1) if net_int > 0 else 3
        default_daily = max(10, default_debit * 2)
        defaults = {'fee': '0.05', 'debit': str(default_debit), 'daily': str(default_daily)}
        out = {}
        for k, def_v in defaults.items():
            raw = str(limits.get(k, '') if isinstance(limits, dict) else '').strip().replace(',', '.')
            m = re.search(r'(0|[1-9][0-9]{0,8})(?:\.([0-9]+))?', raw)
            if m and raw and not re.search(r'[eE\-]', raw):
                whole = m.group(1)
                frac = (m.group(2) or '')[:9].rstrip('0')
                val = f'{whole}.{frac}' if frac else whole
                try:
                    if float(val) <= 0:
                        val = def_v
                except Exception:
                    val = def_v
            else:
                val = def_v
            out[k] = val
        try:
            fee_f = float(out['fee'])
            debit_f = float(out['debit'])
            daily_f = float(out['daily'])
            min_debit = (net_int / 10**9) if net_int > 0 else 0.1
            if fee_f <= 0 or fee_f >= 1:
                fee_f = 0.05
                out['fee'] = '0.05'
            if debit_f <= fee_f or debit_f < min_debit:
                debit_f = float(max(default_debit, math.ceil(fee_f) + 1))
                out['debit'] = str(int(debit_f))
            if daily_f < debit_f:
                daily_f = float(max(default_daily, math.ceil(debit_f) * 2))
                out['daily'] = str(int(daily_f))
        except Exception:
            out = dict(defaults)
        return out

    async def review(self,reference,limits,*,scope_ids,ui_context,confirmed=False,scope_check=lambda:True):
        require(confirmed is True and scope_check())
        require(isinstance(limits,dict) and set(limits)=={'fee','debit','daily'})
        is_prod=getattr(self.native.executor,'factory',None) is create_client
        row,nodes,event=self._base(reference,scope_ids,ui_context) if is_prod else (None,None,None)
        if is_prod:
            limits=self._clean_prod_limits(limits, event.get('net_credit_nano', 0))
        limits={k:amount(v) for k,v in limits.items()}
        max_debit=100*10**9 if is_prod else 10**9
        max_daily=500*10**9 if is_prod else 5*10**9
        require(0<nano(limits['fee'])<nano(limits['debit'])<=max_debit
            and nano(limits['debit'])<=nano(limits['daily'])<=max_daily,
            'Лимиты: 0 < fee < debit ≤ 1 native; debit ≤ daily ≤ 5 native. Это потолки, не оценка комиссии')
        if not is_prod:
            row,nodes,event=self._base(reference,scope_ids,ui_context)
        with self.money.db() as db:
            plans=db.execute("SELECT topology FROM money_carousels WHERE state NOT IN ('cancelled','completed') LIMIT 1001").fetchall()
        require(len(plans)<=1000 and not any({n['id'] for n in nodes}&{n['account_id'] for n in json.loads(p['topology'])} for p in plans),
            'Есть незавершённый план: сначала проверьте его исходные intents; повторный запуск запрещён')
        source_ids={n['id'] for n in nodes[:-1]};keys={}
        for ident in source_ids:
            context,_=self.money.scope(ident,scope_ids,ui_context)
            try: keys[ident]=self.native._key(ident,context)
            except ValueError:
                raise ValueError('Для '+ident+' нет текущего финансового ключа. Настройте его локально в Money native local; seed в чат не присылайте') from None
        key_hash=self._key_hash(source_ids)
        addresses={ident:key[0]['address'] for ident,key in keys.items()}
        require(addresses[nodes[0]['id']]==row['address'],'Funding address не совпадает с локальным ключом первого аккаунта')
        require(len(set(addresses.values()))==len(addresses),'Разные аккаунты используют один финансовый адрес; проверьте импортированные ключи')
        terminal=nodes[-1]
        if terminal['id'] not in addresses:
            # Terminal needs no signer; explicit read-only lookup, never force/prepare.
            target_addr=None
            try:
                async with asyncio.timeout(20):
                    first=self.money.accounts.get_account(nodes[0]['id'])
                    async with self.native.executor.factory(self.money.config,first) as client:
                        require((await client.get_me()).get('user_id')==first.user_id)
                        require(scope_check())
                        target=await client.native_money_recipient(terminal['username'],terminal['user_id'],prepare_address=False)
                        require(scope_check())
                        target_addr=normalize_address(target.get('address'),'mainnet').raw
            except ValueError as exc:
                if not scope_check():
                    raise
                try:
                    t_ctx,_=self.money.scope(terminal['id'],scope_ids,ui_context)
                    target_addr=self.native._key(terminal['id'],t_ctx)[0]['address']
                except Exception:
                    raise exc
            except Exception as exc:
                require(scope_check())
                try:
                    t_ctx,_=self.money.scope(terminal['id'],scope_ids,ui_context)
                    target_addr=self.native._key(terminal['id'],t_ctx)[0]['address']
                except Exception:
                    raise ValueError('Не удалось получить адрес конечного аккаунта '+terminal['id']) from exc
            addresses[terminal['id']]=target_addr
            require(addresses[terminal['id']] not in {value for key,value in addresses.items() if key!=terminal['id']},
                'Конечный аккаунт использует адрес другого узла маршрута')
        require(scope_check() and self._key_hash(source_ids)==key_hash)
        require(self._base(reference,scope_ids,ui_context)[0]==row,'Funding изменился во время review')
        topology=[{'account_id':n['id'],'address':addresses[n['id']]} for n in nodes]
        credentials={}
        for before,after in zip(nodes,nodes[1:]):
            identity,anchor=keys[before['id']]
            policy=make_policy(account=before['id'],account_context=identity['context'],ui_context=ui_context,
                network='mainnet',source=identity['address'],targets=[addresses[after['id']]],scope='transit_only',
                reserve_nano='0',fee_cap_nano=limits['fee'],transfer_debit_cap_nano=limits['debit'],
                daily_reservation_cap_nano=limits['daily'],confirmed=True,allow_high_caps=is_prod)
            credentials[before['id']]={'policy':policy,'identity':dict(identity),'anchor':anchor}
        require(nano(event['net_credit_nano'])<=nano(limits['debit']) and nano(event['net_credit_nano'])<=nano(limits['daily']),
            'Пополнение превышает выбранный debit/day cap')
        token=secrets.token_urlsafe(32);hashed=hashlib.sha256(token.encode()).hexdigest();expires=self.money.clock()+(1800 if is_prod else 180)
        approval={'version':1,'draft_revision':reference['revision'],'ui_context':ui_context,'route':topology,
            'limits':limits,'key_fingerprint':key_hash,'policy_revisions':{ident:{'id':entry['policy'].id,
                'revision':entry['policy'].revision,'key_revision':entry['identity']['revision']} for ident,entry in credentials.items()}}
        snapshot={'reference':dict(reference),'ui_context':ui_context,'scope_ids':frozenset(scope_ids),
            'nodes':topology,'credentials':credentials,'source_ids':source_ids,'key_hash':key_hash,'base_hash':digest(row),
            'approval_hash':digest(approval),'expires':expires}
        with self.money.db() as db:
            db.execute('UPDATE carousel_ui_reviews SET used=1 WHERE draft=? AND used=0',(reference['id'],))
            db.execute('INSERT INTO carousel_ui_reviews(token_hash,draft,expires,used,plan,approval) VALUES(?,?,?,0,NULL,?)',
                (hashed,reference['id'],expires,json.dumps(approval,sort_keys=True)))
        self.reviews={k:v for k,v in self.reviews.items() if v['expires']>self.money.clock() and v['reference']['id']!=reference['id']}
        self.reviews[hashed]=snapshot
        return {'token':token,'funding_id':reference['id'],'expires':expires,'route':topology,
            'fee_cap_nano':limits['fee'],'debit_cap_nano':limits['debit'],'daily_cap_nano':limits['daily'],
            'net_nano':event['net_credit_nano'],'enabled':self.native.executor.allow(),
            'confirmation':'RUN '+reference['id'],'verification':'local_review_not_chain_proof'}

    def inspect(self,draft,*,scope_ids,ui_context):
        """Original-journal snapshot only; never re-arm or provider-poll."""
        funding=self.funding.report(draft,scope_ids=scope_ids,ui_context=ui_context)
        with self.money.db() as db:
            row=db.execute('SELECT r.plan,r.approval,p.ui_context FROM carousel_ui_reviews r JOIN money_carousels p ON p.id=r.plan WHERE r.draft=? ORDER BY r.expires DESC LIMIT 1',(draft,)).fetchone()
        require(row is not None,'У инструкции ещё нет плана исполнения; native intent смотрите в Money native local')
        # Read old context only while every original account remains allowed and
        # identity-checked. This never grants arming/resume in the new context.
        return {**self.carousel.snapshot(row['plan'],scope_ids=scope_ids,ui_context=row['ui_context']),
            'archived_context':row['ui_context']!=ui_context,
            'approval':json.loads(row['approval']) if row['approval'] else None}

    async def run(self,token,*,scope_ids,ui_context,confirmation,scope_check=lambda:True,progress=lambda event:None):
        require(isinstance(token,str) and len(token)<=100)
        hashed=hashlib.sha256(token.encode()).hexdigest();saved=self.reviews.get(hashed)
        require(saved and self.money.clock()<saved['expires'] and scope_ids==saved['scope_ids']
            and ui_context==saved['ui_context'] and confirmation=='RUN '+saved['reference']['id'],
            'Review истёк/изменился или подтверждение неверно; подготовьте заново')
        def guard():
            if not scope_check(): return False
            try:
                return (self._key_hash(saved['source_ids'])==saved['key_hash']
                    and digest(self._base(saved['reference'],scope_ids,ui_context)[0])==saved['base_hash'])
            except (ValueError,KeyError): return False
        require(guard() and self.native.executor.allow(),
            'Scope/ключи/funding изменились или TGB_FINANCE_ENABLE_NATIVE и TGB_FINANCE_ENABLE_MAINNET не включены')
        with self.money.db() as db:
            stored=db.execute('SELECT approval FROM carousel_ui_reviews WHERE token_hash=?',(hashed,)).fetchone()
            require(stored and stored['approval'] and digest(json.loads(stored['approval']))==saved['approval_hash'],
                'Утверждённые лимиты/маршрут изменились; подготовьте новый review')
            changed=db.execute('UPDATE carousel_ui_reviews SET used=1 WHERE token_hash=? AND used=0 AND expires>?',(hashed,self.money.clock()))
            require(changed.rowcount==1,'Review уже использован; повторная отправка запрещена')
        # Consume before plan creation/await: no browser/GUI retry can re-arm it.
        self.reviews.pop(hashed,None)
        plan=self.carousel.create(saved['nodes'],network='mainnet',scope_ids=scope_ids,ui_context=ui_context,confirmed=True)
        with self.money.db() as db: db.execute('UPDATE carousel_ui_reviews SET plan=? WHERE token_hash=?',(plan['id'],hashed))
        try:
            progress({'phase':'armed','plan_id':plan['id'],'total':len(saved['nodes'])-1,'ordinal':0})
            return await self.runner.run(plan['id'],plan['revision'],funding_ref=saved['reference'],credentials=saved['credentials'],
                scope_ids=scope_ids,ui_context=ui_context,confirmation='RUN '+plan['id'],scope_check=guard,progress=progress)
        except BaseException:
            # Only this newly created, still-unlinked metadata plan can be released.
            # Linked payments/claims are never cancelled or deleted by UI cleanup.
            with self.money.db() as db:
                linked=db.execute('SELECT 1 FROM money_carousel_hops WHERE plan=? AND intent IS NOT NULL',(plan['id'],)).fetchone()
                if not linked:
                    changed=db.execute("UPDATE money_carousels SET state='cancelled',revision=? WHERE id=? AND owner=? AND state IN ('active','paused')",
                        (uuid.uuid4().hex,plan['id'],self.carousel.owner))
                    if changed.rowcount: db.execute('DELETE FROM money_carousel_claims WHERE plan=?',(plan['id'],))
            raise

    async def auto_provision_route(self, reference, *, online_sync=False, scope_ids, ui_context, confirmed=False, scope_check=lambda: True):
        """Automatically sync/provision WalletTg keys in FinancialVault and Telegram for all route accounts."""
        require(confirmed is True and scope_check(), 'Confirm automatic route wallet provisioning')
        require(isinstance(reference, dict) and set(reference) == {'id', 'revision'})
        with self.money.db() as db:
            row, nodes = self.funding._load(db, reference['id'], scope_ids, ui_context, reference['revision'])
        self.native._ensure_vault_ready()
        provisioned = []
        for node in nodes:
            require(scope_check())
            ident = node['id']
            context, _ = self.money.scope(ident, scope_ids, ui_context)
            try:
                existing, _ = self.native._key(ident, context)
                provisioned.append({'account_id': ident, 'address': existing['address'], 'status': 'already_configured'})
                continue
            except ValueError:
                pass
            res = await self.native.auto_sync_or_provision_wallet(
                ident,
                online_sync=online_sync,
                scope_ids=scope_ids,
                ui_context=ui_context,
                confirmed=True,
                scope_check=scope_check,
            )
            provisioned.append({
                'account_id': ident,
                'address': res['provisioned_address'],
                'status': res['custody_mode'],
            })
        if provisioned and hasattr(self.native, 'export_wallets_summary'):
            self.native.export_wallets_summary()
        return {'draft_id': reference['id'], 'provisioned': provisioned}

    async def deploy_uninit_node(
        self,
        reference,
        *,
        limits,
        node_id=None,
        target_id=None,
        scope_ids,
        ui_context,
        confirmed=False,
        scope_check=lambda: True,
    ):
        """Deploy an uninitialized (seqno=0) WalletTg node in the route by attaching StateInit."""
        require(confirmed is True and scope_check() and self.native.executor.allow(), 'Confirm explicit uninit wallet deployment')
        require(isinstance(reference, dict) and set(reference) == {'id', 'revision'})
        require(isinstance(limits, dict) and set(limits) == {'fee', 'debit', 'daily'})
        is_prod = getattr(self.native.executor, 'factory', None) is create_client
        if is_prod:
            limits = self._clean_prod_limits(limits)
        parsed_limits = {k: amount(v) for k, v in limits.items()}
        with self.money.db() as db:
            row, nodes = self.funding._load(db, reference['id'], scope_ids, ui_context, reference['revision'])
        src_id = node_id or nodes[0]['id']
        idx = next((i for i, n in enumerate(nodes[:-1]) if n['id'] == src_id), 0)
        dst_id = target_id or nodes[idx + 1]['id']
        context, _ = self.money.scope(src_id, scope_ids, ui_context)
        identity, anchor = self.native._key(src_id, context)
        dst_account = self.money.accounts.get_account(dst_id)
        src_account = self.money.accounts.get_account(src_id)
        is_prod = getattr(self.native.executor, 'factory', None) is create_client
        dst_address = None
        async with asyncio.timeout(60):
            async with self.native.executor.factory(self.money.config, src_account) as client:
                require((await client.get_me()).get('user_id') == src_account.user_id)
                try:
                    rec = await client.native_money_recipient(dst_account.username, dst_account.user_id, prepare_address=False)
                    dst_address = normalize_address(rec.get('address'), 'mainnet').raw
                except Exception:
                    if not is_prod:
                        raise
                    dst_ctx, _ = self.money.scope(dst_id, scope_ids, ui_context)
                    dst_address = self.native._key(dst_id, dst_ctx)[0]['address']
        policy = make_policy(
            account=src_id,
            account_context=context,
            ui_context=ui_context,
            network='mainnet',
            source=identity['address'],
            targets=[dst_address],
            scope='all_native_authorized',
            reserve_nano='0',
            fee_cap_nano=parsed_limits['fee'],
            transfer_debit_cap_nano=parsed_limits['debit'],
            daily_reservation_cap_nano=parsed_limits['daily'],
            confirmed=True,
            all_native_confirmed=True,
            allow_high_caps=is_prod,
        )
        result = await self.native.executor.execute(
            policy,
            identity,
            anchor_public_key=anchor,
            target_account_id=dst_id,
            scope_ids=scope_ids,
            ui_context=ui_context,
            confirmed=True,
            scope_check=scope_check,
            deploy=True,
            send_all=False,
        )
        return {'deployed_account_id': src_id, 'target_account_id': dst_id, 'intent': result}

    async def resume(
        self,
        draft,
        *,
        scope_ids,
        ui_context,
        confirmation,
        scope_check=lambda: True,
        progress=lambda event: None,
    ):
        """Resume an interrupted/paused carousel plan from its stored approval and journal checkpoint."""
        with self.money.db() as db:
            row = db.execute(
                'SELECT r.plan,r.approval,p.revision AS plan_revision,p.ui_context,p.state FROM carousel_ui_reviews r '
                'JOIN money_carousels p ON p.id=r.plan WHERE r.draft=? ORDER BY r.expires DESC LIMIT 1',
                (draft,),
            ).fetchone()
        require(row is not None and row['approval'], 'Нет сохранённого плана/approval для финансового Resume')
        require(row['ui_context'] == ui_context, 'Контекст группы/фильтра изменился; верните исходный фильтр перед Resume')
        plan_id = row['plan']
        plan_revision = row['plan_revision']
        require(confirmation == 'RESUME ' + plan_id, 'Exact RESUME confirmation required')
        approval = json.loads(row['approval'])
        route = approval['route']
        limits = approval['limits']
        source_ids = {n['account_id'] for n in route[:-1]}
        require(self._key_hash(source_ids) == approval['key_fingerprint'], 'Финансовые ключи маршрута изменились после запуска')
        credentials = {}
        is_prod = getattr(self.native.executor, 'factory', None) is create_client
        for before, after in zip(route, route[1:]):
            context, _ = self.money.scope(before['account_id'], scope_ids, ui_context)
            identity, anchor = self.native._key(before['account_id'], context)
            policy = make_policy(
                account=before['account_id'],
                account_context=identity['context'],
                ui_context=ui_context,
                network='mainnet',
                source=identity['address'],
                targets=[after['address']],
                scope='transit_only',
                reserve_nano='0',
                fee_cap_nano=limits['fee'],
                transfer_debit_cap_nano=limits['debit'],
                daily_reservation_cap_nano=limits['daily'],
                confirmed=True,
                allow_high_caps=is_prod,
            )
            credentials[before['account_id']] = {'policy': policy, 'identity': dict(identity), 'anchor': anchor}
        funding_ref = {'id': draft, 'revision': approval['draft_revision']}
        return await self.runner.resume(
            plan_id,
            plan_revision,
            funding_ref=funding_ref,
            credentials=credentials,
            scope_ids=scope_ids,
            ui_context=ui_context,
            confirmation='RESUME ' + plan_id,
            scope_check=scope_check,
            progress=progress,
        )
