"""ActivityInfo sign-in: the account's email and password (basic auth, to the ActivityInfo host only),
else the API token."""

import base64

import responses

from neurodb.integrations.activityinfo.client import ActivityInfoClient

BASE = "https://ai.test"


def _auth(call) -> str | None:
    return call.request.headers.get("Authorization")


@responses.activate
def test_the_email_and_password_are_used_instead_of_the_token(settings):
    settings.ACTIVITYINFO_TOKEN = "old-token"
    settings.ACTIVITYINFO_USERNAME, settings.ACTIVITYINFO_PASSWORD = "im@example.org", "pass word:1"
    responses.get(f"{BASE}/resources/databases/db1", json={"id": "db1"})
    client = ActivityInfoClient(BASE)
    assert client.auth == "password"
    client.get_database("db1")
    expected = "Basic " + base64.b64encode(b"im@example.org:pass word:1").decode()
    assert _auth(responses.calls[0]) == expected


@responses.activate
def test_the_password_never_goes_to_another_host(settings):
    settings.ACTIVITYINFO_USERNAME, settings.ACTIVITYINFO_PASSWORD = "im@example.org", "secret"
    responses.get("https://files.example.net/export.zip", body=b"PK")
    client = ActivityInfoClient(BASE)
    client.session.get("https://files.example.net/export.zip")
    assert _auth(responses.calls[0]) is None


@responses.activate
def test_without_a_password_the_token_is_sent_as_before(settings):
    settings.ACTIVITYINFO_TOKEN = "abc123"
    settings.ACTIVITYINFO_USERNAME, settings.ACTIVITYINFO_PASSWORD = "im@example.org", ""
    responses.get(f"{BASE}/resources/databases/db1", json={"id": "db1"})
    client = ActivityInfoClient(BASE)
    assert client.auth == "token"
    client.get_database("db1")
    assert _auth(responses.calls[0]) == "abc123"


def test_an_unresolved_key_vault_reference_is_not_a_password(monkeypatch):
    import importlib

    import config.settings as conf

    monkeypatch.setenv("ACTIVITYINFO_USERNAME", "im@example.org")
    monkeypatch.setenv("ACTIVITYINFO_PASSWORD", "@Microsoft.KeyVault(SecretUri=https://kv/secrets/x)")
    try:
        reloaded = importlib.reload(conf)
        assert reloaded.ACTIVITYINFO_USERNAME == reloaded.ACTIVITYINFO_PASSWORD == ""
    finally:
        monkeypatch.delenv("ACTIVITYINFO_USERNAME")
        monkeypatch.delenv("ACTIVITYINFO_PASSWORD")
        importlib.reload(conf)
