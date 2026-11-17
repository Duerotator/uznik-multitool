import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Expired native nonce review: synthetic rows/public snapshots, no signing/RPC."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock
import unittest

import test_money_execution as fixture
from modules.money_execution import NativeExecutionError
from modules.money_recovery import NativeNonceRecovery


@unittest.skipUnless(fixture.AVAILABLE,'Pinned codec missing; not scoped acceptance')
class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.ExecutionTests.asyncSetUp(self)
        self.now=1241;self.ident='d'*32
        with self.money.db() as db:
            db.execute('INSERT INTO intents(id,account,context,ui_context,network,source,destination,amount,created,expires,state,prompted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (self.ident,'one',self.context,self.ui,'mainnet',self.source,self.target,'190000000',1000,1180,'uncertain',1000))
            db.execute('INSERT INTO native_commitments VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (self.ident,'mainnet',self.source,7,'200000000','p','r',self.identity['revision'],123,'dispatching','a'*64))
        first={**self.observed,'checked_at':1241,'block_time':1240,'mc_block_seqno':100}
        self.snapshots=[first,{**first,'block_id':'e'*64,'mc_block_seqno':101,'block_time':1241}]
        self.reader.read=AsyncMock(side_effect=lambda *a,**k:copy.deepcopy(self.snapshots.pop(0)))
        self.recovery=NativeNonceRecovery(self.money,self.reader)

    async def asyncTearDown(self): await fixture.ExecutionTests.asyncTearDown(self)
    async def recover(self,**changes):
        return await self.recovery.recover(self.identity,self.public,self.ident,**{'scope_ids':{'one','two'},
            'ui_context':self.ui,'confirmation':'RECOVER '+self.ident,**changes})
    def state(self):
        with self.money.db() as db:
            return tuple(db.execute('SELECT i.state,n.seqno,n.stage,n.max_debit FROM intents i JOIN native_commitments n ON n.intent=i.id WHERE i.id=?',(self.ident,)).fetchone())

    async def test_expired_observed_unused_claim_released_but_budget_and_audit_retained(self):
        reads=self.vault.protector.reads;result=await self.recover()
        self.assertEqual(self.state(),('operator_not_sent',None,'expired_unused_nonce_observed','200000000'))
        self.assertEqual(self.vault.protector.reads,reads);self.assertEqual(self.client.native_money_send.await_count,0)
        self.assertFalse(result['automatic_retry']);self.assertEqual(result['verification'],'provider_reported_not_chain_proof')
        with self.money.db() as db:
            self.assertEqual(self.money.reserved_debit(db,self.source,'mainnet'),200000000)
            evidence=json.loads(db.execute('SELECT evidence FROM reconciliation').fetchone()[0])
            self.assertEqual(evidence['nonce'],7);self.assertEqual(len(evidence['observations']),2)
        with self.assertRaises(NativeExecutionError): await self.recover()

    async def test_not_expired_or_wrong_phrase_never_reads_provider(self):
        with self.assertRaises(NativeExecutionError): await self.recover(confirmation='yes')
        self.now=1240
        with self.assertRaises(NativeExecutionError): await self.recover()
        self.assertEqual(self.reader.read.await_count,0)

    async def test_advanced_nonce_or_stale_chain_does_not_clear_claim(self):
        self.snapshots[0]['seqno']=8
        with self.assertRaises(NativeExecutionError): await self.recover()
        self.assertEqual(self.state()[1],7)

    async def test_same_masterchain_snapshot_not_accepted_as_two_observations(self):
        self.snapshots[1]=copy.deepcopy(self.snapshots[0])
        with self.assertRaises(NativeExecutionError): await self.recover()
        self.assertEqual(self.state()[1],7)

    async def test_existing_positive_or_ambiguous_evidence_is_not_overridden(self):
        for status in ('source_pending','recipient_pending','ambiguous','provider_delivered'):
            with self.money.db() as db:
                db.execute('INSERT OR REPLACE INTO reconciliation VALUES(?,?,?)',(self.ident,1240,json.dumps({'status':status,'message_hash':'b'*64})))
            with self.subTest(status=status),self.assertRaises(NativeExecutionError): await self.recover()
        self.assertEqual(self.reader.read.await_count,0)

    async def test_cas_conflict_keeps_nonce_claimed(self):
        async def observe(*args,**kwargs):
            if len(self.snapshots)==1:
                with self.money.db() as db: db.execute("UPDATE intents SET state='provider_delivered' WHERE id=?",(self.ident,))
            return self.snapshots.pop(0)
        self.reader.read.side_effect=observe
        with self.assertRaises(NativeExecutionError): await self.recover()
        self.assertEqual(self.state()[1],7)

    async def test_closed_scope_or_cancel_does_not_mutate_journal(self):
        with self.assertRaises(NativeExecutionError): await self.recover(scope_check=lambda:False)
        self.reader.read.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.recover()
        self.assertEqual(self.state(),('uncertain',7,'dispatching','200000000'))

    async def test_pre_expiry_block_even_with_fast_local_clock_is_rejected(self):
        self.snapshots[0]['block_time']=1181
        with self.assertRaises(NativeExecutionError): await self.recover()
        self.assertEqual(self.state()[1],7)

    async def test_clock_rollback_during_reads_cannot_shorten_expiry_margin(self):
        async def observe(*args,**kwargs):
            if len(self.snapshots)==1:
                self.now=1230;self.snapshots[0]['checked_at']=1230
            return self.snapshots.pop(0)
        self.reader.read.side_effect=observe
        with self.assertRaises(NativeExecutionError): await self.recover()
        self.assertEqual(self.state()[1],7)

    async def test_later_positive_reconciliation_can_correct_observation_and_retains_audit(self):
        await self.recover()
        self.money.reconciliation=AsyncMock(return_value={'status':'provider_delivered',
            'provider':'toncenter_v3','verification':'provider_reported_not_chain_proof',
            'source_tx':'1'*64,'recipient_tx':'2'*64,'message_hash':'3'*64,'automatic_retry':False})
        result=await self.money.action('reconcile',{'account_id':'one','id':self.ident,'source':self.source,
            'network':'mainnet','confirmed':True},scope_ids={'one','two'},ui_context=self.ui)
        self.assertEqual(result['state'],'provider_delivered')
        self.assertEqual(result['reconciliation']['prior_nonce_review']['nonce'],7)
        self.assertEqual(self.state()[1],None)
        self.assertEqual(self.client.native_money_send.await_count,0)
