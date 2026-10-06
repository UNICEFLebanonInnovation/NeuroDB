"""The action points module of Monitoring insights (FMS §10), read by the action points page
(``reports:action_points``, through lazy imports) and the visit page.

- **eTools action points** (``datamart.ActionPoint``): how a field monitoring one is linked to its visit
  (:func:`visit_links`: the link confidence, High when matched by the visit's activity id, Medium by its
  reference, Unmatched when no visit was found), what the action taken says (:func:`action_taken`, read
  key-tolerant from the record), the AI's verdict on a completed one (``ActionPointReview``, shown only
  while it is up to date: :func:`current_reviews`) and the PME verifications (``ActionPointVerification``,
  the latest one counts: :func:`latest_verifications`), with who may verify (:func:`can_verify`).
- **NeuroDB action points** (``LocalActionPoint``): kept here only, never pushed to eTools. Added by hand
  (:func:`can_add_local`), or made by the refresh (:func:`create_automatic`) when a scored visit's quality
  is Low and the AI flagged its action points (R7, R8 or R32). Listed with their own filters
  (:func:`local_points`, the programme read from keywords as FMS does).
- **Follow-up**: a visit counts as followed up by a NeuroDB action point added by hand (not dropped), or
  by one NeuroDB made that someone marked done (:func:`followed_up_keys`); the Monitoring insights
  follow-up block and NeuroDB Watch read it.

Who an action point is assigned to is shown on staff pages only: it is never sent to the AI.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable
from typing import Any

from django.db.models import Q, QuerySet
from django.utils import timezone

from neurodb.datamart.models import ActionPoint
from neurodb.datamart.services import ap_action_taken, ap_completed_q

from .models import (
    ActionPointReview,
    ActionPointVerification,
    LocalActionPoint,
    ScoreSetting,
    Visit,
    VisitActionPoint,
    VisitRuleResult,
)

CONFIDENCE = {"related_id": "high", "reference": "medium", "reference_number": "medium"}
CONFIDENCE_LABELS = {"high": "High", "medium": "Medium", "unmatched": "Unmatched"}
CONFIDENCE_HELP = {
    "high": "Matched to the visit by its eTools activity id",
    "medium": "Matched to the visit by its reference",
    "unmatched": "A field monitoring action point no visit of Monitoring insights matches",
}
VERIFICATION_FILTERS = ("verified", "rejected", "pending", "none")
AUTO_RULES = ("R7", "R8", "R32")  # the AI's action point flags: a Low visit with one gets a reminder
HIGH_BELOW = 30  # an automatic action point is High below this quality score, else Medium
HIGH_DAYS, MEDIUM_DAYS = 5, 10  # working days (Monday to Friday) to its due date
NOTE_CHARS = 500
TITLE_CHARS = 200
DESCRIPTION_CHARS = 2000
# FMS §10.5: the programme of a NeuroDB action point, read from its title and description
PROGRAMMES: dict[str, tuple[str, tuple[str, ...]]] = {
    "health": ("Health", ("health", "phc", "clinic", "vaccin", "immuni", "hospital", "medical", "disease")),
    "nutrition": (
        "Nutrition",
        ("nutrition", "malnutrition", "breastfeeding", "micronutrient", "muac", "iycf"),
    ),
    "wash": ("WASH", ("wash", "water", "sanitation", "hygiene", "latrine", "sewage")),
    "education": ("Education", ("education", "school", "learning", "teacher", "student", "classroom", "ecd")),
    "child_protection": (
        "Child Protection",
        ("child protection", "gbv", "case management", "violence", "child labour", "child labor", "psea"),
    ),
    "cash": ("Cash", ("cash", "transfer", "voucher", "payment")),
    "mhpss": ("MHPSS", ("mhpss", "psychosocial", "mental health", "pss")),
    "social_policy": ("Social Policy", ("social policy", "social protection", "grant", "allowance")),
    "sbc": ("SBC", ("sbc", "behaviour", "behavior", "communication", "community engagement", "awareness")),
}


# ------------------------------------------------------------------------------------------ eTools points
def action_taken(data: Any) -> str:
    """The action taken written when the action point was closed (``datamart.services.ap_action_taken``:
    the first of its keys that holds a text), on one line; "" when none."""
    return ap_action_taken(data)


def is_completed(status: str) -> bool:
    return (status or "").strip().lower() in ActionPoint.COMPLETED_STATUSES


def completed_q(prefix: str = "") -> Q:
    """The completed action points (completed, closed or resolved, whatever the case)."""
    return ap_completed_q(prefix)


def visit_links(point_ids: Iterable[int]) -> dict[int, dict[str, str]]:
    """``{action point id: {"key", "label", "confidence"}}`` of the field monitoring action points of
    ``point_ids`` linked to a visit; one linked to two visits gives the first by key."""
    out: dict[int, dict[str, str]] = {}
    rows = (
        VisitActionPoint.objects.filter(action_point_id__in=list(point_ids))
        .order_by("action_point_id", "visit__key")
        .values_list("action_point_id", "visit__key", "visit__label", "matched_by")
    )
    for pk, key, label, how in rows:
        out.setdefault(pk, {"key": key, "label": label or key, "confidence": CONFIDENCE.get(how, "medium")})
    return out


def confidence_q(level: str) -> Q | None:
    """The field monitoring action points of one link confidence (high, medium or unmatched)."""
    fm = Q(related_module__iexact="fm")
    if level == "high":
        return fm & Q(
            pk__in=VisitActionPoint.objects.filter(matched_by="related_id").values("action_point_id")
        )
    if level == "medium":
        return fm & Q(
            pk__in=VisitActionPoint.objects.filter(matched_by__in=("reference", "reference_number")).values(
                "action_point_id"
            )
        )
    if level == "unmatched":
        return fm & ~Q(pk__in=VisitActionPoint.objects.values("action_point_id"))
    return None


def current_reviews(points: Iterable[Any]) -> dict[int, ActionPointReview]:
    """``{datamart id: its AI review}`` of the completed ``points`` (``datamart.ActionPoint`` rows) whose
    review is up to date: made with the published instructions, on the description and action taken they
    hold now. An out-of-date review is never shown."""
    from .ai import ap_review

    points = [p for p in points if is_completed(p.status)]
    if not points:
        return {}
    prompt = ap_review.current_prompt_hash()
    if prompt is None:
        return {}
    found = {
        r.datamart_id: r
        for r in ActionPointReview.objects.filter(
            datamart_id__in=[p.datamart_id for p in points], prompt_hash=prompt
        )
    }
    return {
        p.datamart_id: found[p.datamart_id]
        for p in points
        if p.datamart_id in found
        and found[p.datamart_id].input_hash == ap_review.input_hash(p.description, action_taken(p.data))
    }


def verdict_q(verdict: str) -> Q | None:
    """The action points whose current AI verdict is ``verdict`` (a ``Verdict`` value), or that are
    completed with no current verdict (``"none"``)."""
    from .ai import ap_review

    prompt = ap_review.current_prompt_hash()
    reviewed = ActionPointReview.objects.filter(prompt_hash=prompt or "-")
    if verdict == "none":
        return completed_q() & ~Q(datamart_id__in=reviewed.values("datamart_id"))
    if verdict in ActionPointReview.Verdict.values:
        return Q(datamart_id__in=reviewed.filter(verdict=verdict).values("datamart_id"))
    return None


def latest_verifications(datamart_ids: Iterable[int]) -> dict[int, ActionPointVerification]:
    """``{datamart id: its latest PME verification}``."""
    out: dict[int, ActionPointVerification] = {}
    for row in ActionPointVerification.objects.filter(datamart_id__in=list(datamart_ids)).order_by(
        "datamart_id", "-created_at", "-pk"
    ):
        out.setdefault(row.datamart_id, row)
    return out


def verification_history(datamart_id: int) -> list[ActionPointVerification]:
    return list(
        ActionPointVerification.objects.filter(datamart_id=datamart_id)
        .select_related("verified_by")
        .order_by("-created_at", "-pk")
    )


def with_verification(points: QuerySet) -> QuerySet:
    """``points`` with ``pme_state``: the state of each one's latest verification (None: never)."""
    from django.db.models import OuterRef, Subquery

    latest = (
        ActionPointVerification.objects.filter(datamart_id=OuterRef("datamart_id"))
        .order_by("-created_at", "-pk")
        .values("state")[:1]
    )
    return points.annotate(pme_state=Subquery(latest))


def verification_filter(points: QuerySet, state: str) -> QuerySet:
    """``points`` whose latest verification is ``state`` (verified, rejected, pending), or never verified
    (``"none"``)."""
    if state not in VERIFICATION_FILTERS:
        return points
    points = with_verification(points)
    if state == "none":
        return points.filter(pme_state__isnull=True)
    return points.filter(pme_state=state)


def can_verify(user, point) -> bool:
    """May record a PME verification of ``point`` (a ``datamart.ActionPoint``): an Administrator, or a
    Section editor of the point's section (its eTools section name, as NeuroDB matched it)."""
    from neurodb.accounts.roles import can_edit_section
    from neurodb.watch.sections import resolve_names

    from .access import is_admin

    if user is None or not user.is_authenticated:
        return False
    if is_admin(user):
        return True
    ids, _unmapped = resolve_names([point.section])
    return any(can_edit_section(user, sid) for sid in ids)


def verify(point, user, state: str, note: str) -> ActionPointVerification:
    """Record a PME verification of ``point`` by ``user`` (who and when kept automatically)."""
    if state not in ActionPointVerification.State.values:
        raise ValueError(f"unknown verification {state!r}")
    name = (user.get_full_name() or user.get_username()) if user else ""
    return ActionPointVerification.objects.create(
        datamart_id=point.datamart_id,
        state=state,
        note=" ".join((note or "").split())[:NOTE_CHARS],
        verified_by=user,
        verified_by_name=name[:150],
    )


# ------------------------------------------------------------------------------------------ NeuroDB points
def can_add_local(user) -> bool:
    """May add a NeuroDB action point: an Administrator or a Section editor."""
    from neurodb.accounts.roles import SECTION_EDITOR, role_of

    from .access import is_admin

    if user is None or not user.is_authenticated:
        return False
    return is_admin(user) or role_of(user) == SECTION_EDITOR


def can_change_local(user, point: LocalActionPoint, visit: Visit | None = None) -> bool:
    """May change a NeuroDB action point's status: an Administrator, the person who added it, or a
    Section editor (of one of the sections of its visit, when it has one)."""
    from neurodb.accounts.roles import SECTION_EDITOR, can_edit_section, role_of

    from .access import is_admin

    if user is None or not user.is_authenticated:
        return False
    if is_admin(user) or (point.created_by_id and point.created_by_id == user.pk):
        return True
    if role_of(user) != SECTION_EDITOR:
        return False
    if not point.visit_key:
        return True
    if visit is None:
        visit = Visit.objects.filter(key=point.visit_key).only("section_ids").first()
    return visit is None or any(can_edit_section(user, sid) for sid in visit.section_ids or ())


def find_visit(text: str) -> Visit | None:
    """The visit written as its key, its id ("1722", "Visit 1722", "#1722") or its reference."""
    text = " ".join(str(text or "").split())
    if not text:
        return None
    bare = re.sub(r"^(visit\s+|#)", "", text, flags=re.IGNORECASE).strip()[:100]
    found = Visit.objects.filter(key=bare[:40]).first()
    if found is None and bare.isdigit() and len(bare) <= 18:
        found = Visit.objects.filter(activity_id=int(bare)).order_by("key").first()
    if found is None:
        found = (
            Visit.objects.filter(Q(reference__iexact=bare) | Q(reference_number__iexact=bare))
            .order_by("key")
            .first()
        )
    return found


def programme_of(point: LocalActionPoint) -> list[str]:
    """The programmes whose keywords the title or description holds (FMS §10.5)."""
    text = f" {point.title} {point.description} ".lower()
    return [key for key, (_label, words) in PROGRAMMES.items() if any(word in text for word in words)]


def programme_q(key: str) -> Q | None:
    """The NeuroDB action points of one programme (its keywords in the title or description)."""
    if key not in PROGRAMMES:
        return None
    match = Q()
    for word in PROGRAMMES[key][1]:
        match |= Q(title__icontains=word) | Q(description__icontains=word)
    return match


def local_points(params, user=None) -> dict[str, Any]:
    """The NeuroDB action points of the action points page with its own filters: ``lq`` (title,
    description, the role or person it is assigned to), ``lstatus``, ``lpriority``, ``lprogramme``;
    each row says whether ``user`` may change its status."""
    points = LocalActionPoint.objects.select_related("assignee", "created_by")
    q = (params.get("lq") or "").strip()[:100]
    status = params.get("lstatus") or ""
    priority = params.get("lpriority") or ""
    programme = params.get("lprogramme") or ""
    if q:
        points = points.filter(
            Q(title__icontains=q)
            | Q(description__icontains=q)
            | Q(assignee_role__icontains=q)
            | Q(assignee__first_name__icontains=q)
            | Q(assignee__last_name__icontains=q)
            | Q(visit_key__iexact=q)
        )
    if status in LocalActionPoint.Status.values:
        points = points.filter(status=status)
    if priority in LocalActionPoint.Priority.values:
        points = points.filter(priority=priority)
    if (match := programme_q(programme)) is not None:
        points = points.filter(match)
    rows = list(points.order_by("status", "due_date", "-created_at", "-pk")[:500])
    labels = dict(
        Visit.objects.filter(key__in={p.visit_key for p in rows if p.visit_key}).values_list("key", "label")
    )
    today = timezone.localdate()
    for p in rows:
        p.visit_label = labels.get(p.visit_key, p.visit_key)
        p.overdue = p.status == LocalActionPoint.Status.OPEN and p.due_date is not None and p.due_date < today
        p.programmes = [PROGRAMMES[k][0] for k in programme_of(p)]
        p.can_change = can_change_local(user, p) if user is not None else False
    every = LocalActionPoint.objects.all()
    present = {k for p in every.only("title", "description") for k in programme_of(p)}
    return {
        "rows": rows,
        "filtered": bool(q or status or priority or programme),
        "total": every.count(),
        "open": every.filter(status=LocalActionPoint.Status.OPEN).count(),
        "q": q,
        "status": status,
        "priority": priority,
        "programme": programme,
        "programmes": [(k, PROGRAMMES[k][0]) for k in PROGRAMMES if k in present],
        "statuses": LocalActionPoint.Status.choices,
        "priorities": LocalActionPoint.Priority.choices,
    }


def visit_local_points(key: str) -> list[LocalActionPoint]:
    return list(
        LocalActionPoint.objects.filter(visit_key=key)
        .select_related("assignee")
        .order_by("status", "-created_at")
    )


def followed_up_q() -> Q:
    """The NeuroDB action points that count as a visit's follow-up: added by hand and not dropped, or
    made by NeuroDB and marked done."""
    return ~Q(visit_key="") & (
        Q(source=LocalActionPoint.Source.MANUAL, status__in=("open", "done"))
        | Q(source=LocalActionPoint.Source.AUTO, status="done")
    )


def followed_up_keys(keys: Iterable[str] | None = None) -> set[str]:
    """The keys of the visits a NeuroDB action point follows up (of ``keys``, or all)."""
    rows = LocalActionPoint.objects.filter(followed_up_q())
    if keys is not None:
        rows = rows.filter(visit_key__in=list(keys))
    return set(rows.values_list("visit_key", flat=True))


def add_working_days(day: datetime.date, days: int) -> datetime.date:
    """``day`` plus ``days`` working days (Monday to Friday)."""
    while days > 0:
        day += datetime.timedelta(days=1)
        if day.weekday() < 5:
            days -= 1
    return day


def create_automatic(today: datetime.date | None = None) -> int:
    """FMS §10.4: a NeuroDB action point for each scored visit whose quality is Low (below the Medium
    band, 50) with at least one of the AI's action point flags (R7, R8, R32), unless one is open for the
    visit already, or NeuroDB already made one since the visit last changed in eTools. Below 30 it is
    High and due in 5 working days, else Medium and due in 10. Its title names the flag that took the
    most points ("Follow up on R8 — Visit 1670"), its description lists the flags (never a narrative or
    a person). Returns how many were made."""
    from . import privacy
    from .rules import code_order

    today = today or timezone.localdate()
    medium = ScoreSetting.load().band_medium
    visits = list(
        Visit.objects.filter(
            quality_score__isnull=False, quality_score__lt=medium, flags__overlap=list(AUTO_RULES)
        )
        .order_by("key")
        .only("pk", "key", "label", "reference", "quality_score", "flags", "last_modified")
    )
    if not visits:
        return 0
    keys = [v.key for v in visits]
    open_keys = set(
        LocalActionPoint.objects.filter(visit_key__in=keys, status=LocalActionPoint.Status.OPEN).values_list(
            "visit_key", flat=True
        )
    )
    made_at: dict[str, datetime.datetime] = {}
    for key, created in LocalActionPoint.objects.filter(
        visit_key__in=keys, source=LocalActionPoint.Source.AUTO
    ).values_list("visit_key", "created_at"):
        made_at[key] = max(made_at.get(key, created), created)
    results: dict[int, list[VisitRuleResult]] = {}
    for r in VisitRuleResult.objects.filter(visit_id__in=[v.pk for v in visits], status="fail"):
        results.setdefault(r.visit_id, []).append(r)
    names_ = privacy.names()
    made = []
    for visit in visits:
        if visit.key in open_keys:
            continue
        if visit.key in made_at and (
            visit.last_modified is None or visit.last_modified <= made_at[visit.key]
        ):
            continue
        failed = sorted(results.get(visit.pk, []), key=lambda r: code_order(r.rule))
        triggers = [r for r in failed if r.rule in AUTO_RULES]
        if not triggers:
            continue
        # the flag that took the most points; on a tie, the first rule
        dominant = max(triggers, key=lambda r: (r.deducted, -code_order(r.rule)[0]))
        score = float(visit.quality_score)
        high = score < HIGH_BELOW
        reference = visit.reference or visit.label
        lines = [f"Quality score {score:g} (Low). Flags that call for a follow-up:"]
        lines += [f"- {privacy.clean(r.detail or r.rule, 400, names_)[0]}" for r in triggers]
        others = [r.rule for r in failed if r.rule not in AUTO_RULES]
        if others:
            lines.append(f"Other flags: {', '.join(others)}.")
        made.append(
            LocalActionPoint(
                title=f"Follow up on {dominant.rule} — {reference}"[:TITLE_CHARS],
                description="\n".join(lines)[:DESCRIPTION_CHARS],
                visit_key=visit.key,
                priority=LocalActionPoint.Priority.HIGH if high else LocalActionPoint.Priority.MEDIUM,
                due_date=add_working_days(today, HIGH_DAYS if high else MEDIUM_DAYS),
                status=LocalActionPoint.Status.OPEN,
                assignee_role="PME focal point",
                source=LocalActionPoint.Source.AUTO,
                rule=dominant.rule,
                created_by_name="NeuroDB",
            )
        )
    LocalActionPoint.objects.bulk_create(made)
    return len(made)
