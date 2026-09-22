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


def _matrix(qs, row_field: str, col_field: str) -> dict[str, Any]:
    rows: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    cols: set[str] = set()
    for r in qs.values(row_field, col_field, "value"):
        rows[r[row_field] or "All"][r[col_field] or "All"] += r["value"]
        cols.add(r[col_field] or "All")
    col_list = sorted(cols)
    return {
        "columns": col_list,
        "rows": [
            {"label": k, "values": [v.get(c, 0) for c in col_list], "total": sum(v.values())}
            for k, v in sorted(rows.items(), key=_natural)
        ],
        "column_totals": [sum(v.get(c, 0) for v in rows.values()) for c in col_list],
    }


def population_view(year: int, category: str = "total") -> dict[str, Any]:
    """category: total | children | vulnerable (the three v2 sections)."""
    base = PopulationFigure.objects.filter(year=year, category=category)
    national = base.filter(level=PopulationFigure.Level.NATIONAL, age_group="", sex="")
    return {
        "year": year,
        "category": category,
        "totals_by_nationality": {
            r["nationality"]: r["value"] for r in national.values("nationality", "value")
        },
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
