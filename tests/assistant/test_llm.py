"""The provider layer (neurodb.assistant.llm): which provider runs and when the AI is on, the AI settings
snapshot, models and effort, the OpenAI request and response rules, Claude's schema and usage, the failure
messages and the sealed keys. The wire itself is test_claude_wire.py's (Claude) and test_sdk_wire.py's
(OpenAI)."""

import json
import logging
import pickle
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest
from django.conf import settings
from django.core.cache import caches
from django.db import connection
from django.test import override_settings
from django.urls import reverse

from neurodb.assistant import agent, llm, usage
from neurodb.assistant.llm import claude_backend, keys, openai_backend
from neurodb.assistant.models import AISettings, AIUsage, AssistantQuestion
from tests.assistant import anthropic_mock
from tests.assistant.anthropic_mock import (
    MODEL,
    error_body,
    fallback,
    http_error,
    iteration,
    message_json,
    text,
    tool_use,
    turn,
)

ROOT = Path(__file__).resolve().parents[2]
KEY = "kx-test-" + "7" * 32  # a key entered in AI settings (not a real one, and in no real key's form)
OFF = {"AI_ASSISTANT_ENABLED": False, "AI_ASSISTANT_SWITCH": True}


def _enter(row, **fields):
    """Write AI settings as the admin page saves them (sealed keys)."""
    for prefix in ("anthropic", "openai"):
        if f"{prefix}_key" in fields:
            value = fields.pop(f"{prefix}_key")
            setattr(row, f"{prefix}_key_sealed", keys.seal(value) if value else "")
            setattr(row, f"{prefix}_key_last4", keys.last4(value))
    for name, value in fields.items():
        setattr(row, name, value)
    row.save()
    return row


# --------------------------------------------------------------------------------- provider and switch
@pytest.mark.parametrize(
    "pinned, chosen, env_anthropic, entered_anthropic, expected",
    [
        ("auto", "auto", "", False, llm.OPENAI),  # nothing anywhere: OpenAI, as today
        ("auto", "auto", "test-key-not-real", False, llm.CLAUDE),  # Automatic: Claude when its key is there
        ("auto", "auto", "", True, llm.CLAUDE),  # ... entered in AI settings too
        ("auto", "openai", "test-key-not-real", True, llm.OPENAI),  # the administrators' choice
        ("auto", "claude", "", False, llm.CLAUDE),  # chosen, even without a key (AI settings says so)
        ("openai", "claude", "test-key-not-real", True, llm.OPENAI),  # the hosting team's comes first
        ("claude", "openai", "", False, llm.CLAUDE),
    ],
)
def test_which_provider_runs(ai_settings, pinned, chosen, env_anthropic, entered_anthropic, expected):
    _enter(ai_settings, provider=chosen, anthropic_key=KEY if entered_anthropic else "")
    with override_settings(AI_PROVIDER=pinned, ANTHROPIC_API_KEY=env_anthropic):
        assert llm.provider() == expected
        assert llm.chosen_by() == ("hosting" if pinned != "auto" else "admin" if chosen != "auto" else "auto")


def test_an_unreadable_key_does_not_count(ai_settings):
    _enter(ai_settings, anthropic_key=KEY, openai_key=KEY)
    with override_settings(SECRET_KEY="another-secret-key-for-this-test-only-0123456789abcdef"):
        entered = llm.snapshot()
        assert entered.anthropic_entered and not entered.anthropic_readable and not entered.openai_readable
        assert llm.provider() == llm.OPENAI and not llm.anthropic_available()
        with override_settings(**OFF):
            assert not llm.enabled()
    assert llm.snapshot().anthropic_readable  # the snapshot is read again under the right secret key


def test_the_ai_is_on_by_the_environment_or_a_key_entered_for_the_provider_in_use(ai_settings):
    with override_settings(AI_ASSISTANT_ENABLED=True):
        assert llm.enabled()  # the environment's key: no query
    with override_settings(**OFF, OPENAI_API_KEY="x"):
        # an environment key is already inside AI_ASSISTANT_ENABLED: a test that set it False means off
        assert not llm.enabled()
        _enter(ai_settings, openai_key=KEY)
        assert llm.enabled() and llm.provider() == llm.OPENAI
        _enter(ai_settings, provider="claude")  # Claude chosen: its key counts, not OpenAI's
        assert not llm.enabled()
        _enter(ai_settings, anthropic_key=KEY)
        assert llm.enabled()
    with override_settings(AI_ASSISTANT_ENABLED=False, AI_ASSISTANT_SWITCH=False):
        assert not llm.enabled()  # the hosting team's switch: off whatever is entered


def test_no_query_on_the_hot_path(django_assert_num_queries, db):
    with django_assert_num_queries(0):
        with override_settings(AI_ASSISTANT_ENABLED=True):
            assert llm.enabled()
        with override_settings(**OFF):
            assert not llm.enabled() and llm.provider() == llm.OPENAI
            assert llm.names().label == "ChatGPT (OpenAI)"


def test_the_budget_canary_stays_off(db):
    """tests/fmm/test_budget.py sets AI_ASSISTANT_ENABLED=False beside OPENAI_API_KEY="x": AI is off."""
    with override_settings(AI_ASSISTANT_ENABLED=False, OPENAI_API_KEY="x"):
        assert not llm.enabled()


def test_claude_chosen_with_an_unreadable_key_says_not_set_up(ai_settings):
    """The remaining case: AI on through an OpenAI key from the environment, Claude chosen in AI settings,
    its key no longer readable: the features say "not set up"."""
    _enter(ai_settings, provider="claude", anthropic_key=KEY)
    with override_settings(AI_ASSISTANT_ENABLED=True, OPENAI_API_KEY="x", ANTHROPIC_API_KEY=""):
        with override_settings(SECRET_KEY="another-secret-key-for-this-test-only-0123456789abcdef"):
            assert llm.enabled() and llm.provider() == llm.CLAUDE
            with pytest.raises(llm.AssistantUnavailable) as raised:
                llm.connect()
            assert llm.friendly(raised.value) == llm.NOT_SET_UP


def test_no_openai_key_at_all_is_not_set_up(db):
    with override_settings(AI_ASSISTANT_ENABLED=True, OPENAI_API_KEY="", ANTHROPIC_API_KEY=""):
        with pytest.raises(llm.AssistantUnavailable):
            agent.client()


def test_the_key_entered_is_the_one_called_with(ai_settings):
    _enter(ai_settings, openai_key=KEY, anthropic_key="kx-claude-" + "1" * 30)
    with override_settings(**OFF, OPENAI_API_KEY="", ANTHROPIC_API_KEY=""):
        assert agent.client().api_key == KEY
        assert llm.anthropic_key() == "kx-claude-" + "1" * 30
    with override_settings(OPENAI_API_KEY="env-key-not-real"):
        assert llm.openai_key() == "env-key-not-real"  # the environment's comes first


# ------------------------------------------------------------------------------------ the snapshot
def test_a_key_entered_turns_claude_on_with_no_restart(ai_settings, monkeypatch):
    """The saving worker follows at once; another one within its memo's 10 seconds."""
    now = [1000.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(llm, "_pinned", False)
    with override_settings(**OFF):
        assert not llm.enabled()
        stale = llm.snapshot()
        _enter(ai_settings, anthropic_key=KEY)  # the save clears this worker's memo and the shared cache
        assert llm.enabled() and llm.provider() == llm.CLAUDE
        llm._memo = (now[0], stale)  # another worker, which read the settings just before the save
        now[0] += 9
        assert not llm.enabled()
        now[0] += 2  # past its 10 seconds: the shared cache was cleared, it reads the row
        assert llm.enabled() and llm.provider() == llm.CLAUDE


def test_the_snapshot_is_shared_and_holds_no_secret(ai_settings, monkeypatch):
    monkeypatch.setattr(llm, "_pinned", False)
    _enter(ai_settings, anthropic_key=KEY, claude_effort="high")
    llm._memo = None
    assert llm.snapshot().claude_effort == "high"
    kept = caches["shared"].get(llm._cache_key())
    assert kept["claude_effort"] == "high" and kept["anthropic_readable"] is True
    assert kept["anthropic_last4"] == KEY[-4:] and KEY not in json.dumps(kept)
    assert "anthropic_key_sealed" not in kept


def test_a_save_clears_the_snapshot_again_once_committed(ai_settings, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        _enter(ai_settings, claude_effort="high")
        # another worker reads the row before the save is committed, and shares what it saw
        caches["shared"].set(llm._cache_key(), {"claude_effort": ""}, llm.SNAPSHOT_SECONDS)
    assert caches["shared"].get(llm._cache_key()) is None and llm.snapshot().claude_effort == "high"


def test_without_the_table_the_defaults_apply_and_nothing_is_kept(db, monkeypatch):
    """A release's first page before its migration ran: the defaults, the caller's transaction intact."""
    monkeypatch.setattr(llm, "_pinned", False)
    llm.forget()
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE assistant_aisettings RENAME TO assistant_aisettings_later")
    try:
        assert llm.snapshot() == llm.Snapshot()
        assert llm._memo is None and caches["shared"].get(llm._cache_key()) is None
        assert AIUsage.objects.count() == 0  # the transaction still answers
    finally:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE assistant_aisettings_later RENAME TO assistant_aisettings")


def test_nothing_reads_the_settings_at_import_or_start_up():
    """Django set up, the provider layer and the admin imported, the URLs loaded, with no database at all."""
    code = (
        "import django; django.setup()\n"
        "import neurodb.assistant.llm as llm, neurodb.assistant.admin, django.urls\n"
        "django.urls.get_resolver().url_patterns\n"
        "assert llm._memo is None\n"
        "print('ok')\n"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_ENV": "test",
        "DJANGO_SECRET_KEY": "test-only",
        "DATABASE_URL": "postgres://nobody@127.0.0.1:9/none",
    }
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr[-2000:]


# ------------------------------------------------------------------------------------ models, effort
def test_models_follow_the_provider_in_use(ai_settings):
    assert llm.model_for("gpt-4.1", llm.OPENAI) == "gpt-4.1"
    assert llm.model_for("", llm.OPENAI) == settings.AI_ASSISTANT_MODEL
    assert llm.model_for("claude-opus-5", llm.OPENAI) == settings.AI_ASSISTANT_MODEL
    assert llm.model_for("gpt-5.5", llm.CLAUDE) == settings.CLAUDE_MODEL  # an OpenAI model is not used
    assert llm.model_for("claude-sonnet-5-5", llm.CLAUDE) == "claude-sonnet-5-5"
    _enter(ai_settings, claude_model="claude-sonnet-5-5", openai_model="gpt-4.1")
    # a value equal to its provider's settings default is "the default": the choice here replaces it
    assert llm.model_for(settings.CLAUDE_MODEL, llm.CLAUDE) == "claude-sonnet-5-5"
    assert llm.model_for(settings.AI_ASSISTANT_MODEL, llm.OPENAI) == "gpt-4.1"
    assert llm.model_for("gpt-5.4", llm.OPENAI) == "gpt-5.4"
    assert llm.provider_of("Claude-Opus-5") == llm.CLAUDE and llm.provider_of("gpt-5.5") == llm.OPENAI


def test_the_claude_default_is_the_settings_one():
    assert settings.CLAUDE_MODEL == "claude-opus-5-5" and llm.default_model(llm.CLAUDE) == "claude-opus-5-5"


def test_effort_per_provider(ai_settings):
    assert [llm.effort_for(e, llm.CLAUDE) for e in ("none", "minimal", "low", "xhigh", "max")] == [
        "low", "low", "low", "xhigh", "max"
    ]  # fmt: skip
    assert llm.effort_for("none", llm.OPENAI) == "none"  # unchanged on OpenAI
    assert llm.default_effort(llm.CLAUDE) == settings.CLAUDE_EFFORT
    assert llm.default_effort(llm.OPENAI) == settings.AI_ASSISTANT_EFFORT
    _enter(ai_settings, claude_effort="high", openai_effort="low")
    assert llm.default_effort(llm.CLAUDE) == "high" and llm.default_effort(llm.OPENAI) == "low"


def test_names_per_provider():
    assert (
        llm.names(llm.CLAUDE).label == "Claude (Anthropic)"
        and llm.names(llm.CLAUDE).host == "api.anthropic.com"
    )
    assert llm.names(llm.OPENAI).writer == "ChatGPT (OpenAI's GPT model)" and llm.names().api == "OpenAI API"
    assert llm.names_of("claude-opus-5").vendor == "Anthropic" and llm.names_of("gpt-5.5").vendor == "OpenAI"


def test_a_request_builds_with_its_defaults():
    request = llm.Request()
    assert request.sampling == () and request.user == "" and request.as_message and request.schema is None
    assert hash(llm.Request(sampling=(("temperature", 0.3),)))  # frozen and hashable


# ----------------------------------------------------------------------------- the price table
def test_every_model_offered_is_priced_and_takes_the_request_sent():
    for model, entry in usage.CLAUDE_PRICES.items():
        assert model.startswith("claude-") and entry["write"] == entry["input"] * Decimal("1.25")
        assert all(
            isinstance(entry[k], Decimal) and entry[k] > 0 for k in ("input", "cached", "write", "output")
        )
    choices = {m for m, e in usage.CLAUDE_PRICES.items() if e["choice"]}
    fallbacks = {m for m, e in usage.CLAUDE_PRICES.items() if e["fallbacks"]}
    assert fallbacks <= choices and settings.CLAUDE_MODEL in fallbacks
    assert claude_backend.offers_fallbacks("claude-opus-5-5") and not claude_backend.offers_fallbacks(
        "claude-haiku-5-5"
    )
    assert not claude_backend.offers_fallbacks("claude-opus-5-5-preview")  # exact ids only
    default = usage.CLAUDE_PRICES["claude-opus-5-5"]
    assert [default[k] for k in ("input", "cached", "write", "output")] == [
        Decimal("4.00"), Decimal("0.20"), Decimal("5.0000"), Decimal("20.00")
    ]  # fmt: skip
    assert usage.FEATURES[usage.TEST] == "Connection test"


# ----------------------------------------------------------------------------- Claude's schemas
def _schemas():
    from neurodb.cpd import extraction
    from neurodb.fmm.ai import ap_review, ap_summary, checks, sections
    from neurodb.knowledge import indexing, periodic, review, review_paragraph
    from neurodb.review import services
    from neurodb.watch import explain, investigate

    return {
        "fmm_brief": sections.schema(sections.of(None)),
        "fmm_rule_check": checks.SCHEMA,
        "fmm_ap_review": ap_review.SCHEMA,
        "fmm_ap_summary": ap_summary.SCHEMA,
        "doc_review_findings": review.FINDINGS_SCHEMA,
        "doc_review_summary": review.STATEMENTS_SCHEMA,
        "doc_review_enrichment": review.ACTIONS_SCHEMA,
        "doc_review_theme": review_paragraph.SCHEMA,
        "cpd_results_framework": extraction.SCHEMA,
        "decisions": services.DECISION_SCHEMA,
        "morning_note": explain.SCHEMA,
        "report_figures": periodic.FIGURES_SCHEMA,
        "knowledge_document": indexing.SUMMARY_SCHEMA,
        "watch_look_up": investigate.FORMAT["schema"],
    }


def _walk(node, where=""):
    """(where, schema) for every schema node, the property names walked as names."""
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk(item, f"{where}[{i}]")
    elif isinstance(node, dict):
        yield where, node
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for name, sub in value.items():
                    yield from _walk(sub, f"{where}.{name}")
            elif key in ("items", "anyOf", "allOf"):
                yield from _walk(value, f"{where}.{key}")


@pytest.mark.parametrize("name", list(_schemas()))
def test_every_schema_is_one_claude_takes(name):
    from tests.assistant.claude_params import SCHEMA_KEYWORDS

    original = _schemas()[name]
    built = claude_backend.schema(original)
    assert built == claude_backend.schema(json.loads(json.dumps(original)))  # the same bytes every time
    assert json.dumps(built, sort_keys=True) == json.dumps(claude_backend.schema(original), sort_keys=True)
    for where, node in _walk(built):
        assert set(node) <= SCHEMA_KEYWORDS, (where, set(node) - SCHEMA_KEYWORDS)
        assert not isinstance(node.get("type"), list), where
        if node.get("type") == "object":
            assert node["additionalProperties"] is False, where
    moved = {where: node["description"] for where, node in _walk(built) if "{" in node.get("description", "")}
    assert moved == {
        "decisions": {".decisions": "{maxItems: 5}"},
        "morning_note": {".sentences": "{maxItems: 6}"},
    }.get(name, {})
    assert original == _schemas()[name]  # the caller's own constant is left as it is


def test_the_schema_keeps_a_property_named_like_a_keyword_and_turns_type_lists_into_any_of():
    from neurodb.cpd import extraction
    from neurodb.fmm.ai import ap_summary

    assert "pattern" in claude_backend.schema(ap_summary.SCHEMA)["properties"]
    unions = [node for _, node in _walk(claude_backend.schema(extraction.SCHEMA)) if "anyOf" in node]
    assert unions and all(len(node["anyOf"]) == 2 and {"type": "null"} in node["anyOf"] for node in unions)


# ------------------------------------------------------------------------------------------ usage
def test_usage_is_counted_the_same_way_for_both_providers():
    openai_usage = SimpleNamespace(
        input_tokens=150, input_tokens_details=SimpleNamespace(cached_tokens=50), output_tokens=20
    )
    response = SimpleNamespace(output_text="Fine.", status="completed", usage=openai_usage)
    result = openai_backend.Connection.read(None, response, llm.Request())
    assert result.usage == llm.Usage(
        input_tokens=100, cache_read_tokens=50, cache_write_tokens=0, output_tokens=20
    )
    assert usage.split(result.usage) == (100, 50, 20)  # what the ledger reads

    plain = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=10, cache_read_input_tokens=300, cache_creation_input_tokens=40, output_tokens=7
        )
    )
    assert claude_backend.usage(plain) == llm.Usage(
        input_tokens=50, cache_read_tokens=300, cache_write_tokens=40, output_tokens=7
    )
    attempts = [
        SimpleNamespace(type="message", input_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=5, output_tokens=3),
        SimpleNamespace(type="compaction", input_tokens=999, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=99),
        SimpleNamespace(type="fallback_message", input_tokens=20, cache_read_input_tokens=1, cache_creation_input_tokens=0, output_tokens=4),
    ]  # fmt: skip
    fell_back = SimpleNamespace(usage=SimpleNamespace(input_tokens=20, cache_read_input_tokens=1, cache_creation_input_tokens=0,
                                                      output_tokens=4, iterations=attempts))  # fmt: skip
    assert claude_backend.usage(fell_back) == llm.Usage(
        input_tokens=35, cache_read_tokens=1, cache_write_tokens=5, output_tokens=7
    )
    assert claude_backend.fell_back(fell_back) and not claude_backend.fell_back(plain)
    assert claude_backend.usage(SimpleNamespace()) == llm.Usage()


# -------------------------------------------------------------------------------- OpenAI's rules
class _FakeAPI:
    def __init__(self, responses=()):
        self.options = []
        self.sent = []
        self._responses = list(responses)
        self.responses = SimpleNamespace(create=self._create)

    def with_options(self, **options):
        self.options.append(options)
        return self

    def _create(self, **params):
        self.sent.append(params)
        return self._responses.pop(0)


def test_the_openai_request_is_the_one_each_feature_sends(monkeypatch):
    api = _FakeAPI()
    monkeypatch.setattr(agent, "client", lambda: api)
    conn = llm.connect(timeout=60, retries=1)
    assert conn.provider == llm.OPENAI and api.options == [{"timeout": 60, "max_retries": 1}]
    # the daily review's narration: a bare input string, no schema, no cache key
    assert conn.render(
        llm.Request(system="Narrate.", user="{}", as_message=False, max_output_tokens=600)
    ) == {
        "model": settings.AI_ASSISTANT_MODEL,
        "instructions": "Narrate.",
        "input": "{}",
        "reasoning": {"effort": "low"},
        "max_output_tokens": 600,
        "store": False,
    }
    # a structured call with a cache key, a person, sampling and a PDF part
    request = llm.Request(
        system="Check.",
        user=(llm.Pdf("a.pdf", b"%PDF"), llm.Text("Read it.")),
        schema={"type": "object"},
        schema_name="fmm_rule_check",
        cache_key="neurodb-fmm-check-R1",
        user_id="b" * 64,
        effort="medium",
        model="gpt-4.1",
        sampling=(("temperature", 0.3),),
    )
    assert conn.render(request) == {
        "model": "gpt-4.1",
        "instructions": "Check.",
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_file",
                        "filename": "a.pdf",
                        "file_data": "data:application/pdf;base64,JVBERg==",
                    },
                    {"type": "input_text", "text": "Read it."},
                ],
            }
        ],
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 4000,
        "store": False,
        "prompt_cache_key": "neurodb-fmm-check-R1",
        "text": {
            "format": {
                "type": "json_schema",
                "name": "fmm_rule_check",
                "strict": True,
                "schema": {"type": "object"},
            }
        },
        "safety_identifier": "b" * 64,
        "temperature": 0.3,
    }
    assert conn.render(llm.Request(user="Hello"))["input"] == [{"role": "user", "content": "Hello"}]
    assert "instructions" not in conn.render(llm.Request(user="Hello"))
    assert llm.connect().api is api and api.options == [
        {"timeout": 60, "max_retries": 1}
    ]  # none given: as built


def _response(status="completed", text="{}", reason=None, refusal_part=False, error=None):
    content = [SimpleNamespace(type="refusal", refusal="No.")] if refusal_part else []
    return SimpleNamespace(
        output_text=text,
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output=[SimpleNamespace(type="message", content=content)],
        error=SimpleNamespace(code=error, message="x") if error else None,
        usage=None,
    )


@pytest.mark.parametrize(
    "response, stop, reason",
    [
        (_response(), "ok", ""),
        (_response("incomplete", reason="max_output_tokens"), "max_tokens", "max_output_tokens"),
        (_response("incomplete", reason="content_filter"), "refused", "content_filter"),
        (_response("incomplete", reason="something_new"), "incomplete", "something_new"),
        (_response(refusal_part=True), "refused", "refusal"),
        (
            _response("incomplete", reason="max_output_tokens", refusal_part=True),
            "max_tokens",
            "max_output_tokens",
        ),
        (_response("failed", error="server_error"), "failed", "server_error"),
        (_response("failed", error="server_error", refusal_part=True), "refused", "refusal"),
        (SimpleNamespace(output_text='{"a": 1}'), "ok", ""),  # a partial fake
    ],
)
def test_an_openai_response_is_read_as_the_features_read_it(response, stop, reason):
    result = openai_backend.Connection.read(None, response, llm.Request(schema={"type": "object"}))
    assert (result.stop, result.reason) == (stop, reason)
    # the text is always output_text, whatever the status (a cut narration is still shown)
    assert result.text == response.output_text and result.parsed == json.loads(response.output_text)
    assert result.served_by == "" and result.ledger_model == result.model == settings.AI_ASSISTANT_MODEL


def test_an_openai_text_that_is_not_json_is_not_parsed():
    assert (
        openai_backend.Connection.read(None, _response(text="not json"), llm.Request(schema={})).parsed
        is None
    )
    assert (
        openai_backend.Connection.read(None, _response(text="{}"), llm.Request()).parsed is None
    )  # no schema


def _openai_error(cls, status, code=None, message="x", param=None):
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    body = {"code": code, "message": message, "param": param, "type": "invalid_request_error"}
    return cls(message, response=httpx2.Response(status, request=request), body=body)


def test_the_openai_predicates_are_todays():
    """The rules moved into openai_backend give what the features' own rules give today."""
    from neurodb.fmm.ai import insights
    from neurodb.watch import budget

    cases = [
        _openai_error(openai.RateLimitError, 429, "insufficient_quota", "You exceeded your current quota"),
        _openai_error(openai.RateLimitError, 429, "rate_limit_exceeded", "Slow down"),
        _openai_error(
            openai.BadRequestError, 400, "unsupported_value", "reasoning.effort 'none' is not supported"
        ),
        _openai_error(openai.BadRequestError, 400, None, "Unsupported value", param="reasoning.effort"),
        _openai_error(openai.BadRequestError, 400, None, "temperature is not supported", param="temperature"),
        llm.ServiceError("insufficient_quota", "No credit"),
        llm.ServiceError("server_error"),
        ValueError("unexpected"),
    ]
    for exc in cases:
        assert openai_backend.is_quota(exc) == budget.is_quota(exc) == llm.is_quota(exc), exc
        assert (
            openai_backend.effort_refused(exc) == insights._effort_refused(exc) == llm.effort_refused(exc)
        ), exc
    assert [
        openai_backend.policy_block(llm.ServiceError(code)) for code in sorted(openai_backend.POLICY_BLOCKS)
    ] == [True] * 4
    assert openai_backend.policy_block(_openai_error(openai.BadRequestError, 400, "cyber_policy"))
    assert not openai_backend.policy_block(llm.ServiceError("server_error"))
    assert not openai_backend.policy_block(ValueError("cyber_policy"))
    assert agent.ServiceError is llm.ServiceError and agent.AssistantUnavailable is llm.AssistantUnavailable


# ---------------------------------------------------------------------------- Claude's failures
def _claude_error(cls, status, type_=None, message="x"):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    body = error_body(type_, message) if type_ else None
    return cls(message, response=httpx2.Response(status, request=request), body=body)


REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    "exc, message",
    [
        (_claude_error(anthropic.AuthenticationError, 401, "authentication_error"), llm.KEY_REJECTED),
        (_claude_error(anthropic.PermissionDeniedError, 403, "permission_error"), llm.NOT_ALLOWED),
        (_claude_error(anthropic.NotFoundError, 404, "not_found_error"), llm.NO_MODEL),
        (_claude_error(anthropic.RateLimitError, 429, "rate_limit_error"), llm.BUSY),
        (_claude_error(anthropic.OverloadedError, 529, "overloaded_error"), llm.BUSY),
        (_claude_error(anthropic.RequestTooLargeError, 413, "request_too_large"), llm.NOT_PROCESSED),
        (_claude_error(anthropic.BadRequestError, 400, "invalid_request_error"), llm.NOT_PROCESSED),
        (_claude_error(anthropic.UnprocessableEntityError, 422, "invalid_request_error"), llm.NOT_PROCESSED),
        (anthropic.APITimeoutError(request=REQUEST), llm.LATE),
        (_claude_error(anthropic.DeadlineExceededError, 504, "timeout_error"), llm.LATE),
        (anthropic.APIConnectionError(request=REQUEST), llm.UNREACHABLE),
        (_claude_error(anthropic.InternalServerError, 500, "api_error"), llm.UNAVAILABLE),
        (_claude_error(anthropic.ServiceUnavailableError, 503, "api_error"), llm.UNAVAILABLE),
        (_claude_error(anthropic.APIStatusError, 402, "billing_error"), llm.NO_CREDIT),
        (_claude_error(anthropic.APIStatusError, 402), llm.NO_CREDIT),
        (
            _claude_error(
                anthropic.BadRequestError, 400, "invalid_request_error", "Your credit balance is too low"
            ),
            llm.NO_CREDIT,
        ),
        # an error event in the stream: status 200, the API's type
        (_claude_error(anthropic.APIStatusError, 200, "overloaded_error"), llm.BUSY),
        (_claude_error(anthropic.APIStatusError, 200, "rate_limit_error"), llm.BUSY),
        (_claude_error(anthropic.APIStatusError, 200, "timeout_error"), llm.LATE),
        (_claude_error(anthropic.APIStatusError, 200, "billing_error"), llm.NO_CREDIT),
        (_claude_error(anthropic.APIStatusError, 200, "api_error"), llm.UNAVAILABLE),
        (anthropic.APIError("x", REQUEST, body=None), llm.UNAVAILABLE),
        (llm.ServiceError("timeout"), llm.LATE),
        (llm.ServiceError("stream_cut"), llm.UNFINISHED),
        (llm.ServiceError("tool_input"), llm.UNFINISHED),
        (llm.AssistantUnavailable("off"), llm.NOT_SET_UP),
        (ValueError("unexpected"), None),
    ],
)
def test_claude_failures_become_the_known_messages(exc, message):
    assert llm.friendly(exc) == message


def test_the_claude_credit_rule():
    assert llm.is_quota(_claude_error(anthropic.APIStatusError, 402, "billing_error"))
    assert llm.is_quota(_claude_error(anthropic.APIStatusError, 200, "billing_error"))
    assert llm.is_quota(
        _claude_error(
            anthropic.BadRequestError, 400, "invalid_request_error", "Your credit balance is too low"
        )
    )
    assert not llm.is_quota(
        _claude_error(anthropic.RateLimitError, 429, "rate_limit_error", "quota of requests")
    )
    assert not llm.is_quota(_claude_error(anthropic.OverloadedError, 529, "overloaded_error"))
    assert llm.effort_refused(
        _claude_error(anthropic.BadRequestError, 400, "invalid_request_error", "output_config.effort: bad")
    )
    assert not llm.effort_refused(
        _claude_error(anthropic.BadRequestError, 400, "invalid_request_error", "max_tokens")
    )
    beta = f"Unexpected value(s) `{claude_backend.FALLBACK_BETA}` for the `anthropic-beta` header."
    assert claude_backend.beta_refused(
        _claude_error(anthropic.BadRequestError, 400, "invalid_request_error", beta)
    )


# ------------------------------------------------------------------------------------- the keys
def test_keys_are_sealed_and_open_only_with_the_sites_secret_key():
    token = keys.seal(KEY)
    assert KEY not in token and keys.unseal(token) == KEY and keys.last4(KEY) == KEY[-4:]
    assert keys.unseal("") is None and keys.unseal("not-a-token") is None
    old = settings.SECRET_KEY
    with override_settings(SECRET_KEY="another-secret-key-for-this-test-only-0123456789abcdef"):
        assert keys.unseal(token) is None  # the secret key changed: enter the key again
    with override_settings(
        SECRET_KEY="the-new-secret-key-for-this-test-only-0123456789abcdef", SECRET_KEY_FALLBACKS=[old]
    ):
        assert keys.unseal(token) == KEY  # the old secret kept in the fallbacks still opens it
        resealed = keys.reseal(token)
        assert resealed != token and keys.unseal(resealed) == KEY
        with override_settings(SECRET_KEY_FALLBACKS=[]):
            assert keys.unseal(resealed) == KEY  # sealed again under the new secret alone
    assert keys.reseal("") is None and keys.reseal("not-a-token") is None


# ---------------------------------------------------------------------------------- no secret leaves
def test_no_key_entered_leaves_in_a_log_a_row_or_the_cache(claude, ai_settings, caplog):
    caplog.set_level(logging.DEBUG)
    _enter(ai_settings, provider="claude", anthropic_key=KEY)
    server = claude(
        turn([text("Fine.")]),
        http_error(401, error_body("authentication_error", "invalid x-api-key")),
        message_json([text("OK")]),
    )
    with override_settings(**OFF, ANTHROPIC_API_KEY=""):
        assert llm.complete(llm.Request(system="x", user="y", cache_key="k")).text == "Fine."
        with pytest.raises(anthropic.AuthenticationError) as raised:
            llm.connect(retries=0).complete(llm.Request(system="x", user="y"))
        assert KEY not in llm.friendly(raised.value) and KEY not in str(raised.value)
        check = llm.test_connection()
        AISettings.objects.filter(pk=ai_settings.pk).update(check_result=check.text)
    assert [r["headers"]["x-api-key"] for r in server.requests] == [KEY] * 3  # the key entered was used
    assert KEY not in caplog.text and KEY not in "".join(str(r.args) for r in caplog.records)
    assert KEY not in AISettings.objects.get().check_result
    assert not [row for row in AIUsage.objects.values_list() if KEY in str(row)]
    kept = caches["shared"]._cache  # LocMem: the pickled values
    assert not [k for k, value in kept.items() if KEY.encode() in value or KEY in str(pickle.loads(value))]


def test_an_answer_on_a_key_entered_in_ai_settings_shows_it_nowhere(
    claude, ai_settings, client_viewer, caplog
):
    caplog.set_level(logging.DEBUG)
    _enter(ai_settings, provider="claude", anthropic_key=KEY)
    server = claude(
        turn([tool_use("toolu_1", "data_freshness", "{}")], stop_reason="tool_use"),
        http_error(401, error_body("authentication_error", "invalid x-api-key")),
    )
    with override_settings(**OFF, ANTHROPIC_API_KEY=""):
        response = client_viewer.post(reverse("assistant:stream"), {"question": "Is the data up to date?"})
        events = b"".join(response.streaming_content).decode()
    assert [r["headers"]["x-api-key"] for r in server.requests] == [KEY] * 2  # the key entered was used
    assert llm.KEY_REJECTED in events and KEY not in events  # no event the browser reads holds it
    assert KEY not in caplog.text and KEY not in "".join(str(r.args) for r in caplog.records)
    assert not [row for row in AssistantQuestion.objects.values_list() if KEY in str(row)]
    assert not [row for row in AIUsage.objects.values_list() if KEY in str(row)]


# ---------------------------------------------------------------------------------- the ledger per model
def test_each_model_books_the_tokens_it_ran(claude, client_viewer):
    """A refusal fallback in round 2 of 3: the model asked for books rounds 1 and 3 and the declined attempt,
    at its prices; the fallback's model books its own attempt. Each call counts on the model that wrote its
    message, and the two rows add up to the answer's tokens."""
    attempts = [iteration("message", 40, 8), iteration("fallback_message", 70, 30, model="claude-opus-5")]
    claude(
        turn(
            [text("Checking."), tool_use("toolu_1", "data_freshness", "{}")],
            stop_reason="tool_use",
            usage=anthropic_mock.usage(100, 20, write=500),
        ),
        turn(
            [fallback(MODEL, "claude-opus-5"), tool_use("toolu_2", "data_freshness", "{}")],
            stop_reason="tool_use",
            usage=anthropic_mock.usage(70, 30, iterations=attempts),
        ),
        turn([text("Fresh.")], usage=anthropic_mock.usage(150, 10, read=500)),
    )
    response = client_viewer.post(reverse("assistant:stream"), {"question": "Is the data up to date?"})
    assert b'"type": "done"' in b"".join(response.streaming_content)

    rows = {row.model: row for row in AIUsage.objects.filter(feature=usage.ASK)}
    assert sorted(rows) == ["claude-opus-5", MODEL]
    asked, rescued = rows[MODEL], rows["claude-opus-5"]
    assert (asked.calls, asked.input_tokens, asked.cached_tokens, asked.output_tokens) == (2, 790, 500, 38)
    assert (rescued.calls, rescued.input_tokens, rescued.cached_tokens, rescued.output_tokens) == (
        1,
        70,
        0,
        30,
    )
    question = AssistantQuestion.objects.get()
    assert asked.input_tokens + rescued.input_tokens == question.input_tokens == 860
    assert asked.cached_tokens + rescued.cached_tokens == question.cache_read_tokens == 500
    assert asked.output_tokens + rescued.output_tokens == question.output_tokens == 68
    assert question.model == MODEL  # the last round was the model asked for's again


def test_an_answer_without_tokens_per_model_is_one_row_of_the_model_asked_for(db):
    outcome = agent.Outcome(input_tokens=90, cache_read_tokens=230, output_tokens=42, calls=2)  # OpenAI's
    llm.record(usage.ASK, "gpt-5.5", outcome)
    row = AIUsage.objects.get()
    assert (row.model, row.calls, row.input_tokens, row.cached_tokens, row.output_tokens) == (
        "gpt-5.5",
        2,
        90,
        230,
        42,
    )


def test_the_sdk_traces_keep_to_metadata_whatever_the_environment_says(claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE", "content")
    api = claude_backend.client()
    assert api.open_telemetry["traces"]["content_mode"] == "metadata_only"
    assert api.api_key == "test-key-not-real" and api.max_retries == 2
    assert api.timeout.read == 120.0 and api.timeout.connect == 10.0
    assert (
        logging.getLogger("anthropic").getEffectiveLevel() == logging.INFO
    )  # LOGGING: never its DEBUG bodies
