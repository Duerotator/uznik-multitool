"""Per-media publication receipts, independent of archive order or a counter."""
from __future__ import annotations

import secrets
from hashlib import sha256
from pathlib import Path
from typing import Any

from core.storage import read_json, update_json
from utils.rate_limit import human_delay


class StoryPublicationStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "story_publications.json"

    @staticmethod
    def digest(story: dict[str, Any]) -> str:
        payload = story.get("bytes")
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise ValueError("Story media is empty")
        return sha256(payload).hexdigest()

    def records(self, account_id: str, profile_key: str) -> dict[str, Any]:
        return read_json(self.path, {}).get(account_id, {}).get(profile_key, {})

    def has_receipts(self, account_id: str) -> bool:
        return bool(read_json(self.path, {}).get(account_id))

    def record(self, account_id: str, profile_key: str, digest: str, **fields: Any) -> dict[str, Any]:
        def mutate(data):
            entry = data.setdefault(account_id, {}).setdefault(profile_key, {}).setdefault(digest, {})
            entry.setdefault("random_id", secrets.randbelow(2**63 - 1) + 1)
            entry.update(fields)
            return data
        data = update_json(self.path, {}, mutate)
        return data[account_id][profile_key][digest]

    def accept_legacy(self, account_id: str, profile_key: str, stories: list[dict], count: int) -> None:
        for story in stories[:count]:
            self.record(account_id, profile_key, self.digest(story), status="legacy-confirmed", story_id=None)


async def publish_stories(
    client, account, profile_key: str, stories: list[dict[str, Any]],
    store: StoryPublicationStore, *, legacy_count: int = 0, accept_legacy: bool = False,
) -> int:
    if not await client.supports_stories():
        raise NotImplementedError("Story publication is not supported by this MTProto backend")
    records = store.records(account.id, profile_key)
    if legacy_count and not records:
        if not accept_legacy:
            raise RuntimeError("Legacy story counter has no media receipts. Confirm its order in Apply saved profiles before resuming.")
        store.accept_legacy(account.id, profile_key, stories, legacy_count)
    for story in stories:
        digest = store.digest(story)
        receipt = store.records(account.id, profile_key).get(digest, {})
        if receipt.get("status") in {"published", "legacy-confirmed"}:
            continue
        receipt = store.record(account.id, profile_key, digest, status="sending")
        try:
            # Persist the deduplication ID *before* sending. A retry after a lost
            # response uses the same Telegram random_id, never a new publication.
            story_id = await client.upload_story({**story, "random_id": receipt["random_id"]})
            if not isinstance(story_id, int) or story_id <= 0:
                raise RuntimeError("Telegram did not return a publication ID; receipt remains uncertain")
            store.record(account.id, profile_key, digest, status="published", story_id=story_id)
        except BaseException:
            store.record(account.id, profile_key, digest, status="uncertain")
            raise
        delay = 6.0 if story.get("is_video") else 3.0
        await human_delay(delay, delay + 4.0)
    records = store.records(account.id, profile_key)
    return sum(records.get(store.digest(story), {}).get("status") in {"published", "legacy-confirmed"}
               for story in {store.digest(item): item for item in stories}.values())
