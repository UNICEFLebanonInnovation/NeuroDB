"""The Dirasa (Bridging) dashboard (/education/dirasa/), after the programme's Power BI pages: Overview,
Outreach, Attendance barriers, Schools map.

School filters (partner, governorate, school, school type, active during emergency) choose the
schools; the registrations counted are those of the chosen schools (and of the partner chosen), and
the child filters (CWD type, nationality, gender) apply to them only. The in-school figures are the
numbers each school reports about its own children, and teachers are those of the chosen schools:
child filters do not apply to them. Governorate is the school's for schools and the child's in the
"Enrolment by governorate" chart. Outreach is Compiler's Kobo outreach, shared by every programme,
with its own filters (year of the interview, partner and governorate as typed in the form).
"""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from .cube import Cube
from .figures import Figures
from .services import (
    ALL,
    NOT_SPECIFIED,
    Page,
    Slicer,
    barrier_label,
    colour,
    cwd_ids,
    id_options,
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


def options(figures: Figures) -> dict[str, list[tuple[str, str]]]:
    enrolment = figures.cube("enrolment")
    schools = _schools(figures)
    partners = enrolment.values("partner") | {p for s in schools for p in s.get("partners") or []}
    years = sorted((y for y in _outreach_values(figures, "year") if y), reverse=True)
    return {
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
    in the round, else the Dirasa partners listing it; "Not specified": a school without any)."""
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


def registrations(figures: Figures, filters: dict[str, Any], schools: list[dict[str, Any]]) -> Cube:
    """The enrolment rows of the chosen schools and partner, under the child filters."""
    where = {dim: filters[name] for name, dim in CHILD if name in filters}
    if "partner" in filters:
        where["partner"] = filters["partner"]
    if _by_school(filters):
        where["school"] = {s["id"] for s in schools}
    return figures.cube("enrolment").filter(**where)


def _of_schools(cube: Cube, filters: dict[str, Any], schools: list[dict[str, Any]]) -> Cube:
    """Teachers of the chosen schools (every teacher when no school or partner filter is set)."""
    if _by_school(filters) or "partner" in filters:
        return cube.filter(school={s["id"] for s in schools})
    return cube


def school_count(schools: list[dict[str, Any]], key: str) -> int | None:
    """The sum of a number the schools report; None when none of them reported it."""
    values = [(s.get("counts") or {}).get(key) for s in schools]
    values = [v for v in values if v is not None]
    return sum(values) if values else None


def build(figures: Figures, tab: str, filters: dict[str, Any]) -> dict[str, Any]:
    errors = [name for name in ("enrolment", "teachers", "trainings") if figures.cube(name).error]
    errors += [f"outreach {name}" for name in OUTREACH if figures.outreach(name).error]
    if tab == "outreach":
        return {"errors": errors, **_outreach(figures, filters)}
    schools = chosen_schools(figures, filters)
    chosen = registrations(figures, filters, schools)
    data: dict[str, Any] = {"errors": errors}
    if tab == "overview":
        by_nationality: dict[str, int] = {}
        for nid, count in chosen.by("nationality", "registrations").items():
            group = nationality_group(None if nid is None else figures.label_of("nationality", nid))
            by_nationality[group] = by_nationality.get(group, 0) + count
        types: dict[Any, int] = {}
        for school in schools:
            types[school.get("type")] = types.get(school.get("type"), 0) + 1
        lebanese = school_count(schools, "children_lebanese")
        others = school_count(schools, "children_non_lebanese")
        trainings = _of_schools(figures.cube("trainings"), filters, schools)
        with_disability = chosen.filter(disability=cwd_ids(figures, chosen.values("disability")))
        data["kpis"] = {
            "schools": len(chosen.distinct("school", "registrations")),
            "children": chosen.sum("registrations"),
            "in_school": school_count(schools, "children"),
            "cwd": with_disability.sum("registrations"),
            "in_school_cwd": school_count(schools, "cwd"),
            "teachers": _of_schools(figures.cube("teachers"), filters, schools).sum("teachers"),
        }
        data["charts"] = {
            "nationality": pairs(by_nationality, order=GROUPS),
            "gender": pairs(chosen.by("sex", "registrations"), category, order=[category(s) for s in SEXES]),
            "school_types": pairs(types),
            "in_school_nationality": [
                [label, value]
                for label, value in ((_("Lebanese children"), lebanese), (_("Non-Lebanese children"), others))
                if value
            ],
            "governorate": pairs(
                chosen.by("governorate", "registrations"), lambda v: figures.label_of("governorate", v)
            ),
            "trainings": pairs(
                trainings.by("training", "teachers"), lambda v: figures.label_of("training", v)
            ),
        }
    elif tab == "barriers":
        barriers = chosen.by("barrier", "registrations")
        data["kpis"] = {
            "children": chosen.sum("registrations"),
            "with_barrier": sum(count for value, count in barriers.items() if value is not None),
        }
        data["charts"] = {"barriers": pairs(barriers, barrier_label, unknown=False)}
    elif tab == "map":
        data.update(_map(figures, chosen, schools))
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
        "kpis": {"schools": len(schools), "children": chosen.sum("registrations")},
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


PAGE = Page(
    programme="bridging",
    name=gettext_lazy("Dirasa (Bridging)"),
    title=gettext_lazy("Dirasa Programme Dashboard"),
    subtitle=gettext_lazy(
        "Insights summary: children registered by the partners in Compiler's Bridging (Dirasa) "
        "programme, counted in Compiler (counts only)"
    ),
    url_name="education:dirasa",
    period_param="round",
    period_label=gettext_lazy("Round"),
    tabs=TABS,
    slicers=SLICERS,
    options=options,
    build=build,
)
