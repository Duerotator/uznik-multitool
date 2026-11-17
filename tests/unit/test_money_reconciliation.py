import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Synthetic v3 responses only; no wallet, provider accounts or real funds."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx

from modules.money_reconciliation import reconcile, ReconciliationError
from test_money_credit import credit_fixture

SOURCE = '0:' + '01' * 32
DEST = '0:' + '02' * 32
INTENT = {'id': 'a' * 32, 'network': 'testnet', 'source': SOURCE,
          'destination': DEST, 'amount': '100', 'prompted': 1000, 'expires': 1180}


def message():
    return {'source': SOURCE, 'destination': DEST, 'value': '100', 'hash': 'b' * 64,
            'bounced': False, 'opcode': 0,
            'message_content': {'decoded': {'type': 'ton_transfer', 'comment': 'accmanager:' + INTENT['id'], 'encrypted': False}}}


def transaction(account, hash_value):
    return {'account': account, 'hash': hash_value, 'now': 1001, 'emulated': False, 'finality': 2,
            'description': {'type': 'ord', 'aborted': False,
                            'compute_ph': {'success': True, 'skipped': False, 'exit_code': 0}},
            'out_msgs': [], 'in_msg': None}


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = transaction(SOURCE, 'c' * 64)
        self.source['out_msgs'] = [message()]
        self.dest = transaction(DEST, 'd' * 64)
        self.dest['in_msg'] = message()
        self.requests = []
        self.delay = patch('modules.money_reconciliation.asyncio.sleep', return_value=None)
        self.delay.start()

    def tearDown(self):
        self.delay.stop()

    async def check(self, source=None, dest=None):
        def handler(request):
            self.requests.append(request)
            rows = (source if source is not None else [self.source]) if request.url.path.endswith('/transactions') else (dest if dest is not None else [self.dest])
            return httpx.Response(200, json={'transactions': rows})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await reconcile(INTENT, client=client)

    async def test_exact_finalized_pair_fixed_get_and_hash_projection(self):
        result = await self.check()
        self.assertEqual(result['status'], 'provider_delivered')
        self.assertEqual(result['recipient_tx'], 'd' * 64)
        self.assertEqual(result['verification'], 'provider_reported_not_chain_proof')
        self.assertFalse(result['automatic_retry'])
        self.assertNotIn('message_content', json.dumps(result))
        self.assertEqual(len(self.requests), 2)
        for request in self.requests:
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.host, 'testnet.toncenter.com')
        self.assertEqual(self.requests[1].url.params['direction'], 'in')
        self.assertEqual(self.requests[1].url.params['msg_hash'], 'b' * 64)

    async def test_missing_duplicate_and_truncated_never_mean_not_sent(self):
        for rows, expected in (([], 'not_found'), ([self.source] * 2, 'ambiguous'), ([self.source] * 100, 'search_truncated')):
            result = await self.check(source=rows)
            self.assertEqual(result['status'], expected)
        self.assertEqual(len(self.requests), 3)

    async def test_wrong_comment_amount_destination_not_matched(self):
        original = copy.deepcopy(self.source)
        for key, value in (('value', '101'), ('destination', SOURCE), ('bounced', True), ('opcode', False)):
            self.source = copy.deepcopy(original)
            self.source['out_msgs'][0][key] = value
            self.assertEqual((await self.check())['status'], 'not_found')
        self.source = copy.deepcopy(original)
        self.source['out_msgs'][0]['message_content']['decoded']['comment'] = 'other'
        self.assertEqual((await self.check())['status'], 'not_found')

    async def test_pending_emulation_missing_finality_never_delivered(self):
        for field, value in (('finality', 1), ('finality', True), ('finality', None), ('emulated', True)):
            self.source = transaction(SOURCE, 'c' * 64)
            self.source['out_msgs'] = [message()]
            self.source[field] = value
            self.assertEqual((await self.check())['status'], 'source_pending')

    async def test_receiver_failed_or_missing_or_pending_stays_unresolved(self):
        self.assertEqual((await self.check(dest=[]))['status'], 'recipient_pending')
        self.dest['finality'] = 1
        self.assertEqual((await self.check())['status'], 'recipient_pending')
        self.dest['finality'] = 2
        self.dest['description']['aborted'] = True
        self.assertEqual((await self.check())['status'], 'recipient_needs_review')
        self.dest['description']['aborted'] = False
        self.dest['description']['compute_ph']['skipped'] = True
        self.assertEqual((await self.check())['status'], 'recipient_needs_review')

    async def test_failed_action_and_bounce_do_not_assert_refund(self):
        self.source['description']['action'] = {'success': False, 'valid': True, 'no_funds': True, 'result_code': 37}
        self.assertEqual((await self.check())['status'], 'source_needs_review')
        del self.source['description']['action']
        self.dest['out_msgs'] = [{'bounced': True}]
        self.assertEqual((await self.check())['status'], 'recipient_needs_review')
        self.dest['out_msgs'] = [{'bounced': False}]
        self.assertEqual((await self.check())['status'], 'recipient_needs_review')

    async def test_wrong_receiver_hash_peer_or_time_rejected(self):
        original = copy.deepcopy(self.dest)
        for mutate in (lambda d: d.update(account=SOURCE), lambda d: d['in_msg'].update(hash='e' * 64),
                       lambda d: d.update(now=999), lambda d: d['in_msg'].update(value='99')):
            self.dest = copy.deepcopy(original)
            mutate(self.dest)
            with self.assertRaises(ReconciliationError):
                await self.check()

    async def test_redirect_duplicate_json_size_and_timeout_fail_closed(self):
        for response in (httpx.Response(302, headers={'location': 'https://evil.invalid'}),
                         httpx.Response(200, content=b'{"transactions":[],"transactions":[]}'),
                         httpx.Response(200, content=b'x' * (1024 * 1024 + 1))):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as client:
                with self.assertRaises(ReconciliationError):
                    await reconcile(INTENT, client=client)
        async def timeout(request):
            raise httpx.ReadTimeout('synthetic', request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with self.assertRaises(ReconciliationError):
                await reconcile(INTENT, client=client)

    async def test_cancellation_is_not_converted_to_success(self):
        async def cancel(request):
            raise asyncio.CancelledError()
        async with httpx.AsyncClient(transport=httpx.MockTransport(cancel)) as client:
            with self.assertRaises(asyncio.CancelledError):
                await reconcile(INTENT, client=client)

    async def test_finalized_first_credit_is_delivered_but_not_activated(self):
        self.dest.update(credit_fixture(value='100',fees='1'))
        self.dest['in_msg']['bounce']=False
        self.source['out_msgs'][0]['bounce']=False
        result=await self.check()
        self.assertEqual(result['status'],'provider_delivered')
        self.assertEqual(result['recipient_net_nano'],'99')
        self.assertEqual(result['money_activated'],'unknown')
        self.assertFalse(result['signer_ready'])
        self.assertEqual(result['recipient_tx'],'d'*64)

    async def test_active_receiver_net_is_distinct_from_nominal_and_old_balance(self):
        self.dest.update(orig_status='active',end_status='active',total_fees='1',lt='42',
            account_state_before={'account_status':'active','balance':'50'},
            account_state_after={'account_status':'active','balance':'149'})
        self.dest['description'].update(destroyed=False,bounce=None)
        self.dest['in_msg']['bounce']=False
        result=await self.check()
        self.assertEqual(result['status'],'provider_delivered')
        self.assertEqual(result['recipient_before_nano'],'50')
        self.assertEqual(result['recipient_net_nano'],'99')
        self.assertEqual(result['recipient_lt'],'42')
        self.assertEqual(result['transit_accounting'],'provider_reported_net_not_spend_authorized')
        self.assertFalse(result['signer_ready'])
        self.dest['total_fees']='0'
        incomplete=await self.check()
        self.assertEqual(incomplete['status'],'provider_delivered')
        self.assertEqual(incomplete['transit_accounting'],'needs_review')
        self.assertNotIn('recipient_net_nano',incomplete)
        self.dest['total_fees']='1';self.dest['out_msgs']=[{'value':'1'}]
        self.assertEqual((await self.check())['status'],'recipient_needs_review')

    async def test_first_credit_requires_finality_and_strict_credit_evidence(self):
        self.dest.update(credit_fixture(value='100',fees='1'))
        self.dest['in_msg']['bounce']=False
        self.source['out_msgs'][0]['bounce']=False
        self.dest['finality']=1
        self.assertEqual((await self.check())['status'],'recipient_pending')
        self.dest['finality']=2
        self.dest['description']['compute_ph']['reason']='cskip_no_gas'
        self.assertEqual((await self.check())['status'],'recipient_needs_review')
        self.dest['description']['compute_ph']['reason']='cskip_no_state'
        self.dest['total_fees']='0'
        self.assertEqual((await self.check())['status'],'recipient_needs_review')
        self.dest['total_fees']='1'
        self.source['out_msgs'][0]['bounce']=True
        self.assertEqual((await self.check())['status'],'recipient_needs_review')


if __name__ == '__main__':
    unittest.main()
