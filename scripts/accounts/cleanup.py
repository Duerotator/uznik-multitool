"""
Bulk account cleanup: leave all chats/channels, block bots, delete history and contacts.
Skips Telegram service chat (777000).

Usage:
  python scripts/accounts/cleanup.py --accounts YOUR_ACCOUNT_ID  # preview
  python scripts/accounts/cleanup.py --group YOUR_GROUP --execute
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from core.config import AppConfig
from core.models import AccountRecord, utc_now_iso
from core.telegram_client import PyrogramAccountClient
from modules.accounts import AccountService
from utils.telegram_errors import short_error

logger = logging.getLogger("cleanup")

SERVICE_USER_ID = 777000
SKIP_NAMES = ("SpamBot", "Telegram")
SKIP_USERNAMES = frozenset(
    {
        "fastgiveawaysbot",
        "idbot",
        "id_bot",
        "random",
        "randombeast",
        "randombeast_bot",
        "bestrandom_bot",
        "spambot",
        "dateregbot",
    }
)

CLEANUP_COMPLETED_AT = "cleanup_completed_at"
FLOOD_WAIT_SKIP_SECONDS = 30
CONTACT_DELETE_BATCH_SIZE = 100


class LongFloodWait(Exception):
    """Stops cleanup for one account without issuing more Telegram requests."""

    def __init__(self, seconds: int):
        self.seconds = seconds
        super().__init__(f"FloodWait for {seconds}s")


def normalize_whitelist_title(value: str) -> str:
    """Keep letters/digits only, so trailing emoji and punctuation do not affect matching."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    pieces: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if category[0] in {"L", "N", "M"}:
            pieces.append(char)
        else:
            pieces.append(" ")
    return " ".join("".join(pieces).split())


async def build_whitelist_snapshot(client) -> tuple[set[int], set[str], set[str]]:
    """Fetch whitelist identities once before cleanup; never query per dialog."""
    user_ids: set[int] = set()
    usernames = set(SKIP_USERNAMES)
    titles: set[str] = set()
    failures: list[str] = []
    for username in sorted(SKIP_USERNAMES):
        try:
            chat = await client.get_chat(username)
            chat_id = getattr(chat, "id", None)
            if chat_id is None:
                raise RuntimeError("whitelist chat has no id")
            user_ids.add(chat_id)
            canonical_username = str(getattr(chat, "username", "") or "").lstrip("@").casefold()
            if canonical_username:
                usernames.add(canonical_username)
            title = str(getattr(chat, "title", "") or getattr(chat, "first_name", "") or "")
            normalized_title = normalize_whitelist_title(title)
            if normalized_title:
                titles.add(normalized_title)
        except Exception as exc:
            failures.append(f"@{username}: {short_error(exc)}")
    if not user_ids:
        raise RuntimeError("Whitelist snapshot did not resolve any protected account")
    return user_ids, usernames, titles, failures


def should_skip_dialog(
    chat,
    chat_name: str,
    whitelist_user_ids: set[int] | None = None,
    whitelist_usernames: set[str] | None = None,
    whitelist_titles: set[str] | None = None,
) -> bool:
    """Keep service and whitelist identities out of cleanup using only cached metadata."""
    chat_id = getattr(chat, "id", None)
    if chat_id == SERVICE_USER_ID:
        return True
    if whitelist_user_ids is not None and chat_id in whitelist_user_ids:
        return True
    username = str(getattr(chat, "username", "") or "").lstrip("@").casefold()
    known_usernames = whitelist_usernames if whitelist_usernames is not None else SKIP_USERNAMES
    if username in known_usernames:
        return True
    normalized_name = normalize_whitelist_title(chat_name)
    for title in whitelist_titles or set():
        if normalized_name == title:
            return True
    return any(normalized_name.startswith(normalize_whitelist_title(name)) for name in SKIP_NAMES)


def cleanup_completed(account: AccountRecord) -> bool:
    """Return whether the account was fully cleaned by a prior successful run."""
    return bool(account.metadata.get(CLEANUP_COMPLETED_AT))


def _long_flood_wait_seconds(exc: Exception) -> int | None:
    """Return a FloodWait duration only when it exceeds the safe wait limit."""
    if exc.__class__.__name__ != "FloodWait":
        return None

    for attribute in ("value", "seconds", "x"):
        value = getattr(exc, attribute, None)
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            continue
        if seconds > FLOOD_WAIT_SKIP_SECONDS:
            return seconds
    return None


def _raise_if_long_flood_wait(exc: Exception) -> None:
    seconds = _long_flood_wait_seconds(exc)
    if seconds is not None:
        raise LongFloodWait(seconds) from exc


async def delete_dialog_history(client, chat_id: int) -> int:
    """Delete an entire private/basic-group history and remove its dialog row.

    Telegram may process a large history in chunks. messages.DeleteHistory
    reports a non-zero offset while more messages remain, so one invocation is
    not sufficient for every dialog.
    """
    from pyrogram.raw.functions.messages import DeleteHistory

    calls = 0
    while True:
        result = await client.invoke(
            DeleteHistory(
                peer=await client.resolve_peer(chat_id),
                max_id=0,
                just_clear=False,
                revoke=False,
            )
        )
        calls += 1
        if int(getattr(result, "offset", 0) or 0) <= 0:
            return calls
        if calls >= 1000:
            raise RuntimeError(f"History deletion did not finish after {calls} chunks")


async def delete_imported_contacts(client) -> tuple[int, bool]:
    """Delete Telegram contacts and clear saved imported numbers without Telegram accounts."""
    from pyrogram.raw.functions.contacts import ResetSaved

    contacts = await client.get_contacts()
    contact_ids = [contact.id for contact in contacts if not getattr(contact, "is_self", False)]
    for start in range(0, len(contact_ids), CONTACT_DELETE_BATCH_SIZE):
        await client.delete_contacts(contact_ids[start : start + CONTACT_DELETE_BATCH_SIZE])
    reset_saved = bool(await client.invoke(ResetSaved()))
    return len(contact_ids), reset_saved


async def cleanup_one(
    account: AccountRecord,
    config: AppConfig,
    execute: bool,
) -> dict[str, int]:
    from pyrogram.enums import ChatType
    stats = {
        "left": 0,
        "blocked": 0,
        "deleted": 0,
        "contacts": 0,
        "saved_contacts": 0,
        "skipped": 0,
        "errors": 0,
        "finished": 0,
    }
    label = account.label or account.id[:16]
    if cleanup_completed(account):
        stats["skipped"] += 1
        logger.info("[%s] Already cleaned; no Telegram requests will be made", label)
        return stats

    managed_client = PyrogramAccountClient(config, account)
    try:
        await managed_client.start()
        client = managed_client.client
        if client is None:
            raise RuntimeError("Pyrogram client was not initialized")
        me = await client.get_me()
        logger.info("[%s] Connected as %s (@%s)", label, me.first_name, me.username)
    except Exception as exc:
        seconds = _long_flood_wait_seconds(exc)
        if seconds is not None:
            stats["skipped"] += 1
            logger.warning("[%s] FloodWait %ss; skipping account", label, seconds)
            return stats
        logger.error("[%s] Cannot connect: %s", label, short_error(exc))
        stats["errors"] += 1
        return stats

    try:
        dialogs = []
        try:
            whitelist_user_ids, whitelist_usernames, whitelist_titles, whitelist_warnings = await build_whitelist_snapshot(client)
        except Exception as exc:
            stats["errors"] += 1
            logger.error("[%s] Whitelist snapshot failed; cleanup stopped before dialog changes: %s", label, short_error(exc))
            return stats
        for warning in whitelist_warnings:
            logger.warning("[%s] Whitelist entry unavailable and kept as username-only rule: %s", label, warning)
        logger.info(
            "[%s] Whitelist snapshot loaded: ids=%d usernames=%d titles=%d warnings=%d",
            label,
            len(whitelist_user_ids),
            len(whitelist_usernames),
            len(whitelist_titles),
            len(whitelist_warnings),
        )
        async for dialog in client.get_dialogs(limit=None):
            dialogs.append(dialog)

        logger.info("[%s] Found %d dialogs", label, len(dialogs))

        for dialog in dialogs:
            chat = dialog.chat
            chat_id = chat.id
            chat_name = chat.title or chat.first_name or str(chat_id)

            if should_skip_dialog(chat, chat_name, whitelist_user_ids, whitelist_usernames, whitelist_titles):
                stats["skipped"] += 1
                logger.info("[%s] Skipped: %s", label, chat_name)
                continue

            if chat.type in (ChatType.BOT,):
                if execute:
                    try:
                        await client.block_user(chat_id)
                        stats["blocked"] += 1
                        logger.info("[%s] Blocked bot: %s", label, chat_name)
                    except Exception as exc:
                        _raise_if_long_flood_wait(exc)
                        logger.warning("[%s] Block failed for %s: %s", label, chat_name, short_error(exc))
                        stats["errors"] += 1
                    try:
                        chunks = await delete_dialog_history(client, chat_id)
                        stats["deleted"] += 1
                        logger.info(
                            "[%s] Deleted bot dialog: %s (%d chunk%s)",
                            label,
                            chat_name,
                            chunks,
                            "" if chunks == 1 else "s",
                        )
                    except Exception as exc:
                        _raise_if_long_flood_wait(exc)
                        logger.warning(
                            "[%s] Bot dialog delete failed for %s: %s",
                            label,
                            chat_name,
                            short_error(exc),
                        )
                        stats["errors"] += 1
                else:
                    stats["blocked"] += 1
                    stats["deleted"] += 1
                    logger.info("[%s] [DRY] Would block and delete dialog: %s", label, chat_name)
            elif chat.type in (ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP):
                if execute:
                    if chat.type == ChatType.GROUP:
                        try:
                            await delete_dialog_history(client, chat_id)
                            stats["deleted"] += 1
                        except Exception as exc:
                            _raise_if_long_flood_wait(exc)
                            logger.warning(
                                "[%s] Group history delete failed for %s: %s",
                                label,
                                chat_name,
                                short_error(exc),
                            )
                            stats["errors"] += 1
                    try:
                        await client.leave_chat(chat_id)
                        stats["left"] += 1
                        logger.info("[%s] Left: %s", label, chat_name)
                    except Exception as exc:
                        _raise_if_long_flood_wait(exc)
                        err = short_error(exc)
                        if "not a participant" in err.lower() or "not participant" in err.lower():
                            stats["left"] += 1
                            logger.info("[%s] Already left: %s", label, chat_name)
                        else:
                            logger.warning("[%s] Leave failed for %s: %s", label, chat_name, err)
                            stats["errors"] += 1
                else:
                    stats["left"] += 1
                    if chat.type == ChatType.GROUP:
                        stats["deleted"] += 1
                        logger.info("[%s] [DRY] Would delete history and leave: %s", label, chat_name)
                    else:
                        logger.info("[%s] [DRY] Would leave: %s", label, chat_name)
            elif chat.type == ChatType.PRIVATE:
                if chat_id == SERVICE_USER_ID:
                    stats["skipped"] += 1
                elif getattr(chat, "is_deleted", False):
                    stats["skipped"] += 1
                elif execute:
                    try:
                        chunks = await delete_dialog_history(client, chat_id)
                        stats["deleted"] += 1
                        logger.info(
                            "[%s] Deleted chat: %s (%d chunk%s)",
                            label,
                            chat_name,
                            chunks,
                            "" if chunks == 1 else "s",
                        )
                    except Exception as exc:
                        _raise_if_long_flood_wait(exc)
                        logger.warning("[%s] Delete failed for %s: %s", label, chat_name, short_error(exc))
                        stats["errors"] += 1
                else:
                    stats["deleted"] += 1
                    logger.info("[%s] [DRY] Would delete: %s", label, chat_name)

        if execute:
            try:
                deleted_contacts, reset_saved = await delete_imported_contacts(client)
                stats["contacts"] = deleted_contacts
                stats["saved_contacts"] = int(reset_saved)
                logger.info(
                    "[%s] Deleted %d Telegram contact(s); reset saved imported contacts=%s",
                    label,
                    deleted_contacts,
                    reset_saved,
                )
            except Exception as exc:
                _raise_if_long_flood_wait(exc)
                stats["errors"] += 1
                logger.warning("[%s] Contact cleanup failed: %s", label, short_error(exc))
        else:
            logger.info("[%s] [DRY] Would delete all Telegram and saved imported contacts", label)
        stats["finished"] = 1
    except LongFloodWait as exc:
        stats["skipped"] += 1
        logger.warning("[%s] FloodWait %ss; skipping remaining dialogs", label, exc.seconds)
    except Exception as exc:
        seconds = _long_flood_wait_seconds(exc)
        if seconds is not None:
            stats["skipped"] += 1
            logger.warning("[%s] FloodWait %ss; skipping account", label, seconds)
        else:
            logger.error("[%s] Cleanup error: %s", label, short_error(exc))
            stats["errors"] += 1
    finally:
        try:
            await managed_client.stop()
        except Exception:
            pass

    return stats


async def ensure_cleanup_gateway(config: AppConfig) -> asyncio.Task[None] | None:
    """Start Xray only when cleanup needs a missing local VPN gateway."""
    if os.getenv("PROXY_MODE", "").strip().lower() != "vpn_gateway":
        return None

    from modules.proxy_manager import ProxyPool
    from modules.vpn_gateway import gateway_ports, run_gateway_service, sync_gateway_pool

    pool = ProxyPool(config.proxy_pool_db)
    config_path = config.data_dir / "vpn_gateway" / "xray.json"
    ports = gateway_ports(config_path)
    if not ports:
        raise RuntimeError("VPN gateway configuration has no local SOCKS5 exits")

    stats = await sync_gateway_pool(pool, ports)
    if stats["valid"]:
        logger.info("cleanup gateway already ready: valid=%d", stats["valid"])
        return None

    refresh_minutes = int(os.getenv("VPN_GATEWAY_REFRESH_MINUTES", "360"))
    task = asyncio.create_task(
        run_gateway_service(pool, config.data_dir, refresh_minutes),
        name="cleanup-vpn-gateway",
    )
    for _ in range(20):
        await asyncio.sleep(1)
        stats = await sync_gateway_pool(pool, ports)
        if stats["valid"]:
            logger.info("cleanup gateway started: valid=%d", stats["valid"])
            return task

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    raise RuntimeError("Local VPN gateway did not produce a working SOCKS5 exit")


async def stop_cleanup_gateway(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    logger.info("cleanup-owned VPN gateway stopped")


async def run(accounts: list[AccountRecord], config: AppConfig, execute: bool, concurrency: int):
    accounts_svc = AccountService(config)
    gateway_task = await ensure_cleanup_gateway(config)
    semaphore = asyncio.Semaphore(concurrency)
    total = {"left": 0, "blocked": 0, "deleted": 0, "contacts": 0, "saved_contacts": 0, "skipped": 0, "errors": 0}

    async def worker(account: AccountRecord):
        if not account.enabled:
            return
        if cleanup_completed(account):
            total["skipped"] += 1
            logger.info(
                "[%s] Already cleaned; skipped before connecting to Telegram",
                account.label or account.id[:16],
            )
            return
        async with semaphore:
            stats = await cleanup_one(account, config, execute)
            for k in total:
                total[k] += stats[k]
            if execute and stats["finished"] and not stats["errors"]:
                accounts_svc.update_profile_metadata(
                    account.id,
                    {CLEANUP_COMPLETED_AT: utc_now_iso()},
                )
                logger.info("[%s] Cleanup marked as completed", account.label or account.id[:16])

    try:
        outcomes = await asyncio.gather(*(worker(a) for a in accounts), return_exceptions=True)
        for account, outcome in zip(accounts, outcomes):
            if isinstance(outcome, BaseException):
                total["errors"] += 1
                logger.error("[%s] Cleanup failed: %s", account.id, outcome)
    finally:
        await stop_cleanup_gateway(gateway_task)

    mode = "EXECUTED" if execute else "DRY-RUN"
    logger.info(
        "%s: left=%d blocked=%d deleted=%d skipped=%d errors=%d",
        mode, total["left"], total["blocked"], total["deleted"], total["skipped"], total["errors"],
    )


def main():
    parser = argparse.ArgumentParser(description="Bulk account cleanup: leave chats, block bots, delete history and contacts.")
    parser.add_argument("--execute", action="store_true", help="Actually perform cleanup (without this: dry-run only)")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--path", type=Path, help="Exact path of an already imported session")
    scope.add_argument("--accounts", nargs="+", help="Explicit account IDs")
    scope.add_argument("--group", help="Explicit group (inbox means all accounts in this application)")
    parser.add_argument("--concurrency", type=int, default=2, help="Parallel sessions (default: 2)")
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    os.chdir(ROOT)
    config = AppConfig.load(ROOT / ".env")
    config.require_telegram_api()

    accounts_svc = AccountService(config)

    if args.path:
        target = args.path.resolve()
        accounts = [account for account in accounts_svc.list_accounts()
                    if Path(account.session_ref).resolve() == target]
    elif args.accounts:
        accounts = [accounts_svc.get_account(account_id) for account_id in dict.fromkeys(args.accounts)]
    else:
        accounts = accounts_svc.list_accounts(group=args.group, enabled_only=True)

    if not accounts:
        print("No accounts found.")
        return

    mode = "EXECUTE" if args.execute else "DRY-RUN (no changes)"
    print(f"{mode} — {len(accounts)} account(s), concurrency={args.concurrency}")
    if not args.execute:
        print("Add --execute to actually perform cleanup.")
    print()

    asyncio.run(run(accounts, config, args.execute, args.concurrency))


if __name__ == "__main__":
    main()
