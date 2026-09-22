import pytest
import responses

from neurodb.integrations.etools.client import EToolsClient, token_header
from neurodb.integrations.http import IntegrationError, make_session

BASE = "https://etools.test"


def make_client() -> EToolsClient:
    return EToolsClient(BASE, "abc", session=make_session("Token abc", backoff=0))


def test_token_header_adds_scheme_once():
    assert token_header("abc") == "Token abc"
    assert token_header("Token abc") == "Token abc"
    assert token_header("") == ""


def test_default_auth_from_settings(settings):
    settings.ETOOLS_TOKEN = "xyz"
    assert EToolsClient(BASE).session.headers["Authorization"] == "Token xyz"


@responses.activate
def test_list_plain_json_list():
    responses.get(f"{BASE}/api/v2/partners/", json=[{"id": 1}, {"id": 2}])
    assert [i["id"] for i in make_client().list("/api/v2/partners/", page_size=None)] == [1, 2]
    assert "page_size" not in responses.calls[0].request.url


@responses.activate
def test_list_follows_drf_next_links():
    responses.get(
        f"{BASE}/api/audit/engagements/?page_size=1000",
        json={"results": [{"id": 1}], "next": f"{BASE}/api/audit/engagements/?page=2&page_size=1000"},
    )
    responses.get(f"{BASE}/api/audit/engagements/?page=2&page_size=1000", json={"results": [{"id": 2}], "next": None})
    assert [i["id"] for i in make_client().list("/api/audit/engagements/")] == [1, 2]


@responses.activate
def test_list_t2f_pages_from_page_one_until_404():
    responses.get(f"{BASE}/api/t2f/travels/?page_size=1000", json={"data": [{"id": 1}]})
    responses.get(f"{BASE}/api/t2f/travels/?page_size=1000&page=2", json={"data": [{"id": 2}]})
    responses.get(f"{BASE}/api/t2f/travels/?page_size=1000&page=3", status=404)
    assert [i["id"] for i in make_client().list("/api/t2f/travels/")] == [1, 2]
    assert len(responses.calls) == 3


@responses.activate
def test_list_t2f_stops_at_page_count_or_empty_page():
    responses.get(f"{BASE}/api/t2f/travels/?page_size=2", json={"data": [{"id": 1}], "page_count": 2})
    responses.get(f"{BASE}/api/t2f/travels/?page_size=2&page=2", json={"data": [{"id": 2}], "page_count": 2})
    assert [i["id"] for i in make_client().list("/api/t2f/travels/", page_size=2)] == [1, 2]
    responses.get(f"{BASE}/api/t2f/x/?page_size=2", json={"data": []})
    assert list(make_client().list("/api/t2f/x/", page_size=2)) == []


@responses.activate
def test_first_page_404_is_an_error():
    responses.get(f"{BASE}/api/v2/nothing/", status=404)
    with pytest.raises(IntegrationError):
        list(make_client().list("/api/v2/nothing/", page_size=None))


@responses.activate
def test_get_detail():
    responses.get(f"{BASE}/api/v2/partners/7/", json={"id": 7})
    assert make_client().get("/api/v2/partners/7/") == {"id": 7}
