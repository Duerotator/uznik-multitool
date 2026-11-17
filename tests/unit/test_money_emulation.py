import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Synthetic provider projections only; no actual TVM/RPC/signatures/funds."""
import asyncio
import copy
import json
import shutil
import unittest
from pathlib import Path

import httpx

from modules.money_emulation import TonCenterEmulator, MoneyEmulationError
from modules.money_policy import make_policy, quote
from modules.wallet_tg_codec import WalletTgCodec, HELPER, CODE_HASH, REV00_HASH, SUBWALLET
from test_money_credit import credit_fixture


AVAILABLE=bool(shutil.which('node') and (HELPER.parent/'node_modules'/'@ton'/'core'/'package.json').is_file())
ANCHOR='d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a'
TARGET='0:'+'02'*32


@unittest.skipUnless(AVAILABLE,'Pinned codec missing; skips are not scoped acceptance')
class EmulationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now=1000;self.codec=WalletTgCodec(clock=lambda:self.now)
        self.source=self.codec.derive(ANCHOR,'testnet')['source']
        self.policy=make_policy(account='unfunded',account_context='a'*64,ui_context='b'*64,
            network='testnet',source=self.source,targets=[TARGET],scope='transit_only',reserve_nano='0',
            fee_cap_nano='10000000',transfer_debit_cap_nano='1000000000',daily_reservation_cap_nano='2000000000',confirmed=True)
        self.state=dict(network='testnet',address=self.source,active=True,code_hash=CODE_HASH,implementation_hash=REV00_HASH,
            subwallet_id=SUBWALLET['testnet'],provider='toncenter',verification='provider_reported_not_chain_proof',
            coherence='queries_pinned_by_mc_seqno',checked_at=1000,seqno=7,mc_block_seqno=99,
            public_key=ANCHOR,block_id='d'*64,data_hash='f'*64,balance_nano='500000000')
        self.plan=self.make_quote();self.requests=[];self.response=self.fixture()

    def make_quote(self,*,send_all=False,deploy=False):
        return quote(self.policy,self.state,target=TARGET,transit_credit_nano='500000000' if send_all else '200000000',
            reserved_nano='0',min_transfer_nano='100000000',current_account_context='a'*64,current_ui_context='b'*64,
            now=1000,valid_until=1180,intent_id='e'*32,send_all=send_all,deploy=deploy)

    def fixture(self):
        unsigned=self.codec.prepare_emulation(network='testnet',anchor_public_key=ANCHOR,seqno=self.plan['seqno'],valid_until=1180,
            destination=TARGET,amount_nano=self.plan['amount_nano'],intent_id='e'*32,send_all=self.plan['send_mode']==130,deploy=self.plan['deploy'])
        message={'hash':'b'*64,'source':self.source,'destination':TARGET,'value':'190000000',
            'bounced':False,'bounce':False,'opcode':0,'extra_currencies':{},
            'message_content':{'decoded':{'type':'ton_transfer','comment':'accmanager:'+'e'*32,'encrypted':False}}}
        def state(balance):
            return {'balance':balance,'account_status':'active','extra_currencies':{},'code_hash':CODE_HASH,'data_hash':'f'*64}
        def tx(account,hash):
            return {'account':account,'hash':hash,'emulated':True,'out_msgs':[],
                'description':{'type':'ord','aborted':False,'destroyed':False,'compute_ph':{'success':True,'skipped':False,'exit_code':0}}}
        source=tx(self.source,'c'*64);dest=tx(TARGET,'d'*64)
        source['description']['action']={'success':True,'valid':True,'no_funds':False,'result_code':0}
        source.update(account_state_before=state('500000000'),account_state_after=state('308000000'),out_msgs=[message],
            in_msg={'hash':'a'*64,'source':None,'destination':self.source,'message_content':{'hash':unsigned['unsigned_body_hash']}})
        dest.update(account_state_before=state('100000000'),account_state_after=state('289000000'),in_msg=copy.deepcopy(message))
        if self.plan['send_mode']==130:
            source['account_state_after']['balance']='0'
            source['out_msgs'][0]['value']='498000000';dest['in_msg']['value']='498000000'
            dest['account_state_after']['balance']='597000000'
        return {'mc_block_seqno':99,'is_incomplete':False,'trace':{'tx_hash':'c'*64,'in_msg_hash':'a'*64,
            'children':[{'tx_hash':'d'*64,'in_msg_hash':'b'*64,'children':[]}]},'transactions':{'c'*64:source,'d'*64:dest}}

    async def project(self,*,confirmed=True,handler=None,ui_context='b'*64):
        def request(req):
            self.requests.append(req)
            return handler(req) if handler else httpx.Response(200,json=self.response)
        async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
            return await TonCenterEmulator(client=client,codec=self.codec,clock=lambda:self.now).project(
                self.policy,self.state,self.plan,anchor_public_key=ANCHOR,current_account_context='a'*64,
                current_ui_context=ui_context,confirmed=confirmed)

    async def test_exact_unsigned_schema_and_combined_source_recipient_fee(self):
        result=await self.project()
        self.assertEqual(result['estimated_fee_nano'],'3000000')
        self.assertEqual(result['recipient_net_nano'],'189000000')
        self.assertEqual(result['source_debit_nano'],'192000000')
        self.assertEqual(result['verification'],'provider_reported_simulation_not_receipt')
        self.assertFalse(result['production_signing']);self.assertFalse(result['broadcast_supported'])
        self.assertEqual(len(self.requests),1)
        req=self.requests[0]
        self.assertEqual(str(req.url),'https://testnet.toncenter.com/api/emulate/v1/emulateTrace')
        self.assertEqual(req.method,'POST')
        data=json.loads(req.content)
        self.assertEqual(set(data),{'boc','ignore_chksig','include_code_data','include_address_book','include_metadata','with_actions','mc_block_seqno'})
        self.assertTrue(data['ignore_chksig']);self.assertEqual(data['mc_block_seqno'],99)

    async def test_send_all_projection_does_not_destroy(self):
        self.plan=self.make_quote(send_all=True);self.response=self.fixture()
        result=await self.project()
        self.assertEqual(result['source_after_nano'],'0')
        self.assertEqual(result['estimated_fee_nano'],'3000000')
        self.assertEqual(result['recipient_net_nano'],'497000000')
        self.response['transactions']['c'*64]['description']['destroyed']=True
        with self.assertRaises(MoneyEmulationError): await self.project()

    async def test_first_credit_is_supported_without_claiming_wallet_activation(self):
        dest=self.response['transactions']['d'*64]
        dest.update(credit_fixture())
        result=await self.project()
        self.assertEqual(result['recipient_net_nano'],'189000000')
        self.assertEqual(result['estimated_fee_nano'],'3000000')
        self.assertEqual(result['recipient_state'],'uninitialized_credited')
        self.assertFalse(result['signer_ready']);self.assertEqual(result['money_activated'],'unknown')

    async def test_provider_nullable_extra_currency_map_is_not_a_phantom_currency(self):
        for tx in self.response['transactions'].values():
            for key in ('account_state_before','account_state_after'):
                tx[key]['extra_currencies']=None
        self.assertEqual((await self.project())['recipient_state'],'active')

    def deployment_fixture(self):
        implementation=(Path(__file__).resolve().parents[1]/'fixtures/wallet_tg_rev00.b64').read_text().strip()
        initial=self.codec.inspect_initial(network='testnet',anchor_public_key=ANCHOR,implementation_boc=implementation)
        self.state.update(initial,active=False,chain_state='uninit',code_hash=None,data_hash=None,public_key_origin='initial_anchor_not_chain_getter')
        self.plan=self.make_quote(deploy=True);self.response=self.fixture()
        tx=self.response['transactions']['c'*64]
        tx.update(orig_status='uninit',end_status='active')
        tx['description']['destroyed']=False
        tx['description']['compute_ph'].update(account_activated=True,msg_state_used=True)
        tx['account_state_before'].update(account_status='uninit',code_hash=None,data_hash=None)
        tx['account_state_after']['data_hash']=initial['initial_next_data_hash']
        tx['in_msg']['init_state']={'hash':self.source.split(':')[1]}

    async def test_initial_source_projection_requires_exact_deployed_state(self):
        self.deployment_fixture()
        result=await self.project()
        self.assertTrue(result['deployment_projected']);self.assertEqual(result['estimated_fee_nano'],'3000000')
        self.assertFalse(result['production_signing'])

    async def test_deployment_trace_wrong_init_key_seqno_or_missing_activation_fail_closed(self):
        self.deployment_fixture();original=copy.deepcopy(self.response)
        for change in (lambda t:t['in_msg']['init_state'].update(hash='0'*64),
                lambda t:t['account_state_after'].update(data_hash='0'*64),
                lambda t:t['description']['compute_ph'].update(account_activated=False),
                lambda t:t['description']['compute_ph'].update(msg_state_used=False),
                lambda t:t.update(end_status='uninit')):
            self.response=copy.deepcopy(original);change(self.response['transactions']['c'*64])
            with self.subTest(change=change),self.assertRaises(MoneyEmulationError): await self.project()

    async def test_first_credit_bounce_and_inconsistent_balance_never_authorize_signing(self):
        self.response['transactions']['d'*64].update(credit_fixture())
        self.response['transactions']['d'*64]['in_msg']['bounce']=True
        with self.assertRaises(MoneyEmulationError): await self.project()
        self.response['transactions']['d'*64]['in_msg']['bounce']=False
        self.response['transactions']['d'*64]['account_state_after']['balance']='190000000'
        with self.assertRaises(MoneyEmulationError): await self.project()

    async def test_confirmation_context_tampered_quote_fail_before_provider(self):
        with self.assertRaises(MoneyEmulationError): await self.project(confirmed=False)
        with self.assertRaises(MoneyEmulationError): await self.project(ui_context='c'*64)
        self.plan['amount_nano']='200000000'
        with self.assertRaises(MoneyEmulationError): await self.project()
        self.assertEqual(self.requests,[])

    async def test_incomplete_wrong_block_body_identity_and_extra_children(self):
        original=copy.deepcopy(self.response)
        changes=[lambda r:r.update(is_incomplete=True),lambda r:r.update(mc_block_seqno=100),
            lambda r:r['trace']['children'].append(r['trace']['children'][0]),
            lambda r:r['transactions']['c'*64]['in_msg']['message_content'].update(hash='0'*64),
            lambda r:r['transactions']['c'*64]['account_state_before'].update(data_hash='0'*64),
            lambda r:r['transactions']['d'*64].update(account=self.source)]
        for change in changes:
            self.response=copy.deepcopy(original);change(self.response)
            with self.subTest(change=change),self.assertRaises(MoneyEmulationError): await self.project()

    async def test_fee_cap_old_funds_uninitialized_and_bounce_fail_closed(self):
        original=copy.deepcopy(self.response)
        changes=[lambda r:r['transactions']['c'*64]['account_state_after'].update(balance='289000000'),
            lambda r:r['transactions']['d'*64]['account_state_after'].update(balance='270000000'),
            lambda r:r['transactions']['d'*64]['account_state_before'].update(account_status='uninit'),
            lambda r:r['transactions']['d'*64]['in_msg'].update(bounced=True),
            lambda r:r['transactions']['c'*64]['description']['action'].update(success=False),
            lambda r:r['transactions']['c'*64]['out_msgs'][0].update(value='200000000'),
            lambda r:r['transactions']['c'*64]['out_msgs'][0].update(value_extra_currencies={'1':'1'})]
        for change in changes:
            self.response=copy.deepcopy(original);change(self.response)
            with self.subTest(change=change),self.assertRaises(MoneyEmulationError): await self.project()

    async def test_no_retry_redirect_rate_limit_and_stale_result(self):
        for status in (302,429,500):
            self.requests.clear()
            with self.assertRaises(MoneyEmulationError): await self.project(handler=lambda _:httpx.Response(status))
            self.assertEqual(len(self.requests),1)
        with self.assertRaises(MoneyEmulationError):
            await self.project(handler=lambda _:httpx.Response(200,content=b'{"trace":{},"trace":{}}'))
        def stale(_): self.now=1031;return httpx.Response(200,json=self.response)
        with self.assertRaises(MoneyEmulationError): await self.project(handler=stale)

    async def test_cancel_propagates(self):
        def cancel(_): raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.project(handler=cancel)
