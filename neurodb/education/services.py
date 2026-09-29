"""The Makani and Dirasa dashboards: what both pages share (stored years, slicers, caching, labels, maps).

Compiler sends cubes (payload format 2): records counted per combination of every slicer of the
Power BI dashboards the pages follow. A page keeps the cube rows matching the filters and adds them
up (cube.py), in Python, from the stored payload; the result of each tab and set of filters is kept
in the cache for 10 minutes. The unique count of children comes from the older blocks (figures.py),
which only answer a few filters. A payload of format 1 (no cubes) shows an empty state.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from django.core.cache import cache
from django.http import QueryDict
from django.utils import translation
from django.utils.translation import gettext as _

from neurodb.geo.models import GovernorateLocation
from neurodb.geo.services import geojson_for_level

from .figures import Figures
from .models import EducationFigures

CACHE_SECONDS = 600
NONE = "none"  # the option value of "Not specified" (an unknown value in the cube)
ALL = "all"  # the option value of "All years" where a slicer has a default instead of "All"
NOT_SPECIFIED = "Not specified"
_MISSING = object()


# ------------------------------------------------------------------------------------------ pages
@dataclass(frozen=True)
class Slicer:
    name: str  # the query parameter
    label: str
    tabs: tuple[str, ...] = ()  # the tabs showing it; every tab when empty
    kind: str = "id"  # "id": Compiler's integer ids, "text": category names
    search: bool = False  # a long list: the select gets a search box
    required: bool = False  # no "All": the first option is the default (the outreach year)
    own_data: bool = False  # filters data of its own (the outreach): not "not applied" on the other tabs


@dataclass(frozen=True)
class Page:
    programme: str  # Compiler's programme key
    name: str
    title: str
    subtitle: str
    url_name: str
    period_param: str  # "year" (Makani) or "round" (Dirasa)
    period_label: str
    tabs: tuple[tuple[str, str], ...]
    slicers: tuple[Slicer, ...]
    options: Callable[[Figures], dict[str, list[tuple[str, str]]]]
    build: Callable[[Figures, str, dict[str, Any]], dict[str, Any]]

    def tab(self, raw: str | None) -> str:
        keys = [key for key, _label in self.tabs]
        return raw if raw in keys else keys[0]

    def slicers_for(self, tab: str) -> list[Slicer]:
        return [s for s in self.slicers if not s.tabs or tab in s.tabs]


def periods(programme: str) -> list[str]:
    """The stored years (Makani) or rounds (Bridging) of a programme, the latest first (by the start of
    its first round, then by name)."""
    rows = EducationFigures.objects.filter(programme=programme).values_list(
        "year", "payload__rounds__0__start_date"
    )
    return [year for year, _start in sorted(rows, key=lambda r: (str(r[1] or ""), r[0]), reverse=True)]


def record(programme: str, period: str) -> EducationFigures | None:
    """The stored figures, their payload read only when a result is not in the cache."""
    return EducationFigures.objects.defer("payload").filter(programme=programme, year=period).first()


def cached(stored: EducationFigures, part: str, filters: dict[str, Any], build: Callable[[], Any]) -> Any:
    """``build()`` kept for 10 minutes per figures row and fetch, page part, filters and language."""
    stamp = stored.fetched_at.isoformat() if stored.fetched_at else ""
    key_data = [stored.pk, stamp, stored.programme, part, sorted(filters.items()), translation.get_language()]
    key = "education:" + hashlib.sha256(json.dumps(key_data, default=str).encode()).hexdigest()
    data = cache.get(key, _MISSING)
    if data is _MISSING:
        data = build()
        cache.set(key, data, CACHE_SECONDS)
    return data


def page_data(stored: EducationFigures, page: Page, tab: str, params: QueryDict) -> dict[str, Any]:
    """The slicers of the tab (with their options and choices), the other slicers' values to keep
    in the links (and the names of those chosen, which do not apply here), and the tab's figures
    under the filters."""
    figures: Figures | None = None

    def load() -> Figures:
        nonlocal figures
        if figures is None:
            figures = Figures(stored.payload)
        return figures

    options = cached(stored, "options", {}, lambda: page.options(load()) if load().has_cubes else None)
    if options is None:
        return {"old_format": True}
    shown = page.slicers_for(tab)
    filters, selected = parse_filters(params, shown, options)
    names = {s.name for s in shown}
    hidden = [s for s in page.slicers if s.name not in names and params.get(s.name)]
    # a filter chosen on another tab stays in the links, and the page says it does not count here
    elsewhere, _selected = parse_filters(params, hidden, options)
    data = cached(stored, tab, filters, lambda: page.build(load(), tab, filters))
    if data.get("choropleth"):  # the polygons are cached apart (geo.services), once for every filter
        config = {k: v for k, v in data["choropleth"].items() if k != "values"}
        data = {**data, "choropleth": {**config, "geojson": areas_geojson(data["choropleth"])}}
    return {
        "old_format": False,
        "slicers": [
            {"slicer": s, "options": options.get(s.name, []), "selected": selected.get(s.name, "")}
            for s in shown
        ],
        "kept": [(s.name, params[s.name]) for s in hidden],
        "not_applied": [s.label for s in hidden if s.name in elsewhere and not s.own_data],
        "filters": filters,
        "data": data,
    }


def parse_filters(
    params: QueryDict, slicers: Iterable[Slicer], options: dict[str, list[tuple[str, str]]]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The filters chosen (``None``: "Not specified"), and the option chosen per slicer. A value that
    is not among the slicer's options is ignored."""
    filters: dict[str, Any] = {}
    selected: dict[str, str] = {}
    for slicer in slicers:
        choices = [value for value, _label in options.get(slicer.name, [])]
        raw = params.get(slicer.name, "")
        if raw not in choices:
            if not (slicer.required and choices):
                continue
            raw = choices[0]
        selected[slicer.name] = raw
        if raw == ALL:
            continue
        filters[slicer.name] = None if raw == NONE else (int(raw) if slicer.kind == "id" else raw)
    return filters, selected


# ---------------------------------------------------------------------------------------- options
def id_options(figures: Figures, dimension: str, values: Iterable[Any]) -> list[tuple[str, str]]:
    """Ids as options named from the payload's lists, by name; "Not specified" last when some are unknown."""
    values = set(values)
    known = sorted(
        ((str(v), figures.label_of(dimension, v)) for v in values if v is not None),
        key=lambda option: option[1].casefold(),
    )
    return known + ([(NONE, _(NOT_SPECIFIED))] if None in values else [])


def text_options(
    values: Iterable[Any], order: Sequence[str] = (), label: Callable[[str], str] = str
) -> list[tuple[str, str]]:
    """Category names as options: those of ``order`` first, the others by name, unknown last."""
    values = set(values)
    known = [v for v in values if v is not None]
    others = sorted((v for v in known if v not in order), key=str.casefold)
    ordered = [v for v in order if v in known] + others
    return [(v, label(v)) for v in ordered] + ([(NONE, _(NOT_SPECIFIED))] if None in values else [])


# ----------------------------------------------------------------------------------------- charts
def pairs(
    counts: dict[Any, int],
    label: Callable[[Any], str | None] = str,
    *,
    order: Sequence[str] = (),
    unknown: bool = True,
) -> list[list[Any]]:
    """``[[label, count], ...]`` for a chart: equal labels merged, largest first ("Not specified" last)
    or in ``order``. ``unknown=False`` leaves the unknown value out; a label of ``None`` leaves that
    value out."""
    merged: dict[str, int] = {}
    for value, count in counts.items():
        if not count or (value is None and not unknown):
            continue
        name = _(NOT_SPECIFIED) if value is None else label(value)
        if name is None:
            continue
        merged[name] = merged.get(name, 0) + count
    if order:
        rank = {name: i for i, name in enumerate(order)}
        items = sorted(merged.items(), key=lambda item: (rank.get(item[0], len(order)), -item[1], item[0]))
    else:
        last = _(NOT_SPECIFIED)
        items = sorted(merged.items(), key=lambda item: (item[0] == last, -item[1], item[0].casefold()))
    return [[name, count] for name, count in items]


def colour(index: int) -> str:
    """The categorical palette's colour of the ``index``-th group (map points)."""
    return f"var(--nd-cat-{index % 10 + 1})"


# ----------------------------------------------------------------------------------------- labels
def humanise(key: Any) -> str:
    """``"family_moved"`` -> ``"Family moved"``: a stored key as text."""
    text = " ".join(str(key).replace("_", " ").split())
    return text[:1].upper() + text[1:]


def tidy(text: Any) -> str:
    """A stored category with its runs of spaces made single."""
    return " ".join(str(text).split())


def programme_family(value: str | None) -> str | None:
    """The Makani education programme without its level: "BLN Level 2" -> "BLN", "Summer RS Grade 3" ->
    "Summer RS", "YFS Level 1 - RS Grade 9" and the older "RS-YFS" -> "YFS - RS", "CBECE Catch-up" ->
    "CBECE"."""
    text = tidy(value or "")
    if not text:
        return None
    if re.fullmatch(r"(?i)rs\s*-\s*yfs", text):
        return "YFS - RS"
    text = re.sub(r"(?i)\s+(level|grade)\s*\d+", "", text)
    text = re.sub(r"(?i)\s+catch[\s-]?up$", "", text)
    return text.strip() or None


NO_DISABILITY = {"no", "none", "no disability", "لا", "لا يوجد"}


def cwd_ids(figures: Figures, values: Iterable[Any]) -> set[Any]:
    """The disability ids of children with a disability: known and not Compiler's "No" row."""
    return {
        v
        for v in values
        if v is not None
        and str(figures.disabilities.get(v, {}).get("name", "")).strip().casefold() not in NO_DISABILITY
    }


def nationality_group(name: str | None) -> str:
    """Dirasa's three groups: Syrian, Lebanese, Non-Lebanese (Palestinians included)."""
    if name is None:
        return _(NOT_SPECIFIED)
    text = str(name).strip().casefold()
    if "palestin" in text or "فلسطين" in text:
        return "Non-Lebanese"
    if "syria" in text or "سوري" in text:
        return "Syrian"
    if text.startswith("leban") or text.startswith("لبنان"):
        return "Lebanese"
    return "Non-Lebanese"


# Kobo outreach answers arrive as choice names (some cut at 40 characters by older forms) or labels;
# Compiler turns both into one key (lower case, "_" between words, 40 characters at most)
OUTREACH_LABELS = {
    "never_been_engaged_in_any_type_of_learni": "Never engaged in any type of learning",
    "already_enrolled_in_formal_education_rs": "Already enrolled in formal education (RS)",
    "referred_to_dirasa": "Referred to Dirasa",
    "unhcr_registered": "UNHCR registered",
    "unhcr_recorded": "UNHCR recorded",
    "syrian_id": "Syrian ID",
    "lebanese_id": "Lebanese ID",
    "palestinian_id": "Palestinian ID",
    "other_nationality_id": "Other nationality ID",
    "no_papers": "No papers",
    "other": "Other",
    "other_reasons": "Other reasons",
}
OUTREACH_PREFIXES = (  # keys cut short by the older forms, known by their start
    ("multi_service_community", "Multi-service community center (Makani)"),
    ("referral_to_makani", "Referral to Makani (retention)"),
    ("previously_enrolled_in_formal", "Previously enrolled in formal education, out of school"),
    ("previously_enrolled_in_non_formal", "Previously enrolled in non-formal education, out of school"),
    ("was_enrolled_in_formal_education_but", "Enrolled in formal education, did not continue"),
    ("was_enrolled_in_non_formal_education_bu", "Enrolled in non-formal education, did not continue"),
    ("recently_moved_from_syria", "Recently moved from Syria"),
    ("family_has_no_or_expired_doc", "Family has no or expired documents"),
    ("child_has_no_legal_doc", "Child has no legal documents"),
    ("family_needs_more_income", "Family needs more income"),
    ("family_cannot_afford_school", "Family cannot afford school fees"),
    ("family_cannot_afford_station", "Family cannot afford stationery"),
    ("schools_are_not_yet_open", "Schools are not yet open"),
)


def outreach_label(key: str) -> str:
    """An outreach answer's key as text: known keys by name or start, others with spaces for "_"."""
    if key in OUTREACH_LABELS:
        return OUTREACH_LABELS[key]
    for prefix, label in OUTREACH_PREFIXES:
        if key.startswith(prefix):
            return label
    return humanise(key)


BARRIER_LABELS = {
    "availablity_electronic_device": "Availability of an electronic device",
    "marriage engagement pregnancy": "Marriage, engagement or pregnancy",
    "violence bullying": "Violence or bullying",
}


def barrier_label(value: str) -> str:
    return BARRIER_LABELS.get(value) or humanise(value)


# ------------------------------------------------------------------------------------------- maps
# Compiler's governorates (English or Arabic names) and NeuroDB's polygons, matched by name
GOVERNORATE_ALIASES = {
    "akkar": ("Akkar", "عكار"),
    "baalbek-hermel": (
        "Baalbek-Hermel",
        "Baalbeck-Hermel",
        "Baalbek-El Hermel",
        "Baalbeck-El Hermel",
        "بعلبك-الهرمل",
        "بعلبك الهرمل",
    ),
    "beirut": ("Beirut", "بيروت"),
    "bekaa": ("Bekaa", "Beqaa", "البقاع"),
    "mount-lebanon": ("Mount Lebanon", "جبل لبنان"),
    "nabatieh": ("Nabatieh", "El Nabatieh", "Nabatiyeh", "النبطية"),
    "north": ("North", "North Lebanon", "الشمال"),
    "south": ("South", "South Lebanon", "الجنوب"),
}


def _name_key(name: Any) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", str(name or "").casefold()).split())


ALIAS_KEYS = {_name_key(alias): key for key, aliases in GOVERNORATE_ALIASES.items() for alias in aliases}


def governorate_key(name: Any) -> str:
    """One key per governorate whatever its spelling ("El Nabatieh", "النبطية" -> "nabatieh")."""
    key = _name_key(name)
    return ALIAS_KEYS.get(key, key)


def governorate_codes(figures: Figures, ids: Iterable[Any]) -> dict[Any, str]:
    """Compiler governorate id -> the code of NeuroDB's governorate polygon, matched by name."""
    areas: dict[str, str] = {}
    for code, name in GovernorateLocation.objects.order_by("code").values_list("code", "name"):
        areas.setdefault(governorate_key(name), code)
    out = {}
    for gid in ids:
        place = figures.locations.get(gid)
        code = areas.get(governorate_key(place.get("name"))) if place else None
        if code:
            out[gid] = code
    return out


def choropleth(
    figures: Figures, counts: dict[Any, int], value_label: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The map's values per polygon code (the geometry is added after the cache, see areas_geojson)
    and the table beside it, one row per Compiler governorate."""
    codes = governorate_codes(figures, counts)
    values: dict[str, dict[str, Any]] = {}
    rows = []
    for gid, count in sorted(counts.items(), key=lambda item: -item[1]):
        code = codes.get(gid)
        rows.append({"label": figures.label_of("governorate", gid), "value": count, "mapped": bool(code)})
        if code:
            values.setdefault(code, {"value": 0})["value"] += count
    for entry in values.values():
        entry["label"] = f"{entry['value']:,} {value_label}"
    config = {
        "mode": "choropleth",
        "values": values,
        "value_label": value_label,
        "empty_title": _("No governorate on the map under these filters"),
    }
    return config, rows


def areas_geojson(config: dict[str, Any]) -> dict[str, Any]:
    return geojson_for_level("governorate", config.get("values") or {})
