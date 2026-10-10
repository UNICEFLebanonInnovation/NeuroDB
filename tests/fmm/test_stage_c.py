"""Release 2, stage C: the action points module (FMS §10). The page's filters, CSV exports, columns and
details (C1, C2); its charts and their click-through (C3); the AI review of completed action points,
its cache, budget, job, button and schedule (C4); the PME verifications (C5); the AI content summary
(C6); the NeuroDB action points, by hand and made at refresh (C7); and the links: the visit page, the
follow-up block, NeuroDB Watch and Ask NeuroDB (C8). No AI payload ever holds who an action point is
assigned to (a name or an e-mail address)."""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace

import openai
import pytest
from django.conf import settings
from django.core.management import call_command
from django.db.models import Q
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import SECTION_EDITOR, VIEWER, ensure_groups
from neurodb.assistant import agent, usage
from neurodb.assistant import tools as assistant_tools
from neurodb.assistant.models import AIUsage
from neurodb.core import admin_jobs, jobs
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart import services as datamart
from neurodb.fmm import action_points, lebanon, metrics, refresh
from neurodb.fmm.ai import ap_review, ap_summary, budget, profiles
from neurodb.fmm.models import (
    ActionPointReview,
    ActionPointSetting,
    ActionPointSummary,
    ActionPointVerification,
    AIState,
    LocalActionPoint,
    RecordRuleResult,
    Visit,
    VisitEntity,
)
from neurodb.integrations import background
from neurodb.partnerships.models import PartnerOrganization
from neurodb.watch import people
from neurodb.watch.models import SectionMatch

from .conftest import LEAD, MEMBER, MEMBER_EMAIL

pytestmark = pytest.mark.django_db
PAGE = "reports:action_points"
TODAY = datetime.date(2026, 10, 5)
AI_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}
ASSIGNEE_EMAIL = "rania.canary@example.org"


# ------------------------------------------------------------------------------------------ helpers
class FakeAP:
    """``agent.client()`` for the action points' AI: records each call; a review answers by a word of
    its issue (``verdicts``: word -> (verdict, explanation); "Adequately addressed" by default), a
    summary with ``summary`` (a dict, or a function of what was sent); or raises the next of ``errors``."""

    def __init__(self, verdicts=None, summary=None, errors=()):
        self.verdicts = dict(verdicts or {})
        self.summary = summary
        self.errors = list(errors)
        self.requests: list[dict] = []
        self.options: list[dict] = []

    def client(self):
        def with_options(**options):
            self.options.append(options)
            return api

        api = SimpleNamespace(with_options=with_options, responses=SimpleNamespace(create=self.create))
        return api

    def create(self, **params):
        self.requests.append(params)
        if self.errors:
            raise self.errors.pop(0)
        sent = json.loads(params["input"][0]["content"])
        if params["prompt_cache_key"] == "neurodb-fmm-ap-summary":
            out = self.summary(sent) if callable(self.summary) else self.summary
        else:
            verdict, why = next(
                (found for word, found in self.verdicts.items() if word in sent["issue"]),
                ("Adequately addressed", "The action taken resolves the issue raised."),
            )
            out = {"verdict": verdict, "explanation": why}
        return SimpleNamespace(
            output_text=json.dumps(out),
            status="completed",
            output=[],
            usage=SimpleNamespace(
                input_tokens=500, input_tokens_details=SimpleNamespace(cached_tokens=0), output_tokens=100
            ),
        )

    def sent(self) -> list[dict]:
        return [json.loads(r["input"][0]["content"]) for r in self.requests]


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        found = FakeAP(**kw)
        monkeypatch.setattr(agent, "client", found.client)
        return found

    return install


@pytest.fixture
def ai_on():
    from neurodb.watch import people

    people.forget()
    with override_settings(**AI_ON):
        yield


def _completed(day: int) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime(2026, 9, day, 10, 0))


def _point(n: int, **fields) -> dm.ActionPoint:
    values = {
        "datamart_id": n,
        "reference_number": f"LEBA/2026/{n}/AP",
        "description": f"Partner to restock the school kits at the Zahle centre (point {n}).",
        "status": "completed",
        "assigned_to_name": LEAD,
        "office": "Zahle",
        "section": "Education",
        "related_module": "fm",
        "due_date": datetime.date(2026, 9, 1),
        "date_of_completion": _completed(10),
        "data": {
            "action_taken": "The partner delivered 412 kits on 8 September; stock cards were updated.",
            "assigned_to": {"name": LEAD, "email": ASSIGNEE_EMAIL},
            "created": "2026-05-20T09:00:00Z",
        },
    }
    values.update(fields)
    return dm.ActionPoint.objects.create(**values)


@pytest.fixture
def points(db):
    """Five eTools action points: three completed with an action taken (one vague, one also naming a
    contact), one completed with none, one open and overdue in Tripoli."""
    return SimpleNamespace(
        kits=_point(9001),
        vague=_point(
            9002,
            description="Partner to fix the water tanks at the Bekaa site.",
            data={"action_taken": "All actions taken.", "created": "2026-06-02"},
            date_of_completion=_completed(20),
            due_date=datetime.date(2026, 8, 1),
        ),
        contact=_point(
            9003,
            description=f"Call the focal point at +961 3 123 456 or {MEMBER_EMAIL} about the registers.",
            data={
                "actions_taken": "Registers were reviewed with the focal point, see https://evil.example/x"
            },
            date_of_completion=None,
        ),
        no_action=_point(9004, data={}, section="Health"),
        open=_point(
            9005,
            status="open",
            office="Tripoli",
            section="Health",
            related_module="audit",
            due_date=datetime.date(2026, 1, 15),
            date_of_completion=None,
            high_priority=True,
            assigned_to_name="Karim Other",
            data={"created": "2026-01-02"},
        ),
    )


def _user(username: str, role: str, section: Section | None = None) -> User:
    user = User.objects.create_user(
        username=username, email=f"{username}@example.org", password="x-pass-123456", section=section
    )
    user.groups.add(ensure_groups()[role])
    return user


@pytest.fixture
def education(db):
    section = Section.objects.create(name="Education", code="EDU")
    SectionMatch.objects.create(
        etools_name="Education", section=section, how=SectionMatch.How.EXACT, confirmed=True
    )
    return section


def _no_assignee(blob: str) -> None:
    for canary in (
        LEAD,
        ASSIGNEE_EMAIL,
        MEMBER,
        MEMBER_EMAIL,
        "Karim Other",
        "+961 3 123 456",
        "evil.example",
    ):
        assert canary not in blob, canary


# ------------------------------------------------------------------------------------------ C1 filters
def _rows(client, **params) -> list[str]:
    response = client.get(reverse(PAGE), params)
    assert response.status_code == 200
    return [p.reference_number for p in response.context["rows"]]


def test_the_new_filters(client_viewer, points):
    assert set(_rows(client_viewer, office="Tripoli")) == {"LEBA/2026/9005/AP"}
    assert "LEBA/2026/9005/AP" not in _rows(client_viewer, section="Education")
    assert set(_rows(client_viewer, assignee="karim")) == {"LEBA/2026/9005/AP"}
    assert set(_rows(client_viewer, fm="1")) == {f"LEBA/2026/900{n}/AP" for n in (1, 2, 3, 4)}
    dm.ActionPoint.objects.filter(datamart_id=9002).update(
        last_modify_date=timezone.make_aware(datetime.datetime(2026, 6, 3, 8, 0))
    )
    assert _rows(client_viewer, changed_from="2026-06-01", changed_to="2026-06-30") == ["LEBA/2026/9002/AP"]
    amel = PartnerOrganization.objects.create(name="Amel Association", vendor_number="2500212345")
    dm.ActionPoint.objects.filter(datamart_id=9001).update(partner=amel)
    assert _rows(client_viewer, partner=str(amel.pk)) == ["LEBA/2026/9001/AP"]
    # the search covers the action taken, under either of the Datamart's names
    assert _rows(client_viewer, q="stock cards") == ["LEBA/2026/9001/AP"]
    assert _rows(client_viewer, q="registers were reviewed") == ["LEBA/2026/9003/AP"]
    html = client_viewer.get(reverse(PAGE)).content.decode()
    for label in (
        "Office",
        "Section",
        "Partner",
        "Assigned to",
        "Changed in eTools",
        "Field monitoring only",
    ):
        assert label in html, label


def test_a_section_user_lands_on_their_section(client, points, education):
    editor = _user("editor", SECTION_EDITOR, education)
    client.force_login(editor)
    response = client.get(reverse(PAGE))
    assert response.context["own_section"] == ["Education"]
    assert {p.section for p in response.context["rows"]} == {"Education"}
    assert "Your section first: Education" in response.content.decode()
    # any choice made (even an empty one) shows what was chosen
    assert len(_rows(client, section="")) == 5


def test_csv_of_the_filter_and_of_everything(client_viewer, points):
    filtered = client_viewer.get(reverse(PAGE), {"office": "Tripoli", "export": "csv"})
    assert filtered["Content-Type"].startswith("text/csv")
    lines = b"".join(filtered.streaming_content).decode().lstrip("﻿").splitlines()
    assert lines[0].startswith("reference,description,partner,pd,office,section,assigned_to")
    assert len(lines) == 2 and "LEBA/2026/9005/AP" in lines[1]
    everything = client_viewer.get(reverse(PAGE), {"office": "Tripoli", "export": "csv", "all": "1"})
    assert len(b"".join(everything.streaming_content).decode().strip().splitlines()) == 6
    html = client_viewer.get(reverse(PAGE)).content.decode()
    assert "CSV of the filter" in html and "CSV of every action point" in html


# ------------------------------------------------------------------------------------------ C2 columns
def test_visit_links_carry_their_confidence(built, fm_world, client_viewer):
    response = client_viewer.get(reverse(PAGE), {"fm": "1"})
    rows = {p.datamart_id: p for p in response.context["rows"]}
    assert rows[8001].confidence == "high" and rows[8001].visit_link["key"] == "1722"
    assert rows[8002].confidence == "medium"  # by the visit's reference
    assert rows[8003].confidence == "unmatched" and rows[8003].visit_link is None
    html = response.content.decode()
    assert "link High" in html and "link Medium" in html and "No visit found (Unmatched)" in html
    high = client_viewer.get(reverse(PAGE), {"link": "high"}).context["rows"]
    assert {p.datamart_id for p in high} == {8001, 8004}
    unmatched = client_viewer.get(reverse(PAGE), {"link": "unmatched"}).context["rows"]
    assert [p.datamart_id for p in unmatched] == [8003]


def test_the_details_show_every_field_the_verdict_and_the_verifications(client_viewer, points, admin_user):
    ActionPointVerification.objects.create(
        datamart_id=9001,
        state="verified",
        note="Checked on site.",
        verified_by=admin_user,
        verified_by_name="Admin",
    )
    with override_settings(**AI_ON):
        prompt = ap_review.current_prompt_hash()
        ActionPointReview.objects.create(
            datamart_id=9001,
            input_hash=ap_review.input_hash(
                points.kits.description, action_points.action_taken(points.kits.data)
            ),
            prompt_hash=prompt,
            verdict="adequate",
            explanation="The 412 kits delivered resolve the stock-out.",
            reviewed_at=timezone.now(),
        )
    url = reverse("reports:action_point", args=[points.kits.pk])
    html = client_viewer.get(url, HTTP_HX_REQUEST="true").content.decode()
    assert 'class="modal-header"' in html
    for text in (
        "LEBA/2026/9001/AP",
        "restock the school kits",
        "The partner delivered 412 kits",
        "Zahle",
        "Education",
        LEAD,  # staff page: who it is assigned to is shown
        "Adequately addressed",
        "The 412 kits delivered resolve the stock-out.",
        "Verified",
        "Checked on site.",
        "PME verification",
    ):
        assert text in html, text
    assert "Save verification" not in html  # a viewer may not verify
    page = client_viewer.get(url)
    assert page.status_code == 200 and "eTools action point" in page.content.decode()
    # the table shows the verdict and the verification
    table = client_viewer.get(reverse(PAGE)).content.decode()
    assert "Adequately addressed" in table and "Verified" in table


# ------------------------------------------------------------------------------------------ C3 charts
def test_the_charts(points):
    charts = datamart.action_point_charts(dm.ActionPoint.objects.all(), TODAY)
    assert {label: n for label, n, _code in charts["by_status"]} == {"Completed": 4, "Open": 1}
    assert charts["due"] == [["Overdue", 1, "overdue"]]
    # completed: 9001 (due 1 Sep, done 10 Sep: 9 days late), 9002 (due 1 Aug, done 20 Sep: 50 days late),
    # 9004 (done 10 Sep: 9 days late), 9003 has no completion date
    assert charts["timeliness"] == [
        ["Late 1–30 days", 2, "late_30"],
        ["Late 31–90 days", 1, "late_90"],
        ["No dates", 1, "no_dates"],
    ]
    assert (charts["late_average"], charts["late_count"]) == (22.7, 3)
    monthly = charts["monthly"]
    assert monthly["labels"] == ["2026-01", "2026-05", "2026-06", "2026-09"]
    assert monthly["series"] == {"Raised": [1, 1, 1, 0], "Completed": [0, 0, 0, 3]}
    assert monthly["drill"]["series"] == {"Raised": "raised", "Completed": "completed"}
    assert charts["by_office"] == [["Tripoli", 1, "Tripoli"]]
    assert charts["by_section"] == [["Health", 1, "Health"]]


def test_a_bar_opens_its_action_points(client_viewer, points):
    response = client_viewer.get(reverse(PAGE))
    hrefs = response.context["chart_hrefs"]
    assert hrefs["due"].endswith("due={drill}") and "{series_drill}={drill}" in hrefs["monthly"]
    assert "status=open" in hrefs["office"]
    html = response.content.decode()
    for chart in ("ap-chart-status", "ap-chart-due", "ap-chart-time", "ap-chart-month", "ap-chart-office"):
        assert f'data-chart-png="#{chart}"' in html, chart
    assert 'data-href-target="#action-point-results"' in html
    assert _rows(client_viewer, due="overdue") == ["LEBA/2026/9005/AP"]
    assert set(_rows(client_viewer, timeliness="late_30")) == {"LEBA/2026/9001/AP", "LEBA/2026/9004/AP"}
    assert _rows(client_viewer, timeliness="late_90") == ["LEBA/2026/9002/AP"]
    assert _rows(client_viewer, raised="2026-06") == ["LEBA/2026/9002/AP"]
    assert len(_rows(client_viewer, completed="2026-09")) == 3
    drilled = client_viewer.get(reverse(PAGE), {"due": "overdue"}).content.decode()
    assert "Overdue" in drilled and "chip--removable" in drilled


# ------------------------------------------------------------------------------------------ C4 AI review
def test_the_review_reads_completed_points_with_an_action_taken_and_never_the_assignee(points, ai_on, fake):
    found = fake(verdicts={"water tanks": ("Generic/vague", "The action taken is a generic statement.")})
    run = ap_review.run("test")
    assert run.status == "succeeded", run.error
    # 9001, 9002, 9003 reviewed; 9004 has no action taken; 9005 is open
    assert (run.details["reviewed"], run.details["skipped"], run.details["errors"]) == (3, 1, 0)
    assert dict(ActionPointReview.objects.values_list("datamart_id", "verdict")) == {
        9001: "adequate",
        9002: "vague",
        9003: "adequate",
    }
    request = found.requests[0]
    assert request["model"] == settings.AI_ASSISTANT_MODEL and request["store"] is False
    assert request["reasoning"] == {"effort": "low"} and request["temperature"] == 0.3
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["properties"]["verdict"]["enum"] == [
        "Adequately addressed",
        "Partially addressed",
        "Not addressed",
        "Generic/vague",
    ]
    assert "assessing whether action points raised during field monitoring visits" in request["instructions"]
    assert "Never name or describe a person" in request["instructions"]
    assert found.options[0]["timeout"] == settings.FMM_AP_REVIEW_TIMEOUT_SECONDS
    for sent in found.sent():
        assert set(sent) == {"issue", "action_taken"}
    _no_assignee(json.dumps([r["input"] for r in found.requests], ensure_ascii=False))
    assert AIUsage.objects.get(feature=usage.FMM_AP_REVIEW).calls == 3
    assert not AIUsage.objects.filter(feature__in=(usage.FMM, usage.FMM_RULES)).exists()
    status = ap_review.last_run()
    assert (status["reviewed"], status["skipped"], status["errors"]) == (3, 1, 0)


def test_a_review_is_kept_until_the_description_or_the_instructions_change(points, ai_on, fake):
    found = fake()
    ap_review.run("test")
    assert ap_review.run("test").details["up_to_date"] == 3 and len(found.requests) == 3
    assert set(action_points.current_reviews(dm.ActionPoint.objects.all())) == {9001, 9002, 9003}
    # a changed description: the verdict is out of date at once, deleted by the next run, made again
    dm.ActionPoint.objects.filter(datamart_id=9001).update(description="Partner to restock the hygiene kits.")
    assert 9001 not in action_points.current_reviews(dm.ActionPoint.objects.all())
    again = ap_review.run("test")
    assert again.details["stale_removed"] == 1 and again.details["reviewed"] == 1 and len(found.requests) == 4
    # new instructions: every verdict is out of date
    published = profiles.published()
    prompts = dict(published.rule_prompts)
    prompts[lebanon.AP_REVIEW_KEY] += "\n- Be strict about evidence."
    draft = profiles.draft_from(published, None, "stricter review", rule_prompts=prompts)
    profiles.publish(draft, None)
    assert action_points.current_reviews(dm.ActionPoint.objects.all()) == {}
    assert ap_review.run("test").details["reviewed"] == 3


def test_the_explanation_is_cleaned_and_checked(points, ai_on, fake):
    fake(
        verdicts={
            "school kits": ("Partially addressed", f"Only 300 kits arrived, says {LEAD}."),
            "water tanks": ("Not addressed", f"Ask {ASSIGNEE_EMAIL} to confirm."),
        }
    )
    ap_review.run("test")
    found = dict(ActionPointReview.objects.values_list("datamart_id", "explanation"))
    assert found[9001] == ""  # 300 is in neither text: left out (the verdict stays)
    assert ASSIGNEE_EMAIL not in found[9002] and "Ask" in found[9002]
    assert ActionPointReview.objects.get(datamart_id=9001).verdict == "partial"


def test_the_review_stops_at_its_limit_its_cap_failures_and_the_pause(points, ai_on, fake):
    fake()
    assert ap_review.run("test", limit=2).details["reviewed"] == 2
    ActionPointReview.objects.all().delete()
    with override_settings(FMM_AP_REVIEW_DAILY_TOKEN_CAP=100):
        run = ap_review.run("test")
    assert run.details["reviewed"] == 0 and run.details["stopped"].startswith("budget")
    fake(errors=[RuntimeError("down")] * 3)
    run = ap_review.run("test")
    assert run.rows_failed == 3 and run.details["stopped"] == "3 failed reviews in a row"
    import httpx2

    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    error = openai.RateLimitError(
        "You exceeded your current quota", response=httpx2.Response(429, request=request), body=None
    )
    fake(errors=[error])
    run = ap_review.run("test")
    assert "credit" in run.details["stopped"] and budget.paused_until() is not None
    AIState.objects.all().delete()


def test_nothing_runs_while_the_ai_or_the_review_is_off(points, fake):
    found = fake()
    run = ap_review.run("test")
    assert run.details["skipped_reason"] == "AI is switched off" and not found.requests
    with override_settings(**AI_ON):
        ActionPointSetting.objects.update_or_create(pk=1, defaults={"ai_review": False})
        run = ap_review.run("test")
    assert "switched off (Action point settings)" in run.details["skipped_reason"] and not found.requests


def test_the_review_job_has_its_command_button_schedule_and_lock(points, ai_on, fake):
    fake()
    call_command("fmm_ap_review", "--limit", "1", "--triggered-by", "test")
    assert ActionPointReview.objects.count() == 1
    assert SyncRun.objects.filter(job=SyncRun.Job.FMM_AP_REVIEW).count() == 1
    assert jobs.COMMANDS["fmm_ap_review"].args == ("fmm_ap_review",)
    assert any(j.command == ("fmm_ap_review",) for j in admin_jobs.BACKGROUND_JOBS)
    scheduled = ScheduledJob.objects.get(key="fmm-ap-review")
    assert (scheduled.command, scheduled.schedule, scheduled.enabled) == ("fmm_ap_review", "10 6 * * *", True)
    assert background.LOCK_IDS[SyncRun.Job.FMM_AP_REVIEW] == ap_review.LOCK_ID
    assert len(usage.FMM_AP_REVIEW) <= 20 and usage.FMM_AP_REVIEW in usage.FEATURES


def test_run_ai_review_is_for_administrators_with_a_batch_size(
    client, points, admin_user, viewer, monkeypatch
):
    started = []
    monkeypatch.setattr(background, "start_command", lambda *args: started.append(args) or 1)
    url = reverse("reports:action_points_review")
    client.force_login(viewer)
    assert client.post(url, {"batch": "100"}).status_code == 403
    client.force_login(admin_user)
    with override_settings(**AI_ON):
        page = client.get(reverse(PAGE)).content.decode()
        assert "Run AI review" in page and "Batch size" in page and "Not run yet." in page
        assert client.post(url, {"batch": "100", "query": "office=Zahle"}).status_code == 302
        client.post(url, {"batch": "7"})  # not a choice: the default
    assert started == [
        ("fmm_ap_review", "--limit", "100", "--triggered-by", "admin"),
        ("fmm_ap_review", "--limit", "50", "--triggered-by", "admin"),
    ]


def test_the_review_and_summary_prompts_are_seeded_in_a_published_version(monkeypatch):
    from neurodb.fmm.ai import prompts

    monkeypatch.setattr(prompts, "SAFETY_VERSION", 3)  # its hash of the day it was seeded
    version = profiles.published()
    assert version.note.startswith("Action points (Release 2)")
    assert version.based_on.status == "retired" and version.content_hash == version.compute_hash()
    assert set(lebanon.RULE_PROMPTS) | {lebanon.AP_REVIEW_KEY, lebanon.AP_SUMMARY_KEY} == set(
        version.rule_prompts
    )
    assert "Generic/vague" in version.rule_prompts[lebanon.AP_REVIEW_KEY]


# ------------------------------------------------------------------------------------------ C5 verification
def test_who_may_verify_and_the_history(client, points, education, admin_user):
    url = reverse("reports:action_point_verify", args=[points.kits.pk])
    other = Section.objects.create(name="Health", code="HLT")
    for user, allowed in (
        (_user("viewer2", VIEWER), False),
        (_user("health", SECTION_EDITOR, other), False),
        (_user("edu", SECTION_EDITOR, education), True),
        (admin_user, True),
    ):
        client.force_login(user)
        response = client.post(url, {"state": "verified", "note": "Seen."}, HTTP_HX_REQUEST="true")
        assert (response.status_code == 200) is allowed, user.username
    client.force_login(admin_user)
    html = client.post(url, {"state": "rejected", "note": "Not convincing."}, HTTP_HX_REQUEST="true")
    assert "Rejected" in html.content.decode() and "Earlier verifications" in html.content.decode()
    history = ActionPointVerification.objects.filter(datamart_id=9001).order_by("created_at", "pk")
    assert [(h.state, h.verified_by_name) for h in history] == [
        ("verified", "edu"),
        ("verified", "admin"),
        ("rejected", "admin"),
    ]
    bad = client.post(url, {"state": "maybe"}, HTTP_HX_REQUEST="true").content.decode()
    assert "Choose Verified, Rejected or Pending." in bad
    client.force_login(_user("viewer3", VIEWER))
    assert [p.datamart_id for p in client.get(reverse(PAGE), {"pme": "rejected"}).context["rows"]] == [9001]
    assert 9001 not in [p.datamart_id for p in client.get(reverse(PAGE), {"pme": "none"}).context["rows"]]
    assert len(client.get(reverse(PAGE), {"pme": "none"}).context["rows"]) == 4


# ------------------------------------------------------------------------------------------ C6 summary
def _themes(sent):
    refs = [p["ref"] for p in sent["points"]]
    return {
        "themes": [
            {"name": "Stock and supplies", "count": 2, "example": refs[0]},
            {"name": "Water supply", "count": 1, "example": "LEBA/2099/1/AP"},  # not sent: left out
        ],
        "pattern": f"Most of the {sent['count']} action points ask partners to restock supplies.",
    }


def test_the_summary_reads_the_filter_and_is_checked(client, points, viewer, ai_on, fake):
    found = fake(summary=_themes)
    client.force_login(viewer)
    html = client.post(
        reverse("reports:action_points_summary"), {"query": "section=Education"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    sent = found.sent()[0]
    assert sent["count"] == 3 and len(sent["points"]) == 3  # the Education points of the filter
    _no_assignee(json.dumps(found.requests[0]["input"], ensure_ascii=False))
    assert "Stock and supplies" in html and "Water supply" not in html
    assert "Most of the 3 action points ask partners to restock supplies." in html
    assert "Dismiss" in html and 'data-bs-dismiss="alert"' in html
    row = ActionPointSummary.objects.get()
    assert (row.status, row.points, row.called) == ("done", 3, True)
    assert AIUsage.objects.get(feature=usage.FMM_AP_REVIEW).calls == 1


def test_a_summary_whose_counts_do_not_add_up_is_refused(points, viewer, ai_on, fake):
    fake(
        summary=lambda sent: {
            "themes": [{"name": "Kits", "count": 99, "example": sent["points"][0]["ref"]}],
            "pattern": "x",
        }
    )
    result = ap_summary.summarise(dm.ActionPoint.objects.all(), viewer)
    assert not result.ok and result.message == ap_summary.NOT_CHECKED


def test_each_person_has_a_daily_quota_of_summaries(points, viewer, ai_on, fake):
    fake(summary=_themes)
    ActionPointSetting.objects.update_or_create(pk=1, defaults={"summary_per_user_per_day": 2})
    for _ in range(2):
        assert ap_summary.summarise(dm.ActionPoint.objects.all(), viewer).ok
    refused = ap_summary.summarise(dm.ActionPoint.objects.all(), viewer)
    assert not refused.ok and "2 a day" in refused.message
    assert ap_summary.quota(viewer) == (2, 2)  # a refused request does not count
    with override_settings(FMM_AI=False):
        assert ap_summary.summarise(dm.ActionPoint.objects.all(), viewer).message == ap_summary.OFF


# ------------------------------------------------------------------------------------------ C7 NeuroDB points
def test_adding_a_neurodb_action_point_by_hand(client, fm_world, built, viewer):
    url = reverse("reports:local_action_point_new")
    client.force_login(viewer)
    assert client.get(url).status_code == 403
    editor = _user("editor2", SECTION_EDITOR, fm_world.section)
    client.force_login(editor)
    response = client.post(
        url,
        {
            "title": "Check the attendance registers",
            "description": "Ask the partner for the June registers.",
            "visit": "Visit 1727",
            "priority": "high",
            "due_date": "2026-10-20",
            "assignee_role": "Education section lead",
        },
    )
    assert response.status_code == 302
    point = LocalActionPoint.objects.get()
    assert (point.visit_key, point.priority, point.source, point.created_by) == (
        "1727",
        "high",
        "manual",
        editor,
    )
    refused = client.post(url, {"title": "", "visit": "nowhere", "priority": "medium"})
    assert refused.context["errors"].keys() == {"title", "visit"}
    page = client.get(reverse(PAGE)).content.decode()
    assert "NeuroDB action points" in page and "Check the attendance registers" in page
    # its status: the person who added it may change it
    status_url = reverse("reports:local_action_point_status", args=[point.pk])
    client.post(status_url, {"status": "done"})
    point.refresh_from_db()
    assert point.status == "done" and point.closed_at is not None
    client.force_login(viewer)
    assert client.post(status_url, {"status": "open"}).status_code == 403


def test_the_neurodb_list_filters(client_viewer, db):
    LocalActionPoint.objects.create(title="Restock vaccines at the clinic", priority="high")
    LocalActionPoint.objects.create(title="Fix the school latrines", description="WASH works", status="done")
    LocalActionPoint.objects.create(title="Follow up on R8 — Visit 9", source="auto", priority="medium")
    found = action_points.local_points({"lprogramme": "health"})
    assert [p.title for p in found["rows"]] == ["Restock vaccines at the clinic"]
    assert {k for k, _label in found["programmes"]} == {"health", "wash", "education"}
    assert [p.title for p in action_points.local_points({"lq": "latrines"})["rows"]] == [
        "Fix the school latrines"
    ]
    assert len(action_points.local_points({"lstatus": "open"})["rows"]) == 2
    assert len(action_points.local_points({"lpriority": "high"})["rows"]) == 1
    html = client_viewer.get(
        reverse("reports:local_action_points"), {"lq": "nothing like it"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "No NeuroDB action points match your filters." in html and "Clear filters" in html


def _low(datamart_id: int, score: float, *codes: str) -> VisitEntity:
    """A record of Low quality with ``codes`` failed (the first takes 5 points, the next 6...)."""
    entity = VisitEntity.objects.get(datamart_id=datamart_id)
    VisitEntity.objects.filter(pk=entity.pk).update(quality_score=score, flags=list(codes))
    RecordRuleResult.objects.filter(entity=entity).filter(Q(status="fail") | Q(rule__in=codes)).delete()
    for n, code in enumerate(codes):
        RecordRuleResult.objects.create(
            entity=entity,
            rule=code,
            status="fail",
            points=0,
            max_points=5 + n,
            detail=f"{code}: the action points do not answer the delays — said {MEMBER}.",
        )
    return VisitEntity.objects.get(pk=entity.pk)


def test_neurodb_makes_an_action_point_for_a_low_record_flagged_for_its_action_points(built):
    friday = datetime.date(2026, 10, 2)
    _low(111, 25.0, "R3", "R8")  # 1723's SSFA record
    _low(112, 45.0, "R7")  # 1723's partner record
    _low(121, 40.0, "R7", "R32")  # one of 1726's programme documents
    _low(171, 45.0, "R3")  # Low, but no action point flag
    people.forget()  # the team names, read again (the refresh's commit does it in production)
    assert action_points.create_automatic(friday) == 2  # one per visit
    made = {p.visit_key: p for p in LocalActionPoint.objects.all()}
    high, medium = made["1723"], made["1726"]
    # it points at the lowest qualifying record, which also sets its priority
    assert (high.record, high.priority, high.due_date) == (111, "high", datetime.date(2026, 10, 9))
    assert (medium.record, medium.priority, medium.due_date) == (121, "medium", datetime.date(2026, 10, 16))
    assert high.title == "Follow up on R8 — FM-2026-023 · LEB/SSFA2024001"
    assert medium.title.startswith("Follow up on R32 — FM-2026-026 · ")
    assert high.source == "auto" and "Other flags: R3." in high.description
    # every qualifying record of the visit, with its score
    assert "LEB/SSFA2024001 (PD/SSFA): quality score 25 (Low)" in high.description
    assert "Mercy Corps Lebanon (Partner): quality score 45 (Low)" in high.description
    assert (
        MEMBER not in high.description and "Classes held" not in high.description
    )  # no person, no narrative
    assert "R8: the action points do not answer the delays" in high.description
    # one open per visit: nothing more
    assert action_points.create_automatic(friday) == 0
    # marked done: not made again until the visit changes in eTools
    LocalActionPoint.objects.filter(pk=high.pk).update(status="done")
    assert action_points.create_automatic(friday) == 0
    Visit.objects.filter(key="1723").update(last_modified=timezone.now() + datetime.timedelta(minutes=5))
    assert action_points.create_automatic(friday) == 1


def test_the_refresh_makes_them(built, monkeypatch):
    monkeypatch.setattr(action_points, "create_automatic", lambda today=None: 3)
    run = refresh.run(triggered_by="test", scores_only=True, today=TODAY)
    assert run.details["local_action_points_made"] == 3


# ------------------------------------------------------------------------------------------ C8 links
def test_the_visit_page_lists_both_kinds_of_action_points(built, client, admin_user):
    point = dm.ActionPoint.objects.get(datamart_id=8001)
    ActionPointVerification.objects.create(datamart_id=8001, state="pending", verified_by_name="Admin")
    LocalActionPoint.objects.create(title="Check the registers again", visit_key="1722", source="manual")
    client.force_login(admin_user)
    html = client.get(reverse("fmm:visit", args=["1722"])).content.decode()
    assert reverse("reports:action_point", args=[point.pk]) in html
    assert "link High" in html and "Pending" in html
    assert "Check the registers again" in html and "New NeuroDB action point on this visit" in html


def test_neurodb_action_points_count_as_follow_up(built, fm_world):
    from neurodb.fmm.scope import Scope
    from neurodb.watch.detectors import Context, fieldmonitoring

    def without() -> set[str]:
        cache_free = Scope.from_params({"section": "", "preset": "all_time"})
        return {key for key, _name in metrics.action_points(cache_free)["without"]["visits"]}

    def candidates() -> set[str]:
        now = datetime.datetime.combine(TODAY, datetime.time(6, 0), tzinfo=datetime.UTC)
        return {c.entity_key for c in fieldmonitoring.visits_without_follow_up(Context.make(now=now))}

    assert "1727" in without() and "1727" in candidates()
    auto = LocalActionPoint.objects.create(
        title="Follow up on R8 — Visit 1727", visit_key="1727", source="auto"
    )
    assert "1727" in without() and "1727" in candidates()  # a reminder NeuroDB made is no follow-up yet
    auto.status = "done"
    auto.save()
    assert "1727" not in without() and "1727" not in candidates()
    auto.delete()
    LocalActionPoint.objects.create(title="Ask for the plan", visit_key="1727", source="manual")
    assert "1727" not in without() and "1727" not in candidates()
    data = metrics.action_points(Scope.from_params({"section": "", "preset": "all_time"}))
    assert data["local_open"] == 1


def test_ask_counts_action_points_by_verdict_without_texts(points, ai_on):
    prompt = ap_review.current_prompt_hash()
    for n, verdict in ((9001, "adequate"), (9002, "vague")):
        point = dm.ActionPoint.objects.get(datamart_id=n)
        ActionPointReview.objects.create(
            datamart_id=n,
            input_hash=ap_review.input_hash(point.description, action_points.action_taken(point.data)),
            prompt_hash=prompt,
            verdict=verdict,
            explanation="Restock confirmed.",
            reviewed_at=timezone.now(),
        )
    ActionPointVerification.objects.create(datamart_id=9001, state="verified", verified_by_name="Admin")
    assert "fm_action_points" in assistant_tools.TOOLS
    assert "fm_action_points" in agent.SYSTEM_PROMPT
    with assistant_tools.read_only():
        result = assistant_tools.run("fm_action_points", {})
    assert result["etools_action_points"] == {"total": 5, "open": 1, "overdue": 1, "completed": 4}
    verdicts = result["ai_verdicts_of_completed"]
    assert verdicts["reviewed"] == 2 and verdicts["not_reviewed_yet"] == 2
    assert (
        verdicts["by_verdict"]["Adequately addressed"] == 1 and verdicts["by_verdict"]["Generic/vague"] == 1
    )
    assert result["pme_verification"]["Verified"] == 1 and result["pme_verification"]["Not verified yet"] == 4
    blob = json.dumps(result, ensure_ascii=False)
    _no_assignee(blob)
    for text in ("Restock confirmed", "school kits", "delivered 412"):
        assert text not in blob
    assert (
        assistant_tools.run("fm_action_points", {"section": "health"})["etools_action_points"]["total"] == 2
    )
