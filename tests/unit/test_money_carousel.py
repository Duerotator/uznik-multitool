import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Temporary metadata/journal fixtures. No wallets/providers/sessions or payments."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from core.models import AccountRecord
from modules.finance_money import MoneyService
from modules.money_carousel import MoneyCarousel, CarouselError


UI='a'*64
SCOPE={'a','b','x'}


class CarouselTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=1000
        self.accounts={key:AccountRecord(key,'Synthetic','pyrogram','session_file','never-opened.session',user_id=i)
                       for i,key in enumerate(('a','b','x'),start=1)}
        config=SimpleNamespace(data_dir=Path(self.temp.name))
        self.money=MoneyService(config,accounts=SimpleNamespace(get_account=self.accounts.__getitem__),clock=lambda:self.now)
        self.service=MoneyCarousel(self.money)
        self.nodes=[{'account_id':key,'address':'0:'+f'{i:02x}'*32} for i,key in enumerate(('a','b','x'),start=1)]

    def tearDown(self): self.temp.cleanup()
    def create(self,**changes):
        return self.service.create(self.nodes,network='testnet',scope_ids=SCOPE,ui_context=UI,**{'confirmed':True,**changes})
    def view(self,plan,service=None,**changes):
        return (service or self.service).snapshot(plan['id'],scope_ids=SCOPE,ui_context=changes.get('ui_context',UI))
    def attach(self,plan,intent,service=None,**changes):
        return (service or self.service).attach(plan['id'],plan['revision'],intent,scope_ids=SCOPE,ui_context=UI,**{'confirmed':True,**changes})
    def control(self,plan,action,service=None):
        return (service or self.service).control(plan['id'],plan['revision'],action,scope_ids=SCOPE,
            ui_context=UI,confirmation=action.upper()+' '+plan['id'])
    def intent(self,index=0,state='prepared',*,created=None):
        ident=uuid.uuid4().hex;before,after=self.nodes[index:index+2]
        context,_=self.money.scope(before['account_id'],SCOPE,UI)
        with self.money.db() as db:
            db.execute('INSERT INTO intents(id,account,context,ui_context,network,source,destination,amount,created,expires,state,prompted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident,before['account_id'],context,UI,'testnet',before['address'],after['address'],'100000000',
                 self.now if created is None else created,1180,state,1000 if state!='prepared' else None))
        if state=='provider_delivered': self.deliver(ident)
        return ident
    def deliver(self,ident,**changes):
        evidence={'status':'provider_delivered','provider':'toncenter_v3',
            'verification':'provider_reported_not_chain_proof','automatic_retry':False,
            'source_tx':'a'*64,'recipient_tx':'b'*64,'message_hash':(ident*2),**changes}
        with self.money.db() as db:
            db.execute("UPDATE intents SET state='provider_delivered',prompted=1000 WHERE id=?",(ident,))
            db.execute('INSERT OR REPLACE INTO reconciliation VALUES(?,?,?)',(ident,self.now,json.dumps(evidence)))
    def rows(self):
        with self.money.db() as db: return [tuple(r) for r in db.execute('SELECT * FROM intents ORDER BY id')]

    def test_metadata_preview_creates_no_payments_or_second_database(self):
        plan=self.create()
        self.assertEqual(plan['next_ordinal'],0)
        self.assertEqual(plan['production_execution'],'unsupported')
        self.assertFalse(plan['spend_authorized']);self.assertFalse(plan['automatic_resume'])
        self.assertIsNone(plan['transit_credit_nano'])
        self.assertEqual(self.rows(),[])
        self.assertEqual([p.name for p in Path(self.temp.name).glob('*.sqlite3')],['finance_money.sqlite3'])

    def test_finite_scope_confirmation_duplicates_and_aliases(self):
        with self.assertRaises(CarouselError): self.create(confirmed=False)
        original=list(self.nodes)
        for nodes in ([],original[:1],original*18,[original[0],original[0]],
                      [original[0],{'account_id':'b','address':original[0]['address']}],
                      [original[0],{'account_id':'not-selected','address':original[1]['address']}]):
            self.nodes=nodes
            with self.subTest(nodes=len(nodes)),self.assertRaises(CarouselError): self.create()
        self.nodes=original+[original[0]]
        self.assertEqual(len(self.create()['hops']),3)

    def test_single_claim_across_instances_and_explicit_cancel_unlinked(self):
        plan=self.create();other=MoneyCarousel(self.money)
        with self.assertRaises(CarouselError): other.create(self.nodes,network='testnet',scope_ids=SCOPE,ui_context=UI,confirmed=True)
        closed=self.control(plan,'cancel')
        self.assertEqual(closed['state'],'cancelled')
        self.assertEqual(self.create()['state'],'active')

    def test_next_hop_only_after_existing_provider_evidence_and_complete(self):
        plan=self.create();first=self.intent();before=self.rows()
        plan=self.attach(plan,first)
        self.assertEqual(self.rows(),before)
        second=self.intent(1)
        with self.assertRaises(CarouselError): self.attach(plan,second)
        self.deliver(first);plan=self.view(plan)
        self.assertEqual(plan['next_ordinal'],1)
        plan=self.attach(plan,second);self.deliver(second);plan=self.view(plan)
        self.assertTrue(plan['complete']);self.assertEqual(plan['next_ordinal'],None)
        result=self.control(plan,'complete')
        self.assertEqual(result['state'],'completed')
        self.assertEqual(self.create()['state'],'active')

    def test_next_hop_net_candidate_requires_consistent_provider_reconciliation(self):
        plan=self.create();first=self.intent();plan=self.attach(plan,first)
        self.deliver(first,recipient_state='active',
            transit_accounting='provider_reported_net_not_spend_authorized',
            recipient_before_nano='50',recipient_after_nano='149',
            recipient_net_nano='99',recipient_fee_nano='1',incoming_value_nano='100')
        view=self.view(plan)
        self.assertEqual(view['next_ordinal'],1)
        self.assertEqual(view['transit_credit_nano'],'99')
        self.assertEqual(view['hops'][0]['recipient_net_nano'],'99')
        self.assertFalse(view['spend_authorized'])
        self.deliver(first,recipient_after_nano='150')
        self.assertIsNone(self.view(plan)['transit_credit_nano'])
        self.deliver(first,transit_accounting='needs_review')
        self.assertIsNone(self.view(plan)['transit_credit_nano'])

    def test_later_wrong_intent_rolls_back_and_does_not_consume_revision(self):
        plan=self.create();wrong=self.intent(1);before=self.rows()
        with self.assertRaises(CarouselError): self.attach(plan,wrong)
        self.assertEqual(self.rows(),before)
        self.assertEqual(self.view(plan)['revision'],plan['revision'])
        self.assertIsNone(self.view(plan)['hops'][0]['intent_id'])

    def test_code_less_credit_is_delivered_but_cannot_advance_to_next_sender(self):
        plan=self.create();first=self.intent();plan=self.attach(plan,first)
        self.deliver(first,recipient_state='uninitialized_credited',signer_ready=False,money_activated='unknown')
        plan=self.view(plan)
        self.assertEqual(plan['state'],'needs_review')
        self.assertTrue(plan['hops'][0]['delivered'])
        self.assertEqual(plan['hops'][0]['journal_state'],'recipient_activation_needs_review')
        with self.assertRaises(CarouselError): self.attach(plan,self.intent(1))
        self.assertFalse(plan['spend_authorized'])

    def test_duplicate_intent_and_stale_revision_never_reattach(self):
        plan=self.create();intent=self.intent();old=plan;plan=self.attach(plan,intent)
        with self.assertRaises(CarouselError): self.attach(old,intent)
        self.deliver(intent);plan=self.view(plan)
        with self.assertRaises(CarouselError): self.attach(plan,intent)

    def test_receipt_uncertain_operator_and_expired_drafts_do_not_advance(self):
        for state in ('wallet_prompted','wallet_submitted_unverified','uncertain','operator_confirmed','operator_not_sent','prepared'):
            with self.subTest(state=state):
                # Each iteration has its own metadata namespace/database.
                self.tearDown();self.setUp()
                plan=self.create();intent=self.intent(state=state)
                if state=='prepared': self.now=1181
                plan=self.attach(plan,intent)
                self.assertEqual(plan['state'],'needs_review')
                with self.assertRaises(CarouselError): self.attach(plan,self.intent(1))
                with self.assertRaises(CarouselError): self.control(plan,'resume',MoneyCarousel(self.money))
                with self.assertRaises(CarouselError): self.control(plan,'cancel')

    def test_missing_bad_simulation_and_duplicate_evidence_block_progress(self):
        plan=self.create();first=self.intent(state='provider_delivered');plan=self.attach(plan,first)
        second=self.intent(1,state='provider_delivered');self.deliver(second,message_hash=first*2)
        with self.assertRaises(CarouselError): self.attach(plan,second)
        for changes in ({'status':'source_pending'},{'verification':'provider_reported_simulation_not_receipt'},
                        {'automatic_retry':True},{'message_hash':None}):
            self.deliver(first,**changes)
            with self.assertRaises(CarouselError): self.view(plan)
        with self.money.db() as db: db.execute('DELETE FROM reconciliation WHERE intent=?',(first,))
        with self.assertRaises(CarouselError): self.view(plan)

    def test_restart_is_interrupted_explicit_cas_resume_has_no_payment_effect(self):
        plan=self.create();other=MoneyCarousel(self.money)
        self.assertEqual(self.view(plan,other)['state'],'interrupted')
        intent=self.intent()
        with self.assertRaises(CarouselError): self.attach(plan,intent,other)
        before=self.rows();resumed=self.control(plan,'resume',other)
        self.assertEqual(resumed['state'],'active');self.assertEqual(self.rows(),before)
        with self.assertRaises(CarouselError): self.attach(plan,intent)
        other.attach(resumed['id'],resumed['revision'],intent,scope_ids=SCOPE,ui_context=UI,confirmed=True)

    def test_pause_scope_filter_identity_and_disabled_accounts_fail_closed(self):
        plan=self.create();plan=self.control(plan,'pause')
        self.assertEqual(plan['state'],'paused')
        with self.assertRaises(CarouselError): self.attach(plan,self.intent())
        plan=self.control(plan,'resume')
        with self.assertRaises(CarouselError): self.view(plan,ui_context='c'*64)
        with self.assertRaises(CarouselError): self.service.snapshot(plan['id'],scope_ids={'a'},ui_context=UI)
        self.accounts['b'].enabled=False
        with self.assertRaises(CarouselError): self.view(plan)
        self.accounts['b'].enabled=True;self.accounts['b'].user_id=999
        with self.assertRaises(CarouselError): self.view(plan)

    def test_old_intent_or_wrong_network_context_never_claimed(self):
        plan=self.create()
        old=self.intent(created=999)
        with self.assertRaises(CarouselError): self.attach(plan,old)
        current=self.intent()
        with self.money.db() as db: db.execute("UPDATE intents SET network='mainnet' WHERE id=?",(current,))
        with self.assertRaises(CarouselError): self.attach(plan,current)

    def test_unknown_or_manual_confirmation_cannot_bypass_control(self):
        plan=self.create()
        for action in ('complete','unknown'):
            with self.assertRaises(CarouselError): self.control(plan,action)
        with self.assertRaises(CarouselError):
            self.service.control(plan['id'],plan['revision'],'cancel',scope_ids=SCOPE,ui_context=UI,confirmation='yes')

    def test_simultaneous_checkpoint_has_exactly_one_cas_winner(self):
        plan=self.create();first=self.intent();second=self.intent();before=self.rows();barrier=threading.Barrier(2)
        def worker(ident):
            barrier.wait(timeout=5)
            try: return self.attach(plan,ident)
            except CarouselError: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(worker,(first,second)))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(self.rows(),before)

    def test_unrelated_unresolved_intent_blocks_source_without_retry(self):
        self.intent(state='uncertain')
        with self.assertRaises(CarouselError): self.create()
        with self.money.db() as db: db.execute('DELETE FROM intents')
        plan=self.create();intent=self.intent();self.intent(state='wallet_prompted')
        with self.assertRaises(CarouselError): self.attach(plan,intent)
        self.assertIsNone(self.view(plan)['hops'][0]['intent_id'])
