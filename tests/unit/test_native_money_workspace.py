import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Local setup/review/CAS tests. Fake session/provider, ephemeral unfunded keys."""
import json
import asyncio
from pathlib import Path
import unittest

import test_money_execution as fixture
from modules.money_workspace import NativeMoneyWorkspace,amount
from modules.money_execution import NativeExecutionError
from modules.finance_money import FinanceError
from modules.money_native import NativeMoneyError


@unittest.skipUnless(fixture.AVAILABLE,'Pinned codec missing; not scoped acceptance')
class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixture.ExecutionTests.asyncSetUp(self)
        self.workspace=NativeMoneyWorkspace(self.money,vault=self.vault,codec=self.codec,reader=self.reader,
            emulator=self.emulator,client_factory=self.executor.factory,allow=lambda:True)
        self.args={'scope_ids':{'one','two'},'ui_context':self.ui}
        seed=bytearray(self.seed)
        self.workspace.import_key('one',seed,self.public,expected_revision=self.identity['revision'],confirmed=True,**self.args)
        self.assertEqual(seed,bytes(32))
        self.values={'target_id':'two','target_address':self.target,'debit':'0.2','fee':'0.01','daily':'0.5','reserve':'0','allow_existing':True}
        self.report=self.workspace.save_policy('one',self.values,expected_revision=None,confirmed=True,**self.args)
    async def asyncTearDown(self): await fixture.ExecutionTests.asyncTearDown(self)
    async def preview(self,**changes):
        return await self.workspace.preview('one',**{**self.args,'confirmed':True,**changes})
    async def send(self,review,**changes):
        return await self.workspace.send('one',review['review_token'],**{**self.args,'confirmation':'SEND one TO two',**changes})

    async def test_local_setup_never_calls_provider_or_stores_seed(self):
        self.assertEqual(self.reader.read.await_count,0);self.assertEqual(self.client.get_me.await_count,0)
        self.assertFalse(self.report['provider_read_performed'])
        self.assertNotIn(self.seed,self.money.path.read_bytes())
        self.assertNotIn('seed',json.dumps(self.report))

    async def test_preview_never_decrypts_signing_seed_or_creates_payment(self):
        reads=self.vault.protector.reads
        review=await self.preview()
        self.assertEqual(self.vault.protector.reads,reads)
        self.assertFalse(review['signed']);self.assertEqual(review['limits']['amount_nano'],'190000000')
        with self.money.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)
        self.assertNotIn(review['review_token'].encode(),self.money.path.read_bytes())

    async def test_exact_one_use_send_through_actual_executor(self):
        review=await self.preview();result=await self.send(review)
        self.assertEqual(result['state'],'wallet_submitted_unverified')
        with self.assertRaises(NativeExecutionError): await self.send(review)
        self.assertEqual(self.client.native_money_send.await_count,1)

    async def test_policy_cas_and_saved_edit_invalidates_previous_review(self):
        review=await self.preview()
        with self.assertRaises(NativeExecutionError):
            self.workspace.save_policy('one',self.values,expected_revision=None,confirmed=True,**self.args)
        changed={**self.values,'reserve':'0.1'}
        self.workspace.save_policy('one',changed,expected_revision=self.report['policy']['revision'],confirmed=True,**self.args)
        with self.assertRaises(NativeExecutionError): await self.send(review)
        self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_failed_import_wipes_input_and_replacement_disables_policy(self):
        seed=bytearray(self.seed)
        with self.assertRaises(NativeExecutionError):
            self.workspace.import_key('one',seed,self.public,expected_revision=None,confirmed=False,**self.args)
        self.assertEqual(seed,bytes(32))
        record=self.workspace.report('one',**self.args)['key']
        self.workspace.import_key('one',bytearray(self.seed),self.public,expected_revision=record['revision'],confirmed=True,**self.args)
        self.assertFalse(self.workspace.report('one',**self.args)['policy_enabled'])
        with self.assertRaises(NativeExecutionError): await self.preview()

    async def test_expired_unconfirmed_or_wrong_scope_cannot_send(self):
        review=await self.preview()
        with self.assertRaises(NativeExecutionError): await self.send(review,confirmation='yes')
        with self.assertRaises(NativeExecutionError): await self.send(review,scope_check=lambda:False)
        self.now=1061
        with self.assertRaises(NativeExecutionError): await self.send(review)
        self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_changed_wallet_amount_after_review_cannot_be_signed(self):
        review=await self.preview();self.observed['balance_nano']='510000000'
        with self.assertRaises(NativeExecutionError): await self.send(review)
        self.assertEqual(self.client.native_money_send.await_count,0)
        with self.money.db() as db: self.assertEqual(db.execute('SELECT state FROM intents').fetchone()[0],'operator_not_sent')
        with self.assertRaises(NativeExecutionError): await self.send(review)

    async def test_target_username_change_and_denied_preview_have_no_send(self):
        with self.assertRaises(NativeExecutionError): await self.preview(confirmed=False)
        self.accounts['two'].username='changed'
        with self.assertRaises(NativeExecutionError): await self.preview()
        self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_http_facade_still_rejects_seed_and_native_execution(self):
        with self.assertRaises(FinanceError):
            await self.money.action('native-execute',{'account_id':'one','seed':'not-a-key'},**self.args)

    async def test_recipient_read_requires_consent_and_never_force_or_signs(self):
        with self.assertRaises(NativeExecutionError):
            await self.workspace.resolve('one','two',confirmed=False,**self.args)
        reads=self.vault.protector.reads
        result=await self.workspace.resolve('one','two',confirmed=True,**self.args)
        self.assertEqual(result['address'],self.target)
        self.assertEqual(self.vault.protector.reads,reads)
        self.assertIs(self.client.native_money_recipient.await_args.kwargs['prepare_address'],False)
        self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_first_credit_preview_send_and_changed_state_admission(self):
        original=self.emulator.project.side_effect
        async def credit(*args,**kwargs):
            return {**await original(*args,**kwargs),'recipient_state':'uninitialized_credited'}
        self.emulator.project.side_effect=credit
        review=await self.preview()
        self.assertEqual(review['recipient_state'],'uninitialized_credited')
        self.assertEqual(review['money_activated'],'unknown')
        self.emulator.project.side_effect=original
        with self.assertRaises(NativeExecutionError): await self.send(review)
        self.assertEqual(self.client.native_money_send.await_count,0)
        with self.money.db() as db:
            self.assertEqual(db.execute('SELECT stage FROM native_commitments').fetchone()[0],'preflight_failed')

    async def test_first_credit_can_pass_explicit_one_shot_pipeline_not_activation_loop(self):
        original=self.emulator.project.side_effect
        async def credit(*args,**kwargs):
            return {**await original(*args,**kwargs),'recipient_state':'uninitialized_credited'}
        self.emulator.project.side_effect=credit
        review=await self.preview();result=await self.send(review)
        self.assertEqual(result['state'],'wallet_submitted_unverified')
        self.assertFalse(result['confirmed_on_chain'])
        self.assertEqual(self.client.native_money_send.await_count,1)
        with self.assertRaises(NativeExecutionError): await self.send(review)

    async def test_prepare_recipient_reuses_metadata_lease_no_key_or_payment_and_no_auto_send(self):
        self.client.native_money_state.return_value['balance_nano']='500000000'
        reads=self.vault.protector.reads;self.vault.lock()
        result=await self.workspace.prepare_recipient('one','two',confirmation='PREPARE one TO two',confirmed=True,**self.args)
        self.assertTrue(result['prepare_address_requested']);self.assertEqual(result['address'],self.target)
        self.assertFalse(result['signer_ready']);self.assertEqual(result['money_activated'],'unknown')
        self.assertEqual(self.vault.protector.reads,reads)
        kwargs=self.client.native_money_recipient.await_args.kwargs
        self.assertTrue(kwargs['prepare_address']);self.assertTrue(callable(kwargs['admission_check']))
        self.assertEqual(self.client.native_money_send.await_count,0)
        with self.money.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)
        with self.assertRaises(NativeMoneyError):
            await self.workspace.prepare_recipient('one','two',confirmation='PREPARE one TO two',confirmed=True,**self.args)
        self.assertEqual(self.client.native_money_recipient.await_count,1)

    async def test_prepare_recipient_requires_exact_consent_and_current_scope_before_connection(self):
        for values in ({'confirmed':False},{'confirmation':'yes'},{'scope_check':lambda:False}):
            with self.subTest(values=values),self.assertRaises(NativeExecutionError):
                await self.workspace.prepare_recipient('one','two',**{**self.args,'confirmed':True,'confirmation':'PREPARE one TO two',**values})
        self.assertEqual(self.client.get_me.await_count,0)
        self.assertEqual(self.client.native_money_recipient.await_count,0)

    async def test_prepare_recipient_requires_ready_funded_source_no_force_on_failure(self):
        for state in ({'availability':'missing'},{'availability':'ready','balance_nano':'0'}):
            self.client.native_money_state.return_value=state
            with self.assertRaises(NativeMoneyError):
                await self.workspace.prepare_recipient('one','two',confirmation='PREPARE one TO two',confirmed=True,**self.args)
            self.now+=61
        self.assertEqual(self.client.native_money_recipient.await_count,0)

    async def test_prepare_recipient_scope_revoked_inside_adapter_never_forces(self):
        self.client.native_money_state.return_value['balance_nano']='500000000'
        async def recipient(*args,**kwargs):
            self.accounts['two'].username='changed'
            kwargs['admission_check']()
            self.fail('Revoked scope must fail before force')
        self.client.native_money_recipient.side_effect=recipient
        with self.assertRaises(NativeMoneyError):
            await self.workspace.prepare_recipient('one','two',confirmation='PREPARE one TO two',confirmed=True,**self.args)
        self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_prepare_timeout_has_no_retry_and_requires_later_explicit_check(self):
        self.client.native_money_state.return_value['balance_nano']='500000000'
        self.client.native_money_recipient.side_effect=asyncio.TimeoutError()
        with self.assertRaises(NativeMoneyError):
            await self.workspace.prepare_recipient('one','two',confirmation='PREPARE one TO two',confirmed=True,**self.args)
        self.assertEqual(self.client.native_money_recipient.await_count,1)
        with self.assertRaises(NativeMoneyError):
            await self.workspace.prepare_recipient('one','two',confirmation='PREPARE one TO two',confirmed=True,**self.args)
        self.assertEqual(self.client.native_money_recipient.await_count,1)

    async def test_prepare_lease_expiry_during_peer_resolution_blocks_request(self):
        self.client.native_money_state.return_value['balance_nano']='500000000'
        async def recipient(*args,**kwargs):
            self.now+=61
            kwargs['admission_check']()
            self.fail('Expired lease must fail before force')
        self.client.native_money_recipient.side_effect=recipient
        with self.assertRaises(NativeMoneyError):
            await self.workspace.prepare_recipient('one','two',confirmation='PREPARE one TO two',confirmed=True,**self.args)
        self.assertEqual(self.client.native_money_send.await_count,0)

    async def test_prepare_without_returned_address_never_claims_activation_or_sends(self):
        self.client.native_money_state.return_value['balance_nano']='500000000'
        self.client.native_money_recipient.return_value={'address':None,'availability':'address_not_returned'}
        with self.assertRaises(NativeExecutionError):
            await self.workspace.prepare_recipient('one','two',confirmation='PREPARE one TO two',confirmed=True,**self.args)
        self.assertEqual(self.client.native_money_recipient.await_count,1)
        self.assertEqual(self.client.native_money_send.await_count,0)
        with self.assertRaises(NativeMoneyError):
            await self.workspace.prepare_recipient('one','two',confirmation='PREPARE one TO two',confirmed=True,**self.args)
        self.assertEqual(self.client.native_money_recipient.await_count,1)

    def configure_initial_deployment(self):
        implementation=(Path(__file__).resolve().parents[1]/'fixtures/wallet_tg_rev00.b64').read_text().strip()
        initial=self.codec.inspect_initial(network='mainnet',anchor_public_key=self.public,implementation_boc=implementation)
        self.observed.update(initial,active=False,chain_state='uninit',code_hash=None,data_hash=None,
            public_key_origin='initial_anchor_not_chain_getter')
        async def project(policy,observed,plan,**kwargs):
            unsigned=self.codec.prepare_emulation(network='mainnet',anchor_public_key=self.public,seqno=0,
                valid_until=plan['valid_until'],destination=self.target,amount_nano=plan['amount_nano'],intent_id=plan['intent_id'],deploy=True)
            return {'verification':'provider_reported_simulation_not_receipt','quote_hash':plan['quote_hash'],
                'observation_hash':plan['observation_hash'],'unsigned_body_hash':unsigned['unsigned_body_hash'],
                'checked_at':self.now,'deployment_projected':True,'recipient_state':'active',
                'estimated_fee_nano':'3000000','source_debit_nano':'192000000','recipient_net_nano':'189000000'}
        self.emulator.project.side_effect=project

    async def test_deploy_review_requires_special_confirmation_before_key_use(self):
        self.configure_initial_deployment();before=self.vault.protector.reads
        review=await self.preview()
        self.assertTrue(review['deployment_required']);self.assertEqual(self.vault.protector.reads,before)
        self.assertIsNone(review['limits']['data_hash'])
        with self.assertRaises(NativeExecutionError): await self.send(review)
        self.assertEqual(self.vault.protector.reads,before)
        result=await self.send(review,confirmation='DEPLOY AND SEND one TO two')
        self.assertTrue(result['deployment_requested']);self.assertFalse(result['confirmed_on_chain'])
        self.assertEqual(self.client.native_money_send.await_count,1)

    async def test_normal_review_cannot_be_reused_to_deploy_changed_source(self):
        review=await self.preview();self.configure_initial_deployment()
        before=self.vault.protector.reads
        with self.assertRaises(NativeExecutionError): await self.send(review)
        self.assertEqual(self.vault.protector.reads,before);self.assertEqual(self.client.native_money_send.await_count,0)


class AmountTests(unittest.TestCase):
    def test_exact_decimals_without_float_exponents(self):
        self.assertEqual(amount('0.000000001'),'1');self.assertEqual(amount('0'),'0')
        for value in ('1e-9','0.0000000001',0.1,True,'-1','01'):
            with self.subTest(value=value),self.assertRaises(NativeExecutionError): amount(value)
