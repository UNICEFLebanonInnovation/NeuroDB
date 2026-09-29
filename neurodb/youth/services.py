"""The youth dashboard: what Compiler counted, per indicator, and what partners reported in eTools.

Every figure is a count of unique young people read from the one grouping Compiler sent for that
question (see figures.py), so rows are never added up. Targets come from the Compiler programme
documents; they are not set per place, so a governorate filter hides them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.http import QueryDict
from django.utils.dateparse import parse_date
from django.utils.translation import gettext_lazy as _

from . import linking
from .figures import AGE_ORDER, Figures, indicator_label
from .models import YouthFigures, YouthIndicatorLink

BREAKDOWNS = (
    ("partner", _("By partner")),
    ("donor", _("By donor")),
    ("governorate", _("By governorate")),
    ("district", _("By district")),
    ("sex", _("By sex")),
    ("age_group", _("By age group")),
    ("nationality", _("By nationality")),
)


def _int(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() and len(raw) < 12 else None


@dataclass
class YouthFilters:
    year: str = ""
    partner: int | None = None
    donor: int | None = None
    governorate: int | None = None
    master: int | None = None

    @classmethod
    def from_params(cls, params: QueryDict, years: list[str]) -> YouthFilters:
        year = params.get("year", "")
        return cls(
            year=year if year in years else (years[0] if years else ""),
            partner=_int(params.get("partner")),
            donor=_int(params.get("donor")),
            governorate=_int(params.get("governorate")),
            master=_int(params.get("master")),
        )

    def main(self) -> dict[str, int]:
        values = {"partner": self.partner, "donor": self.donor, "governorate": self.governorate}
        return {k: v for k, v in values.items() if v is not None}

    @property
    def active(self) -> bool:
        return bool(self.main() or self.master)


def years() -> list[str]:
    return list(YouthFigures.objects.order_by("-year").values_list("year", flat=True))


def load(year: str) -> YouthFigures | None:
    return YouthFigures.objects.filter(year=year).first()


def _targets(figures: Figures, filters: YouthFilters) -> dict[tuple[str, int], float] | None:
    """Target per indicator, summed over the programme documents that match the filters."""
    if filters.governorate is not None:
        return None
    documents = figures.program_documents
    out: dict[tuple[str, int], float] = {}
    for t in figures.targets:
        document = documents.get(t["program_document"])
        if document is None or t["target"] is None:
            continue
        if filters.partner is not None and document["partner"] != filters.partner:
            continue
        if filters.donor is not None and filters.donor not in document["donors"]:
            continue
        ident = ("sub", t["sub"]) if t["sub"] else ("master", t["master"])
        out[ident] = out.get(ident, 0) + t["target"]
    return out


def _percent(part: float | None, whole: float | None) -> float | None:
    return round(part / whole * 100, 1) if part is not None and whole else None


def _links(figures: Figures, filters: YouthFilters) -> tuple[list[dict[str, Any]], dict]:
    """Every link of the year (under the partner and donor filters), with both sides' figures."""
    rows = linking.link_rows(figures.year)
    documents = figures.program_documents
    if filters.partner is not None or filters.donor is not None:
        keep = []
        for link in rows:
            document = documents.get(link.compiler_pd_id) or {}
            if filters.partner is not None and document.get("partner") != filters.partner:
                continue
            if filters.donor is not None and filters.donor not in document.get("donors", []):
                continue
            keep.append(link)
        rows = keep
    etools = linking.describe(rows, figures.year)
    by_pd_sub = figures.table({}, "sub", "program_document") or {}
    by_pd_master = figures.table({}, "master", "program_document") or {}
    targets = {}
    for t in figures.targets:
        ident = ("sub", t["sub"]) if t["sub"] else ("master", t["master"])
        key = (t["program_document"], *ident)
        targets[key] = (targets.get(key) or 0) + (t["target"] or 0)
    out = []
    by_indicator: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for link in rows:
        if link.level == "sub":
            counted = by_pd_sub.get((link.youth_indicator_id, link.compiler_pd_id))
        else:
            counted = by_pd_master.get((link.youth_indicator_id, link.compiler_pd_id))
        indicator = etools.get((link.pd_id, link.etools_key)) if link.pd_id else None
        reported = indicator.cumulative if indicator else None
        document = documents.get(link.compiler_pd_id) or {}
        entry = {
            "link": link,
            "compiler_pd": link.compiler_pd_code or document.get("project_name") or "—",
            "partner": figures.label("partner", document.get("partner")) if document.get("partner") else "—",
            "youth_indicator": link.youth_indicator,
            "counted": counted or 0,
            "compiler_target": targets.get((link.compiler_pd_id, link.level, link.youth_indicator_id))
            or None,
            "etools": indicator,
            "reported": reported,
            "difference": (counted or 0) - reported if reported is not None else None,
        }
        out.append(entry)
        by_indicator.setdefault((link.level, link.youth_indicator_id), []).append(entry)
    return out, by_indicator


def _indicator_rows(figures: Figures, filters: YouthFilters, by_indicator: dict) -> list[dict[str, Any]]:
    main = filters.main()
    targets = _targets(figures, filters)
    per_master = figures.table(main, "master") or {}
    per_sub = figures.table(main, "sub") or {}
    rows = []
    for master in sorted(figures.masters(), key=lambda m: (m["number"] or "", m["name"])):
        if filters.master is not None and master["id"] != filters.master:
            continue
        reached = per_master.get((master["id"],), 0)
        target = targets.get(("master", master["id"])) if targets is not None else None
        subs = []
        for sub in sorted(figures.subs_of(master["id"]), key=lambda s: (s["number"] or "", s["name"])):
            sub_reached = per_sub.get((sub["id"],), 0)
            sub_target = targets.get(("sub", sub["id"])) if targets is not None else None
            if not sub_reached and not sub_target:
                continue
            subs.append(
                {
                    "id": sub["id"],
                    "label": indicator_label(sub),
                    "reached": sub_reached,
                    "target": sub_target,
                    "percent": _percent(sub_reached, sub_target),
                    "links": by_indicator.get(("sub", sub["id"]), []),
                }
            )
        if not reached and not target and not subs:
            continue
        rows.append(
            {
                "id": master["id"],
                "label": indicator_label(master),
                "reached": reached,
                "target": target,
                "percent": _percent(reached, target),
                "links": by_indicator.get(("master", master["id"]), []),
                "subs": subs,
            }
        )
    return rows


def _breakdown(figures: Figures, filters: dict[str, int], dimension: str) -> list[tuple[str, int]]:
    table = figures.table(filters, dimension) or {}
    rows = [(figures.label(dimension, key[0]), count) for key, count in table.items()]
    if dimension == "age_group":
        return sorted(rows, key=lambda r: AGE_ORDER.index(r[0]) if r[0] in AGE_ORDER else len(AGE_ORDER))
    return sorted(rows, key=lambda r: (-r[1], r[0]))


def _programme_documents(figures: Figures, filters: dict[str, int]) -> list[dict[str, Any]]:
    table = figures.table(filters, "program_document") or {}
    rows = []
    for (pd_id,), count in table.items():
        document = figures.program_documents.get(pd_id) or {}
        if pd_id is None:
            document = {"project_name": _("Activities without a programme document")}
        rows.append(
            {
                "code": document.get("project_code") or "—",
                "name": document.get("project_name") or "",
                "partner": figures.label("partner", document["partner"]) if document.get("partner") else "—",
                "donors": ", ".join(figures.label("donor", d) for d in document.get("donors", [])),
                "period": tuple(parse_date(document.get(k) or "") for k in ("start_date", "end_date")),
                "youth": count,
            }
        )
    return sorted(rows, key=lambda r: (-r["youth"], r["code"]))


def options(figures: Figures) -> dict[str, list[tuple[int, str]]]:
    def listed(dimension: str) -> list[tuple[int, str]]:
        ids = [key[0] for key in (figures.table({}, dimension) or {}) if key[0] is not None]
        return sorted(((i, figures.label(dimension, i)) for i in ids), key=lambda o: o[1].lower())

    return {
        "partners": listed("partner"),
        "donors": listed("donor"),
        "governorates": listed("governorate"),
        "masters": [
            (m["id"], indicator_label(m))
            for m in sorted(figures.masters(), key=lambda m: (m["number"] or "", m["name"]))
        ],
    }


def dashboard(record: YouthFigures, filters: YouthFilters) -> dict[str, Any]:
    figures = Figures(record.payload)
    main = filters.main()
    scoped = {**main, **({"master": filters.master} if filters.master is not None else {})}
    links, by_indicator = _links(figures, filters)
    indicators = _indicator_rows(figures, filters, by_indicator)
    breakdowns = [
        {"key": key, "label": label, "rows": _breakdown(figures, scoped, key)} for key, label in BREAKDOWNS
    ]
    sexes = dict(_breakdown(figures, scoped, "sex"))
    return {
        "figures": figures,
        "total": figures.count(scoped),
        "female": sexes.get("Female", 0),
        "male": sexes.get("Male", 0),
        "partners": len(figures.table(scoped, "partner") or {}),
        "indicators": indicators,
        "breakdowns": breakdowns,
        "programme_documents": _programme_documents(figures, scoped),
        "links": links,
        "linked": sum(1 for link in links if link["link"].source == YouthIndicatorLink.Source.CONFIRMED),
        "suggested": sum(1 for link in links if link["link"].source == YouthIndicatorLink.Source.SUGGESTED),
        "options": options(figures),
        "fetched_at": record.fetched_at,
        "generated_at": figures.payload.get("generated_at"),
        "targets_hidden": filters.governorate is not None,
        "chart_data": {b["key"]: b["rows"][:25] for b in breakdowns},
    }
