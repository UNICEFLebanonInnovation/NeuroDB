"""A complete, hand-written overview payload following the contract of the design brief (section 2).

The page tests render against these dicts so they do not depend on the real service; the lead can
compare their shape with ``neurodb.reports.overview.build`` output (every key the templates read is here).
"""

from __future__ import annotations

import datetime
from typing import Any

FAKE_TODAY = datetime.date(2026, 7, 1)
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
SECTIONS = ["Child Protection", "Education", "WASH"]
GOVERNORATES = ["Akkar", "Baalbek-Hermel", "Beirut", "Bekaa", "Mount Lebanon", "Nabatieh", "North", "South"]

STATUS_ZERO = {"on_track": 0, "off_track": 0, "over_target": 0, "no_target": 0, "not_reported": 0}


def fake_options() -> dict[str, list[str]]:
    return {"sections": list(SECTIONS), "governorates": list(GOVERNORATES)}


def _activityinfo_empty() -> dict[str, Any]:
    return {
        "year": None,
        "cards": [],
        "status_counts": {"on_track": 0, "off_track": 0, "over_target": 0, "no_target": 0},
        "totals": {"databases": 0, "indicators": 0, "reports": 0, "partners": 0},
        "last_runs": [],
    }


def fake_data(today: datetime.date = FAKE_TODAY) -> dict[str, Any]:
    """Every block filled with plausible numbers."""
    return {
        "impact": {
            "children_reached": 48_250,
            "children_etools": 31_400,
            "children_activityinfo": 16_850,
            "delta_previous_year": 12.4,
            "by_governorate": [
                {
                    "name": "Akkar",
                    "etools": 9_800,
                    "activityinfo": 6_200,
                    "reached": 9_800,
                    "population": 120_000,
                    "coverage": 8.2,
                    "level": 6,
                },
                {
                    "name": "Bekaa",
                    "etools": 7_100,
                    "activityinfo": 8_400,
                    "reached": 8_400,
                    "population": 140_000,
                    "coverage": 6.0,
                    "level": 5,
                },
                {
                    "name": "North",
                    "etools": 6_000,
                    "activityinfo": 1_200,
                    "reached": 6_000,
                    "population": 210_000,
                    "coverage": 2.9,
                    "level": 4,
                },
                {
                    "name": "Mount Lebanon",
                    "etools": 4_500,
                    "activityinfo": 500,
                    "reached": 4_500,
                    "population": 390_000,
                    "coverage": 1.2,
                    "level": 3,
                },
                {
                    "name": "Beirut",
                    "etools": 2_400,
                    "activityinfo": 300,
                    "reached": 2_400,
                    "population": 90_000,
                    "coverage": 2.7,
                    "level": 2,
                },
                {
                    "name": "South",
                    "etools": 1_600,
                    "activityinfo": 250,
                    "reached": 1_600,
                    "population": None,
                    "coverage": None,
                    "level": 1,
                },
            ],  # fmt: skip
            "by_section": [
                {
                    "section": "Child Protection",
                    "achieved": 21_000,
                    "target": 30_000,
                    "percent": 70.0,
                    "elapsed": 49.6,
                    "indicators": 12,
                },
                {
                    "section": "Education",
                    "achieved": 8_400,
                    "target": 24_000,
                    "percent": 35.0,
                    "elapsed": 49.6,
                    "indicators": 9,
                },
                {
                    "section": "WASH",
                    "achieved": 2_000,
                    "target": 3_000,
                    "percent": 66.7,
                    "elapsed": 49.6,
                    "indicators": 3,
                },
            ],  # fmt: skip
            "monthly": {
                "labels": list(MONTHS),
                "etools": [0, 4_000, 6_500, 5_200, 7_100, 8_600, 0, 0, 0, 0, 0, 0],
                "activityinfo": [1_200, 2_300, 2_900, 3_100, 3_600, 3_750, 0, 0, 0, 0, 0, 0],
            },
            "population_year": 2025,
            "source": "eTools PRP progress reports and ActivityInfo HPM master indicators, 2026",
        },
        "money": {
            "reserved": 12_400_000.0,
            "disbursed": 7_150_000.0,
            "outstanding": 5_250_000.0,
            "disbursed_percent": 57.7,
            "cost_per_child": 227.7,
            "cost_per_child_previous": 198.2,
            "cost_caveat": "Disbursed to date over the children reached this year: compare sections, not absolute values.",
            "by_section": [
                {
                    "section": "Child Protection",
                    "disbursed": 3_900_000.0,
                    "reserved": 6_000_000.0,
                    "children": 21_000,
                    "cost_per_child": 185.7,
                    "disbursed_percent": 65.0,
                    "achieved_percent": 70.0,
                    "ahead": True,
                },
                {
                    "section": "Education",
                    "disbursed": 2_750_000.0,
                    "reserved": 5_400_000.0,
                    "children": 8_400,
                    "cost_per_child": 327.4,
                    "disbursed_percent": 50.9,
                    "achieved_percent": 35.0,
                    "ahead": False,
                },
                {
                    "section": "WASH",
                    "disbursed": 500_000.0,
                    "reserved": 1_000_000.0,
                    "children": 0,
                    "cost_per_child": None,
                    "disbursed_percent": 50.0,
                    "achieved_percent": None,
                    "ahead": False,
                },
            ],  # fmt: skip
            "by_donor": [
                ["Germany", 4_200_000.0],
                ["European Union", 3_100_000.0],
                ["Japan", 1_900_000.0],
                ["Other", 3_200_000.0],
            ],
            "decisions": [
                {
                    "pd_id": 1,
                    "pd": "LEB/PD2026001",
                    "partner": "Amel Association",
                    "section": "Child Protection",
                    "reason": "ends 15 Sep 2026",
                    "detail": "Ends in 76 days with 70% achieved",
                    "url": "/programmes/1/detail/",
                    "severity": "warning",
                },
                {
                    "pd_id": 2,
                    "pd": "LEB/PD2026002",
                    "partner": "Himaya",
                    "section": "Education",
                    "reason": "under-disbursed",
                    "detail": "65% of the period elapsed, 30% disbursed",
                    "url": "/programmes/2/detail/",
                    "severity": "info",
                },
            ],  # fmt: skip
            "source": "eTools Datamart funds reservations and disbursements, 2026",
        },
        "delivery": {
            "indicators": 24,
            "status_counts": {
                "on_track": 12,
                "off_track": 6,
                "over_target": 2,
                "no_target": 1,
                "not_reported": 3,
            },
            "by_section": [
                {
                    "section": "Child Protection",
                    "on_track": 8,
                    "off_track": 2,
                    "over_target": 1,
                    "no_target": 0,
                    "not_reported": 1,
                    "total": 12,
                },
                {
                    "section": "Education",
                    "on_track": 3,
                    "off_track": 4,
                    "over_target": 1,
                    "no_target": 0,
                    "not_reported": 1,
                    "total": 9,
                },
                {
                    "section": "WASH",
                    "on_track": 1,
                    "off_track": 0,
                    "over_target": 0,
                    "no_target": 1,
                    "not_reported": 1,
                    "total": 3,
                },
            ],  # fmt: skip
            "programme_documents": 18,
            "partners": 15,
            "partners_government": 4,
            "partners_cso": 11,
            "assurance": {
                "field_monitoring_visits": 42,
                "tpm_visits": 31,
                "open_action_points": 27,
                "overdue_high_priority": 4,
                "high_risk_partners": 2,
                "partners_with_pd": 15,
            },
            "findings_by_rating": [["On Track", 30], ["Off Track", 9], ["Not Applicable", 3]],
            "attention": [
                {
                    "severity": "critical",
                    "title": "LEB/PD2026002 · Himaya: 3 children indicators off track",
                    "detail": "Education",
                    "url": "/partner-monitoring/?section=Education&status=off_track",
                    "section": "Education",
                    "children": 9_600,
                },
                {
                    "severity": "warning",
                    "title": "LEB/PD2026003 · Partner C: 2 indicators never reported",
                    "detail": "PD started 150 days ago",
                    "url": "/partner-monitoring/?status=not_reported",
                    "section": "Child Protection",
                    "children": None,
                },
                {
                    "severity": "info",
                    "title": "4 overdue high-priority action points",
                    "detail": "Child Protection",
                    "url": "/action-points/?overdue=1&priority=1",
                    "section": "Child Protection",
                    "children": None,
                },
            ],  # fmt: skip
            "source": "eTools Datamart PD indicators, monitoring findings and action points, 2026",
        },
        "progress": {
            "tpm": {
                "labels": list(MONTHS),
                "planned": [3, 4, 5, 6, 5, 8, 0, 0, 0, 0, 0, 0],
                "completed": [3, 4, 4, 5, 3, 2, 0, 0, 0, 0, 0, 0],
                "overdue": [0, 0, 1, 1, 2, 1, 0, 0, 0, 0, 0, 0],
                "planned_total": 31,
                "completed_total": 21,
                "overdue_total": 5,
                "sites_visited": 27,
                "completion_percent": 67.7,
            },
            "action_points": {
                "labels": list(MONTHS),
                "due": [4, 6, 5, 7, 6, 9, 0, 0, 0, 0, 0, 0],
                "closed": [3, 5, 4, 5, 4, 3, 0, 0, 0, 0, 0, 0],
                "past_due": [1, 2, 3, 5, 7, 13, 0, 0, 0, 0, 0, 0],
                "open_total": 27,
                "high_priority_open": 6,
                "closed_total": 24,
                "due_total": 37,
                "closure_percent": 64.9,
                "median_days_to_close": 18,
                "age": [
                    {"module": "TPM", "under_30": 5, "d30_90": 4, "over_90": 2, "high_priority": 3},
                    {
                        "module": "Field monitoring",
                        "under_30": 6,
                        "d30_90": 3,
                        "over_90": 1,
                        "high_priority": 1,
                    },
                    {"module": "Audit", "under_30": 2, "d30_90": 1, "over_90": 3, "high_priority": 2},
                ],
            },
            "source": "eTools Datamart TPM visits and action points, 2026",
        },
        "freshness": [
            {
                "job": "ai_data",
                "label": "ActivityInfo data import",
                "last_success": None,
                "state": "unknown",
                "state_label": "Never succeeded",
            },
            {
                "job": "etools_datamart",
                "label": "eTools Datamart sync",
                "last_success": None,
                "state": "stale",
                "state_label": "Stale",
            },
            {
                "job": "locations",
                "label": "Locations sync",
                "last_success": None,
                "state": "fresh",
                "state_label": "Fresh",
            },
            {
                "job": "daily_review",
                "label": "Daily AI review",
                "last_success": None,
                "state": "unknown",
                "state_label": "Never succeeded",
            },
        ],  # fmt: skip
        "scope": {"year": today.year, "sections": [], "governorate": "", "today": today.isoformat()},
        "activityinfo": _activityinfo_empty(),
    }


def empty_data(today: datetime.date = FAKE_TODAY) -> dict[str, Any]:
    """What the service returns for a scope with no data at all: zeros, None and empty lists."""
    zeros = [0] * 12
    return {
        "impact": {
            "children_reached": 0,
            "children_etools": 0,
            "children_activityinfo": 0,
            "delta_previous_year": None,
            "by_governorate": [],
            "by_section": [],
            "monthly": {"labels": list(MONTHS), "etools": list(zeros), "activityinfo": list(zeros)},
            "population_year": None,
            "source": "eTools PRP progress reports and ActivityInfo HPM master indicators",
        },
        "money": {
            "reserved": 0.0,
            "disbursed": 0.0,
            "outstanding": 0.0,
            "disbursed_percent": None,
            "cost_per_child": None,
            "cost_per_child_previous": None,
            "cost_caveat": "Disbursed to date over the children reached this year: compare sections, not absolute values.",
            "by_section": [],
            "by_donor": [],
            "decisions": [],
            "source": "eTools Datamart funds reservations",
        },
        "delivery": {
            "indicators": 0,
            "status_counts": dict(STATUS_ZERO),
            "by_section": [],
            "programme_documents": 0,
            "partners": 0,
            "partners_government": 0,
            "partners_cso": 0,
            "assurance": {
                "field_monitoring_visits": 0,
                "tpm_visits": 0,
                "open_action_points": 0,
                "overdue_high_priority": 0,
                "high_risk_partners": 0,
                "partners_with_pd": 0,
            },
            "findings_by_rating": [],
            "attention": [],
            "source": "eTools Datamart",
        },
        "progress": {
            "tpm": {
                "labels": list(MONTHS),
                "planned": list(zeros),
                "completed": list(zeros),
                "overdue": list(zeros),
                "planned_total": 0,
                "completed_total": 0,
                "overdue_total": 0,
                "sites_visited": 0,
                "completion_percent": None,
            },
            "action_points": {
                "labels": list(MONTHS),
                "due": list(zeros),
                "closed": list(zeros),
                "past_due": list(zeros),
                "open_total": 0,
                "high_priority_open": 0,
                "closed_total": 0,
                "due_total": 0,
                "closure_percent": None,
                "median_days_to_close": None,
                "age": [],
            },
            "source": "eTools Datamart",
        },
        "freshness": [],
        "scope": {"year": today.year, "sections": [], "governorate": "", "today": today.isoformat()},
        "activityinfo": _activityinfo_empty(),
    }
