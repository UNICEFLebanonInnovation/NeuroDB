"""Record real Datamart records as test fixtures (``manage record_datamart_samples``).

The sync tests were written from the Swagger; every first contact with production has found a shape
the Swagger did not show. Recording a few scrubbed records per dataset from the live Datamart
gives the tests the real shapes, and ``tests/integrations/test_recorded_samples.py`` replays every
recorded file through the sync.
"""

from __future__ import annotations

import datetime
import json
from itertools import islice
from pathlib import Path
from typing import Any

from neurodb.datamart import catalogue
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.integrations.etools.datamart import DatamartClient

FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "datamart"
LEGACY_PATHS = {
    "partners": "partners",
    "interventions": "interventions",
    "intervention_budgets": "interventions-budget",
    "agreements": "partners/agreements",
    "locations": "locations",
    "location_sites": "location-sites",
}


def endpoint_of(name: str, client: DatamartClient) -> tuple[str, dict[str, Any], bool]:
    """(Datamart path, query parameters, whether the country filter applies) of a sync dataset."""
    if name in sync.DATASETS:
        path, spec = sync.DATASETS[name]
        return path, (spec.params() if spec.params else {}), True
    if name in sync.ENRICHMENTS:
        return sync.ENRICHMENTS[name][0], {}, True
    if name in catalogue.DOCUMENTS:
        spec = catalogue.DOCUMENTS[name]
        if spec.scope == "written_by":
            return LEGACY_PATHS.get(name, spec.path), {}, True
        params, by_country = sync.CountryScope(client).request(spec)
        return spec.path, params, by_country
    raise ValueError(f"unknown dataset {name!r}")


def record(client: DatamartClient, name: str, limit: int) -> dict[str, Any]:
    """The first ``limit`` records of a dataset, scrubbed of contact details."""
    path, params, by_country = endpoint_of(name, client)
    rows = client.list(path, {**params, "page_size": limit}, country=by_country)
    results = [catalogue.scrub(item) for item in islice(rows, limit)]
    return {
        "dataset": name,
        "path": path,
        "params": params,
        "country": by_country,
        "recorded": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "results": results,
    }


def write(sample: dict[str, Any], folder: Path = FIXTURES) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{sample['dataset']}.json"
    target.write_text(json.dumps(sample, indent=1, sort_keys=True, default=str) + "\n")
    return target


def recorded(folder: Path = FIXTURES) -> list[dict[str, Any]]:
    """Every recorded sample, oldest dataset name first."""
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.exists() else []
