"""Local one-use funding admission for native transit-only execution.

References the existing funding observation and payment journal. No balances,
keys, network, payment states or automatic recovery are maintained here.
"""
import json
import re

from modules.money_policy import digest, nano


def require(ok):
    if not ok: raise ValueError('Funding transit changed/consumed; review required')


def setup(db):
    db.execute('CREATE TABLE IF NOT EXISTS native_transit_claims(message_hash TEXT PRIMARY KEY,intent TEXT UNIQUE NOT NULL,reference_hash TEXT NOT NULL)')


def funding_snapshot(money,db,reference,policy,target,scope_ids,ui_context,*,intent=None):
    require(isinstance(reference,dict) and set(reference)=={'id','revision'}
        and all(isinstance(v,str) and re.fullmatch('[a-f0-9]{32}',v) for v in reference.values()))
    require(policy.scope=='transit_only' and policy.network=='mainnet' and ui_context==policy.ui_context)
    row=db.execute('SELECT * FROM money_funding_drafts WHERE id=?',(reference['id'],)).fetchone()
    require(row and row['revision']==reference['revision'] and row['ui_context']==ui_context
        and row['state']=='funding_observed' and row['error'] is None and row['lease'] is None
        and row['address']==policy.source)
    nodes=json.loads(row['nodes']);require(isinstance(nodes,list) and len(nodes)>=2)
    require(nodes[0]['id']==policy.account and nodes[1]['id']==target)
    for node in nodes:
        context,ui=money.scope(node['id'],scope_ids,ui_context)
        account=money.accounts.get_account(node['id'])
        require(context==node['context'] and ui==ui_context and account.user_id==node['user_id']
            and account.username==node['username'])
    event=json.loads(row['event']);require(isinstance(event,dict)
        and event.get('verification')=='provider_reported_not_chain_proof'
        and event.get('address')==policy.source)
    require(all(isinstance(event.get(k),str) and re.fullmatch('[a-f0-9]{64}',event[k])
        for k in ('tx_hash','message_hash')))
    require(isinstance(event.get('lt'),str) and re.fullmatch('0|[1-9][0-9]{0,19}',event['lt'])
        and int(event['lt'])<2**64)
    net=nano(event.get('net_credit_nano'),positive=True);old=nano(event.get('previous_balance_nano'))
    ref=db.execute('SELECT * FROM money_funding_refs WHERE draft=?',(reference['id'],)).fetchone()
    require(ref and all(ref[k]==event[k] for k in ('tx_hash','message_hash','address','lt')))
    claim=db.execute('SELECT * FROM native_transit_claims WHERE message_hash=?',(event['message_hash'],)).fetchone()
    snapshot={'reference':dict(reference),'event':event,'context':policy.account_context,
        'ui_context':ui_context,'source':policy.source,'target':target,
        'net_nano':str(net),'old_nano':str(old),'balance_nano':str(old+net)}
    snapshot['hash']=digest(snapshot)
    require(claim is None if intent is None else claim is not None and claim['intent']==intent
        and claim['reference_hash']==snapshot['hash'])
    return snapshot


def claim_funding(db,snapshot,intent):
    db.execute('INSERT INTO native_transit_claims VALUES(?,?,?)',
        (snapshot['event']['message_hash'],intent,snapshot['hash']))


def check_observation(snapshot,observed):
    require(observed.get('address')==snapshot['source'] and observed.get('network')=='mainnet'
        and observed.get('balance_nano')==snapshot['balance_nano']
        and observed.get('last_transaction')=={'hash':snapshot['event']['tx_hash'],'lt':snapshot['event']['lt']})
    return snapshot['net_nano']
