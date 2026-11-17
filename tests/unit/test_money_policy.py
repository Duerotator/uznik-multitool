import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Local numeric policy fixtures only; no wallets/keys/network or authorization."""
import unittest

from modules.money_policy import MoneyPolicyError, make_policy, quote
from modules.wallet_tg_codec import CODE_HASH, REV00_HASH, SUBWALLET


SOURCE='0:'+'01'*32
TARGET='0:'+'02'*32


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.settings=dict(account='fixture',account_context='a'*64,ui_context='b'*64,
            network='testnet',source=SOURCE,targets=[TARGET],scope='transit_only',
            reserve_nano='0',fee_cap_nano='10000000',transfer_debit_cap_nano='1000000000',
            daily_reservation_cap_nano='2000000000',confirmed=True)
        self.state=dict(network='testnet',address=SOURCE,active=True,code_hash=CODE_HASH,
            implementation_hash=REV00_HASH,subwallet_id=SUBWALLET['testnet'],provider='toncenter',
            verification='provider_reported_not_chain_proof',coherence='queries_pinned_by_mc_seqno',
            checked_at=1000,seqno=7,mc_block_seqno=99,public_key='c'*64,block_id='d'*64,data_hash='f'*64,balance_nano='500000000')
        self.args=dict(target=TARGET,transit_credit_nano='200000000',reserved_nano='0',
            min_transfer_nano='100000000',current_account_context='a'*64,current_ui_context='b'*64,
            now=1000,valid_until=1180,intent_id='e'*32)

    def check(self,**changes):
        return quote(make_policy(**self.settings),self.state,**{**self.args,**changes})

    def test_transit_preserves_old_funds_and_unknown_fee_is_not_zero(self):
        result=self.check()
        self.assertEqual(result['protected_nano'],'300000000')
        self.assertEqual(result['amount_nano'],'190000000')
        self.assertEqual(result['max_debit_nano'],'200000000')
        self.assertIsNone(result['estimated_fee_nano'])
        self.assertTrue(result['emulation_required'])
        self.assertFalse(result['production_signing'])
        self.assertFalse(result['broadcast_supported'])

    def test_drain_requires_no_old_funds_no_reserve_and_budget_for_all(self):
        with self.assertRaises(MoneyPolicyError): self.check(send_all=True)
        result=self.check(send_all=True,transit_credit_nano='500000000')
        self.assertEqual(result['send_mode'],130)
        self.assertFalse(result['send_mode'] & 32)
        self.assertEqual(result['amount_nano'],'0')
        for change in ({'reserve_nano':'1'},{'transfer_debit_cap_nano':'400000000'}):
            with self.subTest(change=change):
                original=dict(self.settings);self.settings.update(change)
                with self.assertRaises(MoneyPolicyError): self.check(send_all=True,transit_credit_nano='500000000')
                self.settings=original

    def test_all_native_requires_separate_authorization(self):
        self.settings['scope']='all_native_authorized'
        with self.assertRaises(MoneyPolicyError): self.check(send_all=True)
        self.settings['all_native_confirmed']=True
        self.assertEqual(self.check(send_all=True)['protected_nano'],'0')

    def test_daily_reservations_and_current_minimum(self):
        with self.assertRaises(MoneyPolicyError): self.check(reserved_nano='1950000000')
        with self.assertRaises(MoneyPolicyError): self.check(min_transfer_nano='200000000')
        with self.assertRaises(MoneyPolicyError): self.check(min_transfer_nano=None)
        with self.assertRaises(MoneyPolicyError): self.check(transit_credit_nano='500000001')

    def test_exact_context_target_freshness_and_integer_inputs(self):
        for change in ({'current_ui_context':'c'*64},{'current_account_context':'c'*64},
                       {'target':SOURCE},{'now':1031},{'now':999},{'valid_until':True},
                       {'reserved_nano':1},{'transit_credit_nano':'1e9'},{'send_all':1}):
            with self.subTest(change=change),self.assertRaises(MoneyPolicyError): self.check(**change)
        self.state['implementation_hash']='0'*64
        with self.assertRaises(MoneyPolicyError): self.check()

    def test_finite_policy_limits_and_confirmations(self):
        for change in ({'confirmed':False},{'targets':[TARGET,TARGET]},{'targets':[]},
                       {'fee_cap_nano':'0'},{'fee_cap_nano':'1000000000'},
                       {'daily_reservation_cap_nano':'5000000001'},{'reserve_nano':True}):
            with self.subTest(change=change),self.assertRaises(MoneyPolicyError):
                make_policy(**{**self.settings,**change})
