"""Release 2, A1: the real eTools field names of the FMM export (the FMS user manual, §13.2) on the
finding rows: listed first in Fields found, read by the build (semicolon lists, modality, programme
areas, coordinates, a P-code), and the HACT answers written on a row taking precedence over the
checklist answers of the same question, never kept as text."""

from __future__ import annotations

import datetime
import json

import pytest
from django.urls import reverse

from neurodb.core.models import SyncRun
from neurodb.datamart import fm
from neurodb.datamart import models as dm
from neurodb.fmm import fields, place, refresh, rules
from neurodb.fmm.models import RecordRuleResult, Visit, VisitEntity

from .conftest import CANARY_TEXT

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)

ETOOLS_NAMES = {  # logical field -> the eTools FMM export's name, which must come first
    "q1_answer": "hact_q1_answer",
    "q2_answer": "hact_q2_answer",
    "q3_answer": "hact_q3_answer",
    "offices": "field_offices",
    "sections": "sections_names",
    "programme_areas": "programme_areas",
    "modality": "monitoring_modality",
    "latitude": "location_lat",
    "longitude": "location_lon",
    "location_type": "location_type",
    "location_pcode": "location_pcode",
    "location_name": "location_name",
    "team": "team_members",
    "visit_goals": "visit_goals",
    "objective": "objective",
    "supplies": "dim_supplies",
    "psea": "dim_psea",
    "status": "status",
}


def _update(row: dm.MonitoringFinding, **extra) -> None:
    """Add keys to a finding row's record, as the FMM export writes them."""
    data = dict(row.data or {})
    data.update(extra)
    dm.MonitoringFinding.objects.filter(pk=row.pk).update(data=data)


def _refresh() -> SyncRun:
    run = refresh.run(triggered_by="test", today=TODAY)
    assert run.status == SyncRun.Status.SUCCEEDED, run.error
    return run


def _result(key: str, rule: str) -> RecordRuleResult:
    """The visit's result of ``rule`` as its records read it: its first failing record's, else its first
    record's (a visit keeps no rule results of its own)."""
    rows = list(
        RecordRuleResult.objects.filter(entity__visit__key=key, rule=rule).order_by("entity__datamart_id")
    )
    return next((r for r in rows if r.status == "fail"), rows[0])


# ------------------------------------------------------------------------------------------ names
@pytest.mark.parametrize("field, name", sorted(ETOOLS_NAMES.items()))
def test_the_etools_export_names_come_first(field, name):
    assert fields.CANDIDATES["field_monitoring"][field][0] == name


@pytest.mark.parametrize(
    "entity_type, kind",
    [("PD/SSFA", "pd"), ("CP Output", "cp_output"), ("Partner", "partner"), ("pd/ssfa", "pd")],
)
def test_the_export_entity_types_map_to_their_kinds(entity_type, kind):
    assert fm.entity_kind(entity_type, "anything") == kind


# ------------------------------------------------------------------------------------------ the build
@pytest.fixture
def exported(fm_world):
    """``fm_world`` with the FMM export's keys on the rows of visits 1722, 1723 and 1727, and every
    row's P-code written flat, as the export writes it."""
    rows = fm_world.rows
    for row in dm.MonitoringFinding.objects.all():
        location = (row.data or {}).get("location") or {}
        if location.get("p_code"):
            dm.MonitoringFinding.objects.filter(pk=row.pk).update(
                data={**row.data, "location_pcode": location["p_code"]}
            )
    for row in rows.values():
        row.refresh_from_db()
    common = {
        "field_offices": "Zahle; Tripoli",
        "sections_names": "Education; WASH",
        "programme_areas": "Learning; Water",
        "monitoring_modality": "TPM - iAPS",
        "visit_goals": "Check the learning sessions.",
        "objective": "Verify attendance.",
    }
    # 1722: the PD row says Q1 Off track in its own answer (the checklist says On track)
    _update(
        rows[101],
        **common,
        hact_q1_answer="Off-track",
        hact_q2_answer="Observed two BLN classes and checked the registers.",
        hact_q3_answer="Registers checked; the partner will replace the missing kits by the end of the month.",
        location_lat=33.851,
        location_lon=35.910,
        location_type="Admin level 3",
        dim_supplies="Yes",
    )
    _update(rows[102], **common, hact_q1_answer="", hact_q2_answer="", hact_q3_answer="")
    _update(rows[103], **common, hact_q1_answer="", hact_q2_answer="", hact_q3_answer="")
    # 1727: no checklist Q2 or Q3; its row leaves Q2 blank and writes a placeholder in Q3
    _update(rows[131], hact_q1_answer="On track", hact_q2_answer="", hact_q3_answer="No comment.")
    # 1723: a row whose location eTools did not link, written with its P-code
    dm.MonitoringFinding.objects.filter(pk=rows[111].pk).update(location=None)
    _update(
        rows[111],
        location=None,
        location_pcode=fm_world.cadasters["saadnayel"].p_code,
        hact_q1_answer=CANARY_TEXT,
    )
    dm.MonitoringFinding.objects.filter(pk=rows[112].pk).update(location=None)
    _update(rows[112], location=None, location_pcode="")
    return fm_world


def test_lists_written_with_semicolons_are_split(exported):
    _refresh()
    visit = Visit.objects.get(key="1722")
    assert visit.offices == ["Zahle", "Tripoli"] and visit.offices_from == "activity"
    assert visit.section_names == ["Education", "WASH"]
    assert visit.programme_areas == ["Learning", "Water"]
    assert visit.modality == "TPM - iAPS"
    assert Visit.objects.get(key="1726").modality == ""


def test_written_coordinates_place_the_visit(exported):
    run = _refresh()
    visit = Visit.objects.get(key="1722")
    assert (visit.latitude, visit.longitude) == (33.851, 35.910)
    assert visit.located_by == "location" and visit.point_precise  # location_type: the lowest level
    assert run.details["location"]["written"] == 1


def test_a_row_without_a_linked_location_is_placed_by_its_pcode(exported):
    run = _refresh()
    visit = Visit.objects.get(key="1723")
    assert visit.location_id == exported.cadasters["saadnayel"].pk
    assert visit.governorate_name == "Bekaa"
    assert run.details["location"]["by_pcode"] == 1


def test_row_answers_are_measured_never_kept_as_text(exported):
    _refresh()
    entity = VisitEntity.objects.get(finding_id=exported.rows[101].pk)
    q1, q2, q3 = (entity.row_answers[k] for k in ("q1", "q2", "q3"))
    assert q1["answered"] and q1["rating"] == "off_track"
    assert q2["answered"] and q2["words"] == 8
    assert q3["answered"] and q3["short"] == ""  # a long answer keeps no hash
    stored = json.dumps(list(VisitEntity.objects.values_list("row_answers", flat=True)))
    for text in ("Registers", "BLN", "Karim", "example.org", "Off-track"):
        assert text not in stored


def test_a_row_q1_takes_precedence_over_the_checklist_answer(exported):
    _refresh()
    entity = VisitEntity.objects.get(finding_id=exported.rows[101].pk)
    assert (entity.hact_q1, entity.hact_q1_from) == ("off_track", "entity")
    # the visit's worst Q1, which R1 reads as answered on this row
    assert Visit.objects.get(key="1722").hact_q1 == "off_track"


def test_a_blank_row_answer_falls_back_to_the_checklist(exported):
    _refresh()
    # 102 (the CP output) leaves Q1 blank: its checklist answer "3" (Off track) still counts
    entity = VisitEntity.objects.get(finding_id=exported.rows[102].pk)
    assert entity.hact_q1 == "off_track"


def test_rules_read_the_row_answers(exported):
    _refresh()
    # 1727 has no checklist Q2: its row writes Q2 blank, so R1 finds it missing (field 3, Q2)
    r1 = _result("1727", "R1")
    assert r1.status == "fail" and r1.detail_key == "missing:0,3"
    assert "Q2 – Activities monitored" in r1.detail
    # 1722: Q2 written on the row; one of its rows has no Q1 (field 2)
    assert _result("1722", "R1").detail_key == "missing:2"


def test_r2_counts_checklist_questions_only():
    answers = [
        rules.AnswerFacts("12", "q1", True, False, 2),
        rules.AnswerFacts("13", "", False, False, 0),
        rules.AnswerFacts("row:q2", "q2", True, False, 9, applies_to="entity", entity=0, from_row=True),
    ]
    assert rules.questions_answered(answers) == (2, 1)


def test_the_visit_page_shows_the_row_answers_and_modality(exported, client_viewer):
    _refresh()
    html = client_viewer.get(reverse("fmm:visit", args=["1722"])).content.decode()
    assert "TPM - iAPS" in html and "Learning, Water" in html
    assert "Observed two BLN classes" in html and "Answers on this record" in html
    assert "Verify attendance." in html  # the visit's objective, once
    # an answer naming someone shows the name to staff, never an e-mail address
    html = client_viewer.get(reverse("fmm:visit", args=["1723"])).content.decode()
    assert "karim.canary@example.org" not in html


# ------------------------------------------------------------------------------------------ precision
GAZETTEER = {
    1: {"id": 1, "latitude": 33.8, "longitude": 35.9, "parent_id": None, "type__admin_level": 2},
    2: {"id": 2, "latitude": None, "longitude": None, "parent_id": 1, "type__admin_level": 3},
    3: {"id": 3, "latitude": 33.81, "longitude": 35.88, "parent_id": 1, "type__admin_level": 3},
}


@pytest.mark.parametrize(
    "point, kind, node, precise",
    [
        ((33.8, 35.9), "Cadaster", 1, True),  # the type is the lowest level's
        ((33.8, 35.9), "admin3", 1, True),
        ((33.8, 35.9), "District", 1, False),  # the district's own centre
        ((33.8, 35.9), "District", 2, False),  # the centre borrowed from its district
        ((33.85, 35.95), "District", 2, True),  # 7 km away from any centre
        ((33.81, 35.88), "", 3, True),  # a cadaster's own point
        ((33.81, 35.88), "", None, False),  # nothing to compare with
    ],
)
def test_written_points_are_precise_by_type_or_distance(point, kind, node, precise):
    found = place.written_point_precise(
        point, kind, GAZETTEER.get(node), GAZETTEER, 3, frozenset({"cadaster"})
    )
    assert found is precise
