"""The Education page: what Compiler counted in Makani and Bridging, by partner and place.

Every child figure is a count of unique children from the one grouping that answers it, so the
rows of a table do not add up to its total. Attendance is in days (recorded, attended), which do
add up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.http import QueryDict
from django.utils.translation import gettext_lazy as _

from .figures import AGE_ORDER, Block, Figures
from .models import EducationFigures

BREAKDOWNS = (
    ("governorate", _("By governorate")),
    ("district", _("By district")),
    ("partner", _("By partner")),
    ("center", _("By center")),
    ("school", _("By school")),
    ("round", _("By round")),
    ("registration_type", _("By registration type")),
    ("registration_level", _("By level")),
    ("sex", _("By sex")),
    ("age_group", _("By age group")),
    ("nationality", _("By nationality")),
    ("disability", _("By disability")),
    ("learning_result", _("By learning result")),
    ("education_status", _("By education status before")),
    ("source", _("By source of identification")),
    ("center_type", _("By center type")),
)
TOP = 30  # rows shown per breakdown (the CSV has them all)


def _int(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() and len(raw) < 12 else None


@dataclass
class EducationFilters:
    programme: str = ""
    year: str = ""
    partner: int | None = None
    governorate: int | None = None

    @classmethod
    def from_params(cls, params: QueryDict, available: dict[str, list[str]]) -> EducationFilters:
        programme = params.get("programme", "")
        if programme not in available:
            programme = next(iter(available), "")
        years = available.get(programme, [])
        year = params.get("year", "")
        return cls(
            programme=programme,
            year=year if year in years else (years[0] if years else ""),
            partner=_int(params.get("partner")),
            governorate=_int(params.get("governorate")),
        )

    def main(self) -> dict[str, int]:
        values = {"partner": self.partner, "governorate": self.governorate}
        return {k: v for k, v in values.items() if v is not None}


def available() -> dict[str, list[str]]:
    """Programme -> its stored years, newest first (Makani first)."""
    out: dict[str, list[str]] = {}
    rows = EducationFigures.objects.order_by("programme", "-counted_at", "-year").values_list(
        "programme", "year"
    )
    for programme, year in rows:
        out.setdefault(programme, []).append(year)
    return dict(sorted(out.items(), key=lambda item: (item[0] != "mscc", item[0])))


def labels() -> dict[str, str]:
    return {
        f.programme: (f.payload.get("label") or f.programme)
        for f in EducationFigures.objects.only("programme", "payload").order_by("programme", "-fetched_at")
    }


def _rate(attended: Any, recorded: Any) -> float | None:
    return round(attended / recorded * 100, 1) if recorded else None


def _rows(
    figures: Figures, block: Block, filters: dict[str, int], dimension: str
) -> list[dict[str, Any]] | None:
    table = block.table(filters, dimension)
    if table is None:
        return None
    rows = [{"label": figures.label_of(dimension, key[0]), **values} for key, values in table.items()]
    if dimension == "age_group":
        return sorted(rows, key=lambda r: AGE_ORDER.index(r["label"]) if r["label"] in AGE_ORDER else 99)
    if dimension in ("round",):
        return sorted(rows, key=lambda r: r["label"])
    return sorted(rows, key=lambda r: (-(r.get("people") or 0), r["label"]))


def _by_sex(figures: Figures, block: Block, filters: dict[str, int], dimension: str) -> list[dict[str, Any]]:
    """People per value of ``dimension``, with girls and boys when the grouping exists."""
    rows = _rows(figures, block, filters, dimension) or []
    split = block.table(filters, dimension, "sex") or {}
    for row in rows:
        row["female"] = row["male"] = None
    by_label = {r["label"]: r for r in rows}
    for (value, sex), values in split.items():
        row = by_label.get(figures.label_of(dimension, value))
        if row is not None and sex in ("Female", "Male"):
            row[sex.lower()] = values.get("people")
    return rows


def dashboard(record: EducationFigures, filters: EducationFilters) -> dict[str, Any]:
    figures = Figures(record.payload)
    main = filters.main()
    registrations = figures.block("registrations")
    total = registrations.total(main)
    sexes = registrations.table(main, "sex") or {}
    attendance = figures.block("attendance")
    attendance_total = attendance.total(main)
    months = [
        {
            "month": key[0],
            "recorded": values.get("days_recorded"),
            "attended": values.get("days_attended"),
            "rate": _rate(values.get("days_attended"), values.get("days_recorded")),
        }
        for key, values in sorted((attendance.table(main, "month") or {}).items(), key=lambda i: str(i[0]))
    ]
    breakdowns = []
    for dimension, label in BREAKDOWNS:
        if dimension in main:
            continue
        rows = _rows(figures, registrations, main, dimension)
        if rows:
            breakdowns.append({"key": dimension, "label": label, "rows": rows[:TOP], "more": len(rows) - TOP})
    sex_age = registrations.table(main, "age_group", "sex")
    matrix = None
    if sex_age:
        matrix = []
        for group in AGE_ORDER:
            female = sex_age.get((group, "Female"), {}).get("people")
            male = sex_age.get((group, "Male"), {}).get("people")
            other = sex_age.get((group, "Not specified"), {}).get("people")
            if female or male or other:
                matrix.append({"age_group": group, "female": female, "male": male, "other": other})
    return {
        "figures": figures,
        "label": figures.label,
        "children": total.get("people", 0),
        "pre_tested": total.get("pre_tested"),
        "post_tested": total.get("post_tested"),
        "female": (sexes.get(("Female",)) or {}).get("people", 0),
        "male": (sexes.get(("Male",)) or {}).get("people", 0),
        "partners": len(registrations.table(main, "partner") or {}),
        "sites": figures.block("sites").total(main).get("people", 0),
        "staff": figures.block("staff").total(main).get("people", 0),
        "attendance_rate": _rate(
            attendance_total.get("days_attended"), attendance_total.get("days_recorded")
        ),
        "days_recorded": attendance_total.get("days_recorded"),
        "months": months,
        "matrix": matrix,
        "services": _by_sex(figures, figures.block("services"), main, "service"),
        "programmes": _by_sex(figures, figures.block("education_programmes"), main, "education_programme"),
        "site_types": _rows(figures, figures.block("sites"), main, "site_type") or [],
        "staff_by_sex": _rows(figures, figures.block("staff"), main, "sex") or [],
        "breakdowns": breakdowns,
        "errors": {
            name: block.get("error")
            for name, block in record.payload.get("blocks", {}).items()
            if block.get("error")
        },
        "options": {
            "partners": sorted(
                (
                    (k[0], figures.label_of("partner", k[0]))
                    for k in registrations.table({}, "partner") or {}
                    if k[0] is not None
                ),
                key=lambda o: o[1].lower(),
            ),
            "governorates": sorted(
                (
                    (k[0], figures.label_of("governorate", k[0]))
                    for k in registrations.table({}, "governorate") or {}
                    if k[0] is not None
                ),
                key=lambda o: o[1].lower(),
            ),
        },
        "rounds": record.payload.get("rounds", []),
        "counted_at": record.counted_at,
        "fetched_at": record.fetched_at,
    }
