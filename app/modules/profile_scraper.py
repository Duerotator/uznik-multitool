from __future__ import annotations

import asyncio
import logging
import random
import re
import string
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.telegram_client import coerce_chat_id, create_client
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from modules.profile_bindings import ProfileBindings
from modules.profile_snapshots import ProfileSnapshotService
from modules.scrape_cache import ProfileFactsCache, ScrapeCache
from modules.profile_archive import ProfileArchive
from utils.rate_limit import human_delay
from utils.telegram_errors import short_error

log = logging.getLogger("ps")

LogFn = Callable[[str], None]


def _norm_ch(target: str) -> str:
    t = (target or "").strip()
    if not t:
        return ""
    if t.startswith("+"):
        return t
    low = t.lower()
    if "t.me/" in low or "telegram.me/" in low or "telegram.dog/" in low:
        if not re.match(r"^https?://", t, re.I):
            t = "https://" + t.lstrip("/")
        path = urlparse(t).path.strip("/")
        parts = [p for p in path.split("/") if p]
        if not parts:
            return ""
        head = parts[0]
        if head.lower() in {"joinchat"} and len(parts) > 1:
            return f"+{parts[1]}"
        if head.startswith("+"):
            return head
        if head.lower() == "c" and len(parts) > 1:
            return f"-100{parts[1]}"
        if head.lower() in {"s", "addstickers", "share", "proxy", "socks", "iv"}:
            return ""
        return head.split("?")[0].lstrip("@")
    t = t.split("?")[0].strip()
    t = t.lstrip("@")
    if "/" in t:
        t = t.split("/")[0]
    return t


@dataclass
class ProfileData:
    user_id: int
    username: str
    first_name: str
    last_name: str
    bio: str
    photo_count: int
    stories_count: int
    is_premium: bool
    avatars: list[bytes] = field(default_factory=list)
    stories: list[dict[str, Any]] = field(default_factory=list)
    music: bytes | None = None
    music_meta: dict[str, Any] = field(default_factory=dict)
    birthday: dict[str, int] | None = None
    has_custom_font: bool = False


@dataclass
class ScrapeResult:
    profiles: list[ProfileData]
    sources: list[str]
    total_parsed: int


class ProfileScraperService:

    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.cache = ScrapeCache(config.data_dir / "scraped_usernames")
        self.profile_facts = ProfileFactsCache(config.data_dir / "profile_facts.json")
        self.archive = ProfileArchive(config.data_dir)

    def get_account_pool(self, size: int = 0) -> list[AccountRecord]:
        all_acc = self.accounts.list_accounts(enabled_only=True)
        spamblocked = [a for a in all_acc if a.metadata.get("health_status") == "invalid" or a.metadata.get("spamblock")]
        clean = [a for a in all_acc if a not in spamblocked]
        random.shuffle(spamblocked)
        random.shuffle(clean)
        pool = spamblocked + clean
        if size > 0:
            pool = pool[:size]
        return pool

    async def scrape_from_group(
        self,
        group: str,
        *,
        scan_limit: int = 1000,
        use_cache: bool = True,
        account_pool: list[AccountRecord] | None = None,
        on_log: LogFn | None = None,
    ) -> ScrapeResult:
        acc_pool = account_pool or self.get_account_pool(size=3)
        if not acc_pool:
            raise RuntimeError("No enabled accounts")
        group = _norm_ch(group)
        if not group:
            raise RuntimeError("Empty group target")

        def _log(msg: str) -> None:
            log.info("%s", msg)
            if on_log:
                on_log(msg)

        # Resolve linked discussion group from a channel
        linked = await self._resolve_linked_chat(group, acc_pool[0])
        if linked and linked != group:
            _log(f"Following linked discussion group: {linked} (from {group})")
            group = linked
            # Don't use cache for the resolved group - it's a different key

        profiles: list[ProfileData] = []
        seen: set[int] = set()
        seen_un: set[str] = set()

        if use_cache:
            cached = self.cache.load_usernames(group)
            if cached:
                for item in cached:
                    uid = int(item.get("user_id") or 0)
                    uname = str(item.get("username") or "").strip().lstrip("@")
                    if not uname:
                        continue
                    un_key = uname.lower()
                    if un_key in seen_un or (uid > 0 and uid in seen):
                        continue
                    if uid:
                        seen.add(uid)
                    seen_un.add(un_key)
                    profiles.append(ProfileData(
                        user_id=uid, username=uname,
                        first_name=str(item.get("first_name") or ""),
                        last_name=str(item.get("last_name") or ""),
                        bio="", photo_count=0, stories_count=0,
                        is_premium=bool(item.get("is_premium", False)),
                    ))
                offset = len(cached)
                _log(f"Cache: {len(profiles)} usernames, scanning +{scan_limit} more…")
        client_fails = 0
        acc_idx = 0

        async def _try_page(client, offset: int) -> list[dict[str, Any]] | None:
            try:
                return await client.get_channel_members(group, limit=200, offset=offset)
            except Exception as exc:
                msg = short_error(exc)
                if any(kw in msg.upper() for kw in ("CHANNEL_PRIVATE", "CHANNEL_INVALID", "MEMBER_NO", "MEMBER")):
                    return None
                raise

        async with create_client(self.config, acc_pool[0]) as prime_client:
            try:
                await prime_client.join_chat(group)
            except Exception as exc:
                log.debug("join %s: %s", group, short_error(exc))

        # Phase 1: try GetParticipants page-by-page with rotation
        offset = 0 if len(profiles) == 0 else len(profiles)  # resume after cache
        scan_limit = max(100, min(int(scan_limit), 5000))
        empty_pages = 0
        max_empty_pages = 8
        total_members = await self._get_participant_count(group, acc_pool[0])
        if total_members:
            _log(f"Scanning {len(profiles)}/{total_members} members of @{group.lstrip('@')}…")
        else:
            _log(f"Scanning group (limit {scan_limit})…")

        while len(profiles) < scan_limit and empty_pages < max_empty_pages:
            acc = acc_pool[acc_idx % len(acc_pool)]
            acc_idx += 1
            try:
                async with create_client(self.config, acc) as client:
                    page = await _try_page(client, offset)
            except Exception as exc:
                msg = str(exc).upper()
                if "FLOOD" in msg and "WAIT" in msg:
                    delay_s = int(getattr(exc, "value", 0) or 0)
                    _log(f"FloodWait {delay_s}s on scan (acc {acc.id[:10]})")
                    await asyncio.sleep(min(delay_s + 3, 120))
                    client_fails += 1
                    if client_fails >= len(acc_pool) * 3:
                        _log("All accounts FloodWait — stopping scan")
                        break
                    continue
                client_fails += 1
                if client_fails >= len(acc_pool) * 2:
                    raise RuntimeError(f"Cannot read members of {group}: {short_error(exc)}") from exc
                await human_delay(0.5, 1.5)
                continue

            client_fails = 0

            if page is None:
                _log("GetParticipants blocked — falling back to message history scraping")
                fallback = await self._scrape_from_messages(group, acc_pool, scan_limit, _log)
                if fallback:
                    profiles = fallback
                break

            if not page:
                empty_pages += 1
                offset += 200
                await human_delay(0.1, 0.25)
                continue

            added = 0
            for m in page:
                uid = int(m.get("user_id") or 0)
                username = str(m.get("username") or "").strip().lstrip("@")
                if not username:
                    continue
                un_key = username.lower()
                if un_key in seen_un or (uid > 0 and uid in seen):
                    continue
                if uid:
                    seen.add(uid)
                seen_un.add(un_key)
                profiles.append(ProfileData(
                    user_id=uid,
                    username=username,
                    first_name=str(m.get("first_name") or ""),
                    last_name=str(m.get("last_name") or ""),
                    bio="", photo_count=0, stories_count=0,
                    is_premium=bool(m.get("is_premium", False)),
                ))
                added += 1
                if len(profiles) >= scan_limit:
                    break

            offset += len(page)
            if added == 0:
                empty_pages += 1
            else:
                empty_pages = 0
            if len(page) < 50:
                empty_pages += 1
            await human_delay(0.2, 0.5)

        _log(f"Collected {len(profiles)} usernames (method=getparticipants|fallback)")

        if profiles:
            cache_data = [{
                "user_id": p.user_id,
                "username": p.username,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "is_premium": p.is_premium,
            } for p in profiles]
            self.cache.save_usernames(group, cache_data, "scrape")

        return ScrapeResult(profiles=profiles, sources=[group], total_parsed=len(profiles))

    async def _scrape_from_messages(
        self,
        group: str,
        pool: list[AccountRecord],
        limit: int,
        _log: LogFn,
    ) -> list[ProfileData] | None:
        acc = pool[0]
        try:
            async with create_client(self.config, acc) as client:
                await client.join_chat(group)
                senders = await client.get_recent_chat_senders(group, limit=min(limit * 2, 500))
                if not senders:
                    return None
                profiles = [ProfileData(
                    user_id=int(m.get("user_id") or 0),
                    username=str(m.get("username") or ""),
                    first_name=str(m.get("first_name") or ""),
                    last_name=str(m.get("last_name") or ""),
                    bio="", photo_count=0, stories_count=0,
                    is_premium=bool(m.get("is_premium", False)),
                ) for m in senders]
                _log(f"Message fallback: {len(profiles)} usernames from recent senders")
                return profiles[:limit]
        except Exception as exc:
            log.warning("message fallback %s: %s", group, exc)
            return None

    async def _get_participant_count(self, target: str, acc: AccountRecord) -> int | None:
        try:
            async with create_client(self.config, acc) as client:
                from pyrogram import raw
                peer = await client.resolve_chat_peer(target)
                if isinstance(peer, raw.types.InputPeerChannel):
                    inp = raw.types.InputChannel(
                        channel_id=getattr(peer, "channel_id", 0),
                        access_hash=getattr(peer, "access_hash", 0),
                    )
                    full = await client.client.invoke(raw.functions.channels.GetFullChannel(channel=inp))
                    count = getattr(full.full_chat, "participants_count", None)
                    if count is not None and count > 0:
                        return int(count)
                elif isinstance(peer, raw.types.InputPeerChat):
                    full = await client.client.invoke(raw.functions.messages.GetFullChat(chat_id=peer.chat_id))
                    participants = getattr(getattr(full, "full_chat", None), "participants", None)
                    if isinstance(participants, list):
                        return len(participants)
        except Exception:
            pass
        return None

    async def scrape_from_channel(self, channel: str, count: int = 50, **kwargs) -> ScrapeResult:
        return await self.scrape_from_group(channel, scan_limit=max(int(count) * 30, 500), **kwargs)

    async def _resolve_linked_chat(self, target: str, acc: AccountRecord) -> str | None:
        try:
            async with create_client(self.config, acc) as client:
                from pyrogram import raw
                peer = await client.resolve_chat_peer(target)
                if not isinstance(peer, raw.types.InputPeerChannel):
                    return None
                inp = raw.types.InputChannel(
                    channel_id=getattr(peer, "channel_id", 0),
                    access_hash=getattr(peer, "access_hash", 0),
                )
                # Only follow the link from a broadcast channel to its discussion group.
                # A supergroup's linked_chat_id points back to its broadcast channel,
                # and following it would break group scraping.
                try:
                    chans = await client.client.invoke(raw.functions.channels.GetChannels(id=[inp]))
                    ch = getattr(chans, "chats", None) or []
                    if ch and getattr(ch[0], "megagroup", False):
                        return None
                except Exception:
                    pass
                full = await client.client.invoke(raw.functions.channels.GetFullChannel(channel=inp))
                lid = getattr(full.full_chat, "linked_chat_id", None)
                if lid:
                    formatted = f"-100{lid}"
                    log.info("resolved linked chat %s for %s", formatted, target)
                    return formatted
        except Exception as exc:
            log.debug("_resolve_linked_chat %s: %s", target, exc)
        return None

    async def fetch_full_profiles(
        self,
        profiles: list[ProfileData],
        *,
        download_avatars: bool = True,
        download_stories: bool = False,
        download_music: bool = False,
        with_birthday: bool = False,
        max_avatars: int = 13,
        max_stories: int = 18,
        min_avatars: int = 0,
        min_stories: int = 0,
        require_bio: bool = False,
        require_stories: bool = False,
        max_count: int = 0,
        probe_account: AccountRecord | None = None,
        progress: OperationProgress | None = None,
        on_log: LogFn | None = None,
    ) -> list[ProfileData]:
        acc = probe_account or self._first_account()
        if acc is None:
            raise RuntimeError("No enabled accounts")
        acc_pool = self.get_account_pool(size=6)
        if acc not in acc_pool:
            acc_pool.insert(0, acc)
        target = max(1, max_count or 5)
        if require_stories:
            min_stories = max(min_stories, 1)
        need_photo_count = min_avatars > 1 or download_avatars
        need_stories = min_stories > 0 or download_stories
        result: list[ProfileData] = []
        checked = 0
        fails = 0
        skipped_filter = 0
        skipped_archived = 0
        cache_hits = 0

        def _log(msg: str) -> None:
            log.info("%s", msg)
            if on_log:
                on_log(msg)

        async with create_client(self.config, acc) as client:
            stories_ok = await client.supports_stories() if hasattr(client, "supports_stories") else False
            if need_stories and not stories_ok:
                _log("Stories API unavailable — story filter/download disabled")
                need_stories = False
                download_stories = False
                min_stories = 0

        pool = list(profiles)
        random.shuffle(pool)
        acc_idx = 0
        cooldowns: dict[str, float] = {}
        # Observed profile-lookup behavior: the fifth short FloodWait may
        # escalate to a day-long wait. Rotate before issuing that request.
        flood_swap_after_count = 4
        flood_cooldown_seconds = 15 * 60

        async def _get_client():
            nonlocal acc_idx
            for _ in range(len(acc_pool) * 2):
                a = acc_pool[acc_idx % len(acc_pool)]
                acc_idx += 1
                if cooldowns.get(a.id, 0.0) <= time.time():
                    return create_client(self.config, a)
            raise RuntimeError("All pool accounts on FloodWait cooldown — stopping")

        def _flood_wait_seconds(exc: Exception) -> int:
            if "FLOOD" not in str(exc).upper() or "WAIT" not in str(exc).upper():
                return 0
            try:
                delay_s = int(getattr(exc, "value", 0) or getattr(exc, "seconds", 0) or 0)
            except (TypeError, ValueError):
                delay_s = 0
            if delay_s:
                return delay_s
            match = re.search(r"FLOOD(?:_PREMIUM)?_WAIT[_ ]?(\d+)", str(exc), re.I)
            return int(match.group(1)) if match else 0

        async def _full_for(client, profile: ProfileData) -> dict[str, Any] | None:
            lookups = []
            if profile.username:
                lookups.append(profile.username)
            if profile.user_id:
                lookups.append(str(profile.user_id))
            for lookup in lookups:
                for _ in range(3):
                    try:
                        full = await client.get_user_full(
                            lookup,
                            with_photo_count=need_photo_count,
                            with_stories=need_stories,
                        )
                        if full:
                            return full
                    except Exception as exc:
                        delay_s = _flood_wait_seconds(exc)
                        if delay_s:
                            raise
                        if "USERNAME_INVALID" in str(exc).upper() or "USER_NOT_PARTICIPANT" in str(exc).upper():
                            break
                        raise
            return None

        async def _prepare_profile(client, profile: ProfileData, full: dict[str, Any]) -> bool:
            nonlocal skipped_filter, skipped_archived
            self.profile_facts.save(full)
            profile.user_id = int(full.get("user_id") or profile.user_id or 0)
            profile.username = str(full.get("username") or profile.username)
            profile.first_name = str(full.get("first_name") or profile.first_name)
            profile.last_name = str(full.get("last_name") or "")
            profile.bio = str(full.get("bio") or "")
            profile.photo_count = int(full.get("photo_count", 0))
            profile.stories_count = int(full.get("stories_count", 0))
            profile.is_premium = bool(full.get("is_premium", profile.is_premium))
            profile.has_custom_font = _detect_custom_font(profile.first_name + profile.last_name + profile.bio)
            if with_birthday:
                birthday = full.get("birthday")
                if isinstance(birthday, dict) and birthday.get("day") and birthday.get("month"):
                    profile.birthday = birthday
            if download_music:
                music_meta = full.get("music")
                if isinstance(music_meta, dict) and music_meta.get("id"):
                    profile.music_meta = music_meta

            if ((min_avatars and profile.photo_count < min_avatars)
                    or (min_stories and profile.stories_count < min_stories)
                    or (require_bio and not profile.bio.strip())):
                skipped_filter += 1
                return False
            archived_key = self.archive.find_key(profile.username, profile.user_id)
            if archived_key:
                skipped_archived += 1
                _log(f"Skipping archived profile {archived_key}")
                return False
            lookup = profile.username or str(profile.user_id)
            if download_avatars and profile.photo_count:
                try:
                    profile.avatars = await client.download_profile_photos(lookup, limit=max_avatars)
                except Exception as exc:
                    log.warning("avatars %s: %s", profile.username, short_error(exc))
                if min_avatars and not profile.avatars:
                    skipped_filter += 1
                    return False
            if download_stories and profile.stories_count:
                try:
                    profile.stories = await client.download_stories(lookup, limit=max_stories)
                except Exception as exc:
                    log.warning("stories %s: %s", profile.username, short_error(exc))
            if profile.username and (profile.avatars or profile.stories or profile.bio):
                try:
                    self.archive.save(profile.username, {
                        "user_id": profile.user_id,
                        "first_name": profile.first_name, "last_name": profile.last_name,
                        "bio": profile.bio, "photo_count": profile.photo_count,
                        "stories_count": profile.stories_count, "is_premium": profile.is_premium,
                        "avatars": profile.avatars, "stories": profile.stories,
                        "music_meta": profile.music_meta, "birthday": profile.birthday,
                    })
                except Exception as exc:
                    log.warning("archive %s: %s", profile.username, short_error(exc))
            return True

        client_cm = await _get_client()
        client = await client_cm.__aenter__()
        try:
            for profile in pool:
                if len(result) >= target:
                    break
                checked += 1
                try:
                    cached_full = None
                    if not with_birthday and not download_music:
                        cached_full = self.profile_facts.get(profile.user_id, profile.username)
                    full = cached_full or await _full_for(client, profile)
                    if cached_full:
                        cache_hits += 1
                    elif getattr(client, "flood_wait_count", 0) >= flood_swap_after_count:
                        _log(
                            f"FloodWait #{client.flood_wait_count} on {client.account.id[:10]} "
                            f"— rotating before the next lookup"
                        )
                        cooldowns[client.account.id] = time.time() + flood_cooldown_seconds
                        await client_cm.__aexit__(None, None, None)
                        await human_delay(2, 5)
                        client_cm = await _get_client()
                        client = await client_cm.__aenter__()
                        pool.append(profile)
                        checked -= 1
                        continue
                    if full is None:
                        fails += 1
                        if progress:
                            progress.mark_error(profile.username or str(profile.user_id))
                        continue
                    if not await _prepare_profile(client, profile, full):
                        if progress:
                            progress.mark_error(profile.username or str(profile.user_id))
                        continue
                    result.append(profile)
                    if progress:
                        progress.mark_ok(profile.username or str(profile.user_id))
                    if len(result) % 5 == 0 or len(result) == target:
                        _log(f"Matched {len(result)}/{target} (chk={checked} flt={skipped_filter} arch={skipped_archived} fail={fails})")
                except Exception as exc:
                    delay_s = _flood_wait_seconds(exc)
                    if delay_s:
                        _log(f"FloodWait {delay_s}s on {client.account.id[:10]} — swapping account")
                        cooldown_for = max(flood_cooldown_seconds, delay_s + 30)
                        cooldowns[client.account.id] = time.time() + cooldown_for
                        await client_cm.__aexit__(None, None, None)
                        await human_delay(2, 5)
                        client_cm = await _get_client()
                        client = await client_cm.__aenter__()
                        pool.append(profile)
                        checked -= 1
                        continue
                    fails += 1
                    if progress:
                        progress.mark_error(profile.username or str(profile.user_id))
                    log.warning("full %s: %s", profile.username or profile.user_id, short_error(exc))
                await human_delay(1.5, 3.5)
        finally:
            await client_cm.__aexit__(None, None, None)

        _log(f"Done: matched={len(result)} wanted={target} pool={len(profiles)} checked={checked} cache={cache_hits} flt={skipped_filter} arch={skipped_archived} fail={fails}")
        return result

    async def apply_to_accounts(
        self,
        accounts: list[AccountRecord],
        source_profiles: list[ProfileData],
        *,
        copy_name: bool = True,
        copy_bio: bool = True,
        copy_username: bool = True,
        copy_avatars: bool = True,
        copy_music: bool = False,
        copy_stories: bool = False,
        copy_birthday: bool = False,
        clear_first: bool = False,
        progress: OperationProgress | None = None,
        on_log: LogFn | None = None,
    ) -> ActionResult:
        result = ActionResult()
        enabled = [a for a in accounts if a.enabled]

        def _log(msg: str) -> None:
            log.info("%s", msg)
            if on_log:
                on_log(msg)

        if not source_profiles:
            msg = "No matching profiles — nothing applied"
            result.add_error("system", msg)
            _log(msg)
            if progress:
                for a in enabled:
                    progress.mark_error(a.id)
            return result

        if len(source_profiles) < len(enabled):
            skipped_count = len(enabled) - len(source_profiles)
            _log(
                f"Applying {len(source_profiles)} matched profile(s) to "
                f"{len(source_profiles)} account(s); {skipped_count} eligible account(s) left unchanged"
            )
            enabled = enabled[: len(source_profiles)]

        try:
            ProfileSnapshotService(self.config).save_latest(enabled, "pre-profile-scrape-apply")
        except Exception as exc:
            log.warning("snapshot failed: %s", short_error(exc))

        pairs = list(zip(enabled, source_profiles[: len(enabled)]))
        random.shuffle(pairs)
        sem = asyncio.Semaphore(1)

        async def worker(acc: AccountRecord, source: ProfileData) -> None:
            async with sem:
                await human_delay(max(self.config.min_action_delay, 5.0), max(self.config.max_action_delay, 12.0))
                try:
                    async with create_client(self.config, acc) as client:
                        uploaded = await _apply_profile(
                            client, acc, source,
                            clear_first=clear_first,
                            copy_name=copy_name, copy_bio=copy_bio, copy_username=copy_username,
                            copy_avatars=copy_avatars, copy_music=copy_music,
                            copy_stories=copy_stories, copy_birthday=copy_birthday,
                        )
                        try:
                            self.accounts.update_profile_metadata(
                                acc.id,
                                {
                                    "profiled": True,
                                    "last_known_bio": source.bio or "",
                                    "profile_source_username": source.username or "",
                                    "profiled_at": datetime.now(timezone.utc).isoformat(),
                                    "stories_uploaded": uploaded,
                                },
                            )
                        except Exception:
                            pass
                        try:
                            ProfileBindings(self.config).bind(
                                acc.id,
                                source.username or str(source.user_id),
                                user_id=source.user_id or None,
                            )
                        except Exception:
                            pass
                        result.add_ok(acc.id)
                        if progress:
                            progress.mark_ok(acc.id)
                except Exception as exc:
                    err = short_error(exc)
                    if _is_frozen(exc):
                        result.add_error(acc.id, err + " [frozen, auto-deleted]")
                        log.warning("%s frozen, deleting", acc.id)
                        try:
                            self.accounts.delete_accounts([acc.id], delete_sessions=True)
                        except Exception:
                            pass
                    else:
                        result.add_error(acc.id, err)
                    if progress:
                        progress.mark_error(acc.id)

        await asyncio.gather(*(worker(a, s) for a, s in pairs))
        _log(result.summary("profile-apply"))
        return result

    async def download_stories_for_bound(
        self,
        premium_accounts: list[AccountRecord],
        *,
        max_stories: int = 18,
        force: bool = False,
        progress: OperationProgress | None = None,
        on_log: LogFn | None = None,
    ) -> ActionResult:
        """Download stories for bound profiles using a premium session (stories only)."""
        result = ActionResult()

        def _log(msg: str) -> None:
            log.info("%s", msg)
            if on_log:
                on_log(msg)

        enabled = [a for a in premium_accounts if a.enabled]
        if not enabled:
            _log("No enabled accounts selected as premium session.")
            return result
        bindings = ProfileBindings(self.config)
        archive = ProfileArchive(self.config.data_dir)
        items: list[tuple[str, dict[str, Any]]] = []
        seen_profile_keys: set[str] = set()
        duplicate_bindings = 0
        for account_id, binding in bindings.all().items():
            key = str(binding.get("profile_key") or "")
            if not archive.has(key):
                continue
            if key in seen_profile_keys:
                duplicate_bindings += 1
                continue
            seen_profile_keys.add(key)
            items.append((account_id, binding))
        if not items:
            _log("No bound profiles found — scrape & apply a profile first, then retry.")
            return result
        if progress:
            progress.set_total(len(items))
        parallelism = min(len(enabled), max(1, self.config.max_concurrency))
        _log(f"Premium sessions: {len(enabled)}, parallel downloads: {parallelism} (round robin)")
        if duplicate_bindings:
            _log(f"Ignored {duplicate_bindings} duplicate profile binding(s) for story download.")
        _log(f"Premium story targets: {len(items)} bound profile(s), up to 13 photos + 5 videos each")
        total = 0
        skipped = 0
        failed = 0
        semaphore = asyncio.Semaphore(parallelism)
        archive_lock = asyncio.Lock()

        async def worker(index: int, account_id: str, binding: dict[str, Any]) -> None:
            nonlocal total, skipped, failed
            key = str(binding.get("profile_key") or "")
            if not force and archive.has_stories(key):
                skipped += 1
                result.add_ok(account_id)
                result.details[account_id] = f"skipped: {key} already has stories"
                if progress:
                    progress.mark_ok(account_id)
                return
            target = binding.get("user_id") or binding.get("username")
            if not target:
                failed += 1
                result.add_error(account_id, "missing user ID and username")
                if progress:
                    progress.mark_error(account_id)
                _log(f"premium stories {key}: missing user ID and username")
                return
            premium = enabled[index % len(enabled)]
            async with semaphore:
                try:
                    async with create_client(self.config, premium) as client:
                        try:
                            stories = await client.download_stories(str(target), limit=max_stories)
                        except Exception as exc:
                            fallback = binding.get("username") if binding.get("user_id") else None
                            if not fallback:
                                raise exc
                            stories = await client.download_stories(str(fallback), limit=max_stories)
                except Exception as exc:
                    failed += 1
                    result.add_error(account_id, short_error(exc))
                    if progress:
                        progress.mark_error(account_id)
                    _log(f"premium stories {key} via {premium.id[:10]}…: {short_error(exc)}")
                    await human_delay(2, 5)
                    return
                try:
                    saved = 0
                    if stories:
                        async with archive_lock:
                            saved = archive.save_stories(key, stories)
                        if not saved and not archive.has_stories(key):
                            raise RuntimeError(f"Could not save downloaded stories for {key}")
                        total += saved
                        _log(f"premium stories {key}: saved {saved}/{len(stories)} (premium {premium.id[:10]}…)")
                    else:
                        _log(f"premium stories {key}: no stories available (premium {premium.id[:10]}…)")
                    result.add_ok(account_id)
                    result.details[account_id] = f"profile={key} saved={saved}"
                    if progress:
                        progress.mark_ok(account_id)
                except Exception as exc:
                    failed += 1
                    result.add_error(account_id, short_error(exc))
                    _log(f"premium stories {key}: archive error: {short_error(exc)}")
                    if progress:
                        progress.mark_error(account_id)
                await human_delay(1.5, 3.5)

        await asyncio.gather(
            *(worker(index, account_id, binding) for index, (account_id, binding) in enumerate(items))
        )
        _log(f"Premium stories done: saved={total} skipped={skipped} failed={failed}")
        return result

    async def apply_saved_profiles(
        self,
        accounts: list[AccountRecord],
        *,
        copy_name: bool = True,
        copy_bio: bool = True,
        copy_username: bool = True,
        copy_avatars: bool = True,
        copy_music: bool = False,
        copy_stories: bool = True,
        copy_birthday: bool = True,
        profile_keys: list[str] | None = None,
        require_stories: bool = False,
        parallelism: int = 1,
        max_stories_per_account: int | None = 5,
        progress: OperationProgress | None = None,
        on_log: LogFn | None = None,
    ) -> ActionResult:
        """Apply locally saved profiles to accounts.

        Account already bound to a profile -> complete the missing work (upload stories only).
        Free account -> take a free (unbound) saved profile and apply it fully, then bind.
        """
        result = ActionResult()
        enabled = [a for a in accounts if a.enabled]

        def _log(msg: str) -> None:
            log.info("%s", msg)
            if on_log:
                on_log(msg)

        if not enabled:
            _log("No enabled accounts in the current group/selection.")
            return result
        archive = ProfileArchive(self.config.data_dir)
        bindings = ProfileBindings(self.config)
        in_use = bindings.profile_keys_in_use()
        requested_keys = [str(key).lower().lstrip("@") for key in (profile_keys or []) if str(key).strip()]
        entries_by_key = {
            str(entry.get("username") or "").lower().lstrip("@"): entry
            for entry in archive.list_profiles()
        }
        candidates = (
            [entries_by_key[key] for key in requested_keys if key in entries_by_key]
            if requested_keys else list(entries_by_key.values())
        )
        free = []
        for entry in candidates:
            key = str(entry.get("username") or "").lower().lstrip("@")
            if not key or key in in_use or not archive.has(key):
                continue
            if require_stories and not archive.has_stories(key):
                continue
            free.append(entry)
        free_queue = list(free)
        sem = asyncio.Semaphore(max(1, parallelism))

        def _to_profile(data: dict[str, Any]) -> ProfileData:
            return ProfileData(
                user_id=int(data.get("user_id") or 0),
                username=data.get("username") or "",
                first_name=data.get("first_name") or "",
                last_name=data.get("last_name") or "",
                bio=data.get("bio") or "",
                photo_count=len(data.get("avatars") or []),
                stories_count=len(data.get("stories") or []),
                is_premium=bool(data.get("is_premium")),
                avatars=data.get("avatars") or [],
                stories=data.get("stories") or [],
                music_meta=data.get("music_meta") or {},
                birthday=data.get("birthday"),
            )

        async def worker(acc: AccountRecord) -> None:
            async with sem:
                await human_delay(max(self.config.min_action_delay, 5.0), max(self.config.max_action_delay, 12.0))
                binding = bindings.get(acc.id)
                if binding and archive.has(binding.get("profile_key", "")):
                    data = archive.load(binding["profile_key"])
                    uploaded_prev = int((acc.metadata or {}).get("stories_uploaded", 0) or 0)
                    stories = (data or {}).get("stories") or []
                    publish_limit = min(
                        len(stories),
                        max_stories_per_account if max_stories_per_account is not None else len(stories),
                    )
                    pending = stories[uploaded_prev:publish_limit]
                    if copy_stories and pending:
                        try:
                            async with create_client(self.config, acc) as client:
                                # Metadata can be missing when an earlier apply was
                                # interrupted after publishing.  The live story list
                                # is authoritative and also repairs old unpinned ones.
                                active_count = await client.pin_active_stories()
                                completed = min(publish_limit, max(uploaded_prev, active_count))
                                if completed != uploaded_prev:
                                    uploaded_prev = completed
                                    pending = stories[uploaded_prev:publish_limit]
                                    self.accounts.update_profile_metadata(
                                        acc.id, {"stories_uploaded": uploaded_prev},
                                    )
                                uploaded = 0
                                for story_info in pending:
                                    try:
                                        await client.upload_story(story_info)
                                        uploaded += 1
                                        delay = 6.0 if story_info.get("is_video") else 3.0
                                        await human_delay(delay, delay + 4.0)
                                    except Exception as exc:
                                        log.warning("%s story upload: %s", acc.id, short_error(exc))
                                try:
                                    self.accounts.update_profile_metadata(
                                        acc.id, {"stories_uploaded": uploaded_prev + uploaded},
                                    )
                                except Exception:
                                    pass
                            _log(f"{acc.id}: +{uploaded} stories (complete, profile {binding['profile_key']})")
                        except Exception as exc:
                            result.add_error(acc.id, short_error(exc))
                            if progress:
                                progress.mark_error(acc.id)
                            return
                    else:
                        _log(f"{acc.id}: profile {binding['profile_key']} already in use, stories up to date")
                    result.add_ok(acc.id)
                    if progress:
                        progress.mark_ok(acc.id)
                    return
                if not free_queue:
                    result.add_error(acc.id, "no free saved profile left")
                    if progress:
                        progress.mark_error(acc.id)
                    return
                entry = free_queue.pop(0)
                key = entry.get("username", "")
                data = archive.load(key)
                if not data:
                    result.add_error(acc.id, f"profile {key} missing")
                    if progress:
                        progress.mark_error(acc.id)
                    return
                source = _to_profile(data)
                try:
                    async with create_client(self.config, acc) as client:
                        uploaded = await _apply_profile(
                            client, acc, source,
                            clear_first=True,
                            copy_name=copy_name, copy_bio=copy_bio, copy_username=copy_username,
                            copy_avatars=copy_avatars, copy_music=copy_music,
                            copy_stories=copy_stories, copy_birthday=copy_birthday,
                            max_stories=max_stories_per_account,
                        )
                    try:
                        self.accounts.update_profile_metadata(
                            acc.id,
                            {
                                "profiled": True,
                                "last_known_bio": source.bio or "",
                                "profile_source_username": source.username or "",
                                "profiled_at": datetime.now(timezone.utc).isoformat(),
                                "stories_uploaded": uploaded,
                            },
                        )
                    except Exception:
                        pass
                    try:
                        bindings.bind(acc.id, source.username or key, user_id=source.user_id or None)
                    except Exception:
                        pass
                    result.add_ok(acc.id)
                    if progress:
                        progress.mark_ok(acc.id)
                    _log(f"{acc.id}: applied saved profile {key}")
                except Exception as exc:
                    err = short_error(exc)
                    if _is_frozen(exc):
                        result.add_error(acc.id, err + " [frozen, auto-deleted]")
                        log.warning("%s frozen, deleting", acc.id)
                        try:
                            self.accounts.delete_accounts([acc.id], delete_sessions=True)
                        except Exception:
                            pass
                    else:
                        result.add_error(acc.id, err)
                    if progress:
                        progress.mark_error(acc.id)

        await asyncio.gather(*(worker(acc) for acc in enabled))
        _log(result.summary("profile-apply-saved"))
        return result

    async def transfer_story_profiles(
        self,
        target_accounts: list[AccountRecord],
        *,
        count: int = 50,
        max_stories_per_account: int | None = 5,
        on_log: LogFn | None = None,
    ) -> ActionResult:
        """Move saved profiles that contain stories to a new account batch.

        A source is released only after the bound account has been completely
        cleared.  This makes an interrupted transfer resumable without assigning
        one archived profile to two accounts.
        """
        from modules.profile_customizer import ProfileCustomizer

        result = ActionResult()

        def _log(msg: str) -> None:
            log.info("%s", msg)
            if on_log:
                on_log(msg)

        targets = [account for account in target_accounts if account.enabled][:max(0, count)]
        if not targets:
            _log("No enabled transfer targets selected.")
            return result

        archive = ProfileArchive(self.config.data_dir)
        bindings = ProfileBindings(self.config)
        accounts_by_id = {account.id: account for account in self.accounts.list_accounts(enabled_only=True)}
        archive_index = {
            str(entry.get("username") or "").lower().lstrip("@"): entry
            for entry in archive.list_profiles()
        }
        all_bindings = bindings.all()
        binding_counts: dict[str, int] = {}
        for binding in all_bindings.values():
            key = str(binding.get("profile_key") or "").lower().lstrip("@")
            if key:
                binding_counts[key] = binding_counts.get(key, 0) + 1
        seen_keys: set[str] = set()
        duplicate_source_bindings = 0
        sources: list[tuple[AccountRecord, str, dict[str, Any]]] = []
        for owner_id, binding in all_bindings.items():
            key = str(binding.get("profile_key") or "").lower().lstrip("@")
            owner = accounts_by_id.get(owner_id)
            entry = archive_index.get(key)
            if binding_counts.get(key, 0) > 1:
                duplicate_source_bindings += 1
                continue
            if not key or key in seen_keys or owner is None or not archive.has_stories(key):
                continue
            seen_keys.add(key)
            sources.append((owner, key, entry or {}))
        sources.sort(key=lambda item: (-int(item[2].get("stories_count") or 0), str(item[2].get("saved_at") or ""), item[1]))
        sources = sources[:len(targets)]
        if duplicate_source_bindings:
            _log(f"Skipped {duplicate_source_bindings} duplicate source binding(s); they require separate cleanup.")
        if len(sources) < len(targets):
            _log(f"Only {len(sources)} unique bound profiles with stories are available for {len(targets)} targets.")
        if not sources:
            return result

        owners = [owner for owner, _key, _entry in sources]
        snapshot_path = ProfileSnapshotService(self.config).save_latest(owners, "transfer-story-profiles")
        _log(f"Saved source profile snapshot: {snapshot_path}")
        _log(f"Clearing {len(owners)} source account(s) before transfer.")
        clear_result = await ProfileCustomizer(self.config).clear_full_profile(owners)
        _log(clear_result.summary("clear-transfer-sources"))

        pairs: list[tuple[AccountRecord, str]] = []
        for (owner, key, _entry), target in zip(sources, targets):
            if clear_result.details.get(owner.id) != "ok":
                _log(f"{owner.id}: source {key} was not cleared; transfer skipped.")
                continue
            current = bindings.get(owner.id)
            if current and str(current.get("profile_key") or "").lower().lstrip("@") == key:
                bindings.unbind(owner.id)
            try:
                self.accounts.update_profile_metadata(owner.id, {"profiled": False})
            except Exception:
                pass
            pairs.append((target, key))
        if not pairs:
            _log("No cleared source profiles available to apply.")
            return result

        _log(f"Applying {len(pairs)} profiles with stories to the new account batch.")
        applied = await self.apply_saved_profiles(
            [target for target, _key in pairs],
            copy_stories=True,
            profile_keys=[key for _target, key in pairs],
            require_stories=True,
            parallelism=min(max(1, self.config.max_concurrency), len(pairs)),
            max_stories_per_account=max_stories_per_account,
            on_log=on_log,
        )
        return applied

    def _first_account(self) -> AccountRecord | None:
        for a in self.accounts.list_accounts(enabled_only=True):
            return a
        return None


async def _apply_profile(
    client,
    acc: AccountRecord,
    source: ProfileData,
    *,
    clear_first: bool = False,
    copy_name: bool = True,
    copy_bio: bool = True,
    copy_username: bool = True,
    copy_avatars: bool = True,
    copy_music: bool = False,
    copy_stories: bool = False,
    copy_birthday: bool = False,
    max_stories: int | None = 5,
) -> int:
    """Apply profile fields to an account; returns the number of stories uploaded."""
    uploaded = 0
    if clear_first:
        try:
            await client.clear_profile_photos()
            await human_delay(2.0, 4.0)
        except Exception as exc:
            log.warning("%s clear photos: %s", acc.id, short_error(exc))
    if copy_name:
        await client.update_profile(
            first_name=source.first_name or "User",
            last_name=source.last_name or "",
        )
        log.info("%s: name <- %s", acc.id, source.first_name)
        await human_delay(1.5, 3.0)
    if copy_bio:
        bio_text = source.bio or ""
        if len(bio_text) > 70:
            bio_text = bio_text[:67] + "..."
        await client.update_profile(bio=bio_text)
        await human_delay(1.0, 2.0)
    if copy_username and source.username:
        gen = await _generate_username(
            client, source.username, source.first_name, source.last_name
        )
        if gen:
            await client.set_username(gen)
            log.info("%s: @%s", acc.id, gen)
        else:
            log.warning("%s: no free username from %s", acc.id, source.username)
        await human_delay(1.0, 2.0)
    if copy_avatars and source.avatars:
        with tempfile.TemporaryDirectory() as d:
            paths: list[Path] = []
            for i, img in enumerate(source.avatars[:13]):
                p = Path(d) / f"av_{i}.jpg"
                p.write_bytes(img)
                paths.append(p)
            try:
                await client.set_profile_photos(paths)
                log.info("%s: %d avatars", acc.id, len(paths))
            except Exception as exc:
                log.warning("%s set photos: %s", acc.id, short_error(exc))
        await human_delay(2.0, 4.0)
    if copy_music and source.music_meta:
        try:
            await client.set_profile_music(source.music_meta)
            log.info("%s: music saved", acc.id)
        except Exception:
            pass
    if copy_birthday and source.birthday:
        try:
            year = source.birthday.get("year")
            await client.set_birthday(source.birthday["day"], source.birthday["month"], year=year)
            log.info(
                "%s: birthday %d.%d%s",
                acc.id,
                source.birthday["day"],
                source.birthday["month"],
                f".{year}" if year else "",
            )
            await human_delay(1.0, 2.0)
        except Exception as exc:
            log.warning("%s birthday: %s", acc.id, short_error(exc))
    if copy_stories and source.stories:
        uploaded = 0
        story_limit = max_stories if max_stories is not None else len(source.stories)
        for story_info in source.stories[:story_limit]:
            try:
                await client.upload_story(story_info)
                uploaded += 1
                delay = 6.0 if story_info.get("is_video") else 3.0
                await human_delay(delay, delay + 4.0)
            except Exception as exc:
                log.warning("%s story upload: %s", acc.id, short_error(exc))
        if uploaded:
            log.info("%s: %d stories uploaded", acc.id, uploaded)
    return uploaded


async def _generate_username(client, source_username: str, first_name: str, last_name: str) -> str | None:
    base = re.sub(r"[^a-zA-Z0-9_]", "", (source_username or "").strip().lstrip("@"))
    if not base:
        return None
    stem = re.sub(r"\d+$", "", base) or base
    initials = "".join(c[0].lower() for c in [first_name, last_name] if c and c[0].isalnum())
    candidates: list[str] = []
    for _ in range(12):
        n = random.randint(1, 9999)
        candidates.append(f"{stem}{n}")
        candidates.append(f"{stem}_{n}")
    for suffix in ("_real", "_off", "_x", "x", "tg", "_ok"):
        candidates.append(f"{stem}{suffix}{random.randint(1, 99)}")
    if initials:
        candidates.append(f"{stem}_{initials}{random.randint(1, 99)}")
    for _ in range(6):
        ch = random.choice(string.ascii_lowercase)
        pos = random.randint(1, max(1, len(stem) - 1))
        candidates.append(f"{stem[:pos]}{ch}{stem[pos:]}{random.randint(1, 999)}")
    seen: set[str] = set()
    for c in candidates:
        c = c[:32]
        if len(c) < 5 or c.lower() in seen:
            continue
        seen.add(c.lower())
        try:
            if await client.check_username_available(c):
                return c
        except Exception:
            continue
        await asyncio.sleep(0.12)
    return None


_CUSTOM_FONT_RANGES = [
    (0x1D400, 0x1D7FF), (0x1F100, 0x1F1FF),
    (0x1F300, 0x1F5FF), (0x1F600, 0x1F9FF), (0xFF00, 0xFFEF),
]


def _detect_custom_font(text: str) -> bool:
    for ch in text:
        cp = ord(ch)
        if cp < 0x400 or (0x400 <= cp <= 0x4FF):
            continue
        for lo, hi in _CUSTOM_FONT_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def _is_frozen(exc: Exception) -> bool:
    msg = str(exc).upper()
    return "FROZEN" in msg or "FROZEN_METHOD" in msg
