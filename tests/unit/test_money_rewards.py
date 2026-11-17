import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Untrusted synthetic snapshots and temporary observation metadata only."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from core.models import AccountRecord
from modules.finance_money import MoneyService
from modules.money_rewards import NativeIncomingCandidates, RewardCandidateError


DEST='0:'+'02'*32
SOURCE='0:'+'03'*32
UI='a'*64


class RewardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=1000
        self.account=AccountRecord('one','Synthetic','pyrogram','session_file','never-open.session',user_id=42)
        self.money=MoneyService(SimpleNamespace(data_dir=Path(self.temp.name)),
            accounts=SimpleNamespace(get_account=lambda _:self.account),clock=lambda:self.now)
        self.service=NativeIncomingCandidates(self.money)
        self.tx={'account':DEST,'hash':'a'*64,'lt':'10','now':990,'emulated':False,'finality':2,
            'in_msg':{'hash':'b'*64,'source':SOURCE,'destination':DEST,'value':'100000000',
                'opcode':0,'bounced':False,'value_extra_currencies':{},'message_content':{'hash':'c'*64,
                    'decoded':{'type':'ton_transfer','comment':'hello','encrypted':False}}},
            'description':{'type':'ord','aborted':False,'destroyed':False,
                'compute_ph':{'skipped':False,'success':True,'exit_code':0}},'out_msgs':[]}

    def tearDown(self): self.temp.cleanup()
    def review(self,rows=None,**changes):
        raw=json.dumps({'transactions':[self.tx] if rows is None else rows}).encode()
        return self.service.review(raw,**{'account_id':'one','address':DEST,'network':'testnet',
            'scope_ids':{'one'},'ui_context':UI,'confirmed':True,**changes})
    def journal(self,*,source=SOURCE,evidence=None,network='testnet',ident='d'*32):
        with self.money.db() as db:
            db.execute('INSERT INTO intents(id,network,source,destination,amount,state) VALUES(?,?,?,?,?,?)',
                (ident,network,source,DEST,'100000000','uncertain'))
            if evidence is not None: db.execute('INSERT INTO reconciliation VALUES(?,?,?)',(ident,1000,json.dumps(evidence)))
    def count(self):
        with self.money.db() as db: return db.execute('SELECT count(*) FROM money_incoming_observations').fetchone()[0]

    def test_candidate_is_not_reward_or_balance_and_no_provider_or_payment(self):
        result=self.review();event=result['events'][0]
        self.assertTrue(event['candidate']);self.assertFalse(event['reward_proven'])
        self.assertFalse(event['spend_authorized']);self.assertFalse(result['collection_supported'])
        self.assertFalse(result['history_complete']);self.assertFalse(result['provider_request_performed'])
        self.assertEqual(result['verification'],'caller_supplied_provider_snapshot_not_chain_proof')
        with self.money.db() as db: self.assertEqual(db.execute('SELECT count(*) FROM intents').fetchone()[0],0)

    def test_message_and_tx_dedup_survives_service_restart(self):
        first=self.review([self.tx,self.tx]);self.assertEqual(len(first['events']),1)
        self.service=NativeIncomingCandidates(self.money)
        second=self.review()
        self.assertEqual(first['events'][0]['observation_id'],second['events'][0]['observation_id'])
        self.assertTrue(second['events'][0]['seen_before']);self.assertEqual(self.count(),1)

    def test_conflicting_message_tx_or_facts_roll_back_whole_snapshot(self):
        different=copy.deepcopy(self.tx);different['hash']='d'*64
        with self.assertRaises(RewardCandidateError): self.review([self.tx,different])
        self.assertEqual(self.count(),0)
        self.review();self.tx['in_msg']['value']='200000000'
        with self.assertRaises(RewardCandidateError): self.review()
        self.tx['in_msg']['value']='100000000';self.tx['in_msg']['hash']='e'*64
        with self.assertRaises(RewardCandidateError): self.review()
        self.assertEqual(self.count(),1)

    def test_own_declared_sender_and_journal_transit_are_excluded(self):
        self.assertEqual(self.review(own_addresses=[SOURCE])['events'][0]['classification'],'own_sender_excluded')
        self.journal(evidence={'message_hash':'b'*64,'status':'recipient_pending'})
        self.assertEqual(self.review()['events'][0]['classification'],'own_transit_excluded')
        # Exclusions are recomputed even when the observation already exists.
        self.assertFalse(self.review()['events'][0]['candidate'])

    def test_networks_isolated_and_no_cached_reward_flag(self):
        self.journal()
        self.assertFalse(self.review()['events'][0]['candidate'])
        self.assertTrue(self.review(network='mainnet')['events'][0]['candidate'])

    def test_reservation_comment_never_proves_reward_or_collection(self):
        self.tx['in_msg']['message_content']['decoded']['comment']='accmanager:'+'d'*32
        self.assertEqual(self.review()['events'][0]['classification'],'reserved_comment_needs_review')
        self.journal()
        self.assertEqual(self.review()['events'][0]['classification'],'own_sender_excluded')

    def test_pending_emulated_failed_bounce_forwarded_and_jetton_not_candidate(self):
        original=copy.deepcopy(self.tx)
        changes=[lambda t:t.update(finality=1),lambda t:t.update(emulated=True),
            lambda t:t['description'].update(aborted=True),lambda t:t['description'].update(destroyed=True),
            lambda t:t['in_msg'].update(bounced=True),lambda t:t.update(out_msgs=[{}]),
            lambda t:t['in_msg'].update(opcode=0x7362d09c),
            lambda t:t['in_msg'].update(value_extra_currencies={'1':'2'})]
        for change in changes:
            self.tx=copy.deepcopy(original);change(self.tx)
            with self.subTest(change=change): self.assertFalse(self.review()['events'][0]['candidate'])

    def test_duplicate_same_identity_different_finality_is_not_silently_merged(self):
        pending=copy.deepcopy(self.tx);pending['finality']=1
        with self.assertRaises(RewardCandidateError): self.review([self.tx,pending])
        self.assertEqual(self.count(),0)

    def test_unverified_pending_can_be_reviewed_after_finality_without_new_event(self):
        self.tx['finality']=1
        self.assertFalse(self.review()['events'][0]['candidate'])
        self.tx['finality']=2
        self.assertTrue(self.review()['events'][0]['candidate']);self.assertEqual(self.count(),1)

    def test_scope_confirmation_invalid_numeric_missing_hash_and_future_fail_closed(self):
        for changes in ({'confirmed':False},{'scope_ids':set()},{'ui_context':'wrong'}):
            with self.subTest(changes=changes),self.assertRaises(RewardCandidateError): self.review(**changes)
        for value in (None,True,'1e8',-1):
            self.tx['in_msg']['value']=value
            with self.subTest(value=value),self.assertRaises(RewardCandidateError): self.review()
        self.tx['in_msg']['value']='100000000';self.tx['in_msg'].pop('hash')
        with self.assertRaises(RewardCandidateError): self.review()
        self.tx['in_msg']['hash']='b'*64;self.tx['now']=1016
        with self.assertRaises(RewardCandidateError): self.review()
        self.assertEqual(self.count(),0)

    def test_private_text_not_stored_and_external_control_not_money(self):
        private='UNTRUSTED_SECRET_TEXT_NOT_A_PRIZE'
        self.tx['in_msg']['message_content']['decoded']['comment']=private
        self.review()
        self.assertNotIn(private.encode(),self.money.path.read_bytes())
        self.tx['in_msg']['hash']='e'*64;self.tx['hash']='f'*64
        self.tx['lt']='11'
        self.tx['in_msg'].update(source=None,value=None)
        self.assertEqual(self.review()['events'][0]['classification'],'external_control')

    def test_corrupt_journal_oversize_v2_projection_and_page_limit(self):
        self.journal(evidence={'message_hash':'invalid'})
        with self.assertRaises(RewardCandidateError): self.review()
        with self.money.db() as db: db.execute('DELETE FROM reconciliation');db.execute('DELETE FROM intents')
        for raw in (b' '* (1024*1024+1),b'{"transactions":[],"transactions":[]}',b'{"ok":true,"result":[]}'):
            with self.assertRaises(RewardCandidateError):
                self.service.review(raw,account_id='one',address=DEST,network='testnet',scope_ids={'one'},ui_context=UI,confirmed=True)
        self.assertTrue(self.review([self.tx]*100)['page_at_limit'])
        with self.assertRaises(RewardCandidateError): self.review([self.tx]*101)

    def test_receiving_lt_reuse_is_rejected_atomically(self):
        other=copy.deepcopy(self.tx);other['hash']='d'*64;other['in_msg']['hash']='e'*64
        with self.assertRaises(RewardCandidateError): self.review([self.tx,other])
        self.assertEqual(self.count(),0)

    def test_terminal_wallet_from_cancelled_carousel_remains_own_declaration(self):
        from modules.money_carousel import MoneyCarousel
        nodes=[{'account_id':'one','address':DEST},{'account_id':'two','address':SOURCE}]
        original=self.money.accounts.get_account
        other=AccountRecord('two','Synthetic','pyrogram','session_file','never-open2.session',user_id=43)
        self.money.accounts.get_account=lambda ident:other if ident=='two' else original(ident)
        carousel=MoneyCarousel(self.money)
        plan=carousel.create(nodes,network='testnet',scope_ids={'one','two'},ui_context=UI,confirmed=True)
        carousel.control(plan['id'],plan['revision'],'cancel',scope_ids={'one','two'},ui_context=UI,confirmation='CANCEL '+plan['id'])
        self.assertEqual(self.review()['events'][0]['classification'],'own_sender_excluded')
