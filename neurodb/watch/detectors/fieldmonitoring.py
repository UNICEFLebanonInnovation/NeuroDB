"""Field monitoring follow-up: a visit that found problems and has no action point after it.

- ``fm_follow_up``: a field monitoring visit of Monitoring insights (``fmm.Visit``) that was reported
  (status submitted or completed), whose worse of its overall rating and its HACT Q1 answer is off track
  or constrained (both the visit's: its worst record's, so one record off track is enough; the follow-up
  stays per visit, as its action points are), that ended between ``ScoreSetting.follow_up_days`` (14)
  and 120 days ago, and that no eTools action point is linked to, nor a NeuroDB action point follows up
  (one added by hand and not dropped, or one NeuroDB made that someone marked done:
  ``fmm.action_points.followed_up_q``). Warning;
  critical when off track and ended more than 30 days ago. Told to the sections of the visit (its eTools
  section names). It closes once an action point is linked to the visit or a NeuroDB one follows it up,
  or when its rating (and Q1) no longer say off track or constrained.

It reads the visits the Monitoring insights refresh builds from the eTools Datamart sync, and runs
only while both are fresh. Its records hold structured fields only (the visit, its date, its rating):
never a narrative, an answer or a person. It starts in trial, like every new check. With
``FMM_ENABLED`` off it finds nothing.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator

from django.apps import apps
from django.conf import settings

from neurodb.core.models import SyncRun

from ..models import WatchItem
from . import (
    CHANGED,
    CONCERN,
    CRITICAL,
    TRIAL,
    WARNING,
    Candidate,
    Close,
    Context,
    Detector,
    evidence,
    record,
    register,
)

DATAMART = SyncRun.Job.ETOOLS_DATAMART
REFRESH = SyncRun.Job.FMM_REFRESH
SOURCE = "eTools field monitoring"
KEY_PREFIX = "fm:followup:"
OLDEST_DAYS = 120  # a visit that ended longer ago than this is no longer followed
CRITICAL_AFTER_DAYS = 30  # off track and ended more than this many days ago: critical
CONCERNS = ("off_track", "constrained")
LABELS = {"off_track": "Off track", "constrained": "Constrained"}


def _day(value: datetime.date) -> str:
    return f"{value.day} {value:%b %Y}"


def _enabled() -> bool:
    return apps.is_installed("neurodb.fmm") and bool(getattr(settings, "FMM_ENABLED", False))


def worse(rating: str, hact_q1: str) -> str:
    """The worse of the visit's overall rating and its HACT Q1 (off track before constrained); "" when
    neither is off track or constrained."""
    found = {rating, hact_q1}
    for code in ("off_track", "constrained"):
        if code in found:
            return code
    return ""


def follow_up_key(visit_key: str) -> str:
    return f"{KEY_PREFIX}{visit_key}"


def _follow_up_days() -> int:
    from neurodb.fmm.models import ScoreSetting

    setting = ScoreSetting.objects.filter(pk=1).first() or ScoreSetting()
    return setting.follow_up_days


def _candidates_qs(ctx: Context):
    """The reported visits that ended between the follow-up delay and 120 days ago, rated (or Q1)
    off track or constrained, with no action point linked."""
    from django.db.models import Exists, OuterRef, Q

    from neurodb.fmm.action_points import followed_up_q
    from neurodb.fmm.models import LocalActionPoint, Visit, VisitActionPoint

    latest = ctx.today - datetime.timedelta(days=_follow_up_days())
    earliest = ctx.today - datetime.timedelta(days=OLDEST_DAYS)
    linked = VisitActionPoint.objects.filter(visit=OuterRef("pk"))
    local = LocalActionPoint.objects.filter(followed_up_q(), visit_key=OuterRef("key"))
    return (
        Visit.objects.filter(status_group="reported", end_date__gte=earliest, end_date__lte=latest)
        .filter(Q(rating__in=CONCERNS) | Q(hact_q1__in=CONCERNS))
        .exclude(Exists(linked))
        .exclude(Exists(local))
        .select_related("partner")
        .order_by("end_date", "key")
    )


def visits_without_follow_up(ctx: Context) -> Iterator[Candidate]:
    """The visits that found problems and have no follow-up action point yet (see the module notes)."""
    if not _enabled():
        return
    for visit in _candidates_qs(ctx):
        code = worse(visit.rating, visit.hact_q1)
        if code:
            yield _candidate(ctx, visit, code)


def _candidate(ctx: Context, visit, code: str) -> Candidate:
    partner = (visit.partner.short_name or visit.partner.name) if visit.partner else ""
    days = (ctx.today - visit.end_date).days
    label = LABELS[code]
    url = visit.get_absolute_url()
    who = f" ({partner})" if partner else ""
    from_q1 = code != visit.rating
    return Candidate(
        key=follow_up_key(visit.key),
        kind=CONCERN,
        severity=CRITICAL if code == "off_track" and days > CRITICAL_AFTER_DAYS else WARNING,
        title=(f"{visit.label}{who} rated {label} on {_day(visit.end_date)} has no follow-up action point"),
        detail=(
            f"The field monitoring visit ended on {_day(visit.end_date)} and its "
            f"{'HACT Q1 answer' if from_q1 else 'rating'} is {label}, but no eTools action point is linked "
            "to it. Raise an action point in eTools (or a NeuroDB action point on the visit), or note in "
            "Monitoring insights why none is needed."
        ),
        etools_sections=list(visit.section_names),
        entity_kind="fm_visit",
        entity_key=visit.key,
        url=url,
        evidence=evidence(
            ctx,
            SOURCE,
            REFRESH,
            [record(visit.label, visit.end_date, label, url)],
            days_open=days,
        ),
    )


def follow_up_resolved(ctx: Context, items: list[WatchItem]) -> dict[str, Close]:
    """Visits followed up: an action point is now linked to the visit (or a NeuroDB one follows it up),
    or its rating and Q1 no longer say off track or constrained. A visit gone from the data gets no
    reason: it is missed, then gone."""
    if not _enabled():
        return {}
    from neurodb.fmm.action_points import followed_up_keys
    from neurodb.fmm.models import Visit, VisitActionPoint

    keys = {item.key[len(KEY_PREFIX) :]: item.key for item in items if item.key.startswith(KEY_PREFIX)}
    if not keys:
        return {}
    visits = {v.key: v for v in Visit.objects.filter(key__in=list(keys))}
    linked = set(
        VisitActionPoint.objects.filter(visit__key__in=list(visits)).values_list("visit__key", flat=True)
    )
    followed = followed_up_keys(visits)
    closes: dict[str, Close] = {}
    for visit_key, item_key in keys.items():
        visit = visits.get(visit_key)
        if visit is None:
            continue
        if visit_key in linked:
            closes[item_key] = Close("an eTools action point is now linked to the visit")
        elif visit_key in followed:
            closes[item_key] = Close("a NeuroDB action point now follows the visit up")
        elif not worse(visit.rating, visit.hact_q1):
            closes[item_key] = Close(
                "the visit's rating no longer says off track or constrained", kind=CHANGED
            )
    return closes


FM_FOLLOW_UP = register(
    Detector(
        id="fm_follow_up",
        label="Field monitoring visits without a follow-up action point",
        run=visits_without_follow_up,
        resolved=follow_up_resolved,
        source_jobs=(DATAMART, REFRESH),
        default_mode=TRIAL,
    )
)
