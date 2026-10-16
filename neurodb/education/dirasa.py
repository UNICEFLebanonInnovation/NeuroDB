"""The Dirasa (Bridging) dashboard (/education/dirasa/), after the programme's Power BI pages: Overview,
Outreach, Attendance barriers, Schools map.

The page shows a year (payload format 4: the CLM rounds whose Bridging start date is in the year, a round
without one by the year in its name, or those Compiler marks current) or a round (older payloads). The line
under the title lists the year's rounds (:func:`rounds_line`) and the Round slicer shows one of them (from
format 4; never on Outreach). A child registered in two rounds of the year counts once in the children card
and twice in the registrations. The rows counted are those of Power BI's view ``vw_bridging_data`` for the
period (from payload format 3, when Compiler could use the view; otherwise Compiler's own rule): the line
under the key figures says which (:func:`neurodb.education.power_bi.note`). School filters (partner,
governorate, school, school type, active during emergency) choose the schools; the registrations counted are
those of the chosen schools (and of the partner chosen), and the child filters (CWD type, nationality, gender)
apply to them only. The school-level figures (in-school children and CWD, type of schools, in-school
nationality, teacher trainings, the map) are those of the schools that have registrations under the filters,
as "Dirasa schools" counts them: a school listed only for its teachers is not one of them. The in-school
figures are the numbers each school reports about its own children. Governorate is the school's for schools
and the child's in the "Enrolment by governorate" chart. Outreach is Compiler's Kobo outreach, shared by every
programme, with its own filters (year of the interview, partner and governorate as typed in the form), not
from the view.

The children card counts unique children by default (Power BI's card is below its nationality donut's total,
which counts registrations: SPEC_PBI E19) and shows the registrations as a second line; the unique count
answers the partner, school and gender filters, and the card shows registrations under the others. The cards
whose Power BI measure is unknown are counted with the version an administrator chose
(:data:`neurodb.education.power_bi.VARIANTS`). The charts leave "Not specified" out, as Power BI's do.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from . import power_bi
from .cube import Cube
from .figures import Figures
from .services import (
    ALL,
    NOT_SPECIFIED,
    Page,
    Slicer,
    barrier_label,
    colour,
    id_options,
    known,
    nationality_group,
    outreach_label,
    pairs,
    text_options,
)

TABS = (
    ("overview", gettext_lazy("Overview")),
    ("outreach", gettext_lazy("Outreach")),
    ("barriers", gettext_lazy("Attendance barriers")),
    ("map", gettext_lazy("Schools map")),
)
CHILD_TABS = ("overview", "barriers")
SCHOOL_TABS = ("overview", "barriers", "map")
OUTREACH_SLICER = {"tabs": ("outreach",), "kind": "text", "own_data": True}  # the Kobo form's own values
SLICERS = (
    # the rounds of the year (payload format 4); a payload counted per round has no round to choose: no slicer
    Slicer("round", gettext_lazy("Round"), tabs=SCHOOL_TABS, only_with_options=True),
    Slicer("disability", gettext_lazy("CWD type"), tabs=CHILD_TABS),
    Slicer("nationality", gettext_lazy("Nationality"), tabs=CHILD_TABS),
    Slicer("gender", gettext_lazy("Gender"), tabs=CHILD_TABS, kind="text"),
    Slicer("school", gettext_lazy("School"), tabs=CHILD_TABS, search=True),
    Slicer("partner", gettext_lazy("Partner"), tabs=SCHOOL_TABS),
    Slicer("governorate", gettext_lazy("Governorate"), tabs=SCHOOL_TABS),
    Slicer("type", gettext_lazy("School type"), tabs=SCHOOL_TABS, kind="text"),
    Slicer("emergency", gettext_lazy("School active during emergency"), tabs=SCHOOL_TABS, kind="text"),
    Slicer("outreach_year", gettext_lazy("Year"), required=True, **OUTREACH_SLICER),
    Slicer("outreach_partner", gettext_lazy("Partner"), **OUTREACH_SLICER),
    Slicer("outreach_governorate", gettext_lazy("Governorate"), **OUTREACH_SLICER),
)
SEXES = ("Female", "Male", NOT_SPECIFIED)
YES_NO = ("Yes", "No", NOT_SPECIFIED)
GROUPS = ("Syrian", "Lebanese", "Non-Lebanese")
CHILD = (("disability", "disability"), ("nationality", "nationality"), ("gender", "sex"))
SCHOOL_FILTERS = ("school", "governorate", "type", "emergency")
OUTREACH = ("education_status", "referral", "dropout_reason", "id_type")


def category(value: str) -> str:
    return _(NOT_SPECIFIED) if value == NOT_SPECIFIED else value


def _schools(figures: Figures) -> list[dict[str, Any]]:
    return figures.payload.get("schools") or []


def _emergency(school: dict[str, Any]) -> str:
    return school.get("emergency") or NOT_SPECIFIED


def _outreach_values(figures: Figures, dim: str) -> set[Any]:
    return set().union(*(figures.outreach(name).values(dim) for name in OUTREACH))


def _school_options(figures: Figures) -> list[tuple[str, str]]:
    """The schools by name, their number added where two share a name."""
    schools = _schools(figures)
    names = [str(s.get("name") or f"#{s['id']}") for s in schools]
    shared = {n for n in names if names.count(n) > 1}
    options = [
        (str(s["id"]), f"{n} ({s['number']})" if n in shared and s.get("number") else n)
        for s, n in zip(schools, names, strict=True)
    ]
    return sorted(options, key=lambda option: option[1].casefold())


def _round_options(figures: Figures) -> list[tuple[str, str]]:
    """The payload's rounds that have registrations or teachers, in the payload's order (by Bridging start),
    when the enrolment cube is by round (payload format 4); none otherwise."""
    enrolment = figures.cube("enrolment")
    if "round" not in enrolment.dims:
        return []
    teachers = figures.cube("teachers")
    used = enrolment.distinct("round", "registrations") | teachers.distinct("round", "teachers")
    return [
        (str(r["id"]), str(r.get("name") or f"#{r['id']}"))
        for r in figures.payload.get("rounds") or []
        if isinstance(r, dict) and r.get("id") in used
    ]


def options(figures: Figures) -> dict[str, list[tuple[str, str]]]:
    enrolment = figures.cube("enrolment")
    schools = _schools(figures)
    partners = enrolment.values("partner") | {p for s in schools for p in s.get("partners") or []}
    years = sorted((y for y in _outreach_values(figures, "year") if y), reverse=True)
    return {
        "round": _round_options(figures),
        "disability": id_options(figures, "disability", enrolment.values("disability")),
        "nationality": id_options(figures, "nationality", enrolment.values("nationality")),
        "gender": text_options(enrolment.values("sex"), SEXES, category),
        "school": _school_options(figures),
        "partner": id_options(figures, "partner", partners),
        "governorate": id_options(figures, "governorate", {s.get("governorate") for s in schools}),
        "type": text_options({s.get("type") for s in schools}),
        "emergency": text_options({_emergency(s) for s in schools}, YES_NO, category),
        "outreach_year": [(y, y) for y in years] + [(ALL, _("All years"))],
        "outreach_partner": text_options(_outreach_values(figures, "partner")),
        "outreach_governorate": text_options(_outreach_values(figures, "governorate")),
    }


def chosen_schools(figures: Figures, filters: dict[str, Any]) -> list[dict[str, Any]]:
    """The schools under the school filters (a school's partners: those that registered children there
    in the period, else the Dirasa partners listing it; "Not specified": a school without any)."""
    out = []
    for school in _schools(figures):
        if "school" in filters and school["id"] != filters["school"]:
            continue
        if "partner" in filters and filters["partner"] not in (school.get("partners") or [None]):
            continue
        if "governorate" in filters and school.get("governorate") != filters["governorate"]:
            continue
        if "type" in filters and school.get("type") != filters["type"]:
            continue
        if "emergency" in filters and _emergency(school) != filters["emergency"]:
            continue
        out.append(school)
    return out


def _by_school(filters: dict[str, Any]) -> bool:
    return any(name in filters for name in SCHOOL_FILTERS)


def in_round(cube: Cube, filters: dict[str, Any]) -> Cube:
    """The cube's rows of the round chosen (payload format 4), when the cube is by round; the cube itself
    otherwise (a cube without the dimension would match no row)."""
    if "round" in filters and "round" in cube.dims:
        return cube.filter(round=filters["round"])
    return cube


def registrations(figures: Figures, filters: dict[str, Any], schools: list[dict[str, Any]]) -> Cube:
    """The enrolment rows of the chosen schools and partner (and round), under the child filters."""
    where = {dim: filters[name] for name, dim in CHILD if name in filters}
    if "partner" in filters:
        where["partner"] = filters["partner"]
    if _by_school(filters):
        where["school"] = {s["id"] for s in schools}
    return in_round(figures.cube("enrolment").filter(**where), filters)


def of_schools(cube: Cube, filters: dict[str, Any], schools: list[dict[str, Any]]) -> Cube:
    """Teachers of the chosen schools (every teacher when no school or partner filter is set), of the round
    chosen."""
    if _by_school(filters) or "partner" in filters:
        cube = cube.filter(school={s["id"] for s in schools})
    return in_round(cube, filters)


def school_count(schools: list[dict[str, Any]], key: str) -> int | None:
    """The sum of a number the schools report; None when none of them reported it."""
    values = [(s.get("counts") or {}).get(key) for s in schools]
    values = [v for v in values if v is not None]
    return sum(values) if values else None


@dataclass(frozen=True)
class Selection:
    """What the filters choose: the schools (school filters), their registrations (and the partner's, under
    the child filters), and the schools among them that have registrations under the filters."""

    schools: list[dict[str, Any]]
    chosen: Cube
    with_children: list[dict[str, Any]]


def selection(figures: Figures, filters: dict[str, Any]) -> Selection:
    """:class:`Selection`, worked out once per payload reader and filters (the cards and charts read it)."""

    def choose() -> Selection:
        schools = chosen_schools(figures, filters)
        chosen = registrations(figures, filters, schools)
        ids = chosen.distinct("school", "registrations")
        return Selection(schools, chosen, [s for s in schools if s["id"] in ids])

    return power_bi.memo(figures, "dirasa:selection", filters, choose)


# slicer -> the block's grouping
BLOCK_FILTERS = {"partner": "partner", "school": "school", "gender": "sex", "round": "round"}


def unique_children(figures: Figures, filters: dict[str, Any]) -> int | None:
    """Unique children from the registrations block; None when it has no grouping for the filters (it answers
    the partner, the school and the gender, alone or the partner with one of the two; from payload format 4
    the round alone, with the partner or with the gender) or a filter is "Not specified". Without the round, a
    child registered in two rounds of the year counts once."""
    if set(filters) - set(BLOCK_FILTERS) or any(v is None for v in filters.values()):
        return None
    block = figures.block("registrations")
    if block.error:
        return None
    table = block.table({BLOCK_FILTERS[name]: value for name, value in filters.items()})
    return None if table is None else (table.get(()) or {}).get("people", 0)


CARDS = ("children", "cwd", "in_school", "in_school_cwd", "teachers")  # power_bi.VARIANTS["bridging"]


def _children_card(figures: Figures, filters: dict[str, Any], chosen: Cube) -> dict[str, Any]:
    """The children card (the version chosen) with both counts, for its second line."""
    card = power_bi.card_value("bridging", "children", figures, filters)
    return {**card, "unique": unique_children(figures, filters), "registrations": chosen.sum("registrations")}


def build(figures: Figures, tab: str, filters: dict[str, Any]) -> dict[str, Any]:
    errors = [name for name in ("enrolment", "teachers", "trainings") if figures.cube(name).error]
    errors += [f"outreach {name}" for name in OUTREACH if figures.outreach(name).error]
    if tab == "outreach":
        return {"errors": errors, **_outreach(figures, filters)}
    chosen = selection(figures, filters)
    data: dict[str, Any] = {"errors": errors}
    if tab == "overview":
        cards = {name: power_bi.card_value("bridging", name, figures, filters) for name in CARDS[1:]}
        cards["children"] = _children_card(figures, filters, chosen.chosen)
        by_nationality: dict[str, int] = {}
        for nid, count in chosen.chosen.by("nationality", "registrations").items():
            if nid is None:  # "Not specified" left out, as Power BI's donut
                continue
            group = nationality_group(figures.label_of("nationality", nid))
            by_nationality[group] = by_nationality.get(group, 0) + count
        types: dict[Any, int] = {}
        for school in chosen.with_children:
            types[school.get("type")] = types.get(school.get("type"), 0) + 1
        lebanese = school_count(chosen.with_children, "children_lebanese")
        others = school_count(chosen.with_children, "children_non_lebanese")
        trainings = in_round(
            figures.cube("trainings").filter(school={s["id"] for s in chosen.with_children}), filters
        )
        data["cards"] = cards
        data["kpis"] = {
            "schools": len(chosen.chosen.distinct("school", "registrations")),
            **{name: cards[name]["value"] for name in CARDS},
        }
        data["charts"] = {
            "nationality": pairs(by_nationality, order=GROUPS),
            "gender": pairs(
                chosen.chosen.by("sex", "registrations"),
                known(category),
                order=[category(s) for s in SEXES if s != NOT_SPECIFIED],
                unknown=False,
            ),
            "school_types": pairs(types, unknown=False),
            "in_school_nationality": [
                [label, value]
                for label, value in ((_("Lebanese children"), lebanese), (_("Non-Lebanese children"), others))
                if value
            ],
            "governorate": pairs(
                chosen.chosen.by("governorate", "registrations"),
                lambda v: figures.label_of("governorate", v),
                unknown=False,
            ),
            "trainings": pairs(
                trainings.by("training", "teachers"), lambda v: figures.label_of("training", v)
            ),
        }
    elif tab == "barriers":
        barriers = chosen.chosen.by("barrier", "registrations")
        card = _children_card(figures, filters, chosen.chosen)
        data["cards"] = {"children": card}
        data["kpis"] = {
            "children": card["value"],
            "with_barrier": sum(count for value, count in barriers.items() if value is not None),
        }
        data["charts"] = {"barriers": pairs(barriers, barrier_label, unknown=False)}
    elif tab == "map":
        data.update(_map(figures, chosen.chosen, chosen.with_children))
        card = _children_card(figures, filters, chosen.chosen)
        data["cards"] = {"children": card}
        data["kpis"]["children"] = card["value"]
    return data


def _outreach(figures: Figures, filters: dict[str, Any]) -> dict[str, Any]:
    where = {
        dim: filters[name]
        for name, dim in (
            ("outreach_year", "year"),
            ("outreach_partner", "partner"),
            ("outreach_governorate", "governorate"),
        )
        if name in filters
    }
    cubes = {name: figures.outreach(name).filter(**where) for name in OUTREACH}
    return {
        "kpis": {
            "outreached": cubes["education_status"].sum("children"),
            "year": filters.get("outreach_year"),
        },
        "charts": {
            name: pairs(cube.by("value", "children"), outreach_label, unknown=False)
            for name, cube in cubes.items()
        },
    }


def _map(figures: Figures, chosen: Cube, schools: list[dict[str, Any]]) -> dict[str, Any]:
    by_school = chosen.by("school", "registrations")
    types = sorted(
        {s.get("type") or NOT_SPECIFIED for s in schools}, key=lambda t: (t == NOT_SPECIFIED, t.casefold())
    )
    colours = {t: colour(i) for i, t in enumerate(types)}
    points, rows = [], []
    for school in schools:
        name = school.get("name") or f"#{school['id']}"
        kind = school.get("type") or NOT_SPECIFIED
        partners = ", ".join(figures.label_of("partner", p) for p in school.get("partners") or []) or "—"
        governorate = figures.label_of("governorate", school.get("governorate"))
        children = by_school.get(school["id"], 0)
        in_school = (school.get("counts") or {}).get("children")
        located = school.get("latitude") is not None and school.get("longitude") is not None
        rows.append(
            {
                "name": name,
                "type": category(kind),
                "partners": partners,
                "governorate": governorate,
                "children": children,
                "in_school": in_school,
                "located": located,
            }
        )
        if located:
            points.append(
                {
                    "name": name,
                    "latitude": school["latitude"],
                    "longitude": school["longitude"],
                    "color": colours[kind],
                    "group": category(kind),
                    "lines": [
                        [_("Partners"), partners],
                        [_("Governorate"), governorate],
                        [_("School type"), category(kind)],
                        [_("Children registered"), f"{children:,}"],
                        [_("In-school children"), "—" if in_school is None else f"{in_school:,}"],
                    ],
                }
            )
    rows.sort(key=lambda r: (-r["children"], str(r["name"]).casefold()))
    located_types = {p["group"] for p in points}
    return {
        "kpis": {"schools": len(schools)},
        "points": {
            "mode": "points",
            "points": points,
            "legend": [
                {"label": category(t), "color": colours[t]} for t in types if category(t) in located_types
            ],
            "legend_title": _("School type"),
            "empty_title": _("No school with a GPS point under these filters"),
        },
        "school_rows": rows,
        "unlocated": sum(1 for r in rows if not r["located"]),
    }


def _date(text: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(text)[:10]) if text else None
    except ValueError:
        return None


def rounds_line(figures: Figures) -> dict[str, Any] | None:
    """The rounds of the year, for the line under the title (payload format 4; None before): whether they are
    the rounds Compiler marks current (setting ``current_rounds``) and, per round in the payload's order, its
    name, Bridging start date, whether its year comes from its name, and its registrations (None when Compiler
    could not count the enrolment cube: the line then gives no count). ``other``: the registrations counted
    whose own round is not one of the year's, or that have none (view mode: the view's rows of the year may
    hold them, CONTRACT_PBI §9.3), so that the line adds up to the registrations shown."""
    if (figures.payload.get("format") or 0) < power_bi.YEARLY_FORMAT:
        return None
    base = ((figures.payload.get("power_bi") or {}).get("base")) or {}
    enrolment = figures.cube("enrolment")
    counted = not enrolment.error
    counts = enrolment.by("round", "registrations")
    entries = [entry for entry in figures.payload.get("rounds") or [] if isinstance(entry, dict)]
    rounds = [
        {
            "name": str(entry.get("name") or f"#{entry.get('id')}"),
            "start": _date(entry.get("start_date")),
            "from_name": entry.get("year_from") == "name",
            "registrations": counts.get(entry.get("id"), 0) if counted else None,
        }
        for entry in entries
    ]
    listed = {entry.get("id") for entry in entries}
    other = sum(n for round_id, n in counts.items() if round_id not in listed) if counted else 0
    return {"current": base.get("period_rule") == "current_rounds", "rounds": rounds, "other": other}


PAGE = Page(
    programme="bridging",
    name=gettext_lazy("Dirasa (Bridging)"),
    title=gettext_lazy("Dirasa Programme Dashboard"),
    subtitle=gettext_lazy(
        "Insights summary: children registered by the partners in Compiler's Bridging (Dirasa) "
        "programme, counted in Compiler (counts only)"
    ),
    url_name="education:dirasa",
    # the period of payloads before format 4 (one round); from format 4 the page selects a year
    # (services.period_field)
    period_param="round",
    period_label=gettext_lazy("Round"),
    tabs=TABS,
    slicers=SLICERS,
    options=options,
    build=build,
    own_tabs=("outreach",),
    rounds=rounds_line,
)
