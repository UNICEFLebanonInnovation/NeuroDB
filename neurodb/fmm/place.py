"""Where a visit took place, as precisely as the data allows.

A visit is placed by its monitoring site's point, else by the coordinates eTools wrote on its rows
(``location_lat``/``location_lon``), else by its location's own point, else by the point of the nearest
ancestor that has one (a district's or a governorate's centre, marked approximate). Only a **precise**
point may match a programme document's planned location by distance: a site; coordinates written on the
rows when their ``location_type`` is the gazetteer's lowest admin level, or when they lie more than
``SAME_POINT_KM`` from the gazetteer's point of the visit's location (its own or its nearest
ancestor's), so they are not a centre; or a location's own point when that location sits at the
gazetteer's lowest admin level (cadasters in Lebanon). A district or governorate centre, its own or
borrowed, never counts as a visit to a site.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

from django.db.models import Max

EARTH_KM = 6371.0088  # mean Earth radius
SAME_POINT_KM = 0.1  # written coordinates this close to the gazetteer's point are taken as that point
_ADMIN_LEVEL = re.compile(r"\b(?:admin|adm|level)\s*(?:level\s*)?(\d)\b")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """The great-circle distance between two points, in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def lowest_admin_level() -> int | None:
    """The deepest admin level of the active gazetteer (cadasters in Lebanon); None when it is empty."""
    from neurodb.geo.models import Location

    return Location.objects.filter(is_active=True).aggregate(level=Max("type__admin_level"))["level"]


def precise(located_by: str, level: int | None, lowest: int | None) -> bool:
    """A point that may match by distance: a site's, or a location's own point at the lowest admin
    level. An ancestor's point, no point, or a higher level's own point (a district centre) is not."""
    if located_by == "site":
        return True
    return located_by == "location" and level is not None and lowest is not None and level == lowest


def _fold(text: Any) -> str:
    return " ".join(str(text or "").casefold().replace("_", " ").replace("-", " ").split())


def lowest_type_names(lowest: int | None) -> frozenset[str]:
    """The names of the gazetteer's location types at its lowest admin level ("cadaster"), folded."""
    from neurodb.geo.models import LocationType

    if lowest is None:
        return frozenset()
    return frozenset(
        _fold(n) for n in LocationType.objects.filter(admin_level=lowest).values_list("name", flat=True)
    )


def type_is_lowest(location_type: Any, lowest: int | None, lowest_types: frozenset[str]) -> bool:
    """eTools' ``location_type`` names the gazetteer's lowest admin level: one of its type names, or
    "admin 3" / "Admin level 3" / "adm3" with its number."""
    folded = _fold(location_type)
    if not folded or lowest is None:
        return False
    if folded in lowest_types:
        return True
    match = _ADMIN_LEVEL.search(folded)
    return bool(match) and int(match.group(1)) == lowest


def written_point_precise(
    point: tuple[float, float],
    location_type: Any,
    node: Mapping[str, Any] | None,
    gazetteer: Mapping[int, Mapping[str, Any]],
    lowest: int | None,
    lowest_types: frozenset[str],
) -> bool:
    """Whether coordinates written on a visit's rows may match by distance: their ``location_type`` is
    the lowest admin level; or they lie more than ``SAME_POINT_KM`` from the gazetteer's point of the
    visit's location (``node``: its own point, else its nearest ancestor's), so they are not a centre;
    or they are that location's own point and it sits at the lowest level. Without a location in the
    gazetteer to compare with, only the type tells."""
    if type_is_lowest(location_type, lowest, lowest_types):
        return True
    current, hops = node, 0
    while current is not None and hops < 8:
        if current.get("latitude") is not None and current.get("longitude") is not None:
            distance = haversine_km(point[0], point[1], current["latitude"], current["longitude"])
            if distance > SAME_POINT_KM:
                return True
            own = current is node
            return own and lowest is not None and current.get("type__admin_level") == lowest
        parent = current.get("parent_id")
        current, hops = (gazetteer.get(parent) if parent else None), hops + 1
    return False
