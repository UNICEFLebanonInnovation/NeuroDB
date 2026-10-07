"""The document review's data and pipeline (``neurodb.knowledge.review``): findings read with a scripted
model, located without AI, key statements and action points, what a new analysis keeps, the budget and
the switches, the job's plumbing, the settings and topics in the admin, and the Ask NeuroDB tool."""

from __future__ import annotations

import datetime
import json
import re
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from neurodb.assistant import agent, tools, usage
from neurodb.assistant.models import AIUsage
from neurodb.core import admin_jobs, jobs
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.geo.models import DistrictLocation, GovernorateLocation
from neurodb.integrations import background
from neurodb.knowledge import review, review_locate, review_prompts
from neurodb.knowledge.models import (
    Document,
    DocumentActionPoint,
    DocumentFinding,
    DocumentReviewSettings,
    DocumentStatement,
    ReviewBatch,
    Topic,
    TopicProgramme,
    Verdict,
)

AI_ON = {"AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}
PAGES = [
    "Annual report 2024. In Akkar, 1,200 children remained out of school in 2024 because of transport "
    "costs. The Ministry of Education will rehabilitate 20 schools by Q3 2025.",
    "Water supply in Zahle district fell by 30 percent during the summer of 2024. Solar pumping is "
    "recommended for the water establishments.",
    "Funding gaps reached 40 percent in 2024 according to the sector partners.",
]
OUT_OF_SCHOOL = {
    "category": "challenge",
    "tag": "Education › Access to learning › Out-of-school children",
    "text": "1,200 children were out of school in Akkar in 2024.",
    "quote": "In Akkar, 1,200 children remained out of school in 2024",
    "place": "Akkar",
    "date": "2024",
    "kind": "reported",
}
SCHOOLS = {
    "category": "action_point",
    "tag": "School infrastructure",
    "text": "The Ministry of Education will rehabilitate 20 schools by Q3 2025.",
    "quote": "The Ministry of Education will rehabilitate 20 schools by Q3 2025",
    "place": "",
    "date": "Q3 2025",
    "kind": "reported",
}
WATER = {
    "category": "observation",
    "tag": "Space travel",  # not a tag of the list: Other
    "text": "Water supply in Zahle fell by a third in the summer.",
    "quote": "Water supply in Zahle district fell by 30 percent",
    "place": "Zahle",
    "date": "summer 2024",
    "kind": "interpreted",
}
FUNDING = {
    "category": "recommendation",
    "tag": "Funding gaps",
    "text": "Donors should close the funding gap.",
    "quote": "Donors must close the gap before winter arrives",  # not in the text
    "place": "Mars",
    "date": "",
    "kind": "reported",
}
BY_PAGE = {"[Page 1]": [OUT_OF_SCHOOL, SCHOOLS], "[Page 2]": [WATER], "[Page 3]": [FUNDING]}


def _number(content: str, word: str) -> int:
    """The number the payload gives the finding whose line holds ``word``."""
    for line in content.splitlines():
        found = re.match(r"\[(\d+)\] ", line)
        if found and word in line:
            return int(found[1])
    return 999


def statements(content: str) -> dict:
    return {
        "statements": [
            {
                "text": "Out-of-school children in Akkar need urgent action.",
                "urgency": 140,  # held to 0-100
                "category": "challenge",
                "place": "Akkar",
                "date": "2024",
                "cites": [_number(content, "out of school"), 999],
            }
        ]
    }


def actions(content: str) -> dict:
    return {
        "action_points": [
            {
                "action": "Rehabilitate 20 schools.",
                "owner": "Ministry of Education",
                "deadline": "Q3 2025",
                "priority": "high",
                "cites": [_number(content, "rehabilitate")],
            },
            {
                "action": "Install solar pumping in the water establishments.",
                "owner": "",
                "deadline": "2025",
                "priority": "medium",
                "cites": [_number(content, "Water supply")],
            },
        ]
    }


class FakeReview:
    """``agent.client()`` for the document review: answers each stage from what was sent. ``broken``:
    a findings part whose text satisfies it gets a reply that is not JSON; ``errors``: raised in turn."""

    def __init__(self, broken=None, errors=(), during=None):
        self.broken = broken
        self.during = during  # called at the summary's call, as a person acts while the AI reads
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
        stage = params["text"]["format"]["name"]
        content = params["input"][0]["content"]
        if stage == "doc_review_findings":
            if self.broken and self.broken(content):
                return self._answer("{'findings': [", status="incomplete")
            out = {"findings": [f for marker, found in BY_PAGE.items() if marker in content for f in found]}
        elif stage == "doc_review_summary":
            if self.during:
                self.during()
            out = statements(content)
        else:
            out = actions(content)
        return self._answer(json.dumps(out))

    @staticmethod
    def _answer(text: str, status: str = "completed"):
        return SimpleNamespace(
            output_text=text,
            status=status,
            output=[],
            usage=SimpleNamespace(
                input_tokens=1000, input_tokens_details=SimpleNamespace(cached_tokens=0), output_tokens=200
            ),
        )

    def stages(self) -> list[str]:
        return [r["text"]["format"]["name"].removeprefix("doc_review_") for r in self.requests]


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        found = FakeReview(**kw)
        monkeypatch.setattr(agent, "client", found.client)
        return found

    return install


@pytest.fixture
def ai_on():
    with override_settings(**AI_ON):
        yield


@pytest.fixture
def places(db):
    GovernorateLocation.objects.create(code="LB1", name="Akkar", ai_id=1)
    GovernorateLocation.objects.create(code="LB7", name="Bekaa", ai_id=7)
    DistrictLocation.objects.create(code="LB71", gov_code="LB7", name="Zahle")


@pytest.fixture
def setting(db):
    found = DocumentReviewSettings.load()
    found.enabled = True
    found.chunk_size = 2000
    found.save()
    return found


@pytest.fixture
def batch(db):
    return ReviewBatch.objects.create(name="Annual reports")


@pytest.fixture
def report(db, batch, places):
    document = Document.objects.create(
        title="Annual report 2024", text="\f".join(PAGES), status=Document.Status.READY, pages=3
    )
    review.put_in_batch([document], batch)
    return document


def _finding(document: Document, words: str) -> DocumentFinding:
    return document.findings.get(text__icontains=words)


# ------------------------------------------------------------------------------------------ the data
def test_the_topics_are_seeded_with_other_and_ten_programmes(db):
    names = set(TopicProgramme.objects.values_list("name", flat=True))
    assert {
        "Education", "Child Protection", "Health & Nutrition", "WASH", "Social Protection & Inclusion",
        "Adolescents & Youth", "Gender", "Emergency/Humanitarian response", "Partnerships & Funding",
        "Monitoring & Data", "Other",
    } == names  # fmt: skip
    other = Topic.other()
    assert other.is_other and other.path == "Other › Other › Other"
    assert Topic.objects.filter(subtopic__programme__name="Education").count() >= 6
    Topic.objects.filter(pk=other.pk).delete()
    assert Topic.other().is_other  # made again


def test_the_settings_start_switched_off_with_the_shipped_prompts(db):
    found = DocumentReviewSettings.load()
    assert not found.enabled and found.token_cap == 1_000_000 and found.statements_limit == 20
    for stage in DocumentReviewSettings.PROMPT_FIELDS:
        assert found.is_default(stage) and review_prompts.REQUIRED_SENTENCES[stage] in found.prompt(stage)
    found.tagging_prompt = "List the findings."  # the JSON sentence removed
    with pytest.raises(ValidationError) as refused:
        found.full_clean()
    assert "tagging_prompt" in refused.value.message_dict
    found.tagging_prompt = f"List the findings carefully.\n{review_prompts.REQUIRED_SENTENCES['tagging']}"
    found.full_clean()
    found.daily_token_cap = 5000
    assert found.token_cap == 5000


# ------------------------------------------------------------------------------------------ the pipeline
def test_a_document_is_read_into_findings_statements_and_action_points(report, setting, fake, ai_on):
    client = fake()
    run = review.run(review.PENDING, triggered_by="test")
    assert run.status == SyncRun.Status.SUCCEEDED and run.rows_written == 1, run.details
    assert client.stages() == ["findings", "summary", "enrichment"]
    assert client.options[0]["max_retries"] == 1
    sent = client.requests[0]
    assert sent["store"] is False and sent["text"]["format"]["strict"] is True
    assert (
        review_prompts.FIXED in sent["instructions"]
        and "Education › Access to learning" in sent["instructions"]
    )
    assert "[Page 1]" in sent["input"][0]["content"] and "[Page 3]" in sent["input"][0]["content"]
    report.refresh_from_db()
    assert report.review_status == Document.ReviewStatus.DONE and report.reviewed_text_hash
    notes = report.review_stage_notes
    assert [notes[s]["state"] for s in review.STAGES] == ["yes"] * 5 and notes["findings"][
        "read_share"
    ] == 100

    school = _finding(report, "out of school")
    assert school.topic.path == "Education › Access to learning › Out-of-school children"
    assert (school.page_label, school.page_from, school.exact_page, school.quote_found) == (
        "p. 1",
        1,
        True,
        True,
    )
    assert (school.place_match, school.governorate_name, school.finding_date) == (
        "governorate", "Akkar", datetime.date(2024, 1, 1),
    )  # fmt: skip
    assert school.evidence == 100  # found 45, reported 25, dated 10, placed 10, tagged 10
    water = _finding(report, "Water supply")
    assert water.topic.is_other and water.tag_text == "Space travel"  # an unknown tag: Other
    assert (water.place_match, water.district_name, water.governorate_name) == ("district", "Zahle", "Bekaa")
    assert water.page_label == "p. 2" and water.evidence == 45 + 10 + 10  # interpreted, Other
    funding = _finding(report, "funding gap")
    assert not funding.quote_found and not funding.exact_page and funding.page_label == "pp. 1–3"
    assert funding.place_match == "unmatched" and funding.place_text == "Mars" and funding.evidence == 25 + 10

    statement = DocumentStatement.objects.get(document=report)
    assert statement.urgency == 100 and list(statement.cites.all()) == [school]
    assert statement.topic == school.topic

    schools, solar = DocumentActionPoint.objects.filter(document=report).order_by("position")
    assert (schools.owner_text, schools.deadline_date, schools.priority, schools.derived) == (
        "Ministry of Education", datetime.date(2025, 9, 30), "high", False,
    )  # fmt: skip
    assert (solar.owner_text, solar.deadline_date, solar.derived) == (
        "Unassigned",
        datetime.date(2025, 12, 31),
        True,
    )
    derived = report.findings.get(derived=True)
    assert (
        derived.category == "action_point"
        and derived.quote == WATER["quote"]
        and derived in solar.cites.all()
    )
    assert AIUsage.objects.get(feature=usage.DOC_REVIEW).calls == 3
    assert run.details["calls"] == 3 and run.details["tokens"] == 3600


def test_a_broken_part_is_read_again_as_two_halves(report, setting, fake, ai_on):
    client = fake(broken=lambda text: "[Page 1]" in text and "[Page 3]" in text)  # the whole part only
    review.run(review.FULL, triggered_by="test")
    report.refresh_from_db()
    assert client.stages()[:3] == ["findings", "findings", "findings"]
    assert report.review_status == Document.ReviewStatus.DONE
    assert report.findings.filter(derived=False).count() == 4


def test_what_still_fails_is_left_out_and_the_document_is_partly_analysed(report, setting, fake, ai_on):
    fake(broken=lambda text: "[Page 3]" in text)  # the whole part and its second half
    run = review.run(review.FULL, triggered_by="test")
    report.refresh_from_db()
    assert report.review_status == Document.ReviewStatus.PARTLY and run.rows_written == 1
    note = report.review_stage_notes["findings"]
    assert note["state"] == "partly" and 0 < note["read_share"] < 100
    assert note["error"] == f"Only {note['read_share']}% read: some parts got no usable answer."
    assert not report.findings.filter(text__icontains="Water").exists()
    assert report.findings.filter(text__icontains="out of school").exists()


def test_nothing_read_fails_the_document_and_keeps_what_an_earlier_analysis_found(
    report, setting, fake, ai_on
):
    fake()
    review.run(review.FULL, triggered_by="test")
    fake(broken=lambda text: True)
    run = review.run(review.FULL, triggered_by="test")
    report.refresh_from_db()
    assert report.review_status == Document.ReviewStatus.FAILED and run.rows_failed == 1
    assert run.status == SyncRun.Status.PARTIAL
    assert report.findings.count() == 5  # the earlier analysis stays


def test_a_new_analysis_keeps_verdicts_manual_findings_and_action_point_statuses(
    report, setting, fake, ai_on, admin_user
):
    fake()
    review.run(review.FULL, triggered_by="test")
    now = timezone.now()
    school = _finding(report, "out of school")
    DocumentFinding.objects.filter(pk=school.pk).update(
        verdict=Verdict.ACCEPTED, reviewed_by=admin_user, reviewed_at=now
    )
    DocumentFinding.objects.filter(text__icontains="Water").update(
        verdict=Verdict.REJECTED, reviewed_by=admin_user
    )
    manual = DocumentFinding.objects.create(
        document=report, category="observation", topic=Topic.other(), text="Teachers were paid late in 2024.",
        quote="1,200 children remained out of school", manual=True, page_from=1, page_to=1,
    )  # fmt: skip
    DocumentActionPoint.objects.filter(action__icontains="schools").update(
        status="done", status_by=admin_user
    )
    DocumentStatement.objects.update(verdict=Verdict.ACCEPTED)

    client = fake()
    review.run(review.FULL, triggered_by="test")
    report.refresh_from_db()
    again = _finding(report, "out of school")
    assert again.pk != school.pk and again.verdict == Verdict.ACCEPTED and again.reviewed_by == admin_user
    assert _finding(report, "Water supply").verdict == Verdict.REJECTED
    assert DocumentFinding.objects.filter(pk=manual.pk, manual=True).exists()
    manual.refresh_from_db()
    assert manual.quote_found and manual.page_label == "p. 1"  # located again
    assert DocumentActionPoint.objects.get(action__icontains="schools").status == "done"
    assert DocumentStatement.objects.get(document=report).verdict == Verdict.ACCEPTED
    # a rejected finding is not read by the summary or the enrichment; a person's finding is
    summary = next(r for r in client.requests if r["text"]["format"]["name"] == "doc_review_summary")
    assert "Water supply" not in summary["input"][0]["content"]
    assert "Teachers were paid late" in summary["input"][0]["content"]


def test_a_verdict_given_while_the_ai_reads_is_kept(report, setting, fake, ai_on, admin_user):
    fake()
    review.run(review.FULL, triggered_by="test")

    def accept():
        DocumentFinding.objects.filter(text__icontains="Water").update(verdict=Verdict.ACCEPTED)
        DocumentActionPoint.objects.filter(action__icontains="solar").update(status="done")

    fake(during=accept)
    review.run(review.FULL, triggered_by="test")
    assert _finding(report, "Water supply").verdict == Verdict.ACCEPTED
    assert DocumentActionPoint.objects.get(action__icontains="solar").status == "done"


def test_the_enrichment_alone_draws_the_action_points_again_and_keeps_statuses(report, setting, fake, ai_on):
    fake()
    review.run(review.FULL, triggered_by="test")
    DocumentActionPoint.objects.filter(action__icontains="solar").update(status="dropped")
    findings_before = set(report.findings.filter(derived=False).values_list("pk", flat=True))
    client = fake()
    review.run(review.ENRICH, triggered_by="test")
    assert client.stages() == ["enrichment"]
    assert set(report.findings.filter(derived=False).values_list("pk", flat=True)) == findings_before
    assert DocumentActionPoint.objects.filter(document=report).count() == 2
    assert DocumentActionPoint.objects.get(action__icontains="solar").status == "dropped"
    assert report.findings.filter(derived=True).count() == 1


def test_locating_again_needs_no_ai(report, setting, fake, ai_on):
    fake()
    review.run(review.FULL, triggered_by="test")
    DocumentFinding.objects.update(evidence=0, page_label="", place_match="")
    DocumentReviewSettings.objects.update(enabled=False)  # no AI needed
    client = fake()
    run = review.run(review.LOCATE, triggered_by="test")
    assert client.requests == [] and run.details["findings_located"] == 5
    assert _finding(report, "out of school").evidence == 100
    assert _finding(report, "Water supply").place_match == "district"


def test_a_document_still_being_read_waits_and_one_without_text_fails(report, setting, fake, ai_on, batch):
    Document.objects.filter(pk=report.pk).update(status=Document.Status.INDEXING)
    empty = Document.objects.create(
        title="Scan", text="", status=Document.Status.FAILED, error="No text layer"
    )
    review.put_in_batch([empty], batch)
    client = fake()
    review.run(review.PENDING, triggered_by="test")
    report.refresh_from_db()
    empty.refresh_from_db()
    assert client.requests == [] and report.review_status == Document.ReviewStatus.PENDING
    assert "still reading" in report.review_stage_notes["waiting"]
    assert empty.review_status == Document.ReviewStatus.FAILED
    assert "No text layer" in empty.review_stage_notes["text"]["error"]


def test_only_documents_in_a_batch_and_not_references_are_analysed(report, setting, fake, ai_on, batch):
    loose = Document.objects.create(title="Loose", text=PAGES[0], status=Document.Status.READY)
    reference = Document.objects.create(title="Guidance", text=PAGES[0], status=Document.Status.READY)
    review.put_in_batch([reference], batch)
    review.set_reference(reference, True)
    archived = ReviewBatch.objects.create(name="Old", archived=True)
    old = Document.objects.create(title="Old", text=PAGES[0], status=Document.Status.READY)
    review.put_in_batch([old], archived)
    fake()
    run = review.run(review.FULL, triggered_by="test")
    assert run.rows_in == 1
    assert not loose.findings.exists() and not reference.findings.exists() and not old.findings.exists()
    review.take_out(report)
    report.refresh_from_db()
    assert report.review_status == Document.ReviewStatus.NOT_IN_REVIEW and report.review_batch is None


# ------------------------------------------------------------------------------------------ limits
def test_switched_off_nothing_is_analysed(report, fake, ai_on):
    client = fake()
    run = review.run(review.PENDING, triggered_by="test")  # off until an administrator turns it on
    assert client.requests == [] and run.details["skipped_reason"] == review.OFF
    report.refresh_from_db()
    assert report.review_status == Document.ReviewStatus.PENDING


def _used_today(tokens: int) -> None:
    used = SimpleNamespace(
        input_tokens=tokens, input_tokens_details=SimpleNamespace(cached_tokens=0), output_tokens=0
    )
    usage.record(usage.DOC_REVIEW, "test-model", used)


def test_the_daily_cap_stops_the_run_cleanly(report, setting, fake, ai_on):
    setting.daily_token_cap = 60_000
    setting.save()
    _used_today(59_000)  # the document fits a day, but not what is left of today
    client = fake()
    run = review.run(review.PENDING, triggered_by="test")
    report.refresh_from_db()
    assert client.requests == [] and run.status == SyncRun.Status.SUCCEEDED
    assert run.details["stopped"] == review.BUDGET and report.review_status == Document.ReviewStatus.PENDING
    assert report.review_stage_notes["waiting"] == review.BUDGET


def test_the_cap_reached_half_way_leaves_the_document_as_it_was(report, setting, fake, ai_on, monkeypatch):
    estimate = review.estimate

    def costly_summary(stage, payload, setting_):
        return 10**9 if stage == review.SUMMARY else estimate(stage, payload, setting_)

    monkeypatch.setattr(review, "estimate", costly_summary)
    client = fake()
    run = review.run(review.PENDING, triggered_by="test")
    report.refresh_from_db()
    assert client.stages() == ["findings"] and run.details["stopped"] == review.BUDGET
    assert report.review_status == Document.ReviewStatus.PENDING and not report.findings.exists()


def test_the_credit_pause_and_the_shared_cap_stop_it(report, setting, fake, ai_on, monkeypatch):
    client = fake()
    monkeypatch.setattr("neurodb.fmm.ai.budget.paused_until", lambda *a: timezone.now())
    run = review.run(review.PENDING, triggered_by="test")
    assert client.requests == [] and run.details["stopped"] == review.PAUSED
    monkeypatch.setattr("neurodb.fmm.ai.budget.paused_until", lambda *a: None)
    _used_today(79_000)  # the document fits 80% of the shared cap, but not what is left of it today
    with override_settings(AI_DAILY_TOKEN_SOFT_CAP=100_000):
        run = review.run(review.PENDING, triggered_by="test")
    report.refresh_from_db()
    assert client.requests == [] and run.details["stopped"] == review.SHARED_BUDGET
    assert report.review_status == Document.ReviewStatus.PENDING


def test_a_document_too_long_for_a_whole_day_fails_without_a_call(report, setting, fake, ai_on):
    setting.daily_token_cap = 5000  # less than one analysis of the report needs
    setting.save()
    client = fake()
    run = review.run(review.PENDING, triggered_by="test")
    report.refresh_from_db()
    assert client.requests == [] and run.rows_failed == 1
    assert report.review_status == Document.ReviewStatus.FAILED
    assert "Daily token cap" in report.review_stage_notes["findings"]["error"]
    with override_settings(AI_DAILY_TOKEN_SOFT_CAP=6000):  # 80% of the shared cap counts too
        setting.daily_token_cap = 0
        setting.save()
        review.run(review.PENDING, triggered_by="test")
    report.refresh_from_db()
    assert client.requests == [] and report.review_status == Document.ReviewStatus.FAILED
    error = report.review_stage_notes["findings"]["error"]
    assert "AI_DAILY_TOKEN_SOFT_CAP" in error and "4,800" in error and "Daily token cap" not in error
    setting.daily_token_cap = 0
    setting.save()
    review.run(review.PENDING, triggered_by="test")  # a failed document is tried again: it fits now
    report.refresh_from_db()
    assert report.review_status == Document.ReviewStatus.DONE and client.requests


def test_a_full_run_stopped_by_the_budget_leaves_the_rest_waiting(
    report, setting, fake, ai_on, batch, monkeypatch
):
    second = Document.objects.create(
        title="Donor report 2024", text="\f".join(PAGES), status=Document.Status.READY, pages=3
    )
    review.put_in_batch([second], batch)
    fake()
    review.run(review.FULL, triggered_by="test")
    assert set(Document.objects.values_list("review_status", flat=True)) == {Document.ReviewStatus.DONE}

    client = fake()
    blocked = review.blocked
    monkeypatch.setattr(  # the budget lasts for one document's three calls
        review,
        "blocked",
        lambda tokens, s: review.BUDGET if len(client.requests) >= 3 else blocked(tokens, s),
    )
    run = review.run(review.FULL, triggered_by="test")
    assert run.details["stopped"] == review.BUDGET and run.details["left_waiting"] == 1
    report.refresh_from_db()
    second.refresh_from_db()
    assert report.review_status == Document.ReviewStatus.DONE
    assert second.review_status == Document.ReviewStatus.PENDING and second.findings.count() == 5  # kept
    monkeypatch.setattr(review, "blocked", blocked)
    run = review.run(review.PENDING, triggered_by="test")  # the next night goes on with it
    second.refresh_from_db()
    assert run.rows_written == 1 and second.review_status == Document.ReviewStatus.DONE


def test_a_document_taken_out_while_the_ai_reads_keeps_nothing(report, setting, fake, ai_on):
    fake(during=lambda: review.take_out(Document.objects.get(pk=report.pk)))
    run = review.run(review.FULL, triggered_by="test")
    report.refresh_from_db()
    assert run.rows_written == 0 and run.details["waiting"] == 1
    assert report.review_status == Document.ReviewStatus.NOT_IN_REVIEW and report.review_batch is None
    assert not report.findings.exists() and not report.statements.exists()


def test_a_finding_a_person_deletes_while_the_ai_reads_is_not_cited(report, setting, fake, ai_on):
    DocumentFinding.objects.create(
        document=report,
        category="observation",
        topic=Topic.other(),
        manual=True,
        text="Children out of school were counted twice.",
        quote="",
    )  # the statement cites it: the first finding with "out of school"
    fake(during=lambda: DocumentFinding.objects.filter(manual=True).delete())
    run = review.run(review.FULL, triggered_by="test")
    report.refresh_from_db()
    assert run.rows_written == 1 and report.review_status == Document.ReviewStatus.DONE
    statement = DocumentStatement.objects.get(document=report)
    assert list(statement.cites.all()) == [] and not report.findings.filter(manual=True).exists()


def test_a_new_text_read_while_the_ai_reads_is_analysed_again(report, setting, fake, ai_on):
    def read_again():
        Document.objects.filter(pk=report.pk).update(text="\f".join([*PAGES, "A new page."]))
        assert not review.text_read(Document.objects.get(pk=report.pk))  # it is being analysed

    fake(during=read_again)
    run = review.run(review.FULL, triggered_by="test")
    report.refresh_from_db()
    assert report.review_status == Document.ReviewStatus.PENDING and run.details["waiting"] == 1
    assert report.review_stage_notes["waiting"] == review.TEXT_CHANGED
    assert report.findings.count() == 5  # kept meanwhile
    fake()
    run = review.run(review.PENDING, triggered_by="test")
    report.refresh_from_db()
    assert run.rows_written == 1 and report.review_status == Document.ReviewStatus.DONE
    assert report.reviewed_text_hash == review.text_hash(report)


def test_a_persons_edit_of_a_finding_is_kept_with_its_verdict(report, setting, fake, ai_on, admin_user):
    fake()
    review.run(review.FULL, triggered_by="test")
    water = _finding(report, "Water supply")
    DocumentFinding.objects.filter(pk=water.pk).update(
        text="Water supply in Zahle fell by 30% in summer 2024.",
        topic=Topic.objects.get(name="Water supply"),
        verdict=Verdict.ACCEPTED,
        edited_by=admin_user,
        edited_at=timezone.now(),
    )
    fake()
    review.run(review.FULL, triggered_by="test")
    again = report.findings.get(text__icontains="by 30% in summer")
    assert again.pk != water.pk and again.verdict == Verdict.ACCEPTED and again.edited_by == admin_user
    assert again.topic.name == "Water supply" and again.evidence == 45 + 10 + 10 + 10  # tagged now
    assert not report.findings.filter(text=WATER["text"]).exists()


def test_a_document_with_no_progress_for_30_minutes_is_marked_failed(report, setting, fake, ai_on):
    Document.objects.filter(pk=report.pk).update(
        review_status=Document.ReviewStatus.RUNNING,
        review_progress_at=timezone.now() - datetime.timedelta(minutes=31),
    )
    other = Document.objects.create(title="Busy", text="x", review_status=Document.ReviewStatus.RUNNING)
    Document.objects.filter(pk=other.pk).update(review_progress_at=timezone.now())
    assert review.left_behind() == 1
    report.refresh_from_db()
    assert (
        report.review_status == Document.ReviewStatus.FAILED
        and report.review_stage_notes["stopped"] == review.STALE
    )


def test_a_document_read_again_with_a_new_text_waits_to_be_analysed(report, setting, fake, ai_on):
    fake()
    review.run(review.FULL, triggered_by="test")
    report.refresh_from_db()
    assert not review.text_read(report)  # the same text
    report.text += "\fA new page."
    assert review.text_read(report)
    report.refresh_from_db()
    assert report.review_status == Document.ReviewStatus.PENDING


# ------------------------------------------------------------------------------------------ the job
def test_the_command_and_its_buttons_and_schedule(report, setting, fake, ai_on, started):
    fake()
    call_command("review_documents", "--document", str(report.pk), "--triggered-by", "test")
    run = SyncRun.objects.get(job=SyncRun.Job.DOC_REVIEW)
    assert run.target == f"full #{report.pk}" and run.triggered_by == "test"
    loose = Document.objects.create(title="Loose", text="x")
    with pytest.raises(CommandError):
        call_command("review_documents", "--document", str(loose.pk))
    waiting = Document.objects.create(title="Not analysed yet", text="x")
    review.put_in_batch([waiting], report.review_batch)
    with pytest.raises(CommandError):  # no analysis to draw the action points again from
        call_command("review_documents", "--enrich", "--document", str(waiting.pk))
    assert jobs.COMMANDS["doc_review"].args == ("review_documents", "--pending")
    buttons = {j.name: j.command for j in admin_jobs.BACKGROUND_JOBS if j.job == SyncRun.Job.DOC_REVIEW}
    assert buttons == {
        "run_doc_review": ("review_documents", "--pending"),
        "run_doc_review_full": ("review_documents", "--full"),
        "run_doc_review_locate": ("review_documents", "--locate"),
    }
    scheduled = ScheduledJob.objects.get(key="doc-review")
    assert (scheduled.command, scheduled.schedule, scheduled.enabled) == ("doc_review", "40 4 * * *", True)
    assert background.LOCK_IDS[SyncRun.Job.DOC_REVIEW] == review.LOCK_ID
    assert usage.DOC_REVIEW in usage.FEATURES
    review.start(report, triggered_by="editor")
    assert started[-1] == ("review_documents", "--document", str(report.pk), "--triggered-by", "editor")


def test_the_admin_settings_are_for_administrators_and_restore_the_shipped_prompt(
    admin_user, client, editor, report
):
    found = DocumentReviewSettings.load()
    found.summary_prompt = f"Short statements.\n{review_prompts.REQUIRED_SENTENCES['summary']}"
    found.save()
    url = reverse("admin:knowledge_documentreviewsettings_change", args=[found.pk])
    client.force_login(admin_user)
    page = client.get(url)
    assert page.status_code == 200 and "Restore the shipped key statements prompt" in page.content.decode()
    data = {
        "enabled": "on", "tagging_prompt": "No sentence here", "summary_prompt": found.summary_prompt,
        "enrichment_prompt": found.enrichment_prompt, "statements_per_document": 0,
        "max_findings_per_chunk": 25, "chunk_size": 12000, "daily_token_cap": 0,
    }  # fmt: skip
    refused = client.post(url, data)
    assert refused.status_code == 200 and "Keep the sentence" in refused.content.decode()
    data.update(tagging_prompt=found.tagging_prompt, restore_summary="on")
    assert client.post(url, data).status_code == 302
    found.refresh_from_db()
    assert found.enabled and found.is_default("summary") and found.updated_by == admin_user
    for name in ("topicprogramme", "topic", "reviewbatch", "documentfinding", "documentactionpoint"):
        assert client.get(reverse(f"admin:knowledge_{name}_changelist")).status_code == 200
    client.force_login(editor)
    assert client.get(url).status_code in (302, 403)


def test_putting_a_document_in_a_batch_in_the_admin_makes_it_wait(admin_client, batch, started):
    document = Document.objects.create(title="Evaluation", text="x", status=Document.Status.READY)
    url = reverse("admin:knowledge_document_change", args=[document.pk])
    page = admin_client.get(url)
    assert page.status_code == 200
    form = {
        "title": document.title,
        "source": "",
        "summary": "",
        "key_points": "[]",
        "review_batch": batch.pk,
    }
    form.update(
        {
            "links-TOTAL_FORMS": 0,
            "links-INITIAL_FORMS": 0,
            "links-MIN_NUM_FORMS": 0,
            "links-MAX_NUM_FORMS": 1000,
        }
    )
    response = admin_client.post(url, form)
    assert response.status_code == 302, response.content.decode()[:3000]
    document.refresh_from_db()
    assert document.review_batch == batch and document.review_status == Document.ReviewStatus.PENDING


# ------------------------------------------------------------------------------------------ Ask NeuroDB
def test_ask_neurodb_searches_the_findings_without_rejected_ones(report, setting, fake, ai_on):
    fake()
    review.run(review.FULL, triggered_by="test")
    assert "search_document_findings" in [d["name"] for d in tools.definitions()]
    assert "search_document_findings" in agent.SYSTEM_PROMPT
    out = tools.run("search_document_findings", {"query": "Akkar children"})
    first = out["findings"][0]
    assert first["finding"] == OUT_OF_SCHOOL["text"] and first["page"] == "p. 1"
    assert first["url"] == f"/knowledge/{report.pk}/" and first["batch"] == "Annual reports"
    assert first["evidence"] == 100 and "instructions" in out["note"]
    DocumentFinding.objects.filter(text__icontains="Akkar").update(verdict=Verdict.REJECTED)
    out = tools.run("search_document_findings", {"query": "Akkar children"})
    assert all("Akkar" not in f["finding"] for f in out["findings"])
    assert tools.run("search_document_findings", {"query": "water", "batch": "Annual"})["findings"]
    assert not tools.run("search_document_findings", {"query": "water", "batch": "Evaluations"})["findings"]
    assert tools.run("search_document_findings", {"query": "water", "batch": str(report.review_batch_id)})[
        "findings"
    ]
    assert not tools.run("search_document_findings", {"query": "water", "batch": "9" * 30})["findings"]
    review.take_out(report)
    assert not tools.run("search_document_findings", {"query": "water"})["findings"]


# ------------------------------------------------------------------------------------------ without AI
@pytest.mark.parametrize(
    ("text", "end", "expected"),
    [
        ("Q3 2025", True, datetime.date(2025, 9, 30)),
        ("Q3 2025", False, datetime.date(2025, 7, 1)),
        ("end of 2025", True, datetime.date(2025, 12, 31)),
        ("March 2025", True, datetime.date(2025, 3, 31)),
        ("H1 2024", True, datetime.date(2024, 6, 30)),
        ("2024-2025", True, datetime.date(2025, 12, 31)),
        ("30/06/2025", False, datetime.date(2025, 6, 30)),
        ("2025-06-30", False, datetime.date(2025, 6, 30)),
        ("first quarter of 2026", True, datetime.date(2026, 3, 31)),
        ("soon", True, None),
        ("", False, None),
    ],
)
def test_dates_are_read_as_periods(text, end, expected):
    assert review_locate.period(text, end=end) == expected


def test_places_are_matched_to_the_gazetteer(places):
    gazetteer = review_locate.Gazetteer()
    assert gazetteer.match("Akkar Governorate").match == "governorate"
    assert gazetteer.match("Zahle, Bekaa").district_name == "Zahle"
    assert gazetteer.match("nationwide").match == "general"
    assert gazetteer.match("Atlantis").match == "unmatched"
    assert gazetteer.match("").match == ""


def test_parts_carry_page_markers_and_quotes_are_found_across_lines():
    document = SimpleNamespace(text="\f".join(PAGES), file=None)
    parts = review_locate.parts(document, 120)
    assert parts[0].text.startswith("[Page 1]") and parts[-1].last == 3
    index = review_locate.TextIndex(document)
    assert index.find("water supply in ZAHLE\ndistrict fell by 30 percent") == 2
    assert index.find("Funding gaps … according to the sector partners") == 3
    assert index.find("not in this document at all, surely") is None
    assert index.find("Funding gaps reached 4") is None  # whole words: "4" is not "40"
    assert index.find("Funding gaps reached 40") == 3
    halves = review_locate.halves(review_locate.Part(1, "page", [(1, PAGES[0]), (2, PAGES[1])]))
    assert [h.first for h in halves] == [1, 2]


def test_the_admin_action_analyses_the_chosen_documents(admin_client, report, batch, started):
    loose = Document.objects.create(title="Loose", text="x", status=Document.Status.READY)
    url = reverse("admin:knowledge_document_changelist")
    response = admin_client.post(
        url, {"action": "analyse_in_review", "_selected_action": [report.pk, loose.pk]}, follow=True
    )
    page = response.content.decode()
    assert "Analysing 1 document(s)" in page and "1 document(s) left out" in page
    assert started == [("review_documents", "--document", str(report.pk), "--triggered-by", "admin")]
    second = Document.objects.create(title="Second", text="y", status=Document.Status.READY)
    review.put_in_batch([second], batch)
    admin_client.post(url, {"action": "analyse_in_review", "_selected_action": [report.pk, second.pk]})
    assert started[-1] == ("review_documents", "--pending", "--triggered-by", "admin")
