"""What other NeuroDB pages read from Monitoring insights. They import this module lazily, only when the
app is installed, so ``neurodb.datamart`` and ``neurodb.reports`` never depend on it at module level."""

from __future__ import annotations


def action_point_ids(key: str) -> list[int]:
    """The ids of the eTools action points linked to the visit ``key`` (``VisitActionPoint``): the same
    set the visit page lists, for the action points page's "From Visit 1722" filter."""
    from .models import VisitActionPoint

    return list(
        VisitActionPoint.objects.filter(visit__key=str(key or "")[:40])
        .order_by("action_point_id")
        .values_list("action_point_id", flat=True)
    )


def visit_label(key: str) -> str:
    """The label of the visit ``key`` ("Visit 1722"), or "" when no visit has that key."""
    from .models import Visit

    return Visit.objects.filter(key=str(key or "")[:40]).values_list("label", flat=True).first() or ""
