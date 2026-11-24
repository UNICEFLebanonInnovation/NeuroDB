"""Machine learning readiness after NeuroDB's own grouping of PD indicators was removed (the owner's decision: eTools
has no such grouping). The comparable groups of the later analysis "unusual results across comparable PDs" are the
country programme outputs: the running PDs that serve one output (``PCA.cp_outputs``, read as the CPD links read
an output: ``cpd.services._norm``), each PD once per output. The two measures of the grouping are gone, and a check
stored before still shows without them.

Before the removal the check measured ``links.indicators_master`` and ``links.master_groups`` and had the threshold
``MASTER_LINKED``; none of these tests would pass then."""

from __future__ import annotations

import datetime
import json
import re

import pytest

from neurodb.insights import readiness
from neurodb.partnerships.models import PCA, PartnerOrganization

day = datetime.date
TODAY = day(2026, 10, 15)
GROUPING = re.compile(r"(?i)master")


@pytest.fixture
def partner(db):
    return PartnerOrganization.objects.create(etl_id="9001", name="Example Partner One", vendor_number="EX1")


def _pd(partner, n: int, status: str, outputs: list[str] | None) -> PCA:
    return PCA.objects.create(
        etl_id=str(9100 + n),
        number=f"EX/PCA2026{n:03}/PD2026{n:03}",
        title=f"Example programme {n}",
        status=status,
        partner=partner,
        start=day(2025, 1, 1),
        end=day(2027, 12, 31),
        cp_outputs=outputs,
    )


def _metric(result: dict, key: str) -> dict:
    return next(m for s in result["sections"] for m in s["metrics"] if f"{s['key']}.{m['key']}" == key)


def test_the_grouping_threshold_and_measures_are_gone():
    assert not hasattr(readiness, "MASTER_LINKED")
    assert readiness.RETIRED_METRICS == {"links.indicators_master", "links.master_groups"}
    assert "links.output_groups" in readiness.HEADLINE
    assert not any(GROUPING.search(key) for key in readiness.HEADLINE)


def test_the_comparable_groups_are_the_outputs_running_pds_serve(partner, monkeypatch):
    monkeypatch.setattr(readiness, "PDS_PER_GROUP", 2)
    _pd(partner, 1, "active", ["Output 1.1: Education"])
    _pd(partner, 2, "signed", ["output 1.1 education"])  # the same output, written another way
    _pd(partner, 3, "suspended", ["Output 1.2: Education", "Output 2.1: Protection"])  # counts in both
    _pd(partner, 4, "draft", ["Output 1.2: Education"])  # not running: in no group
    _pd(partner, 5, "ended", ["Output 1.2: Education", "Output 2.1: Protection"])
    _pd(partner, 6, "active", None)  # no output in eTools
    _pd(partner, 7, "active", [""])
    result = readiness.measure(TODAY)
    found = _metric(result, "links.output_groups")
    assert found["value"] == 1  # output 1.1 (two running PDs); 1.2 and 2.1 have one each, "1.1" is not "1.2"
    assert found["label"] == "Country programme outputs that 2 or more running PDs serve"
    assert "a PD with several outputs counts in each" in found["note"]
    _pd(partner, 8, "active", ["OUTPUT 2.1 - Protection"])
    assert _metric(readiness.measure(TODAY), "links.output_groups")["value"] == 2  # 2.1: PDs 3 and 8


def test_a_measured_check_says_nothing_of_the_grouping(partner):
    _pd(partner, 1, "active", ["Output 1.1: Education"])
    shown = readiness.shown(readiness.measure(TODAY))
    links = next(s for s in shown["sections"] if s["key"] == "links")
    assert "output_groups" in {m["key"] for m in links["metrics"]}
    for section in shown["sections"]:
        for m in section["metrics"]:
            assert not GROUPING.search(" ".join(str(m.get(k) or "") for k in ("key", "label", "note"))), m
    unusual = next(v for v in shown["verdicts"] if v["key"] == "unusual")
    assert not GROUPING.search(json.dumps(unusual)), unusual
    assert "country programme output" in unusual["method"]
    assert unusual["status"] == "not_yet" and "country programme outputs" in unusual["reason"]


def test_a_check_stored_before_shows_without_the_grouping_measures(db):
    stored = {
        "measured_at": "2026-10-12T05:00:00+00:00",
        "sections": [
            {
                "key": "links",
                "label": "How the sources link up",
                "error": "",
                "metrics": [
                    {"key": "funds_pd", "label": "Funds", "value": 0.5, "display": "50%", "note": ""},
                    {"key": "indicators_master", "label": "Old", "value": 0.7, "display": "70%", "note": ""},
                    {"key": "master_groups", "label": "Old groups", "value": 9, "display": "9", "note": ""},
                ],
            }
        ],
        "verdicts": [{"key": "unusual", "question": "?", "method": "", "status": "ready", "checks": []}],
    }
    shown = readiness.shown(stored)
    (links,) = shown["sections"]
    assert [m["key"] for m in links["metrics"]] == ["funds_pd"]
    unusual = next(v for v in shown["verdicts"] if v["key"] == "unusual")
    # worked out again from the measures left: the groups were not measured then, never "ready" from the old ones
    assert unusual["status"] != "ready" and not GROUPING.search(json.dumps(unusual))
