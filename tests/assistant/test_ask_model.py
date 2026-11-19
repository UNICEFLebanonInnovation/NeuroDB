"""Ask NeuroDB's own model and effort (AI settings): chosen for Ask NeuroDB alone, blank for the Claude model
and effort above them, and only while Claude is the provider. NeuroDB Watch, Monitoring insights, the Help
assistant and every background feature are not changed by them: they keep the model and effort they had
(the Claude model, or their own). The question log and AI use name the model that answered, or on OpenAI
the one the requests were sent to; Test the connection tests Ask's pair too, on a line of its own. Also: one
cache read price in the price table, and the AI use page's column names."""

import json
from decimal import Decimal

import pytest
from django.conf import settings
from django.db.migrations.loader import MigrationLoader
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import agent, llm, usage
from neurodb.assistant.admin import AISettingsAdmin, AISettingsForm, AIUsageAdmin
from neurodb.assistant.llm import claude_backend
from neurodb.assistant.models import AISettings, AIUsage, AssistantQuestion
from neurodb.fmm.ai import chat, profiles, sampling
from neurodb.help import assistant as help_assistant
from neurodb.watch import investigate
from tests.assistant import anthropic_mock
from tests.assistant.anthropic_mock import error_body, http_error, message_json, text, turn
from tests.assistant.openai_mock import MockResponsesServer, completed, message
from tests.assistant.test_ai_settings import _errors, _post, _test, _url
from tests.datamart.test_backfill import QUICK_MIGRATIONS
from tests.fmm.test_chat import _scope

GENERAL = "claude-opus-5-5"  # the Claude model of the tests (the claude fixture's CLAUDE_MODEL)
ASK = "claude-sonnet-5-5"
NO_FALLBACK = "claude-haiku-5-5"  # a model that does not take the refusal fallback
EFFORT_SAID = "output_config.effort: 'max' is not supported by this model."


def _choose(row, **values):
    for name, value in values.items():
        setattr(row, name, value)
    row.save()  # the save clears the snapshot: the next read takes the row


def _answer(question="Is the data up to date?", user=None, options=None):
    outcome = agent.Outcome()
    events = list(agent.answer(question, [], outcome, user=user, options=options))
    return outcome, events


def _ask(client, question="Is the data up to date?"):
    response = client.post(reverse("assistant:stream"), {"question": question})
    body = b"".join(response.streaming_content).decode()
    return [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


@pytest.fixture
def page(client, admin_user, ai_settings):
    client.force_login(admin_user)
    return client


# ------------------------------------------------------------------------------------------- the request
def test_ask_runs_on_its_own_model_and_effort_when_they_are_chosen(claude, ai_settings, viewer):
    _choose(ai_settings, ask_model=ASK, ask_effort="high")
    server = claude(turn([text("The data is fresh.")], model=ASK))
    outcome, events = _answer(user=viewer)
    assert events[-1]["type"] == "done"
    body = server.requests[0]["body"]
    assert body["model"] == ASK and body["output_config"] == {"effort": "high"}
    assert outcome.model == ASK


def test_ask_runs_on_the_claude_model_and_effort_when_its_own_are_blank(claude, ai_settings):
    _choose(ai_settings, claude_model="claude-opus-5", claude_effort="low")
    server = claude(turn([text("Fine.")], model="claude-opus-5"))
    _answer()
    body = server.requests[0]["body"]
    assert body["model"] == "claude-opus-5" and body["output_config"] == {"effort": "low"}


def test_each_of_asks_two_choices_stands_alone(claude, ai_settings):
    _choose(ai_settings, ask_effort="xhigh")  # the model stays the Claude model
    server = claude(turn([text("Fine.")]), turn([text("Fine.")], model=ASK))
    _answer()
    first = server.requests[0]["body"]
    assert first["model"] == GENERAL and first["output_config"] == {"effort": "xhigh"}
    _choose(ai_settings, ask_model=ASK, ask_effort="")  # the effort is Claude's again (CLAUDE_EFFORT)
    _answer()
    second = server.requests[1]["body"]
    assert second["model"] == ASK and second["output_config"] == {"effort": settings.CLAUDE_EFFORT}


def test_the_ask_model_applies_only_while_claude_is_the_provider(ai_settings):
    _choose(ai_settings, ask_model=ASK, ask_effort="max")
    assert llm.provider() == llm.OPENAI  # no Anthropic key in the tests
    params = agent._request(None)
    assert params["model"] == settings.AI_ASSISTANT_MODEL
    assert params["reasoning"] == {"effort": settings.AI_ASSISTANT_EFFORT}
    assert llm.ask_model() == settings.AI_ASSISTANT_MODEL and llm.ask_effort() == settings.AI_ASSISTANT_EFFORT


def test_watch_monitoring_insights_help_and_the_background_features_keep_the_claude_model(
    claude, ai_settings, viewer
):
    _choose(ai_settings, ask_model=ASK, ask_effort="max")
    # NeuroDB Watch's look-up, through the API
    server = claude(
        turn([text('{"what_i_found": "Nothing new.", "numbers": []}')]),
        turn([text("Two sentences.")]),
    )
    _answer("The point, as JSON", options=investigate.run_options([], frozenset()))
    watch = server.requests[0]["body"]
    assert watch["model"] == GENERAL
    assert watch["output_config"]["effort"] == llm.effort_for(investigate.EFFORT, llm.CLAUDE)
    # a background feature's call (the daily review, What's new, document summaries...)
    llm.complete(llm.Request(system="Write two sentences.", user="Facts.", effort=llm.default_effort()))
    background = server.requests[1]["body"]
    assert background["model"] == GENERAL
    assert background["output_config"] == {"effort": settings.CLAUDE_EFFORT}
    # the Help assistant and Monitoring insights' chat
    helper = agent._claude_request(viewer, help_assistant.options(viewer, "The page: Home."))
    assert helper["model"] == GENERAL
    assert helper["output_config"]["effort"] == llm.effort_for(help_assistant.EFFORT, llm.CLAUDE)
    version = profiles.published()
    insights = agent._claude_request(
        viewer, chat.options(version, chat.context(_scope(), version), sampling.Plan())
    )
    assert insights["model"] == GENERAL
    assert insights["output_config"]["effort"] == llm.effort_for(version.effort, llm.CLAUDE)


def test_the_refusal_fallback_follows_the_ask_model(claude, ai_settings):
    _choose(ai_settings, ask_model=NO_FALLBACK)
    assert agent._claude_request(None)["model"] == NO_FALLBACK
    assert "fallbacks" not in agent._claude_request(None)  # Ask's model does not take it
    watch = agent._claude_request(None, investigate.run_options([], frozenset()))
    assert watch["model"] == GENERAL and watch["fallbacks"] == "default"  # the Claude model does
    _choose(ai_settings, claude_model=NO_FALLBACK, ask_model=ASK)
    ask = agent._claude_request(None)
    assert ask["model"] == ASK and ask["fallbacks"] == "default"
    assert ask["betas"] == [claude_backend.FALLBACK_BETA]
    assert "fallbacks" not in agent._claude_request(None, investigate.run_options([], frozenset()))


# ------------------------------------------------------------------------------ question log and AI use
def test_the_question_log_and_ai_use_book_asks_calls_under_the_ask_model(claude, ai_settings, client_viewer):
    _choose(ai_settings, ask_model=ASK)
    server = claude(turn([text("Fresh.")], model=ASK, usage=anthropic_mock.usage(100, 20)))
    assert _ask(client_viewer)[-1]["type"] == "done"
    assert server.requests[0]["body"]["model"] == ASK
    assert AssistantQuestion.objects.get().model == ASK
    row = AIUsage.objects.get(feature=usage.ASK)
    assert (row.model, row.calls, row.input_tokens, row.output_tokens) == (ASK, 1, 100, 20)


def test_a_question_that_failed_before_any_answer_is_logged_under_the_ask_model(
    claude, ai_settings, client_viewer
):
    _choose(ai_settings, ask_model=ASK)
    claude(http_error(401, error_body("authentication_error", "invalid x-api-key")))
    assert _ask(client_viewer)[-1] == {"type": "error", "message": llm.KEY_REJECTED}
    assert AssistantQuestion.objects.get().model == ASK
    assert not AIUsage.objects.exists()  # nothing answered: nothing to book


def test_a_question_being_answered_names_the_ask_model(claude, ai_settings, client_viewer):
    _choose(ai_settings, ask_model=ASK)
    response = client_viewer.post(reverse("assistant:stream"), {"question": "Is the data up to date?"})
    assert response.status_code == 200  # the stream is not read: the question is still being answered
    assert AssistantQuestion.objects.get().model == ASK


# --------------------------------------------------------------------------------- test the connection
def test_the_connection_test_also_tests_asks_own_pair_on_a_line_of_its_own(claude, ai_settings):
    _choose(ai_settings, ask_model=ASK, ask_effort="high")
    server = claude(
        message_json([text("OK")], usage_=anthropic_mock.usage(12, 16)),
        message_json([text("OK")], model=ASK, usage_=anthropic_mock.usage(10, 8)),
    )
    check = llm.test_connection()
    general, ask = (request["body"] for request in server.requests)
    assert (general["model"], general["output_config"]) == (GENERAL, {"effort": settings.CLAUDE_EFFORT})
    assert (ask["model"], ask["output_config"]) == (ASK, {"effort": "high"})
    assert ask["fallbacks"] == "default" and ask["messages"] == general["messages"]  # no NeuroDB data
    first, second = check.text.split("\n")
    assert first.startswith(f"Claude (Anthropic) answered with {GENERAL} in ")
    assert first.endswith(": the key, the model and the credit work.")
    assert second.startswith(f"Ask NeuroDB: Claude (Anthropic) answered with {ASK} at effort high in ")
    assert check.ok and check.model == GENERAL
    booked = {row.model: (row.calls, row.input_tokens) for row in AIUsage.objects.filter(feature=usage.TEST)}
    assert booked == {GENERAL: (1, 12), ASK: (1, 10)}


def test_an_effort_the_ask_model_does_not_take_is_said_as_for_the_general_setting(claude, ai_settings):
    _choose(ai_settings, ask_model=NO_FALLBACK, ask_effort="max")
    server = claude(
        message_json([text("OK")]), http_error(400, error_body("invalid_request_error", EFFORT_SAID))
    )
    check = llm.test_connection()
    assert server.requests[1]["body"]["output_config"] == {"effort": "max"}
    assert not check.ok
    assert check.text.split("\n")[1] == (
        f"Ask NeuroDB: the model {NO_FALLBACK} does not take the effort max: choose another Ask NeuroDB effort "
        f"here and test again. The API said: {EFFORT_SAID}"
    )


def test_asks_pair_is_tested_even_when_the_general_effort_was_refused(claude, ai_settings):
    _choose(ai_settings, claude_effort="max", ask_effort="low")
    claude(http_error(400, error_body("invalid_request_error", EFFORT_SAID)), message_json([text("OK")]))
    check = llm.test_connection()
    first, second = check.text.split("\n")
    assert first.startswith(f"The model {GENERAL} does not take the default effort max")
    assert second.startswith(f"Ask NeuroDB: Claude (Anthropic) answered with {GENERAL} at effort low in ")
    assert not check.ok


def test_asks_pair_is_not_tested_when_the_test_above_failed_for_both(claude, ai_settings):
    _choose(ai_settings, ask_model=ASK)
    server = claude(http_error(401, error_body("authentication_error", "invalid x-api-key")))
    check = llm.test_connection()
    assert len(server.requests) == 1  # the key fails for both: one call is enough
    assert check.text == f"{llm.KEY_REJECTED}\nAsk NeuroDB: not tested, as the test above failed."
    assert not check.ok


def test_asks_pair_equal_to_the_general_one_is_not_sent_twice(claude, ai_settings):
    _choose(ai_settings, ask_model=GENERAL, ask_effort=settings.CLAUDE_EFFORT)
    server = claude(message_json([text("OK")]))
    check = llm.test_connection()
    assert len(server.requests) == 1 and check.ok
    assert check.text.split("\n")[1] == "Ask NeuroDB: the same model and effort as above."


def test_with_nothing_chosen_for_ask_the_test_is_one_line(claude, ai_settings):
    claude(message_json([text("OK")]))
    assert "\n" not in llm.test_connection().text


def test_the_admin_shows_each_line_of_the_test(page, claude):
    assert _post(page, ask_model=NO_FALLBACK, ask_effort="max").status_code == 302
    claude(message_json([text("OK")]), http_error(400, error_body("invalid_request_error", EFFORT_SAID)))
    response = _test(page)
    assert response.status_code == 302
    row = AISettings.objects.get()
    general, ask = row.check_result.split("\n")  # both lines are kept whole
    assert ask.endswith(EFFORT_SAID)
    shown = [(m.level_tag, str(m)) for m in response.wsgi_request._messages]
    assert shown[-2:] == [("success", general), ("warning", ask)]  # after the save's own message
    html = page.get(_url()).content.decode()
    assert "Ask NeuroDB: the model claude-haiku-5-5 does not take the effort max" in html


def test_on_openai_the_test_has_no_ask_line(page, monkeypatch):
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    server = MockResponsesServer([completed([message("msg_1", "OK")])]).start()
    monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
    try:
        with override_settings(AI_ASSISTANT_ENABLED=True, OPENAI_API_KEY="test-key-not-real"):
            assert _post(page, ask_model=ASK, ask_effort="high").status_code == 302
            assert _test(page).status_code == 302
    finally:
        server.stop()
    assert len(server.requests) == 1
    assert "Ask NeuroDB" not in AISettings.objects.get().check_result


# ------------------------------------------------------------------------------------------ the page
def test_the_two_fields_sit_with_the_claude_fields(page):
    html = page.get(_url()).content.decode()
    assert "Ask NeuroDB model" in html and "Ask NeuroDB effort" in html
    assert (
        "Only Ask NeuroDB, and only while Claude is the provider. Leave blank to use the Claude model above."
        in html
    )
    fields = dict(AISettingsAdmin.fieldsets)["Claude (Anthropic)"]["fields"]
    assert fields.index("ask_model") == fields.index("claude_effort") + 1
    assert fields.index("ask_effort") == fields.index("ask_model") + 1
    form = AISettingsForm(instance=AISettings.load())
    assert form.fields["ask_model"].choices[1:] == form.fields["claude_model"].choices[1:]
    assert [value for value, _label in form.fields["ask_effort"].choices[1:]] == list(settings.CLAUDE_EFFORTS)
    assert form.fields["ask_model"].choices[0][0] == "" and form.fields["ask_effort"].choices[0][0] == ""


def test_the_choices_are_checked_and_a_save_applies_at_once(page):
    assert "ask_model" in _errors(_post(page, ask_model="claude-3-haiku"))
    assert "ask_effort" in _errors(_post(page, ask_effort="extreme"))
    assert _post(page, ask_model=ASK, ask_effort="high").status_code == 302
    entered = llm.snapshot()
    assert (entered.ask_model, entered.ask_effort) == (ASK, "high")
    # an older release's snapshot in the shared cache has neither: Ask then runs on the Claude settings
    kept = llm._kept({"provider": "claude", "claude_model": GENERAL})
    assert (kept.ask_model, kept.ask_effort) == ("", "")


def test_the_page_says_which_model_ask_runs_on(page, claude):
    assert _post(page, ask_model=ASK, ask_effort="high").status_code == 302
    html = page.get(_url()).content.decode()
    assert f"Ask NeuroDB runs on model {ASK}, effort high." in html  # In use now
    rows = html.split("<tr>")
    ask = next(row for row in rows if row.startswith("<td>Ask NeuroDB</td>"))
    helper = next(row for row in rows if row.startswith("<td>Help assistant</td>"))
    assert f"<code>{ASK}</code>" in ask and f"<code>{GENERAL}</code>" in helper


# ------------------------------------------------------------------------------- prices, labels, migration
def test_the_cache_read_price_of_the_current_sonnet():
    assert usage.price_for(ASK)["cached"] == Decimal("0.10")
    assert usage.price_for(ASK)["input"] == Decimal("2.00")


def test_the_ai_use_columns_say_what_they_count(client, admin_user):
    usage.record(usage.ASK, GENERAL, llm.Usage(input_tokens=1_000, cache_write_tokens=400, output_tokens=5))
    client.force_login(admin_user)
    page = client.get(reverse("admin:assistant_aiusage_changelist")).content.decode()
    assert "Prompt tokens (not cached)" not in page
    assert "Prompt tokens not read from the cache" in page
    # the next column is part of this one: "Of these"
    assert "Of these, written to the cache" in page
    assert page.index("Prompt tokens not read from the cache") < page.index("Of these, written to the cache")
    # the third column says read, as the model's field and OPERATIONS do, never just "cached"
    assert "Cached prompt tokens" not in page
    assert page.index("Of these, written to the cache") < page.index("Prompt tokens read from the cache")


def test_the_ai_use_table_fits_a_laptop_screen():
    """Browser check: a heading of 63 characters ("Written to the cache (part of the prompt tokens not read
    from it)") made the table 1,863 px wide, so Cost was off screen even at 1,920 px. The headings stay
    short, and the three cache headings wrap onto more lines (admin.css)."""
    for name in AIUsageAdmin.list_display:
        heading = str(getattr(getattr(AIUsageAdmin, name, None), "short_description", name))
        assert len(heading) <= 40, heading
    css = (settings.BASE_DIR / "neurodb/web/static/css/admin.css").read_text()
    rule = css.split(".model-aiusage :is(th.column-prompt_tokens, th.column-written, th.column-cached) {", 1)
    assert len(rule) == 2, "no rule for the AI use headings"
    assert "white-space: normal" in rule[1].split("}", 1)[0]


def test_the_connection_test_says_when_it_tries_asks_pair(page):
    url = reverse("admin:assistant_aisettings_test_connection", args=[AISettings.load().pk])
    html = page.get(url, HTTP_HX_REQUEST="true").content.decode()  # the dialog, as the page opens it
    assert "sends that one too" not in html
    assert (
        "While Claude is in use and an Ask NeuroDB model or effort is chosen, it also tries that pair and "
        "says what happened on a line of its own."
    ) in html


def test_the_migration_is_quick():
    found = MigrationLoader(None, ignore_no_migrations=True).disk_migrations
    migration = found[("assistant", "0005_ask_model")]
    assert {type(op).__name__ for op in migration.operations} <= {"AddField", "AlterField"}
    assert ("assistant", "0005_ask_model", "AddField") in QUICK_MIGRATIONS


def test_on_openai_ai_use_books_the_model_the_request_went_to(monkeypatch, ai_settings, client_viewer):
    """AI settings saved while a question is answered: the question's tokens stay under the model they were
    sent to, not under the one read when it ends."""
    from tests.assistant.test_assistant import ENABLED, FakeClient, reply, say

    class SavedMeanwhile(FakeClient):
        def stream(self, **params):
            stream = super().stream(**params)
            _choose(ai_settings, openai_model="gpt-other")  # an administrator saves AI settings
            return stream

    client = SavedMeanwhile([reply(say("Fresh."), input_tokens=100, cached=0, output_tokens=20)])
    monkeypatch.setattr(agent, "client", lambda: client)
    with override_settings(**ENABLED, AI_PROVIDER="openai"):
        assert _ask(client_viewer)[-1]["type"] == "done"
    assert client.requests[0]["model"] == "gpt-5.5"
    row = AIUsage.objects.get(feature=usage.ASK)
    assert (row.model, row.input_tokens, row.output_tokens) == ("gpt-5.5", 100, 20)
    assert AssistantQuestion.objects.get().model == "gpt-5.5"


def test_a_switch_to_claude_while_an_openai_question_runs_books_it_under_openai(
    monkeypatch, ai_settings, client_viewer
):
    from tests.assistant.test_assistant import ENABLED, FakeClient, reply, say

    _choose(ai_settings, provider="openai", ask_model=ASK)

    class SwitchedMeanwhile(FakeClient):
        def stream(self, **params):
            stream = super().stream(**params)
            _choose(ai_settings, provider="claude")
            return stream

    client = SwitchedMeanwhile([reply(say("Fresh."), input_tokens=100, cached=0, output_tokens=20)])
    monkeypatch.setattr(agent, "client", lambda: client)
    with override_settings(**ENABLED, ANTHROPIC_API_KEY="test-key-not-real", AI_PROVIDER="auto"):
        assert _ask(client_viewer)[-1]["type"] == "done"
    assert [row.model for row in AIUsage.objects.filter(feature=usage.ASK)] == ["gpt-5.5"]
