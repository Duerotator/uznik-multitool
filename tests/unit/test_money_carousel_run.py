import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Finite runner with temporary original journal and fake dispatch/receipts only."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid

from core.models import AccountRecord
from modules.finance_money import MoneyService
from modules.money_carousel import MoneyCarousel
from modules.money_carousel_run import MoneyCarouselRunner,CarouselRunError
from modules.money_funding import CarouselFunding
from modules.money_policy import make_policy
from modules.money_transit import setup,claim_funding


UI='a'*64


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=1000;self.sent=[];self.reads=[];self.events=[]
        self.receipt_mode='delivered';self.stop=False;self.cancel_on_read=False
        self.accounts={key:AccountRecord(key,key,'pyrogram','session_file','never-open.session',user_id=i,username=key+'fixture')
            for i,key in enumerate(('a','b','x'),1)}
        self.money=MoneyService(SimpleNamespace(data_dir=Path(self.temp.name)),
            accounts=SimpleNamespace(get_account=self.accounts.__getitem__),clock=lambda:self.now,reconciliation=self.reconcile)
        self.scope=set(self.accounts)
        self.nodes=[{'account_id':key,'address':'0:'+str(i)*64} for i,key in enumerate(self.accounts,1)]
        funding=CarouselFunding(self.money)
        report=funding.create(list(self.accounts),scope_ids=self.scope,ui_context=UI,confirmed=True)
        self.funding_ref={k:report[k] for k in ('id','revision')}
        event={'tx_hash':'c'*64,'message_hash':'d'*64,'lt':'1','source':'0:'+'4'*64,
            'address':self.nodes[0]['address'],'net_credit_nano':'300000000','previous_balance_nano':'0',
            'recipient_state':'active','verification':'provider_reported_not_chain_proof'}
        with self.money.db() as db:
            setup(db)
            db.execute("UPDATE money_funding_drafts SET state='funding_observed',address=?,event=? WHERE id=?",
                (event['address'],json.dumps(event),report['id']))
            db.execute('INSERT INTO money_funding_refs VALUES(?,?,?,?,?)',(event['message_hash'],event['tx_hash'],report['id'],event['address'],'1'))
        self.carousel=MoneyCarousel(self.money)
        self.plan=self.carousel.create(self.nodes,network='mainnet',scope_ids=self.scope,ui_context=UI,confirmed=True)
        self.credentials={}
        for i,(before,after) in enumerate(zip(self.nodes,self.nodes[1:]),1):
            key=before['account_id'];context,_=self.money.scope(key,self.scope,UI)
            policy=make_policy(account=key,account_context=context,ui_context=UI,network='mainnet',source=before['address'],
                targets=[after['address']],scope='transit_only',reserve_nano='0',fee_cap_nano='10000000',
                transfer_debit_cap_nano='1000000000',daily_reservation_cap_nano='2000000000',confirmed=True)
            identity={'account':key,'user_id':self.accounts[key].user_id,'context':context,'network':'mainnet',
                'address':before['address'],'public_key':str(i)*64,'revision':str(i)*32}
            self.credentials[key]={'policy':policy,'identity':identity,'anchor':str(i)*64}
        self.inventory={'exists':True,'locked_in_process':False,'records':[{**v['identity'],'scheme':'ed25519-seed32','version':1} for v in self.credentials.values()]}
        self.executor=SimpleNamespace(money=self.money,allow=lambda:True,execute=self.execute,
            vault=SimpleNamespace(status=lambda:self.inventory),codec=SimpleNamespace(derive=lambda key,network:{'source':'0:'+key}))
        self.runner=MoneyCarouselRunner(self.carousel,self.executor,sleep=self.sleep)

    def tearDown(self): self.temp.cleanup()
    async def sleep(self,seconds): self.now+=seconds
    async def run_plan(self,**changes):
        return await self.runner.run(self.plan['id'],self.plan['revision'],funding_ref=self.funding_ref,
            credentials=self.credentials,scope_ids=self.scope,ui_context=UI,
            confirmation=changes.pop('confirmation','RUN '+self.plan['id']),
            scope_check=lambda:not self.stop,progress=self.events.append,**changes)

    async def execute(self,policy,identity,**kwargs):
        step=kwargs['carousel_step'];ident=uuid.uuid4().hex
        self.assertTrue(kwargs['send_all']);self.assertTrue(kwargs['confirmed'])
        with self.money.db() as db:
            snapshot=step.snapshot(self.money,db,policy,kwargs['target_account_id'],self.scope,UI)
            db.execute('INSERT INTO intents(id,account,context,ui_context,network,source,destination,amount,created,expires,state,prompted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident,policy.account,policy.account_context,UI,'mainnet',policy.source,policy.targets[0],str(int(snapshot['net_nano'])-1000000),
                 self.now,int(self.now)+180,'wallet_submitted_unverified',self.now))
            step.link(db,ident);claim_funding(db,snapshot,ident)
            step.snapshot(self.money,db,policy,kwargs['target_account_id'],self.scope,UI,intent=ident)
        self.sent.append(ident)
        return {'id':ident,'state':'wallet_submitted_unverified'}

    async def reconcile(self,intent):
        self.reads.append(intent['id'])
        if self.cancel_on_read: self.stop=True
        status='provider_delivered' if self.receipt_mode=='delivered' else self.receipt_mode
        if self.receipt_mode=='pending_once': status='recipient_pending' if len(self.reads)==1 else 'provider_delivered'
        if self.receipt_mode=='terminal_uninit': status='provider_delivered'
        evidence={'status':status,'provider':'toncenter_v3','verification':'provider_reported_not_chain_proof',
            'automatic_retry':False,'source_tx':'e'*64,'recipient_tx':intent['id']*2,'message_hash':intent['id']*2,
            'recipient_state':'active','recipient_lt':str(100+len(self.sent)),
            'transit_accounting':'provider_reported_net_not_spend_authorized','incoming_value_nano':intent['amount'],
            'recipient_before_nano':'0','recipient_after_nano':str(int(intent['amount'])-1000000),
            'recipient_net_nano':str(int(intent['amount'])-1000000),'recipient_fee_nano':'1000000'}
        if self.receipt_mode=='uninitialized' or self.receipt_mode=='terminal_uninit' and len(self.sent)==2:
            evidence.update(status='provider_delivered',recipient_state='uninitialized_credited')
        if self.receipt_mode=='missing_lt':
            evidence.update(status='provider_delivered');evidence.pop('recipient_lt')
        return evidence

    def test_no_implicit_start_or_seed_fields(self):
        self.assertEqual(self.sent,[])
        with self.money.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)

    async def test_finite_exact_hops_original_journal_and_net_each_step(self):
        result=await self.run_plan()
        self.assertEqual(result['state'],'completed');self.assertEqual(result['completed_hops'],2)
        self.assertEqual(len(self.sent),2);self.assertFalse(result['automatic_retry'])
        with self.money.db() as db:
            rows=db.execute('SELECT source,destination,amount,state FROM intents ORDER BY created').fetchall()
            self.assertEqual([r['amount'] for r in rows],['299000000','297000000'])
            self.assertTrue(all(r['state']=='provider_delivered' for r in rows))
            self.assertEqual(db.execute('SELECT count(*) FROM native_transit_claims').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM money_carousel_claims').fetchone()[0],0)
        self.assertEqual([e['ordinal'] for e in self.events if e['phase']=='delivered'],[0,1])

    async def test_missing_later_key_or_exact_confirmation_blocks_first_dispatch(self):
        self.inventory['records']=self.inventory['records'][:1]
        with self.assertRaises(CarouselRunError): await self.run_plan()
        self.assertEqual(self.sent,[])
        with self.assertRaises(CarouselRunError): await self.run_plan(confirmation='yes')

    async def test_pending_receipt_has_bounded_reads_no_resend_or_next_hop(self):
        self.receipt_mode='recipient_pending'
        with self.assertRaises(CarouselRunError): await self.run_plan()
        self.assertEqual(len(self.sent),1);self.assertEqual(len(self.reads),self.runner.READ_ATTEMPTS)
        view=self.carousel.snapshot(self.plan['id'],scope_ids=self.scope,ui_context=UI)
        self.assertEqual(view['state'],'paused')
        with self.assertRaises(ValueError): await self.run_plan()
        self.assertEqual(len(self.sent),1)

    async def test_ambiguous_receipt_stops_without_even_read_retry(self):
        self.receipt_mode='ambiguous'
        with self.assertRaises(CarouselRunError): await self.run_plan()
        self.assertEqual(len(self.sent),1);self.assertEqual(len(self.reads),1)

    async def test_pending_then_delivered_repeats_only_read_not_payment(self):
        self.receipt_mode='pending_once'
        self.assertEqual((await self.run_plan())['state'],'completed')
        self.assertEqual(len(self.sent),2);self.assertEqual(len(self.reads),3)

    async def test_terminal_credit_does_not_require_or_claim_terminal_signer(self):
        self.receipt_mode='terminal_uninit'
        self.assertEqual((await self.run_plan())['state'],'completed')
        self.assertEqual(len(self.sent),2)

    async def test_first_credit_or_missing_lt_does_not_enable_next_signer(self):
        self.receipt_mode='uninitialized'
        with self.assertRaises(CarouselRunError): await self.run_plan()
        self.assertEqual(len(self.sent),1)

    async def test_missing_lt_is_not_inferred_from_nominal_or_simulation(self):
        self.receipt_mode='missing_lt'
        with self.assertRaises(CarouselRunError): await self.run_plan()
        self.assertEqual(len(self.sent),1)

    async def test_stop_during_receipt_discards_late_result_keeps_link(self):
        self.cancel_on_read=True
        with self.assertRaises(CarouselRunError): await self.run_plan()
        self.assertEqual(len(self.sent),1)
        with self.money.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reconciliation').fetchone()[0],0)
            self.assertIsNotNone(db.execute('SELECT intent FROM money_carousel_hops WHERE ordinal=0').fetchone()[0])

    async def test_process_restart_never_auto_resumes(self):
        self.runner=MoneyCarouselRunner(MoneyCarousel(self.money),self.executor,sleep=self.sleep)
        with self.assertRaises(ValueError): await self.run_plan()
        self.assertEqual(self.sent,[])

    async def test_full_vault_metadata_does_not_relax_exact_identity_revision(self):
        self.credentials['b']['identity']={**self.credentials['b']['identity'],'revision':'f'*32}
        with self.assertRaises(CarouselRunError): await self.run_plan()
        self.assertEqual(self.sent,[])
