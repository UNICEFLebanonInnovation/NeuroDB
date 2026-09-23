"""Population figures per year (new v3 table), shaped for the population page charts."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from neurodb.core.models import PopulationFigure


def available_years() -> list[int]:
    return list(PopulationFigure.objects.values_list("year", flat=True).distinct().order_by("-year"))


def _natural(item: tuple[str, Any]) -> tuple[int, str]:
    """Age bands sort by their first number ('5-9' before '10-14'); names sort alphabetically."""
    digits = re.match(r"\d+", str(item[0]))
    return (int(digits.group()) if digits else 10**6, str(item[0]))


ALL = PopulationFigure.Nationality.ALL
NATIONALITY_ORDER = [c for c in PopulationFigure.Nationality.values if c != ALL]


def _matrix(qs, row_field: str, col_field: str) -> dict[str, Any]:
    """Rows x columns of summed values. An "ALL" nationality column is the published all-nationality
    figure, so it becomes the row total instead of a column (adding it to the others would count
    everyone twice)."""
    rows: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cols: set[str] = set()
    for r in qs.values(row_field, col_field, "value"):
        rows[r[row_field] or "All"][r[col_field] or "All"] += r["value"]
        cols.add(r[col_field] or "All")
    has_all = ALL in cols
    cols.discard(ALL)
    col_list = [c for c in NATIONALITY_ORDER if c in cols] + sorted(cols - set(NATIONALITY_ORDER))

    def total(values):
        return values[ALL] if has_all and ALL in values else sum(values.get(c, 0) for c in col_list)

    return {
        "columns": col_list,
        "rows": [
            {"label": k, "values": [v.get(c, 0) for c in col_list], "total": total(v)}
            for k, v in sorted(rows.items(), key=_natural)
        ],
        "column_totals": [sum(v.get(c, 0) for v in rows.values()) for c in col_list],
        "grand_total": sum(total(v) for v in rows.values()),
    }


def population_view(year: int, category: str = "total") -> dict[str, Any]:
    """category: total | children | vulnerable (the three v2 sections)."""
    base = PopulationFigure.objects.filter(year=year, category=category)
    national = base.filter(level=PopulationFigure.Level.NATIONAL, age_group="", sex="")
    return {
        "year": year,
        "category": category,
        "grand_total": national.filter(nationality=ALL).values_list("value", flat=True).first(),
        # nationalities only: "ALL" is their sum and would take half of the pie chart
        "totals_by_nationality": dict(
            sorted(
                ((r["nationality"], r["value"]) for r in national.exclude(nationality=ALL).values()),
                key=lambda kv: NATIONALITY_ORDER.index(kv[0]) if kv[0] in NATIONALITY_ORDER else 99,
            )
        ),
        "by_governorate": _matrix(
            base.filter(level="governorate", age_group="", sex="", vulnerability_level=""),
            "area_name",
            "nationality",
        ),
        "by_district": _matrix(
            base.filter(level="district", age_group="", sex="", vulnerability_level=""),
            "area_name",
            "nationality",
        ),
        "by_age_group": _matrix(
            base.filter(level="national", sex="").exclude(age_group=""), "age_group", "nationality"
        ),
        "by_vulnerability": _matrix(
            base.filter(level="district").exclude(vulnerability_level=""), "area_name", "vulnerability_level"
        )
        if category == "vulnerable"
        else None,
    }
