"""Finite, local-only native spend policy and conservative unsigned quotes.

No arming, keys, RPC, signing or journal replacement. Fee cap is an operator
budget, not an estimate. Unknown fees require separate bounded emulation.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import uuid

from core.ton_address import normalize_address
from modules.wallet_tg_codec import CODE_HASH,REV00_HASH,SUBWALLET


class MoneyPolicyError(ValueError):
    pass


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def nano(value, *, positive=False):
    if not isinstance(value,str) or not re.fullmatch('0|[1-9][0-9]{0,35}',value):
        raise MoneyPolicyError('Exact nano integer string required; no float/bool/exponent')
    number=int(value)
    if number>=2**120 or positive and number<=0: raise MoneyPolicyError('Native amount out of range')
    return number


@dataclass(frozen=True)
class NativeSpendPolicy:
    id: str
    revision: str
    account: str
    account_context: str
    ui_context: str
    network: str
    source: str
    targets: tuple
    scope: str
    reserve_nano: str
    fee_cap_nano: str
    transfer_debit_cap_nano: str
    daily_reservation_cap_nano: str


def make_policy(*, account,account_context,ui_context,network,source,targets,scope,
                reserve_nano,fee_cap_nano,transfer_debit_cap_nano,daily_reservation_cap_nano,
                confirmed=False,all_native_confirmed=False,allow_high_caps=False):
    if confirmed is not True: raise MoneyPolicyError('Explicit local policy confirmation required')
    if (not isinstance(account,str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,160}',account)
            or not isinstance(account_context,str) or not re.fullmatch('[a-f0-9]{64}',account_context)
            or not isinstance(ui_context,str) or not re.fullmatch('[a-f0-9]{64}',ui_context)):
        raise MoneyPolicyError('Exact account/UI contexts required')
    source=normalize_address(source,network).raw
    if scope not in {'transit_only','all_native_authorized'} or scope=='all_native_authorized' and all_native_confirmed is not True:
        raise MoneyPolicyError('Spending pre-existing native funds needs separate explicit authorization')
    if not isinstance(targets,(list,tuple)) or not 1<=len(targets)<=50:
        raise MoneyPolicyError('Provide a finite exact recipient allowlist')
    recipients=tuple(normalize_address(value,network).raw for value in targets)
    if len(set(recipients))!=len(recipients) or source in recipients:
        raise MoneyPolicyError('No duplicate/self recipients')
    reserve,fee,cap,daily=(nano(reserve_nano),nano(fee_cap_nano,positive=True),
                         nano(transfer_debit_cap_nano,positive=True),nano(daily_reservation_cap_nano,positive=True))
    max_cap=100*10**9 if allow_high_caps else 10**9
    max_daily=500*10**9 if allow_high_caps else 5*10**9
    if not fee<cap<=max_cap or not cap<=daily<=max_daily or reserve>max_cap:
        cap_label='100' if allow_high_caps else '1'
        daily_label='500' if allow_high_caps else '5'
        raise MoneyPolicyError(f'Initial module limits: debit <={cap_label} native/request, <={daily_label}/day; fee cap below debit cap')
    return NativeSpendPolicy(uuid.uuid4().hex,uuid.uuid4().hex,account,account_context,ui_context,network,
        source,recipients,scope,reserve_nano,fee_cap_nano,transfer_debit_cap_nano,daily_reservation_cap_nano)


def quote(policy,observation,*,target,transit_credit_nano,reserved_nano,min_transfer_nano,
          current_account_context,current_ui_context,now,valid_until,intent_id,send_all=False,deploy=False,allow_deploy_drain=False):
    if not isinstance(policy,NativeSpendPolicy) or type(send_all) is not bool or type(deploy) is not bool or (deploy and send_all and not allow_deploy_drain):
        raise MoneyPolicyError('Explicit native policy and send-all choice required')
    if policy.account_context!=current_account_context or policy.ui_context!=current_ui_context:
        raise MoneyPolicyError('Policy account/group/filter context changed')
    target=normalize_address(target,policy.network).raw
    if target not in policy.targets: raise MoneyPolicyError('Recipient not allowlisted')
    if (not isinstance(observation,dict) or observation.get('network')!=policy.network or observation.get('address')!=policy.source
            or observation.get('implementation_hash')!=REV00_HASH or observation.get('subwallet_id')!=SUBWALLET[policy.network]
            or observation.get('provider')!='toncenter' or observation.get('verification')!='provider_reported_not_chain_proof'
            or observation.get('coherence')!='queries_pinned_by_mc_seqno'):
        raise MoneyPolicyError('Missing/mismatching provider WalletTg observation')
    if deploy:
        if (observation.get('active') is not False or observation.get('chain_state')!='uninit'
                or observation.get('code_hash') is not None or observation.get('data_hash') is not None
                or observation.get('seqno')!=0 or type(observation.get('seqno')) is not int
                or observation.get('initial_code_hash')!=CODE_HASH
                or observation.get('public_key_origin')!='initial_anchor_not_chain_getter'
                or any(not isinstance(observation.get(k),str) or not re.fullmatch('[a-f0-9]{64}',observation[k])
                       for k in ('initial_data_hash','initial_next_data_hash'))):
            raise MoneyPolicyError('Explicit initial WalletTg deployment observation required')
    elif observation.get('active') is not True or observation.get('code_hash')!=CODE_HASH:
        raise MoneyPolicyError('Source is not active; explicit reviewed deployment required')
    if type(observation.get('checked_at')) not in (int,float) or not 0<=now-observation['checked_at']<=30:
        raise MoneyPolicyError('Wallet observation stale/future')
    for key in ('seqno','mc_block_seqno'):
        if type(observation.get(key)) is not int or not 0<=observation[key]<2**32:
            raise MoneyPolicyError('Invalid WalletTg block/header')
    if not isinstance(observation.get('public_key'),str) or not re.fullmatch('[a-f0-9]{64}',observation['public_key']):
        raise MoneyPolicyError('Missing current public key')
    for key in (('block_id',) if deploy else ('block_id','data_hash')):
        if not isinstance(observation.get(key),str) or not re.fullmatch('[a-f0-9]{64}',observation[key]):
            raise MoneyPolicyError('Missing block/storage hash')
    if (type(valid_until) is not int or not 30<=valid_until-now<=300 or not 0<valid_until<2**32
            or not isinstance(intent_id,str) or not re.fullmatch('[a-f0-9]{32}',intent_id)):
        raise MoneyPolicyError('Explicit intent ID and fresh bounded expiry required')
    balance=nano(observation.get('balance_nano'));transit=nano(transit_credit_nano);reserved=nano(reserved_nano)
    minimum=nano(min_transfer_nano,positive=True)
    if transit>balance: raise MoneyPolicyError('Transit evidence exceeds currently observed spendable balance')
    old=balance-transit
    protected=max(nano(policy.reserve_nano),old if policy.scope=='transit_only' else 0)
    available=max(0,balance-protected)
    budget=min(available,nano(policy.transfer_debit_cap_nano),max(0,nano(policy.daily_reservation_cap_nano)-reserved))
    fee=nano(policy.fee_cap_nano)
    if send_all:
        if protected or budget<balance:
            raise MoneyPolicyError('Drain would consume protected old funds/reserve or exceed the debit/daily cap')
        nominal='0';mode=130;debit=balance
        if balance-fee<minimum: raise MoneyPolicyError('Insufficient worst-case net delivery for the current minimum')
    else:
        amount=budget-fee
        if amount<minimum: raise MoneyPolicyError('Insufficient native balance after protected funds and fee ceiling')
        nominal=str(amount);mode=3;debit=budget
    result={'intent_id':intent_id,'policy_id':policy.id,'policy_revision':policy.revision,'account':policy.account,
            'account_context':policy.account_context,'ui_context':policy.ui_context,'network':policy.network,
            'source':policy.source,'target':target,'scope':policy.scope,'transit_credit_nano':transit_credit_nano,
            'protected_nano':str(protected),'balance_nano':str(balance),'reserved_nano':reserved_nano,
            'fee_cap_nano':policy.fee_cap_nano,'max_debit_nano':str(debit),'min_transfer_nano':min_transfer_nano,
            'amount_nano':nominal,'send_mode':mode,'deploy':deploy,'valid_until':valid_until,'seqno':observation['seqno'],
            'public_key':observation['public_key'],'mc_block_seqno':observation['mc_block_seqno'],
            'block_id':observation['block_id'],'observation_hash':digest(observation),
            'estimated_fee_nano':None,'emulation_required':True,'production_signing':False,'broadcast_supported':False}
    return {**result,'quote_hash':digest(result)}
