"""Finite local carousel execution through original native intents/receipts.

No startup resume, key intake, hidden address preparation, dispatch retry or
second payment ledger. Requires an explicitly owned reviewed local plan.
"""
import asyncio
import json
import re
import uuid

from core.telegram_client import create_client
from modules.finance_money import FinanceError
from modules.money_carousel import MoneyCarousel, _net_candidate, hex_id
from modules.money_policy import NativeSpendPolicy, digest, nano
from modules.money_transit import funding_snapshot


class CarouselRunError(ValueError): pass


def require(ok,message='Carousel execution changed; stop and review required'):
    if not ok: raise CarouselRunError(message)


class CarouselStep:
    """Process-local admission, linked atomically before any seed access."""
    def __init__(self,carousel,ident,revision,ordinal,funding_ref):
        require(hex_id(ident) and hex_id(revision) and type(ordinal) is int and 0<=ordinal<=500)
        self.carousel,self.ident,self.revision=carousel,ident,revision
        self.ordinal,self.funding_ref=ordinal,dict(funding_ref) if isinstance(funding_ref,dict) else funding_ref

    def snapshot(self,money,db,policy,target,scope_ids,ui_context,*,intent=None):
        require(money is self.carousel.money and policy.scope=='transit_only')
        plan,nodes,hops=self.carousel._load(db,self.ident,scope_ids,ui_context,self.revision,owned=True)
        require(plan['network']=='mainnet' and 0<=self.ordinal<len(hops))
        before,after=nodes[self.ordinal:self.ordinal+2]
        require(policy.account==before['account_id'] and policy.source==before['address']
            and policy.account_context==before['context'] and policy.ui_context==ui_context
            and policy.network==plan['network'] and target==after['account_id']
            and policy.targets==(after['address'],))
        progress,index,_=self.carousel._progress(db,plan,nodes,hops)
        require(index==self.ordinal and all(p['journal_state']=='provider_delivered' for p in progress[:self.ordinal])
            and all(h['intent'] is None for h in hops[self.ordinal+1:]))
        require(hops[self.ordinal]['intent']==intent)
        if self.ordinal==0:
            return funding_snapshot(money,db,self.funding_ref,policy,target,scope_ids,ui_context,intent=intent)
        previous=hops[self.ordinal-1]['intent']
        record=db.execute('SELECT evidence FROM reconciliation WHERE intent=?',(previous,)).fetchone()
        require(record is not None)
        evidence=json.loads(record['evidence'])
        net=_net_candidate(evidence, allow_uninit=getattr(self.carousel, 'allow_uninit_hops', False))
        require(net is not None and isinstance(evidence.get('recipient_lt'),str),
            'Previous receipt lacks exact active recipient net/hash/LT; no next transfer')
        require(re.fullmatch('0|[1-9][0-9]{0,19}',evidence['recipient_lt']) is not None and int(evidence['recipient_lt'])<2**64)
        original=db.execute('SELECT amount FROM intents WHERE id=?',(previous,)).fetchone()
        require(original and evidence['incoming_value_nano']==original['amount'], 'Receipt value differs from original intent')
        event={'tx_hash':evidence['recipient_tx'],'message_hash':evidence['message_hash'],
            'lt':evidence['recipient_lt'],'address':policy.source}
        old=nano(evidence['recipient_before_nano'])
        snapshot={'reference':{'plan':self.ident,'previous_intent':previous},'event':event,
            'context':policy.account_context,'ui_context':ui_context,'source':policy.source,
            'target':target,'net_nano':net,'old_nano':str(old),'balance_nano':str(old+nano(net))}
        # Freeze the exact original receipt, not merely its positive status.
        snapshot['evidence_hash']=digest(evidence);snapshot['hash']=digest(snapshot)
        claim=db.execute('SELECT * FROM native_transit_claims WHERE message_hash=?',(event['message_hash'],)).fetchone()
        require(claim is None if intent is None else claim is not None and claim['intent']==intent
            and claim['reference_hash']==snapshot['hash'])
        return snapshot

    def link(self,db,intent):
        changed=db.execute('UPDATE money_carousel_hops SET intent=? WHERE plan=? AND ordinal=? AND intent IS NULL',
            (intent,self.ident,self.ordinal))
        require(changed.rowcount==1)
        revision=uuid.uuid4().hex
        changed=db.execute("UPDATE money_carousels SET revision=? WHERE id=? AND revision=? AND owner=? AND state='active'",
            (revision,self.ident,self.revision,self.carousel.owner))
        require(changed.rowcount==1)
        self.revision=revision


class MoneyCarouselRunner:
    READ_ATTEMPTS=4
    READ_DELAY=10
    DEADLINE=7200

    def __init__(self,carousel,executor,*,sleep=asyncio.sleep):
        require(isinstance(carousel,MoneyCarousel) and executor.money is carousel.money)
        self.carousel,self.executor,self.money=carousel,executor,carousel.money
        self.sleep=sleep;self.lock=asyncio.Lock()

    def _credentials(self,nodes,credentials,scope_ids,ui_context):
        require(isinstance(credentials,dict) and set(credentials)=={n['account_id'] for n in nodes[:-1]})
        inventory=self.executor.vault.status()
        require(inventory.get('exists') is True and inventory.get('locked_in_process') is False,
            'Financial vault must be explicitly configured and unlocked before starting')
        frozen={}
        identity_fields={'account','user_id','context','network','address','public_key','revision'}
        inventory_identities=[{k:r.get(k) for k in identity_fields} for r in inventory['records']]
        for before,after in zip(nodes,nodes[1:]):
            entry=credentials[before['account_id']]
            require(isinstance(entry,dict) and set(entry)=={'policy','identity','anchor'})
            policy=entry['policy'];identity=dict(entry['identity'])
            require(isinstance(policy,NativeSpendPolicy) and policy.scope=='transit_only'
                and policy.account==before['account_id'] and policy.account_context==before['context']
                and policy.ui_context==ui_context and policy.network=='mainnet' and policy.source==before['address']
                and policy.targets==(after['address'],) and policy.reserve_nano=='0')
            context,_=self.money.scope(before['account_id'],scope_ids,ui_context)
            account=self.money.accounts.get_account(before['account_id'])
            require(set(identity)==identity_fields and identity in inventory_identities and identity['account']==before['account_id']
                and identity['address']==before['address'] and identity['context']==context
                and identity['user_id']==account.user_id
                and identity['network']=='mainnet' and self.executor.codec.derive(entry['anchor'],'mainnet')['source']==before['address'])
            frozen[before['account_id']]={'policy':policy,'identity':identity,'anchor':entry['anchor']}
        return frozen

    async def run(self,ident,revision,*,funding_ref,credentials,scope_ids,ui_context,confirmation,
                  scope_check=lambda:True,progress=lambda event:None):
        require(confirmation=='RUN '+ident and self.executor.allow() and scope_check())
        require(isinstance(funding_ref,dict) and set(funding_ref)=={'id','revision'})
        require(not self.lock.locked(),'Carousel runner already active')
        # No automatic startup/resume/retry of any linked payment, even a failed one.
        with self.money.db() as db:
            plan,nodes,hops=self.carousel._load(db,ident,scope_ids,ui_context,revision,owned=True)
            require(plan['network']=='mainnet' and all(h['intent'] is None for h in hops),
                'Only a fresh owned plan can start; linked intents require original journal review')
            row=db.execute('SELECT nodes FROM money_funding_drafts WHERE id=?',(funding_ref.get('id'),)).fetchone()
            require(row and [n['id'] for n in json.loads(row['nodes'])]==[n['account_id'] for n in nodes],
                'Funding route must exactly match the reviewed plan')
        frozen=self._credentials(nodes,credentials,scope_ids,ui_context)
        funding_ref=dict(funding_ref);scope_ids=frozenset(scope_ids)
        phase='admission';step=None
        def guard():
            require(scope_check() and self.executor.allow())
            with self.money.db() as db:
                self.carousel._load(db,ident,scope_ids,ui_context,revision,owned=True)
            return True
        def pause():
            with self.money.db() as db:
                db.execute("UPDATE money_carousels SET state='paused',revision=? WHERE id=? AND revision=? AND owner=? AND state='active'",
                    (uuid.uuid4().hex,ident,revision,self.carousel.owner))
        async def wait(seconds):
            require(0<=seconds<=30,'Provider cooldown exceeds bounded controller budget')
            while seconds>0:
                chunk=min(1,seconds);await self.sleep(chunk);seconds-=chunk;guard()
        is_prod=getattr(self.executor,'factory',None) is create_client
        max_attempts=12 if is_prod else self.READ_ATTEMPTS
        deadline=86400 if is_prod else self.DEADLINE
        async with self.lock:
            try:
                async with asyncio.timeout(deadline):
                    for ordinal,(before,after) in enumerate(zip(nodes,nodes[1:])):
                        guard();phase='hop_admission'
                        step=CarouselStep(self.carousel,ident,revision,ordinal,funding_ref)
                        entry=frozen[before['account_id']]
                        progress({'phase':'sending','ordinal':ordinal,'total':len(hops),'account_id':before['account_id']})
                        max_hop_attempts = 3 if is_prod else 1
                        try:
                            for hop_attempt in range(max_hop_attempts):
                                try:
                                    result=await self.executor.execute(entry['policy'],entry['identity'],anchor_public_key=entry['anchor'],
                                        target_account_id=after['account_id'],scope_ids=scope_ids,ui_context=ui_context,
                                        confirmed=True,scope_check=scope_check,send_all=True,carousel_step=step,
                                        funding_ref=funding_ref if ordinal==0 else None)
                                    break
                                except Exception as exc:
                                    if 'outcome uncertain' in str(exc) or hop_attempt + 1 >= max_hop_attempts:
                                        raise
                                    progress({'phase':'waiting_retry','ordinal':ordinal,'total':len(hops),'attempt':hop_attempt+1})
                                    await wait(2.0 * (hop_attempt + 1))
                        finally:
                            revision=step.revision
                        guard();phase='reconciliation'
                        for attempt in range(max_attempts):
                            guard()
                            cooldown=max(0,self.money.due-self.money.clock())
                            if cooldown:
                                await wait(cooldown)
                            try:
                                outcome=await self.money.action('reconcile',{'account_id':before['account_id'],'id':result['id'],
                                    'source':before['address'],'network':'mainnet','confirmed':True},
                                    scope_ids=scope_ids,ui_context=ui_context,scope_check=scope_check)
                            except FinanceError:
                                if attempt+1<max_attempts:
                                    progress({'phase':'waiting_receipt','ordinal':ordinal,'total':len(hops),'intent_id':result['id']})
                                    await wait(self.READ_DELAY)
                                    continue
                                raise
                            guard()
                            if outcome['state']=='provider_delivered': break
                            status=outcome['reconciliation'].get('status')
                            require(status in {'not_found','source_pending','recipient_pending'},'Ambiguous/unsupported receipt; stop')
                            if attempt+1<max_attempts:
                                progress({'phase':'waiting_receipt','ordinal':ordinal,'total':len(hops),'intent_id':result['id']})
                                await wait(self.READ_DELAY)
                        require(outcome['state']=='provider_delivered','Receipt deadline reached; do not repeat the transfer')
                        progress({'phase':'delivered','ordinal':ordinal,'total':len(hops),'intent_id':result['id']})
                    guard();phase='complete'
                    final=self.carousel.control(ident,revision,'complete',scope_ids=scope_ids,ui_context=ui_context,
                        confirmation='COMPLETE '+ident)
                    return {'id':ident,'state':final['state'],'completed_hops':len(hops),
                        'verification':'provider_reported_not_chain_proof','automatic_retry':False,'automatic_resume':False}
            except BaseException as exc:
                pause()
                if isinstance(exc,asyncio.CancelledError): raise
                if not isinstance(exc,Exception): raise
                detail=(f': {exc}' if is_prod else '')
                raise CarouselRunError('Carousel stopped at '+phase+' ('+type(exc).__name__+')'+detail+'; reconcile original linked intents; no automatic retry') from None

    async def resume(
        self,
        ident,
        revision,
        *,
        funding_ref,
        credentials,
        scope_ids,
        ui_context,
        confirmation,
        scope_check=lambda: True,
        progress=lambda event: None,
    ):
        """Reconcile any in-flight linked hop and safely resume remaining hops from checkpoint."""
        require(confirmation == 'RESUME ' + ident and self.executor.allow() and scope_check())
        require(isinstance(funding_ref, dict) and set(funding_ref) == {'id', 'revision'})
        require(not self.lock.locked(), 'Carousel runner already active')
        with self.money.db() as db:
            plan, nodes, hops = self.carousel._load(db, ident, scope_ids, ui_context, revision, owned=False)
            require(plan['network'] == 'mainnet' and plan['state'] not in {'cancelled', 'completed'}, 'Plan is not resumable')
            row = db.execute('SELECT nodes FROM money_funding_drafts WHERE id=?', (funding_ref.get('id'),)).fetchone()
            require(
                row and [n['id'] for n in json.loads(row['nodes'])] == [n['account_id'] for n in nodes],
                'Funding route must exactly match the reviewed plan',
            )
        frozen = self._credentials(nodes, credentials, scope_ids, ui_context)
        funding_ref = dict(funding_ref)
        scope_ids = frozenset(scope_ids)
        phase = 'resume_reconcile'

        def guard():
            require(scope_check() and self.executor.allow())
            with self.money.db() as db:
                self.carousel._load(db, ident, scope_ids, ui_context, revision, owned=False)
            return True

        def pause():
            with self.money.db() as db:
                db.execute(
                    "UPDATE money_carousels SET state='paused',revision=? WHERE id=? AND revision=?",
                    (uuid.uuid4().hex, ident, revision),
                )

        async def wait(seconds):
            require(0 <= seconds <= 30, 'Provider cooldown exceeds bounded controller budget')
            while seconds > 0:
                chunk = min(1, seconds)
                await self.sleep(chunk)
                seconds -= chunk
                guard()

        is_prod = getattr(self.executor, 'factory', None) is create_client
        max_attempts = 12 if is_prod else self.READ_ATTEMPTS
        deadline = 86400 if is_prod else self.DEADLINE

        async with self.lock:
            try:
                async with asyncio.timeout(deadline):
                    # Step 1: Reconcile any linked hop whose intent is not yet provider_delivered
                    for ordinal, hop in enumerate(hops):
                        if not hop['intent']:
                            continue
                        with self.money.db() as db:
                            i_row = db.execute('SELECT state FROM intents WHERE id=?', (hop['intent'],)).fetchone()
                        if i_row and i_row['state'] == 'provider_delivered':
                            continue
                        if i_row and i_row['state'] in {'operator_not_sent', 'cancelled'}:
                            with self.money.db() as db:
                                db.execute('UPDATE money_carousel_hops SET intent=NULL WHERE plan=? AND ordinal=?', (ident, ordinal))
                                db.execute('DELETE FROM native_transit_claims WHERE intent=?', (hop['intent'],))
                            continue
                        before = nodes[ordinal]
                        for attempt in range(max_attempts):
                            guard()
                            cooldown = max(0, self.money.due - self.money.clock())
                            if cooldown:
                                await wait(cooldown)
                            try:
                                outcome = await self.money.action(
                                    'reconcile',
                                    {
                                        'account_id': before['account_id'],
                                        'id': hop['intent'],
                                        'source': before['address'],
                                        'network': 'mainnet',
                                        'confirmed': True,
                                    },
                                    scope_ids=scope_ids,
                                    ui_context=ui_context,
                                    scope_check=scope_check,
                                )
                            except FinanceError:
                                if attempt + 1 < max_attempts:
                                    progress({'phase': 'waiting_receipt', 'ordinal': ordinal, 'total': len(hops), 'intent_id': hop['intent']})
                                    await wait(self.READ_DELAY)
                                    continue
                                raise
                            guard()
                            if outcome['state'] == 'provider_delivered':
                                break
                            status = outcome['reconciliation'].get('status')
                            require(status in {'not_found', 'source_pending', 'recipient_pending'}, 'Ambiguous receipt during resume; stop')
                            if attempt + 1 < max_attempts:
                                progress({'phase': 'waiting_receipt', 'ordinal': ordinal, 'total': len(hops), 'intent_id': hop['intent']})
                                await wait(self.READ_DELAY)
                        require(outcome['state'] == 'provider_delivered', 'In-flight hop not yet delivered; cannot advance safely')

                    # Step 2: Transition plan to active/owned via control('resume')
                    phase = 'resume_control'
                    resumed = self.carousel.control(
                        ident,
                        revision,
                        'resume',
                        scope_ids=scope_ids,
                        ui_context=ui_context,
                        confirmation='RESUME ' + ident,
                    )
                    revision = resumed['revision']
                    start_ordinal = resumed['next_ordinal']

                    # Step 3: Execute remaining hops from start_ordinal
                    if start_ordinal is not None:
                        for ordinal in range(start_ordinal, len(hops)):
                            before, after = nodes[ordinal], nodes[ordinal + 1]
                            guard()
                            phase = 'hop_admission'
                            step = CarouselStep(self.carousel, ident, revision, ordinal, funding_ref)
                            entry = frozen[before['account_id']]
                            progress({'phase': 'sending', 'ordinal': ordinal, 'total': len(hops), 'account_id': before['account_id']})
                            max_hop_attempts = 3 if is_prod else 1
                            try:
                                for hop_attempt in range(max_hop_attempts):
                                    try:
                                        result = await self.executor.execute(
                                            entry['policy'],
                                            entry['identity'],
                                            anchor_public_key=entry['anchor'],
                                            target_account_id=after['account_id'],
                                            scope_ids=scope_ids,
                                            ui_context=ui_context,
                                            confirmed=True,
                                            scope_check=scope_check,
                                            send_all=True,
                                            carousel_step=step,
                                            funding_ref=funding_ref if ordinal == 0 else None,
                                        )
                                        break
                                    except Exception as exc:
                                        if 'outcome uncertain' in str(exc) or hop_attempt + 1 >= max_hop_attempts:
                                            raise
                                        progress({'phase': 'waiting_retry', 'ordinal': ordinal, 'total': len(hops), 'attempt': hop_attempt + 1})
                                        await wait(2.0 * (hop_attempt + 1))
                            finally:
                                revision = step.revision
                            guard()
                            phase = 'reconciliation'
                            for attempt in range(max_attempts):
                                guard()
                                cooldown = max(0, self.money.due - self.money.clock())
                                if cooldown:
                                    await wait(cooldown)
                                try:
                                    outcome = await self.money.action(
                                        'reconcile',
                                        {
                                            'account_id': before['account_id'],
                                            'id': result['id'],
                                            'source': before['address'],
                                            'network': 'mainnet',
                                            'confirmed': True,
                                        },
                                        scope_ids=scope_ids,
                                        ui_context=ui_context,
                                        scope_check=scope_check,
                                    )
                                except FinanceError:
                                    if attempt + 1 < max_attempts:
                                        progress({'phase': 'waiting_receipt', 'ordinal': ordinal, 'total': len(hops), 'intent_id': result['id']})
                                        await wait(self.READ_DELAY)
                                        continue
                                    raise
                                guard()
                                if outcome['state'] == 'provider_delivered':
                                    break
                                status = outcome['reconciliation'].get('status')
                                require(status in {'not_found', 'source_pending', 'recipient_pending'}, 'Ambiguous/unsupported receipt; stop')
                                if attempt + 1 < max_attempts:
                                    progress({'phase': 'waiting_receipt', 'ordinal': ordinal, 'total': len(hops), 'intent_id': result['id']})
                                    await wait(self.READ_DELAY)
                            require(outcome['state'] == 'provider_delivered', 'Receipt deadline reached; do not repeat the transfer')
                            progress({'phase': 'delivered', 'ordinal': ordinal, 'total': len(hops), 'intent_id': result['id']})

                    guard()
                    phase = 'complete'
                    final = self.carousel.control(
                        ident,
                        revision,
                        'complete',
                        scope_ids=scope_ids,
                        ui_context=ui_context,
                        confirmation='COMPLETE ' + ident,
                    )
                    return {
                        'id': ident,
                        'state': final['state'],
                        'completed_hops': len(hops),
                        'resumed_from': start_ordinal if start_ordinal is not None else len(hops),
                        'verification': 'provider_reported_not_chain_proof',
                        'automatic_retry': False,
                        'automatic_resume': False,
                    }
            except BaseException as exc:
                pause()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if not isinstance(exc, Exception):
                    raise
                raise CarouselRunError(
                    'Carousel resume stopped at ' + phase + ' (' + type(exc).__name__ + '); reconcile original linked intents; no automatic retry'
                ) from None
