"""The ledger of AI use on the shared OpenAI key: cached tokens kept apart, one row per day, feature
and model, one call counted per model call in every feature, never failing the feature, and the
read-only admin list (in US dollars only when the prices are set)."""

import datetime
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from neurodb.assistant import usage
from neurodb.assistant.models import AIUsage, AssistantQuestion
from neurodb.cpd import extraction
from neurodb.cpd.models import CountryProgramme, CPDocument
from neurodb.graph import digest
from neurodb.knowledge import indexing, periodic
from neurodb.knowledge.models import Document
from neurodb.review import services as review
from neurodb.review.models import DailyReview
from tests.assistant.test_assistant import ENABLED, FakeClient, _ask, _events, call, reply, say
from tests.reports.overview_fixture import TODAY, make_overview_data

pytestmark = pytest.mark.django_db
MODEL = "test-model"


def tokens(prompt, output, cached=0):
    """A response's usage as the SDK gives it: the prompt tokens include the cached ones."""
    return SimpleNamespace(
        input_tokens=prompt,
        output_tokens=output,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


class FakeResponses:
    """``responses.create`` of a client: every call answers ``text`` with ``usage`` and is kept."""

    def __init__(self, text="Done.", usage_=None):
        self.text, self.usage, self.calls = text, usage_, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.text, usage=self.usage)


@pytest.fixture
def ai(settings, monkeypatch):
    """The assistant switched on, with a model that answers ``text`` (set it before the call)."""
    settings.AI_ASSISTANT_ENABLED, settings.AI_ASSISTANT_MODEL = True, MODEL
    responses = FakeResponses(usage_=tokens(1000, 200, cached=600))
    monkeypatch.setattr("neurodb.assistant.agent.client", lambda: SimpleNamespace(responses=responses))
    return responses


@pytest.fixture
def scripted(monkeypatch):
    """Ask NeuroDB's model, playing back one scripted reply per call."""

    def install(*script):
        client = FakeClient(script)
        monkeypatch.setattr("neurodb.assistant.agent.client", lambda: client)
        return client

    return install


@pytest.fixture
def no_prices(settings):
    """No price set, whatever the environment gives."""
    for name in ("AI_PRICE_INPUT_PER_MTOK", "AI_PRICE_CACHED_PER_MTOK", "AI_PRICE_OUTPUT_PER_MTOK"):
        setattr(settings, name, None)
    return settings


@pytest.fixture
def broken_ledger(monkeypatch):
    """Every write to the ledger fails in the database itself (its table is gone)."""
    monkeypatch.setattr(AIUsage._meta, "db_table", "assistant_aiusage_gone")


def row(feature):
    return AIUsage.objects.get(feature=feature)


# ------------------------------------------------------------------------------------ the ledger
def test_cached_tokens_are_kept_apart_from_the_prompt():
    usage.record(usage.WATCH, MODEL, tokens(1000, 200, cached=600))
    logged = row(usage.WATCH)
    assert (logged.day, logged.model, logged.calls) == (timezone.localdate(), MODEL, 1)
    assert (logged.input_tokens, logged.cached_tokens, logged.output_tokens) == (400, 600, 200)
    assert logged.total_tokens == 1200 and usage.today_total(usage.WATCH) == 1200
    assert usage.split(tokens(50, 5)) == (50, 0, 5)  # no cache details: nothing cached


def test_two_records_on_one_day_add_up_in_one_row():
    usage.record(usage.WATCH, MODEL, tokens(1000, 200, cached=600))
    usage.record(usage.WATCH, MODEL, tokens(500, 100))
    usage.record(usage.ASK, MODEL, tokens(10, 1))
    usage.record(usage.WATCH, "other-model", None)  # a response without usage: the call still counts
    logged = AIUsage.objects.get(feature=usage.WATCH, model=MODEL)
    assert (logged.calls, logged.input_tokens, logged.cached_tokens, logged.output_tokens) == (
        2,
        900,
        600,
        300,
    )
    assert AIUsage.objects.count() == 3
    assert usage.today_total(usage.WATCH) == 1800 and usage.today_calls(usage.WATCH) == 3
    assert usage.today_total() == 1811 and usage.today_calls() == 4
    assert usage.today_total([usage.WATCH, usage.ASK]) == 1811 and usage.today_total(usage.CPD) == 0


def test_yesterdays_use_is_not_todays():
    AIUsage.objects.create(
        day=timezone.localdate() - datetime.timedelta(days=1), feature=usage.WATCH, model=MODEL, calls=9,
        input_tokens=9000,
    )  # fmt: skip
    assert usage.today_total(usage.WATCH) == 0 and usage.today_calls() == 0
    usage.record(usage.WATCH, MODEL, tokens(10, 1))
    assert AIUsage.objects.count() == 2 and usage.today_total() == 11


def test_nothing_is_recorded_without_a_call():
    usage.record(usage.ASK, MODEL, None, calls=0)
    assert not AIUsage.objects.exists()


def test_a_database_error_never_reaches_the_caller(broken_ledger, caplog):
    usage.record(usage.WATCH, MODEL, tokens(10, 1))  # does not raise
    assert "AI use of watch could not be recorded" in caplog.text
    # the failed write was rolled back to its savepoint: the caller's transaction still works
    assert AssistantQuestion.objects.count() == 0
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


# ------------------------------------------------------------------------- one row per call, per feature
@override_settings(**ENABLED)
def test_ask_records_every_model_call_of_an_answer(client_viewer, scripted):
    scripted(reply(say("Four.")))
    assert _events(_ask(client_viewer, "How many databases?"))[-1]["type"] == "done"
    logged = row(usage.ASK)
    # OpenAI counts cached tokens in the prompt: 150 in (50 of them cached) and 20 out
    assert (logged.model, logged.calls) == (ENABLED["AI_ASSISTANT_MODEL"], 1)
    assert (logged.input_tokens, logged.cached_tokens, logged.output_tokens) == (100, 50, 20)
    question = AssistantQuestion.objects.get()
    assert (question.input_tokens, question.cache_read_tokens) == (100, 50)  # the question log agrees


@override_settings(**ENABLED)
def test_ask_counts_each_round_of_lookups(client_viewer, scripted, hierarchy):
    scripted(reply(call("list_databases", {})), reply(say("There is one database.")))
    assert _events(_ask(client_viewer, "How many databases?"))[-1]["type"] == "done"
    logged = row(usage.ASK)
    assert (logged.calls, logged.input_tokens, logged.cached_tokens, logged.output_tokens) == (
        2,
        200,
        100,
        40,
    )


def test_the_review_records_its_summary_and_its_decisions(ai, settings):
    make_overview_data()
    ai.text = '{"decisions": []}'
    created = review.run(date=TODAY, today=TODAY)
    assert created.status == DailyReview.Status.SUCCEEDED and len(ai.calls) == 2  # summary, decisions
    logged = row(usage.REVIEW)
    assert (logged.model, logged.calls, logged.input_tokens, logged.cached_tokens) == (MODEL, 2, 800, 1200)


def test_the_whats_new_note_records_its_call(ai):
    text, written_by = digest.narrate("all sections", ["Amel Association signed a new programme document."])
    assert written_by == MODEL and len(ai.calls) == 1
    assert (row(usage.DIGEST).calls, row(usage.DIGEST).output_tokens) == (1, 200)


def test_a_document_summary_records_its_call(ai):
    ai.text = json.dumps(
        {"summary": "Minutes.", "key_points": [], "document_date": "", "organisations": [], "places": [],
         "references": []}
    )  # fmt: skip
    document = Document.objects.create(title="Minutes", text="The working group met.")
    assert indexing.summarise(document) is True
    assert (row(usage.KNOWLEDGE).calls, row(usage.KNOWLEDGE).model) == (1, MODEL)


def test_periodic_report_figures_record_their_call(ai):
    ai.text = json.dumps({"figures": []})
    document = Document.objects.create(title="Snapshot", text="People displaced: 1,200", periodic=True)
    assert periodic._ai_figures(document, ["People displaced: 1,200"], []) == []
    assert row(usage.PERIODIC).calls == 1


def test_the_country_programme_reading_records_its_call(ai, settings, tmp_path):
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
    settings.MEDIA_ROOT = tmp_path
    ai.text = json.dumps({"outcomes": []})
    cycle = CountryProgramme.objects.create(name="CP 2026-2028", start_year=2026, end_year=2028, current=True)
    document = CPDocument.objects.create(
        programme=cycle, title="CPD", file=SimpleUploadedFile("cpd.txt", b"Outcome 1: Children learn")
    )
    assert extraction.extract(document) == {"outcomes": []}
    assert row(usage.CPD).calls == 1


# ------------------------------------------------------------------- a broken ledger breaks nothing
def test_a_broken_ledger_leaves_the_review_and_the_note_working(ai, broken_ledger):
    make_overview_data()
    ai.text = "Two things need a person today."
    created = review.run(date=TODAY, today=TODAY)
    assert created.status == DailyReview.Status.SUCCEEDED and created.narrated_by == MODEL
    assert created.summary == "Two things need a person today."
    assert digest.narrate("all sections", ["A change."]) == ("Two things need a person today.", MODEL)


@override_settings(**ENABLED)
def test_a_broken_ledger_leaves_ask_working(client_viewer, scripted, broken_ledger):
    scripted(reply(say("Four.")))
    events = _events(_ask(client_viewer, "How many databases?"))
    assert events[-1]["type"] == "done" and events[-1]["answer"] == "Four."
    assert AssistantQuestion.objects.get().status == AssistantQuestion.Status.ANSWERED


# ------------------------------------------------------------------------------- prices and admin
def test_prices_need_the_input_and_output_prices(no_prices):
    settings = no_prices
    assert usage.prices() is None
    settings.AI_PRICE_INPUT_PER_MTOK = "1.25"
    assert usage.prices() is None  # no output price
    settings.AI_PRICE_OUTPUT_PER_MTOK = 10
    price = usage.prices()
    assert price == {"input": Decimal("1.25"), "cached": Decimal("1.25"), "output": Decimal("10")}
    settings.AI_PRICE_CACHED_PER_MTOK = "0.125"
    assert usage.prices()["cached"] == Decimal("0.125")
    settings.AI_PRICE_OUTPUT_PER_MTOK = "ten"
    assert usage.prices() is None
    settings.AI_PRICE_OUTPUT_PER_MTOK = 0
    assert usage.prices() is None  # zero: not set
    one = AIUsage(input_tokens=1_000_000, cached_tokens=2_000_000, output_tokens=100_000)
    assert usage.cost(one, price) == Decimal("4.75")  # 1.25 + 2 x 1.25 + 0.1 x 10


def test_the_admin_lists_the_use_in_tokens_and_dollars_only_with_prices(client, admin_user, no_prices):
    settings = no_prices
    usage.record(usage.ASK, MODEL, tokens(1_500_000, 100_000, cached=500_000))
    client.force_login(admin_user)
    url = reverse("admin:assistant_aiusage_changelist")
    page = client.get(url).content.decode()
    assert "Ask NeuroDB" in page and "1,000,000" in page and "500,000" in page and "1,600,000" in page
    assert "Cost (USD)" not in page
    settings.AI_PRICE_INPUT_PER_MTOK, settings.AI_PRICE_OUTPUT_PER_MTOK = "2", "10"
    page = client.get(url).content.decode()
    assert (
        "Cost (USD)" in page and "$4.00" in page
    )  # 1M x 2 + 0.5M x 2 (cached at the input price) + 0.1M x 10
    assert client.get(url, {"feature": usage.ASK}).status_code == 200
    logged = row(usage.ASK)
    assert client.post(reverse("admin:assistant_aiusage_delete", args=[logged.pk])).status_code == 403
    assert reverse("admin:assistant_aiusage_add") not in page
