import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Synthetic funding instructions/GET/identity/CAS only. No keys or payments."""
import asyncio
import copy
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

from core.models import AccountRecord
from modules.finance_money import MoneyService
from modules.money_funding import CarouselFunding,FundingError,incoming_credit,read_deposit
from test_money_credit import credit_fixture

ADDRESS='0:'+'01'*32
SENDER='0:'+'02'*32
UI='b'*64


def transaction():
    tx=credit_fixture(value='200000000',fees='1000000')
    tx.update(account=ADDRESS,hash='a'*64,lt='1',now=1001,emulated=False,finality=2,
        in_msg={'source':SENDER,'destination':ADDRESS,'hash':'c'*64,'value':'200000000','bounce':False,'bounced':False,'opcode':0,
            'message_content':{'decoded':{'type':'ton_transfer','comment':'','encrypted':False}}})
    return tx


class IncomingTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_net_credit_preserves_old_balance_not_signer(self):
        tx=transaction();result=incoming_credit(tx,ADDRESS,1000,1002)
        self.assertEqual(result['net_credit_nano'],'199000000');self.assertEqual(result['previous_balance_nano'],'0')
        self.assertEqual(result['recipient_state'],'uninitialized_credited')
        tx['orig_status']='uninit';tx['account_state_before'].update(account_status='uninit',balance='50000000')
        tx['account_state_after']['balance']='249000000'
        self.assertEqual(incoming_credit(tx,ADDRESS,1000,1002)['net_credit_nano'],'199000000')

    def test_active_deposit_net_balance_and_fee_consistency(self):
        tx=transaction();tx.update(orig_status='active',end_status='active')
        tx['description']={'type':'ord','aborted':False,'destroyed':False,'compute_ph':{'skipped':False,'success':True,'exit_code':0}}
        tx['account_state_before'].update(account_status='active',balance='50000000')
        tx['account_state_after'].update(account_status='active',balance='249000000')
        self.assertEqual(incoming_credit(tx,ADDRESS,1000,1002)['net_credit_nano'],'199000000')
        tx['total_fees']='0'
        with self.assertRaises(FundingError): incoming_credit(tx,ADDRESS,1000,1002)

    def test_old_pending_bounce_assets_forwarding_and_reserved_transit_rejected(self):
        changes=[lambda t:t.update(now=999),lambda t:t.update(finality=1),lambda t:t.update(emulated=True),
            lambda t:t['in_msg'].update(bounced=True),lambda t:t['in_msg'].update(opcode=123),
            lambda t:t.update(out_msgs=[{}]),lambda t:t['in_msg']['message_content']['decoded'].update(comment='accmanager:'+'f'*32)]
        for change in changes:
            tx=transaction();change(tx)
            with self.subTest(change=change),self.assertRaises(ValueError): incoming_credit(tx,ADDRESS,1000,1002)

    async def test_bounded_fixed_get_no_match_ambiguous_hash_and_no_retry(self):
        requests=[];rows=[transaction()]
        def handler(req): requests.append(req);return httpx.Response(200,json={'transactions':rows})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result=await read_deposit(ADDRESS,1000,clock=lambda:1002,client=client)
            self.assertEqual(result['tx_hash'],'a'*64)
            self.assertEqual(requests[0].method,'GET');self.assertEqual(requests[0].url.host,'toncenter.com')
            self.assertEqual(requests[0].url.params['start_utime'],'1000')
            rows=[];self.assertIsNone(await read_deposit(ADDRESS,1000,clock=lambda:1002,client=client))
            rows=[transaction(),transaction()]
            with self.assertRaises(FundingError): await read_deposit(ADDRESS,1000,clock=lambda:1002,client=client)
            rows=[transaction()]
            with self.assertRaises(FundingError): await read_deposit(ADDRESS,1000,clock=lambda:1002,client=client,tx_hash='d'*64)
        for response in (httpx.Response(302),httpx.Response(429),httpx.Response(200,content=b'{}')):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:response)) as client:
                with self.assertRaises(ValueError): await read_deposit(ADDRESS,1000,clock=lambda:1002,client=client)


class FundingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=1000
        self.accounts={i:AccountRecord(i,i,'pyrogram','session_file','never-open.session',user_id=n,username=i+'fixture') for n,i in enumerate(('one','two','three'),1)}
        self.money=MoneyService(SimpleNamespace(data_dir=Path(self.temp.name)),accounts=SimpleNamespace(get_account=self.accounts.__getitem__),clock=lambda:self.now)
        self.client=SimpleNamespace(get_me=AsyncMock(return_value={'user_id':1,'username':'onefixture'}),
            native_money_state=AsyncMock(return_value={'availability':'ready','address':ADDRESS}))
        @asynccontextmanager
        async def factory(*args): yield self.client
        self.event=incoming_credit(transaction(),ADDRESS,1000,1002)
        self.reader=AsyncMock(side_effect=lambda *a,**k:copy.deepcopy(self.event))
        self.service=CarouselFunding(self.money,client_factory=factory,deposit_reader=self.reader)
        self.args={'scope_ids':set(self.accounts),'ui_context':UI}
    def tearDown(self): self.temp.cleanup()
    def create(self,ids=None): return self.service.create(ids or ['one','two','one'],confirmed=True,**self.args)
    async def check(self,report,**changes):
        return await self.service.check(report['id'],report['revision'],**{**self.args,'confirmed':True,**changes})

    async def test_instruction_is_local_copyable_exact_route_no_payment_or_read(self):
        report=self.create();self.assertEqual(report['fund_to'],'@onefixture')
        self.assertEqual([n['id'] for n in report['route']],['one','two','one'])
        self.client.get_me.assert_not_awaited();self.reader.assert_not_awaited()
        self.assertFalse(report['spend_authorized']);self.assertFalse(report['executor_supported'])
        with self.money.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)

    async def test_button_is_a_read_request_not_deposit_or_start_authorization(self):
        report=self.create();self.reader.return_value=None;self.reader.side_effect=None
        pending=await self.check(report)
        self.assertEqual(pending['state'],'awaiting_funding');self.assertIsNone(pending['observation'])
        self.now+=11;self.reader.return_value=self.event
        observed=await self.check(pending)
        self.assertEqual(observed['state'],'funding_observed');self.assertFalse(observed['spend_authorized'])
        self.assertEqual(observed['observation']['net_credit_nano'],'199000000')
        with self.money.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)

    async def test_live_username_identity_or_wallet_change_blocks_observation(self):
        report=self.create();self.client.get_me.return_value['username']='changed'
        with self.assertRaises(FundingError): await self.check(report)
        self.reader.assert_not_awaited()
        self.now+=31;self.client.get_me.return_value['username']='onefixture'
        observed=await self.check(report);self.now+=11
        self.client.native_money_state.return_value['address']=SENDER
        with self.assertRaises(FundingError): await self.check(observed)
        self.assertTrue(self.service.report(report['id'],**self.args)['stale'])
        self.now+=31;self.client.native_money_state.return_value={'availability':'missing'}
        with self.assertRaises(FundingError): await self.check(observed)

    async def test_scope_stop_timeout_and_cancel_do_not_retry_or_erase_evidence(self):
        report=self.create()
        with self.assertRaises(FundingError): await self.check(report,scope_check=lambda:False)
        self.client.get_me.assert_not_awaited()
        observed=await self.check(report);self.now+=11
        self.reader.side_effect=asyncio.TimeoutError()
        with self.assertRaises(FundingError): await self.check(observed)
        saved=self.service.report(report['id'],**self.args)
        self.assertTrue(saved['stale']);self.assertEqual(saved['observation'],self.event)
        count=self.reader.await_count
        with self.assertRaises(FundingError): await self.check(saved)
        self.assertEqual(self.reader.await_count,count)

    async def test_revision_cancel_during_read_discards_late_result(self):
        report=self.create()
        async def cancel(*a,**k):
            self.service.cancel(report['id'],report['revision'],confirmation='CANCEL '+report['id'],**self.args)
            return self.event
        self.reader.side_effect=cancel
        with self.assertRaises(FundingError): await self.check(report)
        saved=self.service.report(report['id'],**self.args)
        self.assertEqual(saved['state'],'cancelled');self.assertIsNone(saved['observation'])

    async def test_finite_claims_restart_and_duplicate_event_are_conservative(self):
        self.accounts['two'].user_id=1
        with self.assertRaises(FundingError): self.create()
        self.accounts['two'].user_id=2
        self.accounts['two'].username='ONEFIXTURE'
        with self.assertRaises(FundingError): self.create()
        self.accounts['two'].username='twofixture'
        report=self.create()
        with self.assertRaises(FundingError): self.create()
        for ids in (['one','two','two'],['one','one'],['one','outside']):
            with self.assertRaises(ValueError): self.create(ids)
        observed=await self.check(report)
        other=CarouselFunding(self.money)
        self.assertEqual(other.latest(**self.args)['id'],report['id'])
        other.cancel(observed['id'],observed['revision'],confirmation='CANCEL '+observed['id'],**self.args)
        next_report=self.create()
        with self.assertRaises(FundingError): await self.check(next_report)

    async def test_reserved_journal_transit_cannot_fund_another_carousel(self):
        report=self.create()
        with self.money.db() as db:
            db.execute('INSERT INTO intents(id,destination) VALUES(?,?)',('f'*32,ADDRESS))
            db.execute('INSERT INTO reconciliation VALUES(?,?,?)',('f'*32,1000,json.dumps({'message_hash':self.event['message_hash']})))
        with self.assertRaises(FundingError): await self.check(report)

    async def test_stale_instruction_hides_username_but_can_close_metadata_only(self):
        report=self.create();self.accounts['one'].username='changedfixture'
        saved=self.service.latest(**self.args)
        self.assertTrue(saved['stale_context']);self.assertIsNone(saved['fund_to'])
        with self.assertRaises(FundingError): await self.check(report)
        closed=self.service.cancel(saved['id'],saved['revision'],confirmation='CANCEL '+saved['id'],**self.args)
        self.assertEqual(closed['state'],'cancelled')
        self.assertEqual(self.create()['fund_to'],'@changedfixture')

    async def test_existing_unfinished_plan_blocks_new_funding_instruction(self):
        from modules.money_carousel import MoneyCarousel
        MoneyCarousel(self.money).create([{'account_id':'one','address':ADDRESS},{'account_id':'two','address':SENDER}],
            network='mainnet',confirmed=True,**self.args)
        with self.assertRaises(FundingError): self.create()
