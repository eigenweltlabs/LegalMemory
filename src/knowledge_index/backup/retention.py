"""Which backups to keep, and — more carefully — which to delete.

Grandfather-father-son, the same shape restic and borg use: keep the newest backup of
each of the last N days, weeks, months and years, and delete what none of those rules
claims. Counting *distinct periods that contain a backup* rather than calendar periods is
what makes it behave for an appliance that was switched off: a stack that ran nightly for
a fortnight and then sat idle for three months still holds a weekly and a monthly copy,
instead of ageing everything out because the calendar moved on without it.

This module only decides. It returns a decision for every backup, with the reason it was
kept, and the caller performs the deletions — so the plan can be shown to an operator, and
tested, without anything being removed.

Two safety properties are deliberate and load-bearing:

* a backup id this build cannot parse is always kept. An unrecognized directory in the
  destination is something a human put there, or something a newer version wrote, and
  neither is ours to delete;
* ``min_keep`` newest backups are always kept, whatever the rules say. Retention that can
  empty a destination is not retention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from knowledge_index.backup.manifest import parse_backup_id
from knowledge_index.config import BackupRetentionConfig


@dataclass(frozen=True)
class RetentionDecision:
    backup_id: str
    keep: bool
    # Which rules claimed it: "newest", "daily", "weekly", "monthly", "yearly",
    # "unrecognized". Empty when it is being pruned.
    reasons: tuple[str, ...]
    taken_at: datetime | None

    def payload(self) -> dict:
        return {
            "backup_id": self.backup_id,
            "keep": self.keep,
            "reasons": list(self.reasons),
            "taken_at": self.taken_at.isoformat() if self.taken_at else None,
        }


def plan_retention(
    backup_ids: list[str], config: BackupRetentionConfig
) -> list[RetentionDecision]:
    """Decide the fate of every backup at a destination, newest first."""
    parsed: list[tuple[str, datetime | None]] = [
        (item, parse_backup_id(item)) for item in backup_ids
    ]
    # Newest first, and anything unparseable sorted to the end so it never occupies one of
    # the "newest N" slots that a real backup should hold.
    recognized = sorted(
        ((item, moment) for item, moment in parsed if moment is not None),
        key=lambda entry: entry[1],
        reverse=True,
    )
    unrecognized = sorted(item for item, moment in parsed if moment is None)

    reasons: dict[str, set[str]] = {item: set() for item, _ in parsed}
    for item in unrecognized:
        reasons[item].add("unrecognized")
    for item, _moment in recognized[: max(config.min_keep, 0)]:
        reasons[item].add("newest")

    for label, limit, bucket in (
        ("daily", config.daily, _day),
        ("weekly", config.weekly, _week),
        ("monthly", config.monthly, _month),
        ("yearly", config.yearly, _year),
    ):
        if limit <= 0:
            continue
        seen: list[str] = []
        for item, moment in recognized:
            key = bucket(moment)
            if key in seen:
                # Already have the newest backup from this period; the rest of the period
                # is only kept if some other rule wants it.
                continue
            seen.append(key)
            if len(seen) > limit:
                break
            reasons[item].add(label)

    decisions = [
        RetentionDecision(
            backup_id=item,
            keep=bool(reasons[item]),
            reasons=tuple(sorted(reasons[item])),
            taken_at=moment,
        )
        for item, moment in recognized
    ]
    decisions.extend(
        RetentionDecision(backup_id=item, keep=True, reasons=("unrecognized",), taken_at=None)
        for item in unrecognized
    )
    return decisions


def summarize(decisions: list[RetentionDecision]) -> dict:
    pruned = [item for item in decisions if not item.keep]
    return {
        "total": len(decisions),
        "kept": len(decisions) - len(pruned),
        "pruned": [item.backup_id for item in pruned],
        "decisions": [item.payload() for item in decisions],
    }


def _day(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


def _week(moment: datetime) -> str:
    year, week, _weekday = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _month(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


def _year(moment: datetime) -> str:
    return moment.strftime("%Y")
