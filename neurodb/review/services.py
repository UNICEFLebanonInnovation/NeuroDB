"""Running, storing, narrating and serving the daily review.

``run`` is the job: it runs the checks, diffs their findings against the previous review (new,
still open, resolved), ranks them, writes the narrative and records a ``SyncRun``. ``for_page``
shapes one review (today, yesterday, a date or the week) for the overview card.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import time
from collections import Counter
from dataclasses import asdict
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from neurodb.core.models import SyncRun
from neurodb.datamart.monitoring import norm

from . import checks
from .checks import GOOD, Draft
from .models import DailyReview, ReviewFinding

logger = logging.getLogger(__name__)

NEW, STILL_OPEN, RESOLVED = (
    ReviewFinding.State.NEW,
    ReviewFinding.State.STILL_OPEN,
    ReviewFinding.State.RESOLVED,
)
SEVERITY_ORDER = {
    ReviewFinding.Severity.CRITICAL: 0,
    ReviewFinding.Severity.WARNING: 1,
    ReviewFinding.Severity.INFO: 2,
    GOOD: 3,
}
STATE_ORDER = {NEW: 0, STILL_OPEN: 1, RESOLVED: 2}
# Checks that describe a change since the previous review: their findings are new by nature and
# never "resolved" when they stop appearing.
DELTA_CHECKS = frozenset({"new_off_track", "improvements"})
PAGE_FINDINGS = 12  # findings the overview card lists; the rest is a count
HISTORY_DAYS = 7
NARRATION_FINDINGS = 40  # the most important findings the model reads
NARRATION_MAX_OUTPUT_TOKENS = 600
NARRATION_INSTRUCTIONS = (
    "You write the morning note of the UNICEF Lebanon programme monitoring team. The input is the JSON "
    "of today's automated review: its findings (ranked, most important first) and the day's counts. "
    "Write 4 to 6 plain sentences in English for section chiefs, as one paragraph. Use only facts that "
    "are in the JSON; never invent or estimate a number, a date or a cause. Name programme documents "
    "and partners exactly as given. Say what changed since the previous review when the JSON says so. "
    "Findings are prompts to look, not verdicts: do not judge partners or staff, and give no advice "
    "beyond saying that something needs a person today. No headings, no lists, no markdown."
)
MODES = ("today", "yesterday", "week")


# ---------------------------------------------------------------------------------------- run
LOCK_ID = 7140429  # one daily review at a time; neurodb.integrations.background.DAILY_REVIEW_LOCK_ID
KEY_MAX, TITLE_MAX = 300, 300


class ReviewBusy(RuntimeError):
    """Another daily review is running."""


def _lock() -> bool:
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        return bool(cursor.fetchone()[0])


def _unlock() -> None:
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])


def run(
    date: datetime.date | None = None,
    triggered_by: str = "command",
    narrate: bool = True,
    today: datetime.date | None = None,
) -> DailyReview:
    """Run the review for ``date`` (default today) and store it, replacing that day's earlier one.

    ``today`` is the day the checks reason from (tests fix it); it defaults to ``date``. Everything is
    computed before anything is written: a day's earlier review is replaced only when the new one has
    succeeded, and a failed run keeps it. A failing check or narration never fails the run; they are
    recorded on the review. Raises :class:`ReviewBusy` when another review is running.
    """
    if not _lock():
        raise ReviewBusy("another daily review is running")
    try:
        return _run(date, triggered_by, narrate, today)
    finally:
        _unlock()


def _run(date, triggered_by, narrate, today) -> DailyReview:
    date = date or today or timezone.localdate()
    today = today or date
    started = time.monotonic()
    previous = (
        DailyReview.objects.filter(date__lt=date, status=DailyReview.Status.SUCCEEDED)
        .order_by("-date")
        .first()
    )
    sync = SyncRun.objects.create(job=SyncRun.Job.DAILY_REVIEW, target=str(date), triggered_by=triggered_by)
    review = DailyReview(date=date, status=DailyReview.Status.RUNNING, triggered_by=triggered_by)
    errors: dict[str, str] = {}
    findings: list[ReviewFinding] = []
    try:
        ctx = checks.Context(today=today, previous=previous)
        drafts, errors = checks.run_all(ctx)
        ranked = rank(diff(_unique(drafts), previous, failed_checks=set(errors)))
        stats = checks.snapshot(ctx)
        stats.update(
            counts={sev: sum(1 for d, _ in ranked if d.severity == sev) for sev in SEVERITY_ORDER},
            states={state: sum(1 for _, s in ranked if s == state) for state in STATE_ORDER},
            findings=len(ranked),
            check_errors=errors,
            previous_date=previous.date.isoformat() if previous else None,
        )
        review.stats = stats
        review.checks_run = len(checks.CHECKS)
        findings = [
            ReviewFinding(review=review, rank=position, state=state, **_fields(draft))
            for position, (draft, state) in enumerate(ranked)
        ]
        if narrate:
            review.summary = narrate_review(review, findings)
        else:
            review.summary = template_summary(findings, stats)
            review.narrated_by = DailyReview.TEMPLATE
        review.status = DailyReview.Status.SUCCEEDED
        sync.rows_in = stats["indicators"]
    except Exception as exc:
        logger.exception("daily review for %s failed", date)
        review.status = DailyReview.Status.FAILED
        review.error = f"{type(exc).__name__}: {exc}"[:10000]
        findings = []
    review.finished_at = timezone.now()
    review.duration_ms = int((time.monotonic() - started) * 1000)
    try:
        _store(review, findings)
    except Exception as exc:  # the write itself failed: record it on the run, keep the earlier review
        logger.exception("daily review for %s could not be stored", date)
        review.status = DailyReview.Status.FAILED
        review.error = f"{type(exc).__name__}: {exc}"[:10000]
    sync.rows_written = len(findings) if review.status == DailyReview.Status.SUCCEEDED else 0
    sync.rows_failed = len(errors)
    if review.status == DailyReview.Status.FAILED:
        status = SyncRun.Status.FAILED
    else:
        status = SyncRun.Status.PARTIAL if errors else SyncRun.Status.SUCCEEDED
    sync.finish(status, error=review.error, review_id=review.pk, check_errors=errors)
    logger.info(
        "daily review %s: %s, %s findings, narrated by %s in %s ms",
        date,
        review.status,
        sync.rows_written,
        review.narrated_by or "nobody",
        review.duration_ms,
    )
    return review


def _store(review: DailyReview, findings: list[ReviewFinding]) -> None:
    """Swap the day's review in one transaction. A failed run replaces only a failed or running
    earlier row; a succeeded review of the same day stays."""
    with transaction.atomic():
        existing = DailyReview.objects.select_for_update().filter(date=review.date).first()
        if (
            review.status == DailyReview.Status.FAILED
            and existing
            and existing.status == DailyReview.Status.SUCCEEDED
        ):
            return  # keep the good review; the SyncRun says the re-run failed
        if existing:
            existing.delete()
        review.save()
        for finding in findings:
            finding.review = review
        ReviewFinding.objects.bulk_create(findings)


def _fields(draft: Draft) -> dict[str, Any]:
    """A draft as ReviewFinding fields (the model calls the check ``check_id``), within the column
    sizes: an over-long key keeps a hash of its whole text so it stays stable and unique."""
    fields = asdict(draft)
    fields["check_id"] = fields.pop("check")[:40]
    key = fields["key"]
    if len(key) > KEY_MAX:
        digest = hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:16]
        fields["key"] = f"{key[: KEY_MAX - 17]}#{digest}"
    fields["title"] = fields["title"][:TITLE_MAX]
    fields["section"] = (fields["section"] or "")[:128]
    fields["url"] = (fields["url"] or "")[:500]
    return fields


def _unique(drafts: list[Draft]) -> list[Draft]:
    seen: set[str] = set()
    out = []
    for draft in drafts:
        key = _fields(draft)["key"]
        if key in seen:
            continue
        seen.add(key)
        out.append(draft)
    return out


def _as_draft(finding: ReviewFinding, **changes: Any) -> Draft:
    values = {
        "key": finding.key,
        "check": finding.check_id,
        "severity": finding.severity,
        "section": finding.section,
        "title": finding.title,
        "detail": finding.detail,
        "evidence": finding.evidence or {},
        "children": finding.children,
        "url": finding.url,
    }
    values.update(changes)
    return Draft(**values)


def diff(
    drafts: list[Draft], previous: DailyReview | None, failed_checks: set[str] | None = None
) -> list[tuple[Draft, str]]:
    """Each draft with its state against the previous review, plus a resolved finding (severity
    good) for every key open yesterday that no check reports today.

    Findings of a check that failed today are carried forward as still open (it could not look, so
    nothing is resolved). Findings of the checks that describe a change (``DELTA_CHECKS``) are new
    by nature: never still open, never resolved."""
    failed_checks = failed_checks or set()
    previous_findings = list(previous.findings.exclude(state=RESOLVED)) if previous else []
    known = {f.key for f in previous_findings}
    items = [
        (draft, NEW if draft.check in DELTA_CHECKS or _fields(draft)["key"] not in known else STILL_OPEN)
        for draft in drafts
    ]
    today_keys = {_fields(draft)["key"] for draft in drafts}
    for finding in previous_findings:
        if finding.key in today_keys or finding.severity == GOOD or finding.check_id in DELTA_CHECKS:
            continue
        if finding.check_id in failed_checks:
            items.append((_as_draft(finding), STILL_OPEN))
            continue
        items.append(
            (
                _as_draft(
                    finding,
                    severity=GOOD,
                    title=f"Resolved: {finding.title}",
                    detail=(
                        f"Reported at the review of {previous.date:%d %b %Y}; the check no longer finds it."
                    ),
                    evidence={
                        "read": "Confirm it was fixed rather than removed from the data (a report accepted, "
                        "an action point closed, a sync run again).",
                        "records": list((finding.evidence or {}).get("records", []))[:5],
                        "numbers": {},
                    },
                    children=None,
                ),
                RESOLVED,
            )
        )
    return items


def rank(items: list[tuple[Draft, str]]) -> list[tuple[Draft, str]]:
    """Critical first, then the most children behind target, then new before still open."""
    return sorted(
        items,
        key=lambda item: (
            SEVERITY_ORDER.get(item[0].severity, 9),
            -(item[0].children or 0),
            STATE_ORDER.get(item[1], 9),
            item[0].title,
        ),
    )


# --------------------------------------------------------------------------------- narration
def narrate(review: DailyReview, findings: list[ReviewFinding]) -> str:
    """The narrative: written by the assistant when it is configured, else from the template.
    Records who wrote it and the tokens used on ``review`` (not saved here)."""
    stats = review.stats or {}
    if not settings.AI_ASSISTANT_ENABLED:
        review.narrated_by = DailyReview.TEMPLATE
        return template_summary(findings, stats)
    try:
        from neurodb.assistant import agent

        response = agent.client().responses.create(
            model=settings.AI_ASSISTANT_MODEL,
            instructions=NARRATION_INSTRUCTIONS,
            input=json.dumps(narration_input(review, findings), default=str),
            max_output_tokens=NARRATION_MAX_OUTPUT_TOKENS,
            store=False,
            reasoning={"effort": "low"},
        )
        text = (getattr(response, "output_text", "") or "").strip()
        if not text:
            raise ValueError("the model returned no text")
        usage = getattr(response, "usage", None)
        review.model_input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        review.model_output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        review.narrated_by = settings.AI_ASSISTANT_MODEL
        return text
    except Exception:
        logger.exception("daily review %s: narration failed, using the template summary", review.date)
        review.narrated_by = DailyReview.TEMPLATE
        return template_summary(findings, stats)


narrate_review = narrate  # ``run`` takes a ``narrate`` flag; this keeps the function reachable there


def narration_input(review: DailyReview, findings: list[ReviewFinding]) -> dict[str, Any]:
    """What the model reads: the ranked findings (bounded) and the day's counts, nothing personal."""
    stats = review.stats or {}
    previous = _previous_of(review)
    return {
        "date": review.date.isoformat(),
        "previous_review": stats.get("previous_date"),
        "counts_by_severity": stats.get("counts", {}),
        "counts_by_state": stats.get("states", {}),
        "indicators": stats.get("indicators"),
        "programme_documents": stats.get("programme_documents"),
        "indicator_status_counts": stats.get("status_counts", {}),
        "on_track_percent": stats.get("on_track_percent"),
        "previous_on_track_percent": (previous.stats or {}).get("on_track_percent") if previous else None,
        "action_points_past_due": stats.get("action_points_past_due"),
        "tpm_completion": stats.get("tpm_completion"),
        "checks_that_failed": sorted((stats.get("check_errors") or {}).keys()),
        "findings": [
            {
                "severity": f.severity,
                "state": f.state,
                "section": f.section,
                "title": f.title,
                "detail": f.detail,
                "children_behind_target": f.children,
                "numbers": (f.evidence or {}).get("numbers", {}),
            }
            for f in findings[:NARRATION_FINDINGS]
        ],
    }


def template_summary(findings: list[ReviewFinding], stats: dict[str, Any]) -> str:
    """Deterministic sentences: counts by severity, the top three titles, the status share."""
    open_ = [f for f in findings if f.state != RESOLVED and f.severity != GOOD]
    counts = Counter(f.severity for f in open_)
    sentences = []
    if open_:
        sentences.append(
            _(
                "The daily review found %(n)s items to look at: %(critical)s critical, %(warning)s "
                "warnings and %(info)s to note."
            )
            % {
                "n": len(open_),
                "critical": counts.get(ReviewFinding.Severity.CRITICAL, 0),
                "warning": counts.get(ReviewFinding.Severity.WARNING, 0),
                "info": counts.get(ReviewFinding.Severity.INFO, 0),
            }
        )
        sentences.append(_("Start with: %(titles)s.") % {"titles": "; ".join(f.title for f in open_[:3])})
    else:
        sentences.append(_("The daily review found nothing that needs a person today."))
    states = stats.get("states") or {}
    if stats.get("previous_date"):
        sentences.append(
            _("Since the review of %(date)s: %(new)s new, %(still)s still open, %(resolved)s resolved.")
            % {
                "date": stats["previous_date"],
                "new": states.get(NEW, 0),
                "still": states.get(STILL_OPEN, 0),
                "resolved": states.get(RESOLVED, 0),
            }
        )
    status = stats.get("status_counts") or {}
    tracked = status.get("on_track", 0) + status.get("off_track", 0) + status.get("over_target", 0)
    if tracked:
        sentences.append(
            _("%(on)s of the %(tracked)s indicators with a target and a report are on track (%(pct)s %%).")
            % {
                "on": status.get("on_track", 0),
                "tracked": tracked,
                "pct": round(stats.get("on_track_percent") or 0),
            }
        )
    good = [f for f in findings if f.severity == GOOD and f.state != RESOLVED]
    if good:
        sentences.append(_("Good news: %(titles)s.") % {"titles": "; ".join(f.title for f in good[:2])})
    errors = stats.get("check_errors") or {}
    if errors:
        sentences.append(
            _("%(n)s checks could not run (%(names)s); their findings are missing today.")
            % {"n": len(errors), "names": ", ".join(sorted(errors))}
        )
    return " ".join(sentences)


# ------------------------------------------------------------------------------------- page
def _previous_of(review: DailyReview) -> DailyReview | None:
    return (
        DailyReview.objects.filter(date__lt=review.date, status=DailyReview.Status.SUCCEEDED)
        .order_by("-date")
        .first()
    )


def in_sections(section: str, sections: list[str]) -> bool:
    """A country-wide finding (no section) shows under every filter; a section matches whole or
    contained either way, case-insensitively, like the monitoring page's own section default."""
    if not sections or not section:
        return True
    mine = norm(section)
    for wanted in (norm(s) for s in sections):
        if wanted == mine or (len(wanted) >= 3 and (wanted in mine or mine in wanted)):
            return True
    return False


def _counts(findings: list[ReviewFinding]) -> dict[str, int]:
    counts = Counter(f.severity for f in findings)
    return {sev: counts.get(sev, 0) for sev in SEVERITY_ORDER}


def _history(reviews: list[DailyReview]) -> list[dict[str, Any]]:
    return [
        {
            "date": r.date,
            "counts": (r.stats or {}).get("counts") or {},
            "findings": (r.stats or {}).get("findings", 0),
            "on_track_percent": (r.stats or {}).get("on_track_percent"),
        }
        for r in sorted(reviews, key=lambda r: r.date)
    ]


def _changed(review: DailyReview, findings: list[ReviewFinding]) -> list[str]:
    states = Counter(f.state for f in findings)
    changed = [
        _("New: %(n)s") % {"n": states.get(NEW, 0)},
        _("Still open: %(n)s") % {"n": states.get(STILL_OPEN, 0)},
        _("Resolved: %(n)s") % {"n": states.get(RESOLVED, 0)},
    ]
    share = (review.stats or {}).get("on_track_percent")
    if share is not None:
        previous = _previous_of(review)
        before = (previous.stats or {}).get("on_track_percent") if previous else None
        text = _("Indicators on track, country: %(pct)s %%") % {"pct": round(share)}
        if before is not None:
            text += " " + _("(review of %(date)s: %(pct)s %%)") % {
                "date": previous.date.strftime("%d %b"),
                "pct": round(before),
            }
        changed.append(text)
    return changed


def _trend(reviews: list[DailyReview]) -> list[str]:
    """Week mode: how the counts moved from the oldest of the reviews to the latest."""
    if not reviews:
        return []
    ordered = sorted(reviews, key=lambda r: r.date)
    first, last = ordered[0], ordered[-1]
    counts_first = (first.stats or {}).get("counts") or {}
    counts_last = (last.stats or {}).get("counts") or {}
    trend = [
        _("Country-wide over these reviews.") if len(ordered) > 1 else _("Country-wide."),
        _("Critical: from %(a)s to %(b)s")
        % {"a": counts_first.get("critical", 0), "b": counts_last.get("critical", 0)},
        _("Warnings: from %(a)s to %(b)s")
        % {"a": counts_first.get("warning", 0), "b": counts_last.get("warning", 0)},
        _("Resolved over the week: %(n)s")
        % {"n": sum(((r.stats or {}).get("states") or {}).get(RESOLVED, 0) for r in ordered[1:])},
    ]
    share_first = (first.stats or {}).get("on_track_percent")
    share_last = (last.stats or {}).get("on_track_percent")
    if share_first is not None and share_last is not None:
        trend.append(
            _("Indicators on track: from %(a)s %% to %(b)s %%")
            % {"a": round(share_first), "b": round(share_last)}
        )
    return trend


def for_page(day: str | None, sections: list[str]) -> dict[str, Any] | None:
    """The review card of the overview: ``day`` is "today" (the latest review), "yesterday" (the one
    before it), "week" (the last seven, with the findings still open at the latest) or an ISO date.
    Findings are narrowed to ``sections`` (country-wide ones always show). None when no review exists."""
    reviews = list(
        DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED).order_by("-date")[:HISTORY_DAYS]
    )
    if not reviews:
        return None
    day = (day or "today").strip().lower()
    mode, review, missing = "today", reviews[0], ""
    if day == "yesterday":
        mode = "yesterday"
        review = reviews[1] if len(reviews) > 1 else None
        missing = _("There is no earlier review yet.")
    elif day == "week":
        mode = "week"
        window_start = reviews[0].date - datetime.timedelta(days=6)
        reviews = [r for r in reviews if r.date >= window_start]
    elif day != "today":
        try:
            wanted = datetime.date.fromisoformat(day)
        except ValueError:
            wanted = None
        if wanted is not None:
            mode = "date"
            review = DailyReview.objects.filter(date=wanted, status=DailyReview.Status.SUCCEEDED).first()
            missing = _("No review was run on %(date)s.") % {"date": wanted.isoformat()}
    page: dict[str, Any] = {
        "mode": mode,
        "modes": MODES,
        "review": review,
        "sections": sections,
        "history": _history(reviews),
        "missing": missing if review is None else "",
        "findings": [],
        "more": 0,
        "total": 0,
        "counts": _counts([]),
        "changed": [],
        "summary": "",
        "title": _("Daily review"),
        "when": "",
        "narrated_by": "",
        "narrated_label": "",
        "check_errors": {},
    }
    if review is None:
        return page
    findings = [f for f in review.findings.all() if in_sections(f.section, sections)]
    if mode == "week":
        findings = [f for f in findings if f.state != RESOLVED]
        page["title"] = _("Last 7 days, %(first)s to %(last)s") % {
            "first": page["history"][0]["date"].strftime("%d %b"),
            "last": review.date.strftime("%d %b %Y"),
        }
        page["changed"] = _trend(reviews)
    else:
        page["title"] = _("Daily review, %(date)s") % {"date": review.date.strftime("%A %d %B %Y")}
        page["changed"] = _changed(review, findings)
    finished = timezone.localtime(review.finished_at or review.created_at)
    page["when"] = _("Generated at %(time)s on %(date)s, %(n)s findings from %(checks)s checks") % {
        "time": finished.strftime("%H:%M"),
        "date": finished.strftime("%d %b %Y"),
        "n": len(findings),
        "checks": review.checks_run,
    }
    page.update(
        summary=review.summary,
        narrated_by=review.narrated_by,
        narrated_label=(
            _("Summary written from a template")
            if review.narrated_by in ("", DailyReview.TEMPLATE)
            else _("Summary written by the AI assistant from the findings")
        ),
        findings=findings[:PAGE_FINDINGS],
        more=max(len(findings) - PAGE_FINDINGS, 0),
        total=len(findings),
        counts=_counts(findings),
        check_errors=(review.stats or {}).get("check_errors") or {},
    )
    return page
