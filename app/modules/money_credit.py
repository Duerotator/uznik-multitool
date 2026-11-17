"""Strict non-bounceable credit to a code-less account, not wallet activation.

Shared by unsigned simulation and finalized provider reconciliation. Caller
still verifies peers/message identity/finality; this only validates the receiver.
"""
from modules.money_policy import nano


def uninitialized_credit(tx, message):
    def require(condition):
        if not condition: raise ValueError('Unsupported uninitialized credit; review required')
    require(isinstance(tx,dict) and isinstance(message,dict))
    require(tx.get('orig_status') in {'nonexist','uninit'} and tx.get('end_status')=='uninit')
    desc=tx.get('description')
    require(isinstance(desc,dict) and desc.get('type')=='ord' and desc.get('aborted') is True
        and desc.get('destroyed') is False and desc.get('action') is None and desc.get('bounce') is None)
    compute=desc.get('compute_ph')
    require(isinstance(compute,dict) and compute.get('skipped') is True
        and compute.get('reason') in {'cskip_no_state','no_state'} and compute.get('success') is None
        and compute.get('exit_code') is None)
    require(tx.get('out_msgs')==[] and message.get('bounce') is False
        and message.get('bounced') is False and message.get('init_state') is None
        and message.get('value_extra_currencies') in (None,{})
        and message.get('extra_currencies') in (None,{}))
    balances=[]
    for state,status in ((tx.get('account_state_before'),tx['orig_status']),
                         (tx.get('account_state_after'),'uninit')):
        require(isinstance(state,dict) and (state.get('account_status')==status or (status=='nonexist' and state.get('account_status') is None))
            and state.get('extra_currencies') in (None,{})
            and all(state.get(k) is None for k in ('code_hash','data_hash','frozen_hash','code_boc','data_boc')))
        bal=state.get('balance')
        balances.append(0 if status=='nonexist' and bal is None else nano(bal))
    before,after=balances
    require(tx['orig_status']!='nonexist' or before==0)
    value=nano(message.get('value'),positive=True);fees=nano(tx.get('total_fees'))
    credit=desc.get('credit_ph')
    require(isinstance(credit,dict) and credit.get('credit_extra_currencies') in (None,{})
        and credit.get('due_fees_collected') in (None,'0'))
    require(tx.get('total_fees_extra_currencies') in (None,{})
        and 0<after-before<=nano(credit.get('credit'))<=value and value-(after-before)==fees)
    return {'recipient_state':'uninitialized_credited','recipient_before_nano':str(before),
        'recipient_after_nano':str(after),'recipient_net_nano':str(after-before),
        'recipient_fee_nano':str(fees),'money_activated':'unknown','signer_ready':False}


def active_credit(tx, message):
    """Exact provider-reported native delta for one ordinary active receiver.

    A delivered message alone does not establish the amount available for a
    subsequent hop. This is an observation, never a spend authorization.
    """
    def require(condition):
        if not condition: raise ValueError('Active recipient net credit requires review')
    require(isinstance(tx,dict) and isinstance(message,dict)
        and tx.get('orig_status')=='active' and tx.get('end_status')=='active')
    desc=tx.get('description')
    require(isinstance(desc,dict) and desc.get('type')=='ord' and desc.get('aborted') is False
        and desc.get('destroyed') is False and desc.get('bounce') is None)
    compute=desc.get('compute_ph')
    require(isinstance(compute,dict) and compute.get('skipped') is False
        and compute.get('success') is True and type(compute.get('exit_code')) is int
        and compute['exit_code'] in (0,1))
    action=desc.get('action')
    require(action is None or isinstance(action,dict) and action.get('success') is True
        and action.get('valid') is True and action.get('no_funds') is False
        and type(action.get('result_code')) is int and action['result_code']==0)
    require(tx.get('out_msgs')==[] and message.get('bounce') is False
        and message.get('bounced') is False and message.get('value_extra_currencies') in (None,{})
        and tx.get('total_fees_extra_currencies') in (None,{}))
    before,after=tx.get('account_state_before'),tx.get('account_state_after')
    require(all(isinstance(state,dict) and state.get('account_status')=='active'
        and state.get('extra_currencies') in (None,{}) for state in (before,after)))
    previous,updated=nano(before.get('balance')),nano(after.get('balance'))
    value,fees=nano(message.get('value'),positive=True),nano(tx.get('total_fees'))
    net=updated-previous
    require(0<net<=value and value-net==fees)
    return {'recipient_state':'active','recipient_before_nano':str(previous),
        'recipient_after_nano':str(updated),'recipient_net_nano':str(net),
        'recipient_fee_nano':str(fees),'incoming_value_nano':str(value),
        'money_activated':'unknown','signer_ready':False}
