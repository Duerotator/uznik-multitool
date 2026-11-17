import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Desktop arming fixtures: no real keys/config/sessions/RPC or transfers."""
from contextlib import asynccontextmanager
import copy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import test_money_carousel_run as fixtures
import test_money_execution as native_fixtures
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from modules.money_carousel_run import MoneyCarouselRunner
from modules.money_carousel_workspace import CarouselMoneyWorkspace
from modules.money_workspace import NativeMoneyWorkspace
from modules.money_funding import CarouselFunding


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=fixtures.RunnerTests();self.f.setUp();self.reads=0
        self.client=SimpleNamespace(get_me=AsyncMock(return_value={'user_id':1}),
            native_money_recipient=AsyncMock(return_value={'address':self.f.nodes[-1]['address']}))
        @asynccontextmanager
        async def factory(*args): self.reads+=1;yield self.client
        self.f.executor.factory=factory
        native=SimpleNamespace(money=self.f.money,vault=self.f.executor.vault,executor=self.f.executor)
        native._key=lambda ident,context:(self.f.credentials[ident]['identity'],self.f.credentials[ident]['anchor'])
        with self.f.money.db() as db:
            # Fixture's initial planner is not part of this UI review.
            db.execute('DELETE FROM money_carousel_hops');db.execute('DELETE FROM money_carousels');db.execute('DELETE FROM money_carousel_claims')
            db.execute('CREATE TABLE native_key_links(account TEXT PRIMARY KEY,revision TEXT,anchor TEXT)')
            for ident,entry in self.f.credentials.items():
                db.execute('INSERT INTO native_key_links VALUES(?,?,?)',(ident,entry['identity']['revision'],entry['anchor']))
        from modules.money_funding import CarouselFunding
        self.funding=CarouselFunding(self.f.money)
        self.workspace=CarouselMoneyWorkspace(native,self.funding)
        self.workspace.runner=MoneyCarouselRunner(self.workspace.carousel,self.f.executor,sleep=self.f.sleep)
        self.args={'scope_ids':self.f.scope,'ui_context':fixtures.UI}
        self.limits={'fee':'0.01','debit':'1','daily':'5'}
    def tearDown(self): self.f.tearDown()
    async def review(self,**changes):
        return await self.workspace.review(self.f.funding_ref,self.limits,confirmed=True,**self.args,**changes)
    async def run_review(self,review,**changes):
        return await self.workspace.run(review['token'],confirmation=changes.pop('confirmation',review['confirmation']),**self.args,**changes)

    async def test_review_is_unsigned_no_plan_token_hashed_then_one_use_exact_run(self):
        review=await self.review()
        self.assertEqual(self.f.sent,[]);self.assertEqual(self.reads,1)
        self.client.native_money_recipient.assert_awaited_once_with('xfixture',3,prepare_address=False)
        with self.f.money.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM money_carousels').fetchone()[0],0)
            stored=db.execute('SELECT token_hash FROM carousel_ui_reviews').fetchone()[0]
            self.assertNotEqual(stored,review['token'])
        with self.assertRaises(ValueError): await self.run_review(review,confirmation='yes')
        self.assertEqual((await self.run_review(review))['state'],'completed')
        self.assertEqual(len(self.f.sent),2)
        inspected=self.workspace.inspect(self.f.funding_ref['id'],**self.args)
        self.assertEqual(inspected['state'],'completed');self.assertEqual(len(inspected['hops']),2)
        self.assertFalse(inspected['spend_authorized'])
        self.assertEqual(inspected['approval']['limits'],{'fee':'10000000','debit':'1000000000','daily':'5000000000'})
        self.assertNotIn(review['token'],json.dumps(inspected['approval']))
        archive=self.workspace.inspect(self.f.funding_ref['id'],scope_ids=self.f.scope,ui_context='c'*64)
        self.assertTrue(archive['archived_context']);self.assertFalse(archive['spend_authorized'])
        with self.assertRaises(ValueError): self.workspace.inspect(self.f.funding_ref['id'],scope_ids={'a'},ui_context='c'*64)
        with self.assertRaises(ValueError): await self.run_review(review)
        self.assertEqual(len(self.f.sent),2)
        report=self.funding.report(self.f.funding_ref['id'],**self.args)
        self.assertIsNotNone(report['linked_intent']);self.assertIsNone(report['fund_to'])
        with self.assertRaises(ValueError):
            await self.funding.check(self.f.funding_ref['id'],self.f.funding_ref['revision'],confirmed=True,**self.args)

    async def test_key_revision_or_funding_change_revokes_before_first_payment(self):
        review=await self.review()
        with self.f.money.db() as db: db.execute("UPDATE native_key_links SET revision=? WHERE account='b'",('f'*32,))
        with self.assertRaises(ValueError): await self.run_review(review)
        self.assertEqual(self.f.sent,[])

    async def test_expired_disabled_and_restart_reviews_do_not_arm(self):
        review=await self.review();self.f.now+=181
        with self.assertRaises(ValueError): await self.run_review(review)
        review=await self.review();self.f.executor.allow=lambda:False
        with self.assertRaises(ValueError): await self.run_review(review)
        self.assertEqual(self.f.sent,[])

    async def test_new_review_replaces_old_and_stop_during_lookup_has_no_review(self):
        old=await self.review();new=await self.review()
        with self.assertRaises(ValueError): await self.run_review(old)
        self.assertNotEqual(old['token'],new['token'])
        state={'valid':True}
        async def recipient(*args,**kwargs): state['valid']=False;return {'address':self.f.nodes[-1]['address']}
        self.client.native_money_recipient.side_effect=recipient
        with self.assertRaises(ValueError): await self.review(scope_check=lambda:state['valid'])
        self.assertEqual(self.f.sent,[])

    async def test_missing_sender_key_and_uninitialized_source_fail_before_lookup(self):
        self.workspace.native._key=lambda *args:(_ for _ in ()).throw(ValueError('missing'))
        with self.assertRaisesRegex(ValueError,'ключа'): await self.review()
        self.assertEqual(self.reads,0)
        with self.f.money.db() as db:
            row=db.execute('SELECT event FROM money_funding_drafts').fetchone();event=json.loads(row[0]);event['recipient_state']='uninitialized_credited'
            db.execute('UPDATE money_funding_drafts SET event=?',(json.dumps(event),))
        with self.assertRaisesRegex(ValueError,'active'): await self.review()

    async def test_error_before_link_cancels_only_empty_metadata_plan(self):
        review=await self.review()
        def broken(event): raise ValueError('synthetic callback failure')
        with self.assertRaises(ValueError): await self.run_review(review,progress=broken)
        with self.f.money.db() as db:
            self.assertEqual(db.execute('SELECT state FROM money_carousels').fetchone()[0],'cancelled')
            self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM money_carousel_claims').fetchone()[0],0)

    async def test_invalid_caps_and_alias_keys_fail_before_any_session_read(self):
        self.limits['fee']='2'
        with self.assertRaises(ValueError): await self.review()
        self.assertEqual(self.reads,0)
        self.limits['fee']='0.01'
        original=self.workspace.native._key
        self.workspace.native._key=lambda ident,context:(
            {**original(ident,context)[0],'address':self.f.nodes[0]['address']},original(ident,context)[1])
        with self.assertRaisesRegex(ValueError,'один финансовый адрес'): await self.review()
        self.assertEqual(self.reads,0)

    async def test_restart_does_not_restore_financial_review(self):
        review=await self.review()
        restarted=CarouselMoneyWorkspace(self.workspace.native,self.funding)
        with self.assertRaises(ValueError):
            await restarted.run(review['token'],confirmation=review['confirmation'],**self.args)
        self.assertEqual(self.f.sent,[])

    async def test_approval_tamper_does_not_grant_spend(self):
        review=await self.review()
        with self.f.money.db() as db: db.execute("UPDATE carousel_ui_reviews SET approval='{}'")
        with self.assertRaisesRegex(ValueError,'Утверждённые'): await self.run_review(review)
        self.assertEqual(self.f.sent,[])

    async def test_legacy_review_schema_is_upgraded_without_reviving_approval(self):
        with self.f.money.db() as db:
            db.execute('DROP TABLE carousel_ui_reviews') # This test's empty temporary metadata table only.
            db.execute('CREATE TABLE carousel_ui_reviews(token_hash TEXT PRIMARY KEY,draft TEXT,expires REAL,used INTEGER,plan TEXT)')
            db.execute('INSERT INTO carousel_ui_reviews VALUES(?,?,?,0,NULL)',('a'*64,self.f.funding_ref['id'],2000))
        upgraded=CarouselMoneyWorkspace(self.workspace.native,self.funding)
        with self.f.money.db() as db:
            row=db.execute('SELECT * FROM carousel_ui_reviews').fetchone()
            self.assertIsNone(row['approval']);self.assertEqual(row['used'],0)
            self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)
        with self.assertRaises(ValueError): await upgraded.run('legacy',confirmation='RUN '+self.f.funding_ref['id'],**self.args)

    @unittest.skipUnless(native_fixtures.AVAILABLE,'Pinned codec missing; not scoped acceptance')
    async def test_full_workspace_runner_native_codec_two_hops_offline(self):
        fixture=native_fixtures.ExecutionTests();await fixture.asyncSetUp()
        try:
            seed_b=os.urandom(32)
            pub_b=Ed25519PrivateKey.from_private_bytes(seed_b).public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
            addresses={'one':fixture.source,'two':fixture.codec.derive(pub_b,'mainnet')['source']}
            publics={'one':fixture.public,'two':pub_b};sent=[]
            states={ident:{**fixture.observed,'address':address,'public_key':publics[ident],
                'balance_nano':'200000000' if ident=='one' else '0',
                'last_transaction':{'hash':'d'*64,'lt':'1'}} for ident,address in addresses.items()}
            @asynccontextmanager
            async def factory(config,account):
                async def submit(username,user_id,address,boc,random_id,admission_check):
                    assert admission_check();sent.append((account.id,bytes(boc)))
                    states[account.id]['balance_nano']='0'
                yield SimpleNamespace(get_me=AsyncMock(return_value={'user_id':account.user_id}),
                    native_money_state=AsyncMock(return_value={'availability':'ready','address':addresses[account.id],'public_key':publics[account.id]}),
                    native_money_minimum=AsyncMock(return_value='100000000'),
                    native_money_recipient=AsyncMock(side_effect=lambda username,uid,**kw:{'address':addresses['one' if uid==1 else 'two']}),
                    native_money_send=submit)
            async def read(address,*args,**kwargs):
                state=next(s for s in states.values() if s['address']==address)
                return {**copy.deepcopy(state),'checked_at':fixture.now}
            async def project(policy,observed,plan,**kwargs):
                unsigned=fixture.codec.prepare_emulation(network='mainnet',anchor_public_key=kwargs['anchor_public_key'],
                    seqno=plan['seqno'],valid_until=plan['valid_until'],destination=plan['target'],amount_nano='0',
                    intent_id=plan['intent_id'],send_all=True)
                balance=int(plan['balance_nano'])
                return {'verification':'provider_reported_simulation_not_receipt','quote_hash':plan['quote_hash'],
                    'observation_hash':plan['observation_hash'],'unsigned_body_hash':unsigned['unsigned_body_hash'],
                    'checked_at':fixture.now,'estimated_fee_nano':'3000000','source_debit_nano':str(balance),
                    'source_after_nano':'0','outgoing_value_nano':str(balance-2000000),'recipient_net_nano':str(balance-3000000)}
            async def reconcile(intent):
                target=next(key for key,address in addresses.items() if address==intent['destination'])
                net=int(intent['amount'])-1000000;lt=str(100+len(sent));tx=intent['id']*2
                states[target].update(balance_nano=str(net),last_transaction={'hash':tx,'lt':lt})
                return {'status':'provider_delivered','provider':'toncenter_v3','verification':'provider_reported_not_chain_proof',
                    'automatic_retry':False,'source_tx':'f'*64,'recipient_tx':tx,'message_hash':tx,'recipient_lt':lt,
                    'recipient_state':'active','transit_accounting':'provider_reported_net_not_spend_authorized',
                    'incoming_value_nano':intent['amount'],'recipient_before_nano':'0','recipient_after_nano':str(net),
                    'recipient_net_nano':str(net),'recipient_fee_nano':'1000000'}
            fixture.money.reconciliation=reconcile
            native=NativeMoneyWorkspace(fixture.money,vault=fixture.vault,codec=fixture.codec,
                reader=SimpleNamespace(read=read),emulator=SimpleNamespace(project=project),client_factory=factory,allow=lambda:True)
            args={'scope_ids':set(fixture.accounts),'ui_context':fixture.ui}
            native.import_key('one',bytearray(fixture.seed),fixture.public,expected_revision=fixture.identity['revision'],confirmed=True,**args)
            native.import_key('two',bytearray(seed_b),pub_b,expected_revision=None,confirmed=True,**args)
            funding=CarouselFunding(fixture.money)
            draft=funding.create(['one','two','one'],confirmed=True,**args)
            event={'tx_hash':'d'*64,'message_hash':'e'*64,'lt':'1','source':'0:'+'ff'*32,'address':fixture.source,
                'net_credit_nano':'200000000','previous_balance_nano':'0','recipient_state':'active','verification':'provider_reported_not_chain_proof'}
            with fixture.money.db() as db:
                db.execute("UPDATE money_funding_drafts SET state='funding_observed',address=?,event=? WHERE id=?",(fixture.source,json.dumps(event),draft['id']))
                db.execute('INSERT INTO money_funding_refs VALUES(?,?,?,?,?)',(event['message_hash'],event['tx_hash'],draft['id'],fixture.source,'1'))
            workspace=CarouselMoneyWorkspace(native,funding)
            async def sleep(seconds): fixture.now+=seconds
            workspace.runner.sleep=sleep
            review=await workspace.review({k:draft[k] for k in ('id','revision')},self.limits,confirmed=True,**args)
            result=await workspace.run(review['token'],confirmation=review['confirmation'],**args)
            self.assertEqual(result['state'],'completed');self.assertEqual([x[0] for x in sent],['one','two'])
            self.assertEqual(states['one']['balance_nano'],'194000000')
            with fixture.money.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM intents WHERE state=?',('provider_delivered',)).fetchone()[0],2)
                self.assertEqual(db.execute('SELECT count(*) FROM native_commitments WHERE stage=?',('submitted',)).fetchone()[0],2)
            raw=fixture.money.path.read_bytes();self.assertNotIn(fixture.seed,raw);self.assertNotIn(seed_b,raw)
        finally: await fixture.asyncTearDown()
