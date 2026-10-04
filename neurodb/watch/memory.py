"""NeuroDB Watch's memory: what the checks found, remembered from one pass to the next.

:func:`run` runs the checks (:func:`neurodb.watch.detectors.collect`) and :func:`apply` writes what
they found into ``WatchItem`` rows, keyed on lasting identifiers, by these rules:

- **Seen again**: the item is updated (``last_seen_on``, ``source_mark``). ``changed_on`` moves, and
  a dated line is added to its story, only when its severity, due date or state changes ("Got
  worse: was warning, now critical", "Due date moved to 30 Nov 2026").
- **Positive evidence** from the check (a report submitted) closes the item at once: "closed".
- **Missed**: an item its check no longer finds counts a miss only in the morning pass, only when
  the check ran on a fresh source without an error, and only when the check's source mark is newer
  than the item's (data newer than what showed it). Two misses make it "gone" ("no longer in
  eTools"), never "done". A quick pass never counts a miss.
- **Back again** within 14 days of closing: the same row reopens with its first day kept, and it is
  not new. Later, it starts a new episode (new, first seen today).
- **Marked wrong** by people (state "wrong"): it stays hidden while its evidence is the same, and
  opens again when the records or numbers change.
- A check that is off, stale, stopped or failing changes nothing: its items are carried forward.

Nothing else is written here: who is told what is :mod:`neurodb.watch.routing`'s.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from neurodb.core.models import SyncRun

from . import detectors
from .detectors import ALIVE, SEVERITY_RANK, Attach, Candidate, Close, Context, Detector, Result, newer
from .models import RECORDS_MAX, WatchItem, fit_key, hash_evidence

MISSES_TO_GONE = 2  # fresh morning passes in a row without the item
REOPEN_DAYS = 14  # an item back within this many days of closing reopens as the same episode
State = WatchItem.State
SEVERITY_WORDS = {"critical": "critical", "warning": "warning", "info": "to note"}
# Evidence numbers that change every day by themselves: left out of the fingerprint (the last three
# are the daily review's, kept under its names on the findings it imports)
COUNTING_NUMBERS = frozenset(
    {
        "days_left",
        "days_open",
        "days_overdue",
        "days_since",
        "days_ago",
        "days_since_end",
        "days_since_start",
        "elapsed_percent",
    }
)

# The text columns of WatchItem, cut to their size
_SIZES = {
    "title": 300,
    "url": 500,
    "entity_kind": 24,
    "entity_key": 120,
    "review_key": 300,
    "assignment_status": 16,
}


@dataclass
class Outcome:
    """What one pass did to the memory: the keys per event, and per check its counts (for
    ``SyncRun.details``). ``reopened`` holds items back within 14 days and items marked wrong whose
    evidence changed; ``worse`` the items whose severity rose (also when reopened). An item still
    marked wrong is in none of them."""

    mode: str
    new: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)
    worse: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)  # due date moved, or less urgent
    closed: list[str] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    attached: list[str] = field(default_factory=list)
    seen: int = 0
    detectors: dict[str, dict[str, Any]] = field(default_factory=dict)
    stale_sources: list[str] = field(default_factory=list)

    @property
    def errors(self) -> dict[str, str]:
        return {check: stats["error"] for check, stats in self.detectors.items() if stats["error"]}

    def count(self, check: str, event: str) -> None:
        self.detectors[check][event] += 1

    def details(self) -> dict[str, Any]:
        """The part of ``SyncRun.details`` the memory step fills."""
        return {
            "detectors": self.detectors,
            "stale_sources": self.stale_sources,
            "items": {
                "new": len(self.new),
                "reopened": len(self.reopened),
                "worse": len(self.worse),
                "changed": len(self.changed),
                "closed": len(self.closed),
                "gone": len(self.gone),
                "missed": len(self.missed),
                "attached": len(self.attached),
                "seen": self.seen,
            },
        }


def run(ctx: Context, checks: list[Detector] | None = None, sync_run: SyncRun | None = None) -> Outcome:
    """Run the checks (default: every registered one) and remember what they found. With
    ``sync_run``, its details get the counts and each check's error."""
    outcome = apply(ctx, detectors.collect(ctx, checks))
    if sync_run is not None:
        sync_run.details.update(outcome.details())
        sync_run.save(update_fields=["details"])
    return outcome


def apply(ctx: Context, results: list[Result]) -> Outcome:
    """Write the checks' results into the memory, in one transaction (see the module's rules)."""
    outcome = Outcome(mode=ctx.mode)
    for result in results:
        outcome.detectors[result.detector.id] = {
            "mode": result.mode,
            "candidates": len(result.candidates),
            "new": 0,
            "closed": 0,
            "gone": 0,
            "skipped": result.skipped + (f": {', '.join(result.stale_jobs)}" if result.stale_jobs else ""),
            "error": result.error,
        }
        outcome.stale_sources += [job for job in result.stale_jobs if job not in outcome.stale_sources]
    ran = [result for result in results if result.ran]
    with transaction.atomic():
        seen = _remember(ctx, ran, outcome)
        for result in ran:
            for attach in result.attach:
                _attach(ctx, attach, outcome)
        for result in ran:
            _close_or_miss(ctx, result, seen, outcome)
    return outcome


# ---------------------------------------------------------------------------- seen today
def _remember(ctx: Context, results: list[Result], outcome: Outcome) -> set[str]:
    """Create or update an item per candidate; the keys seen in this pass."""
    keys = [fit_key(candidate.key) for result in results for candidate in result.candidates]
    existing = WatchItem.objects.in_bulk(keys, field_name="key")
    seen: set[str] = set()
    for result in results:
        for candidate in result.candidates:
            key = fit_key(candidate.key)
            if key in seen:
                continue  # two checks (or one, twice) found the same thing: the first one counts
            seen.add(key)
            values = _values(ctx, candidate)
            item = existing.get(key)
            if item is None:
                item = _create(ctx, key, values, result.mark)
                outcome.new.append(key)
                outcome.count(result.detector.id, "new")
                continue
            _update(ctx, item, values, result.mark, outcome, result.detector.id)
    outcome.seen = len(seen)
    return seen


def _values(ctx: Context, candidate: Candidate) -> dict[str, Any]:
    """The item's fields from a candidate, within the column sizes. The evidence keeps at most 10
    records and the milestones; the sections come from the confirmed eTools names when the check
    gave none."""
    evidence = dict(candidate.evidence)
    evidence["records"] = list(evidence.get("records") or [])[:RECORDS_MAX]
    evidence.setdefault("numbers", {})
    if candidate.milestones:
        evidence["milestones"] = list(candidate.milestones)
    else:
        evidence.pop("milestones", None)
    etools_sections = [str(name)[:300] for name in candidate.etools_sections if str(name or "").strip()]
    section_ids = sorted({int(pk) for pk in candidate.section_ids})
    if not section_ids and etools_sections:
        section_ids = _section_ids(ctx, etools_sections)
    values = {
        "detector": candidate.detector[:40],
        "kind": candidate.kind,
        "severity": candidate.severity,
        "confidence": candidate.confidence,
        "title": candidate.title,
        "detail": candidate.detail or "",
        "url": candidate.url or "",
        "due_date": candidate.due_date,
        "etools_sections": etools_sections,
        "section_ids": section_ids,
        "scope": candidate.scope,
        "entity_kind": candidate.entity_kind or "",
        "entity_key": str(candidate.entity_key or ""),
        "review_key": candidate.review_key or "",
        "has_owner": bool(candidate.has_owner),
        "assignment_status": candidate.assignment_status or "",
        "evidence": evidence,
        "evidence_hash": fingerprint(evidence),
    }
    for name, size in _SIZES.items():
        values[name] = values[name][:size]
    return values


def _section_ids(ctx: Context, names: list[str]) -> list[int]:
    """The confirmed NeuroDB sections of eTools section names (none for a name not confirmed: the
    item then goes to the administrators, never to every section)."""
    from . import sections

    mapping = ctx.memo("section_map", sections.confirmed_map)
    return sections.resolve_names(names, mapping)[0]


def fingerprint(evidence: dict) -> str:
    """What makes evidence different: its records and numbers, not when it was synced nor the day
    counts that move by themselves (``COUNTING_NUMBERS``). An item marked wrong comes back when this
    changes."""
    evidence = evidence or {}
    numbers = {k: v for k, v in (evidence.get("numbers") or {}).items() if k not in COUNTING_NUMBERS}
    return hash_evidence({"records": evidence.get("records") or [], "numbers": numbers})


def _create(ctx: Context, key: str, values: dict[str, Any], mark: str) -> WatchItem:
    item = WatchItem(
        key=key,
        state=State.OPEN,
        first_seen_on=ctx.today,
        last_seen_on=ctx.today,
        changed_on=ctx.today,
        source_mark=mark,
        **values,
    )
    item.add_story("First noticed", ctx.today)
    item.save()
    return item


def _update(
    ctx: Context, item: WatchItem, values: dict[str, Any], mark: str, outcome: Outcome, check: str
) -> None:
    today = ctx.today
    events: list[str] = []
    changed = False
    if item.state == State.WRONG:
        if values["evidence_hash"] != item.evidence_hash:
            item.state = State.OPEN
            item.add_story("The evidence changed since it was marked wrong: open again", today)
            events.append("reopened")
            changed = True
    elif item.state in (State.CLOSED, State.GONE):
        ended = item.closed_on or item.last_seen_on
        how = "closed" if item.state == State.CLOSED else "went"
        if ended and (today - ended).days <= REOPEN_DAYS:
            item.add_story(f"Back again (it {how} on {_day(ended)})", today)
            events.append("reopened")
        else:
            item.first_seen_on = today
            item.add_story("Noticed again", today)
            events.append("new")
            outcome.count(check, "new")
        item.state = State.OPEN
        item.closed_on = None
        item.close_reason = ""
        changed = True
    if values["severity"] != item.severity:
        was, now = SEVERITY_WORDS[item.severity], SEVERITY_WORDS[values["severity"]]
        if SEVERITY_RANK[values["severity"]] > SEVERITY_RANK.get(item.severity, 0):
            item.add_story(f"Got worse: was {was}, now {now}", today)
            events.append("worse")
        else:
            item.add_story(f"Less urgent: was {was}, now {now}", today)
            events.append("changed")
        changed = True
    if values["due_date"] != item.due_date:
        if values["due_date"] is None:
            item.add_story("Due date removed", today)
        else:
            item.add_story(f"Due date moved to {_day(values['due_date'])}", today)
        events.append("changed")
        changed = True
    if not values["review_key"]:
        values["review_key"] = item.review_key  # a hand-over noted earlier stays
    for name, value in values.items():
        setattr(item, name, value)
    if changed:
        item.changed_on = today
    item.last_seen_on = today
    item.source_mark = max(item.source_mark or "", mark or "")
    item.missed_runs = 0
    item.save()
    if item.state == State.WRONG:
        return  # still hidden: nothing to tell
    for event in dict.fromkeys(events):
        if event == "changed" and {"new", "reopened", "worse"} & set(events):
            continue
        getattr(outcome, event).append(item.key)


def _attach(ctx: Context, attach: Attach, outcome: Outcome) -> None:
    """Note the daily review finding on an item the watch already follows (once)."""
    item = WatchItem.objects.filter(key=fit_key(attach.key), state__in=ALIVE).first()
    review_key = attach.review_key[:300]
    if item is None or item.review_key == review_key:
        return
    item.review_key = review_key
    item.add_story("The daily review now flags it too", ctx.today)
    item.save(update_fields=["review_key", "story"])
    outcome.attached.append(item.key)


# ---------------------------------------------------------------------------- not seen today
def _close_or_miss(ctx: Context, result: Result, seen: set[str], outcome: Outcome) -> None:
    """The check's items it did not find today: closed on its positive evidence, else missed (in
    the morning pass, on data newer than the item's)."""
    check = result.detector.id
    for item in WatchItem.objects.filter(detector=check, state__in=ALIVE).order_by("pk"):
        if item.key in seen:
            continue
        close = result.closes.get(item.key)
        if close is not None:
            _close(ctx, item, close)
            outcome.closed.append(item.key)
            outcome.count(check, "closed")
        elif ctx.daily and item.state == State.OPEN and newer(result.mark, item.source_mark):
            gone = _miss(ctx, item, result.mark)
            outcome.missed.append(item.key)
            if gone:
                outcome.gone.append(item.key)
                outcome.count(check, "gone")


def _close(ctx: Context, item: WatchItem, close: Close) -> None:
    reason = (close.reason or "closed").strip()
    item.state = State.CLOSED
    item.closed_on = ctx.today
    item.changed_on = ctx.today
    item.close_reason = reason[:300]
    item.missed_runs = 0
    if close.review_key:
        item.review_key = close.review_key[:300]
    item.add_story(f"Closed: {reason}", ctx.today)
    item.save()


def _miss(ctx: Context, item: WatchItem, mark: str) -> bool:
    """Count one miss against data newer than the item's (the mark moves on, so the same data never
    counts twice). True when it is now gone."""
    item.missed_runs += 1
    item.source_mark = mark
    if item.missed_runs < MISSES_TO_GONE:
        item.save(update_fields=["missed_runs", "source_mark"])
        return False
    source = (item.evidence or {}).get("source") or "the data"
    item.state = State.GONE
    item.closed_on = ctx.today
    item.changed_on = ctx.today
    item.close_reason = f"No longer in {source}"[:300]
    item.add_story(f"No longer in {source}", ctx.today)
    item.save()
    return True


def _day(value: datetime.date) -> str:
    return f"{value.day} {value:%b %Y}"
