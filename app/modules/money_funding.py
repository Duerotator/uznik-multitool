"""Carousel funding instructions + explicit read-only incoming observation.

Draft/event references share Money's database. No payment row, key, signing,
spendable balance or executor authorization is created by a button press.
"""
import asyncio
import json
import re
import uuid

import httpx

from core.telegram_client import create_client
from core.ton_address import normalize_address
from modules.money_chain_read import MoneyReadError, bounded_json
from modules.money_credit import uninitialized_credit
from modules.money_policy import nano
from modules.money_reconciliation import _hash,_final,_executed


class FundingError(ValueError): pass


def require(ok,message='Funding draft/evidence changed; explicit review required'):
    if not ok: raise FundingError(message)


def incoming_credit(tx,address,created,now):
    """Provider-reported exact net credit, not spend authorization/chain proof."""
    require(isinstance(tx,dict) and normalize_address(tx.get('account'),'mainnet').raw==address)
    require(type(tx.get('now')) is int and int(created)<=tx['now']<=now+15)
    require(_final(tx),'Incoming transaction is not finalized; check explicitly later')
    msg=tx.get('in_msg');require(isinstance(msg,dict) and msg.get('source') is not None)
    source=normalize_address(msg['source'],'mainnet').raw
    require(source!=address and normalize_address(msg.get('destination'),'mainnet').raw==address)
    require(msg.get('bounced') is False and msg.get('value_extra_currencies') in (None,{})
        and tx.get('total_fees_extra_currencies') in (None,{}) and tx.get('out_msgs')==[])
    value=nano(msg.get('value'),positive=True)
    body=msg.get('message_content');decoded=body.get('decoded') if isinstance(body,dict) else None
    opcode=msg.get('opcode')
    valid_opcode=(type(opcode) is int and opcode==0) or opcode in (None,'0x00000000')
    valid_decoded=(
        (isinstance(decoded,dict) and decoded.get('type')=='ton_transfer' and decoded.get('encrypted') is False)
        or (isinstance(decoded,dict) and decoded.get('@type')=='text_comment' and decoded.get('encrypted') in (False,None))
        or (isinstance(decoded,dict) and decoded.get('@type')=='empty_cell')
        or (decoded is None and opcode is None)
    )
    require(valid_opcode and valid_decoded,'Unsupported deposit body/asset; review required')
    comment=decoded.get('comment') if isinstance(decoded,dict) else None
    require(comment is None or isinstance(comment,str) and not comment.startswith('accmanager:'),
        'Own reserved transit is not a new funding deposit')
    desc=tx.get('description');require(isinstance(desc,dict))
    if tx.get('orig_status') in {'nonexist','uninit'}:
        credit=uninitialized_credit(tx,msg)
        before=nano(credit['recipient_before_nano']);net=nano(credit['recipient_net_nano'])
        state='uninitialized_credited'
    else:
        require(_executed(tx) and desc.get('destroyed') is False and desc.get('bounce') is None)
        before_state,after=tx.get('account_state_before'),tx.get('account_state_after')
        require(all(isinstance(s,dict) and s.get('account_status')=='active'
            and s.get('extra_currencies') in (None,{}) for s in (before_state,after)))
        before=nano(before_state.get('balance'));net=nano(after.get('balance'))-before
        require(0<net<=value and value-net==nano(tx.get('total_fees')),'Incoming balance/fees inconsistent')
        state='active'
    require(isinstance(tx.get('lt'),str) and re.fullmatch('0|[1-9][0-9]{0,19}',tx['lt']) and int(tx['lt'])<2**64)
    return {'tx_hash':_hash(tx.get('hash')),'message_hash':_hash(msg.get('hash')),'lt':tx['lt'],
        'source':source,'address':address,'net_credit_nano':str(net),'previous_balance_nano':str(before),
        'recipient_state':state,'verification':'provider_reported_not_chain_proof'}


async def read_deposit(address,created,*,clock,tx_hash=None,client=None):
    async def read(http):
        all_rows=[]
        seen_keys=set()
        for page in range(5):
            params={'account':address,'start_utime':int(created),'end_utime':int(clock())+15,'limit':100,'sort':'asc'}
            if page>0: params['offset']=page*100
            if tx_hash: params['hash']=_hash(tx_hash)
            response=await bounded_json(http,'GET','https://toncenter.com/api/v3/transactions',params=params)
            rows=response.get('transactions') if isinstance(response,dict) else None
            require(isinstance(rows,list),'Missing/truncated incoming page; specify exact transaction hash')
            page_keys={(tx.get('hash'),tx.get('lt')) for tx in rows if isinstance(tx,dict)}
            if page>0 and page_keys and page_keys.issubset(seen_keys):
                require(False,'Missing/truncated incoming page; specify exact transaction hash')
            seen_keys.update(page_keys)
            all_rows.extend(rows)
            if len(rows)<100:
                break
            require(page<4,'Missing/truncated incoming page; specify exact transaction hash')
            if client is None: await asyncio.sleep(1.1)
        incoming=[]
        for tx in all_rows:
            require(isinstance(tx,dict) and normalize_address(tx.get('account'),'mainnet').raw==address)
            if tx_hash: require(_hash(tx.get('hash'))==_hash(tx_hash),'Provider returned another transaction')
            msg=tx.get('in_msg')
            if isinstance(msg,dict) and msg.get('source') is not None and nano(msg.get('value'))>0:
                incoming.append(tx)
        if not incoming:
            return None
        if len(incoming)==1:
            return {**incoming_credit(incoming[0],address,created,clock()),'deposit_count':1}
        credits=[incoming_credit(tx,address,created,clock()) for tx in incoming]
        require(len({c['tx_hash'] for c in credits})==len(credits)
            and len({c['message_hash'] for c in credits})==len(credits)
            and all(int(a['lt'])<int(b['lt']) for a,b in zip(credits,credits[1:]))
            and all(int(a['previous_balance_nano'])+int(a['net_credit_nano'])==int(b['previous_balance_nano']) for a,b in zip(credits,credits[1:])),
            'Several incoming transfers found; specify the funding transaction hash')
        total_net=sum(int(c['net_credit_nano']) for c in credits)
        last=credits[-1]
        return {**last,'previous_balance_nano':credits[0]['previous_balance_nano'],'net_credit_nano':str(total_net),'deposit_count':len(credits)}
    async with asyncio.timeout(45):
        if client is not None: return await read(client)
        last_err=None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(20,connect=10),trust_env=False,follow_redirects=False) as http:
                    return await read(http)
            except (httpx.TransportError,TimeoutError,MoneyReadError) as exc:
                if isinstance(exc,MoneyReadError) and not exc.retryable:
                    raise
                last_err=exc
                if attempt<2: await asyncio.sleep(1.5*(attempt+1))
        raise last_err


class CarouselFunding:
    def __init__(self,money,*,client_factory=create_client,deposit_reader=read_deposit):
        self.money,self.factory,self.reader=money,client_factory,deposit_reader
        with money.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS money_funding_drafts(id TEXT PRIMARY KEY,revision TEXT NOT NULL,ui_context TEXT NOT NULL,nodes TEXT NOT NULL,created REAL NOT NULL,state TEXT NOT NULL,address TEXT,event TEXT,error TEXT,lease TEXT,due REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS money_funding_claims(account TEXT PRIMARY KEY,draft TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS money_funding_refs(message_hash TEXT PRIMARY KEY,tx_hash TEXT UNIQUE NOT NULL,draft TEXT UNIQUE NOT NULL,address TEXT NOT NULL,lt TEXT NOT NULL,UNIQUE(address,lt))')

    def _nodes(self,ids,scope_ids,ui_context):
        require(isinstance(ids,(list,tuple)) and 2<=len(ids)<=502)
        unique=ids[:-1] if ids[-1]==ids[0] else ids
        require(len(set(unique))==len(unique) and all(a!=b for a,b in zip(ids,ids[1:])))
        nodes=[]
        for ident in ids:
            context,_=self.money.scope(ident,scope_ids,ui_context)
            account=self.money.accounts.get_account(ident);username=account.username
            require(isinstance(username,str) and re.fullmatch('[A-Za-z][A-Za-z0-9_]{4,31}',username),'Every route account needs a current username')
            nodes.append({'id':ident,'user_id':account.user_id,'username':username,'context':context})
        checked=nodes[:-1] if ids[-1]==ids[0] else nodes
        require(len({n['user_id'] for n in checked})==len(checked)
            and len({n['username'].lower() for n in checked})==len(checked),'Aliased Telegram users/usernames in route')
        return nodes

    def _load(self,db,ident,scope_ids,ui_context,revision=None):
        row=db.execute('SELECT * FROM money_funding_drafts WHERE id=?',(ident,)).fetchone()
        require(row is not None and row['ui_context']==ui_context)
        if revision is not None: require(row['revision']==revision,'Draft revision changed; reload')
        nodes=json.loads(row['nodes'])
        require(self._nodes([n['id'] for n in nodes],scope_ids,ui_context)==nodes,'Route identity/username/group/filter changed')
        return dict(row),nodes

    def report(self,ident,*,scope_ids,ui_context):
        with self.money.db() as db:
            found=db.execute('SELECT * FROM money_funding_drafts WHERE id=?',(ident,)).fetchone()
            require(found is not None);row=dict(found);nodes=json.loads(row['nodes'])
            linked=None
            if row['event'] and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_transit_claims'").fetchone():
                claim=db.execute('SELECT intent FROM native_transit_claims WHERE message_hash=?',(json.loads(row['event'])['message_hash'],)).fetchone()
                linked=claim['intent'] if claim else None
        require(all(n['id'] in scope_ids for n in nodes),'Draft outside current scope')
        for node in nodes: self.money.scope(node['id'],scope_ids,ui_context)
        try: mismatch=row['ui_context']!=ui_context or self._nodes([n['id'] for n in nodes],scope_ids,ui_context)!=nodes
        except ValueError: mismatch=True
        return {'id':ident,'revision':row['revision'],'route':nodes,'created':row['created'],'state':row['state'],
            'fund_to':None if mismatch or linked else '@'+nodes[0]['username'],'fund_user_id':nodes[0]['user_id'],'address':row['address'],
            'observation':json.loads(row['event']) if row['event'] else None,'error':row['error'],
            'stale':bool(row['error']) or mismatch or bool(linked),'stale_context':mismatch,'linked_intent':linked,
            'executor_supported':False,'spend_authorized':False,'automatic_retry':False}

    def latest(self,*,scope_ids,ui_context):
        with self.money.db() as db:
            rows=db.execute("SELECT id FROM money_funding_drafts WHERE state NOT IN ('cancelled','completed') ORDER BY created DESC LIMIT 100").fetchall()
        for row in rows:
            try: return self.report(row['id'],scope_ids=scope_ids,ui_context=ui_context)
            except (ValueError,KeyError): continue
        return None

    def create(self,ids,*,scope_ids,ui_context,confirmed=False):
        require(confirmed is True,'Confirm the exact finite route; no wallet operation is performed')
        nodes=self._nodes(ids,scope_ids,ui_context);ident,revision=uuid.uuid4().hex,uuid.uuid4().hex
        with self.money.db() as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='money_carousels'").fetchone():
                plans=db.execute("SELECT topology FROM money_carousels WHERE state NOT IN ('cancelled','completed') LIMIT 1001").fetchall()
                require(len(plans)<=1000,'Plan scan exceeds bound')
                require(not any(set(ids)&{n['account_id'] for n in json.loads(p['topology'])} for p in plans),
                    'Accounts already belong to an unfinished carousel plan; review it before funding')
            for account in set(ids):
                require(db.execute('SELECT 1 FROM money_funding_claims WHERE account=?',(account,)).fetchone() is None,'Account already belongs to a funding draft; review it first')
            db.execute('INSERT INTO money_funding_drafts VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (ident,revision,ui_context,json.dumps(nodes),self.money.clock(),'awaiting_funding',None,None,None,None,0))
            db.executemany('INSERT INTO money_funding_claims VALUES(?,?)',[(account,ident) for account in set(ids)])
        return self.report(ident,scope_ids=scope_ids,ui_context=ui_context)

    def cancel(self,ident,revision,*,scope_ids,ui_context,confirmation):
        require(confirmation=='CANCEL '+ident)
        with self.money.db() as db:
            row=db.execute('SELECT * FROM money_funding_drafts WHERE id=?',(ident,)).fetchone()
            require(row and row['revision']==revision,'Draft revision changed')
            # Closing stale metadata is not permission to transfer/refund funds.
            for node in json.loads(row['nodes']): self.money.scope(node['id'],scope_ids,ui_context)
            db.execute("UPDATE money_funding_drafts SET state='cancelled',revision=?,lease=NULL WHERE id=?",(uuid.uuid4().hex,ident))
            db.execute('DELETE FROM money_funding_claims WHERE draft=?',(ident,))
        return self.report(ident,scope_ids=scope_ids,ui_context=ui_context)

    async def check(self,ident,revision,*,scope_ids,ui_context,confirmed=False,tx_hash=None,scope_check=lambda:True):
        require(confirmed is True and scope_check(),'Confirm read-only funding verification')
        require(not self.report(ident,scope_ids=scope_ids,ui_context=ui_context).get('linked_intent'),
            'Funding already linked to a native intent; reconcile it, do not fund/check as a new payment')
        if tx_hash: tx_hash=_hash(tx_hash)
        lease=uuid.uuid4().hex
        with self.money.db() as db:
            row,nodes=self._load(db,ident,scope_ids,ui_context,revision)
            require(row['state']!='cancelled' and row['due']<=self.money.clock(),'Check already active or cooling down; retry explicitly later')
            db.execute('UPDATE money_funding_drafts SET lease=?,due=? WHERE id=?',(lease,self.money.clock()+90,ident))
        def guard():
            require(scope_check())
            with self.money.db() as db:
                current,_=self._load(db,ident,scope_ids,ui_context,revision)
                require(current['lease']==lease and current['due']>self.money.clock() and current['state']!='cancelled')
        source=nodes[0]
        try:
            async with asyncio.timeout(65):
                guard()
                state=None
                if self.factory is create_client:
                    try:
                        async with asyncio.timeout(18):
                            async with self.factory(self.money.config,self.money.accounts.get_account(source['id'])) as client:
                                me=await client.get_me();guard()
                                require(me.get('user_id')==source['user_id'] and me.get('username')==source['username'],'Live funding username/user ID changed; no verification')
                                state=await client.native_money_state();guard()
                    except (FundingError, asyncio.CancelledError):
                        raise
                    except Exception:
                        state=None
                    if not state or state.get('availability')!='ready':
                        try:
                            from modules.financial_vault import FinancialVault
                            inv=FinancialVault(self.money.config.data_dir).status()
                            rec=next((r for r in inv.get('records',[]) if r.get('account')==source['id'] and r.get('network')=='mainnet'),None)
                            if rec and rec.get('address'):
                                state={'availability':'ready','address':rec['address']}
                        except Exception:
                            pass
                    if state is None:
                        raise ValueError('Funding wallet state unavailable')
                else:
                    async with self.factory(self.money.config,self.money.accounts.get_account(source['id'])) as client:
                        me=await client.get_me();guard()
                        require(me.get('user_id')==source['user_id'] and me.get('username')==source['username'],'Live funding username/user ID changed; no verification')
                        state=await client.native_money_state();guard()
                event=None;address=None
                if state.get('availability')=='ready':
                    address=normalize_address(state.get('address'),'mainnet').raw
                    require(row['address'] is None or row['address']==address,'Funding wallet address changed')
                    previous=json.loads(row['event']) if row['event'] else None
                    event=await self.reader(address,row['created'],clock=self.money.clock,tx_hash=tx_hash);guard()
                else:
                    require(state.get('availability') in {'missing','provisioning'})
                    require(row['address'] is None,'Previously known funding wallet disappeared; review before further deposits')
                if event:
                    require(event.get('verification')=='provider_reported_not_chain_proof' and event.get('address')==address)
                    require(nano(event['net_credit_nano'],positive=True)>0)
                with self.money.db() as db:
                    current,_=self._load(db,ident,scope_ids,ui_context,revision)
                    require(current['lease']==lease and current['due']>self.money.clock() and scope_check())
                    if event:
                        refs=db.execute('SELECT draft FROM money_funding_refs WHERE message_hash=? OR tx_hash=? OR (address=? AND lt=?)',(event['message_hash'],event['tx_hash'],address,event['lt'])).fetchall()
                        require(all(ref['draft']==ident for ref in refs),'Funding event already referenced by another draft')
                        old=json.loads(row['event']) if row['event'] else None
                        require(
                            old is None
                            or old==event
                            or (
                                int(event.get('deposit_count',1))>int(old.get('deposit_count',1))
                                and int(event['net_credit_nano'])>int(old['net_credit_nano'])
                                and event.get('address')==old.get('address')
                            ),
                            'Funding facts changed; manual review required',
                        )
                        require(db.execute("SELECT 1 FROM intents i JOIN reconciliation r ON r.intent=i.id WHERE i.destination=? AND json_extract(r.evidence,'$.message_hash')=? LIMIT 1",(address,event['message_hash'])).fetchone() is None,
                            'Existing carousel/tool transit cannot be re-counted as external funding')
                        db.execute('DELETE FROM money_funding_refs WHERE draft=?',(ident,))
                        db.execute('INSERT OR IGNORE INTO money_funding_refs VALUES(?,?,?,?,?)',(event['message_hash'],event['tx_hash'],ident,address,event['lt']))
                    status='funding_observed' if event else 'needs_review' if row['event'] else 'awaiting_funding'
                    db.execute('UPDATE money_funding_drafts SET revision=?,state=?,address=COALESCE(?,address),event=COALESCE(?,event),error=?,lease=NULL,due=? WHERE id=?',
                        (uuid.uuid4().hex,status,address,json.dumps(event) if event else None,'Previously observed transfer not found' if row['event'] and not event else None,self.money.clock()+10,ident))
            return self.report(ident,scope_ids=scope_ids,ui_context=ui_context)
        except BaseException as exc:
            with self.money.db() as db:
                db.execute('UPDATE money_funding_drafts SET error=?,lease=NULL,due=? WHERE id=? AND lease=?',
                    (type(exc).__name__,self.money.clock()+30,ident,lease))
            if isinstance(exc,asyncio.CancelledError): raise
            if not isinstance(exc,Exception): raise
            raise FundingError('Funding verification failed ('+type(exc).__name__+'). No transfer or automatic retry; prior observation remains stale') from None
