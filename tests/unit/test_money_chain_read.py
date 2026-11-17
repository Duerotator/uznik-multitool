import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Pinned provider reads using MockTransport and public compiled code only."""
import base64
import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

import httpx

from modules.money_chain_read import TonCenterWalletReader, MoneyReadError, getter
from modules.wallet_tg_codec import WalletTgCodec, WalletCodecError, HELPER, REV00_HASH, SUBWALLET


AVAILABLE=bool(shutil.which('node') and (HELPER.parent/'node_modules'/'@ton'/'core'/'package.json').is_file())
ANCHOR='d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a'


@unittest.skipUnless(AVAILABLE,'Pinned local codec missing; skips are not scoped acceptance')
class ChainReadTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Public state only, not a seed or a financial signature. Pinned upstream
        # compiled implementation is accompanied by its original MIT license.
        script="""const {beginCell,Cell}=require('@ton/core');
          const code=Cell.fromBoc(Buffer.from('b5ee9c7241010101001a000030ff00209821d7498308b9f240df8085f833d0ed1e20ed53d969427e39','hex'))[0];
          const data=beginCell().storeUint(0,8).storeUint(7,32).storeUint(0x7fff7ffd,32).storeBuffer(Buffer.from(process.argv[1],'hex')).endCell();
          console.log(JSON.stringify({code:code.toBoc().toString('base64'),data:data.toBoc().toString('base64')}));"""
        cls.cells=json.loads(subprocess.run([shutil.which('node'),'-e',script,ANCHOR],cwd=HELPER.parent,
            check=True,capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0).stdout)
        cls.implementation=(Path(__file__).resolve().parents[1]/'fixtures'/'wallet_tg_rev00.b64').read_text().strip()

    def setUp(self):
        self.codec=WalletTgCodec(clock=lambda:1000)
        self.source=self.codec.derive(ANCHOR,'testnet')['source'];self.requests=[]
        self.master={'last':{'workchain':-1,'shard':'8000000000000000','seqno':99,'root_hash':'a'*64,'gen_utime':'1000'}}
        self.account={'ok':True,'result':{'address':self.source,'state':'active','balance':'500000000',
            'sync_utime':1000,'last_transaction_id':{'lt':'1','hash':base64.b64encode(bytes(32)).decode()},
            'block_id':{'seqno':98},**self.cells}}
        self.methods={'seqno':7,'get_subwallet_id':SUBWALLET['testnet'],'get_public_key':int(ANCHOR,16)}
        self.delay=patch('modules.money_chain_read.asyncio.sleep',return_value=None);self.delay.start()

    def tearDown(self): self.delay.stop()

    async def read(self,*,confirmed=True,override=None,allow_deployment=False):
        def handler(request):
            self.requests.append(request)
            if override is not None: return override(request)
            path=request.url.path
            if path.endswith('masterchainInfo'): value=self.master
            elif path.endswith('getAddressInformation'): value=self.account
            elif path.endswith('getConfigParam'): value={'ok':True,'result':{'config':{'bytes':self.implementation}}}
            else:
                data=json.loads(request.content)
                value={'ok':True,'result':{'exit_code':0,'stack':[['num',hex(self.methods[data['method']])]]}}
            return httpx.Response(200,json=value)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TonCenterWalletReader(client=client,codec=self.codec,clock=lambda:1000).read(
                self.source,'testnet',ANCHOR,confirmed=confirmed,allow_deployment=allow_deployment)

    async def test_funded_initial_source_requires_opt_in_and_never_runs_nonexistent_getters(self):
        self.account['result'].update(state='uninitialized',code='',data='')
        with self.assertRaises(MoneyReadError): await self.read()
        self.requests.clear();result=await self.read(allow_deployment=True)
        self.assertFalse(result['active']);self.assertEqual(result['seqno'],0)
        self.assertIsNone(result['code_hash']);self.assertIsNone(result['data_hash'])
        self.assertEqual(result['public_key_origin'],'initial_anchor_not_chain_getter')
        self.assertEqual(result['public_key'],ANCHOR)
        self.assertEqual(result['implementation_hash'],REV00_HASH)
        self.assertEqual(len(self.requests),3)
        self.assertTrue(all(r.method=='GET' for r in self.requests))

    async def test_initial_source_unknown_storage_zero_balance_and_implementation_fail_closed(self):
        self.account['result'].update(state='uninitialized',code='',data='')
        self.account['result']['code']=self.cells['code']
        with self.assertRaises(MoneyReadError): await self.read(allow_deployment=True)
        self.account['result'].update(code='',balance='0')
        with self.assertRaises(MoneyReadError): await self.read(allow_deployment=True)
        self.account['result']['balance']='500000000';self.implementation=self.cells['code']
        with self.assertRaises(MoneyReadError): await self.read(allow_deployment=True)

    async def test_real_public_code_hash_and_pinned_getters_no_signature(self):
        result=await self.read()
        self.assertEqual(result['implementation_hash'],REV00_HASH)
        self.assertEqual(result['public_key'],ANCHOR)
        self.assertEqual(result['balance_nano'],'500000000')
        self.assertEqual(result['last_transaction'],{'lt':'1','hash':'0'*64})
        self.assertEqual(result['verification'],'provider_reported_not_chain_proof')
        self.assertFalse(result['production_signing'])
        self.assertEqual(len(self.requests),6)
        for request in self.requests:
            self.assertEqual(request.url.host,'testnet.toncenter.com')
            if request.url.path.endswith('masterchainInfo'): continue
            if request.method=='POST':
                body=json.loads(request.content)
                self.assertEqual(set(body),{'address','method','stack','seqno'})
                self.assertEqual(body['seqno'],99);self.assertEqual(body['stack'],[])
            else: self.assertEqual(request.url.params['seqno'],'99')
        self.assertEqual(self.requests[2].url.params['param'],'-123')

    async def test_confirmation_before_network(self):
        with self.assertRaises(MoneyReadError): await self.read(confirmed=False)
        self.assertEqual(self.requests,[])

    async def test_state_getter_mismatch_and_unknown_implementation_fail_closed(self):
        self.methods['seqno']=8
        with self.assertRaises(MoneyReadError): await self.read()
        self.methods['seqno']=7;self.implementation=self.cells['code']
        with self.assertRaises(MoneyReadError): await self.read()

    async def test_stale_master_wrong_account_and_inactive_source(self):
        original=copy.deepcopy(self.master)
        for stamp in ('879','1016'):
            self.master['last']['gen_utime']=stamp
            with self.assertRaises(MoneyReadError): await self.read()
        self.master=original
        self.account['result']['state']='uninitialized'
        with self.assertRaises(MoneyReadError): await self.read()
        self.account['result']['state']='active';self.account['result']['address']='0:'+'02'*32
        with self.assertRaises(MoneyReadError): await self.read()

    async def test_http_failures_duplicate_json_and_size_have_no_retries(self):
        for response in (httpx.Response(302,headers={'location':'https://example.com'}),
                         httpx.Response(429),httpx.Response(200,content=b'{"last":{},"last":{}}'),
                         httpx.Response(200,content=b' '* (1024*1024+1))):
            self.requests.clear()
            with self.subTest(status=response.status_code),self.assertRaises(MoneyReadError):
                await self.read(override=lambda _:response)
            self.assertEqual(len(self.requests),1)

    async def test_unsigned_drain_is_zero_signature_mode130_not_destroy(self):
        result=self.codec.prepare_emulation(network='testnet',anchor_public_key=ANCHOR,seqno=7,
            valid_until=1180,destination='0:'+'02'*32,amount_nano='0',intent_id='e'*32,send_all=True)
        script="""const {Cell,loadMessage}=require('@ton/core');
          const m=loadMessage(Cell.fromBoc(Buffer.from(process.argv[1],'base64'))[0].beginParse());
          const b=m.body.beginParse(),sig=b.loadBuffer(64);b.loadUint(32);b.loadUint(32);b.loadUint(32);b.loadUint(32);
          console.log(JSON.stringify({sig:sig.toString('hex'),mode:b.loadUint(8),init:m.init}));"""
        decoded=json.loads(subprocess.run([shutil.which('node'),'-e',script,result['unsigned_external_boc']],
            cwd=HELPER.parent,check=True,capture_output=True,timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0).stdout)
        self.assertEqual(decoded,{'sig':'0'*128,'mode':130,'init':None})
        self.assertFalse(result['signature_valid']);self.assertFalse(result['broadcastable'])
        with self.assertRaises(WalletCodecError):
            self.codec.prepare(network='testnet',anchor_public_key=ANCHOR,seqno=7,valid_until=1180,
                destination='0:'+'02'*32,amount_nano='100000000',intent_id='e'*32,mode=130)

    def test_dedicated_drain_codec_cannot_add_destroy_deploy_amount_or_internal_channel(self):
        prepared=self.codec.prepare_drain(network='testnet',anchor_public_key=ANCHOR,seqno=7,
            valid_until=1180,destination='0:'+'02'*32,intent_id='e'*32)
        self.assertEqual(prepared['request']['mode'],130)
        for changed in ({'mode':162},{'amount_nano':'1'},{'channel':'internal'},{'deploy':True}):
            with self.assertRaises(WalletCodecError): self.codec._run({**prepared['request'],**changed})

    async def test_cancel_propagates_without_retry(self):
        def cancel(_): raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.read(override=cancel)
        self.assertEqual(len(self.requests),1)


class GetterTests(unittest.TestCase):
    def test_exact_stack_width_and_type(self):
        for value in (True,None,'-1','1e3','0x100000000'):
            with self.subTest(value=value),self.assertRaises(MoneyReadError):
                getter({'ok':True,'result':{'exit_code':0,'stack':[['num',value]]}},32)
