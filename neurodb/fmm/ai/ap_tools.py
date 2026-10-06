"""The action points look-up of Ask NeuroDB (``fm_action_points``): the eTools action points counted by
status, by the AI's verdict on the completed ones and by PME verification, with the NeuroDB action points
by status. Counts only: never a description, an action taken, a note or who an action point is
assigned to. Registered into the assistant's tools when the app starts (``apps.FmmConfig.ready``), not
offered to Chat with Data."""

from __future__ import annotations

import datetime
from collections import Counter
from typing import Any

from django.db.models import Count, Q
from django.urls import reverse

from neurodb.assistant.tools import ToolInputError

from .. import action_points
from ..models import ActionPointReview, ActionPointVerification, LocalActionPoint

SECTION_CHARS = 64


def _choice(given: str | None, field: str) -> str:
    """The value of ``field`` the action points hold that matches ``given`` (case and spaces aside)."""
    from neurodb.datamart.models import ActionPoint

    wanted = " ".join(str(given or "").split()).casefold()
    if not wanted:
        return ""
    known = [x for x in ActionPoint.objects.order_by().values_list(field, flat=True).distinct() if x]
    exact = [x for x in known if x.casefold() == wanted]
    if exact:
        return exact[0]
    near = [x for x in known if wanted in x.casefold()]
    if len(near) == 1:
        return near[0]
    raise ToolInputError(
        f"No {field} of the action points matches '{given}'. Known: {', '.join(sorted(known)[:30])}."
    )


def fm_action_points(
    section: str | None = None, office: str | None = None, fm_only: bool = False
) -> dict[str, Any]:
    """The counts (see the module's notes), optionally for one eTools section or office, or the field
    monitoring action points only."""
    from neurodb.datamart.models import ActionPoint

    from . import ap_review

    points = ActionPoint.objects.all()
    filters: dict[str, Any] = {}
    if section:
        filters["section"] = _choice(section[:SECTION_CHARS], "section")
        points = points.filter(section=filters["section"])
    if office:
        filters["office"] = _choice(office[:SECTION_CHARS], "office")
        points = points.filter(office=filters["office"])
    if fm_only:
        filters["field_monitoring_only"] = True
        points = points.filter(related_module__iexact="fm")
    today = datetime.date.today()
    open_ = Q(status__in=ActionPoint.OPEN_STATUSES)
    totals = points.aggregate(
        total=Count("pk"),
        open=Count("pk", filter=open_),
        overdue=Count("pk", filter=open_ & Q(due_date__lt=today)),
        completed=Count("pk", filter=action_points.completed_q()),
    )
    prompt = ap_review.current_prompt_hash()
    ids = points.values("datamart_id")
    verdicts = Counter(
        dict(
            ActionPointReview.objects.filter(prompt_hash=prompt or "-", datamart_id__in=ids)
            .values_list("verdict")
            .annotate(n=Count("pk"))
            .order_by()
        )
    )
    labels = dict(ActionPointReview.Verdict.choices)
    reviewed = sum(verdicts.values())
    latest: dict[int, str] = {}
    for datamart_id, state in (
        ActionPointVerification.objects.filter(datamart_id__in=ids)
        .order_by("datamart_id", "-created_at", "-pk")
        .values_list("datamart_id", "state")
    ):
        latest.setdefault(datamart_id, state)
    states = Counter(latest.values())
    state_labels = dict(ActionPointVerification.State.choices)
    local = Counter(LocalActionPoint.objects.values_list("status", flat=True))
    local_labels = dict(LocalActionPoint.Status.choices)
    return {
        "filters": filters,
        "etools_action_points": {
            "total": totals["total"],
            "open": totals["open"],
            "overdue": totals["overdue"],
            "completed": totals["completed"],
        },
        "ai_verdicts_of_completed": {
            "reviewed": reviewed,
            "not_reviewed_yet": max(totals["completed"] - reviewed, 0),
            "by_verdict": {labels[v]: verdicts.get(v, 0) for v in labels},
        },
        "pme_verification": {
            **{state_labels[s]: states.get(s, 0) for s in state_labels},
            "Not verified yet": max(totals["total"] - len(latest), 0),
        },
        "neurodb_action_points": {local_labels[s]: local.get(s, 0) for s in local_labels},
        "url": reverse("reports:action_points"),
    }


ASK_TOOLS: dict[str, tuple[Any, str, dict, str]] = {
    "fm_action_points": (
        fm_action_points,
        "eTools action points counted: total, open, overdue and completed; the AI's adequacy verdicts on the "
        "completed ones (Adequately addressed, Partially addressed, Not addressed, Generic/vague, and not "
        "reviewed yet); the PME verifications (Verified, Rejected, Pending, not verified yet); and the "
        "NeuroDB action points by status. Counts only, optionally for one eTools section or office, or the "
        "field monitoring action points only.",
        {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "An eTools section name."},
                "office": {"type": "string", "description": "A field office."},
                "fm_only": {"type": "boolean", "description": "Only the field monitoring action points."},
            },
            "required": [],
            "additionalProperties": False,
        },
        "Counting action points",
    ),
}
