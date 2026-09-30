"""The sources an indicator can be linked to, as grouped choices for the admin, and how a choice is
stored on a Link.

Choice values: ``etools:<pd id>:<indicator key>``, ``ai:<master indicator id>``,
``youth:<Compiler year>:<level>:<id>``, ``edu:<programme>:<year or round>``.
"""

from __future__ import annotations

import datetime
from typing import Any

from .models import Indicator, Link

MAX_PDS = 150


def _year_of(text: str | None) -> int | None:
    head = str(text or "")[:4]
    return int(head) if head.isdigit() else None


def choices(indicator: Indicator) -> list[tuple[str, list[tuple[str, str]]]]:
    from neurodb.datamart import monitoring
    from neurodb.education.models import EducationFigures
    from neurodb.indicators.models import MasterIndicator
    from neurodb.youth.figures import Figures, indicator_label
    from neurodb.youth.models import YouthFigures

    from .services import interventions, output_matches

    programme = indicator.programme
    years = set(programme.years)
    groups: list[tuple[str, list[tuple[str, str]]]] = []

    pds = interventions(programme)
    if indicator.output_id:
        pds = [
            pd for pd in pds if any(output_matches(indicator.output, n) for n in (pd.cp_outputs or []))
        ] or pds
    pds = pds[:MAX_PDS]
    if pds:
        year = min(max(datetime.date.today().year, programme.start_year), programme.end_year)
        rows = monitoring.indicators(monitoring.Filters(pd_ids=[pd.pk for pd in pds], scope="all", year=year))
        options = sorted(
            ((f"etools:{r.pd.id}:{r.key}", f"{r.pd.number or r.pd.pk} — {r.title[:140]}") for r in rows),
            key=lambda o: o[1],
        )
        if options:
            groups.append(("eTools PD indicators (partner reporting)", options))

    masters = (
        MasterIndicator.objects.filter(
            is_active=True, database__reporting_year__year__in=[str(y) for y in years]
        )
        .select_related("database__reporting_year")
        .order_by("database__reporting_year__year", "database__name", "sequence", "name")
    )
    options = [
        (
            f"ai:{m.pk}",
            f"{m.database.reporting_year.year} · {m.database.label or m.database.name} — "
            f"{(m.awp_code + ' ') if m.awp_code else ''}{m.name[:140]}",
        )
        for m in masters
    ]
    if options:
        groups.append(("ActivityInfo master indicators", options))

    options = []
    for record in YouthFigures.objects.order_by("-year"):
        if _year_of(record.year) not in years:
            continue
        for item in Figures(record.payload).payload.get("indicators", []):
            options.append(
                (
                    f"youth:{record.year}:{item['level']}:{item['id']}",
                    f"Youth {record.year} — {indicator_label(item)}",
                )
            )
    if options:
        groups.append(("Compiler youth indicators (young people reached)", options))

    names = {"mscc": "Makani", "bridging": "Dirasa"}
    options = [
        (f"edu:{r.programme}:{r.year}", f"{names.get(r.programme, r.programme)} {r.year} — children enrolled")
        for r in EducationFigures.objects.order_by("programme", "-year")
        if _year_of(r.year) in years
    ]
    if options:
        groups.append(("Compiler education (children enrolled)", options))
    return groups


def value_of(link: Link) -> str:
    if link.kind == Link.Kind.ETOOLS:
        return f"etools:{link.pd_id}:{link.etools_key}"
    if link.kind == Link.Kind.ACTIVITYINFO:
        return f"ai:{link.master_id}"
    if link.kind == Link.Kind.YOUTH:
        return f"youth:{link.source_period}:{link.youth_level}:{link.youth_id}"
    if link.kind == Link.Kind.EDUCATION:
        return f"edu:{link.education_programme}:{link.source_period}"
    return ""


def default_year(value: str) -> int | None:
    kind, _, rest = value.partition(":")
    if kind == "ai":
        from neurodb.indicators.models import MasterIndicator

        master = MasterIndicator.objects.select_related("database__reporting_year").filter(pk=rest).first()
        return (
            _year_of(master.database.reporting_year.year)
            if master and master.database.reporting_year
            else None
        )
    if kind == "youth":
        return _year_of(rest.split(":", 1)[0])
    if kind == "edu":
        return _year_of(rest.split(":", 1)[1] if ":" in rest else "")
    return datetime.date.today().year


def apply(link: Link, value: str) -> Link:
    """Set ``link``'s fields from a choice value (raises ValueError)."""
    parts = value.split(":")
    kind = parts[0]
    fields: dict[str, Any] = {
        "pd_id": None,
        "etools_key": "",
        "master_id": None,
        "youth_level": "",
        "youth_id": None,
        "education_programme": "",
        "source_period": "",
    }
    try:
        if kind == "etools":
            from neurodb.partnerships.models import PCA

            pd = PCA.objects.get(pk=int(parts[1]))
            fields.update(pd_id=pd.pk, etools_key=parts[2])
            link.kind = Link.Kind.ETOOLS
            label = f"{pd.number or pd.pk} — {_etools_title(pd.pk, parts[2])}"
        elif kind == "ai":
            from neurodb.indicators.models import MasterIndicator

            master = MasterIndicator.objects.select_related("database").get(pk=int(parts[1]))
            fields.update(master_id=master.pk)
            link.kind = Link.Kind.ACTIVITYINFO
            label = f"{master.database.label or master.database.name} — {master.name}"
        elif kind == "youth":
            period, level, youth_id = ":".join(parts[1:-2]), parts[-2], int(parts[-1])
            fields.update(youth_level=level, youth_id=youth_id, source_period=period)
            link.kind = Link.Kind.YOUTH
            label = f"Youth {period} — {_youth_label(period, level, youth_id)}"
        elif kind == "edu":
            programme, period = parts[1], ":".join(parts[2:])
            fields.update(education_programme=programme, source_period=period)
            link.kind = Link.Kind.EDUCATION
            name = {"mscc": "Makani", "bridging": "Dirasa"}.get(programme, programme)
            label = f"{name} {period} — children enrolled"
        else:
            raise ValueError("Unknown source.")
    except (IndexError, TypeError, ValueError, LookupError) as exc:
        raise ValueError("This source no longer exists.") from exc
    except Exception as exc:  # DoesNotExist of any model
        if exc.__class__.__name__ == "DoesNotExist":
            raise ValueError("This source no longer exists.") from exc
        raise
    for name, field_value in fields.items():
        setattr(link, name, field_value)
    link.label = label.strip()[:500]
    return link


def _etools_title(pd_id: int, key: str) -> str:
    from neurodb.datamart import models as dm

    rows = dm.PDIndicator.objects.filter(intervention_id=pd_id)
    row = (
        rows.filter(datamart_id=int(key[1:])).first()
        if key.startswith("r")
        else rows.filter(source_id=key).first()
    )
    return (row.title if row else key)[:400]


def _youth_label(period: str, level: str, youth_id: int) -> str:
    from neurodb.youth.figures import Figures, indicator_label
    from neurodb.youth.models import YouthFigures

    record = YouthFigures.objects.filter(year=period).first()
    item = Figures(record.payload).indicators.get((level, youth_id)) if record else None
    return indicator_label(item) if item else f"#{youth_id}"
