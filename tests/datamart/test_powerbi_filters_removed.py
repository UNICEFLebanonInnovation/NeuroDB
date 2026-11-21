"""Compare with Power BI once office, emergency, output and partner type left every page (the owner's decision of
21 Nov 2026). The comparison reads NeuroDB's figures through the pages' own functions, for Power BI's own filter
(PD status, year, sections): none of the four was ever among them, and the columns that carry them (Power BI's
Emergency indicator, Office, CP Output, Partner Type) are still compared. On a fixture the results are those the
comparison gave before the change, line for line, and the NeuroDB page each export links to carries none of the
four.

On the world of tests/datamart/powerbi_factory.py (one active programme document of a national CSO, in Office
North, its CP outputs "Output One" and "Output, Two"), with exports that differ from it on each of the four.
"""

# ruff: noqa: F401, F811 (the fixtures come from the module that defines them)
from __future__ import annotations

import datetime

import pytest

from neurodb.datamart import powerbi_results
from neurodb.datamart.models import PowerBIDifference, PowerBIExport, PowerBIUpload
from tests.datamart import powerbi_factory as pf
from tests.datamart.test_powerbi_compare import (
    ACTIVE,
    PARTNER,
    PD,
    SECTIONS,
    checker,
    history,
    started,
    world,
)

pytestmark = pytest.mark.django_db
D = datetime.date
REMOVED = ("office", "emergency", "partner_type", "cp_output")
PP_FILTERS = (*ACTIVE, "is_period_latest is True", "report_status_label is Accepted")


def _pp(**changes):
    row = {
        "section": SECTIONS,
        "cp_output": "Output One, Output, Two",
        "emergency": "No",
        "target": 100,
        "reached": 35,
        "achieved": 0.35,
        "partner": PARTNER,
        "indicator_title": "Fake indicator 1",
    }
    return pf.row("program_performance", **{**row, **changes})


def _pd_indicator(**changes):
    row = {
        "pd": PD, "pd_status": "Active", "partner": PARTNER, "document_type": "PD", "pd_title": "Fake programme 1",
        "start": D(2026, 1, 1), "end": D(2026, 12, 31), "offices": "Office North", "section": SECTIONS,
        "cp_output": "Output One", "indicator_title": "Fake indicator 1", "target": 100, "reached": 35,
    }  # fmt: skip
    return pf.row("pd_indicators", **{**row, **changes})


def _files():
    return [
        (
            "program_performance.xlsx",
            pf.export(
                "program_performance",
                [
                    _pp(emergency="Yes"),  # NeuroDB: the PD is no SPD
                    _pp(
                        indicator_title="Fake percentage 2",
                        target=40,
                        reached=25,
                        achieved=0.625,
                        cp_output="Output Nine",
                    ),  # fmt: skip
                ],
                filters=PP_FILTERS,
            ),
        ),
        (
            "pd_indicators.xlsx",
            pf.export(
                "pd_indicators",
                [
                    _pd_indicator(offices="Office South"),
                    _pd_indicator(
                        indicator_title="Fake percentage 2",
                        target=40,
                        reached=25,
                        section="Section Alpha",
                        cp_output="Output Nine",
                    ),  # fmt: skip
                ],
                filters=(*ACTIVE, "is_blueprint_canonical is True"),
            ),
        ),
        (
            "fund_monitoring.xlsx",
            pf.export(
                "fund_monitoring",
                [
                    pf.row(
                        "fund_monitoring",
                        pd=PD,
                        unicef_planned=9000.0,
                        cash=10000,
                        partner_contribution=1000,
                        partner=PARTNER,
                        partner_type="Government",
                        currency="USD",
                        partner_share=0.1,
                    )  # fmt: skip
                ],
                filters=ACTIVE,
                drop=("financial_contribution",),
            ),
        ),
    ]


def _results(upload):
    out = {}
    for export in PowerBIExport.objects.filter(upload=upload).order_by("kind"):
        lines = PowerBIDifference.objects.filter(export=export).order_by("n")
        out[export.kind] = {
            "state": export.state,
            "verdict": export.verdict,
            "same": export.counts.get("same"),
            "lines": [(line.kind, line.reason, line.rule, line.found) for line in lines],
        }
    return out


# the comparison's results on this fixture before the four filters left the pages (read at bd95042f)
EXPECTED = {
    "fund_monitoring": {
        "state": "compared",
        "verdict": "look",
        "same": 0,
        "lines": [
            ("different", "unknown", "", {"partner_type": ["Government", "Civil Society Organization", None]})
        ],
    },
    "pd_indicators": {
        "state": "compared",
        "verdict": "look",
        "same": 0,
        "lines": [
            ("different", "unknown", "", {"offices": ["Office South", "office north", None]}),
            ("different", "unknown", "", {"cp_output": ["Output Nine", "Output One", None]}),
        ],
    },
    "program_performance": {
        "state": "compared",
        "verdict": "look",
        "same": 0,
        "lines": [
            ("different", "unknown", "", {"emergency": ["Yes", "No", None]}),
            ("different", "unknown", "", {"cp_output": ["Output Nine", "Output One, Output, Two", None]}),
        ],
    },
}


def test_compare_with_power_bi_gives_the_results_it_gave(checker, world):
    upload = pf.compared(checker, *_files())
    assert upload.status == PowerBIUpload.Status.DONE, upload.error
    assert _results(upload) == EXPECTED


def test_the_neurodb_page_of_each_export_carries_none_of_the_four(checker, world):
    upload = pf.compared(checker, *_files())
    for export in PowerBIExport.objects.filter(upload=upload):
        url = powerbi_results.neurodb_url(export)
        assert url, export.kind
        for key in REMOVED:
            assert f"{key}=" not in url, (export.kind, url)
