"""What Compiler counted, compared with the views of its database that Power BI reads (counts only).

Power BI's Makani, Dirasa and Youth dashboards read ``vw_mscc_data``, ``vw_bridging_data`` and
``vw_youth_data_all``. Compiler counts the rows of those views (or, when a view cannot be used, its own
rule) and says how in each payload's ``power_bi`` block: which way it counted and why, the views' masked
definitions and columns, a check of the view's key, and a reconciliation in counts (view rows, what NeuroDB
counted, what it counted before, and the reasons for every difference). Education payloads carry it from
format 3, youth payloads from format 2; an older payload has none ("not compared yet").

The sync keeps the block in small rows (:func:`store`), so Data health never reads the 1-5 MB payloads:
``PowerBiCheck`` per programme and period, ``PowerBiView`` per view described, with the date its definition
last changed. No row about a child or a young person is ever in the block: ids stay in Compiler.

**Card versions.** The view gives the rows Power BI starts from, not what each card counts over them (a DAX
measure NeuroDB cannot read). For the cards whose measure is unknown, :data:`VARIANTS` names every candidate
("version") and how to count it from a payload under a page's filters; the default is today's definition,
except where the evidence is certain (SPEC_PBI §5: Dirasa's children card counts unique children). An
administrator types the number Power BI shows (admin → *Power BI cards*), Data health marks the version that
gives it (:func:`health`), and the administrator picks it there: the pages read the choice at once
(:func:`card_value`; the page caches hold :func:`cards_stamp`), and so does the country programme's total
(:func:`children_total`). A version a payload cannot answer (a measure of a newer format, a filter its
grouping does not hold) gives None, and the page shows the default instead, saying so.

**The page line** (:func:`note`): whether the figures were counted from Power BI's view or from Compiler's own
rule, why, and when Compiler counted.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _

from neurodb.core import compute

from .models import EducationFigures, PowerBiCard, PowerBiCheck, PowerBiView

logger = logging.getLogger(__name__)

VERSION = 1  # the version of the power_bi block this NeuroDB was written for (a newer one only adds keys)
DEFINITION_CHARS = 70_000  # Compiler caps the masked definition at 64 KB; anything longer is cut here too
ERROR_CHARS, MAX_ERRORS = 500, 50  # the parts Compiler could not compare (an error class and a short message)
ALL = "all"  # the youth period of every year together (YouthFigures.ALL)
EDUCATION = ("mscc", "bridging")  # the programmes whose figures are EducationFigures (youth's: YouthFigures)
PROGRAMMES = (  # in the order Data health lists them, with the page each one's figures are drawn on
    ("mscc", _("Makani (MSCC)"), "education:makani"),
    ("bridging", _("Dirasa (Bridging)"), "education:dirasa"),
    ("youth", _("Youth programmes"), "youth:dashboard"),
)
HEALTH_PERIODS = 3  # the latest periods per programme on Data health
YEAR_RULES = ("start_year", "current_rounds")  # Dirasa's period rules from format 4 (format 3: "round")
STALE_AFTER = timedelta(minutes=5)  # a check older than the figures' count by this much is of an older count


# ------------------------------------------------------------------------------------------ card versions
@dataclass(frozen=True)
class Card:
    """The definitions ("variants", shown as versions) one card can be counted with: its default and, per
    variant, the value under a page's filters from the payload's reader (None when that variant cannot answer
    them). ``fallback``: the variant shown when neither the chosen one nor the default can answer the filters
    (Dirasa's unique children answer a few filters only). ``label`` names the card, ``names`` each variant."""

    default: str
    variants: dict[str, Callable[[Any, dict[str, Any]], int | None]] = field(default_factory=dict)
    label: Any = ""
    names: dict[str, Any] = field(default_factory=dict)
    fallback: str = ""


def memo(figures: Any, name: str, filters: dict[str, Any], compute_it: Callable[[], Any]) -> Any:
    """``compute_it()`` once per payload reader, ``name`` and filters: a page's cards and charts read the same
    rows (the enrolment cube kept under the filters, the schools chosen)."""
    store = figures.__dict__.setdefault("_memo", {})
    key = (name, tuple(sorted(filters.items(), key=lambda item: item[0])))
    if key not in store:
        store[key] = compute_it()
    return store[key]


def _measure(cube: Any, name: str) -> int | None:
    """The sum of a cube measure; None when the cube does not have it (a measure of a newer format, or
    ``view_rows`` in rule mode)."""
    return cube.sum(name) if name in cube.measures else None


def _related_total(
    figures: Any, view: str, filters: dict[str, Any], *, this_round: bool = False
) -> int | None:
    """The rows of a related view Compiler profiled (``power_bi.related``), at the period total only (the
    profile is not by partner or place): None under a filter, in a past period's payload (no profile), or when
    the profile was cut or failed. ``this_round``: only the rows of the payload's rounds (the round's, or from
    payload format 4 the rounds of the year), when the profile is by round."""
    if filters:
        return None
    related = ((figures.payload.get("power_bi") or {}).get("related")) or {}
    profile = related.get(view)
    if not isinstance(profile, dict) or not profile.get("exists") or profile.get("truncated"):
        return None
    by, measures = list(profile.get("by") or []), list(profile.get("measures") or [])
    if "rows" not in measures or profile.get("error"):
        return None
    at = len(by) + measures.index("rows")
    rows = profile.get("rows") or []
    if this_round and "round_id" in by:
        rounds = {r.get("id") for r in figures.payload.get("rounds") or []}
        rows = [row for row in rows if row[by.index("round_id")] in rounds]
    return sum(row[at] or 0 for row in rows)


def _cubes(count: Callable[..., int | None]) -> Callable[..., int | None]:
    """A version counted from the cubes: None for a payload without them (format 1)."""

    def value(figures: Any, filters: dict[str, Any], *args: Any) -> int | None:
        return count(figures, filters, *args) if figures.has_cubes else None

    return value


# Makani (vw_mscc_data): the enrolment cube under the page's filters (makani.chosen_rows)
def _makani():
    from . import makani

    return makani


@_cubes
def _mscc_children(figures: Any, filters: dict[str, Any], measure: str) -> int | None:
    return _measure(_makani().chosen_rows(figures, filters), measure)


@_cubes
def _mscc_cwd(figures: Any, filters: dict[str, Any], blank: bool) -> int | None:
    from .services import cwd_ids

    rows = _makani().chosen_rows(figures, filters)
    ids = cwd_ids(figures, rows.values("disability")) | ({None} if blank else set())
    return rows.filter(disability=ids).sum("registrations")


@_cubes
def _mscc_working(figures: Any, filters: dict[str, Any], not_no: bool) -> int | None:
    rows = _makani().chosen_rows(figures, filters)
    if not_no:  # "Yes" and the answers left blank ("Not specified"): Compiler's own rule is <> 'No'
        return rows.sum("registrations") - rows.filter(working="No").sum("registrations")
    return rows.filter(working="Yes").sum("registrations")


@_cubes
def _mscc_staff(figures: Any, filters: dict[str, Any], which: str) -> int | None:
    staff = _makani().staff_rows(figures, filters)
    if which == "active":
        return staff.filter(active="Yes").sum("staff")
    if which == "with_children":
        centers = _makani().chosen_rows(figures, filters).distinct("center", "registrations")
        return staff.filter(center=centers).sum("staff")
    return staff.sum("staff")


def _mscc_staff_view(figures: Any, filters: dict[str, Any]) -> int | None:
    return _related_total(figures, "vw_center_program_staff", filters)


# Dirasa (vw_bridging_data): the schools and registrations chosen by the page's filters (dirasa.selection)
def _dirasa():
    from . import dirasa

    return dirasa


@_cubes
def _bridging_children(figures: Any, filters: dict[str, Any], which: str) -> int | None:
    if which == "unique":
        return _dirasa().unique_children(figures, filters)
    return _measure(_dirasa().selection(figures, filters).chosen, which)


@_cubes
def _bridging_cwd(figures: Any, filters: dict[str, Any]) -> int | None:
    from .services import cwd_ids

    chosen = _dirasa().selection(figures, filters).chosen
    return chosen.filter(disability=cwd_ids(figures, chosen.values("disability"))).sum("registrations")


@_cubes
def _bridging_schools(figures: Any, filters: dict[str, Any], key: str, scope: str) -> int | None:
    """A number the schools report, summed over the schools with registrations under the filters (``round``)
    or, at the period total only, over every school a Dirasa partner lists (``listed``, Compiler's
    ``school_totals``, format 3)."""
    if scope == "round":
        return _dirasa().school_count(_dirasa().selection(figures, filters).with_children, key)
    if filters:
        return None
    totals = (figures.payload.get("school_totals") or {}).get("listed")
    return totals.get(key) if isinstance(totals, dict) else None


@_cubes
def _bridging_teachers(figures: Any, filters: dict[str, Any], which: str) -> int | None:
    """The teachers of the period's rounds (of the round chosen, from payload format 4) at the schools chosen
    or at the schools with registrations under the filters."""
    dirasa = _dirasa()
    chosen = dirasa.selection(figures, filters)
    teachers = figures.cube("teachers")
    if which == "with_children":
        at = teachers.filter(school={s["id"] for s in chosen.with_children})
        return dirasa.in_round(at, filters).sum("teachers")
    return dirasa.of_schools(teachers, filters, chosen.schools).sum("teachers")


def _bridging_teachers_view(figures: Any, filters: dict[str, Any]) -> int | None:
    return _related_total(figures, "vw_teacher_data", filters, this_round=True)


# Youth (vw_youth_data_all): the figures (unique young people enrolled under a master indicator) and the
# blocks of format 2; ``filters`` are the page's (master, partner, donor, governorate)
def _youth_block_value(figures: Any, filters: dict[str, Any], block: str, measure: str) -> int | None:
    from .figures import Block

    data = Block((figures.payload.get("blocks") or {}).get(block) or {})
    if measure not in data.measures or data.error:
        return None
    table = data.table(filters)
    return None if table is None else (table.get(()) or {}).get(measure) or 0


def _youth_reached(figures: Any, filters: dict[str, Any], which: str) -> int | None:
    if which == "enrolled_people":
        return figures.count(filters)
    if which == "enrolments":
        return _youth_block_value(figures, filters, "enrolments", "enrolments")
    measure = "people" if which == "registered_people" else "registrations"
    return _youth_block_value(figures, filters, "registered", measure)


def _youth_partners(figures: Any, filters: dict[str, Any], blank: bool) -> int | None:
    table = figures.table(filters, "partner") or {}
    return len(table) if blank else sum(1 for key in table if key[0] is not None)


def _of(count: Callable[..., int | None], *args: Any) -> Callable[[Any, dict[str, Any]], int | None]:
    """``count`` with its last arguments set: one version of a card."""

    def value(figures: Any, filters: dict[str, Any]) -> int | None:
        return count(figures, filters, *args)

    return value


# programme -> card -> Card (SPEC_PBI §4.3, §5): every candidate the payload can count, today's definition
# first (the default) unless the evidence is certain. The names say what each counts, in Data health's words.
VARIANTS: dict[str, dict[str, Card]] = {
    "mscc": {
        "children": Card(
            "registrations",
            {
                "registrations": _of(_mscc_children, "registrations"),
                "view_rows": _of(_mscc_children, "view_rows"),
            },
            _("Children enrolled"),
            {
                "registrations": _("Registrations"),
                "view_rows": _("Rows of the view (a registration twice when the view repeats it)"),
            },
        ),
        "cwd": Card(
            "known_not_no",
            {"known_not_no": _of(_mscc_cwd, False), "not_no_or_blank": _of(_mscc_cwd, True)},
            _("Children with disabilities"),
            {
                "known_not_no": _("A disability other than “No”"),
                "not_no_or_blank": _("Anything but “No”, blanks included"),
            },
        ),
        "working": Card(
            "yes",
            {"yes": _of(_mscc_working, False), "not_no": _of(_mscc_working, True)},
            _("Working children"),
            {"yes": _("“Yes”"), "not_no": _("Anything but “No”, blanks included")},
        ),
        "immunized": Card(
            "either",
            {
                "either": _of(_mscc_children, "immunized"),
                "record_screened": _of(_mscc_children, "immunized_record"),
                "child_screened": _of(_mscc_children, "immunized_child"),
            },
            _("Child immunized"),
            {
                "either": _("Immunization record or child screened"),
                "record_screened": _("Immunization record screened"),
                "child_screened": _("Child screened for immunization"),
            },
        ),
        "screened": Card(
            "either",
            {"either": _of(_mscc_children, "screened"), "muac_only": _of(_mscc_children, "screened_muac")},
            _("Children screened for malnutrition"),
            {"either": _("A malnutrition result recorded"), "muac_only": _("A MUAC screening recorded")},
        ),
        "staff": Card(
            "all",
            {
                "all": _of(_mscc_staff, "all"),
                "active": _of(_mscc_staff, "active"),
                "at_centres_with_children": _of(_mscc_staff, "with_children"),
                "view_rows": _mscc_staff_view,
            },
            _("Program staff"),
            {
                "all": _("Every facilitator listed"),
                "active": _("Active in the current round"),
                "at_centres_with_children": _("At centres with children under the filters"),
                "view_rows": _("Rows of vw_center_program_staff (no filter)"),
            },
        ),
    },
    "bridging": {
        "children": Card(
            "unique",
            {
                "unique": _of(_bridging_children, "unique"),
                "registrations": _of(_bridging_children, "registrations"),
                "view_rows": _of(_bridging_children, "view_rows"),
            },
            _("Children enrolled in Dirasa"),
            {
                "unique": _("Unique children"),
                "registrations": _("Registrations"),
                "view_rows": _("Rows of the view (a registration twice when the view repeats it)"),
            },
            fallback="registrations",
        ),
        "cwd": Card(
            "registrations",
            {
                "registrations": _bridging_cwd,
                "schools_round": _of(_bridging_schools, "dirasa_cwd", "round"),
                "schools_all_listed": _of(_bridging_schools, "dirasa_cwd", "listed"),
            },
            _("CWD in Dirasa"),
            {
                "registrations": _("Registrations with a disability other than “No”"),
                "schools_round": _("Reported by the schools with registrations"),
                "schools_all_listed": _("Reported by every school a Dirasa partner lists (no filter)"),
            },
        ),
        "in_school": Card(
            "schools_round",
            {
                "schools_round": _of(_bridging_schools, "children", "round"),
                "schools_all_listed": _of(_bridging_schools, "children", "listed"),
            },
            _("In-school children"),
            {
                "schools_round": _("Reported by the schools with registrations"),
                "schools_all_listed": _("Reported by every school a Dirasa partner lists (no filter)"),
            },
        ),
        "in_school_cwd": Card(
            "schools_round",
            {
                "schools_round": _of(_bridging_schools, "cwd", "round"),
                "schools_all_listed": _of(_bridging_schools, "cwd", "listed"),
            },
            _("In-school CWD"),
            {
                "schools_round": _("Reported by the schools with registrations"),
                "schools_all_listed": _("Reported by every school a Dirasa partner lists (no filter)"),
            },
        ),
        "teachers": Card(
            "round",
            {
                "round": _of(_bridging_teachers, "round"),
                "round_at_schools_with_children": _of(_bridging_teachers, "with_children"),
                "view_rows": _bridging_teachers_view,
            },
            _("Dirasa teachers"),
            {
                "round": _("Teachers of the period's rounds, at the schools chosen"),
                "round_at_schools_with_children": _(
                    "Teachers of the period's rounds, at the schools with registrations"
                ),
                "view_rows": _("Rows of vw_teacher_data for the period's rounds (no filter)"),
            },
        ),
    },
    "youth": {
        "reached": Card(
            "enrolled_people",
            {
                "enrolled_people": _of(_youth_reached, "enrolled_people"),
                "registered_people": _of(_youth_reached, "registered_people"),
                "registrations": _of(_youth_reached, "registrations"),
                "enrolments": _of(_youth_reached, "enrolments"),
            },
            _("Young people reached"),
            {
                "enrolled_people": _("Young people enrolled under a youth indicator"),
                "registered_people": _("Young people registered, enrolled or not"),
                "registrations": _("Registrations"),
                "enrolments": _("Enrolments (a young person enrolled twice counts twice)"),
            },
        ),
        "partners": Card(
            "with_blank",
            {"with_blank": _of(_youth_partners, True), "without_blank": _of(_youth_partners, False)},
            _("Partners"),
            {
                "with_blank": _("Partners, a blank partner counted as one"),
                "without_blank": _("Partners named"),
            },
        ),
    },
}


def choices() -> dict[tuple[str, str], str]:
    """(programme, card) -> the version an administrator chose (admin → *Power BI cards*), read once per
    request."""

    def read() -> dict[tuple[str, str], str]:
        rows = PowerBiCard.objects.values_list("programme", "card", "variant")
        return {(programme, card): variant for programme, card, variant in rows}

    return compute.per_request("power-bi:cards", read)


def cards_stamp() -> str:
    """The versions chosen, as a short text that changes with every choice: in the keys of the pages that
    read them (Makani, Dirasa, youth, the country programme), so a new choice shows at once."""
    chosen_ = sorted(f"{programme}:{card}:{variant}" for (programme, card), variant in choices().items())
    return hashlib.sha256(json.dumps(chosen_).encode()).hexdigest()[:12] if chosen_ else "0"


def chosen(programme: str, card: str) -> str:
    """The version the pages show for ``card``: the administrator's choice, else (no choice, or a version no
    longer defined) the default."""
    definition = VARIANTS[programme][card]
    variant = choices().get((programme, card), "")
    return variant if variant in definition.variants else definition.default


def variant_value(
    programme: str, name: str, variant: str, figures: Any, filters: dict[str, Any]
) -> int | None:
    """One version of a card under the filters; None when the payload cannot count it (never raises)."""
    value_of = VARIANTS[programme][name].variants.get(variant)
    if value_of is None:
        return None
    try:
        return value_of(figures, filters)
    except Exception:  # a version a payload cannot answer must not take the page down: the default is shown
        logger.warning("Power BI card %s %s %s: no value", programme, name, variant, exc_info=True)
        return None


def card_value(programme: str, name: str, figures: Any, filters: dict[str, Any]) -> dict[str, Any]:
    """The card as a page shows it: the chosen version's value under the filters, else the default's, else
    the card's fallback (plain data, kept in the page caches).

    ``{"value", "variant" (the version shown), "chosen" (the version picked), "fallback" (True when the
    chosen version could not answer and another is shown), "is_default" (the version shown is the card's
    default), "name" (what the version shown counts), "hint" (a few words for the card when the version shown
    is not the default: "" otherwise, and the page keeps its own hint)}``."""
    card = VARIANTS[programme][name]
    picked = chosen(programme, name)
    tried = list(dict.fromkeys(v for v in (picked, card.default, card.fallback) if v))
    value, used = None, picked
    for variant in tried:
        value = variant_value(programme, name, variant, figures, filters)
        if value is not None:
            used = variant
            break
    name_of = {variant: str(card.names.get(variant, variant)) for variant in (picked, used)}
    if used != picked and picked == card.default:  # nobody chose: the default itself cannot answer
        hint = _("%(default)s cannot be counted under these filters: %(shown)s shown.") % {
            "default": name_of[picked],
            "shown": name_of[used],
        }
    elif used != picked:
        hint = _("The version chosen (%(chosen)s) cannot be counted here: %(shown)s shown.") % {
            "chosen": name_of[picked],
            "shown": name_of[used],
        }
    elif used != card.default:
        hint = _("Version chosen to match Power BI: %(shown)s.") % {"shown": name_of[used]}
    else:
        hint = ""
    return {
        "value": value,
        "variant": used,
        "chosen": picked,
        "fallback": used != picked,
        "is_default": used == card.default,
        "name": name_of[used],
        "hint": str(hint),
    }


def children_total(payload: dict[str, Any], programme: str | None = None) -> int:
    """Children enrolled in a Makani or Dirasa payload, as the programme's page counts its children card (the
    version chosen, at the period total): the country programme reads the same number. A payload of an
    unknown programme counts its registrations, as before; one without cubes (format 1), its unique
    children."""
    from .figures import Figures

    programme = programme or payload.get("programme") or ""
    figures = Figures(payload)
    if figures.has_cubes and figures.cube("enrolment").rows:
        if programme in ("mscc", "bridging"):
            value = card_value(programme, "children", figures, {})["value"]
            if value is not None:
                return value
        return figures.cube("enrolment").sum("registrations")
    return figures.block("registrations").total({}).get("people", 0)


# ------------------------------------------------------------------------------------- the comparison kept


def block(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The payload's ``power_bi`` block; None for a payload of an older Compiler (not compared yet)."""
    data = payload.get("power_bi")
    return data if isinstance(data, dict) else None


def check(programme: str, period: str) -> PowerBiCheck | None:
    return PowerBiCheck.objects.filter(programme=programme, period=str(period)).first()


def _when(text: Any) -> datetime | None:
    try:
        value = parse_datetime(text) if isinstance(text, str) and text else None
    except ValueError:  # well formed but impossible (a 31 June): no time
        value = None
    if value is not None and timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_default_timezone())
    return value


def counted_at(payload: dict[str, Any]) -> datetime | None:
    """When Compiler counted a payload (``counted_at`` of a stored count, else ``generated_at`` of one counted
    on the spot), always with a time zone: the page compares it with the stored check's."""
    return _when(payload.get("counted_at")) or _when(payload.get("generated_at"))


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _figures(programme: str, payload: dict[str, Any]) -> Any:
    if programme == "youth":
        from neurodb.youth.figures import Figures as YouthFigures

        return YouthFigures(payload)
    from .figures import Figures

    return Figures(payload)


def variant_totals(programme: str, payload: dict[str, Any]) -> dict[str, dict[str, int | None]]:
    """card -> {variant: value} at the period total (no filter), for Data health's cards table."""
    cards = VARIANTS.get(programme) or {}
    if not cards:
        return {}
    figures = _figures(programme, payload)
    out: dict[str, dict[str, int | None]] = {}
    for name, card in cards.items():
        values: dict[str, int | None] = {}
        for variant, value_of in card.variants.items():
            try:
                values[variant] = value_of(figures, {})
            except Exception:  # a variant the payload cannot answer (an older format): no value
                logger.warning("Power BI card %s %s %s: no value", programme, name, variant, exc_info=True)
                values[variant] = None
        out[name] = values
    return out


def store(programme: str, period: str, payload: dict[str, Any]) -> list[str]:
    """Keep the payload's comparison with Power BI's view: the ``PowerBiCheck`` of ``programme`` and
    ``period``, and a ``PowerBiView`` per view its catalogue describes. Returns the names of the views whose
    definition changed (a view dropped, created, or created again with another definition counts as a change;
    one Compiler could not describe does not). A payload without the block (an older Compiler) leaves any
    stored check as it is, noting that the figures kept are of a later count."""
    data = block(payload)
    if data is None:  # the comparison kept is now of an older count than the figures: Data health says so
        PowerBiCheck.objects.filter(programme=programme, period=str(period)[:64]).update(
            figures_counted_at=counted_at(payload)
        )
        return []
    checked_at = _when(data.get("checked_at")) or _when(payload.get("counted_at")) or timezone.now()
    with transaction.atomic():  # all or nothing: a failure leaves the rows as they were
        changed = _store_views(_dict(data.get("catalogue")), checked_at)
        _store_check(programme, str(period)[:64], payload, data, checked_at)
    return changed


def _store_check(
    programme: str, period: str, payload: dict[str, Any], data: dict[str, Any], checked_at: datetime
) -> None:
    stored = check(programme, period)
    if stored is not None and stored.checked_at and stored.checked_at > checked_at:
        return  # a newer comparison is already kept
    base = _dict(data.get("base"))
    PowerBiCheck.objects.update_or_create(
        programme=programme,
        period=period,
        defaults={
            "checked_at": checked_at,
            "counted_at": counted_at(payload),
            "figures_counted_at": counted_at(payload),
            "view": str(data.get("view") or "")[:63],
            "mode": str(base.get("mode") or "")[:8],
            "reason": str(base.get("reason") or "")[:300],
            "base": base,
            "key_check": _dict(data.get("key_check")),
            "reconciliation": _dict(data.get("reconciliation")),
            "periods": _list(data.get("periods")),
            "related": _dict(data.get("related")),
            "errors": [str(e)[:ERROR_CHARS] for e in _list(data.get("errors"))[:MAX_ERRORS]],
            "variants": variant_totals(programme, payload),
        },
    )


def _store_views(catalogue: dict[str, Any], checked_at: datetime) -> list[str]:
    """Upsert each described view. A description older than the one kept (a past period's payload, counted
    before the current one) is skipped, so it never turns a definition back. So is a view Compiler could not
    describe (``exists`` not true or false, e.g. a timeout): unknown, never "dropped". A dropped view keeps
    the hash of its last definition, so one created again with the same definition is no change."""
    changed = []
    for raw_name, entry in catalogue.items():
        if not raw_name or not isinstance(entry, dict):
            continue
        exists = entry.get("exists")
        if not isinstance(exists, bool) or entry.get("error"):
            continue  # not described this time: what is kept stays
        name = str(raw_name)[:63]
        row = PowerBiView.objects.filter(name=name).first()
        if row is not None and row.checked_at > checked_at:
            continue
        sha256 = str(entry.get("definition_sha256") or "")[:64] if exists else ""
        if row is None:
            row = PowerBiView(name=name, first_seen=checked_at, sha256=sha256)
        elif not exists:
            if row.exists:  # dropped: named on the sync run; its last definition and hash are kept
                changed.append(name)
            sha256 = row.sha256
        else:
            # the definition before a gap: kept by a drop, or (stored by an earlier build) in previous_sha256
            before = row.sha256 or ("" if row.exists else row.previous_sha256)
            if sha256 != before:
                row.previous_sha256, row.changed_at = before, checked_at
                changed.append(name)
        row.exists, row.sha256, row.checked_at = exists, sha256, checked_at
        row.can_select = exists and bool(entry.get("can_select"))
        if exists:  # a view that disappears keeps its last description
            row.kind = str(entry.get("kind") or "")[:24]
            row.columns = _list(entry.get("columns"))
            row.reads = _list(entry.get("reads"))
            row.definition = str(entry.get("definition") or "")[:DEFINITION_CHARS]
        row.save()
    return changed


# ------------------------------------------------------------------------------------------ the page line
def _number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def note(check: PowerBiCheck | None, counted_at: datetime | None = None) -> dict[str, Any]:
    """The line under a page's key figures (plain data; ``education/_power_bi_note.html`` words it): whether
    Compiler counted the rows of Power BI's view (``state`` "view") or its own rule ("rule", with Compiler's
    ``reason``), for which period, when, and what is left out; "none" before the first comparison (a payload
    of an older Compiler). ``counted_at``: when the page's figures were counted; a check of an older count
    (the payload that came later had no comparison) is ``stale``."""
    if check is None or not check.mode:
        return {"state": "none"}
    base = check.base or {}
    totals = (check.reconciliation or {}).get("totals") or {}
    deleted = base.get("deleted") or "exclude"
    stale = bool(counted_at and check.counted_at and counted_at - check.counted_at > STALE_AFTER)
    out = {
        "state": "view" if check.mode == "view" else "rule",
        "stale": stale,
        "view": check.view,
        "period": check.period,
        "every_year": check.period == ALL,
        "reason": check.reason,
        "deleted": deleted,
        "counted_at": check.counted_at or check.checked_at,
        "deleted_left_out": _number(totals.get("deleted_in_view")) if deleted == "exclude" else 0,
        "not_in_view": _number(totals.get("counted_not_in_view")),
        "errors": len(check.errors or []),
    }
    out["details"] = out["state"] == "rule" or bool(
        out["deleted_left_out"] or out["not_in_view"] or out["errors"]
    )
    return out


def page_note(programme: str, period: str, counted_at: datetime | None = None) -> dict[str, Any]:
    """:func:`note` of the stored check of ``programme`` and ``period`` (one small query)."""
    return note(check(programme, period), counted_at)


# ------------------------------------------------------------------------------------------- Data health
FLAG_LABELS = {  # the reconciliation's reasons, in Data health's words
    "deleted": _("deleted"),
    "tls": _("TLS"),
    "no_round": _("without a round"),
    "no_child": _("without a child"),
    "other_year": _("of another year"),
    "no_student": _("without a student"),
    "other_round": _("of another round"),
    "no_year": _("without a year"),
    "no_enrolment": _("without an enrolment"),
    "no_master": _("not enrolled under a youth indicator"),
}
SETS = ("in_view", "in_before", "counted")
KEY_FACTS = ("rows", "ids", "deleted_rows", "null_keys", "max_rows_per_id", "conflicting", "verified")
KEY_LABELS = (  # the key check's comparisons, each over the view's rows of the period (CONTRACT_PBI §3.3)
    ("found", _("are registrations")),
    ("same_round", _("have the registration's own round")),
    ("same_year", _("have the registration's own year")),  # youth: shown, not required
    ("same_partner", _("have the registration's own partner")),
    ("same_partner_or_centre", _("have the registration's partner, or its centre's")),
    ("same_center", _("have the registration's own centre")),
    ("same_school", _("have the registration's own school")),
    # youth: with the partner, what proves the key (enrolment and registration ids overlap as numbers)
    ("same_deleted", _("agree with the registration on whether it is deleted")),
)


def _view_read(reconciliation: dict[str, Any]) -> bool:
    """Whether Compiler read the view's rows for this comparison: without them (the view missing, no
    permission, the setting ``rule``, a failed key check or an error) the totals that need the view are null,
    and no registration can be said to be in the view or not (CONTRACT_PBI §3.4)."""
    return isinstance(((reconciliation or {}).get("totals") or {}).get("view_ids"), int)


def _not_compared(the_check: PowerBiCheck) -> str:
    """Why the registrations counted were not compared with the view's: "" when they were; "view" when
    Compiler did not read the view's rows (it counted with its own rule); "failed" when it counted the view's
    rows but the comparison itself failed (the reconciliation is ``{}`` and its error is in ``errors``,
    CONTRACT_PBI §3.4): the counts are the view's, so "the view was not read" would be untrue."""
    if _view_read(the_check.reconciliation or {}):
        return ""
    return "failed" if the_check.mode == "view" else "view"


def _reconciled(reconciliation: dict[str, Any]) -> list[dict[str, Any]]:
    """The reconciliation's rows: in which of the three sets, why (the reasons set, in words; "not a
    registration" when the view's id is none), and the counts. ``in_view`` is None when the view's rows were
    not read: unknown, not "no"."""
    by = list(reconciliation.get("by") or [])
    measures = list(reconciliation.get("measures") or [])
    people = measures[2] if len(measures) > 2 else ""
    reasons_of = [name for name in by if name not in SETS]
    read = _view_read(reconciliation)
    out = []
    for row in reconciliation.get("rows") or []:
        if not isinstance(row, list) or len(row) < len(by) + len(measures):
            continue
        flags = dict(zip(by, row[: len(by)], strict=False))
        values = dict(zip(measures, row[len(by) :], strict=False))
        reasons = [str(FLAG_LABELS.get(name, name)) for name in reasons_of if flags.get(name)]
        if reasons_of and all(flags.get(name) is None for name in reasons_of):
            reasons = [str(_("not a registration"))]
        out.append(
            {
                "in_view": bool(flags.get("in_view")) if read else None,
                "in_before": bool(flags.get("in_before")),
                "counted": bool(flags.get("counted")),
                "reasons": reasons,
                "registrations": _number(values.get("registrations")),
                "view_rows": values.get("view_rows"),
                "people": values.get(people) if people else None,
            }
        )
    return out


def _why(rows: list[dict[str, Any]], keep: Callable[[dict[str, Any]], bool]) -> list[tuple[str, int]]:
    """The registrations of the rows ``keep`` takes, per reason (largest first)."""
    out: dict[str, int] = {}
    for row in rows:
        if keep(row) and row["registrations"]:
            label = ", ".join(row["reasons"]) or str(_("other reason"))
            out[label] = out.get(label, 0) + row["registrations"]
    return sorted(out.items(), key=lambda item: (-item[1], item[0]))


def _key_check(key_check: dict[str, Any]) -> list[dict[str, Any]]:
    rows = key_check.get("rows")
    out = []
    for name, label in KEY_LABELS:
        value = key_check.get(name)
        if isinstance(value, int) and isinstance(rows, int) and rows > 0:
            out.append({"label": str(label), "count": value, "percent": round(value / rows * 100, 1)})
    return out


def _related(related: dict[str, Any], round_names: dict[Any, str] | None = None) -> list[dict[str, Any]]:
    """The related views' profiles for Data health. ``round_names``: the rounds' names by id (the view's
    rounds, ``periods``), so that a profile grouped by ``round_id`` names each round next to its id
    (``cells``): from payload format 4 a year holds several rounds, which an id alone does not tell apart."""
    out = []
    names = round_names or {}
    for name, profile in sorted((related or {}).items()):
        if not isinstance(profile, dict):
            continue
        by = list(profile.get("by") or [])
        rows = [r for r in profile.get("rows") or [] if isinstance(r, list)][:50]
        at = by.index("round_id") if "round_id" in by else None
        cells = [
            [
                f"{value} ({names[value]})"
                if i == at and isinstance(value, int | str) and value in names
                else value
                for i, value in enumerate(row)
            ]
            for row in rows
        ]
        out.append(
            {
                "name": name,
                # one Compiler could not describe or read ({"exists": null, "error": ...}) is not "missing"
                "exists": bool(profile.get("exists")) or bool(profile.get("error")),
                "error": str(profile.get("error") or ""),
                "by": by,
                "measures": list(profile.get("measures") or []),
                "rows": rows,
                "cells": cells,  # the rows as shown: a round's id with its name
                # Compiler keeps 50 groups at most: a cut list is not the view's total (its versions: unknown)
                "truncated": bool(profile.get("truncated")),
                # counted for the period's rounds only (Compiler from fixer R3: Dirasa's teachers)
                "period_rounds": bool(profile.get("rounds")),
            }
        )
    return out


YEARLY_FORMAT = 4  # Compiler counts Dirasa per year from this payload format (CONTRACT_PBI §9)


def period_kind(programme: str, payload_format: Any) -> str:
    """The kind of period of a payload: "year" (Makani; Dirasa from format 4) or "round" (Dirasa of an older
    payload)."""
    if programme != "bridging":
        return "year"
    try:
        return "year" if int(payload_format or 0) >= YEARLY_FORMAT else "round"
    except (TypeError, ValueError):
        return "round"


@dataclass(frozen=True)
class StoredPeriods:
    """The stored periods of the education programmes as their pages list them (``listed``, latest first), the
    stored periods of the other kind that the pages no longer list (``hidden``: Dirasa's rounds once Compiler
    counts it by year, or its years after a rollback to format 3), and the kind listed per programme
    (``kinds``: "year" or "round")."""

    listed: dict[str, list[str]]
    hidden: dict[str, set[str]]
    kinds: dict[str, str]


def _year_number(name: str) -> int:
    text = str(name).strip()
    return int(text) if text.isdigit() else -1


def stored_periods(programmes: Iterable[str]) -> StoredPeriods:
    """:class:`StoredPeriods` of the education ``programmes``, in one query (the payload's format, the start
    of its first round and the fetch time are read in the database, not the payload). Makani lists its years.
    Dirasa lists the periods of the kind of the row fetched last (ties: "year"): its years from payload format
    4 (Compiler counts the rounds of a year), its rounds before; the other kind stays stored, hidden, so a
    rollback of Compiler to format 3 lists rounds again once its first rounds are fetched. Years (Makani's
    and Dirasa's) are ordered newest first by their number, whatever their rounds' start dates (a round's
    start date may be empty in Compiler, and with the setting ``current_rounds`` a year may hold a round that
    started the year before); Dirasa's rounds by the start of the payload's first round, then by name, latest
    first: a Dirasa round's name is free text in Compiler, shared by every CLM programme ("Round 10
    2026-2027", "Bridging Round 2 2025-2026"), so its text order need not be its date order. Youth (whose
    page lists its years by name) is never read here."""
    wanted = [p for p in dict.fromkeys(programmes) if p in EDUCATION]
    listed: dict[str, list[str]] = {p: [] for p in wanted}
    hidden: dict[str, set[str]] = {p: set() for p in wanted}
    kinds: dict[str, str] = {p: "year" if p != "bridging" else "round" for p in wanted}
    if not wanted:
        return StoredPeriods(listed, hidden, kinds)
    rows = list(
        EducationFigures.objects.filter(programme__in=wanted).values_list(
            "programme", "year", "payload__rounds__0__start_date", "payload__format", "fetched_at"
        )
    )
    kind_of = {(programme, year): period_kind(programme, fmt) for programme, year, _s, fmt, _f in rows}
    for programme in wanted:
        mine = [r for r in rows if r[0] == programme]
        if programme == "bridging" and mine:
            last = max(mine, key=lambda r: (r[4], kind_of[(r[0], r[1])] == "year"))
            kinds[programme] = kind_of[(last[0], last[1])]
        kind = kinds[programme]
        shown = [r for r in mine if kind_of[(r[0], r[1])] == kind]
        hidden[programme] = {r[1] for r in mine if kind_of[(r[0], r[1])] != kind}
        if kind == "year":  # Makani's years and Dirasa's from format 4: whatever their rounds' start dates
            ordered = sorted(shown, key=lambda r: (_year_number(r[1]), r[1]), reverse=True)
        else:
            ordered = sorted(shown, key=lambda r: (str(r[2] or ""), r[1]), reverse=True)
        listed[programme] = [r[1] for r in ordered]
    return StoredPeriods(listed, hidden, kinds)


def page_periods(programmes: Iterable[str]) -> dict[str, list[str]]:
    """The stored periods of the education ``programmes`` each page lists (Makani's years; Dirasa's years from
    payload format 4, its rounds before), each programme's latest first as its page lists them and opens on
    the first (``education.services.periods``): :func:`stored_periods`."""
    return stored_periods(programmes).listed


def hidden_periods(programmes: Iterable[str]) -> dict[str, set[str]]:
    """The stored periods of the other kind than the one each page lists (:func:`stored_periods`): kept, never
    shown, compared nor offered."""
    return stored_periods(programmes).hidden


def latest_first(
    programme: str,
    periods: Iterable[str],
    page: dict[str, list[str]] | None = None,
    hidden: dict[str, set[str]] | None = None,
) -> list[str]:
    """``periods`` of ``programme`` (all years together and the hidden periods left out), latest first in the
    order its page lists them (:func:`page_periods`; youth's years by name, as its page), so that Data health,
    the card admin and the summary call "latest" the period the page opens on, and never name an old Dirasa
    round once its years are shown. A period the page does not list (no figures stored) comes after, by name.
    ``page`` and ``hidden``: :func:`stored_periods`' answer, when already read (both are read, in one query,
    when neither is given)."""
    periods = [p for p in dict.fromkeys(periods) if p != ALL]
    if programme not in EDUCATION:
        return sorted(periods, reverse=True)
    if page is None and hidden is None:
        read = stored_periods([programme])
        page, hidden = read.listed, read.hidden
    order = (page or {}).get(programme, [])
    dropped = (hidden or {}).get(programme, set())
    periods = [p for p in periods if p not in dropped]
    rank = {period: i for i, period in enumerate(order)}
    listed = sorted((p for p in periods if p in rank), key=rank.__getitem__)
    return listed + sorted((p for p in periods if p not in rank), reverse=True)


def latest_period(
    programme: str,
    checks: list[PowerBiCheck] | None = None,
    page: dict[str, list[str]] | None = None,
    hidden: dict[str, set[str]] | None = None,
) -> str:
    """The programme's latest compared period (not all years together, not a hidden one), as its page orders
    them (:func:`latest_first`): the one a Power BI number typed without a period is compared with.
    ``checks``: the programme's, and ``page`` and ``hidden``: :func:`stored_periods`' answer, when already
    read."""
    if checks is None:
        checks = list(PowerBiCheck.objects.filter(programme=programme).only("programme", "period"))
    periods = latest_first(programme, (c.period for c in checks if c.programme == programme), page, hidden)
    return periods[0] if periods else ""


def typed_for(row: PowerBiCard | None, period: str, latest: str) -> bool:
    """Whether the Power BI number typed in ``row`` is compared with ``period``: the period it was typed for
    (one of the stored periods, chosen in the admin), else the programme's ``latest`` period only."""
    if row is None or row.powerbi_value is None:
        return False
    return period == (row.powerbi_period or latest)


def _cards(
    programme: str, the_check: PowerBiCheck, typed: dict[tuple[str, str], PowerBiCard], latest: str = ""
) -> list[dict]:
    """Each card of the programme: every version's value at the period total, the number Power BI shows when
    an administrator typed it for this period (or, typed without a period, when this is the ``latest``
    period), which versions equal it, and the one in use."""
    out = []
    for name, card in VARIANTS.get(programme, {}).items():
        values = (the_check.variants or {}).get(name) or {}
        row = typed.get((programme, name))
        in_use = row.variant if row is not None and row.variant in card.variants else card.default
        shown = typed_for(row, the_check.period, latest)
        powerbi = row.powerbi_value if shown else None
        versions = [
            {
                "variant": variant,
                "name": str(card.names.get(variant, variant)),
                "value": values.get(variant),
                "equal": powerbi is not None and values.get(variant) == powerbi,
                "in_use": variant == in_use,
                "default": variant == card.default,
            }
            for variant in card.variants
        ]
        out.append(
            {
                "card": name,
                "label": str(card.label or name),
                "versions": versions,
                "powerbi": powerbi,
                "powerbi_period": row.powerbi_period if shown else "",
                "noted_at": row.noted_at if shown else None,
                "none_equal": powerbi is not None and not any(v["equal"] for v in versions),
            }
        )
    return out


def _latest(programme: str, checks: list[PowerBiCheck], periods: StoredPeriods) -> list[PowerBiCheck]:
    """The programme's checks: all years together first (youth), then the periods, latest first as its page
    orders them, the hidden ones left out (:func:`latest_first`; ``periods``: :func:`stored_periods`'
    answer)."""
    mine = {c.period: c for c in checks if c.programme == programme}
    every = [mine[ALL]] if ALL in mine else []
    ordered = latest_first(programme, mine, periods.listed, periods.hidden)
    return every + [mine[period] for period in ordered]


def _stale(the_check: PowerBiCheck) -> bool:
    """Whether the figures kept for the check's period were counted later than the comparison: the payload
    that came after it had none (a Compiler gone back to an earlier version), so the comparison is of an
    older count (as :func:`note` says on the page)."""
    later, compared = the_check.figures_counted_at, the_check.counted_at
    return bool(later and compared and later - compared > STALE_AFTER)


def _row(
    the_check: PowerBiCheck, view: PowerBiView | None, typed: dict, latest: str = "", stale: bool = False
) -> dict[str, Any]:
    """One programme and period on Data health (counts only: no definition, no masked text). ``latest``: the
    programme's latest period (a Power BI number typed without a period is compared with it only);
    ``stale``: the page's figures were counted after this comparison (:func:`_stale`)."""
    reconciliation = the_check.reconciliation or {}
    totals = reconciliation.get("totals") or {}
    rows = _reconciled(reconciliation)
    read = _view_read(reconciliation)
    view_ids, counted = totals.get("view_ids"), totals.get("counted")
    rule = (the_check.base or {}).get("period_rule")
    return {
        "programme": the_check.programme,
        "period": the_check.period,
        "every_year": the_check.period == ALL,
        # Dirasa counted per year (payload format 4): how the rounds of the year were chosen ("" otherwise)
        "year_rule": rule if the_check.programme == "bridging" and rule in YEAR_RULES else "",
        "view": the_check.view,
        "view_state": None
        if view is None
        else {
            "exists": view.exists,
            "kind": view.kind,
            "can_select": view.can_select,
            "changed_at": view.changed_at,
            "first_seen": view.first_seen,
        },
        "mode": the_check.mode,
        "reason": the_check.reason,
        "base": {k: v for k, v in (the_check.base or {}).items() if isinstance(v, str | int | list | None)},
        "view_rows": totals.get("view_rows"),
        "view_ids": view_ids,
        "view_read": read,  # False: "not compared", never "not in the view"
        "not_compared": _not_compared(the_check),  # why not: the view was not read, or the comparison failed
        "stale": stale,
        "counted": counted,
        "difference": counted - view_ids if isinstance(counted, int) and isinstance(view_ids, int) else None,
        "left_out": _why(rows, lambda r: r["in_view"] and not r["counted"]) if read else [],
        "not_in_view": _why(rows, lambda r: r["counted"] and not r["in_view"]) if read else [],
        "before": totals.get("before"),
        "before_difference": totals.get("before") - counted
        if isinstance(totals.get("before"), int) and isinstance(counted, int)
        else None,
        "before_more": _why(rows, lambda r: r["in_before"] and not r["counted"]),
        "before_fewer": _why(rows, lambda r: r["counted"] and not r["in_before"]),
        "reasons": rows,
        "people_measure": (list(reconciliation.get("measures") or []) + ["", "", ""])[2],
        "periods": [p for p in the_check.periods or [] if isinstance(p, dict)],
        "key_check": _key_check(the_check.key_check or {}),
        "key_facts": {k: (the_check.key_check or {}).get(k) for k in KEY_FACTS},
        "related": _related(
            the_check.related or {},
            {
                p["id"]: str(p["name"])
                for p in the_check.periods or []
                if isinstance(p, dict) and isinstance(p.get("id"), int | str) and p.get("name")
            },
        ),
        "cards": _cards(the_check.programme, the_check, typed, latest),
        "checked_at": the_check.checked_at,
        "counted_at": the_check.counted_at,
        "errors": [str(e) for e in the_check.errors or []],
    }


def no_year(latest: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The rounds in no year that an education programme's current year leaves out (payload format 4,
    CONTRACT_PBI §9.5: Makani's rounds without a year, a start date or a year in their name; Dirasa's
    without a Bridging start date or a year in their name), from the payload of each programme's ``latest``
    period (the one its page opens on: Compiler sends them in the current year's payload only): programme ->
    ``{"period", "registrations", "rounds"}`` (the rounds' names), only where registrations are counted in
    no year. Counts and round names only. One query, reading that key of one payload per programme in the
    database (never a payload itself)."""
    wanted = {
        programme: str(period) for programme, period in latest.items() if programme in EDUCATION and period
    }
    if not wanted:
        return {}
    where = Q()
    for programme, period in wanted.items():
        where |= Q(programme=programme, year=period)
    out: dict[str, dict[str, Any]] = {}
    rows = EducationFigures.objects.filter(where).values_list("programme", "year", "payload__no_year")
    for programme, period, value in rows:
        registrations = _dict(value).get("registrations")
        if wanted.get(programme) != period or _number(registrations) <= 0:
            continue
        rounds = [r for r in _list(_dict(value).get("rounds")) if isinstance(r, dict)]
        names = [str(r.get("name") or f"#{r.get('id')}") for r in rounds]
        out[programme] = {"period": period, "registrations": registrations, "rounds": names}
    return out


def health() -> list[dict[str, Any]]:
    """Data health's *Compared with Power BI*: per programme, its latest periods (at most
    :data:`HEALTH_PERIODS`), each compared with Power BI's view, and the registrations of its rounds in no
    year (``no_year``: :func:`no_year`, Makani and Dirasa). Five queries at most, whatever the number of
    periods (the fourth orders Makani's and Dirasa's periods as their pages do, the fifth reads the rounds in
    no year of their latest); counts only."""
    from django.urls import reverse

    checks = list(PowerBiCheck.objects.all())
    names = {c.view for c in checks if c.view}
    views = {
        v.name: v for v in PowerBiView.objects.filter(name__in=names).defer("definition", "columns", "reads")
    }
    typed = {(c.programme, c.card): c for c in PowerBiCard.objects.all()}
    stored = stored_periods({c.programme for c in checks})
    unplaced = no_year({programme: listed[0] for programme, listed in stored.listed.items() if listed})
    out = []
    for programme, label, url_name in PROGRAMMES:
        mine = _latest(programme, checks, stored)
        latest = latest_period(programme, mine, stored.listed, stored.hidden)
        out.append(
            {
                "programme": programme,
                "label": str(label),
                "url": reverse(url_name),
                "checks": [
                    _row(c, views.get(c.view), typed, latest, _stale(c)) for c in mine[:HEALTH_PERIODS]
                ],
                "no_year": unplaced.get(programme),
            }
        )
    return out


def summary(*, with_no_year: bool = False) -> list[dict[str, Any]]:
    """Per programme, its latest reporting period (not all years together) compared with Power BI's view, in
    a few words (counts only): for the assistant, the daily review and the internal API
    (``reports.services.data_health``). Its latest period is the one its page opens on
    (:func:`latest_first`). ``with_no_year``: also say how many registrations are in rounds that belong to no
    year, and which (``no_year``, :func:`no_year`; one more query), a reason a figure may differ from Power
    BI's (Home's programmes line, which does not show it, leaves it out)."""
    checks = list(PowerBiCheck.objects.exclude(period=ALL))
    stored = stored_periods({c.programme for c in checks})
    unplaced = (
        no_year({programme: listed[0] for programme, listed in stored.listed.items() if listed})
        if with_no_year
        else {}
    )
    out = []
    for programme, label, _url in PROGRAMMES:
        latest = next(iter(_latest(programme, checks, stored)), None)
        if latest is None:
            out.append({"programme": programme, "label": str(label), "compared": False})
            continue
        totals = (latest.reconciliation or {}).get("totals") or {}
        view_ids, counted = totals.get("view_ids"), totals.get("counted")
        row = {
            "programme": programme,
            "label": str(label),
            "compared": True,
            "period": latest.period,
            "view": latest.view,
            "mode": latest.mode,
            "reason": latest.reason,
            "view_registrations": view_ids,
            "counted": counted,
            "difference": counted - view_ids
            if isinstance(counted, int) and isinstance(view_ids, int)
            else None,
            "checked_at": latest.checked_at,
            # the figures were counted later, without a comparison: this one is of an older count
            "stale": _stale(latest),
            "errors": len(latest.errors or []),
        }
        if with_no_year:
            # registrations in rounds that belong to no year (Makani, Dirasa): counted in no year at all
            row["no_year"] = unplaced.get(programme)
        out.append(row)
    return out
