"""NeuroDB Watch's background look-up: a few critical open points a day, looked into with Ask NeuroDB's
read-only tools through the real openai SDK against a local stand-in for the Responses API (answer()
streams). Only allow-listed tools run, read-only and filtered; an answer is kept only when grounded;
the switches, the caps and the AI pause stop it."""

import datetime
import json

import pytest
from django.test import override_settings
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.assistant import tools
from neurodb.assistant.models import AIUsage
from neurodb.watch import investigate, redact
from neurodb.watch.models import DetectorSetting, WatchItem, WatchReceipt, WatchRequest, WatchState
from tests.assistant.openai_mock import (
    MockResponsesServer,
    api_error_body,
    function_call,
    http_error,
    message,
    turn,
    usage,
)

pytestmark = pytest.mark.django_db

PD = "LEB/PCA2026001/PD2026001"
KEY = "review:reports_overdue:" + PD
ON = {
    "AI_ASSISTANT_ENABLED": True,
    "OPENAI_API_KEY": "test-key-not-real",
    "AI_ASSISTANT_MODEL": "gpt-5.5",
    "AI_ASSISTANT_EFFORT": "medium",
    "WATCH_ENABLED": True,
    "WATCH_AI": True,
    "WATCH_MODEL": "gpt-5.5",
    "WATCH_INVESTIGATE_ENABLED": True,
    "WATCH_INVESTIGATE_PER_DAY": 3,
    "WATCH_DAILY_TOKEN_CAP": 300_000,
    "WATCH_MAX_MODEL_CALLS_PER_DAY": 24,
    "AI_DAILY_TOKEN_SOFT_CAP": 3_000_000,
}
GROUNDED = f"{PD} has a budget of 125,000 USD, and its last progress report was due on 15 Sep 2026."
BUDGET_ONLY = f"{PD} has a budget of 125,000 USD."
NEVER_OFFERED = {
    "etools_query",
    "etools_record",
    "etools_search",
    "etools_datasets",
    "read_knowledge",
    "search_knowledge",
    "makani_wellbeing",
    "management_brief",
    "make_chart",
}


@pytest.fixture(autouse=True)
def _switched_on():
    with override_settings(**ON):
        yield


@pytest.fixture
def openai_api(monkeypatch):
    """Point the SDK (agent.client() builds a fresh client) at a local stand-in playing back replies."""
    for var in ("NO_PROXY", "no_proxy"):  # reach the local stand-in directly even behind a proxy
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    for var in ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_CUSTOM_HEADERS"):
        monkeypatch.delenv(var, raising=False)
    servers = []

    def install(*replies):
        server = MockResponsesServer(list(replies)).start()
        servers.append(server)
        monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
        return server

    yield install
    for server in servers:
        server.stop()


@pytest.fixture
def planted(db):
    """A person NeuroDB knows, by name and email."""
    return User.objects.create(
        username="jplanted", first_name="Jana", last_name="Plantedone", email="jana.planted@example.org"
    )


@pytest.fixture
def pd_tool(monkeypatch):
    """programme_details answers with figures, a person, an email, a link and a model-written summary."""
    calls = []

    def details(number):
        calls.append(number)
        return {
            "number": number,
            "partner": "Partner A",
            "budget_usd": 125000,
            "last_report_due": "2026-09-15",
            "unicef_focal_points": ["Jana Plantedone"],
            "status_text": "Ask Jana Plantedone at jana.planted@example.org, see https://evil.example/x",
            "summary": "A summary the AI wrote earlier",
            "url": "/programme/1/",
        }

    monkeypatch.setitem(tools.TOOLS, "programme_details", (details, *tools.TOOLS["programme_details"][1:]))
    return calls


def _today() -> datetime.date:
    return timezone.localdate()


def _item(key: str = KEY, *, severity="critical", first_seen=None, changed=None, **fields) -> WatchItem:
    today = _today()
    first_seen = first_seen or today
    values = {
        "key": key,
        "detector": "review",
        "kind": WatchItem.Kind.CONCERN,
        "severity": severity,
        "title": f"Progress reports overdue: {PD} (Partner A)",
        "detail": "Written by code; never sent",
        "url": "/programme/1/",
        "entity_kind": "programme_document",
        "entity_key": PD,
        "etools_sections": ["Education"],
        "evidence": {
            "source": "eTools progress reports",
            "source_job": "etools_datamart",
            "records": [{"label": "QPR 3", "date": "2026-09-30", "value": "overdue", "url": "/programme/1/"}],
            "numbers": {"overdue_reports": 2, "unspent": 48000},
        },
        "first_seen_on": first_seen,
        "last_seen_on": today,
        "changed_on": changed or first_seen,
    }
    values.update(fields)
    return WatchItem.objects.create(**values)


def _found(text: str, numbers: list, item_id: str = "msg_2") -> list[dict]:
    """The model's last turn: the strict JSON answer."""
    answer = json.dumps({"what_i_found": text, "numbers": numbers})
    return turn([message(item_id, answer, phase="final_answer")], usage=usage(3000, 400, cached=1000))


def _looks_up_the_pd(call_id: str = "call_1") -> list[dict]:
    return turn(
        [function_call(f"fc_{call_id}", call_id, "programme_details", json.dumps({"number": PD}))],
        usage=usage(2500, 150, cached=1000),
    )


def _outputs(request: dict) -> dict:
    return {
        item["call_id"]: json.loads(item["output"])
        for item in request["body"]["input"]
        if item.get("type") == "function_call_output"
    }


# ---------------------------------------------------------------------------- a look-up
def test_a_critical_item_is_looked_into_and_kept_when_grounded(openai_api, planted, pd_tool):
    item = _item()
    server = openai_api(_looks_up_the_pd(), _found(GROUNDED, [125000]))

    summary = investigate.run(_today())

    assert summary.kept == [KEY] and summary.tried == [KEY] and summary.skipped == ""
    assert summary.calls == 2 and summary.tokens == 3000 + 400 + 2500 + 150
    item.refresh_from_db()
    assert item.looked_up["text"] == GROUNDED and item.looked_up["numbers"] == [125000]
    assert item.looked_up["tools"] == ["programme_details"] and item.looked_up["kept"] is True
    assert item.looked_up["on"] == _today().isoformat() and item.looked_up["reason"] == ""
    assert investigate.shown(item) == {"label": investigate.LABEL, "text": GROUNDED, "on": _today()}
    # only the look-up was written: never the item's title, severity, due date or story
    assert (
        item.severity == "critical" and item.title.startswith("Progress reports overdue") and not item.story
    )
    # no usefulness gate: it runs before anyone reacted to anything
    assert not WatchReceipt.objects.exists()

    first, second = server.requests
    body = first["body"]
    assert body["model"] == "gpt-5.5" and body["store"] is False
    assert (
        body["reasoning"] == {"effort": "low"} and body["max_output_tokens"] == investigate.MAX_OUTPUT_TOKENS
    )
    assert body["prompt_cache_key"] == "neurodb-watch-investigate"
    assert "safety_identifier" not in body  # no person asked for it
    offered = [t["name"] for t in body["tools"]]
    assert set(offered) == set(investigate.TOOLS) and not set(offered) & NEVER_OFFERED
    assert offered == [name for name in tools.TOOLS if name in investigate.TOOLS]  # the registry's order
    assert body["text"] == {"format": investigate.FORMAT} and investigate.FORMAT["strict"] is True
    assert body["instructions"].endswith(investigate.INSTRUCTIONS.strip())
    # the point goes in as the allow-list writes it: no link, no detail
    point = body["input"][0]["content"]
    assert point.startswith(investigate.QUESTION) and KEY in point
    assert "/programme/1/" not in point and "Written by code" not in point
    # the tool ran, and the AI read its result through the allow-list
    assert pd_tool == [PD]
    result = _outputs(second)["call_1"]
    assert result["budget_usd"] == 125000 and result["last_report_due"] == "2026-09-15"
    assert not {"unicef_focal_points", "summary", "url"} & set(result)
    sent = json.dumps([r["body"] for r in server.requests])
    for withheld in ("Plantedone", "jana.planted@example.org", "evil.example", "/programme/1/", "AI wrote"):
        assert withheld not in sent
    # its tokens count in the watch's daily cap
    row = AIUsage.objects.get(feature="watch")
    assert row.model == "gpt-5.5" and row.calls == 2
    assert (row.input_tokens, row.cached_tokens, row.output_tokens) == (3500, 2000, 550)


def test_a_tool_outside_the_list_is_an_input_error(openai_api, monkeypatch):
    ran = []

    def query(**kwargs):
        ran.append(kwargs)
        return {"rows": [{"value": 125000}]}

    monkeypatch.setitem(tools.TOOLS, "etools_query", (query, *tools.TOOLS["etools_query"][1:]))
    item = _item()
    server = openai_api(
        turn([function_call("fc_1", "call_1", "etools_query", json.dumps({"dataset": "pd"}))]),
        _found(BUDGET_ONLY, [125000]),
    )

    summary = investigate.run(_today())

    error = _outputs(server.requests[1])["call_1"]
    assert error["error"] == "Unknown tool etools_query." and ran == []
    # nothing was looked up, so 125,000 is not grounded: the answer is dropped
    item.refresh_from_db()
    assert summary.dropped == {KEY: "number"} and item.looked_up["kept"] is False
    assert item.looked_up["text"] == "" and item.looked_up["tools"] == []
    assert investigate.shown(item) is None


def test_a_write_inside_a_tool_fails_in_the_read_only_transaction(openai_api, monkeypatch):
    def writes(number):
        WatchRequest.objects.create(reason="written by a tool")
        return {"number": number}

    monkeypatch.setitem(tools.TOOLS, "programme_details", (writes, *tools.TOOLS["programme_details"][1:]))
    item = _item()
    server = openai_api(_looks_up_the_pd(), _found("", []))

    summary = investigate.run(_today())

    assert _outputs(server.requests[1]) == {"call_1": {"error": "The lookup failed on the server."}}
    assert not WatchRequest.objects.exists()
    item.refresh_from_db()  # the watch still writes its own table afterwards
    assert summary.dropped == {KEY: investigate.NOTHING_FOUND} and item.looked_up["reason"] == "nothing_found"


@pytest.mark.parametrize(
    "text, numbers, kept",
    [
        (GROUNDED, [125000], True),
        (GROUNDED.replace("125,000", "130,000"), [130000], False),  # a figure no lookup gave
        (GROUNDED, [125000, 777777], False),  # a listed number no lookup gave
        (GROUNDED.replace("15 Sep", "20 Sep"), [125000], False),  # a date no lookup gave
    ],
    ids=["grounded", "invented-figure", "invented-listed-number", "invented-date"],
)
def test_an_ungrounded_answer_is_discarded(openai_api, pd_tool, text, numbers, kept):
    item = _item()
    openai_api(_looks_up_the_pd(), _found(text, numbers))

    summary = investigate.run(_today())

    item.refresh_from_db()
    assert item.looked_up["kept"] is kept and bool(item.looked_up["text"]) is kept
    assert (summary.kept == [KEY]) is kept
    if not kept:
        assert item.looked_up["numbers"] == [] and summary.dropped[KEY] in ("number", "date")


@pytest.mark.parametrize(
    "answer, reason",
    [
        ({"what_i_found": f"Jana Plantedone follows {PD}.", "numbers": []}, "person"),
        ({"what_i_found": f"Write to jana@example.org about {PD}.", "numbers": []}, "email"),
        ({"what_i_found": f"See /programme/1/ for {PD}.", "numbers": []}, "link"),
        ({"what_i_found": f"See https://etools.example/pd for {PD}.", "numbers": []}, "link"),
        ({"what_i_found": "The budget is **125,000** USD.", "numbers": [125000]}, "markup"),
        ({"what_i_found": "LEB/PCA2026001/PD2026099 is late too.", "numbers": []}, "reference"),
        ({"what_i_found": "x" * 401, "numbers": []}, "too_long"),
        ({"what_i_found": "", "numbers": []}, "nothing_found"),
        ({"found": "a different shape"}, "unreadable"),
        ("not JSON at all", "unreadable"),
    ],
)
def test_judge_keeps_no_names_links_or_unknown_references(answer, reason):
    item = _item()
    raw = answer if isinstance(answer, str) else json.dumps(answer)
    returned = [{"budget_usd": 125000, "last_report_due": "2026-09-15"}]

    text, numbers, why = investigate.judge(raw, item, returned, _today(), {"jana plantedone"})

    assert (text, numbers, why) == ("", [], reason)


def test_judge_keeps_numbers_from_the_item_and_small_counts():
    item = _item()
    text = f"{PD} still has 48,000 USD unspent and 2 reports overdue, with 3 other points open."

    kept = investigate.judge(
        json.dumps({"what_i_found": text, "numbers": [48000, 2, 3]}), item, [], _today(), []
    )

    assert kept == (text, [48000, 2, 3], "")


def test_every_look_up_tool_reads_real_data_without_writing(hub):
    """Each allowed tool, on the hub's records, inside the read-only transaction the look-up uses: none
    needs to write, and its result passes the allow-list as JSON."""
    from neurodb.graph.models import Entity

    partner = Entity.objects.get(kind="partner", name=hub.amel.name)
    samples = {
        "find_anything": {"text": "Amel"},
        "entity_profile": {"kind": "partner", "key": partner.key},
        "connected": {"kind": "partner", "key": partner.key, "to_kind": "programme_document"},
        "programme_details": {"number": hub.pd.number},
        "partner_details": {"partner_id": hub.amel.pk},
        "partner_reporting": {"partner": "Amel"},
        "pd_indicator_progress": {"partner": "Amel"},
        "funds_overview": {"partner": "Amel"},
        "assurance_overview": {"partner": "Amel"},
        "indicator_forecasts": {},
        "whats_new": {},
        "daily_review": {},
        "data_freshness": {},
    }
    assert set(samples) == set(investigate.TOOLS)
    for name, args in samples.items():
        with tools.read_only():
            result = tools.run(name, args, only=investigate.TOOLS)
        clean = redact.for_tool(result)
        assert json.loads(json.dumps(clean)) == clean, name
        assert "url" not in json.dumps(clean), name


# ---------------------------------------------------------------------------- when it runs
@pytest.mark.parametrize(
    "switch", ["WATCH_INVESTIGATE_ENABLED", "WATCH_AI", "AI_ASSISTANT_ENABLED", "WATCH_ENABLED"]
)
def test_nothing_runs_when_switched_off(openai_api, switch):
    _item()
    server = openai_api()

    with override_settings(**{switch: False}):
        summary = investigate.run(_today())

    assert summary.skipped == investigate.OFF and summary.tried == [] and server.requests == []


@pytest.mark.parametrize(
    "spent, reason",
    [
        ({"feature": "watch", "input_tokens": 260_000}, "budget"),  # the watch's tokens: no room for one more
        ({"feature": "watch", "calls": 21}, "budget"),  # the watch's calls: no room for 4 more rounds
        ({"feature": "ask", "input_tokens": 2_380_000}, "budget"),  # every feature: near 80% of the soft cap
        (None, "paused"),
    ],
    ids=["watch-tokens", "watch-calls", "soft-cap", "paused"],
)
def test_the_budget_and_the_pause_stop_it(openai_api, spent, reason):
    _item()
    server = openai_api()
    if spent is None:
        WatchState.objects.create(ai_paused_until=timezone.now() + datetime.timedelta(hours=1))
    else:
        AIUsage.objects.create(day=_today(), model="gpt-5.5", **spent)

    summary = investigate.run(_today())

    assert summary.skipped == reason and server.requests == []
    assert investigate.allowed() == (False, reason)


def test_the_budget_has_room_below_the_caps():
    AIUsage.objects.create(day=_today(), feature="watch", model="gpt-5.5", input_tokens=200_000, calls=12)
    AIUsage.objects.create(day=_today() - datetime.timedelta(days=1), feature="watch", input_tokens=10**7)
    assert investigate.allowed() == (True, "")


def test_critical_items_are_chosen_best_first():
    today = _today()
    earlier = today - datetime.timedelta(days=10)
    for detector, mode in (("review", "on"), ("action_points_due", "on"), ("report_due_soon", "trial")):
        DetectorSetting.objects.create(detector=detector, mode=mode)
    DetectorSetting.objects.create(detector="pd_ending", mode="off")
    old_on = _item("a:old-on", first_seen=earlier, detector="action_points_due", due_date=today)
    old_trial = _item("b:old-trial", first_seen=earlier, detector="report_due_soon", due_date=today)
    old_on_later = _item("c:old-on-later", first_seen=earlier, due_date=today + datetime.timedelta(days=5))
    worse = _item("d:worse", first_seen=earlier, changed=today, detector="report_due_soon")
    new = _item("e:new", detector="report_due_soon")
    changed_since = _item(
        "f:changed-since",
        first_seen=earlier,
        changed=today - datetime.timedelta(days=1),
        looked_up={"on": (today - datetime.timedelta(days=3)).isoformat(), "kept": True, "text": "x"},
    )
    # never looked into: not critical, not open, a check that is off, never sent to the AI, unchanged
    _item("g:warning", severity="warning")
    _item("h:closed", state=WatchItem.State.CLOSED)
    _item("i:check-off", detector="pd_ending")
    _item("system:stale:etools_datamart", kind=WatchItem.Kind.SYSTEM, scope=WatchItem.Scope.ADMINS)
    _item("makani:followup:1", detector="makani_followup")
    _item("donor_account:7")
    _item(
        "j:unchanged",
        first_seen=earlier,
        looked_up={"on": (today - datetime.timedelta(days=1)).isoformat(), "kept": False, "text": ""},
    )
    _item("k:today", looked_up={"on": today.isoformat(), "kept": True, "text": "x"})

    chosen = [item.key for item in investigate.candidates(today)]

    expected = [new, worse, old_on, old_on_later, changed_since, old_trial]
    assert chosen == [item.key for item in expected]
    assert all(not redact.refused(item) for item in investigate.candidates(today))


def test_at_most_the_day_s_look_ups(openai_api, pd_tool):
    today = _today()
    for n in range(3):
        _item(f"done:{n}", looked_up={"on": today.isoformat(), "kept": True, "text": "x"})
    _item()
    server = openai_api()

    summary = investigate.run(today)

    assert summary.skipped == investigate.DONE_TODAY and server.requests == [] and summary.tried == []


@override_settings(WATCH_INVESTIGATE_PER_DAY=2)
def test_the_look_ups_stop_at_the_day_s_number(openai_api, pd_tool):
    for n in range(3):
        _item(f"{KEY}:{n}")
    openai_api(_looks_up_the_pd("a"), _found(GROUNDED, [125000]), _looks_up_the_pd("b"), _found("", []))

    summary = investigate.run(_today())

    assert len(summary.tried) == 2 and summary.skipped == investigate.DONE_TODAY
    assert investigate.done_on(_today()) == 2


def test_a_stop_request_ends_the_run(openai_api):
    _item()
    server = openai_api()
    summary = investigate.run(_today(), stop=lambda: True)
    assert summary.skipped == investigate.STOPPED and server.requests == []


def test_a_look_up_stops_after_four_rounds(openai_api, pd_tool):
    item = _item()
    server = openai_api(*(_looks_up_the_pd(f"call_{n}") for n in range(5)))

    summary = investigate.run(_today())

    assert len(server.requests) == investigate.MAX_ROUNDS == 4 and summary.calls == 4
    item.refresh_from_db()
    assert item.looked_up["kept"] is False and item.looked_up["reason"] == "failed: too many lookups"
    assert summary.dropped == {KEY: "failed: too many lookups"}
    assert AIUsage.objects.get(feature="watch").calls == 4  # every call counts in the cap


# ---------------------------------------------------------------------------- the circuit breaker
def test_the_credit_running_out_pauses_the_ai(openai_api):
    item = _item()
    quota = api_error_body(
        "You exceeded your current quota.", type_="insufficient_quota", code="insufficient_quota"
    )
    server = openai_api(http_error(429, quota))

    summary = investigate.run(_today())

    assert summary.skipped == investigate.QUOTA and summary.dropped == {KEY: investigate.ERROR}
    state = WatchState.get()
    hours = (state.ai_paused_until - timezone.now()).total_seconds() / 3600
    assert 5.9 < hours <= 6 and state.ai_pause_reason == investigate.QUOTA_REASON
    item.refresh_from_db()
    assert item.looked_up == {}  # tried again another day
    assert AIUsage.objects.filter(feature="watch").count() == 0  # no answer, no tokens
    # while paused, nothing is sent
    assert investigate.run(_today()).skipped == investigate.PAUSED and len(server.requests) == 1


def test_three_failures_in_a_row_pause_the_ai(openai_api):
    _item()
    failed = http_error(500, api_error_body("The server had an error.", type_="server_error"))
    server = openai_api(failed, failed, failed)

    for _ in range(2):
        assert investigate.run(_today()).skipped == investigate.ERROR
        assert not WatchState.get().ai_paused_until
    assert investigate.run(_today()).skipped == investigate.ERROR

    state = WatchState.get()
    assert state.ai_paused_until > timezone.now() and state.ai_pause_reason == investigate.ERRORS_REASON
    assert state.consecutive_ai_errors == 0 and len(server.requests) == 3


def test_an_answer_resets_the_failures(openai_api, pd_tool):
    _item()
    WatchState.objects.create(consecutive_ai_errors=2)
    openai_api(_looks_up_the_pd(), _found("", []))

    investigate.run(_today())

    assert WatchState.get().consecutive_ai_errors == 0 and not WatchState.get().ai_paused_until


# ---------------------------------------------------------------------------- what is shown and sent
def test_what_was_found_is_never_sent_to_the_ai_again():
    item = _item(
        looked_up={"on": _today().isoformat(), "kept": True, "text": "Found: 125,000 USD", "numbers": []}
    )

    sent = json.dumps(redact.for_model(item))

    assert "Found: 125,000" not in sent and "looked_up" not in sent
    assert investigate.shown(item)["text"] == "Found: 125,000 USD"
    assert investigate.shown(_item("other", looked_up={"kept": False, "text": ""})) is None
