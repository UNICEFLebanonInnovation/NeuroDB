"""Assistant lookups over the periodic reports (snapshots, situation reports issued again and again):
which reports and measures there are, and a measure's values over time with the changes between
dates, as kept from the editions (``neurodb/knowledge/periodic.py``). Like the other tools they read
stored data and only read.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from django.urls import reverse

from .tools import ToolInputError, _clean, _schema

MAX_MEASURES = 80  # listed at once
MAX_KEYS = 12  # followed at once
MAX_POINTS = 60  # per measure, the latest kept


def _series(report: str | None):
    from neurodb.knowledge.models import ReportSeries

    every = ReportSeries.objects.all()
    if not report:
        if every.count() == 1:
            return every.first()
        raise ToolInputError(
            "Say which report: " + "; ".join(every.values_list("name", flat=True)[:30])
            if every.exists()
            else "No periodic report has been added to the knowledge base yet."
        )
    text = report.strip()
    if text.isdigit() and (found := every.filter(pk=int(text)).first()):
        return found
    matches = list(every.filter(name__icontains=text)[:5])
    if not matches:
        words = re.findall(r"[a-z0-9]+", text.lower())
        matches = [s for s in every if all(w in s.key for w in words)][:5]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        names = "; ".join(every.values_list("name", flat=True)[:30]) or "none yet"
        raise ToolInputError(f"No periodic report matches '{report}'. Reports: {names}.")
    raise ToolInputError("Several reports match: " + "; ".join(s.name for s in matches) + ". Say which one.")


def _date(value: str | None, name: str) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ToolInputError(f"'{name}' must be a date as YYYY-MM-DD.") from exc


def _measure(m: dict[str, Any]) -> dict[str, Any]:
    out = {
        "measure": m["key"],
        "group": m["group"],
        "metric": m["metric"],
        **({"breakdown": m["breakdown"]} if m["breakdown"] else {}),
        "unit": "%" if m["is_percent"] else m["unit"],
        "latest": m["latest"],
        "dates": m["points"],
        "since": m["first_date"],
    }
    if m["source"]:
        out["source"] = m["source"]
    if m["internal"]:
        out["internal_use"] = True
    return out


def periodic_reports(report: str | None = None, measure: str | None = None) -> dict[str, Any]:
    from django.db.models import Count, Max, Min, Q

    from neurodb.knowledge import periodic
    from neurodb.knowledge.models import Document, ReportSeries

    ready = Q(editions__status=Document.Status.READY)
    rows = ReportSeries.objects.annotate(
        n=Count("editions", filter=ready, distinct=True),
        first=Min("editions__issued_on", filter=ready),
        last=Max("editions__issued_on", filter=ready),
        last_edition=Max("editions__edition", filter=ready),
    ).order_by("-last", "name")
    if not rows.exists():
        return {"note": "No periodic report has been added to the knowledge base yet.", "reports": []}
    if report is None and rows.count() > 1 and not measure:
        return {
            "reports": [
                {
                    "report": s.name,
                    "editions": s.n,
                    "first_issued": s.first,
                    "last_issued": s.last,
                    "latest_edition": s.last_edition,
                    "url": s.get_absolute_url(),
                }
                for s in rows
            ],
            "next": "Call periodic_reports with a report to list its measures.",
        }
    series = _series(report)
    info = rows.get(pk=series.pk)
    found = periodic.measures(series, measure or "")
    editions = [
        {"edition": d.edition, "issued_on": d.issued_on, "url": d.get_absolute_url()}
        for d in periodic.editions(series)
    ]
    return {
        "report": series.name,
        "editions": editions,
        "first_issued": info.first,
        "last_issued": info.last,
        "measures": [_measure(m) for m in found[:MAX_MEASURES]],
        "measures_found": len(found),
        **(
            {"note": "Too many measures to list: give words to narrow them."}
            if len(found) > MAX_MEASURES
            else {}
        ),
        "how_to_read": (
            "Each measure is one thing counted in every edition (its 'measure' key). 'latest' is its value "
            "on the latest date any edition gives. Use report_figures with the keys for values over time."
        ),
        "url": series.get_absolute_url(),
    }


def report_figures(
    report: str | None = None,
    measures: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    edition: int | None = None,
) -> dict[str, Any]:
    from neurodb.knowledge import periodic
    from neurodb.knowledge.models import ReportFigure

    series = _series(report)
    catalog = {m["key"]: m for m in periodic.measures(series)}
    keys: list[str] = []
    unmatched = []
    for wanted in measures or []:
        if wanted in catalog:
            keys.append(wanted)
            continue
        hits = [m["key"] for m in periodic.measures(series, wanted)]
        keys += hits[:5] if hits else []
        if not hits:
            unmatched.append(wanted)
    keys = list(dict.fromkeys(keys))[:MAX_KEYS]
    if not keys:
        raise ToolInputError(
            "Name the measures (keys or words from periodic_reports)."
            + (f" Nothing matches: {', '.join(unmatched)}." if unmatched else "")
        )
    first, last = _date(start, "start"), _date(end, "end")
    out_measures = []
    if edition is not None:  # what one edition said
        document = periodic.editions(series).filter(edition=edition).first()
        if document is None:
            raise ToolInputError(f"No edition {edition} of {series.name}.")
        rows = ReportFigure.objects.filter(document=document, key__in=keys).order_by("key", "as_of")
        for key in keys:
            m = catalog[key]
            values = [
                {"as_of": f.as_of, "value": f.value, "target": f.target, "page": f.page}
                for f in rows
                if f.key == key
            ]
            out_measures.append({**_measure(m), "values": values})
        scope = {
            "edition": edition,
            "issued_on": document.issued_on,
            "edition_url": document.get_absolute_url(),
        }
    else:
        lines = periodic.timeline(series, keys, first, last)
        for key in keys:
            points = lines[key][-MAX_POINTS:]
            m = catalog[key]
            out_measures.append(
                {
                    **_measure(m),
                    "values": [
                        {
                            "as_of": p["as_of"],
                            "value": p["value"],
                            **({"target": p["target"]} if p["target"] is not None else {}),
                            "edition": p["edition"],
                        }
                        for p in points
                    ],
                    **periodic.changes(points),
                }
            )
        scope = {"from": first, "to": last}
    return _clean(
        {
            "report": series.name,
            **scope,
            "measures": out_measures,
            **({"not_found": unmatched} if unmatched else {}),
            "how_to_read": (
                "Values are by the date they are about. When several editions give a value for the same "
                "date, the newest edition's counts. 'steps' are the changes from one date to the next, "
                "'overall' from the first to the last date shown. Figures marked internal_use are for "
                "internal use only: say so when giving them."
            ),
            "url": series.get_absolute_url(),
            "editions_url": reverse("knowledge:series_index"),
        }
    )


REPORT_TOOLS = {
    "periodic_reports": (
        periodic_reports,
        "Periodic reports kept in the knowledge base (snapshots, situation reports issued again and again, "
        "e.g. the escalation of hostilities snapshot): with no report, the list of reports and their "
        "editions; with a report, its editions and the measures followed in them (what is counted, latest "
        "value, how many dates), optionally only those matching 'measure' words.",
        _schema(
            {
                "report": {"type": "string", "description": "the report's name or words of it"},
                "measure": {"type": "string", "description": "words to narrow the measures, e.g. shelters"},
            }
        ),
        "Reading the periodic reports",
    ),
    "report_figures": (
        report_figures,
        "Values of measures of a periodic report over time (by the date each value is about, across all "
        "editions), with the change between dates, the overall change, highest and lowest; or, with "
        "'edition', the values printed in that one edition. Give measure keys from periodic_reports, or "
        "words. Use for counts per period, before and after, differences and trends.",
        _schema(
            {
                "report": {"type": "string"},
                "measures": {"type": "array", "items": {"type": "string"}},
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD"},
                "edition": {"type": "integer", "minimum": 1, "maximum": 10000},
            },
            ["measures"],
        ),
        "Reading figures over time",
    ),
}
