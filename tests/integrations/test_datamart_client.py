import base64

import pytest
import requests
import responses

from neurodb.integrations.etools.datamart import DatamartClient, DatamartNotConfigured, configured
from neurodb.integrations.http import IntegrationError, make_session

BASE = "https://datamart.test"
USER, PASSWORD = "svc-user@example.org", "not-a-real-secret"


def make_client(**kwargs) -> DatamartClient:
    kwargs.setdefault("country", "Lebanon")
    return DatamartClient(BASE, USER, PASSWORD, session=make_session("", backoff=0), **kwargs)


def expected_auth() -> str:
    return "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


@responses.activate
def test_list_sends_basic_auth_country_filter_and_follows_pages():
    url = f"{BASE}/api/latest/datamart/partners/"
    responses.get(
        url,
        match=[responses.matchers.query_param_matcher({"page_size": "2", "country_name": "Lebanon"})],
        json={
            "count": 3,
            "next": f"{url}?page=2&page_size=2&country_name=Lebanon",
            "results": [{"id": 1}, {"id": 2}],
        },
    )
    responses.get(
        url,
        match=[
            responses.matchers.query_param_matcher({"page": "2", "page_size": "2", "country_name": "Lebanon"})
        ],
        json={"count": 3, "next": None, "results": [{"id": 3}]},
    )
    items = list(make_client(page_size=2).list("partners"))
    assert [i["id"] for i in items] == [1, 2, 3]
    assert all(call.request.headers["Authorization"] == expected_auth() for call in responses.calls)


@responses.activate
def test_nested_dataset_path_and_version():
    responses.get(f"{BASE}/api/v1/datamart/partners/assessment/", json={"results": [], "next": None})
    assert list(make_client(version="v1").list("/partners/assessment/")) == []


@responses.activate
def test_next_link_on_http_is_upgraded_to_the_configured_scheme():
    url = f"{BASE}/api/latest/datamart/grants/"
    responses.get(
        url, json={"results": [{"id": 1}], "next": "http://datamart.test/api/latest/datamart/grants/?page=2"}
    )
    responses.get(f"{url}?page=2", json={"results": [{"id": 2}], "next": None})
    assert [i["id"] for i in make_client().list("grants")] == [1, 2]
    assert responses.calls[1].request.url.startswith("https://")


@responses.activate
def test_next_link_to_another_host_is_refused_and_gets_no_credentials():
    url = f"{BASE}/api/latest/datamart/grants/"
    responses.get(url, json={"results": [{"id": 1}], "next": "https://elsewhere.test/steal?page=2"})
    client = make_client()
    with pytest.raises(IntegrationError, match="another host"):
        list(client.list("grants"))
    assert len(responses.calls) == 1


def test_credentials_are_only_attached_for_the_datamart_host():
    client = make_client()
    own = requests.Request("GET", f"{BASE}/api/latest/datamart/x/").prepare()
    other = requests.Request("GET", "https://elsewhere.test/x/").prepare()
    assert client.session.auth(own).headers["Authorization"] == expected_auth()
    assert "Authorization" not in client.session.auth(other).headers


@responses.activate
def test_errors_do_not_carry_the_credentials():
    responses.get(
        f"{BASE}/api/latest/datamart/partners/", status=401, json={"detail": "Invalid username/password."}
    )
    with pytest.raises(IntegrationError) as exc:
        list(make_client().list("partners"))
    assert exc.value.status == 401
    assert PASSWORD not in str(exc.value) and USER not in str(exc.value)


def test_missing_credentials(settings):
    settings.ETOOLS_USERNAME, settings.ETOOLS_PASSWORD = "", ""
    assert not configured()
    with pytest.raises(DatamartNotConfigured):
        DatamartClient(BASE)


def test_credentials_from_settings(settings):
    settings.ETOOLS_USERNAME, settings.ETOOLS_PASSWORD = USER, PASSWORD
    assert configured()
    client = DatamartClient(BASE)
    request = requests.Request("GET", f"{BASE}/api/latest/datamart/x/").prepare()
    assert client.session.auth(request).headers["Authorization"] == expected_auth()
