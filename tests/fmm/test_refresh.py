"""The Monitoring insights refresh (``fmm_refresh``): the key probe (``--probe-only``), the full pass
that builds the visits and swaps them in, the scores-only pass, the requests that are never lost, the
run after every Datamart sync, one run at a time, recorded with meaningful counts."""

import datetime
import threading
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.fmm import fields, refresh, score, status
from neurodb.fmm.management.commands import fmm_refresh as command
from neurodb.fmm.models import (
    FieldMapping,
    KeyProbe,
    QuestionAnswer,
    RefreshRequest,
    Visit,
    VisitActionPoint,
    VisitEntity,
    VisitReview,
    VisitRuleResult,
)
from neurodb.integrations import background

from .conftest import OTHER_TEXT

pytestmark = pytest.mark.django_db


def _probe(**kw):
    return refresh.run(triggered_by="test", probe_only=True, **kw)


def _command(*args) -> str:
    out = StringIO()
    call_command("fmm_refresh", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def held_elsewhere(db):
    """Another process holds the refresh's lock (a run in progress)."""
    other = connections.create_connection("default")
    with other.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [refresh.LOCK_ID])
    yield
    other.close()


def test_the_probe_relinks_probes_and_chooses(fm_world):
    dm.MonitoringFinding.objects.update(intervention=None, pd_match="")
    run = _probe()
    assert (run.job, run.target, run.status, run.triggered_by) == (
        SyncRun.Job.FMM_REFRESH,
        "probe",
        SyncRun.Status.SUCCEEDED,
        "test",
    )
    # 1: the PD written in the LEBA/ form is linked to the amendment that covers the visit date
    leba = dm.MonitoringFinding.objects.get(datamart_id=101)
    assert (leba.intervention, leba.pd_match) == (fm_world.pds["amended"], "token")
    assert run.details["pd_relinked"]["token"] >= 1
    # 2 and 3
    assert KeyProbe.objects.filter(dataset="field_monitoring").exists()
    assert FieldMapping.objects.count() == sum(len(f) for f in fields.CANDIDATES.values())
    total = sum(run.details["datasets"][d]["records"] for d in fields.DATASETS)
    assert run.rows_in == total and run.rows_failed == 0
    assert run.rows_written == KeyProbe.objects.count()
    assert run.details["duration_ms"] >= 0


def test_the_run_details_hold_the_rates(fm_world):
    details = _probe().details
    findings = dm.MonitoringFinding.objects.count()
    assert details["activity_ids"] == {
        "rows": findings,
        "with_id": findings - 1,
        "share": round((findings - 1) / findings, 4),
    }
    pd = details["pd_resolved"]
    assert pd["pd_kind_rows"] == pd["exact"] + pd["token"] + pd["base"] + pd["title"] + pd["unresolved"]
    assert pd["token"] >= 1  # the LEBA/ form
    questions = details["questions"]
    assert questions["records"] == dm.DatamartDocument.objects.filter(dataset="fm_questions").count()
    assert questions["unanswered_seen"] is True and 0 < questions["answered_share"] < 1
    assert "field_monitoring.sections" in details["fields_not_found"]
    ratings = {raw: (code, n) for raw, code, n in details["ratings_seen"]}
    assert ratings["Off Track"] == ("off_track", 2) and ratings["Not Monitored"][0] == "not_monitored"
    statuses = {raw: code for raw, code, _ in details["statuses_seen"]}
    assert statuses["data_collection"] == "data_collection" and statuses["cancelled"] == "cancelled"
    kinds = {raw: code for raw, code, _ in details["entity_types_seen"]}
    assert kinds["CP Output"] == "cp_output" and kinds["PD/SSFA"] == "pd" and kinds["Partner"] == "partner"


def test_unknown_ratings_and_statuses_are_listed(fm_world):
    dm.MonitoringFinding.objects.filter(datamart_id=103).update(overall_finding_rating="Partially fine")
    dm.MonitoringFinding.objects.filter(datamart_id=141).update(status="in review")
    details = _probe().details
    assert details["ratings_unknown"] == {"Partially fine": 1}
    assert details["statuses_unknown"] == {"in review": 1}


def test_two_probes_give_the_same_rows(fm_world):
    _probe()
    first = list(
        KeyProbe.objects.order_by("dataset", "key").values_list("dataset", "key", "records", "types")
    )
    mappings = list(
        FieldMapping.objects.order_by("pk").values_list("pk", "dataset", "field", "chosen_key", "state")
    )
    _probe()
    assert (
        list(KeyProbe.objects.order_by("dataset", "key").values_list("dataset", "key", "records", "types"))
        == first
    )
    assert (
        list(FieldMapping.objects.order_by("pk").values_list("pk", "dataset", "field", "chosen_key", "state"))
        == mappings
    )


def test_the_probe_builds_no_visit(fm_world):
    run = _probe()
    assert KeyProbe.objects.exists() and FieldMapping.objects.exists()
    assert not Visit.objects.exists() and run.target == "probe"


def test_a_record_that_cannot_be_read_is_counted_and_the_others_are_read(fm_world, monkeypatch):
    add = fields.Probe.add

    def flaky(self, record):
        if isinstance(record, dict) and record.get("id") == 102:
            raise ValueError("unreadable record")
        add(self, record)

    monkeypatch.setattr(fields.Probe, "add", flaky)
    run = _probe()
    assert run.status == SyncRun.Status.PARTIAL and run.rows_failed == 1
    assert run.details["errors"][0]["error"] == "ValueError: unreadable record"
    assert run.details["datasets"]["field_monitoring"]["failed"] == 1
    assert KeyProbe.objects.filter(dataset="field_monitoring").exists()


def test_a_step_that_fails_keeps_the_previous_keys(fm_world, monkeypatch):
    _probe()
    before = KeyProbe.objects.count()
    dm.DatamartDocument.objects.filter(dataset="offices").delete()

    def broken(probes):
        raise RuntimeError("the choice failed")

    monkeypatch.setattr(fields, "resolve_all", broken)
    run = _probe()
    assert run.status == SyncRun.Status.FAILED and run.error == "RuntimeError: the choice failed"
    assert run.rows_failed == 0 and KeyProbe.objects.count() == before  # nothing half written
    assert KeyProbe.objects.filter(dataset="offices").exists()


def test_one_refresh_at_a_time(fm_world, held_elsewhere):
    assert _probe() is None
    assert not SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH).exists()
    assert background.LOCK_IDS[SyncRun.Job.FMM_REFRESH] == refresh.LOCK_ID == 7140432
    assert command.BUSY in _command("--probe-only")


def test_switched_off_it_does_nothing(fm_world, settings):
    settings.FMM_ENABLED = False
    assert _probe() is None and not KeyProbe.objects.exists()
    assert command.SWITCHED_OFF in _command("--probe-only")


def test_the_command_writes_a_summary(fm_world):
    out = _command("--probe-only", "--triggered-by", "admin")
    assert "Monitoring insights refresh [probe] succeeded" in out
    assert SyncRun.objects.get(job=SyncRun.Job.FMM_REFRESH).triggered_by == "admin"


def test_a_failed_probe_fails_the_command(fm_world, monkeypatch):
    monkeypatch.setattr(fields, "resolve_all", lambda probes: 1 / 0)
    with pytest.raises(CommandError, match="fmm_refresh"):
        _command("--probe-only")


def test_the_refresh_is_wanted_after_a_sync_of_its_sources():
    def ran(target, status=SyncRun.Status.SUCCEEDED):
        return SyncRun(job=SyncRun.Job.ETOOLS_DATAMART, target=target, status=status)

    assert refresh.wanted_after([ran("grants"), ran("fm_questions")])
    assert refresh.wanted_after([ran("field_monitoring", SyncRun.Status.PARTIAL)])
    assert not refresh.wanted_after([ran("grants"), ran("field_monitoring", SyncRun.Status.FAILED)])
    assert not refresh.wanted_after([])


# ------------------------------------------------------------------------------------------ full pass
TODAY = datetime.date(2026, 10, 5)


def _full(**kw):
    return refresh.run(triggered_by="test", today=kw.pop("today", TODAY), **kw)


def _snapshot() -> dict:
    visit_fields = [f.name for f in Visit._meta.concrete_fields if f.name != "refreshed_at"]
    return {
        "visits": list(Visit.objects.order_by("key").values_list(*visit_fields)),
        "entities": sorted(
            VisitEntity.objects.values_list(
                "visit__key",
                "datamart_id",
                "kind",
                "pd_id",
                "rating",
                "cp_output",
                "hact_q1",
                "hact_q1_from",
                "narrative_hash",
                "narrative_placeholder",
            )
        ),
        "answers": sorted(
            QuestionAnswer.objects.values_list(
                "document_id",
                "visit_key",
                "entity__datamart_id",
                "partner_id",
                "applies_to",
                "answer_code",
                "role",
            )
        ),
        "links": sorted(VisitActionPoint.objects.values_list("visit__key", "action_point_id", "matched_by")),
        "rules": sorted(
            VisitRuleResult.objects.values_list(
                "visit__key", "rule", "status", "points", "max_points", "detail_key", "detail", "measure"
            )
        ),
    }


def test_a_full_refresh_builds_the_visits_with_meaningful_counts(fm_world):
    run = _full()
    assert (run.target, run.status, run.triggered_by) == ("full", SyncRun.Status.SUCCEEDED, "test")
    findings = dm.MonitoringFinding.objects.count()
    questions = dm.DatamartDocument.objects.filter(dataset="fm_questions").count()
    assert run.rows_in == findings + questions and run.rows_written == Visit.objects.count() == 8
    assert run.rows_failed == 0
    assert KeyProbe.objects.exists() and FieldMapping.objects.exists()  # steps 1-3 too
    details = run.details
    for key in ("visits", "findings", "pd_resolved", "location", "governorate", "sections_from",
                "offices_from", "action_points", "questions", "fields_not_found", "activity_ids"):  # fmt: skip
        assert key in details, key
    assert details["rules_version"] == 1 and details["duration_ms"] >= 0
    questions_details = details["questions"]
    assert questions_details["records"] == questions == questions_details["parsed"]
    assert questions_details["unanswered_seen"] is True and questions_details["linked"] == questions
    pd = details["pd_resolved"]
    assert pd["pd_kind_rows"] == pd["exact"] + pd["token"] + pd["base"] + pd["title"] + pd["unresolved"]
    # step 6: the visits scored (1724 is in progress and 1725 cancelled), the roles, the rule results
    assert details["scored"] == Visit.objects.exclude(quality_score=None).count() == 6
    assert questions_details["roles"] == {"q1": 6, "q2": 1, "q3": 2, "psea": 2}
    assert questions_details["q1_applies_to"] == {"entity": 4, "partner": 1, "visit": 1}
    assert details["rule_results"]["R6"] == {"pass": 1, "fail": 1, "na": 4, "nap": 2}
    assert VisitRuleResult.objects.count() == 6 * Visit.objects.count()
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {1}
    assert set(QuestionAnswer.objects.values_list("role", flat=True)) == {"q1", "q2", "q3", "psea", ""}


def test_two_refreshes_give_the_same_rows_and_keep_the_visit_pks(fm_world):
    _full()
    first = _snapshot()
    _full()
    assert _snapshot() == first


def test_a_visit_gone_from_the_data_is_removed_and_reviews_stay(fm_world):
    _full()
    VisitReview.objects.create(visit_key="1725", status=VisitReview.Status.DATA_ISSUE)
    kept = Visit.objects.get(key="1722").pk
    dm.MonitoringFinding.objects.filter(monitoring_activity_id=1725).delete()
    _full()
    assert not Visit.objects.filter(key="1725").exists() and Visit.objects.get(key="1722").pk == kept
    assert VisitReview.objects.filter(visit_key="1725").exists()


def test_a_record_that_cannot_be_read_is_skipped_and_counted_once(fm_world, monkeypatch):
    from neurodb.fmm import build

    row = build._Builder._row
    add = fields.Probe.add

    def flaky_row(self, record):
        if record["datamart_id"] == 102:
            raise ValueError("unreadable finding")
        return row(self, record)

    def flaky_add(self, record):
        if isinstance(record, dict) and record.get("id") == 102:
            raise ValueError("unreadable finding")
        add(self, record)

    monkeypatch.setattr(build._Builder, "_row", flaky_row)
    monkeypatch.setattr(fields.Probe, "add", flaky_add)
    run = _full()
    assert run.status == SyncRun.Status.PARTIAL and run.rows_failed == 1  # met twice, counted once
    assert run.details["errors"][0]["count"] == 2
    assert Visit.objects.get(key="1722").entities == 2


def test_a_failed_build_or_swap_keeps_the_previous_visits_and_keys(fm_world, monkeypatch):
    from neurodb.fmm import build

    _full()
    before, probes = _snapshot(), KeyProbe.objects.count()
    dm.DatamartDocument.objects.filter(dataset="offices").delete()
    dm.MonitoringFinding.objects.filter(datamart_id=103).update(overall_finding_rating="Off Track")

    def broken(self, visits_by_key):
        raise RuntimeError("the swap failed")
        yield  # pragma: no cover

    monkeypatch.setattr(build.BuildResult, "question_answers", broken)
    run = _full()
    assert (run.status, run.error, run.rows_failed) == (
        SyncRun.Status.FAILED,
        "RuntimeError: the swap failed",
        0,
    )
    assert _snapshot() == before  # the deletes of the swap were rolled back
    assert KeyProbe.objects.count() == probes and KeyProbe.objects.filter(dataset="offices").exists()

    monkeypatch.setattr(build, "build_visits", lambda ctx: 1 / 0)
    run = _full()
    assert run.status == SyncRun.Status.FAILED and "ZeroDivisionError" in run.error
    assert _snapshot() == before


def test_one_full_refresh_at_a_time(fm_world, held_elsewhere):
    assert _full() is None and _full(scores_only=True) is None
    assert not SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH).exists()
    assert command.BUSY in _command()


def test_the_command_runs_each_kind(fm_world):
    assert "Monitoring insights refresh [full] succeeded" in _command()
    assert Visit.objects.count() == 8
    assert "Monitoring insights refresh [scores] succeeded" in _command("--scores-only")
    with pytest.raises(CommandError):
        _command("--scores-only", "--probe-only")


# ------------------------------------------------------------------------------------------ scores only
def test_scores_only_reads_no_record_and_keeps_the_answers(fm_world, monkeypatch):
    from neurodb.fmm import build

    _full()
    before = _snapshot()

    def no_records(*args, **kwargs):
        raise AssertionError("a scores-only pass read the records")

    for target, name in (
        (fields, "records"),
        (fields, "probe"),
        (build, "build_visits"),
        (refresh.fm, "relink_findings"),
    ):
        monkeypatch.setattr(target, name, no_records)
    run = _full(scores_only=True)
    assert (run.target, run.status) == ("scores", SyncRun.Status.SUCCEEDED)
    assert run.rows_in == run.rows_written == 8 and run.rows_failed == 0
    assert _snapshot() == before  # answers, entities, links and pks in place


def test_a_later_day_recounts_the_overdue_action_points(fm_world):
    _full(today=datetime.date(2026, 8, 1))
    assert Visit.objects.get(key="1726").action_points_overdue == 0  # due 1 Sep 2026
    _full(scores_only=True, today=TODAY)  # no data changed
    visit = Visit.objects.get(key="1726")
    assert (visit.action_points_open, visit.action_points_overdue, visit.action_points_high_open) == (1, 1, 1)


# ------------------------------------------------------------------------------------------ scoring
def test_scores_only_reads_the_narratives_but_never_a_finding_record(fm_world, monkeypatch):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _full()

    def refused(self, value=None):
        raise AssertionError("a scores-only pass read MonitoringFinding.data")

    monkeypatch.setattr(dm.MonitoringFinding, "data", property(refused, refused))
    with CaptureQueriesContext(connection) as queries:
        run = _full(scores_only=True)
    assert run.status == SyncRun.Status.SUCCEEDED
    sql = [q["sql"] for q in queries.captured_queries]
    assert not [q for q in sql if '"datamart_monitoringfinding"."data"' in q]
    assert any('"datamart_monitoringfinding"."narrative_finding"' in q for q in sql)


def test_a_later_day_raises_urgency_with_no_data_change(fm_world):
    _full(today=datetime.date(2026, 8, 10))
    early = {v.key: v for v in Visit.objects.all()}
    assert early["1727"].urgency_parts["follow_up"] == 0  # 9 days after an off-plan visit: not yet
    assert early["1726"].urgency_parts["follow_up"] == 5  # a high-priority action point due 1 Sep
    _full(scores_only=True, today=TODAY)
    late = {v.key: v for v in Visit.objects.all()}
    assert late["1727"].urgency_parts["follow_up"] == 20  # no follow-up action point after 14 days
    assert late["1727"].urgency == early["1727"].urgency + 20
    assert late["1726"].urgency_parts["follow_up"] == 20  # now overdue: 12, and high priority: 8
    assert late["1726"].urgency == early["1726"].urgency + 15


def test_a_q1_pattern_change_rescored_updates_q1_psea_r3_and_urgency(fm_world):
    from neurodb.fmm.models import ScoreSetting

    _full()
    before = {v.key: v for v in Visit.objects.all()}
    assert (before["1722"].hact_q1, before["1726"].psea_flag, before["1727"].hact_q1) == (
        "off_track",
        True,
        "constrained",
    )
    assert VisitRuleResult.objects.get(visit__key="1728", rule="R3").status == "nap"
    setting = ScoreSetting.load()  # Q1 is now the attendance question, and no question is PSEA
    setting.question_patterns = {**setting.question_patterns, "q1": ["attendance registers"], "psea": []}
    setting.save()
    run = _full(scores_only=True)
    after = {v.key: v for v in Visit.objects.all()}
    assert after["1722"].hact_q1 == "" and VisitEntity.objects.get(datamart_id=101).hact_q1 == ""
    assert after["1726"].psea_flag is None
    assert after["1728"].hact_q1 == "other"  # its attendance answer is "Yes", not a rating
    r3 = dict(VisitRuleResult.objects.filter(rule="R3").values_list("visit__key", "detail_key"))
    assert (r3["1722"], r3["1728"]) == ("q1_missing", "q1_unrecognised")
    assert after["1727"].urgency_parts["rating"] == 0 and after["1727"].urgency < before["1727"].urgency
    roles = set(QuestionAnswer.objects.filter(question_text=OTHER_TEXT).values_list("role", flat=True))
    assert roles == {"q1"} and not QuestionAnswer.objects.filter(role="psea").exists()
    assert run.details["questions"]["roles"] == {"q1": 2, "q2": 1, "q3": 2, "psea": 0}


def test_a_visit_that_cannot_be_scored_is_counted_and_the_others_are_scored(fm_world, monkeypatch):
    one = score._score_one

    def flaky(visit, *args):
        if visit.key == "1722":
            raise ValueError("cannot score")
        return one(visit, *args)

    monkeypatch.setattr(score, "_score_one", flaky)
    for kw in ({}, {"scores_only": True}):
        run = _full(**kw)
        assert (run.status, run.rows_failed) == (SyncRun.Status.PARTIAL, 1), kw
        visit = Visit.objects.get(key="1722")
        assert (visit.quality_score, visit.not_scored_reason, visit.hact_q1, visit.urgency) == (
            None,
            "could not be scored",
            "",
            0,
        )
        assert not VisitRuleResult.objects.filter(visit=visit).exists()
        assert Visit.objects.exclude(quality_score=None).count() == 5


def test_a_rules_version_saved_during_a_full_pass_with_the_real_scorer(fm_world, monkeypatch, started):
    from neurodb.fmm import versions as rule_versions

    load, saved = score.Rulebook.load, []

    def load_then_save():
        book = load()
        if not saved:  # saved after this pass read the version, without a request
            saved.append(rule_versions.record_rules(None, "saved meanwhile"))
        return book

    monkeypatch.setattr(score.Rulebook, "load", staticmethod(load_then_save))
    _full()
    assert [(target, version) for target, _, version in _passes()] == [("full", 1), ("scores", 2)]
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {2}


# ------------------------------------------------------------------------------------------ requests
@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(background, "start_command", lambda *args: calls.append(args) or 1)
    return calls


@pytest.fixture
def versions(monkeypatch):
    """The current rules version, as a test sets it (without saving a rule)."""
    current = {"n": 1}
    monkeypatch.setattr(refresh, "current_rules_version", lambda: current["n"])
    return current


def _passes() -> list[tuple[str, str, int]]:
    return list(
        SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH)
        .order_by("started_at", "pk")
        .values_list("target", "triggered_by", "details__rules_version")
    )


def test_a_request_starts_the_command_on_commit_with_varargs(db, started, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        refresh.request("scores", "admin:5")
        assert started == []  # not before the save is committed
    assert started == [("fmm_refresh", "--scores-only", "--triggered-by", "admin:5")]
    with django_capture_on_commit_callbacks(execute=True):
        refresh.request("full", "admin:5")
    assert started[-1] == ("fmm_refresh", "--triggered-by", "admin:5")
    row = RefreshRequest.load()
    assert row.scores_requested_at and row.full_requested_at and row.requested_by == "admin:5"
    assert status.rescore_pending()
    with pytest.raises(ValueError):
        refresh.request("everything", "admin:5")


def _in_another_process(**kw):
    """Run a refresh in another database connection, as the command a save starts would."""
    out = {}

    def target():
        try:
            out["run"] = refresh.run(**kw)
        finally:
            connections.close_all()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    return out["run"]


def test_a_rescore_asked_while_a_refresh_runs_is_served_by_that_refresh(fm_world, versions, monkeypatch):
    """A rule saved while a full refresh holds the lock: its command finds the lock held and stops; the
    refresh sees the request before it releases the lock and runs the scores-only pass."""
    calls = []

    def scorer(visits, today, version, **kw):  # the scoring step; the save lands during the first
        calls.append(version)
        if len(calls) == 1:
            versions["n"] = 2
            refresh.request("scores", "admin:7")  # the save (its command starts after the commit:)
            assert _in_another_process(triggered_by="admin:7", scores_only=True) is None  # lock held
        return score.Scored()

    monkeypatch.setattr(refresh, "_score", scorer)
    last = _full()
    assert _passes() == [("full", "test", 1), ("scores", "admin:7", 2)] and last.target == "scores"
    assert calls == [1, 2]
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {2}
    assert RefreshRequest.load().scores_requested_at is None and not status.rescore_pending()


def test_a_request_written_just_before_the_lock_is_released_is_served_after(
    fm_world, versions, monkeypatch, started
):
    wanted = refresh._wanted
    looks = []

    def late(since, last, triggered_by):
        answer = wanted(since, last, triggered_by)
        looks.append(answer)
        if len(looks) == 1:  # the request lands right after the end-of-run look
            versions["n"] = 2
            refresh.request("scores", "admin:8")
        return answer

    monkeypatch.setattr(refresh, "_wanted", late)
    _full()
    assert looks[0] == (None, "")  # nothing yet at the end-of-run look; the look after release saw it
    assert _passes() == [("full", "test", 1), ("scores", "admin:8", 2)]
    assert set(Visit.objects.values_list("rules_version", flat=True)) == {2}


def test_a_rules_version_saved_during_a_full_run_gives_a_second_pass(fm_world, versions, monkeypatch):
    def scorer(visits, today, version, **kw):
        versions["n"] = version + 1  # saved after this pass read the version
        return score.Scored()

    monkeypatch.setattr(refresh, "_score", scorer)
    monkeypatch.setattr(refresh.settings, "FMM_REFRESH_MAX_PASSES", 2)
    _full()
    assert [(target, version) for target, _, version in _passes()] == [("full", 1), ("scores", 2)]


def test_the_passes_of_one_run_are_bounded(fm_world, versions, monkeypatch, settings, started):
    settings.FMM_REFRESH_MAX_PASSES = 3

    def scorer(visits, today, version, **kw):
        refresh.request("scores", "admin:9")  # every pass sees a newer request
        return score.Scored()

    monkeypatch.setattr(refresh, "_score", scorer)
    _full()
    assert [target for target, _, _ in _passes()] == ["full", "scores", "scores"]
    assert status.rescore_pending()  # the last request waits for the next run


def test_a_failed_pass_leaves_its_request(fm_world, versions, monkeypatch, started):
    refresh.request("full", "admin:3")
    monkeypatch.setattr(refresh.build, "build_visits", lambda ctx: 1 / 0)
    run = _full()
    assert run.status == SyncRun.Status.FAILED and len(_passes()) == 1
    assert RefreshRequest.load().full_requested_at is not None and status.rescore_pending()


def test_a_served_request_is_cleared(fm_world, versions, started):
    refresh.request("scores", "admin:3")
    _full(scores_only=True)
    assert RefreshRequest.load().scores_requested_at is None and not status.rescore_pending()
    refresh.request("scores", "admin:3")
    refresh.request("full", "admin:3")
    _full()  # a full pass serves both
    row = RefreshRequest.load()
    assert row.scores_requested_at is None and row.full_requested_at is None
    assert [target for target, _, _ in _passes()] == ["scores", "full"]


def test_a_full_request_left_by_a_scores_only_pass_is_served_by_the_same_run(fm_world, versions, started):
    """A key pinned just before a scores-only run took the lock: its own full command found the lock
    held and stopped. The scores-only pass does not serve it, so the same run makes the full pass
    after it, instead of leaving it to the next morning."""
    _full()
    refresh.request("scores", "admin:3")
    refresh.request("full", "admin:4")
    last = _full(scores_only=True)
    assert [(target, who) for target, who, _ in _passes()] == [
        ("full", "test"),
        ("scores", "test"),
        ("full", "admin:4"),
    ]
    assert last.target == "full" and not status.rescore_pending()


def test_a_request_left_by_the_key_probe_is_served_by_the_same_run(fm_world, versions, started):
    refresh.request("scores", "admin:3")
    _probe()
    assert [target for target, _, _ in _passes()] == ["probe", "scores"]
    assert RefreshRequest.load().scores_requested_at is None


def test_a_request_committed_after_a_pass_read_the_settings_is_not_lost(
    fm_world, versions, started, django_capture_on_commit_callbacks
):
    """A save stamps its request inside its own transaction. A pass that starts before the commit reads
    the old settings, and then clears that stamp as served; the stamp written again at the commit is
    newer than that pass, so the request waits for (and gets) a pass of its own."""
    with django_capture_on_commit_callbacks() as callbacks:  # the save, not committed yet
        refresh.request("full", "admin:6")
    _full()  # started before the commit
    assert RefreshRequest.load().full_requested_at is None
    for callback in callbacks:  # the commit
        callback()
    assert RefreshRequest.load().full_requested_at is not None and status.rescore_pending()
    assert started == [("fmm_refresh", "--triggered-by", "admin:6")]
    _full()  # the command it started
    assert RefreshRequest.load().full_requested_at is None and not status.rescore_pending()


def test_scores_are_pending_while_a_visit_has_an_older_rules_version(fm_world, versions):
    _full()
    assert not status.rescore_pending()
    versions["n"] = 2
    assert status.rescore_pending()


# ------------------------------------------------------------------------------------------ after a sync
@pytest.fixture
def datamart_sync(monkeypatch):
    """``sync_etools_datamart`` with its sync replaced: ``targets`` are the datasets it says it synced."""
    from neurodb.integrations.management.commands import sync_etools_datamart as sync_command

    targets = []

    def fake_sync_all(only=None, triggered_by="command"):
        return [
            SyncRun.objects.create(job=SyncRun.Job.ETOOLS_DATAMART, target=t, status=SyncRun.Status.SUCCEEDED)
            for t in targets
        ]

    monkeypatch.setattr(sync_command, "sync_all", fake_sync_all)

    def run(*names) -> str:
        targets[:] = names
        out = StringIO()
        call_command("sync_etools_datamart", stdout=out)
        return out.getvalue()

    return run


def _refreshes():
    return SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH)


def test_the_refresh_runs_after_a_sync_of_its_sources_once_the_datamart_lock_is_released(
    fm_world, datamart_sync, monkeypatch
):
    inner = refresh.run
    held = []

    def watched(**kw):
        held.append(background.datamart_lock_is_held())
        return inner(**kw)

    monkeypatch.setattr(refresh, "run", watched)
    datamart_sync("grants")
    assert not _refreshes().exists()
    out = datamart_sync("field_monitoring")
    assert held == [False]
    assert _refreshes().get().target == "full" and Visit.objects.count() == 8
    assert "Monitoring insights refresh [full] succeeded" in out


def test_a_refresh_that_fails_never_fails_the_sync(fm_world, datamart_sync, monkeypatch):
    monkeypatch.setattr(refresh, "_full", lambda triggered_by, today: 1 / 0)
    out = datamart_sync("fm_questions")  # no CommandError
    run = _refreshes().get()
    assert run.status == SyncRun.Status.FAILED and "ZeroDivisionError" in run.error
    assert "Monitoring insights refresh [full] failed" in out


def test_the_refresh_after_a_sync_can_run_in_the_background_or_not_at_all(
    fm_world, datamart_sync, settings, started
):
    settings.FMM_REFRESH_AFTER_SYNC = "background"
    datamart_sync("field_monitoring")
    assert started == [("fmm_refresh", "--triggered-by", "command")] and not _refreshes().exists()
    settings.FMM_REFRESH_AFTER_SYNC = "off"
    datamart_sync("field_monitoring")
    settings.FMM_REFRESH_AFTER_SYNC, settings.FMM_ENABLED = "inline", False
    datamart_sync("field_monitoring")
    assert len(started) == 1 and not _refreshes().exists()


def test_a_refresh_that_cannot_start_never_fails_the_sync(
    fm_world, datamart_sync, settings, monkeypatch, caplog
):
    def broken(*args):
        raise OSError("cannot start a process")

    settings.FMM_REFRESH_AFTER_SYNC = "background"
    monkeypatch.setattr(background, "start_command", broken)
    out = datamart_sync("field_monitoring")  # no CommandError, no traceback
    assert "could not be started after this sync" in out and "could not start" in caplog.text
    assert not _refreshes().exists()


def test_a_request_made_during_the_key_probe_is_served_too(fm_world, versions, monkeypatch, started):
    probe = refresh._probe

    def probing(triggered_by):
        refresh.request("full", "admin:4")  # a key pinned while the probe runs
        return probe(triggered_by)

    monkeypatch.setattr(refresh, "_probe", probing)
    refresh.run(triggered_by="test", probe_only=True)
    monkeypatch.setattr(refresh, "_probe", probe)
    assert [(target, who) for target, who, _ in _passes()] == [("probe", "test"), ("full", "admin:4")]
    assert Visit.objects.exists() and RefreshRequest.load().full_requested_at is None


def test_rows_that_all_fail_never_empty_the_visits(fm_world, monkeypatch):
    from neurodb.fmm import build

    _full()
    before = _snapshot()

    def broken(self, record):
        raise ValueError("unreadable finding")

    monkeypatch.setattr(build._Builder, "_row", broken)
    run = _full()
    assert run.status == SyncRun.Status.FAILED and "none of the" in run.error
    assert _snapshot() == before
