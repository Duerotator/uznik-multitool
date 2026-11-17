"""Finite local carousel checkpoints referencing the existing Money journal.

No payment creation, prompt capability, signer, provider read or execution loop.
Plan topology is metadata, NOT a second payment ledger. Restart requires explicit
review/resume; ambiguous intents are never retried. Wallet links are declarations.
"""
import json
import re
import uuid

from core.ton_address import normalize_address
from modules.finance_money import FinanceError
from modules.money_policy import MoneyPolicyError, nano
from modules.ton_readonly import _unique, ReadError


class CarouselError(ValueError):
    pass


def require(condition,message='Carousel changed/unsupported; review required'):
    if not condition: raise CarouselError(message)


def hex_id(value,size=32):
    return isinstance(value,str) and re.fullmatch('[a-f0-9]{'+str(size)+'}',value) is not None


def _net_candidate(evidence, *, allow_uninit=False):
    """Read-only net amount from exact reconciliation, never spend permission."""
    allowed_states = {'active', 'uninitialized_credited'} if allow_uninit else {'active'}
    if not isinstance(evidence,dict) or evidence.get('recipient_state') not in allowed_states or evidence.get('transit_accounting')!='provider_reported_net_not_spend_authorized':
        return None
    try:
        before=nano(evidence.get('recipient_before_nano'))
        after=nano(evidence.get('recipient_after_nano'))
        net=nano(evidence.get('recipient_net_nano'),positive=True)
        fees=nano(evidence.get('recipient_fee_nano'))
        value=nano(evidence.get('incoming_value_nano'),positive=True)
    except MoneyPolicyError: return None
    return str(net) if after-before==net and value-net==fees else None


class MoneyCarousel:
    def __init__(self,money,*,allow_uninit_hops=False):
        self.money=money
        self.allow_uninit_hops=allow_uninit_hops
        self.owner=uuid.uuid4().hex
        # Same SQLite database/transaction boundary as intents + reconciliation.
        with self.money.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS money_carousels(id TEXT PRIMARY KEY, revision TEXT NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL, network TEXT NOT NULL, ui_context TEXT NOT NULL, topology TEXT NOT NULL, created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS money_carousel_hops(plan TEXT NOT NULL, ordinal INTEGER NOT NULL, intent TEXT UNIQUE, PRIMARY KEY(plan,ordinal))')
            db.execute('CREATE TABLE IF NOT EXISTS money_carousel_claims(network TEXT NOT NULL, address TEXT NOT NULL, plan TEXT NOT NULL, PRIMARY KEY(network,address))')

    def _scope(self,nodes,scope_ids,ui_context):
        require(hex_id(ui_context,64),'Exact current group/filter context required')
        result=[]
        for idx,node in enumerate(nodes):
            if idx==len(nodes)-1 and len(nodes)>=2 and node.get('external') is True and node.get('account_id')=='__wallet_x__':
                result.append(dict(node))
                continue
            try: context,ui=self.money.scope(node['account_id'],scope_ids,ui_context)
            except (FinanceError,KeyError,LookupError):
                raise CarouselError('Account removed/disabled/outside current filters') from None
            require(ui==ui_context)
            if 'context' in node: require(node['context']==context,'Account identity changed')
            result.append({**node,'context':context})
        return result

    def create(self,nodes,*,network,scope_ids,ui_context,confirmed=False,terminal_external=None):
        require(confirmed is True,'Explicit local plan confirmation required')
        require(network in ('mainnet','testnet') and isinstance(nodes,list) and 2<=len(nodes)<=502)
        if terminal_external is not None:
            require(isinstance(terminal_external,dict) and set(terminal_external)=={'account_id','address'}
                    and terminal_external['account_id']=='__wallet_x__','Unsupported external carousel terminal')
            try: ext_addr=normalize_address(terminal_external['address'],network).raw
            except (ValueError,TypeError): raise CarouselError('Invalid external Wallet X terminal address') from None
        else:
            ext_addr=None
        topology=[]
        for idx,node in enumerate(nodes):
            require(isinstance(node,dict) and set(node)=={'account_id','address'})
            require(isinstance(node['account_id'],str) and re.fullmatch('[A-Za-z0-9_.:-]{1,160}',node['account_id']))
            try: address=normalize_address(node['address'],network).raw
            except (ValueError,TypeError): raise CarouselError('Invalid declared wallet address') from None
            entry={'account_id':node['account_id'],'address':address}
            if ext_addr is not None and idx==len(nodes)-1:
                require(node['account_id']=='__wallet_x__' and address==ext_addr,'External terminal does not match topology tail')
                entry['external']=True
            topology.append(entry)
        # A final return to A or external Wallet X is allowed, but repeated intermediates/alias addresses are not.
        is_ext_tail = bool(topology[-1].get('external'))
        unique=topology[:-1] if (topology[-1]==topology[0] or is_ext_tail) else topology
        require(len({n['account_id'] for n in unique})==len(unique)
                and len({n['address'] for n in unique})==len(unique),'Duplicate/aliased intermediate accounts')
        if is_ext_tail:
            require(topology[-1]['address'] not in {n['address'] for n in unique},'External terminal address conflicts with route sender')
        require(all(a['address']!=b['address'] for a,b in zip(topology,topology[1:])))
        topology=self._scope(topology,scope_ids,ui_context)
        ident,revision=uuid.uuid4().hex,uuid.uuid4().hex
        with self.money.db() as db:
            db.execute("DELETE FROM money_carousel_claims WHERE plan IN (SELECT id FROM money_carousels WHERE state IN ('cancelled','completed'))")
            for node in unique:
                require(db.execute('SELECT 1 FROM money_carousel_claims WHERE network=? AND address=?',
                    (network,node['address'])).fetchone() is None,'Wallet already claimed by an unfinished plan')
                require(db.execute("SELECT 1 FROM intents WHERE network=? AND source=? AND state IN ('wallet_prompted','wallet_submitted_unverified','uncertain')",
                    (network,node['address'])).fetchone() is None,'Source has an unresolved Money intent')
            db.execute('INSERT INTO money_carousels VALUES(?,?,?,?,?,?,?,?)',
                (ident,revision,self.owner,'active',network,ui_context,json.dumps(topology,sort_keys=True),self.money.clock()))
            db.executemany('INSERT INTO money_carousel_hops(plan,ordinal) VALUES(?,?)',[(ident,i) for i in range(len(topology)-1)])
            db.executemany('INSERT INTO money_carousel_claims VALUES(?,?,?)',[(network,n['address'],ident) for n in unique])
        return self.snapshot(ident,scope_ids=scope_ids,ui_context=ui_context)

    def _load(self,db,ident,scope_ids,ui_context,revision=None,owned=False):
        require(hex_id(ident))
        row=db.execute('SELECT * FROM money_carousels WHERE id=?',(ident,)).fetchone()
        require(row is not None,'Unknown carousel')
        plan=dict(row)
        require(plan['ui_context']==ui_context,'Group/filter context changed')
        if revision is not None: require(plan['revision']==revision,'Stale carousel revision')
        if owned: require(plan['owner']==self.owner and plan['state']=='active','Plan paused/interrupted; explicit review required')
        try: nodes=json.loads(plan['topology'],object_pairs_hook=_unique)
        except (ValueError,ReadError): raise CarouselError('Invalid plan topology') from None
        require(isinstance(nodes,list) and 2<=len(nodes)<=502)
        nodes=self._scope(nodes,scope_ids,ui_context)
        hops=db.execute('SELECT * FROM money_carousel_hops WHERE plan=? ORDER BY ordinal',(ident,)).fetchall()
        require(len(hops)==len(nodes)-1 and [h['ordinal'] for h in hops]==list(range(len(hops))))
        return plan,nodes,hops

    def _progress(self,db,plan,nodes,hops):
        result=[];next_ordinal=None;blocked=False;seen_messages=set()
        for index,hop in enumerate(hops):
            state='unlinked';evidence=None
            if hop['intent']:
                row=db.execute('SELECT * FROM intents WHERE id=?',(hop['intent'],)).fetchone()
                require(row is not None,'Linked intent missing; do not recreate it')
                before,after=nodes[index:index+2]
                require(row['account']==before['account_id'] and row['context']==before['context']
                        and row['ui_context']==plan['ui_context'] and row['network']==plan['network']
                        and row['source']==before['address'] and row['destination']==after['address']
                        and row['created']>=plan['created'],'Journal intent no longer matches topology/context')
                state=row['state']
                if state=='provider_delivered':
                    rec=db.execute('SELECT * FROM reconciliation WHERE intent=?',(hop['intent'],)).fetchone()
                    require(rec is not None,'Missing provider reconciliation')
                    try: evidence=json.loads(rec['evidence'],object_pairs_hook=_unique)
                    except (ValueError,ReadError): raise CarouselError('Malformed provider reconciliation') from None
                    require(isinstance(evidence,dict) and evidence.get('status')=='provider_delivered'
                            and evidence.get('verification')=='provider_reported_not_chain_proof'
                            and evidence.get('provider')=='toncenter_v3' and evidence.get('automatic_retry') is False
                            and all(hex_id(evidence.get(k),64) for k in ('source_tx','recipient_tx','message_hash'))
                            and type(row['prompted']) in (int,float) and row['prompted']<=rec['checked']<=self.money.clock(),
                            'Delivery evidence incomplete; no next hop')
                    require(evidence['message_hash'] not in seen_messages,'Duplicate delivery message across hops')
                    seen_messages.add(evidence['message_hash'])
                    if evidence.get('recipient_state')=='uninitialized_credited' and index<len(hops)-1 and not self.allow_uninit_hops:
                        state='recipient_activation_needs_review'
                elif state=='prepared' and row['expires']<=self.money.clock(): state='expired_needs_review'
            if state!='provider_delivered':
                if next_ordinal is None: next_ordinal=index
                if state not in {'unlinked','prepared'}: blocked=True
            elif next_ordinal is not None:
                raise CarouselError('Out-of-order linked journal; review required')
            # Only one pending link; it cannot be skipped by a later attachment.
            if hop['intent'] and next_ordinal is not None and index>next_ordinal:
                raise CarouselError('Later hop linked before previous delivery')
            result.append({'ordinal':index,'account_id':nodes[index]['account_id'],
                'destination_account_id':nodes[index+1]['account_id'],'intent_id':hop['intent'],
                'journal_state':state,'delivered':state in {'provider_delivered','recipient_activation_needs_review'},
                'recipient_state':evidence.get('recipient_state','unknown') if evidence else 'unknown',
                'recipient_net_nano':_net_candidate(evidence, allow_uninit=self.allow_uninit_hops)})
        return result,next_ordinal,blocked

    def snapshot(self,ident,*,scope_ids,ui_context):
        with self.money.db() as db:
            plan,nodes,hops=self._load(db,ident,scope_ids,ui_context)
            progress,next_ordinal,blocked=self._progress(db,plan,nodes,hops)
        state=plan['state']
        if state=='active' and plan['owner']!=self.owner: state='interrupted'
        if state=='active' and blocked: state='needs_review'
        candidate=progress[next_ordinal-1]['recipient_net_nano'] if next_ordinal is not None and next_ordinal>0 else None
        return {'id':ident,'revision':plan['revision'],'state':state,'network':plan['network'],
            'topology':[{'account_id':n['account_id'],'address':n['address']} for n in nodes],
            'hops':progress,'next_ordinal':next_ordinal,'complete':next_ordinal is None,
            'source_ownership':'operator_declared_not_verified','transit_credit_nano':candidate,
            'spend_authorized':False,'production_execution':'unsupported',
            'automatic_retry':False,'automatic_resume':False}

    def attach(self,ident,revision,intent_id,*,scope_ids,ui_context,confirmed=False):
        require(confirmed is True and hex_id(intent_id),'Explicit existing-intent checkpoint confirmation required')
        with self.money.db() as db:
            plan,nodes,hops=self._load(db,ident,scope_ids,ui_context,revision,owned=True)
            _,index,blocked=self._progress(db,plan,nodes,hops)
            require(index is not None and not blocked and hops[index]['intent'] is None,'Current hop already linked/unresolved')
            require(db.execute('SELECT 1 FROM intents WHERE id=?',(intent_id,)).fetchone() is not None,'No such existing Money intent')
            require(db.execute('SELECT 1 FROM money_carousel_hops WHERE intent=?',(intent_id,)).fetchone() is None,'Intent already checkpointed')
            require(db.execute("SELECT 1 FROM intents WHERE network=? AND source=? AND id<>? AND state IN ('wallet_prompted','wallet_submitted_unverified','uncertain')",
                (plan['network'],nodes[index]['address'],intent_id)).fetchone() is None,'Another intent owns this source; review it first')
            db.execute('UPDATE money_carousel_hops SET intent=? WHERE plan=? AND ordinal=?',(intent_id,ident,index))
            # Validate the newly referenced intent inside the same transaction; a
            # mismatch rolls back the attachment, never changes a payment row.
            _,_,updated=self._load(db,ident,scope_ids,ui_context)
            self._progress(db,plan,nodes,updated)
            db.execute('UPDATE money_carousels SET revision=? WHERE id=?',(uuid.uuid4().hex,ident))
        return self.snapshot(ident,scope_ids=scope_ids,ui_context=ui_context)

    def control(self,ident,revision,action,*,scope_ids,ui_context,confirmation):
        require(action in {'pause','resume','cancel','complete'} and confirmation==action.upper()+' '+ident,
                'Exact explicit plan-control confirmation required')
        with self.money.db() as db:
            plan,nodes,hops=self._load(db,ident,scope_ids,ui_context,revision)
            require(plan['state'] not in {'cancelled','completed'},'Plan already terminal')
            _,index,blocked=self._progress(db,plan,nodes,hops)
            if action=='resume':
                require(not blocked and (plan['state']=='paused' or plan['owner']!=self.owner),'No safe resumable checkpoint')
                state='active'
            elif action=='cancel':
                require(all(h['intent'] is None for h in hops),'Linked payments cannot be cancelled/retried through a plan')
                state='cancelled'
            elif action=='complete':
                require(index is None and not blocked,'Every hop requires provider-delivered reconciliation')
                state='completed'
            else: state='paused'
            if state in {'cancelled','completed'}:
                db.execute('DELETE FROM money_carousel_claims WHERE plan=?',(ident,))
            if state=='completed' and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='money_funding_claims'").fetchone():
                accounts=[n['account_id'] for n in nodes]
                placeholders=','.join('?'*len(accounts))
                drafts=[r[0] for r in db.execute(f'SELECT DISTINCT draft FROM money_funding_claims WHERE account IN ({placeholders})',accounts).fetchall()]
                db.execute(f'DELETE FROM money_funding_claims WHERE account IN ({placeholders})',accounts)
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='money_funding_drafts'").fetchone():
                    for draft_id in drafts:
                        if db.execute('SELECT 1 FROM money_funding_claims WHERE draft=? LIMIT 1',(draft_id,)).fetchone() is None:
                            db.execute("UPDATE money_funding_drafts SET state='completed',revision=?,lease=NULL WHERE id=? AND state<>'cancelled'",(uuid.uuid4().hex,draft_id))
            db.execute('UPDATE money_carousels SET revision=?,owner=?,state=? WHERE id=?',
                (uuid.uuid4().hex,self.owner,state,ident))
        return self.snapshot(ident,scope_ids=scope_ids,ui_context=ui_context)
