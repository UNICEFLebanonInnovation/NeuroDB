"""eTools locations -> ``geo.LocationType`` / ``geo.Location`` (v2 ``locations/tasks.py``).

Ports ``sync_location_type_data`` (``/api/locations-types/``) and ``sync_locations_data``
(``/api/locations/``): upsert by the eTools id, ``geo_point`` ``POINT (lon lat)`` split into
longitude/latitude, ``cas_code`` taken from ``p_code`` (``A-B-C`` -> ``A``; ``LB_CAS_x`` -> ``x``).

Fixed v2 defects: ``count`` was incremented without being initialised (every location raised
``NameError`` after its save and the loop silently continued) -> counters live on the ``SyncRun``;
``type`` was only printed -> set from ``gateway.id`` when that ``LocationType`` exists; ``parent``
(commented out in v2) is linked in a second pass once every location row exists. The legacy table
carries MPTT columns without defaults; new rows get ``lft=1, rght=2, level=0, tree_id=id`` so the
insert succeeds (v3 does not maintain the tree).
"""

from __future__ import annotations

import logging
from typing import Any

from neurodb.core.models import SyncRun
from neurodb.geo.models import Location, LocationType
from neurodb.integrations.etools.client import EToolsClient
from neurodb.integrations.runs import fail, finish_by_counts, new_run, process_items

logger = logging.getLogger(__name__)


def parse_point(geo_point: str | None) -> tuple[float | None, float | None]:
    """``"POINT (35.5 33.9)"`` -> (longitude, latitude); anything else -> (None, None)."""
    if not geo_point or "(" not in geo_point:
        return None, None
    try:
        longitude, latitude = geo_point.split("(", 1)[1].rstrip(")").split()
        return float(longitude), float(latitude)
    except ValueError:
        return None, None


def cas_code_from_p_code(p_code: str) -> str | None:
    """The v2 rules, later rule wins: ``A-B-C`` -> ``A``; ``LB_CAS_x`` -> ``x``."""
    code = None
    parts = p_code.split("-")
    if len(parts) == 3:
        code = parts[0]
    parts = p_code.split("LB_CAS_")
    if len(parts) == 2:
        code = parts[1]
    return code


def sync_location_types(run: SyncRun, *, client: EToolsClient | None = None) -> SyncRun:
    client = client or EToolsClient()

    def handle(item: dict[str, Any]) -> None:
        location_type, _ = LocationType.objects.get_or_create(
            id=int(item["id"]), defaults={"name": item["name"]}
        )
        location_type.name = item["name"]
        location_type.admin_level = item.get("admin_level")
        location_type.save()

    try:
        process_items(
            run, client.list("/api/locations-types/", page_size=None), handle, lambda i: str(i.get("id"))
        )
    except Exception as exc:
        fail(run, exc)
        raise
    return finish_by_counts(run)


def _upsert_location(item: dict[str, Any], type_ids: set[int]) -> None:
    pk = int(item["id"])
    location, _ = Location.objects.get_or_create(
        id=pk, defaults={"name": item.get("name") or "", "lft": 1, "rght": 2, "level": 0, "tree_id": pk}
    )
    location.name = item.get("name") or ""
    location.p_code = item.get("p_code") or ""
    gateway = item.get("gateway") or {}
    gateway_id = gateway.get("id") if isinstance(gateway, dict) else gateway
    location.type_id = int(gateway_id) if gateway_id is not None and int(gateway_id) in type_ids else None
    location.longitude, location.latitude = parse_point(item.get("geo_point"))
    cas_code = cas_code_from_p_code(location.p_code)
    if cas_code is not None:
        location.cas_code = cas_code
    location.save()


def _link_parents(items: list[dict[str, Any]]) -> int:
    existing = set(Location.objects.values_list("pk", flat=True))
    linked = 0
    for item in items:
        parent = item.get("parent")
        if parent in (None, "") or int(parent) not in existing or int(item["id"]) not in existing:
            continue
        Location.objects.filter(pk=int(item["id"])).update(parent_id=int(parent))
        linked += 1
    return linked


def sync_locations(run: SyncRun, *, client: EToolsClient | None = None) -> SyncRun:
    client = client or EToolsClient()
    try:
        items = list(client.list("/api/locations/", page_size=None))
        type_ids = set(LocationType.objects.values_list("pk", flat=True))
        process_items(run, items, lambda item: _upsert_location(item, type_ids), lambda i: str(i.get("id")))
        linked = _link_parents(items)
    except Exception as exc:
        fail(run, exc)
        raise
    return finish_by_counts(run, parents_linked=linked)


def sync_all_locations(
    *, triggered_by: str = "schedule", client: EToolsClient | None = None
) -> list[SyncRun]:
    """Types first (locations reference them), then locations; each with its own run."""
    client = client or EToolsClient()
    runs: list[SyncRun] = []
    for target, sync in (("location_types", sync_location_types), ("locations", sync_locations)):
        run = new_run(SyncRun.Job.LOCATIONS, target=target, triggered_by=triggered_by)
        try:
            sync(run, client=client)
        except Exception:
            logger.error("locations %s aborted", target)
        runs.append(run)
    return runs
