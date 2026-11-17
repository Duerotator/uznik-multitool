"""Explicit expired/observed-unused nonce recovery. No signing or resubmission.

This is provider-reported operator review, not a cryptographic not-sent proof.
Reservations stay charged; the original intent is never replayed automatically.
"""
import asyncio
import json

from modules.money_execution import require
from modules.wallet_tg_codec import CODE_HASH,REV00_HASH,SUBWALLET
from modules.ton_readonly import _unique


class NativeNonceRecovery:
    def __init__(self,money,reader): self.money,self.reader=money,reader

    async def recover(self,identity,anchor,intent_id,*,scope_ids,ui_context,confirmation,scope_check=lambda:True):
        require(confirmation=='RECOVER '+intent_id,'Exact RECOVER intent confirmation required')
        source=identity['account'];network=identity['network']
        require(network=='mainnet')
        context,_=self.money.scope(source,scope_ids,ui_context)
        require(identity['context']==context)
        with self.money.db() as db:
            intent=db.execute('SELECT * FROM intents WHERE id=? AND account=?',(intent_id,source)).fetchone()
            native=db.execute('SELECT * FROM native_commitments WHERE intent=?',(intent_id,)).fetchone()
            rec=db.execute('SELECT * FROM reconciliation WHERE intent=?',(intent_id,)).fetchone()
            require(intent and native and type(native['seqno']) is int,'No native nonce claim to recover')
            intent,native=dict(intent),dict(native);prior=dict(rec) if rec else None
            require(intent['state'] in {'uncertain','wallet_submitted_unverified','operator_not_sent'}
                and native['stage'] in {'signing','dispatching','submitted'}
                and intent['context']==context and intent['network']==network
                and intent['source']==native['source']==identity['address'] and native['network']==network
                and intent['receipt_hash'] is None,'Intent identity/state/receipt requires reconciliation, not nonce recovery')
            require(self.money.clock()>intent['expires']+60,'Wait for validity expiry + 60 seconds')
            if prior:
                evidence=json.loads(prior['evidence'],object_pairs_hook=_unique)
                require(isinstance(evidence,dict) and evidence.get('status')=='not_found'
                    and not any(evidence.get(k) for k in ('message_hash','source_tx','recipient_tx')),
                    'Existing transaction evidence is not overridden; reconcile the original intent')

        def check():
            require(scope_check() and self.money.scope(source,scope_ids,ui_context)[0]==context)
        def validate(observation):
            require(isinstance(observation,dict) and observation.get('network')==network
                and observation.get('address')==identity['address'] and observation.get('public_key')==identity['public_key']
                and observation.get('active') is True and observation.get('code_hash')==CODE_HASH
                and observation.get('implementation_hash')==REV00_HASH
                and observation.get('subwallet_id')==SUBWALLET[network]
                and observation.get('provider')=='toncenter' and observation.get('verification')=='provider_reported_not_chain_proof'
                and observation.get('coherence')=='queries_pinned_by_mc_seqno','Unsupported recovery observation')
            require(type(observation.get('seqno')) is int and observation['seqno']==native['seqno'],
                'Nonce advanced or state is behind; original payment needs reconciliation')
            require(type(observation.get('mc_block_seqno')) is int and 0<=observation['mc_block_seqno']<2**31)
            require(type(observation.get('block_time')) is int and observation['block_time']>=intent['expires']+30
                and -15<=self.money.clock()-observation['block_time']<=120,'Chain tip is not safely beyond request expiry')
            require(type(observation.get('checked_at')) in (int,float) and 0<=self.money.clock()-observation['checked_at']<=30)
            block=observation.get('block_id')
            require(isinstance(block,str) and len(block)==64 and all(c in '0123456789abcdef' for c in block))
            return {k:observation[k] for k in ('seqno','mc_block_seqno','block_id','block_time')}
        check()
        async with asyncio.timeout(140):
            first=validate(await self.reader.read(identity['address'],network,anchor,confirmed=True));check()
            second=validate(await self.reader.read(identity['address'],network,anchor,confirmed=True));check()
        require(second['mc_block_seqno']>first['mc_block_seqno'] and second['block_id']!=first['block_id']
            and second['block_time']>=first['block_time'],'Need two advancing masterchain observations; retry the READ explicitly later')
        require(self.money.clock()>intent['expires']+60,'Local clock changed during recovery; review again')
        result={'status':'expired_unused_nonce_observed','verification':'provider_reported_not_chain_proof',
            'provider':'toncenter_pinned_getters','intent':intent_id,'nonce':native['seqno'],
            'observations':[first,second],'automatic_retry':False,'not_a_delivery_receipt':True}
        with self.money.db() as db:
            current=db.execute('SELECT * FROM intents WHERE id=?',(intent_id,)).fetchone()
            current_native=db.execute('SELECT * FROM native_commitments WHERE intent=?',(intent_id,)).fetchone()
            current_rec=db.execute('SELECT * FROM reconciliation WHERE intent=?',(intent_id,)).fetchone()
            require(current and dict(current)==intent and current_native and dict(current_native)==native
                and (dict(current_rec) if current_rec else None)==prior,'Journal changed during recovery; stale result discarded')
            db.execute("UPDATE intents SET state='operator_not_sent' WHERE id=?",(intent_id,))
            db.execute("UPDATE native_commitments SET seqno=NULL,stage='expired_unused_nonce_observed' WHERE intent=?",(intent_id,))
            db.execute('INSERT OR REPLACE INTO reconciliation VALUES(?,?,?)',(intent_id,self.money.clock(),json.dumps(result,sort_keys=True)))
        return result
