"""Shared summaries for reviewing potentially destructive bulk operations."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Collection, Iterable, Mapping, Sequence

from core.models import AccountRecord


@dataclass(frozen=True)
class BulkOperationPreview:
    action: str
    group: str
    selected: int
    enabled: int
    sleeping: int
    without_proxy: int

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)

    def format_text(self) -> str:
        return "\n".join((
            f"Action: {self.action}",
            f"Group: {self.group}",
            f"Selected: {self.selected}",
            f"Enabled: {self.enabled}",
            f"Sleeping: {self.sleeping}",
            f"Without VPN: {self.without_proxy}",
        ))


def build_bulk_operation_preview(
    action: str,
    group: str,
    accounts: Iterable[AccountRecord],
    *,
    sleeping_ids: Collection[str] = (),
) -> BulkOperationPreview:
    items = list(accounts)
    sleeping = set(sleeping_ids)
    return BulkOperationPreview(
        action=action,
        group=group,
        selected=len(items),
        enabled=sum(1 for account in items if account.enabled),
        sleeping=sum(1 for account in items if account.id in sleeping),
        without_proxy=sum(1 for account in items if not account.proxy),
    )


def balance_proxy_assignments(
    target_ids: Sequence[str],
    available_urls: Sequence[str],
    current_by_account: Mapping[str, str | None],
) -> list[tuple[str, str]]:
    """Assign every target to the currently least-used available VPN exit.

    Existing assignments belonging to the target set are intentionally removed
    from the starting counters: reassignment replaces those values, so counting
    both the old and new value creates an artificial pool limit.
    """
    targets = list(dict.fromkeys(target_ids))
    urls = list(dict.fromkeys(url for url in available_urls if url))
    if not targets or not urls:
        return []

    target_set = set(targets)
    usage = Counter(
        url
        for account_id, url in current_by_account.items()
        if account_id not in target_set and url in urls
    )
    order = {url: index for index, url in enumerate(urls)}
    assignments: list[tuple[str, str]] = []
    for account_id in targets:
        url = min(urls, key=lambda item: (usage[item], order[item]))
        assignments.append((account_id, url))
        usage[url] += 1
    return assignments
