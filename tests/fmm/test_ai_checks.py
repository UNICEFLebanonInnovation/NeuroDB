"""The AI checks of the narrative quality rules (``fmm.ai.checks``, stage B3, per record since Release 2
step 5): one call per record and rule with the rule's prompt and fields, the strict answer format, the
answers kept by what was asked (a record checked again only when its inputs or the rule's prompt change;
records with the same texts share one answer), provisional scores while checks are pending, the carry-
over of the checks made per visit, the nightly job (this year's records of visits with several records
first, the day's budget and what is left, the pause, failures in a row, the rescore at the end, its
button and schedule), and what may never be sent: the visit's label, the entity's name, the team, the
visit lead, monitors' e-mail addresses and who an action point is assigned to."""

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
from neurodb.fmm import refresh, score
from neurodb.fmm.ai import budget, checks, profiles
from neurodb.fmm.models import (
    AICheckAnswer,
    AIState,
    RecordRuleResult,
    ScoreSetting,
    Visit,
    VisitAICheck,
    VisitEntity,
    VisitRuleResult,
)

from .conftest import CANARIES, LEAD, PD_BASE, PD_EDU, SSFA, FakeChecks

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
AI_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}
AI_RULES = ("R3", "R5", "R6", "R7", "R8", "R32")
RECORDS = 10  # the records of the scored visits (1722: 3, 1723: 2, 1726: 2, the others 1 each)
CALLS = 52  # their 60 checks: 1726's two programme documents send the same texts, 1727 and the visit
# known by its reference the same R3 input, 1723's SSFA and 1728's the same R3 input
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
    AICheckAnswer.objects.all().delete()
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
    pending = 3 * len(AI_RULES)  # its three records' checks
    assert (visit.quality_score, visit.urgency, visit.ai_pending) == (None, None, pending)
    assert visit.provisional_score is not None
    assert visit.not_scored_reason == f"provisional: {pending} AI checks pending"
    statuses = dict(VisitRuleResult.objects.filter(visit=visit).values_list("rule", "status"))
    assert {statuses[code] for code in AI_RULES} == {"pending"}
    assert Visit.objects.filter(quality_score__isnull=False).count() == 0  # never full marks meanwhile
    assert VisitEntity.objects.filter(quality_score__isnull=False).count() == 0
    details = refresh.SyncRun.objects.filter(job=SyncRun.Job.FMM_REFRESH).latest("started_at").details
    assert (details["provisional"], details["records_provisional"]) == (6, RECORDS)


def test_with_the_ai_off_the_ai_rules_are_off_and_the_scores_final(built):
    visit = Visit.objects.get(key="1722")
    assert visit.quality_score is not None and visit.ai_pending == 0
    statuses = dict(VisitRuleResult.objects.filter(visit=visit).values_list("rule", "status"))
    assert {statuses[code] for code in AI_RULES} == {"off"}


# ------------------------------------------------------------------------------------------ the job
def test_the_job_checks_every_record_once_multi_record_visits_first_and_rescores(built, ai_on, fake):
    found = fake()
    run = _run()
    assert (run.status, run.rows_written, run.rows_failed) == ("succeeded", CALLS, 0)
    assert run.details["checked"] == CALLS and run.details["rescored"] == "succeeded"
    assert (run.details["shared"], run.details["records"], run.details["visits"]) == (8, RECORDS, 6)
    assert (
        run.details["records_pending"],
        run.details["checks_pending"],
        run.details["nights_estimate"],
    ) == (
        0,
        0,
        0,
    )
    first = found.sent()[0]
    # this year's visits with several records first, the newest of them (1726, started 15 July) first
    assert first == {
        "entity_type": "PD/SSFA",
        "hact_q2_answer": "",
        "narrative_finding": "Sessions were delayed and suspended for two weeks.",
    }
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
    assert AICheckAnswer.objects.count() == CALLS and not AICheckAnswer.objects.filter(carried=True).exists()
    visit = Visit.objects.get(key="1722")
    assert visit.quality_score is not None and visit.ai_pending == 0 and visit.urgency is not None
    assert not VisitEntity.objects.filter(visit__status="completed", quality_score=None).exists()
    assert AIUsage.objects.get(feature=usage.FMM_RULES).calls == CALLS
    assert not AIUsage.objects.filter(feature=usage.FMM).exists()
    again = _run()
    assert (again.rows_written, again.details["up_to_date"], again.details["shared"]) == (0, 60, 0)
    assert len(found.requests) == CALLS


def test_a_flag_carries_the_ai_explanation_cleaned_and_checked(built, ai_on, fake):
    fake(
        verdicts={
            "R3": (False, f"Q2 restates the partner's report; ask {LEAD} at karim.canary@example.org."),
            "R6": (False, "The narrative cites 9999 children that the report does not mention."),
        }
    )
    _run()
    r3 = RecordRuleResult.objects.get(entity__datamart_id=101, rule="R3")
    assert r3.status == "fail" and r3.detail.startswith(
        "R3: Q2 lacks specific or disaggregated activity evidence — "
    )
    assert LEAD not in r3.detail and "@" not in r3.detail and "[name withheld]" in r3.detail
    r6 = RecordRuleResult.objects.get(entity__datamart_id=101, rule="R6")
    assert (
        r6.detail
        == "R6: General Observation is incoherent, duplicates Q2, or does not address visit objective"
    )
    # the visit's own result names the record that failed it first
    assert VisitRuleResult.objects.get(visit__key="1722", rule="R3").detail.startswith("(PD/SSFA) R3: ")
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
    record_1722 = next(s for s in r7 if "Registers checked." in s["hact_q3_answer"])
    assert record_1722["action_points_assigned_to"] == "1 of 1 action points assigned"
    assert "team_members" not in blob and "visit_lead" not in blob


def test_a_full_refresh_reads_the_answers_the_job_made(built, ai_on, fake):
    """The records a full refresh builds send what the job's records sent: no answer is out of date
    and nothing is checked again (each record's inputs hash the same from the build as from the store)."""
    found = fake()
    _run()
    scores = dict(VisitEntity.objects.values_list("datamart_id", "quality_score"))
    run = refresh.run(triggered_by="test", today=TODAY)
    assert run.status == "succeeded", run.error
    assert not VisitEntity.objects.filter(ai_pending__gt=0).exists()
    assert dict(VisitEntity.objects.values_list("datamart_id", "quality_score")) == scores
    assert _run().details["up_to_date"] == 60 and len(found.requests) == CALLS


def test_a_check_is_made_again_only_when_its_inputs_or_its_prompt_change(built, ai_on, fake):
    found = fake()
    _run()
    row = dm.MonitoringFinding.objects.get(datamart_id=101)
    row.narrative_finding = "Classes held as planned; two of the three rooms were in use."
    row.save()
    _run()
    redone = [
        (s["entity_type"], s["narrative_finding"], r["prompt_cache_key"].rsplit("-", 1)[1])
        for s, r in zip(found.sent()[CALLS:], found.requests[CALLS:], strict=True)
    ]
    # the rules that read the narrative, on that record only
    assert sorted(redone) == [
        ("PD/SSFA", "Classes held as planned; two of the three rooms were in use.", code)
        for code in ("R3", "R32", "R6", "R7", "R8")
    ]
    published = profiles.published()
    prompts = dict(published.rule_prompts)
    prompts["hact_q1_q2_alignment"] += "\nBe strict."
    draft = profiles.draft_from(published, None, "stricter Q1-Q2 check", rule_prompts=prompts)
    profiles.publish(draft, None)
    _rescore()
    assert Visit.objects.get(key="1722").ai_pending == 3  # R5's answers are out of date, on 3 records
    _run()
    last = found.requests[CALLS + 5 :]
    # one per distinct R5 input: 1726's two records still share theirs
    assert {r["prompt_cache_key"] for r in last} == {"neurodb-fmm-check-R5"} and len(last) == RECORDS - 1


def test_the_days_budget_stops_the_job_and_the_rest_waits_for_the_next_run(built, ai_on, fake):
    found = fake()
    with override_settings(FMM_RULES_DAILY_TOKEN_CAP=12_000):
        run = _run()
    assert run.details["stopped"].startswith("budget") and 0 < run.rows_written < CALLS
    assert len(found.requests) == run.rows_written
    assert Visit.objects.filter(ai_pending__gt=0).exists()  # rescored: the rest provisional
    # what waits: every record still provisional, the calls they need, the nights at this pace
    left = CALLS - run.rows_written
    assert run.details["checks_pending"] == left
    assert run.details["records_pending"] == VisitEntity.objects.filter(ai_pending__gt=0).count() > 0
    assert run.details["nights_estimate"] == -(-left // run.rows_written)
    rest = _run()
    assert run.rows_written + rest.rows_written == CALLS and rest.details["stopped"] == ""
    assert rest.details["records_pending"] == 0 and rest.details["nights_estimate"] == 0


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
    assert (run.rows_failed, AICheckAnswer.objects.count()) == (3, 0)  # nothing kept; 3 in a row stop it
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


def test_the_checks_made_per_visit_of_a_visit_gone_from_etools_are_deleted(built, ai_on, fake):
    # a visit gone and one with several records: dropped; a single record's rule switched off (R9): kept
    for key, rule in (("gone", "R3"), ("1722", "R3"), ("1727", "R9")):
        VisitAICheck.objects.create(
            visit_key=key,
            rule=rule,
            input_hash="x",
            prompt_hash="y",
            passed=True,
            checked_at=datetime.datetime.now(datetime.UTC),
        )
    run = refresh.run(triggered_by="test", today=TODAY)
    assert run.status == "succeeded"
    assert run.details["ai_checks_carried"] == {"carried": 0, "dropped": 2, "left": 1}
    assert list(VisitAICheck.objects.values_list("visit_key", flat=True)) == ["1727"]


def test_the_visit_page_shows_a_provisional_score_and_its_pending_checks(built, ai_on, fake, client_viewer):
    fake()
    _run(limit=6)  # the checks of 1726's records only: first in the order, its two share their texts
    assert list(Visit.objects.filter(quality_score__isnull=False).values_list("key", flat=True)) == ["1726"]
    html = client_viewer.get("/fmm/visits/1722/").content.decode()
    assert "provisional (18 AI checks pending)" in html and ">Pending<" in html
    done = client_viewer.get("/fmm/visits/1726/").content.decode()
    assert "AI check passed: The report is specific." in done


# ------------------------------------------------------------------------------------------ records
NAMES = ("Amel Association", "Mercy Corps Lebanon", PD_BASE, PD_EDU, SSFA, "Visit 17", "FM-2026", "FM/2026")


def test_a_check_sends_one_record_without_the_visit_or_the_entitys_name(built, ai_on, fake):
    found = fake()
    _run()
    for sent in found.sent():
        assert "entity_type" in sent and not {"visit", "entities", "entity"} & set(sent)
    blob = json.dumps(found.sent(), ensure_ascii=False)
    for name in NAMES:
        assert name not in blob, name
    # the visit's own fields are repeated on each record, as FMS does
    r8 = [
        s for s, r in zip(found.sent(), found.requests, strict=True) if r["prompt_cache_key"].endswith("R8")
    ]
    assert {s["action_points_count"] for s in r8} == {0, 1}


def test_records_with_the_same_texts_share_one_check(built, ai_on, fake):
    found = fake(verdicts={("Sessions were delayed", "R8"): (False, "The delays have no action point.")})
    run = _run()
    asked = [json.dumps(s, sort_keys=True) for s in found.sent()]
    assert len(asked) == len(set(asked)) == CALLS  # never the same question twice
    first, second = (RecordRuleResult.objects.get(entity__datamart_id=n, rule="R8") for n in (121, 122))
    assert (
        (first.status, first.detail)
        == (second.status, second.detail)
        == (
            "fail",
            "R8: Bottlenecks or issues identified in narrative/Q1/Q2 but Q3 lacks corresponding action points — "
            "The delays have no action point.",
        )
    )
    assert run.details["shared"] == 8 and AICheckAnswer.objects.filter(rule="R8", passed=False).count() == 1


def _legacy_checks(book, keys, passed=True):
    """The checks Release 2 made per visit, as it kept them (its payload and prompt hash)."""
    items = checks._items(list(Visit.objects.filter(key__in=keys).order_by("key")))
    inputs = checks.collect(items)
    for item in items:
        for rule in book.ai_rules():
            prompt = book.prompts[rule.params["ai_prompt_key"]]
            VisitAICheck.objects.create(
                visit_key=item.key,
                rule=rule.code,
                input_hash=checks.input_hash(checks.legacy_payload(inputs[item.key], rule, 1500)),
                prompt_hash=checks.legacy_prompt_hash(rule, prompt),
                model="gpt-old",
                passed=passed,
                detail="Checked per visit.",
                input_tokens=1000,
                output_tokens=100,
                checked_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC),
            )


def test_the_legacy_payload_is_what_a_visit_check_sent(built, ai_on):
    book = score.Rulebook.load()
    item = checks._items([Visit.objects.get(key="1727")])[0]
    sent = checks.legacy_payload(checks.collect([item])["1727"], book.rules["R3"], 1500)
    assert sent == {
        "visit": "Visit 1727",
        "entities": [
            {
                "entity": PD_BASE + "-2",
                "entity_type": "PD/SSFA",
                "hact_q2_answer": "",
                "narrative_finding": "",
            }
        ],
    }
    assert checks.legacy_prompt_hash(book.rules["R3"], "x") != checks.prompt_hash(book.rules["R3"], "x")


def test_the_checks_of_single_record_visits_are_carried_over_once(built, ai_on, fake):
    book = score.Rulebook.load()
    _legacy_checks(book, ["1722", "1726", "1727", "1728", "r-e4e046b73e2c"])
    stale = VisitAICheck.objects.get(visit_key="1728", rule="R7")
    stale.input_hash = "changed since"
    stale.save()
    out = checks.carry_over(book)
    # 1727, 1728 and the visit known by its reference have one record: 17 of their 18 checks carried
    # (1728's R7 is out of date); 1722 and 1726 have several: they inherit nothing
    assert out == {"carried": 17, "dropped": 13, "left": 0}
    assert not VisitAICheck.objects.exists()
    carried = AICheckAnswer.objects.filter(carried=True)
    assert carried.count() == 16  # 1727's R3 input is the reference-only visit's: one answer for both
    assert set(carried.values_list("model", flat=True)) == {"gpt-old"}
    _rescore()
    scored = set(Visit.objects.filter(quality_score__isnull=False).values_list("key", flat=True))
    assert scored == {"1727", "r-e4e046b73e2c"}  # 1728 waits for its R7 check
    assert Visit.objects.get(key="1726").ai_pending == 12 and Visit.objects.get(key="1728").ai_pending == 1
    # the job carries over first, then checks what is left, the visits with several records first
    found = fake()
    run = _run(limit=6)
    assert run.details["carried"] == 0 and run.details["legacy_checks_left"] == 0
    assert len(found.requests) == 6 and "Sessions were delayed" in found.sent()[0]["narrative_finding"]
    # 1726 first: its two records share their checks, so both are scored now; 1722 still waits
    assert (Visit.objects.get(key="1726").ai_pending, Visit.objects.get(key="1722").ai_pending) == (0, 18)


def test_the_refresh_carries_the_visit_checks_over_before_it_scores(built, ai_on):
    """The deployment: the first refresh (before any AI checks run) scores the single-record visits with
    the verdicts they had, instead of leaving every scored visit without a score until the job runs."""
    _legacy_checks(score.Rulebook.load(), ["1722", "1727", "1728"], passed=False)
    run = refresh.run(triggered_by="test", today=TODAY)
    assert run.status == "succeeded", run.error
    # 1727 and 1728 have one record each (12 checks carried); 1722 has three (6 dropped)
    assert run.details["ai_checks_carried"] == {"carried": 12, "dropped": 6, "left": 0}
    for key in ("1727", "1728"):
        visit = Visit.objects.get(key=key)
        assert visit.quality_score is not None and visit.ai_pending == 0, key
        assert set(AI_RULES) <= set(visit.flags), key
    assert Visit.objects.get(key="1722").ai_pending == 18  # its records wait for the job
    assert not VisitAICheck.objects.exists()


def test_with_the_ai_off_the_visit_checks_still_count(built):
    """The AI checks job does not run while the AI is off: the refresh carries the verdicts over, so a
    pause never moves the scores."""
    _legacy_checks(score.Rulebook.load(), ["1727"], passed=False)
    assert checks.run("test").details["skipped"] == "AI is switched off"
    _rescore()
    statuses = dict(RecordRuleResult.objects.filter(entity__visit__key="1727").values_list("rule", "status"))
    assert {statuses[code] for code in AI_RULES} == {"fail"}
    assert AICheckAnswer.objects.filter(carried=True).count() == len(AI_RULES)


def test_a_rule_not_on_keeps_its_visit_checks_for_later(built, ai_on, fake, monkeypatch):
    book = score.Rulebook.load()
    _legacy_checks(book, ["1727"])
    rule = book.rules["R7"]
    rule.enabled = False
    rule.save()
    out = checks.carry_over(score.Rulebook.load())
    assert out == {"carried": 5, "dropped": 0, "left": 1}
    assert list(VisitAICheck.objects.values_list("rule", flat=True)) == ["R7"]
    fake()
    assert _run(limit=1).details["legacy_checks_left"] == 1
    # what waits is not read again by every refresh (each one carries over before it scores)
    monkeypatch.setattr(checks, "collect", lambda items: pytest.fail("texts read for a check that waits"))
    assert checks.carry_over(score.Rulebook.load()) == {"carried": 0, "dropped": 0, "left": 1}
    # switched on again, it is carried over by the next refresh
    monkeypatch.undo()
    rule.enabled = True
    rule.save()
    _rescore()
    assert not VisitAICheck.objects.exists()
    assert AICheckAnswer.objects.filter(rule="R7", carried=True).count() == 1


def test_re_check_carried_answers_deletes_a_batch(built, ai_on):
    book = score.Rulebook.load()
    _legacy_checks(book, ["1727", "1728"])
    checks.carry_over(book)
    assert checks.recheck_carried(limit=5) == (5, 7)
    assert checks.recheck_carried() == (7, 0) and checks.recheck_carried() == (0, 0)


def test_an_answer_read_is_marked_used_once_a_day(built, ai_on, fake):
    fake()
    _run()
    AICheckAnswer.objects.update(last_used=datetime.date(2026, 1, 1))
    refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    assert set(AICheckAnswer.objects.values_list("last_used", flat=True)) == {TODAY}


def test_store_answers_keeps_answers_by_record(built, ai_on):
    assert checks.store_answers({(121, "R8"): (False, "No action point."), (999, "R8"): (True, "")}, "x") == 1
    _rescore()
    for n in (121, 122):  # the same texts: the answer kept for one serves both
        assert RecordRuleResult.objects.get(entity__datamart_id=n, rule="R8").status == "fail"
    assert AICheckAnswer.objects.get().model == "x"
