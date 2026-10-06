"""The AI checks of the narrative quality rules (``fmm.ai.checks``, stage B3): one call per visit and
rule with the rule's prompt and fields, the strict answer format, the cache (a visit and rule checked
again only when its inputs or the rule's prompt change), provisional scores while checks are pending,
the nightly job (newest first, the day's budget, the pause, failures in a row, the rescore at the end,
its button and schedule), and what may never be sent: the team, the visit lead, monitors' e-mail
addresses and who an action point is assigned to."""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace

import openai
import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import override_settings

from neurodb.assistant import agent, usage
from neurodb.assistant.models import AIUsage
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart import models as dm
from neurodb.fmm import refresh
from neurodb.fmm.ai import budget, checks, profiles
from neurodb.fmm.models import AIState, ScoreSetting, Visit, VisitAICheck, VisitRuleResult

from .conftest import CANARIES, LEAD, FakeChecks

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
AI_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}
AI_RULES = ("R3", "R5", "R6", "R7", "R8", "R32")
SCORED = (
    "1722",
    "1723",
    "1726",
    "1727",
    "1728",
    "r-e4e046b73e2c",
)  # completed: report finalization or completed


@pytest.fixture
def ai_on():
    from neurodb.watch import people

    people.forget()
    with override_settings(**AI_ON):
        yield


@pytest.fixture
def built(built):
    """The built world before any AI check: the checks it was built with are forgotten."""
    VisitAICheck.objects.all().delete()
    _rescore()
    return built


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        found = FakeChecks(**kw)
        monkeypatch.setattr(agent, "client", found.client)
        return found

    return install


def _rescore():
    run = refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    assert run.status == "succeeded", run.error


def _run(**kw) -> SyncRun:
    return checks.run("test", **kw)


# ------------------------------------------------------------------------------------------ provisional
def test_while_checks_are_pending_a_visit_is_provisional_and_not_scored(built, ai_on):
    _rescore()
    visit = Visit.objects.get(key="1722")
    assert (visit.quality_score, visit.urgency, visit.ai_pending) == (None, None, len(AI_RULES))
    assert (
        visit.provisional_score is not None and visit.not_scored_reason == "provisional: 6 AI checks pending"
    )
    statuses = dict(VisitRuleResult.objects.filter(visit=visit).values_list("rule", "status"))
    assert {statuses[code] for code in AI_RULES} == {"pending"}
    assert Visit.objects.filter(quality_score__isnull=False).count() == 0  # never full marks meanwhile
    assert (
        refresh.SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH)
        .latest("started_at")
        .details["provisional"]
        == 6
    )


def test_with_the_ai_off_the_ai_rules_are_off_and_the_scores_final(built):
    visit = Visit.objects.get(key="1722")
    assert visit.quality_score is not None and visit.ai_pending == 0
    statuses = dict(VisitRuleResult.objects.filter(visit=visit).values_list("rule", "status"))
    assert {statuses[code] for code in AI_RULES} == {"off"}


# ------------------------------------------------------------------------------------------ the job
def test_the_job_checks_every_visit_once_newest_first_and_rescores(built, ai_on, fake):
    found = fake()
    run = _run()
    assert (run.status, run.rows_written, run.rows_failed) == ("succeeded", 36, 0)
    assert run.details["checked"] == 36 and run.details["rescored"] == "succeeded"
    first = found.sent()[0]
    assert first["visit"] == "Visit 1727"  # ended 1 August 2026: the newest scored visit
    request = found.requests[0]
    assert request["model"] == settings.AI_ASSISTANT_MODEL
    assert (request["store"], request["reasoning"], request["max_output_tokens"]) == (
        False,
        {"effort": "low"},
        2000,
    )
    assert request["temperature"] == 0.3
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["required"] == ["is_coherent", "detail"]
    assert {r["prompt_cache_key"] for r in found.requests} == {f"neurodb-fmm-check-{c}" for c in AI_RULES}
    assert "Q2 must demonstrate INDEPENDENT VERIFICATION" in next(
        r["instructions"] for r in found.requests if r["prompt_cache_key"].endswith("R3")
    )
    assert all("Never name or describe a person" in r["instructions"] for r in found.requests)
    assert found.options[0]["timeout"] == settings.FMM_RULES_TIMEOUT_SECONDS
    assert VisitAICheck.objects.count() == 36
    visit = Visit.objects.get(key="1722")
    assert visit.quality_score is not None and visit.ai_pending == 0 and visit.urgency is not None
    assert AIUsage.objects.get(feature=usage.FMM_RULES).calls == 36
    assert not AIUsage.objects.filter(feature=usage.FMM).exists()
    again = _run()
    assert (again.rows_written, again.details["up_to_date"]) == (0, 36) and len(found.requests) == 36


def test_a_flag_carries_the_ai_explanation_cleaned_and_checked(built, ai_on, fake):
    fake(
        verdicts={
            "R3": (False, f"Q2 restates the partner's report; ask {LEAD} at karim.canary@example.org."),
            "R6": (False, "The narrative cites 9999 children that the report does not mention."),
        }
    )
    _run()
    r3 = VisitRuleResult.objects.get(visit__key="1722", rule="R3")
    assert r3.status == "fail" and r3.detail.startswith(
        "R3: Q2 lacks specific or disaggregated activity evidence — "
    )
    assert LEAD not in r3.detail and "@" not in r3.detail and "[name withheld]" in r3.detail
    r6 = VisitRuleResult.objects.get(visit__key="1722", rule="R6")
    assert (
        r6.detail
        == "R6: General Observation is incoherent, duplicates Q2, or does not address visit objective"
    )
    visit = Visit.objects.get(key="1722")
    assert {"R3", "R6"} <= set(visit.flags)
    assert visit.category_deductions["evidence"] == 20.0 and visit.category_deductions["coherence"] == 15.0


def test_no_person_ever_reaches_a_check(built, ai_on, fake):
    point = dm.ActionPoint.objects.get(datamart_id=8001)
    point.assigned_to_name, point.description = (
        LEAD,
        f"Follow up the registers with {LEAD} (rania@x.example).",
    )
    point.save()
    found = fake()
    _run()
    blob = json.dumps([r["input"] for r in found.requests]) + json.dumps(
        [r["instructions"] for r in found.requests]
    )
    for canary in (*CANARIES, "rania@x.example"):
        assert canary not in blob, canary
    r7 = [
        s for s, r in zip(found.sent(), found.requests, strict=True) if r["prompt_cache_key"].endswith("R7")
    ]
    visit_1722 = next(s for s in r7 if s["visit"] == "Visit 1722")
    assert visit_1722["action_points_assigned_to"] == "1 of 1 action points assigned"
    assert "team_members" not in blob and "visit_lead" not in blob


def test_a_check_is_made_again_only_when_its_inputs_or_its_prompt_change(built, ai_on, fake):
    found = fake()
    _run()
    row = dm.MonitoringFinding.objects.get(datamart_id=101)
    row.narrative_finding = "Classes held as planned; two of the three rooms were in use."
    row.save()
    _run()
    redone = [
        (s["visit"], r["prompt_cache_key"].rsplit("-", 1)[1])
        for s, r in zip(found.sent()[36:], found.requests[36:], strict=True)
    ]
    # the rules that read the narrative, on that visit only
    assert sorted(redone) == [("Visit 1722", code) for code in ("R3", "R32", "R6", "R7", "R8")]
    published = profiles.published()
    prompts = dict(published.rule_prompts)
    prompts["hact_q1_q2_alignment"] += "\nBe strict."
    draft = profiles.draft_from(published, None, "stricter Q1-Q2 check", rule_prompts=prompts)
    profiles.publish(draft, None)
    _rescore()
    assert Visit.objects.get(key="1722").ai_pending == 1  # R5's answers are out of date
    _run()
    last = found.requests[41:]
    assert {r["prompt_cache_key"] for r in last} == {"neurodb-fmm-check-R5"} and len(last) == len(SCORED)


def test_the_days_budget_stops_the_job_and_the_rest_waits_for_the_next_run(built, ai_on, fake):
    found = fake()
    with override_settings(FMM_RULES_DAILY_TOKEN_CAP=12_000):
        run = _run()
    assert run.details["stopped"].startswith("budget") and 0 < run.rows_written < 36
    assert len(found.requests) == run.rows_written
    assert Visit.objects.filter(ai_pending__gt=0).exists()  # rescored: the rest provisional
    rest = _run()
    assert run.rows_written + rest.rows_written == 36 and rest.details["stopped"] == ""


def test_the_shared_soft_cap_also_stops_it(built, ai_on, fake):
    fake()
    usage.record(
        "ask",
        "gpt",
        SimpleNamespace(input_tokens=int(settings.AI_DAILY_TOKEN_SOFT_CAP * 0.8), output_tokens=0),
    )
    run = _run()
    assert run.rows_written == 0 and run.details["stopped"].startswith("budget")


def test_the_credit_running_out_pauses_the_ai_and_three_failures_stop_the_job(built, ai_on, fake):
    import httpx2

    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    quota = openai.RateLimitError(
        "You exceeded your current quota", response=httpx2.Response(429, request=request), body=None
    )
    fake(errors=[quota])
    run = _run()
    assert run.details["stopped"] == "paused: the OpenAI credit ran out" and run.rows_written == 0
    assert budget.paused_until() is not None
    assert _run().details["skipped"] == budget.REASONS[budget.PAUSED]
    AIState.objects.update(paused_until=None)
    busy = openai.RateLimitError("Busy", response=httpx2.Response(429, request=request), body=None)
    fake(errors=[busy, busy, busy])
    run = _run()
    assert (run.rows_failed, run.rows_written, run.details["stopped"]) == (3, 0, "3 failed checks in a row")
    assert run.status == "partial"


def test_an_answer_that_is_not_the_format_is_not_kept(built, ai_on, fake, monkeypatch):
    found = fake()
    original = found.create

    def broken(**params):
        answer = original(**params)
        answer.output_text = '{"verdict": "fine"}'
        return answer

    found.create = broken
    run = _run()
    assert (run.rows_failed, VisitAICheck.objects.count()) == (3, 0)  # nothing kept; 3 in a row stop it
    assert run.details["stopped"] == "3 failed checks in a row"


def test_switched_off_nothing_is_checked(built, fake):
    found = fake()
    assert _run().details["skipped"] == "AI is switched off"
    with override_settings(**AI_ON):
        setting = ScoreSetting.load()
        setting.ai_checks = False
        setting.save()
        assert _run().details["skipped"].startswith("AI checks are switched off")
        _rescore()
    assert found.requests == [] and Visit.objects.get(key="1722").ai_pending == 0


def test_the_model_and_its_settings_come_from_the_score_settings(built, ai_on, fake):
    setting = ScoreSetting.load()
    setting.ai_model, setting.ai_max_output_tokens, setting.ai_temperature = "gpt-other", 3000, None
    setting.save()
    found = fake()
    _run(limit=1)
    request = found.requests[0]
    assert (request["model"], request["max_output_tokens"]) == ("gpt-other", 3000)
    assert "temperature" not in request


def test_the_command_and_its_button_and_schedule(built, ai_on, fake):
    from neurodb.core.admin_jobs import BACKGROUND_JOBS
    from neurodb.core.jobs import COMMANDS
    from neurodb.integrations import background

    found = fake()
    call_command("fmm_ai_checks", "--limit", "3", "--triggered-by", "test")
    run = SyncRun.objects.get(job=SyncRun.Job.FMM_AI_CHECKS)
    assert (run.rows_written, run.triggered_by) == (3, "test") and len(found.requests) == 3
    assert run.details["stopped"] == "stopped after 3 checks"
    assert COMMANDS["fmm_ai_checks"].args == ("fmm_ai_checks",)
    assert ("fmm_ai_checks",) in [spec.command for spec in BACKGROUND_JOBS]
    assert background.LOCK_IDS[SyncRun.Job.FMM_AI_CHECKS] == checks.LOCK_ID == 7140434
    job = ScheduledJob.objects.get(key="fmm-ai-checks")
    assert (job.command, job.schedule, job.enabled) == ("fmm_ai_checks", "50 5 * * *", True)


def test_the_checks_of_a_visit_gone_from_etools_are_deleted(built, ai_on, fake):
    fake()
    _run(limit=1)
    VisitAICheck.objects.create(
        visit_key="gone",
        rule="R3",
        input_hash="x",
        prompt_hash="y",
        passed=True,
        checked_at=datetime.datetime.now(datetime.UTC),
    )
    run = refresh.run(triggered_by="test", today=TODAY)
    assert run.status == "succeeded"
    assert not VisitAICheck.objects.filter(visit_key="gone").exists() and VisitAICheck.objects.count() == 1


def test_the_visit_page_shows_a_provisional_score_and_its_pending_checks(built, ai_on, fake, client_viewer):
    fake()
    _run(limit=6)  # the checks of 1727 only, the newest
    assert list(Visit.objects.filter(quality_score__isnull=False).values_list("key", flat=True)) == ["1727"]
    html = client_viewer.get("/fmm/visits/1722/").content.decode()
    assert "provisional (6 AI checks pending)" in html and ">Pending<" in html
    done = client_viewer.get("/fmm/visits/1727/").content.decode()
    assert "AI check passed: The report is specific." in done
