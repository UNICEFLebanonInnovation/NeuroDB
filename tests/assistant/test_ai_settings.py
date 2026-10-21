"""Admin → Data and sync → AI settings: the provider, the write-only keys, the models and default effort,
the refusal fallback and Test the connection. No command line: every change is made here, applies within
seconds, and a key entered here is never shown again."""

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.assistant import llm
from neurodb.assistant.llm import keys
from neurodb.assistant.models import AISettings, AIUsage
from tests.assistant import openai_mock
from tests.assistant.anthropic_mock import error_body, http_error, message_json, text
from tests.assistant.openai_mock import MockResponsesServer, completed, message

KEY = "kx-test-" + "5" * 32  # entered in the tests (not a real key, and in no real key's form)
OTHER = "kx-other-" + "6" * 31
CHANGE = "admin:assistant_aisettings_change"


@pytest.fixture
def page(client, admin_user, ai_settings):
    client.force_login(admin_user)
    return client


def _url(row=None):
    return reverse(CHANGE, args=[(row or AISettings.load()).pk])


def _post(client, **fields):
    data = {
        "provider": "auto",
        "claude_model": "",
        "claude_effort": "",
        "claude_fallbacks": "on",
        "openai_model": "",
        "openai_effort": "",
        "anthropic_key": "",
        "openai_key": "",
        "_save": "Save",
        **fields,
    }
    data = {k: v for k, v in data.items() if v is not None}
    return client.post(_url(), data)


def _errors(response):
    form = response.context["adminform"].form
    return {name: [str(e) for e in errors] for name, errors in form.errors.items()}


def test_the_page_is_in_data_and_sync_and_opens_the_one_row(page):
    home = page.get(reverse("admin:index")).content.decode()
    assert "AI settings" in home
    listed = page.get(reverse("admin:assistant_aisettings_changelist"))
    assert listed.status_code == 302 and listed["Location"] == _url()
    html = page.get(_url()).content.decode()
    assert "In use now" in html and "Test the connection" in html
    assert "ChatGPT (OpenAI), model gpt-5.5 (AI_ASSISTANT_MODEL)" in html  # nothing entered: as today
    assert "Claude Opus 5.5 (recommended)" in html and "Claude Haiku 5.5" in html  # the price table's choices
    assert "Claude Opus 4.8" not in html  # priced, not offered: its request shape is not this one
    assert AISettings.objects.count() == 1
    assert page.get(reverse("admin:assistant_aisettings_add")).status_code == 403


def test_a_key_is_write_only(page):
    response = _post(page, anthropic_key=f"  {KEY}\n")
    assert response.status_code == 302, _errors(response)
    row = AISettings.objects.get()
    assert keys.unseal(row.anthropic_key_sealed) == KEY and KEY not in row.anthropic_key_sealed
    assert row.anthropic_key_last4 == KEY[-4:] and row.anthropic_key_set_at is not None
    html = page.get(_url()).content.decode()
    assert KEY not in html and row.anthropic_key_sealed not in html
    assert f"A key ending …{KEY[-4:]} was entered on" in html and 'autocomplete="new-password"' in html
    assert "Claude (Anthropic), model claude-opus-5-5 (CLAUDE_MODEL)" in html  # Automatic: Claude now
    assert "chosen automatically, as an Anthropic key is entered here." in html
    log = LogEntry.objects.get()
    assert KEY not in log.change_message and "Anthropic API key" in log.get_change_message()
    # left empty, the key stays; a save seals it again with the current secret key
    assert _post(page, claude_effort="high").status_code == 302
    row.refresh_from_db()
    assert keys.unseal(row.anthropic_key_sealed) == KEY and row.claude_effort == "high"


def test_a_save_applies_at_once(page):
    with override_settings(AI_ASSISTANT_ENABLED=False, AI_ASSISTANT_SWITCH=True):
        assert not llm.enabled() and llm.provider() == llm.OPENAI
        assert _post(page, anthropic_key=KEY).status_code == 302
        assert llm.enabled() and llm.provider() == llm.CLAUDE  # the save cleared the snapshot
        assert _post(page, provider="openai", openai_key=OTHER).status_code == 302
        assert llm.provider() == llm.OPENAI and llm.openai_key() == OTHER


@pytest.mark.parametrize("value", ["short-key", "kx-test with a space inside it, 40 chars"])
def test_a_key_that_is_not_one_is_refused_without_repeating_it(page, value):
    response = _post(page, openai_key=value)
    assert response.status_code == 200
    assert _errors(response) == {
        "openai_key": ["This is not an API key: a key is at least 20 characters long, with no spaces."]
    }
    assert value not in response.content.decode() and not AISettings.objects.get().openai_key_sealed


def test_a_key_set_by_the_hosting_team_comes_first(page):
    with override_settings(ANTHROPIC_API_KEY="env-anthropic-key-not-real-wxyz"):
        html = page.get(_url()).content.decode()
        assert (
            "Set by the hosting team (ANTHROPIC_API_KEY, ending …wxyz): it comes before a key entered here."
            in html
        )
        assert "env-anthropic-key-not-real" not in html
        assert "chosen automatically, as an Anthropic key is set by the hosting team." in html
        # the input is disabled: what is posted for it is ignored
        assert _post(page, anthropic_key=KEY).status_code == 302
        assert not AISettings.objects.get().anthropic_key_sealed
    with override_settings(AI_PROVIDER="openai"):
        html = page.get(_url()).content.decode()
        assert "set by the hosting team (AI_PROVIDER=openai): it comes before the choice here." in html


def test_choosing_a_provider_needs_its_key(page):
    response = _post(page, provider="claude")
    assert _errors(response) == {"anthropic_key": ["Enter the Anthropic API key first, or choose Automatic."]}
    response = _post(page, provider="openai")
    assert _errors(response) == {"openai_key": ["Enter the OpenAI API key first, or choose Automatic."]}
    assert _post(page, provider="claude", anthropic_key=KEY).status_code == 302  # in the same form
    assert AISettings.objects.get().provider == "claude" and llm.provider() == llm.CLAUDE
    with override_settings(OPENAI_API_KEY="env-openai-key-not-real"):
        assert _post(page, provider="openai").status_code == 302  # the hosting team's key counts


def test_removing_a_key(page):
    assert _post(page, provider="claude", anthropic_key=KEY).status_code == 302
    # the only key of the provider chosen cannot go
    response = _post(page, provider="claude", remove_anthropic_key="on")
    assert _errors(response) == {
        "remove_anthropic_key": ["This is the only Anthropic key: choose Automatic or OpenAI first."]
    }
    assert _post(page, provider="auto", remove_anthropic_key="on").status_code == 302
    row = AISettings.objects.get()
    assert (row.anthropic_key_sealed, row.anthropic_key_last4, row.anthropic_key_set_at) == ("", "", None)
    assert llm.provider() == llm.OPENAI
    assert (
        "No key yet. Create one in the Claude Console (platform.claude.com, API keys)"
        in page.get(_url()).content.decode()
    )


def test_a_key_that_can_no_longer_be_read(page):
    assert _post(page, anthropic_key=KEY).status_code == 302
    with override_settings(SECRET_KEY="another-secret-key-for-this-test-only-0123456789abcdef"):
        page.force_login(User.objects.get(username="admin"))  # the session was signed with the old secret
        html = page.get(_url()).content.decode()
        assert (
            "The key entered here can no longer be read: the site's secret key changed. Enter the key again."
            in html
        )
        assert "ChatGPT (OpenAI)" in html and llm.provider() == llm.OPENAI
        # a save keeps it as it is (it may open again with the old secret key) until a new one is entered
        assert _post(page).status_code == 302
        assert AISettings.objects.get().anthropic_key_last4 == KEY[-4:]


def test_a_save_seals_the_keys_again_with_the_new_secret_key(page, settings):
    old = settings.SECRET_KEY
    assert _post(page, openai_key=KEY).status_code == 302
    sealed = AISettings.objects.get().openai_key_sealed
    settings.SECRET_KEY, settings.SECRET_KEY_FALLBACKS = (
        "a-rotated-secret-key-for-this-test-0123456789abcdef",
        [old],
    )
    page.force_login(User.objects.get(username="admin"))  # the session was signed with the old secret key
    assert _post(page).status_code == 302
    resealed = AISettings.objects.get().openai_key_sealed
    assert resealed != sealed
    settings.SECRET_KEY_FALLBACKS = []
    assert keys.unseal(resealed) == KEY


def test_the_model_choices(page):
    response = _post(page, claude_model="claude-3-haiku")  # not one Claude's request shape fits
    assert "claude_model" in _errors(response)
    response = _post(page, openai_model="claude-opus-5")
    assert _errors(response) == {
        "openai_model": ["This is a Claude model: choose it under Claude (Anthropic)."]
    }
    assert (
        _post(page, claude_model="claude-sonnet-5-5", openai_model="gpt-4.1", openai_effort="low").status_code
        == 302
    )
    assert llm.model_for("", llm.CLAUDE) == "claude-sonnet-5-5" and llm.model_for("", llm.OPENAI) == "gpt-4.1"
    assert llm.default_effort(llm.OPENAI) == "low"
    html = page.get(_url()).content.decode()
    assert "ChatGPT (OpenAI), model gpt-4.1 (chosen here), default effort low (chosen here)" in html


def test_the_keys_are_kept_out_of_error_reports(page):
    response = _post(page, anthropic_key=KEY)
    assert set(response.wsgi_request.sensitive_post_parameters) == {"anthropic_key", "openai_key"}


# ------------------------------------------------------------------------------ test the connection
def _test(client, **headers):
    url = reverse("admin:assistant_aisettings_test_connection", args=[AISettings.load().pk])
    return client.post(url, {"_form_submitted": "on"}, **headers)


def test_the_connection_test_on_claude(page, claude):
    server = claude(message_json([text("OK")]))
    response = _test(page)
    assert response.status_code == 302 and response["Location"] == _url()
    row = AISettings.objects.get()
    assert row.checked_at is not None and row.check_result.startswith(
        "Claude (Anthropic) answered with claude-opus-5-5"
    )
    shown = [str(m) for m in response.wsgi_request._messages]
    assert shown == [row.check_result]
    assert server.requests[0]["body"]["messages"] == [{"role": "user", "content": "Reply with the word OK."}]
    assert AIUsage.objects.get().feature == "test"
    assert row.check_result in page.get(_url()).content.decode()


def test_a_failed_connection_test_is_a_warning(page, claude):
    claude(http_error(401, error_body("authentication_error", "invalid x-api-key")))
    response = _test(page, HTTP_HX_REQUEST="true")  # the dialog posts with HTMX
    assert response.status_code == 204 and response["HX-Redirect"] == _url()
    assert AISettings.objects.get().check_result == llm.KEY_REJECTED
    assert [(m.level_tag, str(m)) for m in response.wsgi_request._messages] == [("warning", llm.KEY_REJECTED)]


def test_the_connection_test_on_openai(page, monkeypatch):
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    server = MockResponsesServer([completed([message("msg_1", "OK")])]).start()
    monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
    try:
        with override_settings(AI_ASSISTANT_ENABLED=True, OPENAI_API_KEY="test-key-not-real"):
            assert _test(page).status_code == 302
    finally:
        server.stop()
    body = server.requests[0]["body"]
    assert body == {
        "model": "gpt-5.5",
        "input": "Reply with the word OK.",  # no NeuroDB data
        "max_output_tokens": 16,
        "store": False,
        "reasoning": {"effort": "medium"},  # the default effort (AI_ASSISTANT_EFFORT), as the features send
    }
    assert AISettings.objects.get().check_result.startswith("ChatGPT (OpenAI) answered with gpt-5.5 in ")


def test_the_connection_test_says_when_the_model_does_not_take_the_default_effort(page, monkeypatch):
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    said = "Unsupported value: 'reasoning.effort' does not support 'minimal' with this model."
    server = MockResponsesServer([openai_mock.http_error(400, openai_mock.api_error_body(said))]).start()
    monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
    try:
        with override_settings(AI_ASSISTANT_ENABLED=True, OPENAI_API_KEY="test-key-not-real"):
            assert _post(page, openai_effort="minimal").status_code == 302
            assert _test(page).status_code == 302
    finally:
        server.stop()
    assert server.requests[0]["body"]["reasoning"] == {"effort": "minimal"}  # what Ask NeuroDB would send
    assert AISettings.objects.get().check_result == (
        "The model gpt-5.5 does not take the default effort minimal: choose another default effort here and "
        f"test again. The API said: {said}"
    )


def test_the_connection_test_with_the_ai_off(page, claude):
    server = claude()
    for provider in ("claude", "openai"):
        with override_settings(AI_PROVIDER=provider, AI_ASSISTANT_ENABLED=False, AI_ASSISTANT_SWITCH=False):
            assert _test(page).status_code == 302
        # what "In use now" says above it, not the message for the people using the AI
        assert AISettings.objects.get().check_result == (
            "The hosting team switched the AI off (AI_ASSISTANT_ENABLED=false): nothing was sent."
        )
    assert server.requests == [] and not AIUsage.objects.exists()


def test_the_connection_test_with_no_key_says_which_key_is_missing(page, claude):
    server = claude()
    with override_settings(ANTHROPIC_API_KEY=""):
        assert _test(page).status_code == 302
    assert AISettings.objects.get().check_result == (
        "Claude (Anthropic) has no API key yet, so nothing was sent: enter one here, or ask the hosting team "
        "to set ANTHROPIC_API_KEY."
    )
    assert server.requests == [] and not AIUsage.objects.exists()


def test_only_administrators_change_ai_settings(client, viewer, ai_settings, roles):
    client.force_login(viewer)
    assert client.get(_url()).status_code == 302  # not staff: the admin's sign-in page
    viewer.is_staff = True
    viewer.save()
    assert client.get(reverse("admin:assistant_aisettings_changelist")).status_code == 403
    assert _test(client).status_code == 403
    viewer.user_permissions.add(Permission.objects.get(codename="view_aisettings"))
    assert client.get(_url()).status_code == 200
    assert _test(client).status_code == 403  # the test needs the change permission
    assert AISettings.objects.get().checked_at is None


def test_the_test_result_never_holds_the_key(page, claude, ai_settings):
    claude(http_error(400, error_body("invalid_request_error", "max_tokens: must be at least 1")))
    with override_settings(ANTHROPIC_API_KEY=""):  # the key entered here, not the hosting team's
        assert _post(page, provider="claude", anthropic_key=KEY).status_code == 302
        _test(page)
    row = AISettings.objects.get()
    assert row.checked_at <= timezone.now() and KEY not in row.check_result
    assert row.check_result == f"{llm.NOT_PROCESSED} The API said: max_tokens: must be at least 1"


# --------------------------------------------------------------------------- which model each feature runs on
def test_the_page_lists_which_model_each_feature_runs_on(page, settings):
    html = page.get(_url()).content.decode()
    assert "Which model each AI feature runs on" in html and "Runs on now" in html
    assert "NeuroDB Watch" in html and f"WATCH_MODEL: {settings.WATCH_MODEL}" in html
    assert "is an OpenAI model" not in html  # OpenAI in use: every feature runs on what it asks for
    assert _post(page, anthropic_key=KEY).status_code == 302  # Automatic: Claude now
    html = page.get(_url()).content.decode()
    assert f"{settings.WATCH_MODEL} is an OpenAI model: {settings.CLAUDE_MODEL} runs instead." in html
    assert "AI_ASSISTANT_MODEL names a Claude model" not in html
    settings.AI_ASSISTANT_MODEL = "claude-opus-5"  # left over from an earlier release
    html = page.get(_url()).content.decode()
    assert (
        "AI_ASSISTANT_MODEL names a Claude model; OpenAI calls would fail; choose an OpenAI model here."
        in html
    )


# ------------------------------------------------------------------------------------ Needs attention
def _lines():
    from neurodb.web import health

    return {line.key: line for line in health.warnings() if line.key.startswith("ai_")}


def test_needs_attention_says_when_the_provider_chosen_has_no_key(ai_settings):
    assert _lines() == {}  # Automatic with no key: OpenAI as today, nothing to say
    with override_settings(AI_PROVIDER="claude", ANTHROPIC_API_KEY=""):
        line = _lines()["ai_no_key"]
        assert line.text == (
            "Claude (Anthropic) is chosen for the AI features (AI_PROVIDER=claude) but no Anthropic API key "
            "is entered: the AI features are off."
        )
        assert line.url == reverse("admin:assistant_aisettings_changelist")
    AISettings.objects.filter(pk=ai_settings.pk).update(provider="claude")  # chosen there before a key
    llm.forget()
    assert "(AI settings)" in _lines()["ai_no_key"].text
    with override_settings(AI_ASSISTANT_SWITCH=False):  # the hosting team switched the AI off
        assert _lines() == {}


def test_needs_attention_says_when_a_key_entered_can_no_longer_be_read(page):
    assert _post(page, anthropic_key=KEY).status_code == 302
    assert _lines() == {}
    with override_settings(SECRET_KEY="another-secret-key-for-this-test-only-0123456789abcdef"):
        line = _lines()["ai_key_unreadable:anthropic"]
        assert line.text == (
            "The Anthropic API key entered in AI settings can no longer be read, as the site's secret key "
            "changed: enter it again."
        )
        assert KEY[-4:] not in line.text and llm.provider() == llm.OPENAI  # Automatic moved: it says so
        with override_settings(ANTHROPIC_API_KEY="test-key-not-real"):  # the hosting team's key comes first
            assert "ai_key_unreadable:anthropic" not in _lines()


def test_needs_attention_says_when_a_key_vault_reference_did_not_resolve(ai_settings):
    with override_settings(AI_KEY_VAULT_UNRESOLVED=("ANTHROPIC_API_KEY",)):
        assert _lines()["ai_key_vault:anthropic"].text == (
            "ANTHROPIC_API_KEY is a Key Vault reference that did not resolve: the hosting team should check "
            "the secret anthropic-api-key, or remove ANTHROPIC_API_KEY if the key is not kept in Key Vault."
        )
    with override_settings(AI_KEY_VAULT_UNRESOLVED=("OPENAI_API_KEY",)):
        assert list(_lines()) == ["ai_key_vault:openai"]
