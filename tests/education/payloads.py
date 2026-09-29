"""Synthetic education payloads (format 2), built from fake registrations the way Compiler builds them:
a cube counts the records per combination of its dimensions, a block counts unique children per
grouping, and the lists name every id used. Tests and the local demo use them; nothing here is real.
"""

from __future__ import annotations

import re
from itertools import combinations
from typing import Any

MAKANI_DIMS = (
    "center",
    "partner",
    "governorate",
    "sex",
    "nationality",
    "disability",
    "caregiver",
    "working",
    "programme",
    "education_status",
    "package",
    "id_type",
    "malnutrition",
    "delay",
)
MAKANI_MEASURES = (
    "registrations",
    "married",
    "idp",
    "counselling",
    "immunized",
    "screened",
    "minimum_meals",
    "vaccinated",
)
STAFF_DIMS = ("center", "partner", "governorate", "sex", "active")
DIRASA_DIMS = (
    "school",
    "partner",
    "governorate",
    "sex",
    "nationality",
    "disability",
    "level",
    "learning_result",
    "barrier",
)
DIRASA_MEASURES = ("registrations", "pre_tested", "post_tested")
OUTREACH_DIMS = ("year", "partner", "governorate", "value")
OUTREACH_QUESTIONS = ("education_status", "referral", "dropout_reason", "id_type")
TEXT_DEFAULTS = {
    "sex": "Not specified",
    "caregiver": "Not specified",
    "working": "Not specified",
    "active": "Not specified",
}

PARTNERS = [
    {"id": 5, "name": "Makani Partner", "short_name": "MP"},
    {"id": 6, "name": "Another NGO", "short_name": ""},
    {"id": 7, "name": "Dirasa Partner", "short_name": "DP"},
]
LOCATIONS = [
    {"id": 20, "name": "Akkar", "p_code": "", "parent": None, "type": "Governorate"},
    {"id": 21, "name": "Halba", "p_code": "", "parent": 20, "type": "District"},
    {"id": 22, "name": "Beirut", "p_code": "", "parent": None, "type": "Governorate"},
    {"id": 23, "name": "بعلبك-الهرمل", "p_code": "", "parent": None, "type": "Governorate"},
    {"id": 24, "name": "El Nabatieh", "p_code": "", "parent": None, "type": "Governorate"},
]
NATIONALITIES = [
    {"id": 1, "name": "Syrian"},
    {"id": 4, "name": "Palestinian from Lebanon"},
    {"id": 5, "name": "Lebanese"},
    {"id": 2, "name": "Iraqi"},
]
DISABILITIES = [
    {"id": 1, "name": "No"},
    {"id": 3, "name": "Difficulty hearing"},
    {"id": 6, "name": "Difficulty seeing"},
]
ID_TYPES = [
    {"id": 1, "name": "UNHCR Registered"},
    {"id": 5, "name": "Lebanese national ID"},
    {"id": 7, "name": "Caregiver has no ID"},
]
TRAININGS = [{"id": 1, "name": "Inclusion"}, {"id": 2, "name": "SEL"}, {"id": 3, "name": "PSEA"}]
LISTS = {
    "partners": PARTNERS,
    "locations": LOCATIONS,
    "nationalities": NATIONALITIES,
    "disabilities": DISABILITIES,
    "id_types": ID_TYPES,
    "trainings": TRAININGS,
}
CENTERS = [
    {
        "id": 100, "name": "Halba center", "partner": 5, "governorate": 20, "district": 21, "cadaster": None,
        "type": "Community Hub", "emergency": "Yes", "is_active": True, "latitude": 34.54, "longitude": 36.08,
    },
    {
        "id": 101, "name": "Beirut center", "partner": 6, "governorate": 22, "district": None, "cadaster": None,
        "type": "SDC", "emergency": "No", "is_active": True, "latitude": 33.89, "longitude": 35.5,
    },
    {
        "id": 102, "name": "Hermel center", "partner": 5, "governorate": 23, "district": None, "cadaster": None,
        "type": None, "emergency": "Not specified", "is_active": False, "latitude": None, "longitude": None,
    },
]  # fmt: skip
SCHOOLS = [
    {
        "id": 300, "name": "Akkar private school", "number": "1001", "type": "Private School", "governorate": 20,
        "district": 21, "emergency": "Yes", "latitude": 34.55, "longitude": 36.1, "listed": [7],
        "counts": {"children": 400, "children_male": 190, "children_female": 210, "children_lebanese": 300,
                   "children_non_lebanese": 100, "cwd": 12, "dirasa_children": 40, "dirasa_cwd": 2},
    },
    {
        "id": 301, "name": "Beirut free school", "number": "1002", "type": "Private Free School", "governorate": 22,
        "district": None, "emergency": "No", "latitude": 33.88, "longitude": 35.51, "listed": [7, 6],
        "counts": {"children": 250, "children_male": 120, "children_female": 130, "children_lebanese": 150,
                   "children_non_lebanese": 100, "cwd": None, "dirasa_children": 20, "dirasa_cwd": None},
    },
    {
        "id": 302, "name": "Nabatieh school", "number": "1003", "type": None, "governorate": 24,
        "district": None, "emergency": "Not specified", "latitude": None, "longitude": None, "listed": [6],
        "counts": {"children": None, "children_male": None, "children_female": None, "children_lebanese": None,
                   "children_non_lebanese": None, "cwd": None, "dirasa_children": None, "dirasa_cwd": None},
    },
]  # fmt: skip


def _sort_key(row: list[Any]) -> tuple:
    return tuple("" if v is None else str(v) for v in row)


def cube(records: list[dict[str, Any]], dims: tuple[str, ...], measures: tuple[str, ...]) -> dict[str, Any]:
    """Records counted per combination of ``dims``; a measure is the sum of the records' values (1 for
    ``registrations``, ``staff``, ``teachers`` and ``children``, a record without it counts 0)."""
    cells: dict[tuple, list[int]] = {}
    for record in records:
        key = tuple(record.get(d, TEXT_DEFAULTS.get(d)) for d in dims)
        cell = cells.setdefault(key, [0] * len(measures))
        for i, measure in enumerate(measures):
            cell[i] += int(
                record.get(measure, 1 if measure in ("registrations", "staff", "teachers", "children") else 0)
            )
    rows = sorted((list(key) + values for key, values in cells.items()), key=_sort_key)
    return {"dims": list(dims), "measures": list(measures), "rows": rows}


def groupings(main: tuple[str, ...], details: tuple[str, ...], extra=()) -> list[tuple[str, ...]]:
    """Compiler's groupings: every subset of ``main``, each alone and with one detail, and ``extra``."""
    out = []
    for size in range(len(main) + 1):
        for chosen in combinations(main, size):
            out.append(tuple(sorted(chosen)))
            out += [tuple(sorted(chosen + (d,))) for d in details]
    return list(dict.fromkeys(out + [tuple(sorted(e)) for e in extra]))


def people_block(records, main, details, extra=(), flags=()) -> dict[str, Any]:
    """Unique children (``child``) per grouping, and per flag the unique children with it set."""
    figures = []
    for by in groupings(main, details, extra):
        cells: dict[tuple, list[set]] = {}
        for r in records:
            sets = cells.setdefault(
                tuple(r.get(d, TEXT_DEFAULTS.get(d)) for d in by), [set() for _ in (None, *flags)]
            )
            sets[0].add(r["child"])
            for i, flag in enumerate(flags, start=1):
                if r.get(flag):
                    sets[i].add(r["child"])
        rows = [list(k) + [len(s) for s in sets] for k, sets in cells.items()]
        figures.append({"by": list(by), "rows": sorted(rows, key=_sort_key)})
    return {"measures": ["people", *flags], "figures": figures}


def text_key(value: str | None) -> str | None:
    """Compiler's outreach key: lower case, "_" for other characters, 40 characters, no "_" at the ends."""
    if value is None:
        return None
    key = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())[:40].strip("_")
    return key or None


def _named(items: list[dict[str, Any]], ids: set[Any]) -> list[dict[str, Any]]:
    return [item for item in items if item["id"] in ids]


def _ids(records, *names) -> set[Any]:
    return {r.get(n) for r in records for n in names if r.get(n) is not None}


def makani_payload(
    registrations: list[dict[str, Any]],
    staff: list[dict[str, Any]] = (),
    *,
    year: str = "2026",
    counted_at: str = "2026-09-29T09:04:37+03:00",
    centers: list[dict[str, Any]] = CENTERS,
    lists: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Makani figures of ``registrations`` (dicts with ``child``, ``center`` and the cube's dimensions and
    flags; partner and governorate default to the center's) and ``staff`` (``center``, ``sex``, ``active``)."""
    named = {**LISTS, **(lists or {})}
    by_id = {c["id"]: c for c in centers}
    records = []
    for r in registrations:
        center = by_id.get(r.get("center"), {})
        record = {"partner": center.get("partner"), "governorate": center.get("governorate"), **r}
        record["screened"] = 1 if record.get("malnutrition") is not None else 0
        records.append(record)
    staff_records = [
        {"partner": by_id[s["center"]]["partner"], "governorate": by_id[s["center"]]["governorate"], **s}
        for s in staff
    ]
    cubes = {
        "enrolment": cube(records, MAKANI_DIMS, MAKANI_MEASURES),
        "staff": cube(staff_records, STAFF_DIMS, ("staff",)),
    }
    used_centers = _ids(records + staff_records, "center")
    listed = [c for c in centers if c["id"] in used_centers]
    places = _ids(records + staff_records, "governorate") | _ids(
        listed, "governorate", "district", "cadaster"
    )
    return {
        "format": 2,
        "programme": "mscc",
        "label": "Makani (MSCC)",
        "year": year,
        "counted_at": counted_at,
        "rounds": [{"id": 1, "name": f"Round 1 {year}", "start_date": f"{year}-01-15", "current": True}],
        "partners": _named(named["partners"], _ids(records + staff_records + listed, "partner")),
        "locations": _named(named["locations"], places),
        "sites": [
            {k: c[k] for k in ("id", "name", "partner", "governorate", "district", "type")} for c in listed
        ],
        "centers": listed,
        "nationalities": _named(named["nationalities"], _ids(records, "nationality")),
        "disabilities": _named(named["disabilities"], _ids(records, "disability")),
        "id_types": _named(named["id_types"], _ids(records, "id_type")),
        "blocks": {
            "registrations": people_block(
                records,
                ("partner", "governorate"),
                ("center", "sex", "nationality", "disability"),
                (("sex", "nationality"),),
            ),
            "staff": people_block(
                [dict(s, child=i) for i, s in enumerate(staff_records)], ("partner", "governorate"), ("sex",)
            ),
        },
        "cubes": cubes,
    }


def dirasa_payload(
    registrations: list[dict[str, Any]],
    teachers: list[dict[str, Any]] = (),
    outreach: list[dict[str, Any]] = (),
    *,
    round_name: str = "Bridging 2025-2026",
    start_date: str = "2025-10-01",
    counted_at: str = "2026-09-29T09:07:25+03:00",
    schools: list[dict[str, Any]] = SCHOOLS,
    lists: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Dirasa figures of ``registrations`` (``child``, ``school``, ``partner``, the child's ``governorate``,
    the cube's dimensions and pre/post-test flags), ``teachers`` (``school``, ``sex``, ``trainings``: ids)
    and the Kobo ``outreach`` children (raw ``year``, ``partner``, ``governorate`` and answers)."""
    named = {**LISTS, **(lists or {})}
    records = [{"learning_result": "in_progress", **r} for r in registrations]
    trained = [
        {"school": t.get("school"), "sex": t.get("sex"), "training": k}
        for t in teachers
        for k in t.get("trainings", ())
    ]
    cubes = {
        "enrolment": cube(records, DIRASA_DIMS, DIRASA_MEASURES),
        "teachers": cube(list(teachers), ("school", "sex"), ("teachers",)),
        "trainings": cube(trained, ("school", "sex", "training"), ("teachers",)),
    }
    used = _ids(records + list(teachers), "school")
    registered: dict[Any, set] = {}
    for r in records:
        if r.get("school") is not None and r.get("partner") is not None:
            registered.setdefault(r["school"], set()).add(r["partner"])
    listed = []
    for s in schools:
        if s["id"] in used:
            school = {k: v for k, v in s.items() if k != "listed"}
            school["partners"] = sorted(registered.get(s["id"]) or s.get("listed") or ())
            listed.append(school)
    places = _ids(records, "governorate") | _ids(listed, "governorate", "district")
    partner_ids = _ids(records, "partner") | {p for s in listed for p in s["partners"]}
    outreach_rows = [
        {
            "year": o.get("year"),
            "partner": (o.get("partner") or "").strip() or None,
            "governorate": (o.get("governorate") or "").strip() or None,
            **{q: text_key(o.get(q)) for q in OUTREACH_QUESTIONS},
        }
        for o in outreach
    ]
    return {
        "format": 2,
        "programme": "bridging",
        "label": "Bridging (Dirasa)",
        "year": round_name,
        "counted_at": counted_at,
        "rounds": [{"id": 9, "name": round_name, "start_date": start_date, "current": True}],
        "partners": _named(named["partners"], partner_ids),
        "locations": _named(named["locations"], places),
        "sites": [
            {k: s[k] for k in ("id", "name", "number", "governorate", "district", "type")} for s in listed
        ],
        "schools": listed,
        "nationalities": _named(named["nationalities"], _ids(records, "nationality")),
        "disabilities": _named(named["disabilities"], _ids(records, "disability")),
        "trainings": named["trainings"],
        "blocks": {
            "registrations": people_block(
                records, ("partner", "governorate"), ("school", "sex"), flags=("pre_tested", "post_tested")
            ),
        },
        "cubes": cubes,
        "outreach": {
            "cubes": {
                q: cube([dict(o, value=o[q]) for o in outreach_rows], OUTREACH_DIMS, ("children",))
                for q in OUTREACH_QUESTIONS
            }
        },
    }


def format_one(payload: dict[str, Any]) -> dict[str, Any]:
    """The same figures as an older Compiler sends them: no cubes, no lists of format 2."""
    old = {
        k: v for k, v in payload.items() if k not in ("cubes", "centers", "schools", "id_types", "outreach")
    }
    return {**old, "format": 1}
