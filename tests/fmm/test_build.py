"""Building the visits (``fmm.build``, step 5 of the refresh): grouping, rating and status, links to
programme documents, places, sections, offices, teams and action points, the answers of each visit, and
what is never kept (narratives and answers) or can never fail the refresh (over-long values)."""

import datetime

import pytest
from django.contrib.postgres.fields import ArrayField
from django.db import models

from neurodb.core.models import SyncRun
from neurodb.datamart import fm
from neurodb.datamart import models as dm
from neurodb.fmm import build, parse, place, refresh
from neurodb.fmm.models import (
    FieldMapping,
    QuestionAnswer,
    RefreshRequest,
    Visit,
    VisitActionPoint,
    VisitEntity,
    VisitRuleResult,
)
from neurodb.geo.models import Location
from neurodb.reports.overview import governorate_key

from .conftest import CANARY_TEXT, MEMBER, MEMBER_EMAIL, PD_LEBA, TREE, _finding

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)


def _refresh(**kw) -> SyncRun:
    return refresh.run(triggered_by="test", today=TODAY, **kw)


def _visit(key: str) -> Visit:
    return Visit.objects.get(key=key)


@pytest.fixture
def built(fm_world):
    run = _refresh()
    assert run.status == SyncRun.Status.SUCCEEDED, run.error
    return run


# ------------------------------------------------------------------------------------------ grouping
def test_rows_are_grouped_into_visits_by_their_key(fm_world, built):
    keys = set(Visit.objects.values_list("key", flat=True))
    reference_key = fm.visit_key(None, "FM/2026/9", 0)
    assert keys == {"1722", "1723", "1724", "1725", "1726", "1727", "1728", reference_key}
    assert len(keys) == fm.count_visits(dm.MonitoringFinding.objects.all())
    visit = _visit("1722")
    assert (visit.activity_id, visit.label, visit.reference, visit.reference_number) == (
        1722,
        "Visit 1722",
        "FM-2026-022",
        "FM-2026-022",
    )
    assert visit.entities == 3 and visit.entity_rows.count() == 3
    assert visit.entity_kinds == ["pd", "cp_output", "partner"]
    assert set(visit.findings().values_list("datamart_id", flat=True)) == {101, 102, 103}
    assert (visit.start_date, visit.end_date) == (datetime.date(2026, 5, 11), datetime.date(2026, 5, 12))
    by_reference = _visit(reference_key)
    assert by_reference.label == "FM/2026/9" and by_reference.activity_id is None
    assert built.details["visits"] == 8 and built.details["findings"] == dm.MonitoringFinding.objects.count()


def test_a_row_without_id_or_reference_is_its_own_visit(fm_world):
    row = _finding(181, entity="Somewhere", status="completed", end_date=datetime.date(2026, 2, 1))
    run = _refresh()
    visit = _visit(f"f-{row.pk}")
    assert visit.label == "Finding 181" and visit.entities == 1
    assert run.details["rows_without_reference"] == 1


def test_the_rating_is_the_worst_rated_entity(fm_world, built):
    assert _visit("1722").rating == "off_track"  # On Track, Off Track, On Track
    assert _visit("1722").rating_counts == {"on_track": 2, "off_track": 1}
    assert (_visit("1722").entities_rated, _visit("1723").entities_rated) == (3, 0)
    assert _visit("1723").rating == "not_monitored"  # blank and Not Monitored: no entity rated
    assert _visit("1724").rating == "not_monitored"  # in progress, not rated yet
    assert build.visit_rating(["on_track", "constrained", "not_monitored"]) == "constrained"
    assert build.visit_rating(["other", "not_monitored"]) == "not_monitored"


def test_an_unknown_rating_is_recorded(fm_world):
    dm.MonitoringFinding.objects.filter(datamart_id=103).update(overall_finding_rating="Partially fine")
    _refresh()
    assert _visit("1722").issues["rating_unknown"] == {"Partially fine": 1}


def test_the_status_is_the_most_advanced_and_disagreements_are_recorded(fm_world, built):
    assert (_visit("1722").status, _visit("1722").status_group) == ("completed", "reported")
    assert (_visit("1724").status, _visit("1724").status_group) == ("data_collection", "in_progress")
    assert _visit("1725").status_group == "cancelled"
    assert "status_conflict" not in _visit("1722").issues
    dm.MonitoringFinding.objects.filter(datamart_id=102).update(status="submitted")
    dm.MonitoringFinding.objects.filter(datamart_id=103).update(status="In review")
    _refresh()
    visit = _visit("1722")
    assert (visit.status, visit.status_raw) == ("completed", "completed")
    assert visit.issues["status_conflict"] == {"completed": 1, "submitted": 1, "In review": 1}


def test_a_blank_status_column_is_read_from_the_record(fm_world):
    row = dm.MonitoringFinding.objects.get(datamart_id=131)
    row.status = ""
    row.data["status"] = "Report Finalization"
    row.save()
    _refresh()
    assert (_visit("1727").status, _visit("1727").status_group) == ("report_finalization", "in_progress")


def test_issues_name_missing_dates_partners_and_programme_documents(fm_world):
    _finding(
        191,
        entity="LEB/PCA2099999/PD2099001",
        entity_type="PD/SSFA",
        monitoring_activity="FM-2026-091",
        monitoring_activity_id=1791,
        status="completed",
    )
    run = _refresh()
    issues = _visit("1791").issues
    assert issues == {"no_date": True, "rows_without_partner": 1, "pd_unresolved": 1}
    assert run.details["without_date"] == 1
    assert _visit("1722").issues == {}


def test_one_reference_under_several_keys_is_recorded(fm_world):
    _finding(
        192,
        entity="Amel Association",
        entity_type="Partner",
        monitoring_activity="FM-2026-022",  # 1722's reference, under another activity id
        monitoring_activity_id=1792,
        status="completed",
        end_date=datetime.date(2026, 5, 12),
    )
    run = _refresh()
    assert _visit("1722").issues["reference_conflict"] == 2 == _visit("1792").issues["reference_conflict"]
    assert run.details["reference_conflicts"] == 1


# ------------------------------------------------------------------------------------------ links
def test_programme_documents_and_outputs(fm_world, built):
    pds = fm_world.pds
    visit = _visit("1722")
    # the LEBA/ form is the amendment that covers the visit; the programme activities name the base
    # number, which also gives the amendment (by the PCA/PD pair first)
    assert visit.pd == pds["amended"] and visit.pd_ids == [pds["amended"].pk]
    assert visit.pd_numbers == [pds["amended"].number]
    row = visit.entity_rows.get(datamart_id=101)
    assert (row.kind, row.pd, row.pd_match) == ("pd", pds["amended"], "token")
    assert visit.entity_rows.get(datamart_id=102).cp_output == "2.2 INCREASED ACCESS TO EDUCATION"
    assert visit.cp_outputs == ["2.2 INCREASED ACCESS TO EDUCATION"]
    assert visit.programme_activities == ["BLN classes", "Homework support"]
    assert _visit("1726").pd_ids == [pds["ssfa"].pk, pds["education"].pk]
    assert visit.partner == fm_world.partners["amel"] and visit.partner_ids == [fm_world.partners["amel"].pk]


def test_a_reference_in_the_record_links_a_row_without_one(fm_world):
    row = dm.MonitoringFinding.objects.get(datamart_id=103)
    row.data["intervention_number"] = "LEB/SSFA2024001-1"
    row.save()
    _refresh()
    entity = VisitEntity.objects.get(datamart_id=103)
    assert (entity.pd, entity.pd_match) == (fm_world.pds["ssfa"], "base")
    assert fm_world.pds["ssfa"].pk in _visit("1722").pd_ids


def test_governorate_and_district_from_the_location_else_the_site(fm_world, built):
    visit = _visit("1722")
    assert (visit.governorate, visit.governorate_name, visit.governorate_key) == (
        fm_world.governorates["bekaa"],
        "Bekaa",
        governorate_key("Bekaa"),  # the overview's key ("beqaa")
    )
    assert (visit.district_name, visit.place_name) == ("Zahle", "Zahle town")
    site_only = _visit("1728")  # the location arrived as a name only
    assert site_only.location is None and site_only.site == fm_world.sites["schools"]
    assert (site_only.governorate_name, site_only.district_name) == ("Bekaa", "Zahle")
    assert site_only.place_name == "Saadnayel public school"
    assert built.details["governorate"] == {"linked": 7, "via_site_only": 1, "unlinked": 0}


def test_points_site_location_ancestor_or_none_and_which_are_precise(fm_world, built):
    assert place.lowest_admin_level() == 3
    site = _visit("1726")
    assert (site.located_by, site.located_level, site.point_precise) == ("site", None, True)
    assert (site.latitude, site.longitude) == (34.452, 35.813)
    cadaster = _visit("1722")  # a cadaster's own point
    assert (cadaster.located_by, cadaster.located_level, cadaster.point_precise) == ("location", 3, True)
    district = _visit("1727")  # a district's own centre: never precise
    assert (district.located_by, district.located_level, district.point_precise) == ("location", 2, False)
    assert not district.approximate
    # a cadaster without a point under Baalbek: the district's point, approximate; Bebnine: no point at all
    hamlet = Location.objects.create(
        id=37, name="Hamlet", p_code="LB37", type=fm_world.cadasters["douris"].type,
        parent=fm_world.districts["baalbek"], **TREE,
    )  # fmt: skip
    dm.MonitoringFinding.objects.filter(datamart_id=111).update(location=hamlet)
    dm.MonitoringFinding.objects.filter(datamart_id=112).update(location=hamlet)
    dm.MonitoringFinding.objects.filter(datamart_id=151).update(location=fm_world.cadasters["bebnine"])
    run = _refresh()
    ancestor = _visit("1723")
    assert (ancestor.located_by, ancestor.located_level, ancestor.point_precise) == ("ancestor", 2, False)
    assert ancestor.approximate and ancestor.approximate_from == "Baalbek"
    nowhere = _visit("1725")
    assert (nowhere.located_by, nowhere.latitude, nowhere.point_precise) == ("", None, False)
    assert run.details["location"]["ancestor"] == 1 and run.details["location"]["none"] == 1


def test_precise_points():
    assert place.precise("site", None, 3) and place.precise("location", 3, 3)
    assert not place.precise("location", 2, 3) and not place.precise("ancestor", 3, 3)
    assert not place.precise("", None, 3) and not place.precise("location", 3, None)
    assert place.haversine_km(33.8938, 35.5018, 34.4367, 35.8497) == pytest.approx(69, abs=1)


def test_sections_activity_then_pd_then_action_point_then_partner(fm_world):
    for row in dm.MonitoringFinding.objects.filter(monitoring_activity_id=1722):
        row.data["sections"] = [{"id": 3, "name": "Education"}]
        row.save()
    dm.ActionPoint.objects.filter(datamart_id=8002).update(section="WASH", office="Zahle")
    _finding(
        193,
        partner=fm_world.partners["mercy"],
        entity="Mercy Corps Lebanon",
        entity_type="Partner",
        monitoring_activity="FM-2026-029",
        monitoring_activity_id=1729,
        status="completed",
        end_date=datetime.date(2026, 7, 1),
    )
    run = _refresh()
    assert (_visit("1722").section_names, _visit("1722").sections_from) == (["Education"], "activity")
    assert _visit("1722").section_ids == [fm_world.section.pk]  # through the confirmed section map
    assert (_visit("1724").section_names, _visit("1724").sections_from) == (["Education"], "pd")
    assert (_visit("1723").section_names, _visit("1723").sections_from) == (["WASH"], "action_point")
    assert _visit("1723").section_ids == []  # "WASH" has no confirmed match
    # no programme document: the sections of the partner's PDs running on the visit date, flagged
    assert (_visit("1729").section_names, _visit("1729").sections_from) == (["Education"], "partner")
    assert (_visit("1725").section_names, _visit("1725").sections_from) == ([], "")
    assert run.details["sections_from"]["partner"] == 1 and run.details["sections_from"]["activity"] == 1


def test_offices_activity_then_pd_then_action_point(fm_world):
    dm.ActionPoint.objects.filter(datamart_id=8002).update(office="Zahle")
    run = _refresh()
    assert (_visit("1722").offices, _visit("1722").offices_from) == (["Zahle"], "activity")
    assert (_visit("1724").offices, _visit("1724").offices_from) == (["Tripoli"], "pd")
    assert (_visit("1723").offices, _visit("1723").offices_from) == (["Zahle"], "action_point")
    assert (_visit("1725").offices, _visit("1725").offices_from) == ([], "")
    assert run.details["offices_from"] == {"activity": 1, "pd": 3, "action_point": 1, "none": 3}


def test_a_pd_office_text_is_split_on_commas(fm_world):
    fm_world.pds["education"].offices_set = None
    fm_world.pds["education"].offices_names = "Tripoli, Akkar"
    fm_world.pds["education"].save()
    _refresh()
    assert _visit("1724").offices == ["Tripoli", "Akkar"]


def test_action_points_by_id_reference_and_reference_number(fm_world):
    dm.MonitoringFinding.objects.filter(datamart_id=141).update(reference_number="FM-RN-1724")
    dm.ActionPoint.objects.create(
        datamart_id=8005, related_module="FM", module_reference_number="fm-rn-1724", status="open"
    )
    run = _refresh()
    links = set(VisitActionPoint.objects.values_list("visit__key", "action_point__datamart_id", "matched_by"))
    assert links == {
        ("1722", 8001, "related_id"),
        ("1723", 8002, "reference"),
        ("1726", 8004, "related_id"),
        ("1724", 8005, "reference_number"),
    }
    assert run.details["action_points"] == {
        "fm_total": 5,
        "related_id": 2,
        "reference": 1,
        "reference_number": 1,
        "unlinked": 1,
    }
    counts = Visit.objects.values_list(
        "action_points", "action_points_open", "action_points_overdue", "action_points_high_open"
    )
    assert counts.get(key="1722") == (1, 1, 0, 0)  # open, due in December
    assert counts.get(key="1723") == (1, 0, 0, 0)  # completed
    assert counts.get(key="1726") == (1, 1, 1, 1)  # open, overdue, high priority
    assert counts.get(key="1727") == (0, 0, 0, 0)


def test_action_point_counts_follow_the_open_statuses_and_the_day():
    facts = [
        build.ActionPointFacts(1, "open", datetime.date(2026, 9, 1), True),
        build.ActionPointFacts(2, "open", None, False),
        build.ActionPointFacts(3, "completed", datetime.date(2026, 1, 1), True),
    ]
    assert build.ap_counts(facts, TODAY) == {
        "action_points": 3,
        "action_points_open": 2,
        "action_points_overdue": 1,
        "action_points_high_open": 1,
    }
    assert build.ap_counts(facts, datetime.date(2026, 8, 1))["action_points_overdue"] == 0


def test_the_team_is_names_only(fm_world):
    row = dm.MonitoringFinding.objects.get(datamart_id=121)
    row.data["team_members"] = [
        {"name": MEMBER, "email": MEMBER_EMAIL},
        {"email": "only.an.address@example.org"},
        {"first_name": "Nour", "last_name": "Khalil"},
    ]
    row.save()
    _refresh()
    visit = _visit("1726")
    assert visit.team == ["Rania Canary", "Karim Canary", "Nour Khalil"]  # the visit lead first
    assert visit.team_unnamed == 1
    names = [name for team in Visit.objects.values_list("team", flat=True) for name in team]
    assert names and not any("@" in name for name in names)
    assert _visit("1724").team == []  # no lead, no team


def test_the_search_text(fm_world, built):
    search = _visit("1722").search
    for part in (
        "visit 1722",
        "fm-2026-022",
        "amel association",
        "amel",
        "2500212345",
        "zahle town",
        "bekaa",
    ):
        assert part in search
    assert "leb/pca2023597/pd2025123-2" in search
    assert "canary" not in search  # never a person


# ------------------------------------------------------------------------------------------ answers
def test_answers_apply_to_an_entity_a_partner_or_the_visit(fm_world, built):
    def applied(visit_key):
        return [
            (a.question_key, a.applies_to, a.entity.datamart_id if a.entity else None, a.partner_id)
            for a in QuestionAnswer.objects.filter(visit_key=visit_key).order_by("document_id")
        ]

    assert applied("1722")[:2] == [("12", "entity", 101, None), ("12", "entity", 102, None)]
    assert applied("1726") == [
        ("12", "partner", None, fm_world.partners["mercy"].pk),
        ("15", "visit", None, None),
    ]
    assert applied("1727") == [("12", "visit", None, None)]
    reference_key = fm.visit_key(None, "FM/2026/9", 0)
    assert applied(reference_key) == [("12", "entity", 161, None)]  # joined by the activity reference
    assert built.details["questions"]["applies_to"] == {"entity": 4, "partner": 1, "visit": 8}


def test_an_answer_naming_the_partner_applies_to_its_own_row(fm_world):
    doc = dm.DatamartDocument.objects.filter(dataset="fm_questions", data__question_id=13).get()
    doc.data.update(entity="AMEL", entity_type="Partner")  # the short name of 1722's partner
    doc.save()
    _refresh()
    answer = QuestionAnswer.objects.get(document_id=doc.pk)
    assert (answer.applies_to, answer.entity.datamart_id) == ("entity", 103)


def test_answers_keep_codes_and_counts_never_their_text(fm_world, built):
    rows = {
        (a.visit_key, a.question_key): a
        for a in QuestionAnswer.objects.filter(visit_key__in=["1723", "1728"]).union(
            QuestionAnswer.objects.filter(visit_key="1722").exclude(question_key="12")
        )
    }
    on_track = QuestionAnswer.objects.get(entity__datamart_id=101)
    off_track = QuestionAnswer.objects.get(entity__datamart_id=102)  # the option code "3"
    option = QuestionAnswer.objects.get(visit_key="1727")
    assert (on_track.answer_code, on_track.rating, on_track.answered) == ("on_track", "on_track", True)
    assert (off_track.answer_code, off_track.rating) == ("off_track", "off_track")
    assert (option.answer_code, option.rating) == ("constrained", "constrained")  # the option code "2"
    assert (rows[("1723", "14")].answered, rows[("1723", "14")].placeholder) == (False, True)  # "n/a"
    assert (rows[("1723", "16")].answered, rows[("1723", "16")].placeholder) == (False, False)  # blank
    assert rows[("1728", "16")].answer_code == "yes"  # true
    q3 = rows[("1722", "14")]
    assert (q3.answer_words, q3.summary_words) == (2, parse.word_count(CANARY_TEXT))
    assert q3.question_text.startswith("Q3") and q3.question_order == 3 and q3.method == "Interview"
    assert (q3.is_hact, on_track.is_hact) == (False, True)


def test_questions_asked_and_answered_count_question_and_entity_pairs(fm_world, built):
    figures = dict(Visit.objects.values_list("key", "questions_asked"))
    assert (_visit("1722").questions_asked, _visit("1722").questions_answered) == (5, 5)
    assert (_visit("1723").questions_asked, _visit("1723").questions_answered) == (3, 1)
    assert _visit("1724").questions_asked is None  # no question data
    assert figures["1726"] == 2
    assert built.details["questions"]["visits_with_questions"] == 6


def test_an_answer_of_no_visit_is_kept_unlinked(fm_world):
    dm.DatamartDocument.objects.create(
        dataset="fm_questions",
        record_key="59999",
        data={"id": 59999, "monitoring_activity_id": 9999, "monitoring_activity": "FM-9999",
              "question_id": 12, "question_text": "Q", "answer": "On track"},
    )  # fmt: skip
    run = _refresh()
    answer = QuestionAnswer.objects.get(question_text="Q")
    assert (answer.visit, answer.visit_key, answer.applies_to) == (None, "", "")
    assert run.details["questions"]["unlinked"] == 1


def test_a_visit_that_cannot_be_built_is_skipped_and_its_answers_kept_unlinked(fm_world, monkeypatch):
    """A visit that fails after its answers were matched to its entity rows: those rows are never
    written, so its answers must not point at them (the whole swap would fail on it)."""
    search = build.search_text

    def flaky(*parts):
        if "Visit 1722" in parts:
            raise ValueError("unreadable visit")
        return search(*parts)

    monkeypatch.setattr(build, "search_text", flaky)
    run = _refresh()
    assert (run.status, run.rows_failed) == (SyncRun.Status.PARTIAL, 1), run.error
    assert not Visit.objects.filter(key="1722").exists() and Visit.objects.count() == 7
    documents = dm.DatamartDocument.objects.filter(dataset="fm_questions", data__monitoring_activity_id=1722)
    kept = QuestionAnswer.objects.filter(document_id__in=documents.values("pk"))
    assert kept.count() == 5
    assert set(kept.values_list("visit", "visit_key", "entity", "partner", "applies_to")) == {
        (None, "", None, None, "")
    }
    questions = run.details["questions"]
    assert (questions["linked"], questions["unlinked"]) == (8, 5)
    assert sum(questions["applies_to"].values()) == 8


# ------------------------------------------------------------------------------------------ never kept
TEXTS = (
    CANARY_TEXT,
    "Layla Saab",
    "evil.example",
    "412 children",
    "Classes held as planned",
    "Sessions were delayed",
    "keeps its registers",
    "Registers checked.",
    "Referred through the PSEA channel",
    "BLN classes, Homework support",
)
DATA_TABLES = (
    Visit,
    VisitEntity,
    QuestionAnswer,
    VisitActionPoint,
    VisitRuleResult,
    FieldMapping,
    RefreshRequest,
)


def _stored_texts():
    for model in DATA_TABLES:
        names = [
            f.name
            for f in model._meta.concrete_fields
            if isinstance(f, models.CharField | models.TextField | models.JSONField | ArrayField)
        ]
        for row in model.objects.values_list(*names):
            yield model.__name__, " ".join(str(value) for value in row)


def test_no_data_table_holds_a_narrative_or_an_answer(fm_world, monkeypatch):
    monkeypatch.setattr(refresh.background, "start_command", lambda *args: 1)
    refresh.request("scores", "test")  # a request row too
    _refresh()
    assert Visit.objects.exists() and QuestionAnswer.objects.exists() and VisitRuleResult.objects.exists()
    for model, text in _stored_texts():
        for canary in TEXTS:
            assert canary not in text, (model, canary)


# ------------------------------------------------------------------------------------------ cut to fit
def test_over_long_values_under_every_key_are_cut_and_the_refresh_succeeds(fm_world):
    long = "x" * 2000
    team = [{"name": f"Member {n:02d} {long}"} for n in range(60)]
    dm.MonitoringFinding.objects.filter(datamart_id=131).update(status="")  # read from the record
    for row in dm.MonitoringFinding.objects.all():
        for key in {
            k.split(".")[0] for keys in build.fields.CANDIDATES["field_monitoring"].values() for k in keys
        }:
            row.data[key] = [long, long] if key in ("sections", "offices") else long
        row.data["team_members"] = team
        row.save()
    for dataset in ("fm_questions", "fm_programme_activities"):
        for doc in dm.DatamartDocument.objects.filter(dataset=dataset):
            for keys in build.fields.CANDIDATES[dataset].values():
                for key in keys:
                    if "." not in key and key not in ("monitoring_activity_id", "activity_id", "question_id"):
                        doc.data[key] = long
            doc.save()
    run = _refresh()
    assert run.status == SyncRun.Status.SUCCEEDED, run.error
    visit = _visit("1722")
    assert len(visit.team) == 50 and all(len(name) <= 120 for name in visit.team)
    assert visit.section_names and all(len(name) == 200 for name in visit.section_names)
    assert all(len(output) <= 300 for output in visit.cp_outputs)
    assert _visit("1727").status_raw == "x" * 40 and _visit("1727").status_group == "unknown"
    assert all(len(t) <= 500 for t in QuestionAnswer.objects.values_list("question_text", flat=True))
    assert all(len(m) <= 100 for m in QuestionAnswer.objects.values_list("method", flat=True))


def test_fit_and_fit_list():
    assert build.fit(Visit, "label", "  " + "y" * 200) == "y" * 110
    assert build.fit(Visit, "place_name", None) == ""
    assert build.fit_list(Visit, "team", ["a" * 300, "", None, "b"], max_items=2) == ["a" * 120, "b"]
    assert build.fit_list(Visit, "partner_ids", ["3", 4]) == [3, 4]


def test_narrative_facts():
    words, digest, placeholder = build.narrative_facts("word " * 30)
    assert (words, len(digest), placeholder) == (30, 40, False)
    assert build.narrative_facts("Too short") == (2, "", False)
    assert build.narrative_facts("n/a") == (2, "", True)
    assert build.narrative_facts("") == (0, "", False)
    # the hash ignores case and punctuation, so a copy is found whatever its spelling
    assert build.narrative_facts("Word, " * 30)[1] == digest


def test_entity_rows_carry_their_narrative_measures_not_the_text(fm_world, built):
    rows = {e.datamart_id: e for e in VisitEntity.objects.all()}
    assert rows[102].narrative_words == parse.word_count(CANARY_TEXT) and rows[102].narrative_hash
    assert rows[101].narrative_hash == ""  # under 25 words
    assert rows[111].narrative_words == 0
    assert rows[101].entity == PD_LEBA and rows[101].rating_raw == "On Track"


def test_section_names_written_on_visits_join_the_section_map(fm_world):
    from neurodb.watch import sections

    row = dm.MonitoringFinding.objects.get(datamart_id=131)
    row.data["sections"] = ["Health & Nutrition"]
    row.save()
    _refresh()
    assert sections._fm_names() == ["Health & Nutrition"]  # visits whose sections come from a PD add none
    assert sections._fm_names in sections.SOURCES and "Health & Nutrition" in sections.etools_names()
