"""Key-tolerant reading of the eTools field monitoring records: the probe of their keys, the choice of
the key of each field (Fields found), and the reading of values and answers whatever their shape."""

import re
from pathlib import Path

import pytest
from django.conf import settings

from neurodb.datamart import models as dm
from neurodb.fmm import fields, parse, privacy, refresh
from neurodb.fmm.models import FieldMapping, KeyProbe, QuestionAnswer, Visit

from . import conftest
from .conftest import CANARIES, MEMBER, MEMBER_EMAIL

pytestmark = pytest.mark.django_db

FMM = Path(settings.BASE_DIR) / "neurodb" / "fmm"
State = FieldMapping.State


def _probe():
    return refresh.run(triggered_by="test", probe_only=True)


def _chosen(dataset: str) -> dict[str, str]:
    return {m.field: m.chosen_key for m in FieldMapping.objects.filter(dataset=dataset)}


def _states(dataset: str) -> dict[str, str]:
    return {m.field: m.state for m in FieldMapping.objects.filter(dataset=dataset)}


# ------------------------------------------------------------------------------------------ shapes
def test_shape_a_gives_its_flat_keys(fm_questions_variant):
    fm_questions_variant("A")
    _probe()
    chosen = _chosen("fm_questions")
    assert chosen["activity_id"] == "monitoring_activity_id"
    assert chosen["activity_ref"] == "monitoring_activity"
    assert chosen["question_id"] == "question_id"
    assert chosen["question_text"] == "question_text"
    assert chosen["answer"] == "answer" and chosen["summary"] == "summary"
    assert chosen["is_hact"] == "is_hact" and chosen["order"] == "order"
    assert chosen["entity"] == "entity" and chosen["entity_type"] == "entity_type"
    assert chosen["method"] == "method"
    assert _states("fm_questions")["answer_label"] == State.MISSING
    assert _chosen("fm_options") == {"question_id": "question_id", "value": "value", "label": "label"}


def test_shape_b_gives_its_other_names(fm_questions_variant):
    fm_questions_variant("B")
    _probe()
    chosen, states = _chosen("fm_questions"), _states("fm_questions")
    assert chosen["activity_id"] == "activity_id"
    assert chosen["activity_ref"] == "reference_number"
    # "question" holds the text: it is no id, so the question has no id here
    assert states["question_id"] == State.MISSING and chosen["question_text"] == "question"
    assert chosen["answer"] == "value" and chosen["summary"] == "specific_details"
    assert chosen["entity"] == "related_to" and chosen["entity_type"] == "level"
    assert _chosen("fm_options")["question_id"] == "question"


def test_shape_c_gives_its_nested_keys(fm_questions_variant):
    fm_questions_variant("C")
    _probe()
    chosen = _chosen("fm_questions")
    assert chosen["activity_id"] == "monitoring_activity.id"
    assert chosen["activity_ref"] == "monitoring_activity"  # an object: its reference_number is read
    assert chosen["question_id"] == "question.id"
    assert chosen["question_text"] == "question.text"
    assert chosen["answer"] == "answer" and chosen["answer_label"] == "answer.label"
    assert chosen["is_hact"] == "question.is_hact"
    assert _chosen("fm_options")["question_id"] == "question.id"


def test_shape_d_finds_nothing_and_says_so(fm_questions_variant):
    fm_questions_variant("D")
    run = _probe()
    assert set(_states("fm_questions").values()) == {State.MISSING}
    assert all(fields.key_for("fm_questions", f) is None for f in fields.CANDIDATES["fm_questions"])
    assert not fields.available("fm_questions", "answer")
    not_found = run.details["fields_not_found"]
    for field in ("answer", "question_text", "activity_id", "summary"):
        assert f"fm_questions.{field}" in not_found
    questions = run.details["questions"]
    assert questions["answers_found"] is False and questions["answered_share"] is None
    assert questions["unanswered_seen"] is False


def test_shape_d_leaves_r2_r3_and_r5_not_available_not_failed(fm_world, fm_questions_variant):
    """Nothing read from the checklist answers: the rule that needs them (R2) says "not available", it
    does not flag the visits (nor take its missing-value deduction); the AI checks R3 and R5 are off
    while none is made."""
    from neurodb.fmm.models import VisitRuleResult

    fm_questions_variant("D")
    run = refresh.run(triggered_by="test")
    assert not QuestionAnswer.objects.exists() and "fm_questions.answer" in run.details["fields_not_found"]
    results = VisitRuleResult.objects.filter(rule__in=("R2", "R3", "R5"), visit__status_group="reported")
    assert set(results.filter(rule="R2").values_list("status", flat=True)) == {"na"}
    assert set(results.exclude(rule="R2").values_list("status", flat=True)) == {"off"}
    assert not results.filter(status="fail").exists()
    assert Visit.objects.filter(status_group="reported").exclude(quality_score=None).exists()  # R1


def test_the_probe_measures_the_answers_given(fm_questions_variant):
    fm_questions_variant("A")
    questions = _probe().details["questions"]
    # 6 records: one left blank and one "n/a" are not answered
    assert questions["records"] == 6 and questions["answered"] == 4 and questions["unanswered"] == 2
    assert questions["answered_share"] == round(4 / 6, 4) and questions["unanswered_seen"] is True


def test_no_unanswered_record_is_seen_when_every_answer_is_given(fm_questions_variant):
    for doc in fm_questions_variant("C"):
        if not doc.data["answer"]["value"] or doc.data["answer"]["value"] == "n/a":
            doc.data["answer"] = {"value": "Yes", "label": "Yes"}
            doc.save()
    questions = _probe().details["questions"]
    assert questions["unanswered_seen"] is False and questions["answered_share"] == 1.0


# ------------------------------------------------------------------------------------------ choosing
def _figures(total: int, **usable: int) -> dict:
    """Probe figures: ``usable`` maps key -> records with a usable text value."""
    return {
        "total": total,
        "keys": {key: n for key, n in usable.items()},
        "usable": {(key, "text"): n for key, n in usable.items()},
    }


def test_the_first_listed_key_that_fills_half_the_records_wins():
    mapping = fields.resolve("fm_questions", "answer", _figures(10, answer=6, value=8), None)
    assert (mapping.chosen_key, mapping.state, mapping.coverage) == ("answer", State.FOUND, 0.6)
    assert mapping.candidates == [{"key": "answer", "coverage": 0.6}, {"key": "value", "coverage": 0.8}]


def test_a_much_fuller_later_key_makes_the_choice_ambiguous():
    mapping = fields.resolve("fm_questions", "answer", _figures(10, answer=5, value=8), None)
    assert (mapping.chosen_key, mapping.state) == ("answer", State.AMBIGUOUS)


def test_below_half_the_fullest_key_is_taken_and_none_is_missing():
    mapping = fields.resolve("fm_questions", "answer", _figures(10, answer=1, value=3), None)
    assert (mapping.chosen_key, mapping.state) == ("value", State.FOUND)
    mapping = fields.resolve("fm_questions", "answer", _figures(10, other=10), None)
    assert (mapping.chosen_key, mapping.state, mapping.candidates) == ("", State.MISSING, [])
    mapping = fields.resolve("fm_questions", "answer", _figures(0), None)
    assert mapping.state == State.MISSING


def test_the_minimum_coverage_is_a_setting(settings):
    settings.FMM_KEY_MIN_COVERAGE = 0.7
    mapping = fields.resolve("fm_questions", "answer", _figures(10, answer=6, value=9), None)
    assert mapping.chosen_key == "value"


def test_an_override_the_data_shows_wins():
    current = FieldMapping(dataset="fm_questions", field="answer", override_key="response")
    mapping = fields.resolve("fm_questions", "answer", _figures(10, answer=9, response=2), current)
    assert (mapping.chosen_key, mapping.state, mapping.coverage) == ("response", State.OVERRIDE, 0.2)
    assert mapping.override_key == "response"


def test_an_override_the_data_does_not_show_falls_back_and_says_so():
    current = FieldMapping(dataset="fm_questions", field="answer", override_key="gone")
    mapping = fields.resolve("fm_questions", "answer", _figures(10, answer=9), current)
    assert (mapping.chosen_key, mapping.state) == ("answer", State.OVERRIDE_MISSING)
    assert mapping.override_key == "gone"


def test_an_id_needs_a_number_and_a_bool_a_bool():
    figures = {
        "total": 4,
        "keys": {"question": 4, "is_hact": 4},
        "usable": {("question", "id"): 0, ("question", "text"): 4, ("is_hact", "bool"): 1},
    }
    assert fields.coverage("fm_questions", "question", figures, "question_id") == 0
    assert fields.coverage("fm_questions", "question", figures, "question_text") == 1
    assert fields.coverage("fm_questions", "is_hact", figures, "is_hact") == 0.25


def test_the_refresh_keeps_an_override_and_rows_stay_the_same(fm_world, fm_questions_variant):
    fm_questions_variant("A")
    _probe()
    pks = dict(FieldMapping.objects.values_list("field", "pk").filter(dataset="fm_questions"))
    FieldMapping.objects.filter(dataset="fm_questions", field="answer").update(override_key="method")
    _probe()
    mapping = FieldMapping.objects.get(dataset="fm_questions", field="answer")
    assert (mapping.override_key, mapping.chosen_key, mapping.state) == ("method", "method", State.OVERRIDE)
    assert dict(FieldMapping.objects.values_list("field", "pk").filter(dataset="fm_questions")) == pks
    assert fields.key_for("fm_questions", "answer") == "method" and fields.available("fm_questions", "answer")


def test_key_for_reads_the_stored_choice(fm_world):
    assert fields.key_for("fm_questions", "answer") is None  # never probed
    _probe()
    assert fields.key_for("fm_questions", "answer") == "answer"
    assert fields.key_for("field_monitoring", "offices") == "field_office"
    assert fields.key_for("field_monitoring", "team") == "team_members"
    assert fields.available("field_monitoring", "status")
    assert not fields.available("field_monitoring", "sections")


# ------------------------------------------------------------------------------------------ the probe
def test_the_probe_counts_keys_and_their_types(fm_world):
    _probe()
    probes = {p.key: p for p in KeyProbe.objects.filter(dataset="field_monitoring")}
    total = dm.MonitoringFinding.objects.count()
    assert probes["entity"].records == total and probes["entity"].total == total
    assert probes["location"].types == {"dict": total}
    assert probes["location.p_code"].records == total - 1  # the row known by its place name only
    team = dm.MonitoringFinding.objects.filter(data__has_key="team_members").count()
    assert probes["team_members"].types == {"list": team}
    assert probes["team_members.name"].records == team  # the keys of a list's objects, one level down
    assert probes["monitoring_activity_id"].types == {"int": total - 1, "null": 1}
    questions = {p.key: p for p in KeyProbe.objects.filter(dataset="fm_questions")}
    assert questions["answer"].types["str"] >= 1 and questions["answer"].types["bool"] == 1


def test_contact_keys_are_removed_before_probing(fm_world):
    row = fm_world.rows[101]
    row.data = {**row.data, "email": MEMBER_EMAIL, "phone_number": "03-123456", "lead": {"mobile": "1"}}
    row.save()
    _probe()
    keys = set(KeyProbe.objects.filter(dataset="field_monitoring").values_list("key", flat=True))
    assert "email" not in keys and "phone_number" not in keys and "lead.mobile" not in keys
    # the team's e-mail addresses are stored in the record but never probed
    assert "team_members.email" not in keys and "team_members.name" in keys


def test_examples_are_cleaned_and_person_keys_withheld(fm_world):
    _probe()
    examples = {(p.dataset, p.key): p.examples for p in KeyProbe.objects.all()}
    assert examples[("field_monitoring", "visit_lead")] == [privacy.WITHHELD]
    assert examples[("field_monitoring", "team_members")] == [privacy.WITHHELD]
    assert examples[("field_monitoring", "team_members.name")] == [privacy.WITHHELD]
    every = " ".join(str(e) for values in examples.values() for e in values)
    for canary in CANARIES:
        assert canary not in every, canary
    assert all(len(e) <= privacy.EXAMPLE_CHARS for values in examples.values() for e in values)
    assert len(examples[("field_monitoring", "narrative_finding")]) == 3
    assert "Off Track" in examples[("field_monitoring", "overall_finding_rating")]


def test_a_team_name_written_in_a_text_is_cleaned_from_the_examples(fm_world):
    """The team's names are not on NeuroDB's lists yet: the probe learns them from the person keys."""
    from neurodb.watch import people

    assert not any("karim" in name for name in people.known_names())
    row = fm_world.rows[103]
    row.narrative_finding = f"{MEMBER} checked the registers."
    row.data = {**row.data, "narrative_finding": row.narrative_finding}
    row.save()
    dm.MonitoringFinding.objects.exclude(pk=row.pk).update(data={})
    _probe()
    example = KeyProbe.objects.get(dataset="field_monitoring", key="narrative_finding").examples
    assert example == ["[name withheld] checked the registers."]


def test_the_words_of_a_comment_are_not_taken_for_names(fm_world):
    """Names are learnt from the keys that name people (the team), not from the comments people
    wrote: a comment's words written elsewhere stay in the examples."""
    row = fm_world.rows[103]
    row.narrative_finding = "Registers kept up to date in both centres."
    row.data = {**row.data, "narrative_finding": row.narrative_finding, "comments": [row.narrative_finding]}
    row.save()
    dm.MonitoringFinding.objects.exclude(pk=row.pk).update(data={})
    _probe()
    examples = {p.key: p.examples for p in KeyProbe.objects.filter(dataset="field_monitoring")}
    assert examples["comments"] == [privacy.WITHHELD]
    assert examples["narrative_finding"] == ["Registers kept up to date in both centres."]


def test_a_record_that_is_not_an_object_counts_but_holds_no_key(db):
    dm.DatamartDocument.objects.create(dataset="offices", record_key="1", data={"name": "Zahle"})
    dm.DatamartDocument.objects.create(dataset="offices", record_key="2", data=["odd"])
    run = _probe()
    probe = KeyProbe.objects.get(dataset="offices", key="name")
    assert (probe.records, probe.total) == (1, 2) and run.details["datasets"]["offices"]["records"] == 2
    assert FieldMapping.objects.get(dataset="offices", field="name").coverage == 0.5


def test_dynamic_keys_are_capped(db, monkeypatch):
    monkeypatch.setattr(fields, "MAX_KEYS", 5)
    for n in range(3):  # the same keys in every record: counted once each
        dm.DatamartDocument.objects.create(
            dataset="sections",
            record_key=str(n),
            data={"name": "Education", **{f"k{i}": i for i in range(9)}},
        )
    run = _probe()
    # Postgres keeps an object's shorter keys first: k0-k4 fill the cap, k5-k8 are left out, and the
    # field's own key ("name") is counted all the same
    keys = set(KeyProbe.objects.filter(dataset="sections").values_list("key", flat=True))
    assert keys == {"k0", "k1", "k2", "k3", "k4", "name"}
    assert run.details["datasets"]["sections"]["keys_not_kept"] == 4
    assert FieldMapping.objects.get(dataset="sections", field="name").chosen_key == "name"


def test_a_fields_own_keys_are_counted_past_the_cap(db, monkeypatch):
    """Keys written before the answer's must not push the answer out of the probe."""
    monkeypatch.setattr(fields, "MAX_KEYS", 3)
    dm.DatamartDocument.objects.create(
        dataset="fm_questions",
        record_key="1",
        data={"a1": 1, "a2": 2, "a3": 3, "a4": 4, "answer": "Yes", "question_text": "Q1"},
    )
    run = _probe()
    keys = set(KeyProbe.objects.filter(dataset="fm_questions").values_list("key", flat=True))
    assert {"answer", "question_text"} <= keys and "a4" not in keys
    assert fields.key_for("fm_questions", "answer") == "answer"
    assert run.details["questions"]["answered"] == 1


def test_a_pinned_key_is_measured_like_a_candidate(fm_world):
    """An administrator may pin a key that is not listed: its coverage counts usable values of the
    field's kind, its answers are counted, and a pinned team key is withheld from the examples."""
    dm.DatamartDocument.objects.filter(dataset="fm_questions").delete()
    for n, reply in enumerate(("Yes", "n/a", "", "Off track")):
        dm.DatamartDocument.objects.create(
            dataset="fm_questions",
            record_key=str(n),
            data={"id": n, "reply_text": reply, "ref_no": "x", "question_text": "Q1"},
        )
    _probe()
    FieldMapping.objects.filter(dataset="fm_questions", field="answer").update(override_key="reply_text")
    FieldMapping.objects.filter(dataset="fm_questions", field="activity_id").update(override_key="ref_no")
    FieldMapping.objects.filter(dataset="field_monitoring", field="team").update(override_key="crew")
    dm.MonitoringFinding.objects.filter(datamart_id=101).update(
        data={**fm_world.rows[101].data, "crew": "Nadia Crewmate"}
    )
    run = _probe()
    answer = FieldMapping.objects.get(dataset="fm_questions", field="answer")
    assert (answer.chosen_key, answer.state, answer.coverage) == ("reply_text", State.OVERRIDE, 0.75)
    questions = run.details["questions"]
    assert (questions["answered"], questions["unanswered"], questions["unanswered_seen"]) == (2, 2, True)
    # "x" is no id: the pinned key is used, but fills no record
    activity = FieldMapping.objects.get(dataset="fm_questions", field="activity_id")
    assert (activity.chosen_key, activity.coverage) == ("ref_no", 0)
    crew = KeyProbe.objects.get(dataset="field_monitoring", key="crew")
    assert crew.examples == [privacy.WITHHELD]
    assert "Nadia" not in str(list(KeyProbe.objects.values_list("examples", flat=True)))


def test_answers_are_counted_over_the_records_read(fm_questions_variant):
    fm_questions_variant("A")
    dm.DatamartDocument.objects.create(dataset="fm_questions", record_key="odd", data=["not a record"])
    questions = _probe().details["questions"]
    assert questions["records"] == 7 and (questions["answered"], questions["unanswered"]) == (4, 2)
    assert questions["answered_share"] == round(4 / 6, 4)


# ------------------------------------------------------------------------------------------ values
def test_dotted_keys_walk_objects_and_lists():
    record = {
        "question": {"id": "12", "text": " Q1 ", "is_hact": True},
        "sections": [{"name": "Education"}, {"name": "WASH"}, "odd"],
        "a.b": "literal",
    }
    assert parse.value(record, "question.id", "id") == 12
    assert parse.value(record, "question.text") == "Q1"
    assert parse.value(record, "question.is_hact", "bool") is True
    assert parse.value(record, "sections.name") == "Education, WASH"
    assert parse.value(record, "a.b") == "literal"
    assert parse.value(record, "question.missing") is None
    assert parse.value("not a record", "question") is None


@pytest.mark.parametrize(
    "raw, kind, expected",
    [
        ({"id": 1722, "reference_number": "FM-2026-022"}, "text", "FM-2026-022"),
        ({"id": 1722, "reference_number": "FM-2026-022"}, "id", 1722),
        ({"value": "off_track", "label": "Off track"}, "text", "Off track"),
        ([{"name": "A"}, {"title": "B"}, None], "text", "A, B"),
        (["7", "8"], "id", 7),
        ([], "text", None),
        (12.0, "text", "12"),
        (0, "id", None),
        (-3, "int", -3),
        ("12", "int", 12),
        ("1.5", "id", None),
        (True, "id", None),
        (True, "text", "Yes"),
        (False, "text", "No"),
        ("true", "bool", None),
        ("   ", "text", None),
        ({"other": 1}, "text", None),
    ],
)
def test_values_of_every_shape(raw, kind, expected):
    assert parse.as_kind(raw, kind) == expected


OPTIONS = {("12", "2"): "Constrained", ("12", "3"): "Off track"}


@pytest.mark.parametrize(
    "answer, label, summary, answered, placeholder, code",
    [
        ("Yes", None, None, True, False, "yes"),
        ("y", None, None, True, False, "yes"),
        ("No", None, None, True, False, "no"),
        ("Off track", None, None, True, False, "off_track"),
        ("2", None, None, True, False, "constrained"),  # an option code, read through its label
        ("1", "On track", None, True, False, "on_track"),  # the answer label when no option matches
        (True, None, None, True, False, "yes"),
        (False, None, None, True, False, "no"),
        ("n/a", None, "", False, True, ""),
        ("N/A.", None, None, False, True, ""),
        ("see above", None, None, False, True, ""),
        ("", None, "", False, False, ""),
        (None, None, "Visited both centres.", True, False, ""),
        ("Registers kept", None, None, True, False, ""),
    ],
)
def test_answers_are_read_as_codes(answer, label, summary, answered, placeholder, code):
    parsed = parse.read_answer(answer, label, summary, options=OPTIONS, question_key="12")
    assert (parsed.answered, parsed.placeholder, parsed.answer_code) == (answered, placeholder, code)


def test_an_answer_keeps_its_rating_and_word_counts_never_its_text():
    parsed = parse.read_answer(
        "3", None, "Two centres were closed this week.", options=OPTIONS, question_key="12"
    )
    assert (parsed.rating, parsed.answer_code) == ("off_track", "off_track")
    assert (parsed.answer_words, parsed.summary_words) == (2, 6)
    assert parse.read_answer("Yes").rating == ""
    assert parse.read_answer("n/a").rating == ""
    assert parse.read_answer("Not monitored").rating == "not_monitored"


# ------------------------------------------------------------------------------------------ who reads data
DATA_READS = re.compile(r"""\.data\[|\.data\.get\(|\["data"\]|values_list\("pk", "data"\)""")
MAY_READ_DATA = {"fields.py", "parse.py", "build.py", "views.py"}


def test_only_the_readers_read_the_records_data():
    offenders = []
    for path in FMM.rglob("*.py"):
        relative = path.relative_to(FMM)
        if DATA_READS.search(path.read_text(encoding="utf-8")) and (
            relative.parts[0] == "ai" or str(relative) not in MAY_READ_DATA
        ):
            offenders.append(str(relative))
    assert offenders == []
    assert DATA_READS.search((FMM / "fields.py").read_text(encoding="utf-8"))  # the pattern still matches


# ------------------------------------------------------------------------------------------ answers built
def _answers_of(visit_key: str) -> list[tuple]:
    return list(
        QuestionAnswer.objects.filter(visit_key=visit_key)
        .order_by("document_id")
        .values_list("answered", "placeholder", "answer_code", "applies_to")
    )


ANSWERED = [True, True, True, True, False, False]  # Q1 code, Q2, Q3, PSEA, "n/a", blank
PLACEHOLDER = [False, False, False, False, True, False]


@pytest.mark.parametrize(
    "shape, codes, applies_to, ids",
    [
        # flat: the option code "2" is Constrained; the entity is the PD row
        ("A", ["constrained", "", "", "no", "", ""], "entity", True),
        # other names: no question id, so the option code cannot be looked up; the partner's row
        ("B", ["", "", "", "no", "", ""], "entity", False),
        # nested: the answer object's label; no entity, so the visit
        ("C", ["constrained", "", "", "no", "", ""], "visit", True),
    ],
)
def test_each_shape_gives_its_answers(fm_world, fm_questions_variant, shape, codes, applies_to, ids):
    fm_questions_variant(shape)
    run = refresh.run(triggered_by="test")
    answers = _answers_of("1722")
    assert [a[0] for a in answers] == ANSWERED and [a[1] for a in answers] == PLACEHOLDER
    assert [a[2] for a in answers] == codes and {a[3] for a in answers} == {applies_to}
    keys = list(QuestionAnswer.objects.order_by("document_id").values_list("question_key", flat=True))
    assert (keys[:2] == ["12", "13"]) is ids and len(set(keys)) == 6
    visit = Visit.objects.get(key="1722")
    assert (visit.questions_asked, visit.questions_answered) == (6, 4)
    assert run.details["questions"]["linked"] == 6


def test_shape_d_gives_no_answers_and_says_why(fm_world, fm_questions_variant):
    fm_questions_variant("D")
    run = refresh.run(triggered_by="test")
    assert run.status == "succeeded" and not QuestionAnswer.objects.exists()
    assert "fm_questions.question_text" in run.details["fields_not_found"]
    assert set(Visit.objects.values_list("questions_asked", flat=True)) == {None}


# ------------------------------------------------------------------------------------------ texts
def test_search_texts_finds_narratives_and_answer_values_only(fm_world):
    refresh.run(triggered_by="test")
    keys = list(Visit.objects.values_list("key", flat=True))
    hits = parse.search_texts(keys, "REGISTERS", 10)
    assert [(h.visit_key, h.where) for h in hits] == [("1722", "narrative"), ("1722", "answer")]
    assert hits[1].text == "Registers checked."
    summary = parse.search_texts(keys, "psea channel", 10)
    assert [(h.visit_key, h.where, h.text) for h in summary] == [
        ("1726", "summary", "Referred through the PSEA channel.")
    ]
    # in a key's name only, in another field, or in a person: nothing
    assert parse.search_texts(keys, "method", 10) == []  # a key name
    assert parse.search_texts(keys, "Interview", 10) == []  # the method's value
    assert parse.search_texts(keys, "Lebanon", 10) == []  # the country
    assert [h.where for h in parse.search_texts(keys, "Rania", 10)] == ["narrative"]  # written in one
    assert parse.search_texts(keys, "team", 10) == []
    # newest visits first, at most the limit, only the visits asked
    assert [h.visit_key for h in parse.search_texts(keys, "a", 50)][:2] == ["1726", "1726"]
    assert len(parse.search_texts(keys, "registers", 1)) == 1
    assert parse.search_texts(["1723"], "registers", 10) == []
    # a visit without a date comes after the dated ones, not before the newest
    Visit.objects.filter(key="1726").update(end_date=None, visit_date=None)
    found = [h.visit_key for h in parse.search_texts(keys, "a", 50)]
    assert found[0] != "1726" and found[-1] == "1726"


def test_a_person_like_answer_key_is_never_searched(fm_world, monkeypatch):
    refresh.run(triggered_by="test")
    FieldMapping.objects.filter(dataset="fm_questions", field="summary").update(chosen_key="comment")
    fields.forget()
    doc = dm.DatamartDocument.objects.filter(dataset="fm_questions").first()
    doc.data["comment"] = "Spoke with the headmaster"
    doc.save()
    assert parse.search_texts(["1722"], "headmaster", 10) == []


def test_visit_answers_read_the_texts_back_with_option_labels(fm_world):
    refresh.run(triggered_by="test")
    answers = parse.visit_answers(Visit.objects.get(key="1727"))
    assert answers == [(conftest.Q1_TEXT, "Constrained", "")]  # the option code "2"
    first = parse.visit_answers(Visit.objects.get(key="1722"))
    assert first[0] == (conftest.Q1_TEXT, "On track", "") and first[1][1] == "Off track"
    assert parse.visit_answers(Visit.objects.get(key="1724")) == []


def test_question_keys_and_folding():
    assert parse.question_key(12, "anything") == "12" and parse.question_key("12", "") == "12"
    assert parse.question_key(None, "Q2 – Activities monitored") == parse.question_key(
        None, "q2 activities  MONITORED"
    )
    assert len(parse.question_key(None, "x")) == 40 and parse.question_key(None, "") == ""
    assert parse.fold("Écoles: À l'heure!") == "ecoles a l heure"
    assert parse.text_list([{"name": "Education"}, "WASH", "", "WASH", None]) == ["Education", "WASH"]
    assert parse.text_list("Health") == ["Health"] and parse.text_list(None) == []
