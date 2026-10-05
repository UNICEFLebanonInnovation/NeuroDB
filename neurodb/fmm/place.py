"""Where a visit took place, as precisely as the data allows.

A visit is placed by its monitoring site's point, else by its location's own point, else by the point
of the nearest ancestor that has one (a district's or a governorate's centre, marked approximate). Only
a **precise** point may match a programme document's planned location by distance: a site, or a
location's own point when that location sits at the gazetteer's lowest admin level (cadasters in
Lebanon). A district or governorate centre, its own or borrowed, never counts as a visit to a site.
"""

from __future__ import annotations

import math

from django.db.models import Max

EARTH_KM = 6371.0088  # mean Earth radius


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
