"""Admin-area geometry as GeoJSON, built once from the v2 polygon tables and cached."""

from __future__ import annotations

import json
from typing import Any

from django.core.cache import cache

from .models import CadasterLocation, DistrictLocation, GovernorateLocation, SimpleLocation

LEVEL_MODELS = {
    "governorate": GovernorateLocation,
    "district": DistrictLocation,
    "cadaster": CadasterLocation,
}
CACHE_SECONDS = 24 * 3600


def _ring(points: list[str] | None) -> list[list[float]]:
    """v2 stored each vertex as the text '[lon, lat]'; tolerate '{lon,lat}' and 'lon lat' too."""
    ring: list[list[float]] = []
    for raw in points or []:
        text = str(raw).strip()
        try:
            pair = (
                json.loads(text)
                if text.startswith("[")
                else [float(x) for x in text.strip("{}()").replace(",", " ").split()]
            )
            lon, lat = float(pair[0]), float(pair[1])
        except (ValueError, TypeError, IndexError, json.JSONDecodeError):
            continue
        ring.append([lon, lat])
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def geojson_for_level(level: str, values: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """FeatureCollection for one admin level; ``values`` (by area code) are merged into properties."""
    model = LEVEL_MODELS[level]
    key = f"geo:{level}:v1"
    features = cache.get(key)
    if features is None:
        features = []
        for area in model.objects.all().only("code", "name", "polygon_coordinates"):
            ring = _ring(area.polygon_coordinates)
            if len(ring) < 4:
                continue
            features.append(
                {
                    "type": "Feature",
                    "id": area.code,
                    "properties": {"code": area.code, "name": area.name},
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                }
            )
        cache.set(key, features, CACHE_SECONDS)
    if values:
        merged = []
        for f in features:
            props = {
                **f["properties"],
                **{k: v for k, v in values.get(f["id"], {}).items() if k not in ("code", "name")},
            }
            merged.append({**f, "properties": props})
        features = merged
    return {"type": "FeatureCollection", "features": features}


def site_lookup() -> dict[str, dict[str, Any]]:
    """p_code -> coordinates and cadaster code for planned-location maps."""
    key = "geo:sites:v1"
    data = cache.get(key)
    if data is None:
        data = {
            s.p_code: {
                "name": s.name,
                "latitude": s.latitude,
                "longitude": s.longitude,
                "cas_code": s.cas_code,
            }
            for s in SimpleLocation.objects.exclude(p_code="")
        }
        cache.set(key, data, CACHE_SECONDS)
    return data
