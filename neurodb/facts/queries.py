"""Parameterised aggregation queries over the v2 fact table (the only raw SQL in NeuroDB v3).

Every query here reproduces a rule that lived in ``pivoting/queries.py`` of v2, with these
differences (each recorded in docs/DIVERGENCES.md):

* all inputs are bound parameters; nothing is spliced into SQL text;
* the ``funded_by = 'UNICEF'`` filter is an explicit flag instead of 26 commented-out lines;
* division by a zero target returns NULL instead of raising or returning 0;
* SUM_OVER_SUM master indicators get a value on dashboards (v2 computed them only in Neuro Reports).

Semantics (from v2):

* a leaf value is ``SUM(indicator_value)`` of activity records whose ``indicator_id`` equals the
  leaf's ``ai_indicator`` within the same database;
* a sub-indicator value is the SUM (or, for AVERAGE subs, the average) of its leaf totals;
* a master indicator combines the sub-indicators linked with effect ``TOTAL`` according to its
  aggregation method: SUM of all leaf values, AVERAGE of the monthly sums, MAXIMUM of the
  monthly sums, COUNT of activity records, or SUM_OVER_SUM = numerator sub / denominator sub.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import connection

MONTH_LABELS = {
    "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr", "05": "May", "06": "Jun",
    "07": "Jul", "08": "Aug", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
}  # fmt: skip


def _rows(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


@dataclass(frozen=True)
class FactFilter:
    """Filters applied to activity records before aggregation."""

    database_id: int
    funded_by_unicef_only: bool = False
    month_to: int | None = None  # 1..12, inclusive, on month_name 'YYYY-MM...'
    edited_before: date | None = None  # v2 HPM cut-off: exclude records edited after this date

    def sql(self) -> tuple[str, dict[str, Any]]:
        clauses = ["i.database_id = %(database_id)s"]
        params: dict[str, Any] = {"database_id": self.database_id}
        if self.funded_by_unicef_only:
            clauses.append("r.funded_by = 'UNICEF'")
        if self.month_to:
            clauses.append(
                "r.month_name IS NOT NULL AND length(r.month_name) >= 7 "
                "AND substring(r.month_name from 6 for 2) ~ '^[0-9]{2}$' "
                "AND substring(r.month_name from 6 for 2)::integer <= %(month_to)s"
            )
            params["month_to"] = self.month_to
        if self.edited_before:
            clauses.append("(r.last_edited_time IS NULL OR r.last_edited_time < %(edited_before)s)")
            params["edited_before"] = self.edited_before
        return " AND ".join(clauses), params


# Leaf and sub-indicator CTEs shared by the master-level queries.
_LEAF_CTE = """
leaf AS (
    SELECT i.id AS indicator_id, r.month_name, SUM(r.indicator_value) AS value, COUNT(*) AS reports
    FROM pivoting_indicatornew i
    JOIN pivoting_activityreportnew r ON r.indicator_id = i.ai_indicator AND r.dbase_id = i.database_id
    WHERE {where}
    GROUP BY i.id, r.month_name
),
leaf_total AS (
    SELECT indicator_id, SUM(value) AS value, SUM(reports) AS reports FROM leaf GROUP BY indicator_id
),
sub AS (
    SELECT s.id AS sub_id,
           CASE WHEN s.aggregation_method = 'AVERAGE' THEN AVG(lt.value) ELSE SUM(lt.value) END AS value,
           SUM(lt.reports) AS reports
    FROM pivoting_subindicator s
    JOIN pivoting_subindicator_indicators si ON si.subindicator_id = s.id
    JOIN leaf_total lt ON lt.indicator_id = si.indicatornew_id
    GROUP BY s.id, s.aggregation_method
),
sub_month AS (
    SELECT si.subindicator_id AS sub_id, l.month_name, SUM(l.value) AS value
    FROM pivoting_subindicator_indicators si
    JOIN leaf l ON l.indicator_id = si.indicatornew_id
    GROUP BY si.subindicator_id, l.month_name
)
"""

_MASTER_SQL = """
WITH {leaf_cte},
master_total AS (
    SELECT ms.master_id,
           SUM(sub.value)   AS sum_value,
           SUM(sub.reports) AS reports
    FROM pivoting_mastersubindicator ms JOIN sub ON sub.sub_id = ms.sub_id
    WHERE ms.effect = 'TOTAL'
    GROUP BY ms.master_id
),
master_month AS (
    SELECT ms.master_id, sm.month_name, SUM(sm.value) AS value
    FROM pivoting_mastersubindicator ms JOIN sub_month sm ON sm.sub_id = ms.sub_id
    WHERE ms.effect = 'TOTAL'
    GROUP BY ms.master_id, sm.month_name
),
master_month_agg AS (
    SELECT master_id, AVG(value) AS avg_value, MAX(value) AS max_value FROM master_month GROUP BY master_id
),
ratio AS (
    SELECT n.master_id, n.value AS numerator, d.value AS denominator
    FROM (SELECT ms.master_id, SUM(sub.value) AS value FROM pivoting_mastersubindicator ms
          JOIN sub ON sub.sub_id = ms.sub_id WHERE ms.effect = 'NUMERATOR' GROUP BY ms.master_id) n
    JOIN (SELECT ms.master_id, SUM(sub.value) AS value FROM pivoting_mastersubindicator ms
          JOIN sub ON sub.sub_id = ms.sub_id WHERE ms.effect = 'DENOMINATOR' GROUP BY ms.master_id) d
      ON d.master_id = n.master_id
)
SELECT m.id, m.name, m.awp_code, m.aggregation_method, m.reporting_level, m.indicator_type,
       m.unit, m.sequence, m.is_active,
       {label_expr} AS label,
       {target_expr} AS target,
       {ram_expr} AS ram_result,
       CASE m.aggregation_method
            WHEN 'SUM'          THEN mt.sum_value
            WHEN 'AVERAGE'      THEN mm.avg_value
            WHEN 'MAXIMUM'      THEN mm.max_value
            WHEN 'MINIMUM'      THEN mm.max_value
            WHEN 'COUNT'        THEN mt.reports
            WHEN 'SUM_OVER_SUM' THEN CASE WHEN rt.denominator > 0 THEN rt.numerator * 100.0 / rt.denominator END
       END AS value,
       mt.reports AS reports,
       rt.numerator, rt.denominator
FROM pivoting_masterindicator m
{report_join}
LEFT JOIN master_total mt ON mt.master_id = m.id
LEFT JOIN master_month_agg mm ON mm.master_id = m.id
LEFT JOIN ratio rt ON rt.master_id = m.id
WHERE m.database_id = %(database_id)s AND m.is_active = true {report_where}
ORDER BY m.sequence, m.awp_code
"""


def master_indicator_values(f: FactFilter, *, report_id: int | None = None) -> list[dict[str, Any]]:
    """Value, target and label of every active master indicator of a database.

    With ``report_id`` the label/target/RAM result overrides of that Neuro Report apply and only
    the report's master indicators are returned (v2 ``NEUROREPORT`` query).
    """
    where, params = f.sql()
    if report_id is not None:
        params["report_id"] = report_id
        report_join = "JOIN pivoting_neuroreportmasterindicator nr ON nr.master_id = m.id"
        report_where = "AND nr.report_id = %(report_id)s"
        label_expr = "COALESCE(NULLIF(nr.label, ''), m.name)"
        target_expr = "COALESCE(NULLIF(nr.target, 0), NULLIF(m.awp_target, 0))"
        ram_expr = "COALESCE(NULLIF(nr.ram_result, 0), m.ram_result)"
    else:
        report_join, report_where = "", ""
        label_expr, target_expr, ram_expr = "m.name", "NULLIF(m.awp_target, 0)", "m.ram_result"
    sql = _MASTER_SQL.format(
        leaf_cte=_LEAF_CTE.format(where=where),
        label_expr=label_expr,
        target_expr=target_expr,
        ram_expr=ram_expr,
        report_join=report_join,
        report_where=report_where,
    )
    return _rows(sql, params)


_SUB_SQL = """
WITH {leaf_cte}
SELECT s.id, s.name, s.awp_code, s.target, s.aggregation_method,
       ms.label, ms.effect, ms.listed, ms.sequence, ms.target AS link_target,
       sub.value, sub.reports
FROM pivoting_mastersubindicator ms
JOIN pivoting_subindicator s ON s.id = ms.sub_id
LEFT JOIN sub ON sub.sub_id = s.id
WHERE ms.master_id = %(master_id)s
ORDER BY ms.sequence, s.awp_code
"""


def sub_indicator_values(master_id: int, f: FactFilter) -> list[dict[str, Any]]:
    """Sub-indicators of one master indicator with their values (v2 ``MASTER_SUB_INDICATORS``)."""
    where, params = f.sql()
    params["master_id"] = master_id
    return _rows(_SUB_SQL.format(leaf_cte=_LEAF_CTE.format(where=where)), params)


_ANALYTICAL_SQL = """
SELECT CONCAT(m.awp_code, '_', m.name) AS master_indicator,
       NULLIF(m.awp_target, 0)         AS target,
       ms.label                        AS sub_indicator,
       ms.sequence                     AS sequence,
       CASE WHEN ms.effect = 'TOTAL' THEN 'Counted in master indicator'
            ELSE 'Not counted in master indicator' END AS value_role,
       i.name                          AS ai_indicator,
       i.gender, i.nationality, i.disability, i.programme, i.age_group AS age,
       r.indicator_name, r.indicator_awp_code AS awp_code, r.emergency,
       r.location_adminlevel_governorate AS governorate,
       r.location_adminlevel_caza        AS district,
       r.location_adminlevel_cadastral_area AS cadaster,
       r.partner_label AS partner, r.project_label AS pd, r.project_plan AS plan, r.project,
       r.database_ai_id AS database_ai_id,
       substring(r.month_name from 6 for 2) AS month_num,
       SUM(r.indicator_value) AS indicator_value
FROM pivoting_masterindicator m
JOIN pivoting_mastersubindicator ms ON ms.master_id = m.id
JOIN pivoting_subindicator s ON s.id = ms.sub_id
JOIN pivoting_subindicator_indicators si ON si.subindicator_id = s.id
JOIN pivoting_indicatornew i ON i.id = si.indicatornew_id
JOIN pivoting_activityreportnew r ON r.indicator_id = i.ai_indicator AND r.dbase_id = m.database_id
WHERE m.database_id = %(database_id)s AND m.is_active = true AND r.indicator_value > 0 {extra}
GROUP BY 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23
"""


def analytical_rows(f: FactFilter, *, emergency: str | None = None) -> list[dict[str, Any]]:
    """Flat rows for the pivot table (v2 ``DATABASE_ACTIVITYINFO`` / ``NEUROREPORT_ACTIVITYINFO``).

    ``emergency`` is validated to 'yes'/'no' and bound as a parameter (v2 spliced it into SQL).
    """
    _, params = f.sql()
    extra = ""
    if f.funded_by_unicef_only:
        extra += " AND r.funded_by = 'UNICEF'"
    if emergency in ("yes", "no"):
        extra += " AND lower(r.emergency) = %(emergency)s"
        params["emergency"] = emergency
    rows = _rows(_ANALYTICAL_SQL.format(extra=extra), params)
    for row in rows:
        row["month"] = MONTH_LABELS.get(row.pop("month_num") or "", "")
    return rows


_AREA_SQL = """
SELECT {code_col} AS code, MAX({name_col}) AS name,
       COUNT(*) AS interventions, COUNT(DISTINCT r.location_name) AS locations,
       COUNT(DISTINCT r.partner_label) AS partners, SUM(r.indicator_value) AS value
FROM pivoting_activityreportnew r
WHERE r.dbase_id = %(database_id)s AND COALESCE({code_col}, '') <> '' {extra}
GROUP BY {code_col}
ORDER BY interventions DESC
"""
_AREA_COLUMNS = {
    "governorate": ("r.location_adminlevel_governorate_code", "r.location_adminlevel_governorate"),
    "district": ("r.location_adminlevel_caza_code", "r.location_adminlevel_caza"),
    "cadaster": ("r.location_adminlevel_cadastral_area_code", "r.location_adminlevel_cadastral_area"),
    "site": ("r.location_name", "r.location_name"),
}


def interventions_by_area(f: FactFilter, level: str, **filters: str) -> list[dict[str, Any]]:
    """Counts and totals per admin area or site (v2 intervention-map and snapshot feeds).

    ``level`` is one of governorate, district, cadaster, site (validated). Optional exact-match
    filters: partner, pd, month, governorate, district.
    """
    code_col, name_col = _AREA_COLUMNS[level]
    _, params = f.sql()
    extra = " AND r.funded_by = 'UNICEF'" if f.funded_by_unicef_only else ""
    mapping = {
        "partner": "r.partner_label",
        "pd": "r.project_label",
        "month": "substring(r.month_name from 6 for 2)",
        "governorate": "r.location_adminlevel_governorate",
        "district": "r.location_adminlevel_caza",
    }
    for key, value in filters.items():
        if key in mapping and value:
            extra += f" AND {mapping[key]} = %({key})s"
            params[key] = value
    return _rows(_AREA_SQL.format(code_col=code_col, name_col=name_col, extra=extra), params)


_SITES_SQL = """
SELECT r.location_name AS name, MAX(r.location_latitude) AS latitude, MAX(r.location_longitude) AS longitude,
       COUNT(*) AS interventions, SUM(r.indicator_value) AS value,
       MAX(r.location_adminlevel_governorate) AS governorate, MAX(r.location_adminlevel_caza) AS district
FROM pivoting_activityreportnew r
WHERE r.dbase_id = %(database_id)s AND COALESCE(r.location_name, '') <> '' {extra}
GROUP BY r.location_name
"""


def sites(f: FactFilter) -> list[dict[str, Any]]:
    """Intervention sites with coordinates (strings in v2; converted to floats where possible)."""
    _, params = f.sql()
    extra = " AND r.funded_by = 'UNICEF'" if f.funded_by_unicef_only else ""
    rows = _rows(_SITES_SQL.format(extra=extra), params)
    for row in rows:
        for key in ("latitude", "longitude"):
            try:
                row[key] = float(row[key]) if row[key] not in (None, "", "NA") else None
            except (TypeError, ValueError):
                row[key] = None
    return rows


_MONTHLY_SQL = """
SELECT substring(r.month_name from 6 for 2) AS month_num, SUM(r.indicator_value) AS value, COUNT(*) AS reports
FROM pivoting_activityreportnew r
WHERE r.dbase_id = %(database_id)s AND r.month_name IS NOT NULL AND length(r.month_name) >= 7 {extra}
GROUP BY 1 ORDER BY 1
"""


def monthly_totals(f: FactFilter) -> list[dict[str, Any]]:
    """Total reported value per month of the database's year (new in v3: trend sparkline)."""
    _, params = f.sql()
    extra = " AND r.funded_by = 'UNICEF'" if f.funded_by_unicef_only else ""
    rows = _rows(_MONTHLY_SQL.format(extra=extra), params)
    return [{"month": MONTH_LABELS.get(r["month_num"] or "", r["month_num"]), **r} for r in rows]


_DISTINCT_SQL = """
SELECT DISTINCT {col} AS value FROM pivoting_activityreportnew r
WHERE r.dbase_id = %(database_id)s AND COALESCE({col}, '') <> '' ORDER BY 1
"""
_DISTINCT_COLUMNS = {
    "partner": "r.partner_label",
    "pd": "r.project_label",
    "governorate": "r.location_adminlevel_governorate",
    "district": "r.location_adminlevel_caza",
    "month": "substring(r.month_name from 6 for 2)",
}


def distinct_values(database_id: int, dimension: str) -> list[str]:
    """Filter options for one dimension of a database (validated column name)."""
    col = _DISTINCT_COLUMNS[dimension]
    rows = _rows(_DISTINCT_SQL.format(col=col), {"database_id": database_id})
    return [r["value"] for r in rows]


_SUMMARY_SQL = """
SELECT r.dbase_id AS database_id, COUNT(*) AS reports, SUM(r.indicator_value) AS value,
       COUNT(DISTINCT r.partner_label) AS partners, COUNT(DISTINCT r.location_name) AS sites,
       MAX(r.last_edited_time) AS last_edited
FROM pivoting_activityreportnew r
WHERE r.dbase_id = ANY(%(database_ids)s)
GROUP BY r.dbase_id
"""


def database_summaries(database_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Per-database record counts for the overview page (new in v3)."""
    if not database_ids:
        return {}
    rows = _rows(_SUMMARY_SQL, {"database_ids": list(database_ids)})
    return {r["database_id"]: r for r in rows}


_ETOOLS_LOCATIONS_SQL = """
SELECT p.number AS pd_number, p.title, p.partner_name, p.status, p.start, p."end", p.section_names,
       unnest(p.location_p_codes) AS p_code
FROM etools_pca p
WHERE p.status = ANY(%(statuses)s)
ORDER BY p.number
"""


def etools_planned_locations(statuses: list[str]) -> list[dict[str, Any]]:
    """Planned locations per programme document (v2 ``ETOOLS_LOCATIONS`` export)."""
    return _rows(_ETOOLS_LOCATIONS_SQL, {"statuses": statuses})
