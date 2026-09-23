"""AI assistant: the streaming loop against a scripted fake Claude client, the page, limits, search."""

import json
import uuid
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import agent, tools
from neurodb.assistant.models import AssistantQuestion

ENABLED = {"AI_ASSISTANT_ENABLED": True, "ANTHROPIC_API_KEY": "test-key-not-real"}


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(name, args, id_="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=id_)


def message(content, stop_reason="end_turn"):
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=50
    )
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage, model="claude-opus-5")


class FakeStream:
    def __init__(self, final):
        self.final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        for block in self.final.content:
            if block.type == "text":
                for word in block.text.split(" "):
                    yield SimpleNamespace(type="text", text=word + " ")

    def get_final_message(self):
        return self.final


class FakeClient:
    """Plays back one scripted message per model call and records the requests."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **params):
        self.requests.append(json.loads(json.dumps(params, default=_dump)))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return FakeStream(step)


def _dump(obj):
    return vars(obj) if isinstance(obj, SimpleNamespace) else str(obj)


@pytest.fixture
def fake(monkeypatch):
    def install(*script):
        client = FakeClient(script)
        monkeypatch.setattr(agent, "client", lambda: client)
        return client

    return install


def _events(response):
    body = b"".join(response.streaming_content).decode()
    return [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]


def _ask(client, question, conversation=None):
    data = {"question": question}
    if conversation:
        data["conversation"] = str(conversation)
    return client.post(reverse("assistant:stream"), data)


@override_settings(**ENABLED)
def test_answer_streams_lookups_and_a_sanitized_answer(client_viewer, hierarchy, fake):
    db = hierarchy["database"]
    claude = fake(
        message([text("Let me check the databases."), tool_use("list_databases", {})], "tool_use"),
        message(
            [
                text(
                    f"**{db.label}** has 2 indicators. See the [dashboard](/databases/{db.id}/).\n\n"
                    "| Indicator | Value |\n|---|---|\n| Children | 300 |\n\n"
                    "<script>alert(1)</script>![x](https://evil.example/x.png?leak=1)"
                )
            ]
        ),
    )

    response = _ask(client_viewer, "How is Child Protection doing?")

    assert response["Content-Type"] == "text/event-stream"
    events = _events(response)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "text" and "tool" in kinds and kinds[-1] == "done"
    assert next(e for e in events if e["type"] == "tool")["label"] == "Listing databases and reports"
    html = events[-1]["html"]
    assert f'href="/databases/{db.id}/"' in html and 'target="_blank"' in html
    assert "<table>" in html
    assert "<script" not in html and "<img" not in html and "evil.example" not in html

    # the request follows the Claude API guidance: model, effort, fallbacks, cached system, tools
    first = claude.requests[0]
    assert first["model"] == "claude-opus-5"
    assert first["output_config"] == {"effort": "medium"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Today is" in first["system"][1]["text"]
    assert all(t["eager_input_streaming"] for t in first["tools"])
    # the tool result went back to Claude with the matching id
    second = claude.requests[1]
    result = second["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "toolu_1" and result["is_error"] is False
    assert db.label in result["content"]

    logged = AssistantQuestion.objects.get()
    assert logged.status == "answered" and logged.user.username == "viewer"
    assert logged.tools[0]["tool"] == "list_databases" and logged.tools[0]["ok"]
    assert logged.input_tokens == 200 and logged.cache_read_tokens == 100 and logged.output_tokens == 40
    assert logged.answer.startswith(f"**{db.label}**")


@override_settings(**ENABLED)
def test_follow_up_questions_carry_the_conversation(client_viewer, db, fake):
    conversation = uuid.uuid4()
    claude = fake(message([text("There are 4 databases.")]), message([text("In 2025 there were 3.")]))

    _events(_ask(client_viewer, "How many databases?", conversation))
    _events(_ask(client_viewer, "And in 2025?", conversation))

    history = claude.requests[1]["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user"]
    assert history[0]["content"] == "How many databases?"
    assert history[1]["content"] == "There are 4 databases."


@override_settings(**ENABLED)
def test_invalid_tool_arguments_go_back_to_claude_as_an_error(client_viewer, db, fake):
    claude = fake(
        message([tool_use("activity_breakdown", {"database_id": 1, "group_by": "colour"})], "tool_use"),
        message([text("Sorry, I used a wrong grouping.")]),
    )
    _events(_ask(client_viewer, "Reports by colour?"))
    result = claude.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "must be one of" in json.loads(result["content"])["error"]


@override_settings(**ENABLED)
def test_refusal_is_reported_not_shown_as_an_answer(client_viewer, db, fake):
    fake(message([], "refusal"))
    events = _events(_ask(client_viewer, "Something the model declines"))
    assert events[-1]["type"] == "error"
    assert AssistantQuestion.objects.get().status == "refused"


@override_settings(**ENABLED)
def test_api_errors_become_friendly_messages(client_viewer, db, fake):
    response = httpx2.Response(429, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    fake(anthropic.RateLimitError("rate limited", response=response, body=None))
    events = _events(_ask(client_viewer, "Anything"))
    assert events == [
        {"type": "error", "message": "The AI service is busy right now. Please try again in a minute."}
    ]
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and "RateLimitError" in logged.error


@override_settings(**ENABLED, AI_ASSISTANT_HOURLY_LIMIT=2)
def test_hourly_limit_per_user(client_viewer, db, fake):
    fake(message([text("One.")]), message([text("Two.")]))
    _events(_ask(client_viewer, "Q1"))
    _events(_ask(client_viewer, "Q2"))
    third = _ask(client_viewer, "Q3")
    assert third.status_code == 429
    assert AssistantQuestion.objects.filter(status="limited").count() == 1


@override_settings(**ENABLED)
def test_questions_are_validated(client_viewer, db):
    assert _ask(client_viewer, "   ").status_code == 400
    assert _ask(client_viewer, "x" * 1001).status_code == 400


def test_disabled_without_an_api_key(client_viewer, db):
    with override_settings(AI_ASSISTANT_ENABLED=False, ANTHROPIC_API_KEY=""):
        page = client_viewer.get(reverse("assistant:ask")).content.decode()
        assert "not set up yet" in page
        assert _ask(client_viewer, "Hello?").status_code == 503


def test_assistant_requires_sign_in(client, db):
    assert client.get(reverse("assistant:ask")).status_code == 302
    assert client.post(reverse("assistant:stream"), {"question": "hi"}).status_code in (302, 403)


@override_settings(**ENABLED)
def test_ask_page_and_search_entry(client_viewer, hierarchy):
    page = client_viewer.get(reverse("assistant:ask") + "?q=hello").content.decode()
    assert 'data-initial-question="hello"' in page and "js/ask.js" in page

    search = reverse("reports:search")
    question = client_viewer.get(search + "?q=which indicators are off track", HTTP_HX_REQUEST="true")
    html = question.content.decode()
    assert "Ask NeuroDB AI" in html  # a question with no name matches: the AI entry is offered
    keyword = client_viewer.get(search + "?q=Children", HTTP_HX_REQUEST="true").content.decode()
    assert keyword.index("Children") < keyword.index("Ask NeuroDB AI")  # keyword search: results first


# ------------------------------------------------------------------------- the tools themselves


def test_database_results_match_the_dashboard(hierarchy):
    db = hierarchy["database"]
    data = tools.run("database_results", {"database_id": db.id})
    by_code = {i["awp_code"]: i for i in data["indicators"]}
    assert by_code["1"]["achieved_value"] == 500.0  # 100 + 50 + 200 + 150
    assert by_code["1"]["target"] == 1000.0
    assert data["url"] == reverse("reports:database_dashboard", args=[db.id])


def test_activity_breakdown_groups_and_filters(hierarchy):
    db = hierarchy["database"]
    by_partner = tools.run("activity_breakdown", {"database_id": db.id, "group_by": "partner"})
    assert {r["partner"]: r["activity_reports"] for r in by_partner["rows"]} == {
        "Partner A": 2,
        "Partner B": 2,
    }
    feb = tools.run("activity_breakdown", {"database_id": db.id, "group_by": "governorate", "month": 2})
    assert [r["governorate"] for r in feb["rows"]] == ["Akkar"]


def test_tool_arguments_are_checked():
    with pytest.raises(tools.ToolInputError):
        tools.validate("database_results", {})
    with pytest.raises(tools.ToolInputError):
        tools.validate("database_results", {"database_id": "3"})
    with pytest.raises(tools.ToolInputError):
        tools.validate("population", {"category": "total", "extra": 1})
    with pytest.raises(tools.ToolInputError):
        tools.validate("no_such_tool", {})
    assert tools.validate("neuro_report", {"report_id": 1, "month": 5}) == {"report_id": 1, "month": 5}


def test_every_tool_definition_is_well_formed():
    definitions = tools.definitions()
    assert len({d["name"] for d in definitions}) == len(definitions) == len(tools.TOOLS)
    for d in definitions:
        assert d["input_schema"]["additionalProperties"] is False
        assert set(d["input_schema"]["required"]) <= set(d["input_schema"]["properties"])
        assert len(d["description"]) > 40


def test_rendering_keeps_alignment_and_drops_unsafe_links():
    html = agent.render("|a|b|\n|---|--:|\n|x|1|\n\n[bad](javascript:alert(1)) [ok](/databases/3/)")
    assert '<td align="right">1</td>' in html
    assert "javascript:" not in html
    assert 'href="/databases/3/"' in html


def test_questions_are_listed_in_the_admin(client, db, roles):
    from neurodb.accounts.models import User

    root = User.objects.create_superuser(
        username="root", email="root@example.org", password="root-pass-123456"
    )
    AssistantQuestion.objects.create(
        user=root, question="How many partners?", answer="Eight.", output_tokens=12
    )
    client.force_login(root)
    page = client.get(reverse("admin:assistant_assistantquestion_changelist"))
    assert page.status_code == 200 and "How many partners?" in page.content.decode()
    assert "AI questions" in client.get(reverse("admin:index")).content.decode()


@override_settings(**ENABLED)
def test_a_stopped_answer_is_logged_as_stopped(rf, viewer, fake):
    from neurodb.assistant.views import _stream

    fake(message([text("A long answer that the user stops.")]))
    request = rf.post("/ask/stream/")
    request.user = viewer
    stream = _stream(request, "Something long", None)
    next(stream)  # the first words arrive, then the browser goes away and the server closes the stream
    stream.close()
    logged = AssistantQuestion.objects.get()
    assert logged.status == "failed" and logged.error == "stopped"
