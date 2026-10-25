"""Power BI's "Applied filters" line (Compare with Power BI: datamart.powerbi_filters): every clause shape the
exports show, read in a fixed order; the shapes NeuroDB does not apply; fields and values NeuroDB does not know
kept without their value; reading never raises. Fake values only."""

from __future__ import annotations

import random
import string

import pytest

from neurodb.datamart import powerbi_filters as pf


def _line(*clauses: str) -> str:
    return "Applied filters:\n" + "\n".join(clauses)


def _states(found: pf.Filter) -> list[tuple[str, str]]:
    return [(c.words, c.state) for c in found.clauses]


def test_every_clause_shape_the_exports_show():
    found = pf.parse(
        _line(
            "status is active",
            "reporting_year is 2026",
            "report_status_label is Due, Overdue, Sent back, or Submitted",
            "actual_value is less than or equal to 0 or is blank",
            "location_value is greater than 0",
            "is_period_latest is True",
            "is_blueprint_canonical is True",
            "country_name is Lebanon",
            "section_name is Section Alpha",
        ),
        sections=["Section Alpha", "Section Beta"],
    )
    assert found.pd_statuses == ["active"] and found.years == [2026]
    assert found.report_statuses == ["Due", "Overdue", "Sent back", "Submitted"]
    assert found.actual_value == {"op": "<=", "value": 0.0, "or_blank": True}
    assert found.location_value == {"op": ">", "value": 0.0, "or_blank": False}
    assert found.period_latest is True and found.canonical is True and found.country_ok is True
    assert found.sections == ["Section Alpha"]
    assert _states(found) == [
        ("status is active", "applied"),
        ("reporting_year is 2026", "applied"),
        ("report_status_label is Due, Overdue, Sent back or Submitted", "applied"),
        ("actual_value is less than or equal to 0 or is blank", "applied"),
        ("location_value is greater than 0", "applied"),
        ("is_period_latest is True", "as_read"),
        ("is_blueprint_canonical is True", "as_read"),
        ("country_name is Lebanon", "applied"),
        ("section_name is Section Alpha", "applied"),
    ]
    assert found.others == []


def test_less_than_or_equal_to_or_is_blank_is_never_read_as_less_than():
    found = pf.parse(_line("actual_value is less than or equal to 5 or is blank"))
    assert found.actual_value == {"op": "<=", "value": 5.0, "or_blank": True}
    found = pf.parse(_line("actual_value is less than 5"))
    assert found.actual_value == {"op": "<", "value": 5.0, "or_blank": False}


def test_a_value_holding_a_comma_is_never_split_and_blank_is_read():
    sections = ["Section Alpha, Beta and Gamma", "Section Delta"]
    found = pf.parse(_line("section_name is Section Alpha, Beta and Gamma or Section Delta"), sections)
    assert found.sections == ["Section Alpha, Beta and Gamma", "Section Delta"]
    found = pf.parse(_line("report_status_label is Accepted or (Blank)"))
    assert found.report_statuses == ["Accepted"] and found.clauses[0].values == ["Accepted", None]


def test_a_pd_status_reads_as_neurodb_s_value_whatever_its_case():
    assert pf.parse(_line("status is Active or ended")).pd_statuses == ["active", "ended"]


@pytest.mark.parametrize(
    "clause",
    [
        "status is not active",
        "section_name contains Alpha",
        "section_name does not start with Alpha",
        "reporting_year is on or after 2025",
        "reporting_year is after 2025 and is before 2027",
        "status is blank",
        "location_value is less than 0 or is blank",
    ],
)
def test_shapes_neurodb_does_not_apply_keep_no_value(clause):
    found = pf.parse(_line(clause))
    [only] = found.clauses
    assert only.state == "cannot" and only.values == [] and found.others == [only]
    assert "Alpha" not in only.words and "2025" not in only.words and "active" not in only.words


def test_a_field_neurodb_does_not_know_keeps_its_name_but_never_its_value():
    found = pf.parse(_line("name is Zq Unknownperson", "focal_point is jane.doe@example.invalid"))
    assert _states(found) == [("name is …", "unknown_field"), ("focal_point is …", "unknown_field")]
    assert "Unknownperson" not in repr(found.to_json()) and "example.invalid" not in repr(found.to_json())


def test_a_value_neurodb_does_not_know_is_not_kept():
    found = pf.parse(_line("section_name is Section Nowhere"), sections=["Section Alpha"])
    assert _states(found) == [("section_name is …", "unknown_value")] and found.sections == []
    assert "Nowhere" not in repr(found.to_json())


def test_a_clause_neurodb_cannot_read_keeps_no_text():
    found = pf.parse(_line("Zq Fakeperson wrote this", "Section Alpha"))
    assert _states(found) == [("", "unread"), ("", "unread")]
    assert "Fakeperson" not in repr(found.to_json())


def test_another_country_is_said_without_its_name():
    found = pf.parse(_line("country_name is Fakeland"))
    assert found.country_ok is False and _states(found) == [("country_name is …", "other_country")]
    assert "Fakeland" not in repr(found.to_json())


def test_reading_never_raises():
    rng = random.Random(7)
    for _ in range(300):
        text = "".join(rng.choice(string.printable) for _ in range(rng.randint(0, 80)))
        pf.parse(text)
        pf.parse(_line(text))
    assert pf.parse(None).clauses == [] and pf.parse("").clauses == []
    assert pf.parse("status is active").clauses == []  # no "Applied filters:" first
    assert pf.parse(_line("", "  ")).clauses == []


def test_a_slicer_field_added_to_the_known_fields_is_applied(monkeypatch):
    monkeypatch.setitem(pf.FIELDS, "fake_year_field", "year")
    found = pf.parse(_line("fake_year_field is 2025"))
    assert _states(found) == [("fake_year_field is 2025", "applied")] and found.years == [2025]


def test_the_filter_round_trips_through_json():
    found = pf.parse(_line("status is active", "name is Zq Unknownperson", "reporting_year is 2025 or 2026"))
    again = pf.Filter.from_json(found.to_json())
    assert again == found and again.years == [2025, 2026] and len(again.others) == 1
