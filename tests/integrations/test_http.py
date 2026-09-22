import pytest
import requests
import responses

from neurodb.integrations.http import IntegrationError, get_json, make_session, post_json

URL = "https://api.test/resources/thing"


def test_session_carries_auth_and_timeout(settings):
    session = make_session("Token secret-value")
    assert session.headers["Authorization"] == "Token secret-value"
    assert session.headers["Accept"] == "application/json"
    assert session.timeout == settings.INTEGRATION_TIMEOUT_SECONDS
    assert session.get_adapter("https://x").max_retries.total == 5


@responses.activate(registry=responses.registries.OrderedRegistry)
def test_get_json_retries_on_5xx_then_succeeds():
    responses.get(URL, status=503)
    responses.get(URL, status=502)
    responses.get(URL, json={"ok": True})
    session = make_session("Token secret-value", backoff=0)
    assert get_json(session, URL, page=2) == {"ok": True}
    assert len(responses.calls) == 3
    assert responses.calls[-1].request.url.endswith("?page=2")


@responses.activate
def test_get_json_raises_with_status_and_without_token():
    responses.get(URL, status=403, body='{"detail": "Invalid token secret-value"}')
    session = make_session("Token secret-value", backoff=0)
    with pytest.raises(IntegrationError) as info:
        get_json(session, URL)
    assert info.value.status == 403
    assert "403" in str(info.value)
    assert "Token secret-value" not in str(info.value)


@responses.activate
def test_non_json_body_is_an_integration_error():
    responses.get(URL, body="<html>", status=200)
    with pytest.raises(IntegrationError):
        get_json(make_session("", backoff=0), URL)


@responses.activate
def test_post_json():
    responses.post(URL, json={"id": "job1"})
    assert post_json(make_session("", backoff=0), URL, {"type": "x"}) == {"id": "job1"}
    assert responses.calls[0].request.body == b'{"type": "x"}'


@responses.activate
def test_connection_error_is_wrapped():
    responses.get(URL, body=requests.ConnectionError("boom"))
    with pytest.raises(IntegrationError) as info:
        get_json(make_session("", backoff=0), URL)
    assert info.value.status is None
