"""Every recorded Datamart sample (tests/fixtures/datamart/*.json) goes through its sync cleanly."""

import pytest
import responses

from neurodb.core.models import SyncRun
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools import samples
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.http import make_session

pytestmark = pytest.mark.django_db
BASE = "https://datamart.test"
RECORDED = samples.recorded()


def _client(country: str) -> DatamartClient:
    return DatamartClient(
        BASE, "svc-user", "not-a-real-secret", country=country, session=make_session("", backoff=0)
    )


@pytest.mark.skipif(not RECORDED, reason="no recorded samples: run manage record_datamart_samples")
@pytest.mark.parametrize("sample", RECORDED, ids=[s["dataset"] for s in RECORDED])
@responses.activate
def test_recorded_sample_syncs_without_failed_rows(sample, settings):
    results = sample["results"]
    country = next((str(r["country_name"]) for r in results if r.get("country_name")), "Lebanon")
    if "business_area" in sample["params"]:
        settings.ETOOLS_DATAMART_BUSINESS_AREA = sample["params"]["business_area"]
    if "business_area_code" in sample["params"]:
        settings.ETOOLS_DATAMART_BUSINESS_AREA = sample["params"]["business_area_code"]
    responses.get(f"{BASE}/api/latest/{sample['path']}/", json={"results": results, "next": None})
    (run,) = sync.sync_all(only=[sample["dataset"]], triggered_by="test", client=_client(country))
    assert run.status != SyncRun.Status.FAILED, run.error
    assert run.rows_failed == 0, run.details.get("errors")
    assert run.rows_in == len(results)


def test_every_sync_dataset_has_an_endpoint_to_record(settings):
    settings.ETOOLS_DATAMART_BUSINESS_AREA = "2490"  # the PRP datasets filter by it, no lookup needed
    client = _client("Lebanon")
    for name in sync.ENTITY_SYNCS:
        path, params, by_country = samples.endpoint_of(name, client)
        assert path and isinstance(params, dict) and isinstance(by_country, bool), name


@responses.activate
def test_record_and_replay_round_trip(tmp_path):
    client = _client("Lebanon")
    responses.get(
        f"{BASE}/api/latest/datamart/partners/",
        json={
            "results": [
                {"id": 1, "source_id": 1, "name": "P", "email": "x@example.org", "country_name": "Lebanon"}
            ],
            "next": None,
        },
    )
    sample = samples.record(client, "partners", 5)
    assert sample["path"] == "partners" and "email" not in sample["results"][0]
    written = samples.write(sample, tmp_path)
    assert samples.recorded(tmp_path)[0]["dataset"] == "partners" and written.name == "partners.json"
