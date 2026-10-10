"""The field monitoring visits in the knowledge hub (``graph``): one ``fm_visit`` entity per visit
dated (start, else end) in the last ``FMM_HUB_MONTHS`` months, linked to what it is about (its
programme documents, its partners and the country programme outputs it monitored), its sections and
its places.

An entity carries structured facts only (date, status group, rating: its worst record's, quality: the
mean of its records, urgency band: its most urgent record's): never a narrative, an answer, a visit lead
or a team name. The hub keeps visits, not their records: a record is read on its visit's page. The
hub's builder (``graph.builders``) imports this module lazily, and only when the app is installed and
switched on.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.conf import settings
from django.utils import timezone

ORIGIN = "eTools field monitoring"
CP_ORIGIN = "eTools CP outputs monitored"


def months_back(day: datetime.date, months: int) -> datetime.date:
    """The same day ``months`` months before ``day`` (the last day of a shorter month)."""
    index = day.year * 12 + day.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    for last in (31, 30, 29, 28):
        try:
            return datetime.date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise ValueError(day)  # pragma: no cover - a month always has 28 days


def entity_name(label: str, partner: str, day: datetime.date | None) -> str:
    """ "Visit 1722 · Amel Association · 12 May 2026" (the parts that are known)."""
    when = f"{day.day} {day:%b %Y}" if day else ""
    return " · ".join(part for part in (label, partner, when) if part)


def attrs_of(visit) -> dict[str, Any]:
    """The facts of a visit the hub keeps, to identify it and to notice a change of rating: structured
    values only (its quality is the mean of its records, as on its page)."""
    return {
        "date": visit.visit_date.isoformat() if visit.visit_date else "",
        "status_group": visit.status_group,
        "rating": visit.rating,
        "quality": float(visit.quality_score) if visit.quality_score is not None else None,
        "urgency_band": visit.urgency_band,
    }


def _outputs() -> list:
    """The outputs of the current country programme (the latest one when none is marked current)."""
    from neurodb.cpd.models import CountryProgramme, Output

    programme = CountryProgramme.objects.filter(current=True).first() or CountryProgramme.objects.first()
    if programme is None:
        return []
    return list(Output.objects.filter(outcome__programme=programme))


def add_field_monitoring(c, names, today: datetime.date | None = None) -> None:
    """Add the visits dated (start, else end) in the last ``FMM_HUB_MONTHS`` months to the collector
    ``c``, with their links: ``about`` their programme documents, partners and matched country
    programme outputs, ``in_section`` their sections and ``takes_place_in`` their governorate and
    district."""
    from neurodb.cpd.services import output_matches
    from neurodb.graph.models import Entity

    from .models import Visit

    K = Entity.Kind
    today = today or timezone.localdate()
    since = months_back(today, int(getattr(settings, "FMM_HUB_MONTHS", 24)))
    visits = (
        Visit.objects.filter(visit_date__gte=since, visit_date__lte=today)
        .select_related("partner")
        .order_by("visit_date", "key")
    )
    outputs: list | None = None
    matched: dict[str, list[int]] = {}
    for v in visits.iterator(chunk_size=2000):
        partner = (v.partner.short_name or v.partner.name) if v.partner else ""
        ref = c.entity(
            K.FM_VISIT,
            v.key,
            entity_name(v.label, partner, v.visit_date),
            aliases=[v.reference, v.reference_number, *([str(v.activity_id)] if v.activity_id else [])],
            url=v.get_absolute_url(),
            attrs=attrs_of(v),
            lookup={"tool": "fm_visit", "args": {"visit": v.key}},
        )
        for pd_id in v.pd_ids:
            c.edge(ref, "about", (K.PROGRAMME, str(pd_id)), ORIGIN)
        for partner_id in v.partner_ids:
            c.edge(ref, "about", (K.PARTNER, str(partner_id)), ORIGIN)
        for name in v.cp_outputs:
            if name not in matched:
                if outputs is None:
                    outputs = _outputs()
                matched[name] = [o.pk for o in outputs if output_matches(o, name)]
            for output_id in matched[name]:
                c.edge(ref, "about", (K.CPD_OUTPUT, str(output_id)), CP_ORIGIN)
        for section_id in v.section_ids:
            c.edge(ref, "in_section", (K.SECTION, str(section_id)), ORIGIN)
        for section in v.section_names:
            c.edge(ref, "in_section", names.get(K.SECTION, section), ORIGIN)
        c.edge(ref, "takes_place_in", names.get(K.GOVERNORATE, v.governorate_name), ORIGIN)
        c.edge(ref, "takes_place_in", names.get(K.DISTRICT, v.district_name), ORIGIN)
