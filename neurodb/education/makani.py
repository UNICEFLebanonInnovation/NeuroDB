"""The Makani (MSCC) dashboard (/education/makani/), after the programme's Power BI pages: Overview,
Education, Health and nutrition, Maps.

The enrolment cube counts registrations, as the Power BI does ("Children enrolled"): a child
registered at two centers counts twice there. The unique count of children comes from the
registrations block, which only answers filters on partner, governorate and center. The education
programme and status, and the health and nutrition answers, are those of each registration's latest
record. Program staff is today's list (the record has no year), filtered by place only.
"""

from __future__ import annotations

from typing import Any

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from .figures import Figures
from .services import (
    NOT_SPECIFIED,
    Page,
    Slicer,
    choropleth,
    colour,
    cwd_ids,
    id_options,
    pairs,
    programme_family,
    text_options,
    tidy,
)

TABS = (
    ("overview", gettext_lazy("Overview")),
    ("education", gettext_lazy("Education")),
    ("health", gettext_lazy("Health and nutrition")),
    ("maps", gettext_lazy("Maps")),
)
SLICERS = (
    Slicer("disability", gettext_lazy("CWD type")),
    Slicer("service", gettext_lazy("Education services"), kind="text"),
    Slicer("nationality", gettext_lazy("Child nationality")),
    Slicer("gender", gettext_lazy("Child gender"), kind="text"),
    Slicer("caregiver", gettext_lazy("Caregiver"), kind="text"),
    Slicer("working", gettext_lazy("Working children"), kind="text"),
    Slicer("partner", gettext_lazy("Partner")),
    Slicer("governorate", gettext_lazy("Governorate")),
    Slicer("center", gettext_lazy("Center"), search=True),
    Slicer("emergency", gettext_lazy("Center active during emergency"), kind="text"),
)
SEXES = ("Female", "Male", NOT_SPECIFIED)
CAREGIVERS = ("Mother", "Father", "Other", NOT_SPECIFIED)
YES_NO = ("Yes", "No", NOT_SPECIFIED)
CHILD = (  # slicer -> enrolment dimension
    ("disability", "disability"),
    ("nationality", "nationality"),
    ("gender", "sex"),
    ("caregiver", "caregiver"),
    ("working", "working"),
)
PLACE = ("partner", "governorate", "center")  # the unique count's grouping holds these filters


def category(value: str) -> str:
    return _(NOT_SPECIFIED) if value == NOT_SPECIFIED else value


def _centers(figures: Figures) -> list[dict[str, Any]]:
    return figures.payload.get("centers") or []


def _emergency(center: dict[str, Any]) -> str:
    return center.get("emergency") or NOT_SPECIFIED


def options(figures: Figures) -> dict[str, list[tuple[str, str]]]:
    enrolment, staff = figures.cube("enrolment"), figures.cube("staff")
    return {
        "disability": id_options(figures, "disability", enrolment.values("disability")),
        "service": text_options({programme_family(v) for v in enrolment.values("programme")}),
        "nationality": id_options(figures, "nationality", enrolment.values("nationality")),
        "gender": text_options(enrolment.values("sex"), SEXES, category),
        "caregiver": text_options(enrolment.values("caregiver"), CAREGIVERS, category),
        "working": text_options(enrolment.values("working"), YES_NO, category),
        "partner": id_options(figures, "partner", enrolment.values("partner") | staff.values("partner")),
        "governorate": id_options(
            figures, "governorate", enrolment.values("governorate") | staff.values("governorate")
        ),
        "center": id_options(figures, "center", enrolment.values("center") | staff.values("center")),
        "emergency": text_options({_emergency(c) for c in _centers(figures)}, YES_NO, category),
    }


def _place_where(figures: Figures, filters: dict[str, Any]) -> dict[str, Any]:
    """Partner, governorate and center, and the centers active (or not) during an emergency."""
    where = {name: filters[name] for name in PLACE if name in filters}
    if "emergency" in filters:
        centers = {c["id"] for c in _centers(figures) if _emergency(c) == filters["emergency"]}
        where["center"] = centers & {where["center"]} if "center" in where else centers
    return where


def _where(figures: Figures, filters: dict[str, Any]) -> dict[str, Any]:
    where = {dim: filters[name] for name, dim in CHILD if name in filters}
    if "service" in filters:
        programmes = figures.cube("enrolment").values("programme")
        where["programme"] = {v for v in programmes if programme_family(v) == filters["service"]}
    return {**where, **_place_where(figures, filters)}


def unique_count(figures: Figures, filters: dict[str, Any]) -> int | None:
    """Unique children from the registrations block; None when the filters are not only partner,
    governorate or center (the block has no grouping for them)."""
    if set(filters) - set(PLACE) or any(v is None for v in filters.values()):
        return None
    table = figures.block("registrations").table(filters)
    return None if table is None else (table.get(()) or {}).get("people", 0)


def _families(counts: dict[Any, int]) -> dict[Any, int]:
    out: dict[Any, int] = {}
    for programme, count in counts.items():
        family = programme_family(programme)
        out[family] = out.get(family, 0) + count
    return out


def _malnutrition(value: str) -> str:
    text = tidy(value)
    return _("No malnutrition") if text.casefold() == "no malnutrition screening" else text


def _delay(value: str) -> str | None:
    return None if value.strip().casefold() == "no" else tidy(value)


def build(figures: Figures, tab: str, filters: dict[str, Any]) -> dict[str, Any]:
    enrolment = figures.cube("enrolment")
    chosen = enrolment.filter(**_where(figures, filters))
    kpis: dict[str, Any] = {
        "centers": len(chosen.distinct("center", "registrations")),
        "children": chosen.sum("registrations"),
        "cwd": chosen.filter(disability=cwd_ids(figures, chosen.values("disability"))).sum("registrations"),
    }
    errors = [name for name in ("enrolment", "staff") if figures.cube(name).error]
    if figures.block("registrations").error:
        errors.append(_("unique count"))
    data: dict[str, Any] = {"kpis": kpis, "errors": errors}
    if tab == "overview":
        kpis.update(
            unique=unique_count(figures, filters),
            working=chosen.filter(working="Yes").sum("registrations"),
            married=chosen.sum("married"),
            counselling=chosen.sum("counselling"),
            idp=chosen.sum("idp"),
            staff=figures.cube("staff").filter(**_place_where(figures, filters)).sum("staff"),
        )
        data["charts"] = {
            "gender": pairs(chosen.by("sex", "registrations"), category, order=[category(s) for s in SEXES]),
            "governorate": pairs(
                chosen.by("governorate", "registrations"), lambda v: figures.label_of("governorate", v)
            ),
            "nationality": pairs(
                chosen.by("nationality", "registrations"), lambda v: figures.label_of("nationality", v)
            ),
            "programme": pairs(_families(chosen.by("programme", "registrations")), unknown=False),
            "package": pairs(chosen.by("package", "registrations"), tidy),
        }
    elif tab == "education":
        data["charts"] = {
            "programme_level": pairs(chosen.by("programme", "registrations"), tidy, unknown=False),
            "id_type": pairs(chosen.by("id_type", "registrations"), lambda v: figures.label_of("id_type", v)),
            "education_status": pairs(chosen.by("education_status", "registrations"), tidy, unknown=False),
        }
    elif tab == "health":
        kpis.update(
            immunized=chosen.sum("immunized"),
            screened=chosen.sum("screened"),
            minimum_meals=chosen.sum("minimum_meals"),
            vaccinated=chosen.sum("vaccinated"),
        )
        data["charts"] = {
            "malnutrition": pairs(chosen.by("malnutrition", "registrations"), _malnutrition, unknown=False),
            "delays": pairs(chosen.by("delay", "registrations"), _delay, unknown=False),
        }
    elif tab == "maps":
        data.update(_maps(figures, chosen))
    return data


def _maps(figures: Figures, chosen) -> dict[str, Any]:
    by_governorate = chosen.by("governorate", "registrations")
    known = {k: v for k, v in by_governorate.items() if k is not None}
    config, governorates = choropleth(figures, known, _("children"))
    by_center = chosen.by("center", "registrations")
    centers = [(figures.centers.get(cid) or {"id": cid}, count) for cid, count in by_center.items() if cid]
    centers.sort(key=lambda item: (-item[1], str(item[0].get("name") or "")))
    groups = sorted(  # by name, an unknown governorate last
        {c.get("governorate") for c, _count in centers},
        key=lambda g: (g is None, figures.label_of("governorate", g).casefold()),
    )
    colours = {g: colour(i) for i, g in enumerate(groups)}
    points, rows = [], []
    for center, count in centers:
        name = center.get("name") or f"#{center['id']}"
        partner = figures.label_of("partner", center.get("partner")) if center.get("partner") else "—"
        governorate = figures.label_of("governorate", center.get("governorate"))
        emergency = category(_emergency(center))
        located = center.get("latitude") is not None and center.get("longitude") is not None
        rows.append(
            {
                "name": name,
                "partner": partner,
                "governorate": governorate,
                "children": count,
                "emergency": emergency,
                "located": located,
            }
        )
        if located:
            points.append(
                {
                    "name": name,
                    "latitude": center["latitude"],
                    "longitude": center["longitude"],
                    "color": colours[center.get("governorate")],
                    "group": governorate,
                    "lines": [
                        [_("Partner"), partner],
                        [_("Governorate"), governorate],
                        [_("Children"), f"{count:,}"],
                        [_("Active during emergency"), emergency],
                    ],
                }
            )
    located_groups = {p["group"] for p in points}
    legend = [
        {"label": figures.label_of("governorate", g), "color": colours[g]}
        for g in groups
        if figures.label_of("governorate", g) in located_groups
    ]
    return {
        "choropleth": config,
        "governorate_rows": governorates,
        "unknown_governorate": by_governorate.get(None, 0),
        "points": {
            "mode": "points",
            "points": points,
            "legend": legend,
            "legend_title": _("Governorate"),
            "empty_title": _("No center with a GPS point under these filters"),
        },
        "center_rows": rows,
        "unlocated": sum(1 for r in rows if not r["located"]),
    }


PAGE = Page(
    programme="mscc",
    name=gettext_lazy("Makani (MSCC)"),
    title=gettext_lazy("Makani Programme Dashboard"),
    subtitle=gettext_lazy(
        "Insights summary: children registered by the partners in Compiler's Makani (MSCC) programme, "
        "counted in Compiler (counts only)"
    ),
    url_name="education:makani",
    period_param="year",
    period_label=gettext_lazy("Year"),
    tabs=TABS,
    slicers=SLICERS,
    options=options,
    build=build,
)
