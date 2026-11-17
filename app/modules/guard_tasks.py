"""Resumable read-only one-pass Guard; successful accounts are never re-checked."""
import asyncio
import json
from modules.accounts import AccountService
from modules.profile_apply import fingerprint
from modules.security_guard import SecurityGuardService, GuardResult, pause
from core.task_history import HistoryConflict, checked_spec, checked_done


def guard_plan(config, ids, ui_context):
    checked_spec({"kind": "guard-once", "account_ids": ids, "context": "0"*64})
    wanted = set(ids)
    accounts = [item for item in AccountService(config).list_accounts() if item.id in wanted and item.enabled]
    if {item.id for item in accounts} != wanted:
        raise HistoryConflict("Selected account was removed or disabled")
    context = fingerprint([ui_context, sorted([a.id, a.user_id, a.backend, a.session_kind, a.session_ref, a.group, sorted(a.groups)] for a in accounts)])
    return {"kind": "guard-once", "account_ids": sorted(ids), "context": context}


async def run_guard_task(config, history, job_id, ui_context, *, stop_event=None, progress=None):
    row = history.get(job_id)
    spec = checked_spec(json.loads(row["spec"]))
    if not spec or guard_plan(config, spec["account_ids"], ui_context) != spec:
        raise HistoryConflict("Guard account context changed; no automatic replacement")
    stop, result = stop_event or asyncio.Event(), GuardResult()
    done = set(checked_done(row, spec))
    if progress:
        progress.set_total(len(spec["account_ids"]))
        for account in sorted(done):
            progress.mark_ok(account)
    service = SecurityGuardService(config)
    for account in spec["account_ids"]:
        if stop.is_set():
            raise asyncio.CancelledError()
        if account in done:
            continue
        if guard_plan(config, spec["account_ids"], ui_context) != spec:
            raise HistoryConflict("Guard selection changed during task")
        outcome = await service.check([account], stop_event=stop)
        if stop.is_set():
            raise asyncio.CancelledError()
        if outcome.ok:
            history.checkpoint(job_id, account)
            result.add_ok(account)
            if progress:
                progress.mark_ok(account)
        elif outcome.skipped:
            result.skipped += outcome.skipped
        else:
            result.add_error(account, "Guard not observed; cooldown/error/context requires review")
            if progress:
                progress.mark_error(account)
        result.details.update(outcome.details)
        if account != spec["account_ids"][-1] and await pause(stop, 2):
            raise asyncio.CancelledError()
    return result
