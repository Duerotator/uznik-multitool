import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Initial StateInit/signature/runtime with unfunded random keys and fake I/O."""
import copy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import AsyncMock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import test_money_execution as fixture
from modules.money_execution import NativeExecutionError
from modules.money_policy import quote,MoneyPolicyError
from modules.wallet_tg_codec import HELPER,WalletCodecError


@unittest.skipUnless(fixture.AVAILABLE,'Pinned codec missing; not scoped acceptance')
class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.ExecutionTests.asyncSetUp(self)
        implementation=(Path(__file__).resolve().parents[1]/'fixtures/wallet_tg_rev00.b64').read_text().strip()
        self.initial=self.codec.inspect_initial(network='mainnet',anchor_public_key=self.public,implementation_boc=implementation)
        self.observed.update(self.initial,active=False,chain_state='uninit',code_hash=None,data_hash=None,
            public_key_origin='initial_anchor_not_chain_getter')
        async def project(policy,observed,plan,**kwargs):
            unsigned=self.codec.prepare_emulation(network='mainnet',anchor_public_key=self.public,seqno=0,
                valid_until=1180,destination=self.target,amount_nano=plan['amount_nano'],intent_id=plan['intent_id'],deploy=True)
            return {'verification':'provider_reported_simulation_not_receipt','quote_hash':plan['quote_hash'],
                'observation_hash':plan['observation_hash'],'unsigned_body_hash':unsigned['unsigned_body_hash'],
                'checked_at':self.now,'deployment_projected':True,'recipient_state':'active',
                'estimated_fee_nano':'3000000','source_debit_nano':'192000000','recipient_net_nano':'189000000'}
        self.emulator.project.side_effect=project
    async def asyncTearDown(self): await fixture.ExecutionTests.asyncTearDown(self)
    async def execute(self,**changes):
        return await self.executor.execute(self.policy,self.identity,anchor_public_key=self.public,target_account_id='two',
            scope_ids={'one','two'},ui_context=self.ui,**{'confirmed':True,'deploy':True,**changes})

    async def test_initial_signed_envelope_deploys_only_exact_anchor_and_keeps_original_journal(self):
        result=await self.execute()
        self.assertEqual(result['state'],'wallet_submitted_unverified');self.assertTrue(result['deployment_requested'])
        self.assertFalse(result['confirmed_on_chain']);self.assertEqual(self.client.native_money_send.await_count,1)
        boc=self.client.native_money_send.await_args.args[3]
        import base64
        script="""const {Cell,loadMessage,contractAddress}=require('@ton/core');
          const msg=loadMessage(Cell.fromBoc(Buffer.from(process.argv[1],'base64'))[0].beginParse());
          const storage=msg.init.data.beginParse(),body=msg.body.beginParse();
          const sig=body.loadBuffer(64),payload=body.asCell();
          const op=body.loadUint(32),sub=body.loadUint(32),until=body.loadUint(32),seq=body.loadUint(32),mode=body.loadUint(8);
          console.log(JSON.stringify({source:msg.info.dest.toRawString(),init:contractAddress(0,msg.init).toRawString(),
            revision:storage.loadUint(8),initial_seqno:storage.loadUint(32),subwallet:storage.loadUint(32),
            key:storage.loadBuffer(32).toString('hex'),signature:sig.toString('hex'),hash:payload.hash().toString('hex'),seq,mode}));"""
        parsed=json.loads(subprocess.run([self.codec.node,'-e',script,base64.b64encode(boc).decode()],cwd=HELPER.parent,
            check=True,capture_output=True,timeout=10).stdout)
        self.assertEqual(parsed['source'],self.source);self.assertEqual(parsed['init'],self.source)
        self.assertEqual(parsed['key'],self.public);self.assertEqual(parsed['initial_seqno'],0)
        self.assertEqual(parsed['revision'],0);self.assertEqual(parsed['seq'],0);self.assertEqual(parsed['mode'],3)
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.public)).verify(bytes.fromhex(parsed['signature']),bytes.fromhex(parsed['hash']))
        with self.money.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT seqno FROM native_commitments').fetchone()[0],0)
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.client.native_money_send.await_count,1)

    async def test_ordinary_mode_cannot_silently_deploy(self):
        before=self.vault.protector.reads
        with self.assertRaises(NativeExecutionError): await self.execute(deploy=False)
        self.assertEqual(self.vault.protector.reads,before);self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_unprojected_deployment_or_changed_anchor_blocks_key_access(self):
        before=self.vault.protector.reads
        original=self.emulator.project.side_effect
        async def denied(*a,**k): return {**await original(*a,**k),'deployment_projected':False}
        self.emulator.project.side_effect=denied
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.vault.protector.reads,before)
        self.observed['public_key']='a'*64
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.vault.protector.reads,before)

    async def test_deployment_during_emulation_invalidates_before_signing(self):
        initial=copy.deepcopy(self.observed);latest={**initial,'active':True,'code_hash':fixture.CODE_HASH,'data_hash':'f'*64}
        self.reader.read=AsyncMock(side_effect=[initial,latest])
        before=self.vault.protector.reads
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.vault.protector.reads,before);self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_telegram_key_change_after_simulation_blocks_initial_signature(self):
        first=copy.deepcopy(self.client.native_money_state.return_value)
        self.client.native_money_state.side_effect=[first,{**first,'public_key':'a'*64}]
        before=self.vault.protector.reads
        with self.assertRaises(NativeExecutionError): await self.execute()
        self.assertEqual(self.vault.protector.reads,before);self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_deployment_codec_rejects_noninitial_or_internal_requests(self):
        values=dict(network='mainnet',anchor_public_key=self.public,seqno=0,valid_until=1180,
            destination=self.target,amount_nano='190000000',intent_id='e'*32,deploy=True)
        for change in ({'seqno':1},{'channel':'internal'},{'deploy':1},{'mode':130}):
            with self.subTest(change=change),self.assertRaises(WalletCodecError): self.codec.prepare(**{**values,**change})

    async def test_deployment_policy_requires_explicit_choice_and_never_drains(self):
        values=dict(target=self.target,transit_credit_nano='0',reserved_nano='0',min_transfer_nano='100000000',
            current_account_context=self.context,current_ui_context=self.ui,now=1000,valid_until=1180,intent_id='e'*32)
        with self.assertRaises(MoneyPolicyError): quote(self.policy,self.observed,**values)
        with self.assertRaises(MoneyPolicyError): quote(self.policy,self.observed,**values,deploy=True,send_all=True)
        self.assertTrue(quote(self.policy,self.observed,**values,deploy=True)['deploy'])
