"""Bounded, read-only TON Center v3 evidence. Not a lite-client/ownership proof."""
import asyncio
import base64
import json
import re

import httpx

from core.ton_address import normalize_address
from modules.ton_readonly import ReadError, _unique, _unsigned, toncenter_headers
from modules.money_credit import active_credit, uninitialized_credit


class ReconciliationError(ValueError):
    def __init__(self, message='Reconciliation failed', *, status_code=None, retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _hash(value):
    if not isinstance(value, str):
        raise ValueError('Missing hash')
    if re.fullmatch('[a-fA-F0-9]{64}', value):
        return value.lower()
    if len(value) != 44:
        raise ValueError('Invalid hash')
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != 32:
        raise ValueError('Invalid hash')
    return decoded.hex()


def _page(raw, limit):
    if len(raw) > 1024 * 1024:
        raise ValueError('Response too large')
    data = json.loads(raw, object_pairs_hook=_unique)
    rows = data.get('transactions') if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > limit or any(not isinstance(r, dict) for r in rows):
        raise ValueError('Unsupported transaction page')
    return rows


def _address(value, network):
    return normalize_address(value, network).raw


def _message_matches(msg, intent):
    if not isinstance(msg, dict):
        return False
    content = msg.get('message_content')
    decoded = content.get('decoded') if isinstance(content, dict) else None
    opcode = msg.get('opcode')
    valid_opcode = (type(opcode) is int and opcode == 0) or opcode == '0x00000000'
    valid_decoded = isinstance(decoded, dict) and (
        (decoded.get('type') == 'ton_transfer' and decoded.get('encrypted') is False)
        or (decoded.get('@type') == 'text_comment' and decoded.get('encrypted') in (False, None))
    )
    return (valid_decoded and decoded.get('comment') == 'accmanager:' + intent['id']
            and valid_opcode
            and _address(msg.get('source'), intent['network']) == intent['source']
            and _address(msg.get('destination'), intent['network']) == intent['destination']
            and _unsigned(msg.get('value')) == intent['amount']
            and msg.get('bounced') is False and msg.get('value_extra_currencies') in (None, {}))


def _final(tx):
    fin = tx.get('finality')
    return tx.get('emulated') is False and ((type(fin) is int and fin == 2) or fin == 'finalized')


def _executed(tx):
    """Conservative subset: successful ordinary VM execution; skipped compute needs review."""
    desc = tx.get('description')
    if not isinstance(desc, dict) or desc.get('type') != 'ord' or desc.get('aborted') is not False:
        return False
    compute = desc.get('compute_ph')
    if not isinstance(compute, dict) or compute.get('skipped') is not False or compute.get('success') is not True:
        return False
    if type(compute.get('exit_code')) is not int or compute['exit_code'] not in (0, 1):
        return False
    action = desc.get('action')
    return action is None or (isinstance(action, dict) and action.get('success') is True
        and action.get('valid') is True and action.get('no_funds') is False
        and type(action.get('result_code')) is int and action['result_code'] == 0)


async def reconcile(intent, *, client=None, paginate=None):
    """Bounded source scan and recipient confirmation. Absence never means 'not sent'."""
    network = intent['network']
    if network not in {'mainnet', 'testnet'} or not re.fullmatch('[a-f0-9]{32}', intent['id']):
        raise ReconciliationError('Unsupported stored intent')
    host = 'toncenter.com' if network == 'mainnet' else 'testnet.toncenter.com'
    start, end = max(0, int(intent['prompted']) - 60), int(intent['expires']) + 60
    should_paginate = (client is None) if paginate is None else bool(paginate)

    async def get(http, path, params, limit):
        headers = toncenter_headers({
            'Accept-Encoding': 'identity',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
        })
        async with http.stream('GET', f'https://{host}/api/v3/{path}', params=params,
                headers=headers, follow_redirects=False) as response:
            if response.status_code != 200 or response.headers.get('content-encoding', 'identity').lower() != 'identity':
                retryable = response.status_code in {429, 500, 502, 503, 504}
                raise ReconciliationError(
                    f'Reconciliation provider HTTP {response.status_code}; retry only explicitly',
                    status_code=response.status_code,
                    retryable=retryable,
                )
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 1024 * 1024:
                    raise ReconciliationError('Reconciliation response exceeds size limit')
                chunks.append(chunk)
            return _page(b''.join(chunks), limit)

    async def check(http):
        rows = []
        seen_keys = set()
        truncated = False
        max_pages = 5 if should_paginate else 1
        for page_idx in range(max_pages):
            params = {'account': intent['source'], 'start_utime': start, 'end_utime': end, 'limit': 100, 'sort': 'asc'}
            if page_idx > 0:
                params['offset'] = page_idx * 100
            page = await get(http, 'transactions', params, 100)
            page_keys = {(tx.get('hash'), tx.get('lt')) for tx in page if isinstance(tx, dict)}
            if page_idx > 0 and page_keys and page_keys.issubset(seen_keys):
                truncated = True
                break
            seen_keys.update(page_keys)
            rows.extend(page)
            if len(page) < 100:
                break
            if page_idx == max_pages - 1:
                truncated = True
            elif client is None:
                await asyncio.sleep(1.1)
        evidence = {'status': 'not_found', 'verification': 'provider_reported_not_chain_proof',
                    'provider': 'toncenter_v3', 'automatic_retry': False, 'search_complete': False}
        if truncated:
            return {**evidence, 'status': 'search_truncated'}
        matches = []
        for tx in rows:
            if _address(tx.get('account'), network) != intent['source']:
                raise ValueError('Wrong source account')
            if type(tx.get('now')) is not int or not start <= tx['now'] <= end:
                raise ValueError('Wrong source time')
            outgoing = tx.get('out_msgs')
            if not isinstance(outgoing, list) or len(outgoing) > 256:
                raise ValueError('Missing messages')
            for msg in outgoing:
                if _message_matches(msg, intent):
                    matches.append((tx, msg))
        if len(matches) != 1:
            return {**evidence, 'status': 'ambiguous' if matches else 'not_found'}
        source, msg = matches[0]
        evidence.update(source_tx=_hash(source.get('hash')), message_hash=_hash(msg.get('hash')))
        if not _final(source):
            return {**evidence, 'status': 'source_pending'}
        if not _executed(source):
            return {**evidence, 'status': 'source_needs_review'}
        await asyncio.sleep(1.2)  # Public provider, no request burst or background poll.
        received = await get(http, 'transactionsByMessage', {'msg_hash': evidence['message_hash'],
            'direction': 'in', 'limit': 2}, 2)
        if len(received) != 1:
            return {**evidence, 'status': 'recipient_pending' if not received else 'ambiguous'}
        dest = received[0]
        incoming = dest.get('in_msg')
        if (_address(dest.get('account'), network) != intent['destination']
                or not _message_matches(incoming, intent) or _hash(incoming.get('hash')) != evidence['message_hash']
                or type(dest.get('now')) is not int or not source['now'] <= dest['now'] <= source['now'] + 600):
            raise ValueError('Receiving transaction mismatch')
        evidence['recipient_tx'] = _hash(dest.get('hash'))
        if not _final(dest):
            return {**evidence, 'status': 'recipient_pending'}
        if dest.get('orig_status') in {'nonexist','uninit'}:
            try:
                credit=uninitialized_credit(dest,incoming)
                if uninitialized_credit(dest,msg)!=credit:
                    raise ValueError('Contradictory source/recipient credit evidence')
            except ValueError:
                return {**evidence, 'status':'recipient_needs_review'}
            lt=dest.get('lt')
            if isinstance(lt,str) and re.fullmatch('0|[1-9][0-9]{0,19}',lt) and int(lt)<2**64:
                credit['recipient_lt']=lt
            credit['incoming_value_nano']=str(_unsigned(incoming.get('value')))
            credit['transit_accounting']='provider_reported_net_not_spend_authorized'
            return {**evidence,**credit,'status':'provider_delivered'}
        if not _executed(dest):
            # Does not assert a completed refund, nor authorize resending.
            return {**evidence, 'status': 'recipient_needs_review'}
        outgoing = dest.get('out_msgs')
        # Restrict terminal delivery to a simple receiver: forwarded/refunded value
        # (even without a bounced flag) needs contract/trace-specific review.
        if not isinstance(outgoing, list) or outgoing:
            return {**evidence, 'status': 'recipient_needs_review'}
        try:
            credit = active_credit(dest, incoming)
        except ValueError:
            # Message delivery is independent of the stricter balance delta.
            # Never synthesize a transit amount from the nominal intent amount.
            return {**evidence, 'status': 'provider_delivered',
                    'transit_accounting': 'needs_review'}
        lt=dest.get('lt')
        if isinstance(lt,str) and re.fullmatch('0|[1-9][0-9]{0,19}',lt) and int(lt)<2**64:
            credit['recipient_lt']=lt
        return {**evidence, **credit, 'status': 'provider_delivered',
                'transit_accounting': 'provider_reported_net_not_spend_authorized'}

    try:
        async with asyncio.timeout(45):
            if client is not None:
                return await check(client)
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
            }
            async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(20, connect=5), trust_env=False, follow_redirects=False) as http:
                last_err = None
                for attempt in range(3):
                    try:
                        return await check(http)
                    except (ReconciliationError, httpx.HTTPError, TimeoutError) as exc:
                        if isinstance(exc, ReconciliationError) and not exc.retryable:
                            raise
                        last_err = exc
                        if attempt < 2:
                            await asyncio.sleep(2.0 * (attempt + 1))
                if last_err is not None:
                    raise last_err
    except ReconciliationError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError, ReadError, httpx.HTTPError, TimeoutError):
        raise ReconciliationError('Incomplete/mismatching reconciliation response or timeout; intent remains unresolved') from None
