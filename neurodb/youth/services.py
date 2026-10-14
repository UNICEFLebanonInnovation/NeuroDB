"""The youth dashboard: what Compiler counted, per indicator, and what partners reported in eTools.

Every figure is a count of unique young people read from the one grouping Compiler sent for that
question (see figures.py), so rows are never added up. Targets come from the Compiler programme
documents; they are not set per place, so a governorate filter hides them.

The page (``youth.views.dashboard``) loads in parts: its two parts read one :func:`build`, kept ten minutes in
the cache every worker shares (:func:`neurodb.core.compute.once`) under a key that holds the year's stored
figures (their row and the time they were read from Compiler), the filters, the day, the year's links to
eTools (which staff confirm, correct or add in the admin, :func:`links_stamp`), the card versions an
administrator chose (:func:`neurodb.education.power_bi.cards_stamp`) and
:func:`neurodb.core.compute.data_stamp` (the last finished sync, which covers eTools and Compiler, and the
indicator flags). A build is plain data, the same for every user; its breakdowns' names are translated as it
is computed, so it is kept per language. The filter bar's choices (:func:`filter_options`) are read in the
database from the few lists they need, never from the whole payload, and kept for every worker the same way.

From payload format 2 Compiler counts the registrations of Power BI's view ``vw_youth_data_all`` (the line
under the key figures says so, :func:`neurodb.education.power_bi.note`), and sends the registered young people
(enrolled or not) and the enrolments beside the figures: the headline and the partners are counted with the
version an administrator chose (today's meaning by default), the headline's second line gives the registered
young people, and a breakdown gives them by the governorate of their address. "All years" (the year
``YouthFigures.ALL``) is the count over every year, registrations without a year included; it has no targets.
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import connection
from django.http import QueryDict
from django.utils.dateparse import parse_date
from django.utils.translation import gettext_lazy as _

from neurodb.core import compute
from neurodb.education import power_bi

from . import linking
from .figures import AGE_ORDER, Figures, indicator_label
from .models import YouthFigures, YouthIndicatorLink

CACHE_SECONDS = (
    600  # a build or the filter's choices in the shared cache; the key moves with every sync and edit
)
CACHE_VERSION = 1

BREAKDOWNS = (
    ("partner", _("By partner")),
    ("donor", _("By donor")),
    ("governorate", _("By governorate")),
    ("district", _("By district")),
    ("sex", _("By sex")),
    ("age_group", _("By age group")),
    ("nationality", _("By nationality")),
)


def _int(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() and len(raw) < 12 else None


@dataclass
class YouthFilters:
    year: str = ""
    partner: int | None = None
    donor: int | None = None
    governorate: int | None = None
    master: int | None = None

    @classmethod
    def from_params(cls, params: QueryDict, years: list[str]) -> YouthFilters:
        """``years``: the periods the page offers, the latest year first (``YouthFigures.ALL`` among them when
        the figures of all years together are stored)."""
        year = params.get("year", "")
        return cls(
            year=year if year in years else (years[0] if years else ""),
            partner=_int(params.get("partner")),
            donor=_int(params.get("donor")),
            governorate=_int(params.get("governorate")),
            master=_int(params.get("master")),
        )

    def main(self) -> dict[str, int]:
        values = {"partner": self.partner, "donor": self.donor, "governorate": self.governorate}
        return {k: v for k, v in values.items() if v is not None}

    @property
    def active(self) -> bool:
        return bool(self.main() or self.master)


def stored_years() -> tuple[list[str], bool]:
    """The stored reporting years, latest first, and whether the figures of all years together
    (``YouthFigures.ALL``) are stored too: one query."""
    stored = list(YouthFigures.objects.order_by("-year").values_list("year", flat=True))
    return [y for y in stored if y != YouthFigures.ALL], YouthFigures.ALL in stored


def years() -> list[str]:
    """The stored reporting years, latest first (the figures of all years together are not a year)."""
    return list(YouthFigures.objects.yearly().order_by("-year").values_list("year", flat=True))


def load(year: str) -> YouthFigures | None:
    return YouthFigures.objects.filter(year=year).first()


def stored_row(year: str) -> YouthFigures | None:
    """The year's stored figures without their payload (3 MB a year): what the page's frame and the keys
    read."""
    return YouthFigures.objects.defer("payload").filter(year=year).first()


# ------------------------------------------------------------------------------ plain rows
@dataclass(frozen=True)
class LinkRef:
    """A youth indicator link as the page draws it (plain data: a build is kept for every worker)."""

    pk: int
    pd_id: int | None
    etools_title: str
    source: str


@dataclass(frozen=True)
class EtoolsRef:
    """The eTools indicator of a link, with what the partner reported, as the page draws it."""

    key: str
    pd_id: int
    url: str
    target: float | None
    reached: float | None
    reached_in_year: float | None
    tracking: str
    tracking_label: str
    prp_cumulative: float | None
    mismatch: bool

    @classmethod
    def of(cls, indicator: Any) -> EtoolsRef:
        return cls(
            key=indicator.key,
            pd_id=indicator.pd.id,
            url=indicator.url,
            target=indicator.target,
            reached=indicator.reached,
            reached_in_year=indicator.reached_in_year,
            tracking=indicator.tracking,
            tracking_label=str(indicator.tracking_label),
            prp_cumulative=indicator.prp_cumulative,
            mismatch=indicator.mismatch,
        )


def _targets(figures: Figures, filters: YouthFilters) -> dict[tuple[str, int], float] | None:
    """Target per indicator, summed over the programme documents that match the filters."""
    if filters.governorate is not None:
        return None
    documents = figures.program_documents
    out: dict[tuple[str, int], float] = {}
    for t in figures.targets:
        document = documents.get(t["program_document"])
        if document is None or t["target"] is None:
            continue
        if filters.partner is not None and document["partner"] != filters.partner:
            continue
        if filters.donor is not None and filters.donor not in document["donors"]:
            continue
        ident = ("sub", t["sub"]) if t["sub"] else ("master", t["master"])
        out[ident] = out.get(ident, 0) + t["target"]
    return out


def _percent(part: float | None, whole: float | None) -> float | None:
    return round(part / whole * 100, 1) if part is not None and whole else None


def _links(figures: Figures, filters: YouthFilters) -> tuple[list[dict[str, Any]], dict]:
    """Every link of the year (under the partner and donor filters), with both sides' figures."""
    rows = linking.link_rows(figures.year)
    documents = figures.program_documents
    if filters.partner is not None or filters.donor is not None:
        keep = []
        for link in rows:
            document = documents.get(link.compiler_pd_id) or {}
            if filters.partner is not None and document.get("partner") != filters.partner:
                continue
            if filters.donor is not None and filters.donor not in document.get("donors", []):
                continue
            keep.append(link)
        rows = keep
    etools = linking.describe(rows, figures.year)
    by_pd_sub = figures.table({}, "sub", "program_document") or {}
    by_pd_master = figures.table({}, "master", "program_document") or {}
    targets = {}
    for t in figures.targets:
        ident = ("sub", t["sub"]) if t["sub"] else ("master", t["master"])
        key = (t["program_document"], *ident)
        targets[key] = (targets.get(key) or 0) + (t["target"] or 0)
    out = []
    by_indicator: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for link in rows:
        if link.level == "sub":
            counted = by_pd_sub.get((link.youth_indicator_id, link.compiler_pd_id))
        else:
            counted = by_pd_master.get((link.youth_indicator_id, link.compiler_pd_id))
        indicator = etools.get((link.pd_id, link.etools_key)) if link.pd_id else None
        # what the partner reached in the same year as Compiler's count, never the PD to date
        reported = indicator.reached_in_year if indicator else None
        document = documents.get(link.compiler_pd_id) or {}
        entry = {
            "link": LinkRef(pk=link.pk, pd_id=link.pd_id, etools_title=link.etools_title, source=link.source),
            "compiler_pd": link.compiler_pd_code or document.get("project_name") or "—",
            "partner": figures.label("partner", document.get("partner")) if document.get("partner") else "—",
            "youth_indicator": link.youth_indicator,
            "counted": counted or 0,
            "compiler_target": targets.get((link.compiler_pd_id, link.level, link.youth_indicator_id))
            or None,
            "etools": EtoolsRef.of(indicator) if indicator else None,
            "reported": reported,
            "year": linking.year_number(figures.year),
            "difference": (counted or 0) - reported if reported is not None else None,
        }
        out.append(entry)
        by_indicator.setdefault((link.level, link.youth_indicator_id), []).append(entry)
    return out, by_indicator


def _indicator_rows(figures: Figures, filters: YouthFilters, by_indicator: dict) -> list[dict[str, Any]]:
    main = filters.main()
    targets = _targets(figures, filters)
    per_master = figures.table(main, "master") or {}
    per_sub = figures.table(main, "sub") or {}
    rows = []
    for master in sorted(figures.masters(), key=lambda m: (m["number"] or "", m["name"])):
        if filters.master is not None and master["id"] != filters.master:
            continue
        reached = per_master.get((master["id"],), 0)
        target = targets.get(("master", master["id"])) if targets is not None else None
        subs = []
        for sub in sorted(figures.subs_of(master["id"]), key=lambda s: (s["number"] or "", s["name"])):
            sub_reached = per_sub.get((sub["id"],), 0)
            sub_target = targets.get(("sub", sub["id"])) if targets is not None else None
            if not sub_reached and not sub_target:
                continue
            subs.append(
                {
                    "id": sub["id"],
                    "label": indicator_label(sub),
                    "reached": sub_reached,
                    "target": sub_target,
                    "percent": _percent(sub_reached, sub_target),
                    "links": by_indicator.get(("sub", sub["id"]), []),
                }
            )
        if not reached and not target and not subs:
            continue
        rows.append(
            {
                "id": master["id"],
                "label": indicator_label(master),
                "reached": reached,
                "target": target,
                "percent": _percent(reached, target),
                "links": by_indicator.get(("master", master["id"]), []),
                "subs": subs,
            }
        )
    return rows


def _home_governorates(figures: Figures, filters: dict[str, int]) -> dict[str, Any] | None:
    """Registered young people (enrolled or not) by the governorate of their address (format 2's
    ``registered`` block): None for a payload without it; ``rows`` None when the block has no grouping for
    the filters (it answers the partner only: not the master indicator, the donor or the place of the
    enrolment)."""
    from neurodb.education.figures import Block

    data = (figures.payload.get("blocks") or {}).get("registered")
    if not isinstance(data, dict):
        return None
    table = Block(data).table(filters, "home_governorate")
    if table is None:
        return {"rows": None}
    rows = [(figures.label("governorate", key[0]), m.get("people") or 0) for key, m in table.items()]
    return {"rows": sorted(rows, key=lambda r: (-r[1], r[0]))}


def _breakdown(figures: Figures, filters: dict[str, int], dimension: str) -> list[tuple[str, int]]:
    table = figures.table(filters, dimension) or {}
    rows = [(figures.label(dimension, key[0]), count) for key, count in table.items()]
    if dimension == "age_group":
        return sorted(rows, key=lambda r: AGE_ORDER.index(r[0]) if r[0] in AGE_ORDER else len(AGE_ORDER))
    return sorted(rows, key=lambda r: (-r[1], r[0]))


def _programme_documents(figures: Figures, filters: dict[str, int]) -> list[dict[str, Any]]:
    table = figures.table(filters, "program_document") or {}
    rows = []
    for (pd_id,), count in table.items():
        document = figures.program_documents.get(pd_id) or {}
        if pd_id is None:
            document = {"project_name": str(_("Activities without a programme document"))}
        rows.append(
            {
                "code": document.get("project_code") or "—",
                "name": document.get("project_name") or "",
                "partner": figures.label("partner", document["partner"]) if document.get("partner") else "—",
                "donors": ", ".join(figures.label("donor", d) for d in document.get("donors", [])),
                "period": tuple(parse_date(document.get(k) or "") for k in ("start_date", "end_date")),
                "youth": count,
            }
        )
    return sorted(rows, key=lambda r: (-r["youth"], r["code"]))


def options(figures: Figures) -> dict[str, list[tuple[int, str]]]:
    def listed(dimension: str) -> list[tuple[int, str]]:
        ids = [key[0] for key in (figures.table({}, dimension) or {}) if key[0] is not None]
        return sorted(((i, figures.label(dimension, i)) for i in ids), key=lambda o: o[1].lower())

    return {
        "partners": listed("partner"),
        "donors": listed("donor"),
        "governorates": listed("governorate"),
        "masters": [
            (m["id"], indicator_label(m))
            for m in sorted(figures.masters(), key=lambda m: (m["number"] or "", m["name"]))
        ],
    }


def dashboard(record: YouthFigures, filters: YouthFilters) -> dict[str, Any]:
    """Every figure of the youth page (and of Ask NeuroDB's youth look-up) for the year's stored figures and
    the filters, as plain data: the links are :class:`LinkRef` and :class:`EtoolsRef`. The filter bar's
    choices are :func:`filter_options`."""
    figures = Figures(record.payload)
    main = filters.main()
    scoped = {**main, **({"master": filters.master} if filters.master is not None else {})}
    links, by_indicator = _links(figures, filters)
    indicators = _indicator_rows(figures, filters, by_indicator)
    breakdowns = [
        {"key": key, "label": str(label), "rows": _breakdown(figures, scoped, key)}
        for key, label in BREAKDOWNS
    ]
    sexes = dict(_breakdown(figures, scoped, "sex"))
    # the headline and the partners, counted with the version an administrator chose (power_bi.VARIANTS)
    reached = power_bi.card_value("youth", "reached", figures, scoped)
    partners = power_bi.card_value("youth", "partners", figures, scoped)
    other = "registered_people" if reached["variant"] == "enrolled_people" else "enrolled_people"
    every_year = filters.year == YouthFigures.ALL
    return {
        "total": reached["value"],
        "female": sexes.get("Female", 0),
        "male": sexes.get("Male", 0),
        "partners": partners["value"],
        "cards": {"reached": reached, "partners": partners},
        # the headline's second line: registered young people (format 2), or the enrolled ones when another
        # version is shown
        "second": {
            "variant": other,
            "value": power_bi.variant_value("youth", "reached", other, figures, scoped),
        },
        "home_governorates": _home_governorates(figures, scoped),
        "power_bi": power_bi.page_note("youth", record.year, power_bi.counted_at(figures.payload)),
        "every_year": every_year,
        "indicators": indicators,
        "breakdowns": breakdowns,
        "programme_documents": _programme_documents(figures, scoped),
        "links": links,
        "linked": sum(1 for link in links if link["link"].source == YouthIndicatorLink.Source.CONFIRMED),
        "suggested": sum(1 for link in links if link["link"].source == YouthIndicatorLink.Source.SUGGESTED),
        "fetched_at": record.fetched_at,
        "generated_at": figures.payload.get("generated_at"),
        "targets_hidden": filters.governorate is not None or every_year,
    }


# ------------------------------------------------------------------------------ the filter bar
# the lists of the payload the filter's choices read, and the groupings of each choice alone
_OPTION_LISTS = ("year", "partners", "donors", "locations", "indicators")
_OPTION_GROUPINGS = (["partner"], ["donor"], ["governorate"])


def _options_payload(record: YouthFigures) -> dict[str, Any]:
    """The part of the stored payload :func:`options` reads, cut out in the database: the year, the names of
    the partners, donors, places and indicators, and the three groupings of a partner, a donor or a
    governorate alone (a few KB of 3 MB, which Python then never decodes)."""
    table = connection.ops.quote_name(YouthFigures._meta.db_table)
    lists = ", ".join(f"'{name}', payload->'{name}'" for name in _OPTION_LISTS)
    sql = (
        f"SELECT jsonb_build_object({lists}, 'figures', (SELECT coalesce(jsonb_agg(g), '[]'::jsonb) "  # noqa: S608
        "FROM jsonb_array_elements(payload->'figures') g WHERE g->'by' = ANY(%s::jsonb[])))::text "
        f"FROM {table} WHERE id = %s"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, [[json.dumps(by) for by in _OPTION_GROUPINGS], record.pk])
        row = cursor.fetchone()
    return json.loads(row[0]) if row else {}


def _options_key(record: YouthFigures) -> str:
    stamp = record.fetched_at.isoformat() if record.fetched_at else ""
    return f"youth-options:v{CACHE_VERSION}:{record.pk}:{stamp}"


def filter_options(record: YouthFigures) -> dict[str, list[tuple[int, str]]]:
    """The filter bar's choices for the year's stored figures (:func:`options`), kept ten minutes for every
    worker under the row and the time its figures were read from Compiler (a build stores them too, from the
    payload it decodes anyway). The names come from Compiler, never translated, so one value serves every
    language."""
    if settings.DEBUG:
        return options(Figures(_options_payload(record)))
    return compute.once(
        _options_key(record),
        lambda: options(Figures(_options_payload(record))),
        CACHE_SECONDS,
        lang_neutral=True,
    )


# ------------------------------------------------------------------------------ the stored build
def links_stamp(year: str) -> str:
    """The year's links to eTools as a short text that changes with every link confirmed, corrected, added
    or removed (in the admin's form or by its *Confirm* action, which changes no time of change) and with
    every sync, which replaces the suggestions: a digest of the rows themselves, in one query. Read once per
    request."""

    def read() -> str:
        table = connection.ops.quote_name(YouthIndicatorLink._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT md5(coalesce(string_agg(t::text, ',' ORDER BY t.id), '')) FROM {table} t "  # noqa: S608
                "WHERE t.year = %s",
                [year],
            )
            return cursor.fetchone()[0]

    return compute.per_request(f"youth:links:{year}", read)


def fingerprint(year: str) -> str:
    """What a build of the year depends on besides its figures, filters and day, as 16 hex digits: the
    year's links (:func:`links_stamp`), the card versions an administrator chose
    (:func:`neurodb.education.power_bi.cards_stamp`) and :func:`neurodb.core.compute.data_stamp`. In the
    build's key, and in the address of each part of the page (``as_of``)."""
    return compute.fingerprint(f"{links_stamp(year)}:{power_bi.cards_stamp()}:{compute.data_stamp()}")


def _cache_key(record: YouthFigures, filters: YouthFilters, today: datetime.date, stamp: str = "") -> str:
    """The stored figures (their row and when they were read from Compiler), the filters, the day (the eTools
    status of each link follows it) and :func:`fingerprint` (read now unless given). Nothing about the person
    asking."""
    fetched = record.fetched_at.isoformat() if record.fetched_at else ""
    chosen = json.dumps(
        [filters.year, filters.partner, filters.donor, filters.governorate, filters.master],
        separators=(",", ":"),
    )
    return (
        f"youth:v{CACHE_VERSION}:{record.pk}:{fetched}:{chosen}:{today.isoformat()}:"
        f"{stamp or fingerprint(record.year)}"
    )


def build(
    record: YouthFigures, filters: YouthFilters, today: datetime.date, *, as_of: str = ""
) -> dict[str, Any]:
    """:func:`dashboard`, computed once across every worker and kept ten minutes (per language). ``record``
    may be read without its payload (:func:`stored_row`): the payload is read only to compute. ``as_of``, in
    a part request, is the :func:`fingerprint` the page's frame read: both parts show one build even when a
    link was confirmed or a sync came before or between them (:func:`neurodb.core.compute.once_as_of`)."""

    def compute_it() -> dict[str, Any]:
        record.refresh_from_db(fields=["payload"])
        value = dashboard(record, filters)
        try:  # the filter bar's choices, from the payload decoded here, unless another request is at it
            compute.once(
                _options_key(record),
                lambda: options(Figures(record.payload)),
                CACHE_SECONDS,
                wait=0,
                lang_neutral=True,
            )
        except compute.Pending:
            pass
        return value

    if settings.DEBUG:
        return compute_it()
    return compute.once_as_of(
        lambda stamp: _cache_key(record, filters, today, stamp),
        as_of,
        fingerprint(record.year),
        compute_it,
        CACHE_SECONDS,
    )


def stored(record: YouthFigures, filters: YouthFilters, today: datetime.date) -> bool:
    """Whether :func:`build` would answer at once, from what is already stored: the page's frame then draws
    both parts inline (it never computes the build itself)."""
    if settings.DEBUG:
        return False
    return compute.peek(_cache_key(record, filters, today))[0]
