"""Unsigned pre-sign TonCenter simulation adapter; disconnected from payments.

Fixed request schema, invalid zero signature, no broadcast/signing route. A
provider projection is NOT a receipt, chain proof or permission to use a key.
Supports exact active receivers or strict code-less first credits, never
equating an incoming credit with Telegram readiness or a deployed signer.
"""
import asyncio
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.ton_address import normalize_address
from modules.money_chain_read import MoneyReadError, bounded_json
from modules.money_policy import digest, nano, quote
from modules.money_credit import uninitialized_credit
from modules.money_reconciliation import _hash
from modules.wallet_tg_codec import CODE_HASH, WalletTgCodec


class MoneyEmulationError(ValueError):
    pass


def require(condition):
    if not condition: raise MoneyEmulationError('Incomplete/unsupported simulation; manual review required')


def account(value,network):
    return normalize_address(value,network).raw


def successful(tx, *, source):
    require(isinstance(tx,dict) and tx.get('emulated') is True)
    desc=tx.get('description')
    require(isinstance(desc,dict) and desc.get('type')=='ord' and desc.get('aborted') is False)
    compute=desc.get('compute_ph')
    require(isinstance(compute,dict) and compute.get('skipped') is False
            and compute.get('success') is True and type(compute.get('exit_code')) is int and compute['exit_code'] in (0,1))
    action=desc.get('action')
    if source or action is not None:
        require(isinstance(action,dict) and action.get('success') is True and action.get('valid') is True
                and action.get('no_funds') is False and type(action.get('result_code')) is int and action['result_code']==0)


def balance(state):
    require(isinstance(state,dict) and state.get('account_status')=='active'
            and state.get('extra_currencies') in (None,{}))
    return nano(state.get('balance'))


def parse_projection(response,plan,observation,unsigned, *, checked_at):
    """Strict finite subset of ton-indexer EmulateTraceResponse, not a receipt."""
    require(isinstance(response,dict) and type(response.get('mc_block_seqno')) is int
            and response['mc_block_seqno']==plan['mc_block_seqno'] and response.get('is_incomplete') is False)
    trace=response.get('trace');transactions=response.get('transactions')
    require(isinstance(trace,dict) and isinstance(transactions,dict) and len(transactions)==2)
    children=trace.get('children')
    require(isinstance(children,list) and len(children)==1 and isinstance(children[0],dict)
            and children[0].get('children',[])==[])
    rows={}
    for key,tx in transactions.items():
        normalized=_hash(key)
        require(normalized not in rows and isinstance(tx,dict) and _hash(tx.get('hash'))==normalized)
        rows[normalized]=tx
    source_hash=_hash(trace.get('tx_hash'));dest_hash=_hash(children[0].get('tx_hash'))
    require(source_hash!=dest_hash and source_hash in rows and dest_hash in rows)
    source,dest=rows[source_hash],rows[dest_hash];network=plan['network']
    require(account(source.get('account'),network)==plan['source'] and account(dest.get('account'),network)==plan['target'])
    successful(source,source=True)
    incoming=source.get('in_msg');out=source.get('out_msgs');received=dest.get('in_msg')
    require(isinstance(incoming,dict) and incoming.get('source') is None
            and account(incoming.get('destination'),network)==plan['source'])
    content=incoming.get('message_content')
    require(isinstance(content,dict) and _hash(content.get('hash'))==unsigned['unsigned_body_hash'])
    require(_hash(trace.get('in_msg_hash'))==_hash(incoming.get('hash')))
    require(isinstance(out,list) and len(out)==1 and isinstance(out[0],dict) and isinstance(received,dict)
            and dest.get('out_msgs')==[])
    message=out[0];message_hash=_hash(message.get('hash'))
    require(message_hash==_hash(received.get('hash'))==_hash(children[0].get('in_msg_hash')))
    for item in (message,received):
        opcode=item.get('opcode')
        valid_opcode=(type(opcode) is int and opcode==0) or opcode=='0x00000000'
        require(account(item.get('source'),network)==plan['source'] and account(item.get('destination'),network)==plan['target']
                and item.get('bounced') is False and item.get('bounce') is False
                and valid_opcode
                and item.get('value_extra_currencies') in (None,{}) and (item.get('extra_currencies') or {})=={})
        body=item.get('message_content');require(isinstance(body,dict))
        decoded=body.get('decoded')
        valid_decoded=isinstance(decoded,dict) and (
            (decoded.get('type')=='ton_transfer' and decoded.get('encrypted') is False)
            or (decoded.get('@type')=='text_comment' and decoded.get('encrypted') in (False,None))
        )
        require(valid_decoded and decoded.get('comment')=='accmanager:'+plan['intent_id'])
    value=nano(message.get('value'),positive=True)
    require(nano(received.get('value'),positive=True)==value)
    before=source.get('account_state_before');after=source.get('account_state_after')
    source_after=balance(after)
    require(_hash(after.get('code_hash'))==CODE_HASH)
    if plan['deploy']:
        live_indexer=message.get('opcode')=='0x00000000'
        require(isinstance(before,dict) and before.get('account_status')=='uninit'
            and before.get('extra_currencies') in (None,{})
            and all(before.get(k) is None for k in ('code_hash','data_hash','frozen_hash'))
            and source.get('orig_status')=='uninit' and source.get('end_status')=='active'
            and (live_indexer or (source['description']['compute_ph'].get('account_activated') is True
                and source['description']['compute_ph'].get('msg_state_used') is True))
            and source['description'].get('destroyed') is False
            and unsigned.get('initial_data_hash')==observation['initial_data_hash']
            and unsigned.get('initial_next_data_hash')==observation['initial_next_data_hash']
            and _hash(after.get('data_hash'))==observation['initial_next_data_hash'])
        init=incoming.get('init_state')
        require(isinstance(init,dict) and _hash(init.get('hash'))==plan['source'].split(':')[1])
        source_before=nano(before.get('balance'))
    else:
        source_before=balance(before)
        require(_hash(before.get('code_hash'))==CODE_HASH and _hash(before.get('data_hash'))==observation['data_hash'])
    receiver=dest.get('account_state_before')
    if isinstance(receiver,dict) and receiver.get('account_status') in {'nonexist','uninit'}:
        try: credit=uninitialized_credit(dest,received)
        except ValueError: raise MoneyEmulationError('Unsupported first-credit trace; no signing authorized') from None
        recipient_before=nano(credit['recipient_before_nano']);recipient_after=nano(credit['recipient_after_nano'])
    else:
        successful(dest,source=False)
        recipient_before,recipient_after=balance(receiver),balance(dest.get('account_state_after'))
        credit={'recipient_state':'active','money_activated':'unknown','signer_ready':False}
    debit=source_before-source_after;net=recipient_after-recipient_before
    require(source_before==nano(plan['balance_nano']) and debit>0 and net>0
            and debit>=value>=net and debit<=nano(plan['max_debit_nano'])
            and source_after>=nano(plan['protected_nano']) and net>=nano(plan['min_transfer_nano']))
    if plan['send_mode']==3: require(value==nano(plan['amount_nano']))
    else: require(plan['send_mode']==130 and source_after==0 and source['description'].get('destroyed') is False)
    total_fee=debit-net
    require(total_fee<=nano(plan['fee_cap_nano']))
    return {**credit,'deployment_projected':plan['deploy'],'quote_hash':plan['quote_hash'],'observation_hash':plan['observation_hash'],
            'mc_block_seqno':plan['mc_block_seqno'],'unsigned_body_hash':unsigned['unsigned_body_hash'],
            'estimated_fee_nano':str(total_fee),'source_debit_nano':str(debit),'recipient_net_nano':str(net),
            'outgoing_value_nano':str(value),'source_after_nano':str(source_after),'checked_at':checked_at,
            'verification':'provider_reported_simulation_not_receipt','production_signing':False,
            'broadcast_supported':False,'automatic_retry':False}


class TonCenterEmulator:
    def __init__(self, *, client=None,codec=None,clock=time.time):
        self.client,self.clock=client,clock
        self.codec=codec or WalletTgCodec(clock=clock)

    async def project(self,policy,observation,plan, *, anchor_public_key,
                      current_account_context,current_ui_context,confirmed=False,allow_deploy_drain=False):
        if confirmed is not True: raise MoneyEmulationError('Explicit unsigned simulation confirmation required')
        try:
            # Recompute every guard/field; mutable quote hashes alone are not authorization.
            expected=quote(policy,observation,target=plan['target'],transit_credit_nano=plan['transit_credit_nano'],
                reserved_nano=plan['reserved_nano'],min_transfer_nano=plan['min_transfer_nano'],
                current_account_context=current_account_context,current_ui_context=current_ui_context,
                now=self.clock(),valid_until=plan['valid_until'],intent_id=plan['intent_id'],
                send_all=plan['send_mode']==130,deploy=plan['deploy'],allow_deploy_drain=allow_deploy_drain)
            require(expected==plan)
            observation,plan=dict(observation),dict(plan)
            unsigned_kwargs={'allow_high_caps': True} if nano(policy.transfer_debit_cap_nano, positive=True) > 10**9 else {}
            unsigned=self.codec.prepare_emulation(network=plan['network'],anchor_public_key=anchor_public_key,
                seqno=plan['seqno'],valid_until=plan['valid_until'],destination=plan['target'],
                amount_nano=plan['amount_nano'],intent_id=plan['intent_id'],
                send_all=plan['send_mode']==130,deploy=plan['deploy'],allow_deploy_drain=allow_deploy_drain,**unsigned_kwargs)
            require(unsigned['source']==plan['source'])
            # Independently establish that the placeholder is NOT an authorization.
            try:
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(plan['public_key'])).verify(
                    bytes(64),bytes.fromhex(unsigned['payload_hash']))
            except InvalidSignature: pass
            else: raise MoneyEmulationError('Invalid-signature placeholder unexpectedly verified')
            host='toncenter.com' if plan['network']=='mainnet' else 'testnet.toncenter.com'
            body={'boc':unsigned['unsigned_external_boc'],'ignore_chksig':True,'include_code_data':True,
                  'include_address_book':False,'include_metadata':False,'with_actions':False,'mc_block_seqno':plan['mc_block_seqno']}
            if self.client is not None:
                response=await bounded_json(self.client,'POST',f'https://{host}/api/emulate/v1/emulateTrace',body=body)
                require(0<=self.clock()-observation['checked_at']<=30 and plan['valid_until']-self.clock()>=30)
                return {**parse_projection(response,plan,observation,unsigned,checked_at=self.clock()),'request_hash':digest(body)}
            headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36','Accept':'application/json, text/plain, */*'}
            async with httpx.AsyncClient(headers=headers,timeout=httpx.Timeout(20,connect=5),trust_env=False,follow_redirects=False) as http:
                response=None
                last_err=None
                for attempt in range(4):
                    try:
                        response=await bounded_json(http,'POST',f'https://{host}/api/emulate/v1/emulateTrace',body=body)
                        break
                    except (httpx.HTTPError,TimeoutError,MoneyReadError) as err:
                        if isinstance(err,MoneyReadError) and not err.retryable:
                            raise
                        last_err=err
                        if attempt<3:
                            await asyncio.sleep(2.0*(attempt+1))
                if response is None and last_err is not None:
                    raise last_err
                require(0<=self.clock()-observation['checked_at']<=30 and plan['valid_until']-self.clock()>=30)
                return {**parse_projection(response,plan,observation,unsigned,checked_at=self.clock()),'request_hash':digest(body)}
        except MoneyEmulationError: raise
        except (ValueError,TypeError,KeyError,RecursionError,TimeoutError,httpx.HTTPError,MoneyReadError):
            raise MoneyEmulationError('Unsigned simulation failed/unsupported; no signing authorized') from None
