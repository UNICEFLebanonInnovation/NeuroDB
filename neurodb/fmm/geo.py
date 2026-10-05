"""The Map tab: where the visits of a filter took place, set against the places their programme
documents planned (``PCA.locations``).

Each visit with a point is matched against the planned locations of **its own** programme documents
(:func:`match`):

1. **by coordinates**, closer than ``FMM_MATCH_KM`` (2 km), only when both points are precise: the
   visit placed by its monitoring site or by its location's own point at the gazetteer's lowest admin
   level (``Visit.point_precise``), and the planned location a lowest-level location with its own
   point. A district's or governorate's centre, its own or borrowed, never counts as a visit to a site;
2. **by place**: the same location or P-code, the visit's location lying under the planned one, or the
   same name (folded);
3. **not linked** to a planned location.

A planned location no visit of the filter matched is "PD location not yet visited" (a ring). The
planned locations are those of the programme documents of the visits shown (``visited``, the
default), or those of every active programme document of the filter's sections, partners and
governorate (``active``).

:func:`map_points` gives the ``edumap.js`` configuration (points mode), the counts of the legend and
the rows of the text table below the map, which lists every point drawn with its link (the keyboard
route: the map's popups open on hover or tap only). No point carries a team member, a visit lead or a
narrative.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from django.conf import settings
from django.urls import reverse
from django.utils.dateformat import format as date_format
from django.utils.translation import gettext as _

from . import place
from .models import Visit
from .scope import NONE, RATING_LABELS, Scope

MAX_POINTS = 1000  # points drawn at most (visits first, most urgent first; then planned locations)
APPROXIMATE_OPACITY = 0.6  # a visit point that is not precise is drawn fainter
PD_SCOPES = ("visited", "active")
MATCHES = ("coords", "place", "none")
PLANNED = "planned"  # the group of the planned locations not yet visited
COLORS = {
    "coords": "var(--nd-success)",
    "place": "var(--nd-cat-6)",
    "none": "var(--nd-cat-2)",
    PLANNED: "var(--nd-muted)",
}
# the only keys a point or the configuration may carry (a test pins them: no team, lead or narrative)
POINT_KEYS = frozenset(
    {"name", "latitude", "longitude", "color", "group", "lines", "href", "open", "shape", "opacity"}
)
CONFIG_KEYS = frozenset(
    {"mode", "open", "points", "legend", "legend_title", "empty_title", "aria_label", "focus"}
)


@dataclass(frozen=True)
class PdLocation:
    """One planned location of one programme document, placed through the gazetteer."""

    pd_id: int
    pd_number: str
    partner_id: int | None
    location_id: int
    name: str
    p_code: str
    governorate: str
    governorate_key: str
    district: str
    latitude: float | None
    longitude: float | None
    level: int | None  # the location's own admin level
    own_point: bool  # placed by its own point (else by an ancestor's, or not at all)
    precise: bool  # its own point at the lowest admin level: only these match by coordinates
    approximate_from: str  # the ancestor whose point is used
    ancestors: frozenset[int]  # its own id and its ancestors' ids

    @property
    def located(self) -> bool:
        return self.latitude is not None and self.longitude is not None


# ------------------------------------------------------------------------------------------ places
def _ancestors(location_id: int | None, gazetteer: dict[int, dict[str, Any]]) -> frozenset[int]:
    """``location_id`` and the ids of every location above it."""
    out: set[int] = set()
    node, hops = gazetteer.get(location_id) if location_id else None, 0
    while node is not None and hops < 8:
        out.add(node["id"])
        node, hops = gazetteer.get(node["parent_id"]) if node["parent_id"] else None, hops + 1
    return frozenset(out)


def pd_location(
    pd_id: int,
    pd_number: str,
    partner_id: int | None,
    location_id: int,
    gazetteer: dict[int, dict[str, Any]],
    lowest: int | None,
) -> PdLocation | None:
    """The planned location ``location_id`` of a programme document, placed by its own point or its
    nearest ancestor's (None when the gazetteer does not know it)."""
    from neurodb.datamart.monitoring import _place
    from neurodb.reports.overview import governorate_key

    row = gazetteer.get(location_id)
    if row is None:
        return None
    placed = _place(location_id, gazetteer)
    own = row["latitude"] is not None and row["longitude"] is not None
    level = row["type__admin_level"]
    return PdLocation(
        pd_id=pd_id,
        pd_number=pd_number or "",
        partner_id=partner_id,
        location_id=location_id,
        name=row["name"] or "",
        p_code=row["p_code"] or "",
        governorate=placed["governorate"],
        governorate_key=governorate_key(placed["governorate"]) if placed["governorate"] else "",
        district=placed["district"],
        latitude=placed["latitude"],
        longitude=placed["longitude"],
        level=level,
        own_point=own,
        precise=place.precise("location" if own else "ancestor", level, lowest),
        approximate_from="" if own else (placed["located_by"] or ""),
        ancestors=_ancestors(location_id, gazetteer),
    )


def gazetteer_with_lowest(ids: set[int]) -> tuple[dict[int, dict[str, Any]], int | None]:
    """The locations ``ids`` and every ancestor, shaped as ``datamart.monitoring._gazetteer`` gives
    them, and the gazetteer's lowest admin level (``place.lowest_admin_level``), in one query (a
    recursive walk up the tree) rather than one per level."""
    from django.db import connection

    from neurodb.geo.models import Location, LocationType

    wanted = sorted({int(i) for i in ids if i})
    if not wanted:
        return {}, None
    location, kind = Location._meta.db_table, LocationType._meta.db_table
    sql = f"""
        WITH RECURSIVE tree(id, parent_id) AS (
            SELECT id, parent_id FROM {location} WHERE id = ANY(%s)
            UNION
            SELECT l.id, l.parent_id FROM {location} l JOIN tree t ON l.id = t.parent_id
        )
        SELECT l.id, l.name, l.p_code, l.latitude, l.longitude, l.parent_id, k.admin_level, k.name,
               (SELECT MAX(k2.admin_level) FROM {location} l2 JOIN {kind} k2 ON l2.type_id = k2.id
                WHERE l2.is_active)
        FROM {location} l LEFT JOIN {kind} k ON l.type_id = k.id
        WHERE l.id IN (SELECT id FROM tree)
    """  # noqa: S608 (table names from the models, values as parameters)
    found: dict[int, dict[str, Any]] = {}
    lowest = None
    with connection.cursor() as cursor:
        cursor.execute(sql, [wanted])
        for pk, name, p_code, lat, lon, parent, level, level_name, deepest in cursor.fetchall():
            found[pk] = {
                "id": pk,
                "name": name,
                "p_code": p_code,
                "latitude": lat,
                "longitude": lon,
                "parent_id": parent,
                "type__admin_level": level,
                "type__name": level_name,
            }
            lowest = deepest
    return found, lowest


def _visit_ancestors(visit: Visit) -> frozenset[int]:
    """The visit's location (else its site's) and every location above it (read when not given)."""
    from neurodb.datamart.models import MonitoringSite

    start = visit.location_id
    if start is None and visit.site_id is not None:
        start = MonitoringSite.objects.filter(pk=visit.site_id).values_list("parent_id", flat=True).first()
    return _ancestors(start, gazetteer_with_lowest({start} if start else set())[0])


def _distance(visit: Visit, loc: PdLocation) -> float | None:
    if visit.latitude is None or visit.longitude is None or not loc.located:
        return None
    return place.haversine_km(visit.latitude, visit.longitude, loc.latitude, loc.longitude)


def match(
    visit: Visit,
    pd_locations: list[PdLocation],
    km: float,
    ancestors: frozenset[int] | None = None,
) -> tuple[str, PdLocation | None, float | None]:
    """How ``visit`` matches the planned locations of its programme documents: ``("coords", the
    nearest planned location, its distance in km)`` when both points are precise and closer than
    ``km``; else ``("place", the planned location, the distance or None)`` by the same location or
    P-code, the visit's location lying under the planned one, or the same name; else ``("none", None,
    None)``. ``ancestors`` (the visit's location and those above it) is read when not given."""
    from .parse import fold

    if visit.point_precise and visit.latitude is not None and visit.longitude is not None:
        best: tuple[PdLocation, float] | None = None
        for loc in pd_locations:
            if not (loc.precise and loc.located):
                continue
            distance = place.haversine_km(visit.latitude, visit.longitude, loc.latitude, loc.longitude)
            if distance < km and (best is None or distance < best[1]):
                best = (loc, distance)
        if best is not None:
            return "coords", best[0], best[1]
    if not pd_locations:
        return "none", None, None
    if ancestors is None:
        ancestors = _visit_ancestors(visit)
    pcode = (visit.place_pcode or "").strip().upper()
    name = fold(visit.place_name)
    # the same place first (location id or P-code), then the planned area that holds the visit, then
    # the same name
    for found in (
        lambda loc: loc.location_id == visit.location_id or bool(pcode and loc.p_code.upper() == pcode),
        lambda loc: loc.location_id in ancestors,
        lambda loc: bool(name and fold(loc.name) == name),
    ):
        hits = [loc for loc in pd_locations if found(loc)]
        if hits:
            loc = min(hits, key=lambda h: (_distance(visit, h) is None, _distance(visit, h) or 0.0))
            return "place", loc, _distance(visit, loc)
    return "none", None, None


# ------------------------------------------------------------------------------------------ the map
def _day(value) -> str:
    return date_format(value, "j M Y") if value else ""


def _rating_text(v: Visit) -> str:
    """ "On track · rated 12 May 2026"; "Not rated yet · ends 30 Sep 2026" for a planned or in-progress
    visit without a rating."""
    if v.rating == "not_monitored" and v.status_group in ("planned", "in_progress"):
        text = _("Not rated yet")
        return f"{text} · {_('ends %(date)s') % {'date': _day(v.end_date)}}" if v.end_date else text
    text = _(RATING_LABELS.get(v.rating, "Other"))
    return f"{text} · {_('rated %(date)s') % {'date': _day(v.end_date)}}" if v.end_date else text


def _approximate_text(v: Visit) -> str:
    """Why a visit point is not precise: placed at an ancestor's point, or at a larger area's own."""
    from neurodb.datamart.monitoring import LEVEL_DISTRICT, LEVEL_GOVERNORATE

    if v.point_precise:
        return ""
    if v.located_by == "ancestor":
        area = v.approximate_from
    elif v.located_level == LEVEL_DISTRICT:
        area = v.district_name  # the district's own centre
    elif v.located_level == LEVEL_GOVERNORATE:
        area = v.governorate_name
    else:
        area = v.place_name
    return _("approximate: placed at %(place)s") % {"place": area or _("a larger area")}


def _km(distance: float | None) -> str:
    return "" if distance is None else f"{distance:.1f} km"


MATCH_LABELS = {
    "coords": "Matched by coordinates",
    "place": "Matched by place",
    "none": "Actual visit — not linked to a PD location",
}


def match_label(kind: str) -> str:
    """A visit's match in words."""
    return _(MATCH_LABELS.get(kind, MATCH_LABELS["none"]))


def _active_pds(scope: Scope):
    """The active programme documents of the filter's sections, partners and programme document."""
    from django.db.models import Q

    from neurodb.partnerships.models import PCA

    qs = PCA.objects.filter(status__iexact="active")
    if scope.empty:  # narrowed to nothing, as its visits are
        return qs.none()
    if scope.sections:
        names = [s for s in scope.sections if s != NONE]
        wanted = Q(section_names__overlap=names) if names else Q(pk__in=[])
        if NONE in scope.sections:
            wanted |= Q(section_names=[])
        qs = qs.filter(wanted)
    if scope.partners:
        qs = qs.filter(partner_id__in=scope.partners)
    if scope.pd is not None:
        qs = qs.filter(pk=scope.pd)
    return qs


def map_points(
    scope: Scope,
    pd_scope: Literal["visited", "active"] = "visited",
    km: float | None = None,
    when: str | None = None,
) -> dict[str, Any]:
    """The Map tab of ``scope``: the ``edumap.js`` configuration (``config``), the legend counts
    (``counts``) and the rows of the text table below the map (``visit_rows``, ``planned_rows``,
    ``unlocated_rows``). Kept ten minutes like every block (``metrics.cached``; ``when`` is
    ``metrics.stamp``)."""
    from . import metrics

    pd_scope = pd_scope if pd_scope in PD_SCOPES else "visited"
    km = float(settings.FMM_MATCH_KM if km is None else km)
    return metrics.cached(scope, f"map:{pd_scope}:{km:g}", lambda: _compute(scope, pd_scope, km), when)


def _compute(scope: Scope, pd_scope: str, km: float) -> dict[str, Any]:
    from django.db.models import F

    from neurodb.partnerships.models import PCA

    visits = list(
        scope.visits()
        .select_related("partner", "site")
        .only(
            "key",
            "label",
            "end_date",
            "status_group",
            "rating",
            "quality_score",
            "urgency",
            "latitude",
            "longitude",
            "located_by",
            "located_level",
            "point_precise",
            "district_name",
            "governorate_name",
            "approximate_from",
            "place_name",
            "place_pcode",
            "location_id",
            "site_id",
            "pd_ids",
            "partner__name",
            "partner__short_name",
            "site__parent_id",
        )
        .order_by("-urgency", F("end_date").desc(nulls_last=True), "key")
    )

    # the programme documents whose planned locations are drawn, and those the visits are matched to
    visit_pds = {pk for v in visits for pk in v.pd_ids}
    active = set(_active_pds(scope).values_list("pk", flat=True)) if pd_scope == "active" else set()
    pds = {
        row["pk"]: row
        for row in PCA.objects.filter(pk__in=visit_pds | active).values(
            "pk", "number", "partner_id", "partner__name", "partner__short_name"
        )
    }
    if pd_scope == "active":
        shown = active
    else:
        shown = {
            pk
            for pk in visit_pds
            if pk in pds
            and (not scope.partners or pds[pk]["partner_id"] in scope.partners)
            and (scope.pd is None or pk == scope.pd)
        }
    pairs = list(
        PCA.locations.through.objects.filter(pca_id__in=list(pds)).values_list("pca_id", "location_id")
    )

    # the gazetteer of every planned location and visit place (a site's parent when the visit has no
    # location), with their ancestors
    starts = {v.pk: v.location_id or (v.site.parent_id if v.site_id and v.site else None) for v in visits}
    gazetteer, lowest = gazetteer_with_lowest({loc for _pd, loc in pairs} | {s for s in starts.values() if s})
    by_pd: dict[int, list[PdLocation]] = defaultdict(list)
    for pd_id, location_id in pairs:
        loc = pd_location(
            pd_id, pds[pd_id]["number"], pds[pd_id]["partner_id"], location_id, gazetteer, lowest
        )
        if loc is not None:
            by_pd[pd_id].append(loc)

    # every visit of the filter is matched (one without a point can still match by place)
    matched: set[tuple[int, int]] = set()
    results: dict[int, tuple[str, PdLocation | None, float | None]] = {}
    for v in visits:
        own = [loc for pk in v.pd_ids for loc in by_pd.get(pk, ())]
        found = match(v, own, km, ancestors=_ancestors(starts[v.pk], gazetteer))
        results[v.pk] = found
        if found[1] is not None:
            # the place is visited for each of the visit's programme documents that planned it, not
            # only for the one match() names: two of its PDs planning one cadaster draw no ring there
            place_id = found[1].location_id
            matched.update((loc.pd_id, loc.location_id) for loc in own if loc.location_id == place_id)

    region = scope.governorate if scope.governorate and scope.governorate != NONE else ""
    not_visited = sorted(
        (
            loc
            for pk in shown
            for loc in by_pd.get(pk, ())
            if (loc.pd_id, loc.location_id) not in matched and (not region or loc.governorate_key == region)
        ),
        key=lambda loc: (loc.pd_number, loc.name.casefold(), loc.location_id),
    )
    planned_count = sum(
        1 for pk in shown for loc in by_pd.get(pk, ()) if not region or loc.governorate_key == region
    )

    mapped = [v for v in visits if v.latitude is not None and v.longitude is not None]
    unlocated = [v for v in visits if v.latitude is None or v.longitude is None]
    points: list[dict[str, Any]] = []
    visit_rows: list[dict[str, Any]] = []
    counts = {kind: 0 for kind in MATCHES}
    for v in mapped:
        kind, loc, distance = results[v.pk]
        counts[kind] += 1
        if len(points) >= MAX_POINTS:
            continue
        url = reverse("fmm:visit", args=[v.key])
        approximate = _approximate_text(v)
        lines = [
            [_("Partner"), (v.partner.short_name or v.partner.name) if v.partner else "—"],
            [_("Date"), _day(v.end_date) or "—"],
            [_("Rating"), _rating_text(v)],
            [_("Quality"), _percent(v.quality_score)],
            [_("Match"), match_label(kind)],
        ]
        if distance is not None:
            lines.append([_("Distance"), _km(distance)])
        if loc is not None:
            lines.append([_("PD location"), f"{loc.name} · {loc.pd_number}"])
        if approximate:
            lines.append([_("Where"), approximate])
        point = {
            "name": v.label,
            "latitude": v.latitude,
            "longitude": v.longitude,
            "color": COLORS[kind],
            "group": kind,
            "lines": lines,
            "href": url,
        }
        if not v.point_precise:
            point["opacity"] = APPROXIMATE_OPACITY
        points.append(point)
        visit_rows.append(
            {
                "key": v.key,
                "label": v.label,
                "url": url,
                "date": v.end_date,
                "match": kind,
                "match_label": match_label(kind),
                "distance": _km(distance),
                "pd_location": loc.name if loc else "",
                "pd_number": loc.pd_number if loc else "",
                "pd_url": reverse("reports:programme_detail", args=[loc.pd_id]) if loc else "",
                "approximate": approximate,
            }
        )
    visits_drawn = len(points)

    # one ring per planned place not yet visited (two programme documents planned at one place share it)
    rings: dict[int, list[PdLocation]] = {}
    for loc in not_visited:
        if loc.located:
            rings.setdefault(loc.location_id, []).append(loc)
    rings_drawn = 0
    for locs in rings.values():
        if len(points) >= MAX_POINTS:
            break
        first = locs[0]
        partners = sorted(
            {pds[loc.pd_id]["partner__short_name"] or pds[loc.pd_id]["partner__name"] or "" for loc in locs}
            - {""}
        )
        lines = [
            [_("Programme documents") if len(locs) > 1 else _("Programme document"),
             ", ".join(loc.pd_number for loc in locs)],
            [_("Partner"), ", ".join(partners) or "—"],
            [_("Governorate"), first.governorate or "—"],
            [_("Status"), _("PD location not yet visited")],
        ]  # fmt: skip
        if not first.own_point:
            where = first.approximate_from or _("a larger area")
            lines.append([_("Where"), _("approximate: placed at %(place)s") % {"place": where}])
        points.append(
            {
                "name": first.name,
                "latitude": first.latitude,
                "longitude": first.longitude,
                "color": COLORS[PLANNED],
                "group": PLANNED,
                "shape": "ring",
                "lines": lines,
                "href": reverse("reports:programme_detail", args=[first.pd_id]),
                "open": "page",
            }
        )
        rings_drawn += 1

    planned_rows = [
        {
            "pd_number": loc.pd_number,
            "pd_url": reverse("reports:programme_detail", args=[loc.pd_id]),
            "partner": pds[loc.pd_id]["partner__short_name"] or pds[loc.pd_id]["partner__name"] or "",
            "place": loc.name,
            "governorate": loc.governorate,
            "located": loc.located,
            "approximate": not loc.own_point and loc.located,
        }
        for loc in not_visited[:MAX_POINTS]
    ]
    unlocated_rows = [
        {
            "key": v.key,
            "label": v.label,
            "url": reverse("fmm:visit", args=[v.key]),
            "date": v.end_date,
            "place": v.place_name,
            "match_label": match_label(results[v.pk][0]),
        }
        for v in unlocated[:MAX_POINTS]
    ]

    legend = []
    for group, label, n in (
        ("coords", _("Matched by coordinates"), counts["coords"]),
        ("place", _("Matched by place"), counts["place"]),
        ("none", _("Not linked to a PD location"), counts["none"]),
        (PLANNED, _("PD location not yet visited"), rings_drawn),
    ):
        if n:
            entry = {"label": f"{label} ({n})", "color": COLORS[group], "group": group, "toggle": True}
            if group == PLANNED:
                entry["shape"] = "ring"
            legend.append(entry)
    return {
        "config": {
            "mode": "points",
            "open": "modal",
            "points": points,
            "legend": legend,
            "legend_title": _("Visits and planned locations"),
            "empty_title": _("No visit in this filter has coordinates"),
            "aria_label": _(
                "Map of the visits of this filter and the places their programme documents planned"
            ),
        },
        "counts": {
            "visits": len(visits),
            "mapped": len(mapped),
            **counts,
            "approximate": sum(1 for v in mapped if not v.point_precise),
            "unlocated": len(unlocated),
            "planned": planned_count,  # planned locations drawn from (programme document, place)
            "not_visited": len(not_visited),
            "not_visited_unlocated": sum(1 for loc in not_visited if not loc.located),
            "rings": rings_drawn,
            "drawn": len(points),
            "visits_drawn": visits_drawn,
            "capped": len(points) < len(mapped) + len(rings),
        },
        "visit_rows": visit_rows,
        "planned_rows": planned_rows,
        "planned_more": max(len(not_visited) - len(planned_rows), 0),
        "unlocated_rows": unlocated_rows,
        "unlocated_more": max(len(unlocated) - len(unlocated_rows), 0),
        "pd_scope": pd_scope,
        "km": km,
    }


def _percent(value) -> str:
    from neurodb.web.templatetags.ui import percent

    return percent(value) if value is not None else _("not scored")
