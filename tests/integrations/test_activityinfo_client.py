import pytest
import responses

from neurodb.integrations.activityinfo.client import ActivityInfoClient
from neurodb.integrations.http import IntegrationError, make_session

BASE = "https://ai.test"


def make_client(sleeps: list | None = None) -> ActivityInfoClient:
    return ActivityInfoClient(
        BASE,
        "ai-token",
        session=make_session("ai-token", backoff=0),
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_auth_header_is_bare_token_like_v2_token_auth():
    client = ActivityInfoClient(BASE, "ai-token")
    assert client.session.headers["Authorization"] == "ai-token"


def test_settings_are_the_default_source(settings):
    settings.ACTIVITYINFO_BASE_URL = "https://settings.test/"
    settings.ACTIVITYINFO_TOKEN = "from-settings"
    client = ActivityInfoClient()
    assert client.base_url == "https://settings.test"
    assert client.session.headers["Authorization"] == "from-settings"


@responses.activate
def test_export_flow(database):
    responses.post(f"{BASE}/resources/jobs", json={"id": "job9"})
    responses.get(f"{BASE}/resources/jobs/job9", json={"state": "STARTED"})
    responses.get(
        f"{BASE}/resources/jobs/job9", json={"state": "COMPLETED", "result": {"downloadUrl": "/dl/x.txt"}}
    )
    responses.get(f"{BASE}/dl/x.txt", body=b"Database\x1fValue\nx\x1f1\n")
    sleeps: list = []
    assert make_client(sleeps).export_database(database) == b"Database\x1fValue\nx\x1f1\n"
    payload = responses.calls[0].request.body
    assert b'"databaseId": "ck2yrizmo2"' in payload
    assert b'"folderId": "cfolder16"' in payload
    assert b"LEFT(Month,4) == '2025'" in payload
    assert b'"format": "LONG"' in payload
    assert sleeps == [2.0]


@responses.activate
def test_poll_is_bounded():
    responses.get(f"{BASE}/resources/jobs/j", json={"state": "started"})
    client = make_client()
    with pytest.raises(IntegrationError, match="not completed after 3 attempts"):
        client.poll_export("j", max_attempts=3, interval=0)
    assert len(responses.calls) == 3


@responses.activate
def test_failed_job_raises():
    responses.get(f"{BASE}/resources/jobs/j", json={"state": "failed", "error": {"message": "nope"}})
    with pytest.raises(IntegrationError, match="nope"):
        make_client().poll_export("j")
