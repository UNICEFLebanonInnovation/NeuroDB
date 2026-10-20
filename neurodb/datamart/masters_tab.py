"""The Master indicators tab of Partner monitoring (``view=masters``): the eTools master indicators of the
chosen sections in one year with their partners' PD indicators (:mod:`neurodb.datamart.masters`), the masters
NeuroDB proposes and the PD indicators in no master, as the tab's parts, its detail and its export read them.

**Its own filter** (:func:`rows_filter`). The PD indicators are partner monitoring's stored rows
(:func:`neurodb.datamart.monitoring.stored_indicators`) of the sections, the year, every programme document
running in it (closed ones too, as the nightly job reads them), the report type and the locations (Reached's
own location filter). The status, the search and the CP output choose master rows only, never their subs: a
master's figure never depends on them. The PD status, document type, office, partner type, emergency,
programme document and title tag filters are not applied (:data:`NOT_APPLIED`), and the tab says so. A partner
filter shows that partner's contribution to each master it takes part in, with its share of a sum (a share of
a percentage or an average means nothing: the rule of the ActivityInfo partner pages).

**Speed.** One stored read of the chosen sections, and at most one more by programme document for the linked
indicators it does not hold (an indicator that moved to another section, or a programme document whose other
indicators are elsewhere). The figures are plain Python over those rows; the masters, their links and years
are read from the database at each request, so an edit shows at once and nothing else is stored.

Nothing here reads out who updated or confirmed anything (``updated_by``, ``confirmed_by``) or an e-mail
address, and no AI is used.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Prefetch, prefetch_related_objects

from neurodb.partnerships import statuses as pd_statuses

from . import headline, master_links, masters, monitoring
from .masters import Figure, Row, Sub, key_of
from .models import EtoolsMaster, EtoolsMasterLink, PDIndicator
from .reached import norm
from .tags import TAG_FIELDS

MASTER = EtoolsMaster.Status
Method, Role, Status = EtoolsMaster.Method, EtoolsMasterLink.Role, EtoolsMasterLink.Status
UNATTACHED_LIMIT = 500  # PD indicators the "Not in a master" list shows (the counts are complete)
PROPOSAL_TITLES = 5  # PD indicators named under each proposed master (then "and N more")
QUARTER_LABELS = ("Q1", "to Q2", "to Q3", "Year")  # Reached from 1 January: the fourth is the year

# the partner monitoring filters this tab does not apply: a master counts every programme document of its
# section running in the year
NOT_APPLIED = {
    "pd_status": "PD status",
    "document_type": "document type",
    "office": "office",
    "partner_type": "partner type",
    "emergency": "emergency",
    "pd": "programme document",
    **{tag: tag.replace("_", " ") for tag in TAG_FIELDS},
}

Curator = Callable[[str], bool]  # may this person curate the masters of an eTools section


# ------------------------------------------------------------------------------------------------ the filter
def rows_filter(filters: monitoring.Filters, sections: Sequence[str] | None = None) -> monitoring.Filters:
    """The partner monitoring filter the tab reads its PD indicators with: the chosen sections (or
    ``sections``), the year (this year without one), every programme document running in it, the report type
    and the locations. Nothing else: the other filters would drop subs and change the masters' figures."""
    return monitoring.Filters(
        sections=list(filters.sections if sections is None else sections),
        year=filters.year or monitoring.today().year,
        scope="year",
        report_type=filters.report_type,
        locations=list(filters.locations),
    )


def not_applied(params) -> list[str]:
    """The partner monitoring filters set in ``params`` (the page's query) that this tab does not apply."""
    return [label for key, label in NOT_APPLIED.items() if any(v for v in params.getlist(key))]


def curator(user) -> Curator:
    """Who may curate a section's masters on the tab (accept or dismiss a proposal, confirm, reject, change a
    role, attach, remove, rename, set the year's code and target): an Administrator, or a Section editor whose
    NeuroDB section is the eTools section's (:func:`neurodb.watch.sections.resolve_names`), the rule of
    :func:`neurodb.datamart.headline.can_set`. Viewers and donors never. The section map is read once."""
    from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, role_of

    if user is None or not user.is_authenticated:
        return lambda section: False
    role = role_of(user)
    if role == ADMIN:
        return lambda section: True
    if role != SECTION_EDITOR or not getattr(user, "section_id", None):
        return lambda section: False
    from neurodb.watch.sections import confirmed_map, resolve_names

    mapping = confirmed_map()
    return lambda section: user.section_id in resolve_names([section], mapping)[0]


def can_curate(user, section: str) -> bool:
    """:func:`curator` for one section (the POST views)."""
    return curator(user)(section)


# ------------------------------------------------------------------------------------------------- reading
def _masters(sections: Sequence[str] = (), pk: int | None = None) -> list[EtoolsMaster]:
    """The active and proposed masters of the sections (every section when none is chosen), or the one
    ``pk``, with their links (and each link's programme document) and years."""
    found = EtoolsMaster.objects.filter(status__in=(MASTER.ACTIVE, MASTER.PROPOSED))
    if pk is not None:
        found = found.filter(pk=pk)
    wanted = {norm(s) for s in sections if s.strip()}
    chosen = [
        m
        for m in found.order_by("section", "sequence", "name", "pk")
        if not wanted or norm(m.section) in wanted
    ]
    prefetch_related_objects(
        chosen,
        Prefetch(
            "links",
            queryset=EtoolsMasterLink.objects.select_related("pd__partner").order_by("sequence", "id"),
        ),
        "years",
    )
    return chosen


def _read(
    chosen: Sequence[EtoolsMaster], base: monitoring.Filters, today: datetime.date, as_of: str
) -> tuple[list[Row], dict[tuple[int, str], Row], set[int]]:
    """The tab's PD indicators: the stored rows of ``base`` and, read once more by programme document, those
    of the masters' links it does not hold. Returns the rows of ``base``, every row by key, and the programme
    documents whose indicators of the year are held in full (``running`` of
    :func:`neurodb.datamart.masters.figures`)."""
    rows = monitoring.stored_indicators(base, today, as_of=as_of)
    by_key = {key_of(row): row for row in rows}
    running = {row.pd.id for row in rows}
    missing = sorted(
        {
            link.pd_id
            for master in chosen
            for link in master.links.all()
            if link.pd_id and link.status != Status.REJECTED and (link.pd_id, link.ident) not in by_key
        }
    )
    if missing:
        by_pd = dataclasses.replace(base, sections=[], pd_ids=missing)
        for row in monitoring.stored_indicators(by_pd, today, as_of=as_of):
            by_key.setdefault(key_of(row), row)
            running.add(row.pd.id)
    return rows, by_key, running


def _figures(
    chosen: Sequence[EtoolsMaster],
    rows: Iterable[Row],
    running: set[int],
    base: monitoring.Filters,
    today: datetime.date,
) -> list[Figure]:
    """:func:`neurodb.datamart.masters.figures` of the masters, with the children guard of a sum. With a
    location filter, a link whose indicator is not planned at the chosen places is not listed (it is not "no
    longer in eTools")."""
    year = base.year
    sums = any(m.method == Method.SUM for m in chosen)
    out = masters.figures(
        chosen,
        rows,
        year,
        today,
        running=running,
        national=headline.national_children(year) if sums else None,
        is_children=headline.children_rule() if sums else None,
    )
    if base.locations:
        gone = {(s.link.pd_id, s.link.ident) for f in out for s in f.subs if s.reason == masters.GONE}
        if gone:
            existing = _existing({pd_id for pd_id, _ident in gone})
            for f in out:
                f.subs = [
                    s
                    for s in f.subs
                    if s.reason != masters.GONE or (s.link.pd_id, s.link.ident) not in existing
                ]
    return out


def _existing(pd_ids: Iterable[int]) -> set[tuple[int, str]]:
    """The PD indicators of these programme documents in eTools now, by key, wherever they are planned."""
    rows = PDIndicator.objects.filter(intervention_id__in=sorted(pd_ids)).values_list(
        "intervention_id", "source_id", "title"
    )
    return {
        (pd_id, masters.ident_code(monitoring.ident_of(pd_id, source_id, title)))
        for pd_id, source_id, title in rows.distinct()
    }


# --------------------------------------------------------------------------------------------- what is shown
@dataclass
class SubLine:
    """One sub row of a master: its PD indicator's figures as the tab shows and exports them."""

    sub: Sub
    partner: str
    partner_id: int | None
    pd_id: int | None
    pd_number: str
    title: str
    tags: dict[str, str]
    url: str  # its partner monitoring detail ("" when no longer in eTools)
    target: float | None  # the PD's own target (its whole period)
    quarters: list[float | None]
    parts: bool  # a percentage or a ratio: its numerator and denominator are shown
    percentage: bool
    pd_status: str
    method_label: str
    tracking: str  # its own status over its PD period, as Program Performance shows it
    tracking_label: str
    monthly: bool  # read from the monthly humanitarian reports (no quarterly report)
    report_type: str
    flags: list[str] = field(default_factory=list)

    @property
    def link(self) -> EtoolsMasterLink:
        return self.sub.link

    @property
    def suggested(self) -> bool:
        return self.sub.status == Status.SUGGESTED

    @property
    def strong(self) -> bool:
        return self.suggested and (self.link.score or 0) >= master_links.STRONG

    @property
    def reconfirm(self) -> bool:
        """A suggestion, or a confirmed link whose type changed in eTools: confirming it (again) counts it."""
        return self.suggested or self.sub.reason == masters.TYPE_CHANGED


@dataclass
class Line:
    """A master row of the tab: its figure (with a partner filter, that partner's contribution), the master's
    whole figure (the status, always), the partner's share of a sum, and its sub rows."""

    figure: Figure
    whole: Figure
    subs: list[SubLine]
    share: float | None = None
    curate: bool = False

    @property
    def master(self) -> EtoolsMaster:
        return self.figure.master

    @property
    def quarters(self) -> list[float | None]:
        return [self.figure.quarters.get(q) for q in masters.QUARTERS]

    @property
    def suggested(self) -> int:
        return sum(1 for s in self.subs if s.suggested)

    @property
    def strong(self) -> int:
        return sum(1 for s in self.subs if s.strong)

    @property
    def percentage(self) -> bool:
        return self.master.method == Method.PERCENT

    @property
    def hints(self) -> list[str]:
        """What to know before reading the figure: counted programme documents that plan the same places (a
        sum: partners may reach the same people there), subs read from monthly reports, subs whose indicator
        moved to another section."""
        out = []
        counted = [s for s in self.subs if s.sub.counted and s.sub.row is not None]
        if self.master.method == Method.SUM:
            places: dict[str, tuple[str, set[str]]] = {}
            for s in counted:
                for place in s.sub.row.locations:
                    name, pds = places.setdefault(norm(place), (place, set()))
                    pds.add(s.pd_number or "a PD")
            shared = sorted((name, sorted(pds)) for name, pds in places.values() if len(pds) > 1)
            for name, pds in shared[:3]:
                joined = " and ".join(pds) if len(pds) == 2 else ", ".join(pds[:-1]) + f" and {pds[-1]}"
                out.append(f"{joined} both plan {name}: partners may reach the same people there.")
            if len(shared) > 3:
                out.append(f"{len(shared) - 3} more places are planned by two programme documents or more.")
        monthly = sum(1 for s in self.subs if s.monthly)
        if monthly:
            out.append(
                f"{monthly} PD indicator{'s are' if monthly > 1 else ' is'} read from the monthly "
                "humanitarian reports: no quarterly report has a value for it."
            )
        moved = sum(1 for s in self.subs if s.sub.note)
        if moved:
            out.append(
                f"{moved} PD indicator{'s are' if moved > 1 else ' is'} now in another section in eTools: "
                "still read here."
            )
        return out


def _partner(pd) -> tuple[str, int | None]:
    if pd is None:
        return "", None
    partner = pd.partner
    if partner is not None:
        return partner.name, partner.id
    return pd.partner_name or "", pd.partner_id


def _sub_line(sub: Sub, report_type: str) -> SubLine:
    row, link = sub.row, sub.link
    if row is not None:
        partner, partner_id = _partner(row.pd)
        unit = sub.unit_class
        line = SubLine(
            sub=sub,
            partner=partner,
            partner_id=partner_id,
            pd_id=row.pd.id,
            pd_number=row.pd.number or "",
            title=row.title,
            tags={k: v for k, v in row.tags.items() if v},
            url=row.url,
            target=row.target,
            quarters=[sub.quarters.get(q) for q in masters.QUARTERS],
            parts=unit in (masters.PERCENTAGE, masters.RATIO),
            percentage=unit == masters.PERCENTAGE,
            pd_status=pd_statuses.label(row.pd.status),
            method_label=row.method_label,
            tracking=row.tracking,
            tracking_label=row.tracking_label,
            monthly=row.report_type != report_type,
            report_type=row.report_type,
        )
    else:  # no longer in eTools: what it was when linked
        partner, partner_id = _partner(link.pd)
        line = SubLine(
            sub=sub,
            partner=partner,
            partner_id=partner_id,
            pd_id=link.pd_id,
            pd_number=link.pd_number or (link.pd.number if link.pd else "") or "",
            title=link.title,
            tags={},
            url="",
            target=None,
            quarters=[None] * 4,
            parts=link.unit_class in (masters.PERCENTAGE, masters.RATIO),
            percentage=link.unit_class == masters.PERCENTAGE,
            pd_status=pd_statuses.label(link.pd.status) if link.pd else "",
            method_label="",
            tracking="",
            tracking_label="",
            monthly=False,
            report_type="",
        )
    if line.monthly:
        line.flags.append("From monthly reports")
    if sub.note:
        line.flags.append(sub.note)
    if row is not None and row.partial:
        line.flags.append("Partial history")
    if row is not None and row.mismatch:
        line.flags.append("Differs from PRP")
    return line


def _line(figure: Figure, whole: Figure, report_type: str, curate: Curator) -> Line:
    line = Line(
        figure=figure,
        whole=whole,
        subs=[_sub_line(s, report_type) for s in figure.subs],
        curate=curate(figure.master.section),
    )
    if (
        figure is not whole
        and figure.master.method == Method.SUM
        and whole.value
        and figure.value is not None
    ):
        line.share = figure.value * 100 / whole.value
    return line


def _keeps(line: Line, filters: monitoring.Filters) -> bool:
    """The master rows the status, the search (words of the name, code, unit or keywords) and the CP output
    keep: they choose masters, never subs."""
    master = line.master
    if filters.status and line.whole.status != filters.status:
        return False
    if filters.q and norm(filters.q) not in norm(
        " ".join((master.name, line.whole.code, master.unit, master.keywords))
    ):
        return False
    return not filters.cp_output or norm(filters.cp_output) in norm(master.cp_output)


def _grouped(lines: list[Line]) -> list[dict[str, Any]]:
    """Section -> CP output -> masters, as the ActivityInfo databases read."""
    sections: dict[str, dict[str, Any]] = {}
    for line in lines:
        master = line.master
        section = sections.setdefault(
            norm(master.section), {"section": master.section, "outputs": {}, "count": 0, "strong": 0}
        )
        section["count"] += 1
        section["strong"] += line.strong
        section["curate"] = line.curate
        output = section["outputs"].setdefault(
            norm(master.cp_output), {"cp_output": master.cp_output.strip(), "lines": []}
        )
        output["lines"].append(line)
    return [
        {
            **s,
            "outputs": sorted(
                s["outputs"].values(), key=lambda o: (not o["cp_output"], norm(o["cp_output"]))
            ),
        }
        for _key, s in sorted(sections.items())
    ]


@dataclass
class Proposed:
    """A master NeuroDB proposes, with its PD indicators (suggested links), for Accept or Dismiss."""

    master: EtoolsMaster
    links: list[EtoolsMasterLink]
    curate: bool = False

    @property
    def pds(self) -> int:
        return len({link.pd_id for link in self.links})

    @property
    def method_label(self) -> str:
        return masters.METHODS[self.master.method]["label"]

    @property
    def shown(self) -> list[EtoolsMasterLink]:
        return self.links[:PROPOSAL_TITLES]

    @property
    def more(self) -> int:
        return max(len(self.links) - PROPOSAL_TITLES, 0)


@dataclass
class Unattached:
    """A PD indicator of the year in no master (no confirmed link to an active one), with what NeuroDB says of
    it: its suggestion, the proposal it is in, "Ambiguous: A or B", or why it cannot join one."""

    row: Row
    unit: str
    partner: str
    partner_id: int | None
    note: str

    @property
    def key(self) -> str:
        pd_id, ident = key_of(self.row)
        return f"{pd_id}|{ident}"


@dataclass
class Tab:
    """Everything the tab's parts and its export read, for one filter."""

    year: int
    report_type: str
    partner: bool  # a partner filter: the lines are that partner's contributions
    lines: list[Line] = field(default_factory=list)
    groups: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[Proposed] = field(default_factory=list)
    unattached: list[Unattached] = field(default_factory=list)  # the first UNATTACHED_LIMIT
    unattached_count: int = 0
    indicators: int = 0  # the PD indicators of the year the filter reads...
    in_master: int = 0  # ...and those in a master (a confirmed link to an active one)
    attach: list[EtoolsMaster] = field(default_factory=list)  # the active masters this person may attach to

    @property
    def status_counts(self) -> dict[str, int]:
        counts = Counter(line.whole.status for line in self.lines)
        return {key: counts.get(key, 0) for key in monitoring.LABELS}

    @property
    def suggested(self) -> int:
        return sum(line.suggested for line in self.lines)

    @property
    def proposed_links(self) -> int:
        return sum(len(p.links) for p in self.proposals)

    @property
    def curates(self) -> bool:
        return (
            bool(self.attach)
            or any(p.curate for p in self.proposals)
            or any(line.curate for line in self.lines)
        )


def read(
    filters: monitoring.Filters, today: datetime.date, *, as_of: str = "", curate: Curator | None = None
) -> Tab:
    """The tab for the page's filter: its master rows (those active in the year, :func:`masters.appears`, that
    the status, search and CP output keep; with a partner filter those the partner takes part in), grouped by
    section and CP output; the proposals; the PD indicators in no master; and the counts of the tiles.
    ``curate``: :func:`curator` of the person reading (default: nobody curates)."""
    curate = curate or (lambda section: False)
    base = rows_filter(filters)
    year = base.year
    chosen = _masters(filters.sections)
    rows, by_key, running = _read(chosen, base, today, as_of)
    every = list(by_key.values())
    active = [m for m in chosen if m.status == MASTER.ACTIVE]
    shown = [m for m in active if masters.appears(m, year, running)]
    whole = _figures(shown, every, running, base, today)
    partners = {int(p) for p in filters.partners if p.isdigit()}
    tab = Tab(year=year, report_type=base.report_type, partner=bool(partners))
    if partners:
        mine = [row for row in every if row.pd.partner_id in partners]
        theirs = _figures(shown, mine, {row.pd.id for row in mine}, base, today)
        pairs = [(f, w) for f, w in zip(theirs, whole, strict=True) if f.subs]
        rows = [row for row in rows if row.pd.partner_id in partners]
    else:
        pairs = [(w, w) for w in whole]
    lines = [_line(f, w, base.report_type, curate) for f, w in pairs]
    tab.lines = [line for line in lines if _keeps(line, filters)]
    tab.groups = _grouped(tab.lines)
    tab.proposals = [
        Proposed(
            master=m,
            links=[x for x in m.links.all() if x.status == Status.SUGGESTED],
            curate=curate(m.section),
        )
        for m in chosen
        if m.status == MASTER.PROPOSED
    ]
    tab.attach = sorted(
        (m for m in active if curate(m.section)), key=lambda m: (norm(m.section), norm(m.name))
    )
    _unattached(tab, rows, by_key, chosen)
    return tab


def _unattached(
    tab: Tab, rows: list[Row], by_key: dict[tuple[int, str], Row], chosen: Sequence[EtoolsMaster]
) -> None:
    """The PD indicators of the year the filter reads with no confirmed link to an active master, each with
    NeuroDB's word on it; nothing is silently dropped."""
    pd_ids = sorted({row.pd.id for row in rows})
    homes = set(
        EtoolsMasterLink.objects.filter(
            status=Status.CONFIRMED, master__status=MASTER.ACTIVE, pd_id__in=pd_ids
        ).values_list("pd_id", "ident")
    )
    suggested: dict[tuple[int, str], list[str]] = defaultdict(list)
    found = (
        EtoolsMasterLink.objects.filter(
            status=Status.SUGGESTED, master__status__in=(MASTER.ACTIVE, MASTER.PROPOSED), pd_id__in=pd_ids
        )
        .order_by("-score", "master__name")
        .values_list("pd_id", "ident", "master__name", "master__status", "score")
    )
    for pd_id, ident, name, status, score in found:
        if status == MASTER.PROPOSED:
            suggested[(pd_id, ident)].append(f"In the proposed master indicator {name}")
        else:
            suggested[(pd_id, ident)].append(
                f"Suggested for {name}" + (f" ({score:.2f})" if score is not None else "")
            )
    free = sorted(
        (row for row in rows if key_of(row) not in homes),
        key=lambda row: (norm(row.section), (row.pd.number or "").lower(), norm(row.title)),
    )
    tab.indicators, tab.in_master = len(rows), len(rows) - len(free)
    tab.unattached_count = len(free)
    shown = free[:UNATTACHED_LIMIT]
    active = [m for m in chosen if m.status == MASTER.ACTIVE]
    quiet = [key_of(row) for row in shown if key_of(row) not in suggested]
    links = [link for m in active for link in m.links.all()]
    unclear = master_links.ambiguous(by_key, active, links, quiet) if quiet and active else {}
    for row in shown:
        key = key_of(row)
        unit = masters.unit_class(row)
        if not row.section.strip() or row.section.strip() == monitoring.NO_SECTION:
            note = "It names no section in eTools: it cannot join a master indicator."
        elif key in suggested:
            note = "; ".join(suggested[key])
        elif key in unclear:
            note = master_links.ambiguous_note(unclear[key])
        elif unit == masters.OTHER:
            note = "Not a number, a percentage or a ratio: it can only be shown in a master indicator."
        else:
            note = "No suggestion yet"
        partner, partner_id = _partner(row.pd)
        tab.unattached.append(
            Unattached(row=row, unit=unit, partner=partner, partner_id=partner_id, note=note)
        )


def detail(pk: int, filters: monitoring.Filters, today: datetime.date) -> Line | None:
    """One master's line for its detail (the modal): the PD indicators of its own section in the year, read
    as the tab reads them (the report type and the locations of the tab; never a partner's share). None for a
    master that is neither active nor proposed."""
    found = _masters(pk=pk)
    if not found:
        return None
    master = found[0]
    base = rows_filter(filters, sections=[master.section])
    _rows, by_key, running = _read(found, base, today, "")
    (figure,) = _figures(found, list(by_key.values()), running, base, today)
    return _line(figure, figure, base.report_type, lambda section: False)


# --------------------------------------------------------------------------------------------------- export
EXPORT_COLUMNS = (
    "level",
    "section",
    "year",
    "code",
    "master",
    "method",
    "unit",
    "target",
    "value",
    "at_least",
    "to_q1",
    "to_q2",
    "to_q3",
    "to_q4",
    "achieved_percent",
    "status",
    "status_as_of",
    "coverage",
    "counts_as",
    "counted",
    "partner",
    "programme_document",
    "indicator",
    "pd_target",
    "reached_in_year",
    "numerator",
    "denominator",
    "report_type",
    "sub_method",
    "pd_period_status",
    "flags",
)
SHARE_COLUMN = "partner_share_percent"  # with a partner filter, after the value


def export_columns(tab: Tab) -> tuple[str, ...]:
    if not tab.partner:
        return EXPORT_COLUMNS
    at = EXPORT_COLUMNS.index("at_least") + 1
    return (*EXPORT_COLUMNS[:at], SHARE_COLUMN, *EXPORT_COLUMNS[at:])


def _rounded(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def export_rows(tab: Tab) -> Iterable[dict[str, Any]]:
    """The tab as rows for the CSV and Excel exports: one per master (its figure, with a partner filter the
    partner's contribution and the master's status) and one per sub (its own figures and whether it counts),
    with a ``level`` column. Never who updated or confirmed anything, nor an e-mail address."""
    for line in tab.lines:
        figure, master = line.figure, line.master
        common = {
            "section": master.section,
            "year": tab.year,
            "code": line.whole.code,
            "master": master.name,
            "method": figure.method_label,
            "unit": master.unit,
        }
        yield {
            "level": "master",
            **common,
            "target": line.whole.target,
            "value": _rounded(figure.value, 4),
            "at_least": figure.at_least,
            SHARE_COLUMN: _rounded(line.share),
            **{f"to_q{q}": _rounded(v, 4) for q, v in zip(masters.QUARTERS, line.quarters, strict=True)},
            "achieved_percent": _rounded(line.whole.achieved),
            "status": line.whole.status_label,
            "status_as_of": line.whole.as_of,
            "coverage": figure.coverage,
        }
        for s in line.subs:
            sub = s.sub
            yield {
                "level": "sub",
                **common,
                **{f"to_q{q}": _rounded(v, 4) for q, v in zip(masters.QUARTERS, s.quarters, strict=True)},
                "counts_as": sub.counts_as,
                "counted": "Yes" if sub.counted else sub.reason,
                "partner": s.partner,
                "programme_document": s.pd_number,
                "indicator": s.title,
                "pd_target": s.target,
                "reached_in_year": _rounded(sub.value, 4),
                "numerator": sub.numerator if s.parts else None,
                "denominator": sub.denominator if s.parts else None,
                "report_type": monitoring.REPORT_TYPES.get(s.report_type, s.report_type),
                "sub_method": s.method_label,
                "pd_period_status": s.tracking_label,
                "flags": "; ".join(s.flags),
            }
