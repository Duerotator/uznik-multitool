"""Native incoming candidates from explicit, untrusted v3 snapshot review.

No provider requests, reward proof, balance, signer, redemption or sweep. Dedup
metadata uses the existing Money database; payment truth stays in its journals.
"""
import hashlib
import json
import re

from core.ton_address import normalize_address
from modules.finance_money import FinanceError
from modules.money_policy import digest
from modules.money_reconciliation import _hash, _final, _executed
from modules.ton_readonly import _unique, _unsigned, ReadError


class RewardCandidateError(ValueError):
    pass


def require(condition,message='Incomplete/conflicting incoming snapshot; review required'):
    if not condition: raise RewardCandidateError(message)


def parse_snapshot(raw,address,network,now):
    require(isinstance(raw,bytes) and len(raw)<=1024*1024,'Snapshot size/type unsupported')
    data=json.loads(raw,object_pairs_hook=_unique)
    require(isinstance(data,dict) and isinstance(data.get('transactions'),list) and len(data['transactions'])<=100)
    result=[]
    for tx in data['transactions']:
        require(isinstance(tx,dict) and normalize_address(tx.get('account'),network).raw==address)
        tx_hash=_hash(tx.get('hash'));lt=_unsigned(tx.get('lt'),bits=64)
        require(type(tx.get('now')) is int and 0<=tx['now']<=now+15)
        msg=tx.get('in_msg');out=tx.get('out_msgs')
        require(isinstance(msg,dict) and isinstance(out,list) and len(out)<=256)
        source=normalize_address(msg['source'],network).raw if msg.get('source') is not None else None
        require(normalize_address(msg.get('destination'),network).raw==address)
        amount='0' if source is None and msg.get('value') is None else _unsigned(msg.get('value'))
        content=msg.get('message_content');require(isinstance(content,dict))
        body_hash=_hash(content.get('hash'));decoded=content.get('decoded')
        comment=decoded.get('comment') if isinstance(decoded,dict) else None
        # Never store arbitrary messages, BOC, names or a supposed prize text.
        require(comment is None or isinstance(comment,str) and len(comment)<=4096)
        reference=None
        if isinstance(comment,str) and re.fullmatch('accmanager:[a-f0-9]{32}',comment): reference=comment[11:]
        facts={'tx_hash':tx_hash,'message_hash':_hash(msg.get('hash')),'lt':lt,'time':tx['now'],
               'source':source,'destination':address,'amount_nano':amount,'body_hash':body_hash,
               'comment_digest':hashlib.sha256(comment.encode()).hexdigest() if comment is not None else None,
               'intent_reference':reference}
        native=msg.get('value_extra_currencies') in (None,{}) and tx.get('total_fees_extra_currencies') in (None,{})
        known_body=(type(msg.get('opcode')) is int and msg['opcode']==0 and isinstance(decoded,dict)
                    and decoded.get('type')=='ton_transfer' and decoded.get('encrypted') is False)
        desc=tx.get('description')
        safe=(_final(tx) and _executed(tx) and isinstance(desc,dict) and desc.get('destroyed') is False
              and msg.get('bounced') is False and not out and known_body and native)
        reason=('external_control' if source is None else 'zero_value' if int(amount)==0 else
                'bounce_refund' if msg.get('bounced') is True else 'unsupported_asset' if not native else
                'unverified_incoming_candidate' if safe else 'needs_review')
        result.append((facts,reason))
    return result,len(data['transactions'])==100


class NativeIncomingCandidates:
    def __init__(self,money):
        self.money=money
        with money.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS money_incoming_observations(network TEXT NOT NULL, address TEXT NOT NULL, message_hash TEXT NOT NULL, tx_hash TEXT NOT NULL, lt TEXT NOT NULL, facts TEXT NOT NULL, first_seen REAL NOT NULL, PRIMARY KEY(network,address,message_hash), UNIQUE(network,address,tx_hash), UNIQUE(network,address,lt))')

    def _own_evidence(self,db,network,own_addresses):
        own=set(own_addresses);references={};messages={}
        # Bounded historical journal scan. Unknown overflow is never an empty ledger.
        rows=db.execute('SELECT i.id,i.source,i.destination,i.amount,r.evidence FROM intents i LEFT JOIN reconciliation r ON i.id=r.intent WHERE i.network=? LIMIT 10001',(network,)).fetchall()
        require(len(rows)<=10000,'Journal exclusion scan exceeds bound; candidates unavailable')
        for row in rows:
            source=normalize_address(row['source'],network).raw
            destination=normalize_address(row['destination'],network).raw
            own.add(source);references[row['id']]=(source,destination,row['amount'])
            if row['evidence'] is not None:
                evidence=json.loads(row['evidence'],object_pairs_hook=_unique)
                require(isinstance(evidence,dict))
                if 'message_hash' in evidence:
                    key=_hash(evidence['message_hash'])
                    require(key not in messages or messages[key]==(source,destination),'Conflicting journal message exclusion')
                    messages[key]=(source,destination)
        # Retain completed/cancelled plan declarations too: releasing a metadata
        # lease does not turn the terminal wallet into an unrelated reward sender.
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='money_carousels'").fetchone()
        if exists:
            plans=db.execute('SELECT topology FROM money_carousels WHERE network=? LIMIT 1001',(network,)).fetchall()
            require(len(plans)<=1000,'Plan exclusion scan exceeds bound')
            for row in plans:
                require(isinstance(row['topology'],str) and len(row['topology'])<=65536)
                topology=json.loads(row['topology'],object_pairs_hook=_unique)
                require(isinstance(topology,list) and 2<=len(topology)<=52)
                own.update(normalize_address(node['address'],network).raw for node in topology)
        return own,references,messages

    def review(self,raw,*,account_id,address,network,scope_ids,ui_context,own_addresses=(),confirmed=False):
        require(confirmed is True,'Explicit local snapshot review required; no implicit network reads')
        require(network in ('mainnet','testnet') and isinstance(ui_context,str) and re.fullmatch('[a-f0-9]{64}',ui_context))
        require(isinstance(own_addresses,(list,tuple,set)) and len(own_addresses)<=1000)
        try:
            address=normalize_address(address,network).raw
            declared={normalize_address(value,network).raw for value in own_addresses}
            context,ui=self.money.scope(account_id,scope_ids,ui_context)
            facts,truncated=parse_snapshot(raw,address,network,self.money.clock())
            result=[];page_messages={};page_transactions={}
            with self.money.db() as db:
                own,references,messages=self._own_evidence(db,network,declared|{address})
                for event,reason in facts:
                    key=event['message_hash'];tx=event['tx_hash'];fingerprint=digest(event)
                    if key in page_messages:
                        require(page_messages[key]==(fingerprint,reason),'Conflicting duplicate message within snapshot')
                        continue
                    require(tx not in page_transactions,'One receiving transaction has conflicting message identities')
                    page_messages[key]=(fingerprint,reason);page_transactions[tx]=key
                    stored=db.execute('SELECT facts FROM money_incoming_observations WHERE network=? AND address=? AND message_hash=?',(network,address,key)).fetchone()
                    other=db.execute('SELECT message_hash FROM money_incoming_observations WHERE network=? AND address=? AND tx_hash=?',(network,address,tx)).fetchone()
                    require(other is None or other['message_hash']==key,'Transaction identity changed across snapshots')
                    same_lt=db.execute('SELECT message_hash FROM money_incoming_observations WHERE network=? AND address=? AND lt=?',(network,address,event['lt'])).fetchone()
                    require(same_lt is None or same_lt['message_hash']==key,'Receiving transaction LT reused with a different identity')
                    if stored:
                        require(json.loads(stored['facts'],object_pairs_hook=_unique)==event,'Message facts changed across snapshots')
                    else:
                        db.execute('INSERT INTO money_incoming_observations VALUES(?,?,?,?,?,?,?)',
                            (network,address,key,tx,event['lt'],json.dumps(event,sort_keys=True),self.money.clock()))
                    # Recomputed from current journals on every review; no cached
                    # "reward" flag survives newly discovered own transit evidence.
                    if key in messages:
                        reason='own_transit_excluded' if messages[key]==(event['source'],address) else 'journal_conflict_needs_review'
                    elif event['source'] in own: reason='own_sender_excluded'
                    elif event['intent_reference']:
                        expected=references.get(event['intent_reference'])
                        reason='own_transit_excluded' if expected==(event['source'],address,event['amount_nano']) else 'reserved_comment_needs_review'
                    result.append({k:event[k] for k in ('tx_hash','message_hash','source','destination','amount_nano')}|
                        {'observation_id':digest([network,address,key]),'seen_before':stored is not None,'classification':reason,
                         'candidate':reason=='unverified_incoming_candidate','reward_proven':False,'spend_authorized':False})
                require(self.money.scope(account_id,scope_ids,ui_context)==(context,ui),'Account context changed during local review')
            return {'account_id':account_id,'context':context,'ui_context':ui,'network':network,'address':address,'events':result,
                'verification':'caller_supplied_provider_snapshot_not_chain_proof','history_complete':False,
                'page_at_limit':truncated,'native_currency':'TON','reward_proven':False,'collection_supported':False,
                'provider_request_performed':False,'automatic_retry':False}
        except RewardCandidateError: raise
        except (ValueError,TypeError,KeyError,ReadError,RecursionError,FinanceError):
            raise RewardCandidateError('Invalid journal/snapshot/scope; no reward or collection authorization') from None
