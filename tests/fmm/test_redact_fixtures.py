"""``fmm_redact_fixtures``: recorded field monitoring samples are made safe to commit (people replaced
by "Person N", texts cleaned and cut) before anyone commits them."""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from neurodb.fmm.management.commands.fmm_redact_fixtures import TEXT_CHARS

from .conftest import CANARIES, CANARY_TEXT, KEPT, LEAD, MEMBER, MEMBER_EMAIL

pytestmark = pytest.mark.django_db


def _write(folder, dataset, results):
    path = folder / f"{dataset}.json"
    sample = {
        "dataset": dataset,
        "path": "x/",
        "params": {},
        "country": True,
        "recorded": "now",
        "results": results,
    }
    path.write_text(json.dumps(sample, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _run(*args) -> str:
    out = StringIO()
    call_command("fmm_redact_fixtures", *[str(a) for a in args], stdout=out)
    return out.getvalue()


@pytest.fixture
def recorded(tmp_path):
    findings = _write(
        tmp_path,
        "field_monitoring",
        [
            {
                "id": 1,
                "monitoring_activity_id": 1722,
                "visit_lead": LEAD,
                "team_members": [{"name": MEMBER, "id": 77}, {"name": LEAD}],
                "narrative_finding": CANARY_TEXT + " " + "More detail. " * 40,
                "overall_finding_rating": "On Track",
                "monitoring_activity_end_date": "2026-05-11",
                "is_programmatic_visit": True,
            }
        ],
    )
    questions = _write(
        tmp_path,
        "fm_questions",
        [
            {
                "id": 50001,
                "answer": "Yes",
                "summary": f"{MEMBER} met the staff on 2026-05-11.",
                "person_responsible": MEMBER,
            },
            {"id": 50002, "answer": "Line one\nline two", "summary": ""},
        ],
    )
    partners = _write(tmp_path, "partners", [{"id": 1, "name": "Amel Association", "focal": LEAD}])
    return tmp_path, findings, questions, partners


def test_people_become_numbered_persons_and_texts_are_cleaned(recorded):
    folder, findings, questions, partners = recorded
    before = partners.read_text(encoding="utf-8")
    out = _run(folder)
    assert "field_monitoring.json: 1 records" in out and "fm_questions.json: 2 records" in out
    record = json.loads(findings.read_text(encoding="utf-8"))["results"][0]
    # numbered in the order the file lists them (its keys are sorted: the team before the lead)
    assert record["team_members"] == [{"name": "Person 1", "id": 77}, {"name": "Person 2"}]
    assert record["visit_lead"] == "Person 2"
    assert len(record["narrative_finding"]) == TEXT_CHARS
    assert record["overall_finding_rating"] == "On Track" and record["monitoring_activity_id"] == 1722
    assert record["is_programmatic_visit"] is True and record["monitoring_activity_end_date"] == "2026-05-11"
    first, second = json.loads(questions.read_text(encoding="utf-8"))["results"]
    assert first["person_responsible"] == "Person 1"  # the same person keeps the same number
    assert (
        first["summary"] == "[name withheld] met the staff on 2026-05-11."
    )  # a name learnt from a person key
    assert second["answer"] == "Line one\nline two"  # nothing to remove: left exactly as it was
    everything = findings.read_text(encoding="utf-8") + questions.read_text(encoding="utf-8")
    for canary in (*CANARIES, MEMBER_EMAIL):
        assert canary not in everything, canary
    for kept in KEPT[:1]:
        assert kept in everything
    assert partners.read_text(encoding="utf-8") == before  # not a dataset of Monitoring insights


def test_running_it_twice_changes_nothing_more(recorded):
    folder, findings, questions, _ = recorded
    _run(folder)
    once = findings.read_text(encoding="utf-8") + questions.read_text(encoding="utf-8")
    _run(folder)
    assert findings.read_text(encoding="utf-8") + questions.read_text(encoding="utf-8") == once


def test_a_file_named_is_rewritten_and_a_dry_run_writes_nothing(recorded):
    folder, findings, _, partners = recorded
    before = findings.read_text(encoding="utf-8")
    assert "(dry run: not written)" in _run(findings, "--dry-run")
    assert findings.read_text(encoding="utf-8") == before
    _run(partners)
    assert json.loads(partners.read_text(encoding="utf-8"))["results"][0]["focal"] == "Person 1"
    with pytest.raises(CommandError, match="does not exist"):
        _run(folder / "missing.json")


def test_an_empty_folder_says_so(tmp_path):
    assert "No recorded field monitoring sample" in _run(tmp_path)
