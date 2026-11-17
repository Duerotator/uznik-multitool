import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Credit-only receiver fixtures, not real wallet activation/TVM/network."""
import copy
import unittest

from modules.money_credit import uninitialized_credit


def credit_fixture(value='190000000',before='0',fees='1000000',status='nonexist'):
    def state(status,balance):
        return {'account_status':status,'balance':balance,'extra_currencies':None,
            'code_hash':None,'data_hash':None,'frozen_hash':None}
    return {'orig_status':status,'end_status':'uninit','out_msgs':[],
        'total_fees':fees,'total_fees_extra_currencies':{},
        'description':{'type':'ord','aborted':True,'destroyed':False,
            'compute_ph':{'skipped':True,'reason':'cskip_no_state'},
            'credit_ph':{'credit':value,'credit_extra_currencies':{}}},
        'account_state_before':state(status,before),
        'account_state_after':state('uninit',str(int(before)+int(value)-int(fees)))}


class CreditTests(unittest.TestCase):
    def setUp(self):
        self.tx=credit_fixture()
        self.msg={'value':'190000000','bounce':False,'bounced':False}
    def test_credit_is_net_of_fee_not_activation_or_signer(self):
        result=uninitialized_credit(self.tx,self.msg)
        self.assertEqual(result['recipient_net_nano'],'189000000')
        self.assertEqual(result['recipient_state'],'uninitialized_credited')
        self.assertEqual(result['money_activated'],'unknown')
        self.assertFalse(result['signer_ready'])
    def test_existing_code_less_balance_is_not_counted_as_new_credit(self):
        tx=credit_fixture(before='50000000',status='uninit')
        self.assertEqual(uninitialized_credit(tx,self.msg)['recipient_net_nano'],'189000000')
    def test_unsafe_status_code_execution_or_effects_are_rejected(self):
        changes=[lambda t:t.update(orig_status='active'),lambda t:t.update(end_status='active'),
            lambda t:t['account_state_before'].update(code_hash='a'*64),
            lambda t:t['account_state_after'].update(data_hash='a'*64),
            lambda t:t['description'].update(destroyed=True),
            lambda t:t['description'].update(action={}),lambda t:t['description'].update(bounce={}),
            lambda t:t['description'].update(aborted=False),lambda t:t.update(out_msgs=[{}]),
            lambda t:t['description']['compute_ph'].update(reason='cskip_no_gas'),
            lambda t:t['description']['compute_ph'].update(success=True),
            lambda t:t['description']['compute_ph'].update(exit_code=0)]
        for change in changes:
            tx=copy.deepcopy(self.tx);change(tx)
            with self.subTest(change=change),self.assertRaises(ValueError): uninitialized_credit(tx,self.msg)
    def test_bounce_init_extra_currencies_are_rejected(self):
        for changes in ({'bounce':True},{'bounce':None},{'bounced':True},{'init_state':{}},
                        {'value_extra_currencies':{'1':'1'}},{'extra_currencies':{'1':'1'}}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                uninitialized_credit(self.tx,{**self.msg,**changes})
    def test_incomplete_or_inconsistent_credit_fee_balance_fails_closed(self):
        changes=[lambda t:t.update(total_fees='0'),lambda t:t.pop('total_fees'),
            lambda t:t['account_state_before'].update(balance='1'),
            lambda t:t['account_state_after'].update(balance='0'),
            lambda t:t['description']['credit_ph'].update(credit='1'),
            lambda t:t['description']['credit_ph'].update(due_fees_collected='1'),
            lambda t:t['account_state_after'].update(extra_currencies={'1':'1'})]
        for change in changes:
            tx=copy.deepcopy(self.tx);change(tx)
            with self.subTest(change=change),self.assertRaises(ValueError): uninitialized_credit(tx,self.msg)
