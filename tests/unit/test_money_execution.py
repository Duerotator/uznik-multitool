import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Integrated native pipeline with ephemeral unfunded keys/fake transport only."""
import asyncio
import base64
from contextlib import asynccontextmanager
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat

from core.models import AccountRecord
from modules.finance_money import MoneyService,FinanceError
from modules.financial_vault import FinancialVault
from modules.money_policy import make_policy
from modules.money_execution import NativeExecutionError
from modules.money_funding import CarouselFunding
from modules.money_carousel import MoneyCarousel
from modules.money_carousel_run import CarouselStep
from modules.money_send_transport import send_request,transfer_minimum,SEND
from modules.wallet_tg_codec import WalletTgCodec,CODE_HASH,REV00_HASH,SUBWALLET
from test_wallet_tg_codec import Protector,AVAILABLE,HELPER


@unittest.skipUnless(AVAILABLE,'Pinned codec missing; NOT scoped acceptance')
class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=1000;self.seed=os.urandom(32)
        self.public=Ed25519PrivateKey.from_private_bytes(self.seed).public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
        self.codec=WalletTgCodec(clock=lambda:self.now);self.source=self.codec.derive(self.public,'mainnet')['source']
        self.target='0:'+'02'*32;self.ui='b'*64
        self.accounts={key:AccountRecord(key,'Synthetic','pyrogram','session_file','never-open.session',user_id=i,username=key+'fixture')
            for i,key in enumerate(('one','two'),start=1)}
        self.money=MoneyService(SimpleNamespace(data_dir=Path(self.temp.name)),accounts=SimpleNamespace(get_account=self.accounts.__getitem__),clock=lambda:self.now)
        self.context,_=self.money.scope('one',{'one','two'},self.ui)
        self.vault=FinancialVault(self.temp.name,protector=Protector());self.vault.create(confirmed=True)
        self.identity={'account':'one','user_id':1,'context':self.context,'network':'mainnet','address':self.source,'public_key':self.public}
        self.identity['revision']=self.vault.put(**self.identity,seed=self.seed,confirmed=True)
        self.policy=make_policy(account='one',account_context=self.context,ui_context=self.ui,network='mainnet',source=self.source,
            targets=[self.target],scope='all_native_authorized',reserve_nano='0',fee_cap_nano='10000000',
            transfer_debit_cap_nano='200000000',daily_reservation_cap_nano='1000000000',confirmed=True,all_native_confirmed=True)
        self.observed={'address':self.source,'network':'mainnet','public_key':self.public,'seqno':7,'balance_nano':'500000000',
            'data_hash':'a'*64,'code_hash':CODE_HASH,'implementation_hash':REV00_HASH,'active':True,'checked_at':1000,
            'mc_block_seqno':99,'block_id':'c'*64,'subwallet_id':SUBWALLET['mainnet'],'provider':'toncenter',
            'verification':'provider_reported_not_chain_proof','coherence':'queries_pinned_by_mc_seqno'}
        self.reader=SimpleNamespace(read=AsyncMock(side_effect=lambda *a,**k:copy.deepcopy(self.observed)))
        self.client=SimpleNamespace(get_me=AsyncMock(return_value={'user_id':1}),
            native_money_state=AsyncMock(return_value={'availability':'ready','address':self.source,'public_key':self.public}),
            native_money_minimum=AsyncMock(return_value='100000000'),
            native_money_recipient=AsyncMock(return_value={'address':self.target}),
            native_money_send=AsyncMock(return_value={'rpc_returned':True}))
        @asynccontextmanager
        async def factory(*args): yield self.client
        async def project(policy,observed,plan,**kwargs):
            unsigned=self.codec.prepare_emulation(network='mainnet',anchor_public_key=self.public,seqno=7,valid_until=1180,
                destination=self.target,amount_nano=plan['amount_nano'],intent_id=plan['intent_id'])
            return {'verification':'provider_reported_simulation_not_receipt','quote_hash':plan['quote_hash'],
                'observation_hash':plan['observation_hash'],'unsigned_body_hash':unsigned['unsigned_body_hash'],'checked_at':self.now,
                'estimated_fee_nano':'3000000','source_debit_nano':'192000000','recipient_net_nano':'189000000'}
        self.emulator=SimpleNamespace(project=AsyncMock(side_effect=project))
        self.executor=self.money.native_executor(self.vault,codec=self.codec,reader=self.reader,emulator=self.emulator,client_factory=factory,allow=lambda:True)

    async def asyncTearDown(self): self.temp.cleanup()
    async def execute(self,**changes):
        return await self.executor.execute(self.policy,self.identity,anchor_public_key=self.public,target_account_id='two',
            scope_ids={'one','two'},ui_context=self.ui,**{'confirmed':True,**changes})
    def row(self):
        with self.money.db() as db:
            return dict(db.execute('SELECT * FROM intents ORDER BY created DESC LIMIT 1').fetchone())
    def commitment(self):
        with self.money.db() as db: return dict(db.execute('SELECT * FROM native_commitments').fetchone())

    def funded_transit(self):
        funding=CarouselFunding(self.money)
        report=funding.create(['one','two'],scope_ids=set(self.accounts),ui_context=self.ui,confirmed=True)
        event={'tx_hash':'d'*64,'message_hash':'e'*64,'lt':'1','source':self.target,'address':self.source,
            'net_credit_nano':'200000000','previous_balance_nano':'300000000',
            'recipient_state':'active','verification':'provider_reported_not_chain_proof'}
        with self.money.db() as db:
            db.execute("UPDATE money_funding_drafts SET state='funding_observed',address=?,event=? WHERE id=?",
                (self.source,json.dumps(event),report['id']))
            db.execute('INSERT INTO money_funding_refs VALUES(?,?,?,?,?)',
                (event['message_hash'],event['tx_hash'],report['id'],self.source,event['lt']))
        self.policy=replace(self.policy,scope='transit_only')
        self.observed['last_transaction']={'hash':event['tx_hash'],'lt':'1'}
        original=self.emulator.project.side_effect
        async def project(*args,**kwargs):
            return {**await original(*args,**kwargs),'source_after_nano':'308000000'}
        self.emulator.project.side_effect=project
        return funding,{'id':report['id'],'revision':report['revision']}

    async def test_funding_transit_uses_one_event_and_preserves_old_funds(self):
        _,reference=self.funded_transit()
        result=await self.execute(funding_ref=reference)
        self.assertEqual(result['state'],'wallet_submitted_unverified')
        plan=self.emulator.project.await_args.args[2]
        self.assertEqual(plan['transit_credit_nano'],'200000000')
        self.assertEqual(plan['protected_nano'],'300000000')
        self.assertEqual(self.row()['amount'],'190000000')
        with self.money.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM native_transit_claims').fetchone()[0],1)
            db.execute("UPDATE intents SET state='provider_delivered' WHERE id=?",(result['id'],))
        with self.assertRaises(ValueError): await self.execute(funding_ref=reference)
        self.assertEqual(self.client.native_money_send.await_count,1)

    async def test_same_balance_but_later_transaction_blocks_transit_before_key_access(self):
        _,reference=self.funded_transit()
        self.observed['last_transaction']['hash']='f'*64
        with patch.object(self.vault,'use_seed',side_effect=AssertionError('must not read seed')):
            with self.assertRaises(NativeExecutionError): await self.execute(funding_ref=reference)
        self.assertEqual(self.row()['state'],'operator_not_sent')
        self.client.native_money_send.assert_not_awaited()
        self.observed['last_transaction']['hash']='d'*64
        with self.assertRaises(ValueError): await self.execute(funding_ref=reference)

    async def test_cancelled_funding_or_excess_debit_cannot_spend_old_balance(self):
        funding,reference=self.funded_transit()
        original=self.emulator.project.side_effect
        async def excessive(*args,**kwargs):
            return {**await original(*args,**kwargs),'source_after_nano':'299999999'}
        self.emulator.project.side_effect=excessive
        with patch.object(self.vault,'use_seed',side_effect=AssertionError('must not read seed')):
            with self.assertRaises(NativeExecutionError): await self.execute(funding_ref=reference)
        self.client.native_money_send.assert_not_awaited()
        funding.cancel(reference['id'],reference['revision'],scope_ids=set(self.accounts),ui_context=self.ui,
            confirmation='CANCEL '+reference['id'])
        with self.assertRaises(ValueError): await self.execute(funding_ref=reference)

    async def test_transit_reference_is_exact_and_not_an_all_funds_override(self):
        _,reference=self.funded_transit()
        with self.assertRaises(ValueError): await self.execute(funding_ref={**reference,'revision':'0'*32})
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.policy=replace(self.policy,scope='all_native_authorized')
        with self.assertRaises(NativeExecutionError): await self.execute(funding_ref=reference)
        with self.money.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM native_transit_claims').fetchone()[0],0)

    async def test_cancellation_during_simulation_rechecks_funding_before_seed(self):
        funding,reference=self.funded_transit();original=self.emulator.project.side_effect
        async def cancel(*args,**kwargs):
            result=await original(*args,**kwargs)
            funding.cancel(reference['id'],reference['revision'],scope_ids=set(self.accounts),ui_context=self.ui,
                confirmation='CANCEL '+reference['id'])
            return result
        self.emulator.project.side_effect=cancel
        with patch.object(self.vault,'use_seed',side_effect=AssertionError('must not read seed')):
            with self.assertRaises(NativeExecutionError): await self.execute(funding_ref=reference)
        self.client.native_money_send.assert_not_awaited()

    def drain_transit(self):
        funding,reference=self.funded_transit()
        with self.money.db() as db:
            event=json.loads(db.execute('SELECT event FROM money_funding_drafts WHERE id=?',(reference['id'],)).fetchone()[0])
            event['previous_balance_nano']='0'
            db.execute('UPDATE money_funding_drafts SET event=? WHERE id=?',(json.dumps(event),reference['id']))
        self.observed['balance_nano']='200000000'
        async def project(policy,observed,plan,**kwargs):
            unsigned=self.codec.prepare_emulation(network='mainnet',anchor_public_key=self.public,seqno=7,valid_until=1180,
                destination=self.target,amount_nano='0',intent_id=plan['intent_id'],send_all=True)
            return {'verification':'provider_reported_simulation_not_receipt','quote_hash':plan['quote_hash'],
                'observation_hash':plan['observation_hash'],'unsigned_body_hash':unsigned['unsigned_body_hash'],
                'checked_at':self.now,'estimated_fee_nano':'3000000','source_debit_nano':'200000000',
                'recipient_net_nano':'197000000','outgoing_value_nano':'198000000','source_after_nano':'0'}
        self.emulator.project.side_effect=project
        return funding,reference

    async def test_send_all_signs_mode130_zero_nominal_but_journals_projected_value(self):
        _,reference=self.drain_transit()
        result=await self.execute(funding_ref=reference,send_all=True)
        self.assertTrue(result['send_all_requested']);self.assertFalse(result['confirmed_on_chain'])
        self.assertEqual(self.row()['amount'],'198000000')
        boc=self.client.native_money_send.await_args.args[3]
        script="""const {Cell,loadMessage,loadMessageRelaxed}=require('@ton/core');
          const m=loadMessage(Cell.fromBoc(Buffer.from(process.argv[1],'base64'))[0].beginParse());
          const b=m.body.beginParse(),sig=b.loadBuffer(64),hash=b.asCell().hash().toString('hex');
          b.loadUint(32);b.loadUint(32);b.loadUint(32);b.loadUint(32);const mode=b.loadUint(8);
          const out=loadMessageRelaxed(b.loadRef().beginParse());
          console.log(JSON.stringify({sig:sig.toString('hex'),hash,mode,value:out.info.value.coins.toString(),
            dest:out.info.dest.toRawString(),init:m.init,bounce:out.info.bounce}));"""
        decoded=json.loads(subprocess.run([self.codec.node,'-e',script,base64.b64encode(boc).decode()],
            cwd=HELPER.parent,check=True,capture_output=True,timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0).stdout)
        self.assertEqual(decoded['mode'],130);self.assertFalse(decoded['mode']&32)
        self.assertEqual(decoded['value'],'0');self.assertEqual(decoded['dest'],self.target)
        self.assertIsNone(decoded['init']);self.assertFalse(decoded['bounce'])
        Ed25519PrivateKey.from_private_bytes(self.seed).public_key().verify(
            bytes.fromhex(decoded['sig']),bytes.fromhex(decoded['hash']))

    async def test_send_all_never_drains_old_funds_or_existing_reserve(self):
        _,reference=self.funded_transit()
        with patch.object(self.vault,'use_seed',side_effect=AssertionError('must not read seed')):
            with self.assertRaises(NativeExecutionError): await self.execute(funding_ref=reference,send_all=True)
        self.client.native_money_send.assert_not_awaited()

    async def test_send_all_requires_zero_remainder_and_exact_full_debit_projection(self):
        _,reference=self.drain_transit();original=self.emulator.project.side_effect
        async def remainder(*args,**kwargs):
            return {**await original(*args,**kwargs),'source_after_nano':'1'}
        self.emulator.project.side_effect=remainder
        with patch.object(self.vault,'use_seed',side_effect=AssertionError('must not read seed')):
            with self.assertRaises(NativeExecutionError): await self.execute(funding_ref=reference,send_all=True)
        self.client.native_money_send.assert_not_awaited()

    async def test_send_all_cannot_consume_an_explicit_reserve(self):
        _,reference=self.drain_transit();self.policy=replace(self.policy,reserve_nano='1')
        with patch.object(self.vault,'use_seed',side_effect=AssertionError('must not read seed')):
            with self.assertRaises(NativeExecutionError): await self.execute(funding_ref=reference,send_all=True)
        self.client.native_money_send.assert_not_awaited()

    async def test_send_all_is_not_implicit_deployment_or_mode3_review(self):
        _,reference=self.drain_transit()
        for changes in ({'deploy':True},{'send_all':1},{'reviewed_limits':{} }):
            with self.assertRaises(NativeExecutionError):
                await self.execute(funding_ref=reference,**{'send_all':True,**changes})
        self.policy=replace(self.policy,scope='all_native_authorized')
        with self.assertRaises(NativeExecutionError): await self.execute(send_all=True)
        with self.money.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)

    async def test_native_carousel_first_link_is_durable_before_dispatch(self):
        _,reference=self.drain_transit();carousel=MoneyCarousel(self.money)
        plan=carousel.create([{'account_id':'one','address':self.source},{'account_id':'two','address':self.target}],
            network='mainnet',scope_ids=set(self.accounts),ui_context=self.ui,confirmed=True)
        step=CarouselStep(carousel,plan['id'],plan['revision'],0,reference)
        original=self.vault.use_seed
        def checked_seed(**kwargs):
            with self.money.db() as db:
                linked=db.execute('SELECT intent FROM money_carousel_hops').fetchone()[0]
                self.assertIsNotNone(linked)
                self.assertEqual(db.execute('SELECT state FROM intents WHERE id=?',(linked,)).fetchone()[0],'uncertain')
            return original(**kwargs)
        with patch.object(self.vault,'use_seed',side_effect=checked_seed):
            result=await self.execute(funding_ref=reference,send_all=True,carousel_step=step)
        with self.money.db() as db:
            self.assertEqual(db.execute('SELECT intent FROM money_carousel_hops').fetchone()[0],result['id'])
            self.assertEqual(db.execute('SELECT revision FROM money_carousels').fetchone()[0],step.revision)
        self.assertNotEqual(step.revision,plan['revision'])

    async def test_native_next_hop_uses_original_receipt_not_funding_or_nominal(self):
        self.drain_transit()
        self.accounts['three']=AccountRecord('three','Synthetic','pyrogram','session_file','never-open.session',user_id=3,username='threefixture')
        scope=set(self.accounts);previous_source='0:'+'03'*32;carousel=MoneyCarousel(self.money)
        plan=carousel.create([{'account_id':'three','address':previous_source},
            {'account_id':'one','address':self.source},{'account_id':'two','address':self.target}],
            network='mainnet',scope_ids=scope,ui_context=self.ui,confirmed=True)
        context,_=self.money.scope('three',scope,self.ui)
        evidence={'status':'provider_delivered','provider':'toncenter_v3','verification':'provider_reported_not_chain_proof',
            'automatic_retry':False,'source_tx':'a'*64,'recipient_tx':'d'*64,'message_hash':'f'*64,'recipient_lt':'1',
            'recipient_state':'active','transit_accounting':'provider_reported_net_not_spend_authorized',
            'incoming_value_nano':'200000000','recipient_before_nano':'0','recipient_after_nano':'200000000',
            'recipient_net_nano':'200000000','recipient_fee_nano':'0'}
        with self.money.db() as db:
            db.execute('INSERT INTO intents(id,account,context,ui_context,network,source,destination,amount,created,expires,state,prompted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('a'*32,'three',context,self.ui,'mainnet',previous_source,self.source,'200000000',1000,1180,'provider_delivered',1000))
            db.execute('INSERT INTO reconciliation VALUES(?,?,?)',('a'*32,1000,json.dumps(evidence)))
        plan=carousel.attach(plan['id'],plan['revision'],'a'*32,scope_ids=scope,ui_context=self.ui,confirmed=True)
        step=CarouselStep(carousel,plan['id'],plan['revision'],1,None)
        result=await self.executor.execute(self.policy,self.identity,anchor_public_key=self.public,target_account_id='two',
            scope_ids=scope,ui_context=self.ui,confirmed=True,send_all=True,carousel_step=step)
        self.assertEqual(result['state'],'wallet_submitted_unverified')
        with self.money.db() as db:
            self.assertEqual(db.execute('SELECT intent FROM native_transit_claims WHERE message_hash=?',('f'*64,)).fetchone()[0],result['id'])
        self.assertEqual(self.client.native_money_send.await_count,1)

    async def test_real_codec_sign_vault_and_one_dispatch_share_existing_journal(self):
        result=await self.execute()
        self.assertEqual(result['state'],'wallet_submitted_unverified');self.assertFalse(result['confirmed_on_chain'])
        self.assertEqual(self.client.native_money_send.await_count,1)
        args=self.client.native_money_send.await_args.args
        self.assertEqual(args[:3],('twofixture',2,self.target))
        self.assertIsInstance(args[3],bytes);self.assertGreater(len(args[3]),64)
        self.assertEqual(args[4],self.commitment()['random_id'])
        self.assertEqual(self.row()['amount'],'190000000')
        self.assertEqual(self.commitment()['stage'],'submitted')
        self.assertNotIn(self.seed,self.money.path.read_bytes())
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.client.native_money_send.await_count,1)

    async def test_preflight_identity_failure_does_not_sign_or_submit(self):
        self.client.get_me.return_value={'user_id':999}
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.row()['state'],'operator_not_sent')
        self.assertEqual(self.commitment()['stage'],'preflight_failed')
        self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_no_confirmation_or_disabled_has_no_journal_claim(self):
        with self.assertRaises(NativeExecutionError): await self.execute(confirmed=False)
        self.executor.allow=lambda:False
        with self.assertRaises(NativeExecutionError): await self.execute()
        with self.money.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)

    async def test_changed_chain_state_during_emulation_cannot_dispatch(self):
        changed={**self.observed,'seqno':8}
        self.reader.read=AsyncMock(side_effect=[self.observed,changed])
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.row()['state'],'operator_not_sent');self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_timeout_after_signing_is_uncertain_and_never_retried(self):
        self.client.native_money_send.side_effect=TimeoutError('synthetic timeout')
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.row()['state'],'uncertain')
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.client.native_money_send.await_count,1)

    async def test_cancel_at_dispatch_is_durable_uncertain(self):
        self.client.native_money_send.side_effect=asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.execute()
        self.assertEqual(self.row()['state'],'uncertain')

    async def test_locked_vault_fails_after_durable_claim_without_dispatch(self):
        self.vault.lock()
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.row()['state'],'uncertain');self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_original_money_reconciliation_can_finish_native_intent(self):
        result=await self.execute()
        self.money.reconciliation=AsyncMock(return_value={'status':'provider_delivered'})
        await self.money.action('reconcile',{'account_id':'one','id':result['id'],'source':self.source,'network':'mainnet','confirmed':True},scope_ids={'one','two'},ui_context=self.ui)
        self.assertEqual(self.row()['state'],'provider_delivered')

    async def test_manual_review_cannot_release_an_unexpired_native_request(self):
        result=await self.execute()
        with self.assertRaises(FinanceError):
            await self.money.action('resolve',{'account_id':'one','id':result['id'],'source':self.source,'network':'mainnet',
                'decision':'confirmed_in_wallet','confirmation':'REVIEW '+result['id']},scope_ids={'one','two'},ui_context=self.ui)

    async def test_legacy_unknown_fees_block_native_before_session_or_keys(self):
        with self.money.db() as db:
            db.execute('INSERT INTO intents(id,source,network,amount,created,prompted,state) VALUES(?,?,?,?,?,?,?)',
                ('e'*32,self.source,'mainnet','100000000',1000,1000,'provider_delivered'))
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.client.get_me.await_count,0)

    async def test_reserved_debit_counts_fee_ceiling_and_same_seqno_never_re_signs(self):
        result=await self.execute()
        with self.money.db() as db:
            self.assertEqual(self.money.reserved_debit(db,self.source,'mainnet'),200000000)
            db.execute("UPDATE intents SET state='provider_delivered' WHERE id=?",(result['id'],))
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.client.native_money_send.await_count,1)

    async def test_scope_change_or_changed_minimum_stops_before_signature(self):
        self.client.native_money_minimum.side_effect=['100000000','200000000']
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.client.native_money_send.await_count,0)
        self.assertEqual(self.row()['state'],'operator_not_sent')

    async def test_legacy_prompt_cannot_mix_with_completed_native_day(self):
        result=await self.execute();token='f'*48
        with self.money.db() as db:
            db.execute("UPDATE intents SET state='provider_delivered' WHERE id=?",(result['id'],))
            db.execute('INSERT INTO intents(id,token_hash,account,context,ui_context,network,source,destination,amount,created,expires,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                ('e'*32,hashlib.sha256(token.encode()).hexdigest(),'one',self.context,self.ui,'mainnet',self.source,self.target,'100000000',1000,1180,'prepared'))
        with patch('modules.finance_money.settings',return_value={'mainnet_enabled':True}),self.assertRaisesRegex(FinanceError,'unknown TON Connect fees'):
            await self.money.action('prompt',{'account_id':'one','id':'e'*32,'review_token':token,'confirmed':True},scope_ids={'one','two'},ui_context=self.ui)

    async def test_non_allowlisted_resolved_recipient_never_reaches_signer(self):
        self.client.native_money_recipient.return_value={'address':'0:'+'03'*32}
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.client.native_money_send.await_count,0)
        self.assertEqual(self.row()['state'],'operator_not_sent')


class SendWireTests(unittest.TestCase):
    def test_exact_send_wire_both_backends_no_gasless_and_redacted_repr(self):
        boc=b'public-synthetic-boc'
        expected=struct.pack('<II',SEND,0)+bytes([len(boc)])+boc+bytes(-(len(boc)+1)%4)+struct.pack('<Iqqq',0xf21158c6,42,-7,123)
        for backend in ('pyrogram','telethon'):
            request=send_request(backend,boc,user_id=42,access_hash=-7,random_id=123)
            self.assertEqual(request.write() if backend=='pyrogram' else bytes(request),expected)
            if backend=='telethon': self.assertNotIn('public-synthetic-boc',repr(request.to_dict()))
        with self.assertRaises(ValueError): send_request('pyrogram',b'x'*16385,user_id=42,access_hash=-7,random_id=123)

    def test_dynamic_minimum_exact_value_no_default(self):
        number=SimpleNamespace(ID=0x2be0dfa4,value=100000000.0)
        row=SimpleNamespace(key='wallet_transfer_min_nanos',value=number)
        response=SimpleNamespace(config=SimpleNamespace(ID=0x99c1d49d,value=[row]))
        self.assertEqual(transfer_minimum(response),'100000000')
        number.value=0.1
        with self.assertRaises(ValueError): transfer_minimum(response)
        response.config.value=[]
        with self.assertRaises(ValueError): transfer_minimum(response)


class FinalAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_revocation_during_last_recipient_read_prevents_mutation_both_backends(self):
        from core.telegram_client import PyrogramAccountClient,TelethonAccountClient
        from modules import money_native_tl as tl
        from pyrogram import raw
        from telethon.tl import types
        address='0:'+'02'*32
        for backend,cls,user,peer in (
            ('pyrogram',PyrogramAccountClient,raw.types.User(id=42,access_hash=7),raw.types.PeerUser(user_id=42)),
            ('telethon',TelethonAccountClient,types.User(id=42,access_hash=7),types.PeerUser(user_id=42))):
            allowed=True;calls=[]
            async def invoke(request):
                nonlocal allowed
                ident=getattr(request,'ID',getattr(request,'CONSTRUCTOR_ID',None));calls.append(ident)
                if ident==tl.METHODS['addresses']:
                    allowed=False
                    return SimpleNamespace(ID=tl.USER_ADDRESSES,addresses=[SimpleNamespace(ID=tl.USER_ADDRESS,user_id=42,address=address,public_key=bytes(32))])
                return SimpleNamespace(users=[user],peer=peer)
            with self.subTest(backend=backend),self.assertRaises(ValueError):
                await cls.native_money_send(SimpleNamespace(_native_money_invoke=invoke),'target',42,address,b'public-synthetic',123,admission_check=lambda:allowed)
            self.assertNotIn(SEND,calls)
            self.assertIn(tl.METHODS['addresses'],calls)
