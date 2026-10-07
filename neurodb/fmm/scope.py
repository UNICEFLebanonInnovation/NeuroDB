"""The filter of the Monitoring insights page (:class:`Scope`): which visits a page, a link, a chart cell
or a chat answer is about.

A scope is a period (a preset such as "this year" or "all time", a calendar year or two dates) and
filters on the visits: sections, governorate, field offices, partners, a programme document, entity
types, ratings, status groups, monitoring modalities, quality bands (High, Medium, Low, Pending: no
score), urgency levels (High, Medium, Low; a visit without a score has none), programmatic visits only,
a search text, and the drill-downs that charts and links add. Periods read the visit's start date
(``Visit.visit_date``: the start date, else the end date when eTools has no start date); a visit
with neither is left out of every period.

**Sections.** A user with a section sees it by default, as on the overview, but only on a bare visit
to the page (no ``section`` key in the address). Every link FMM writes carries ``section`` explicitly
(:attr:`Scope.query`, :func:`link`), empty when every section is shown, so that following a link never
re-applies the default and the figures equal those of the page the link came from.

**Governorate.** Given as a key ("bekaa") or a gazetteer name ("Beqaa", "Baalbek - El Hermel"), as
the overview sends names; both are read with ``reports.overview.governorate_key``. "none" means the
visits whose governorate is not known.

**Entity-level filters** (entity type, partner) keep a visit when at least one of its entities
matches; :meth:`Scope.entities` then counts the matching rows only.
"""

from __future__ import annotations

import calendar
import datetime
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlencode

from django.db.models import Exists, OuterRef, Q, QuerySet, Subquery
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format
from django.utils.translation import gettext as _

from neurodb.datamart import fm

PRESETS = (
    "this_year",
    "last_year",
    "year",
    "this_quarter",
    "last_quarter",
    "last_30",
    "last_90",
    "all_time",
    "custom",
)
PRESET_LABELS = {
    "this_year": "This year",
    "last_year": "Last year",
    "year": "Calendar year",
    "this_quarter": "This quarter",
    "last_quarter": "Last quarter",
    "last_30": "Last 30 days",
    "last_90": "Last 90 days",
    "all_time": "All time",
    "custom": "Custom dates",
}
ALL_TIME_START = datetime.date(1990, 1, 1)  # "all time": every visit, whatever its date
QUALITY_BANDS = ("high", "medium", "low", "pending")
QUALITY_LABELS = {"high": "High", "medium": "Medium", "low": "Low", "pending": "Pending (no score)"}
URGENCY_LEVELS = ("high", "medium", "low")
URGENCY_LABELS = {"high": "High", "medium": "Medium", "low": "Low"}
NONE = "none"  # "visits without a section / governorate / office"
RATINGS = ("on_track", "constrained", "off_track", "not_monitored")
RATING_LABELS = {
    "on_track": "On track",
    "constrained": "Constrained",
    "off_track": "Off track",
    "not_monitored": "Not monitored",
}
STATUS_GROUPS = ("planned", "in_progress", "reported", "cancelled", "unknown")
STATUS_LABELS = {
    "planned": "Planned",
    "in_progress": "In progress",
    "reported": "Reported",
    "cancelled": "Cancelled",
    "unknown": "Status unknown",
}
KIND_LABELS = {"pd": "PD/SSFA", "cp_output": "CP output", "partner": "Partner", "other": "Other"}
RULE_CODE = re.compile(r"^R\d{1,3}$")  # a quality rule's id (R1 ... R32)
RULE_STATES = ("pass", "fail", "na", "nap", "off", "pending")
# the one pass counts scores in buckets of 10 points (90-100 holds 100); the chart shows FMS's buckets of 20
# points (80-100 holds 100), and both are read, so an older link keeps opening its visits
CHART_BUCKETS = {f"{low}-{low + 10}": (low, low + 10) for low in range(0, 100, 10)}
BUCKETS = {
    **CHART_BUCKETS,
    **{"0-20": (0, 20), "20-40": (20, 40), "40-60": (40, 60), "60-80": (60, 80), "80-100": (80, 100)},
}
FLAG_COUNTS = ("0", "1", "2", "3+")  # the flags-per-visit rows; "N+" (N or more) is read for any N
_FLAGS = re.compile(r"^(\d)(\+?)$")
URGENCY_BANDS = ("red", "amber", NONE)
REVIEW_STATES = ("reviewed", "follow_up", "data_issue", NONE)
# The drill-downs of chart cells and links, in the order the address carries them
DRILL_KEYS = (
    "month",
    "visit_status",
    "hact_q1",
    "bucket",
    "flag",
    "flags",
    "urgency",
    "location",
    "issue",
    "rule",
    "rule_state",
    "review",
)
MAX_VALUES = 50  # values kept per filter
Q_CHARS = 200
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_ISSUE = re.compile(r"^(R\d{1,3}):([a-z0-9_:,]{1,40})$")


def _drill_ok(key: str, value: str) -> bool:
    """A drill value the scope understands: codes only, never a label as a chart draws it."""
    if key == "month":
        return bool(_MONTH.match(value))
    if key == "visit_status":  # an eTools status (the morning briefing's tiles)
        return value in fm.STATUSES
    if key == "hact_q1":
        return value in ("on_track", "constrained", "off_track", NONE)
    if key == "bucket":
        return value in BUCKETS or value == NONE
    if key in ("flag", "rule"):
        return bool(RULE_CODE.match(value))
    if key == "flags":
        return bool(_FLAGS.match(value))
    if key == "urgency":
        return value in URGENCY_BANDS
    if key == "location":
        return value.isdigit() and len(value) <= 12
    if key == "issue":
        return bool(_ISSUE.match(value))
    if key == "rule_state":
        return value in RULE_STATES
    if key == "review":
        return value in REVIEW_STATES
    return False


def _today(today: datetime.date | None) -> datetime.date:
    return today or timezone.localdate()


def quarter_of(day: datetime.date) -> tuple[datetime.date, datetime.date]:
    """The first and last day of the calendar quarter of ``day``."""
    first_month = 3 * ((day.month - 1) // 3) + 1
    start = datetime.date(day.year, first_month, 1)
    last_month = first_month + 2
    return start, datetime.date(day.year, last_month, calendar.monthrange(day.year, last_month)[1])


def period(preset: str, today: datetime.date, year: int | None = None) -> tuple[datetime.date, datetime.date]:
    """The first and last day of a preset (not ``custom``)."""
    if preset == "year" and year:
        return datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    if preset == "last_year":
        return datetime.date(today.year - 1, 1, 1), datetime.date(today.year - 1, 12, 31)
    if preset == "this_quarter":
        return quarter_of(today)
    if preset == "last_quarter":
        start, _end = quarter_of(today)
        return quarter_of(start - datetime.timedelta(days=1))
    if preset in ("last_30", "last_90"):
        days = 30 if preset == "last_30" else 90
        return today - datetime.timedelta(days=days - 1), today
    if preset == "all_time":
        return ALL_TIME_START, datetime.date(today.year + 10, 12, 31)
    return datetime.date(today.year, 1, 1), datetime.date(today.year, 12, 31)


def _iso(raw: Any) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(raw or "").strip()[:10])
    except ValueError:
        return None


def _values(params: Mapping, key: str) -> list[str]:
    """Every non-blank value of ``key`` (repeated keys), at most MAX_VALUES, in order, once each."""
    getlist = getattr(params, "getlist", None)
    raw = getlist(key) if getlist else ([params[key]] if key in params else [])
    out: list[str] = []
    for value in raw:
        for part in value if isinstance(value, list | tuple) else [value]:
            text = str(part or "").strip()[:200]
            if text and text not in out:
                out.append(text)
    return out[:MAX_VALUES]


MAX_ID = 2**31 - 1  # partner ids are kept in an integer array: a larger number matches nothing


def _ints(values: list[str]) -> tuple[int, ...]:
    """The ids among ``values``; a number too large for an id is left out (Postgres would refuse it)."""
    return tuple(sorted({int(v) for v in values if v.isdigit() and len(v) <= 10 and int(v) <= MAX_ID}))


def _same_day(year: int, day: datetime.date) -> datetime.date:
    """``day`` in ``year`` (28 February for a 29 February in a year without one)."""
    return datetime.date(year, day.month, min(day.day, calendar.monthrange(year, day.month)[1]))


def _day(value: datetime.date) -> str:
    return date_format(value, "j M Y")


@dataclass(frozen=True)
class Scope:
    """Which visits a page, a link or an answer is about (see the module docstring)."""

    preset: str
    start: datetime.date
    end: datetime.date
    sections: tuple[str, ...] = ()  # eTools names; "none" = visits without a section
    governorate: str = ""  # overview.governorate_key; "none" = not located
    offices: tuple[str, ...] = ()  # "none" = office not known
    partners: tuple[int, ...] = ()
    pd: int | None = None  # set by the PD page's link; shown as a removable chip
    entity_types: tuple[str, ...] = ()  # pd | cp_output | partner | other
    ratings: tuple[str, ...] = ()  # visit rating: on_track | constrained | off_track | not_monitored
    statuses: tuple[str, ...] = ()  # status groups
    modalities: tuple[str, ...] = ()  # eTools' monitoring modality; "none" = not known
    quality_bands: tuple[str, ...] = ()  # high | medium | low | pending (no score)
    urgency_levels: tuple[str, ...] = ()  # high (red) | medium (amber) | low (below amber, scored)
    programmatic: bool = False
    q: str = ""  # matches Visit.search (folded)
    drill: tuple[tuple[str, str], ...] = ()  # (key, value) of DRILL_KEYS
    year: int | None = None  # the calendar year of the "year" preset
    # the user's own section was applied because the address named none (the "show all" chip)
    default_section: bool = field(default=False, compare=False)
    empty: bool = False  # narrowed to nothing (chat tools): a value outside the bound scope

    # -------------------------------------------------------------------------------- reading
    @classmethod
    def from_params(
        cls, params: Mapping, user=None, today: datetime.date | None = None, when: str | None = None
    ) -> Scope:
        """The scope of a query string (a ``QueryDict`` or a plain mapping).

        ``?year=Y`` means 1 January to 31 December of Y and wins over a preset (the year menu adds it to
        any address); ``period`` is the filter bar's own field (a preset or a year); ``custom`` reads
        ``?from=&to=`` as ISO dates. The user's default section applies only when the address has no
        ``section`` key at all.
        """
        today = _today(today)
        year_text = str(params.get("year") or "").strip()
        chosen = str(params.get("period") or params.get("preset") or "").strip()
        year = int(year_text) if re.fullmatch(r"(19|20)\d\d", year_text) else None
        if year is None and re.fullmatch(r"(19|20)\d\d", chosen):
            year = int(chosen)
        if year is not None:
            preset = "year"
            start, end = period("year", today, year)
        elif chosen == "custom":
            preset = "custom"
            first, last = _iso(params.get("from")), _iso(params.get("to"))
            start = first or datetime.date(today.year, 1, 1)
            end = last or datetime.date((first or today).year, 12, 31)
            if start > end:
                start, end = end, start
        else:
            preset = chosen if chosen in PRESETS and chosen != "year" else "this_year"
            start, end = period(preset, today)

        default_section = False
        if "section" in params:
            sections = tuple(_values(params, "section"))
        else:
            sections = ()
            section = getattr(user, "section", None) if user is not None else None
            if section is not None:
                from neurodb.datamart.monitoring import default_sections

                sections = tuple(default_sections(user, options(when)["sections"]))
                default_section = bool(sections)

        governorate = str(params.get("governorate") or "").strip()[:100]
        if governorate and governorate.lower() != NONE:
            from neurodb.reports.overview import governorate_key

            governorate = governorate_key(governorate)
        elif governorate:
            governorate = NONE

        pd_text = str(params.get("pd") or "").strip()
        drill = tuple(
            (key, value) for key in DRILL_KEYS for value in _values(params, key)[:1] if _drill_ok(key, value)
        )
        return cls(
            preset=preset,
            start=start,
            end=end,
            year=year,
            sections=sections,
            governorate=governorate,
            offices=tuple(_values(params, "office")),
            partners=_ints(_values(params, "partner")),
            pd=int(pd_text) if pd_text.isdigit() and len(pd_text) <= 12 else None,
            entity_types=tuple(sorted(set(_values(params, "entity_type")) & set(fm.KINDS))),
            ratings=tuple(sorted(set(_values(params, "rating")) & set(RATINGS))),
            statuses=tuple(sorted(set(_values(params, "status")) & set(STATUS_GROUPS))),
            modalities=tuple(_values(params, "modality")),
            quality_bands=tuple(b for b in QUALITY_BANDS if b in _values(params, "quality")),
            urgency_levels=tuple(u for u in URGENCY_LEVELS if u in _values(params, "urgency_level")),
            programmatic=str(params.get("programmatic") or "").lower() in ("1", "true", "on", "yes"),
            q=" ".join(str(params.get("q") or "").split())[:Q_CHARS],
            drill=drill,
            default_section=default_section,
        )

    # -------------------------------------------------------------------------------- querying
    def filtered(self, qs: QuerySet | None = None) -> QuerySet:
        """``qs`` (every visit by default) narrowed by every filter of the scope but the period."""
        from .models import Visit

        qs = Visit.objects.all() if qs is None else qs
        if self.empty:
            return qs.none()
        if self.sections:
            names = [s for s in self.sections if s != NONE]
            match = Q(section_names__overlap=names) if names else Q(pk__in=[])
            if NONE in self.sections:
                match |= Q(section_names=[])
            qs = qs.filter(match)
        if self.governorate:
            qs = qs.filter(governorate_key="" if self.governorate == NONE else self.governorate)
        if self.offices:
            names = [o for o in self.offices if o != NONE]
            match = Q(offices__overlap=names) if names else Q(pk__in=[])
            if NONE in self.offices:
                match |= Q(offices=[])
            qs = qs.filter(match)
        if self.partners and self.entity_types:  # one entity must match both
            from .models import VisitEntity

            rows = VisitEntity.objects.filter(
                visit=OuterRef("pk"), kind__in=self.entity_types, partner_id__in=self.partners
            )
            qs = qs.filter(Exists(rows))
        elif self.partners:
            qs = qs.filter(partner_ids__overlap=list(self.partners))
        elif self.entity_types:
            qs = qs.filter(entity_kinds__overlap=list(self.entity_types))
        if self.pd is not None:
            qs = qs.filter(pd_ids__contains=[self.pd])
        if self.ratings:
            # Not monitored (planned, not conducted) is a reported visit with nothing rated; a planned or
            # in-progress one is not rated yet (metrics.counted_rating)
            rated = [r for r in self.ratings if r != "not_monitored"]
            match = Q(rating__in=rated) if rated else Q(pk__in=[])
            if "not_monitored" in self.ratings:
                match |= Q(rating="not_monitored", status_group="reported")
            qs = qs.filter(match)
        if self.statuses:
            qs = qs.filter(status_group__in=self.statuses)
        if self.modalities:
            names = [m for m in self.modalities if m != NONE]
            match = Q(modality__in=names) if names else Q(pk__in=[])
            if NONE in self.modalities:
                match |= Q(modality="")
            qs = qs.filter(match)
        if self.quality_bands:
            match = Q(pk__in=[])
            for band in self.quality_bands:
                match |= Q(quality_score__isnull=True) if band == "pending" else Q(score_band=band)
            qs = qs.filter(match)
        if self.urgency_levels:  # the bands the scoring gave, from the urgency thresholds (Score settings)
            levels = {
                "high": Q(urgency_band="red"),
                "medium": Q(urgency_band="amber"),
                "low": Q(urgency_band="", urgency__isnull=False),
            }
            match = Q(pk__in=[])
            for level in self.urgency_levels:
                match |= levels[level]
            qs = qs.filter(match)
        if self.programmatic:
            qs = qs.filter(is_programmatic=True)
        if self.q:
            from .build import search_text

            needle = search_text(self.q)
            if needle:
                qs = qs.filter(search__contains=needle)
        for key, value in self.drill:
            qs = _drill(qs, key, value, self.drill)
        return qs

    def visits(self) -> QuerySet:
        """The visits of the scope: visit date (start, else end) in the period (a visit without one
        is left out), every filter and drill-down applied."""
        return self.filtered().filter(visit_date__gte=self.start, visit_date__lte=self.end)

    def undated(self) -> QuerySet:
        """The visits every filter but the period keeps that have no date at all (the data note)."""
        return self.filtered().filter(visit_date=None)

    def entities(self) -> QuerySet:
        """The finding rows of the scope's visits; the entity type and partner filters also filter the
        rows themselves."""
        from .models import VisitEntity

        qs = VisitEntity.objects.filter(visit__in=self.visits().values("pk"))
        if self.entity_types:
            qs = qs.filter(kind__in=self.entity_types)
        if self.partners:
            qs = qs.filter(partner_id__in=self.partners)
        return qs

    @property
    def entity_filtered(self) -> bool:
        """An entity-level filter is on: the entity figures count the matching rows only."""
        return bool(self.entity_types or self.partners)

    # -------------------------------------------------------------------------------- identity
    def canonical(self) -> dict[str, Any]:
        """The scope as plain values: the preset token instead of dates for every preset but
        ``custom`` (so "this year" is the same scope all year long)."""
        out: dict[str, Any] = {"preset": self.preset}
        if self.preset == "year":
            out["year"] = self.year
        elif self.preset == "custom":
            out["from"], out["to"] = self.start.isoformat(), self.end.isoformat()
        out.update(
            {
                "sections": sorted(self.sections),
                "governorate": self.governorate,
                "offices": sorted(self.offices),
                "partners": sorted(self.partners),
                "pd": self.pd,
                "entity_types": sorted(self.entity_types),
                "ratings": sorted(self.ratings),
                "statuses": sorted(self.statuses),
                "programmatic": self.programmatic,
                "q": self.q,
                "drill": sorted([k, v] for k, v in self.drill),
                "empty": self.empty,
            }
        )
        # Release 2's filters only when set, so that a scope without them keeps its hash (and its brief)
        for name in ("modalities", "quality_bands", "urgency_levels"):
            if getattr(self, name):
                out[name] = sorted(getattr(self, name))
        return out

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()

    def pairs(self, *, drill: bool = True) -> list[tuple[str, str]]:
        """The query string's (key, value) pairs; ``section`` always among them."""
        out: list[tuple[str, str]] = []
        if self.preset == "year":
            out.append(("year", str(self.year)))
        elif self.preset == "custom":
            out += [("preset", "custom"), ("from", self.start.isoformat()), ("to", self.end.isoformat())]
        elif self.preset != "this_year":
            out.append(("preset", self.preset))
        out += [("section", s) for s in self.sections] or [("section", "")]
        if self.governorate:
            out.append(("governorate", self.governorate))
        out += [("office", o) for o in self.offices]
        out += [("partner", str(p)) for p in self.partners]
        if self.pd is not None:
            out.append(("pd", str(self.pd)))
        out += [("entity_type", k) for k in self.entity_types]
        out += [("rating", r) for r in self.ratings]
        out += [("status", s) for s in self.statuses]
        out += [("modality", m) for m in self.modalities]
        out += [("quality", b) for b in self.quality_bands]
        out += [("urgency_level", u) for u in self.urgency_levels]
        if self.programmatic:
            out.append(("programmatic", "1"))
        if self.q:
            out.append(("q", self.q))
        if drill:
            out += list(self.drill)
        return out

    @property
    def query(self) -> str:
        """The canonical query string of links, drill-downs and pagination. It always carries
        ``section`` ("section=" when every section is shown), so re-reading it never applies the
        user's default section again."""
        return urlencode(self.pairs())

    @property
    def query_without_drill(self) -> str:
        return urlencode(self.pairs(drill=False))

    # -------------------------------------------------------------------------------- variants
    def previous(self) -> Scope:
        """The period of the same length just before (this year: last year up to the same day)."""
        if self.preset == "this_year":
            today = min(_today(None), self.end)
            start = datetime.date(self.start.year - 1, 1, 1)
            end = _same_day(self.start.year - 1, today)
        elif self.preset in ("year", "last_year"):
            start = datetime.date(self.start.year - 1, 1, 1)
            end = datetime.date(self.start.year - 1, 12, 31)
        elif self.preset in ("this_quarter", "last_quarter"):
            start, end = quarter_of(self.start - datetime.timedelta(days=1))
        elif self.preset == "all_time":  # nothing before all time
            return replace(self, preset="custom", year=None, default_section=False, empty=True)
        else:
            days = (self.end - self.start).days + 1
            end = self.start - datetime.timedelta(days=1)
            start = end - datetime.timedelta(days=days - 1)
        return replace(self, preset="custom", start=start, end=end, year=None, default_section=False)

    def narrow(self, **kw: Any) -> Scope:
        """The scope narrowed by ``kw`` (the chat tools' arguments): the intersection only. A value
        outside the bound scope empties it; nothing ever widens it."""
        scope = self
        empty = self.empty
        for key, value in kw.items():
            if value in (None, "", (), []):
                continue
            if key in (
                "sections",
                "offices",
                "partners",
                "entity_types",
                "ratings",
                "statuses",
                "modalities",
                "quality_bands",
                "urgency_levels",
            ):
                given = tuple(value) if isinstance(value, list | tuple | set) else (value,)
                if key == "partners":
                    given = _ints([str(v) for v in given])
                bound = getattr(self, key)
                kept = tuple(v for v in given if not bound or v in bound)
                empty = empty or not kept
                scope = replace(scope, **{key: tuple(sorted(set(kept)))})
            elif key in ("governorate", "pd"):
                if key == "governorate" and value != NONE:
                    from neurodb.reports.overview import governorate_key

                    value = governorate_key(str(value))
                bound = getattr(self, key)
                if bound not in (None, "") and bound != value:
                    empty = True
                scope = replace(scope, **{key: value})
            elif key in ("start", "end"):
                day = value if isinstance(value, datetime.date) else _iso(value)
                if day is None:
                    continue
                start = max(scope.start, day) if key == "start" else scope.start
                end = min(scope.end, day) if key == "end" else scope.end
                empty = empty or start > end
                scope = replace(scope, preset="custom", start=start, end=end, year=None)
            elif key == "programmatic":
                scope = replace(scope, programmatic=scope.programmatic or bool(value))
            elif key == "q":
                text = " ".join(str(value).split())[:Q_CHARS]
                if not scope.q:
                    scope = replace(scope, q=text)
                elif text and text != scope.q:  # both searches apply
                    scope = replace(scope, drill=scope.drill + (("q", text),))
            elif key in DRILL_KEYS and _drill_ok(key, str(value)):
                scope = replace(scope, drill=scope.drill + ((key, str(value)),))
        return replace(scope, empty=empty, default_section=False)

    # -------------------------------------------------------------------------------- wording
    def period_label(self) -> str:
        if self.preset == "all_time":
            return _("All time")
        if self.start.year == self.end.year:
            return f"{date_format(self.start, 'j M')} – {_day(self.end)}"
        return f"{_day(self.start)} – {_day(self.end)}"

    def label(self) -> str:
        """ "1 Jan – 31 Dec 2026 · Education · Bekaa": the period, the sections and the place."""
        from django.conf import settings

        parts = [self.period_label()]
        sections = [_("No section") if s == NONE else s for s in self.sections]
        if sections:
            parts.append(", ".join(sections))
        if self.governorate == NONE:
            parts.append(_("Not located"))
        elif self.governorate:
            from neurodb.reports.overview import governorate_names

            parts.append(governorate_names().get(self.governorate, self.governorate.title()))
        else:
            parts.append(getattr(settings, "ETOOLS_DATAMART_COUNTRY", "") or _("the country"))
        return " · ".join(parts)


def _drill(qs: QuerySet, key: str, value: str, drill: tuple[tuple[str, str], ...]) -> QuerySet:
    """One drill-down applied to ``qs`` (codes only: see ``_drill_ok``)."""
    from .models import VisitReview, VisitRuleResult

    if key == "month":
        year, month = (int(part) for part in value.split("-"))
        return qs.filter(visit_date__year=year, visit_date__month=month)
    if key == "visit_status":
        return qs.filter(status=value)
    if key == "hact_q1":
        return qs.filter(hact_q1="" if value == NONE else value)
    if key == "bucket":
        if value == NONE:
            return qs.filter(quality_score=None)
        low, high = BUCKETS[value]
        qs = qs.filter(quality_score__gte=low)
        return qs.filter(quality_score__lte=high) if high == 100 else qs.filter(quality_score__lt=high)
    if key == "flag":
        return qs.filter(flags__contains=[value])
    if key == "flags":
        count, more = _FLAGS.match(value).groups()
        qs = qs.exclude(quality_score=None)  # flags per visit count the scored visits
        return qs.filter(flag_count__gte=int(count)) if more else qs.filter(flag_count=int(count))
    if key == "urgency":  # "none": below amber; a visit without urgency (not scored) is in no band
        if value == NONE:
            return qs.filter(urgency_band="", urgency__isnull=False)
        return qs.filter(urgency_band=value)
    if key == "location":
        return qs.filter(location_id=int(value))
    if key == "issue":
        rule, detail_key = _ISSUE.match(value).groups()
        results = VisitRuleResult.objects.filter(
            visit=OuterRef("pk"), rule=rule, detail_key=detail_key, status="fail"
        )
        return qs.filter(Exists(results))
    if key == "rule":
        state = dict(drill).get("rule_state", "fail")
        return qs.filter(
            Exists(VisitRuleResult.objects.filter(visit=OuterRef("pk"), rule=value, status=state))
        )
    if key == "review":
        latest = VisitReview.objects.filter(visit_key=OuterRef("key")).order_by("-created_at", "-pk")
        qs = qs.annotate(_review=Subquery(latest.values("status")[:1]))
        return qs.filter(_review=None) if value == NONE else qs.filter(_review=value)
    if key == "q":
        from .build import search_text

        return qs.filter(search__contains=search_text(value))
    return qs  # rule_state is read with rule


def link(**params: Any) -> str:
    """An address of the Monitoring insights page: ``reverse("fmm:dashboard")`` plus ``params`` (a list
    gives a repeated key), with ``section=`` added when absent. Every link into FMM from another page
    is built with it, so a user with a section sees the same figures as the page they came from."""
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        for item in value if isinstance(value, list | tuple) else [value]:
            if item is None:
                continue
            pairs.append((key, "1" if item is True else str(item)))
    if "section" not in params:
        pairs.append(("section", ""))
    return f"{reverse('fmm:dashboard')}?{urlencode(pairs)}"


def options(when: str | None = None) -> dict[str, list]:
    """The filter bar's choices: the eTools section names, field offices and monitoring modalities of
    the visits, the gazetteer's governorates (``[key, name]``), the partners with visits (``[id,
    name]``) and the years of the visits' dates (start, else end). Kept ten minutes per refresh (``when``:
    ``metrics.stamp``)."""
    from django.core.cache import cache
    from django.db import connection

    from neurodb.partnerships.models import PartnerOrganization
    from neurodb.reports.overview import governorate_names

    from .models import Visit

    if when is None:
        from .metrics import stamp

        when = stamp()
    key = f"fmm:v3:options:{when}"
    found = cache.get(key)
    if found is not None:
        return found
    table = Visit._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT ARRAY(SELECT DISTINCT unnest(section_names) FROM {table}), "  # noqa: S608
            f"ARRAY(SELECT DISTINCT unnest(offices) FROM {table}), "
            f"ARRAY(SELECT DISTINCT unnest(partner_ids) FROM {table}), "
            f"ARRAY(SELECT DISTINCT EXTRACT(YEAR FROM visit_date)::int FROM {table} "
            "WHERE visit_date IS NOT NULL), "
            f"ARRAY(SELECT DISTINCT modality FROM {table} WHERE modality <> '')"
        )
        sections, offices, partner_ids, years, modalities = cursor.fetchone()

    def names(values) -> list[str]:
        return sorted({v.strip() for v in values or () if v and v.strip()}, key=str.casefold)

    partners = [
        [p.pk, p.short_name or p.name]
        for p in PartnerOrganization.objects.filter(pk__in=list(partner_ids or ())).only(
            "pk", "name", "short_name"
        )
    ]
    found = {
        "sections": names(sections),
        "offices": names(offices),
        "governorates": [[k, name] for k, name in governorate_names().items()],
        "partners": sorted(partners, key=lambda p: str(p[1]).casefold()),
        "years": sorted({int(y) for y in years or ()}, reverse=True),
        "modalities": names(modalities),
    }
    cache.set(key, found, 600)
    return found
