"""Suggesting the partners' PD indicators for the eTools master indicators, and proposing new masters, every
night (the ``etools-masters`` schedule, or *Run a job*), as the youth links are suggested
(:mod:`neurodb.youth.linking`). No AI is used: structure keys and word scores only. Nothing counts until a
person confirms it (:func:`accept`, :func:`confirm`); the job only writes suggestions and proposals.

**Candidates** (:func:`pool`): the PD indicators of every programme document running this calendar year or
the last, each once (:func:`neurodb.datamart.monitoring.collect`: no Reached is computed; draft and
cancelled PDs are never read), except those that name no section and those already counted in a confirmed
total. A master that holds the indicator already, confirmed or rejected, is never suggested again. Reached is
read only to check a confirmed link whose type looks changed (:func:`_changed`).

**Rules**, in order: the first that finds a master wins. Each needs the master's section, an active master
and a type its method takes as the indicator's role.

a. *Replacement* (1.0): a confirmed link of the same programme document is no longer in eTools and its title
   reads the same (:func:`neurodb.datamart.masters.base_title`). A suggestion only: the link is never moved.
b. *Same structure* (0.95): the same section, CP output, type and base title
   (:func:`neurodb.datamart.masters.structure_key`) as one of the master's confirmed totals.
c. *Similar title* (0.6 to 0.89): the share of the master's words (its name, its keywords or a confirmed
   total's title) found in the indicator's title and PD output (:func:`neurodb.youth.linking.score`), 0.1 more
   for the same CP output; never when the indicator's age group is none of the confirmed totals'. A similar
   title never scores as a strong match (0.9 or more), which only a structure gives.

When two masters score within 0.05 of each other the indicator gets no suggestion: it is "ambiguous".

**Proposals**: the indicators left with no suggestion and no confirmed link that share a structure key across
two or more programme documents, under a key no master holds (a dismissed one included), become one proposed
master: a sum of numbers, a percentage or a ratio, named after its shortest title without gender, nationality
or disability.

**Default roles**, when several indicators of one programme document go to one master: an indicator without
gender, nationality or disability next to ones with them is in the total and they are shown (its breakdown);
girls and boys (two genders, two nationalities) are all in the total; otherwise the largest target is in the
total and the others are shown. A total already confirmed there keeps its role.

**Writing** (:func:`write`), in one transaction: every suggested link is replaced and every proposed master
NeuroDB made is rebuilt. Confirmed and rejected links and dismissed masters are never touched. A proposal made
again keeps its master (its id, a name someone gave it, a link someone rejected there).
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import DatabaseError, connection, transaction
from django.db.models import Exists, OuterRef

from neurodb.cpd.services import _norm as output_norm
from neurodb.youth import linking

from . import masters, monitoring
from .masters import Row, key_of
from .models import EtoolsMaster, EtoolsMasterLink
from .reached import norm

if TYPE_CHECKING:
    from neurodb.core.models import SyncRun

logger = logging.getLogger(__name__)

Method, Role, Status = EtoolsMaster.Method, EtoolsMasterLink.Role, EtoolsMasterLink.Status
MASTER = EtoolsMaster.Status

LOCK_ID = 7140440  # one run at a time; neurodb.integrations.background.ETOOLS_MASTERS_LOCK_ID
REPLACEMENT_SCORE = 1.0  # rule a
STRUCTURE_SCORE = 0.95  # rule b, and the links of a proposal
TITLE_SCORE = linking.MIN_SCORE  # rule c: at least 0.6...
TITLE_CAP = 0.89  # ...and below the strong matches the admin confirms in one click
STRONG = 0.9  # a strong match: "Confirm suggestions scoring 0.9 or more"
CP_OUTPUT_BONUS = 0.1
AMBIGUITY = 0.05  # two masters this close: no suggestion

# what a proposal combines, by its indicators' type
METHOD_OF = {masters.NUMBER: Method.SUM, masters.PERCENTAGE: Method.PERCENT, masters.RATIO: Method.RATIO}
SUBSETS = ("gender", "nationality", "disability")  # the title tags of a breakdown (an age group is a family)

# why NeuroDB suggested a link (EtoolsMasterLink.reason)
REPLACES = "Replaces an indicator no longer in eTools"
SAME_STRUCTURE = "Same CP output, type and title as {n} confirmed indicator{s}"
SIMILAR_TITLE = "Similar title"
PROPOSED_FROM = "Same CP output, type and title in {n} programme documents"
AMBIGUOUS = "Ambiguous: {names}"
BUSY = "The eTools master indicator suggestions are already being worked out."
NOT_PROPOSED = "Not a proposed master indicator"

# the counts a run stores for each section (its SyncRun details: counts only, never a title or a name)
COUNTS = (
    "proposed",  # proposed masters
    "in_proposals",  # PD indicators in them
    "suggested",  # PD indicators suggested for an active master (the replacements among them)
    "replacements",
    "ambiguous",  # left without a suggestion: two masters fit as well
    "no_longer_in_etools",  # confirmed links of running PDs whose indicator is gone
    "type_changed",  # confirmed links whose indicator is of another type now
    "unattached",  # PD indicators in no master, suggested for none
)


# ---------------------------------------------------------------------------------------------- the plan
@dataclass
class Suggestion:
    """One PD indicator suggested for a master (``master``), or for a proposal (``master`` None)."""

    row: Row
    score: float
    reason: str
    rule: str  # replacement | structure | title | proposal
    role: str = Role.TOTAL
    master: EtoolsMaster | None = None


@dataclass
class Proposal:
    """A master NeuroDB proposes from one structure key, and its PD indicators."""

    key: str
    section: str
    name: str
    method: str
    cp_output: str
    links: list[Suggestion] = field(default_factory=list)
    master: EtoolsMaster | None = None  # the proposed master made for this key before: kept and rebuilt

    @property
    def pds(self) -> int:
        return len({s.row.pd.id for s in self.links})


@dataclass
class Plan:
    """What a run writes and finds (:func:`plan`)."""

    year: int
    candidates: list[Row] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    # PD indicator (PD id, ident) -> the masters that fit it as well as each other
    ambiguous: dict[tuple[int, str], list[EtoolsMaster]] = field(default_factory=dict)
    # confirmed links of a running programme document whose indicator is no longer in eTools
    gone: list[EtoolsMasterLink] = field(default_factory=list)
    type_changed: list[EtoolsMasterLink] = field(default_factory=list)
    unattached: list[Row] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)  # a section's key (norm) -> its name

    @property
    def written(self) -> int:
        """The links the run writes."""
        return len(self.suggestions) + sum(len(p.links) for p in self.proposals)

    def counts(self) -> dict[str, dict[str, int]]:
        """The run's :data:`COUNTS` for each section, by its name."""
        out: dict[str, Counter] = defaultdict(Counter)

        def add(section: str, what: str, n: int = 1) -> None:
            out[self.names.get(norm(section), section)][what] += n

        for p in self.proposals:
            add(p.section, "proposed")
            add(p.section, "in_proposals", len(p.links))
        for s in self.suggestions:
            add(s.master.section, "suggested")
            if s.rule == "replacement":
                add(s.master.section, "replacements")
        sections = {key_of(row): row.section for row in self.candidates}
        for key in self.ambiguous:
            add(sections.get(key, ""), "ambiguous")
        for link in self.gone:
            add(link.master.section, "no_longer_in_etools")
        for link in self.type_changed:
            add(link.master.section, "type_changed")
        for row in self.unattached:
            add(row.section, "unattached")
        return {section: {what: found[what] for what in COUNTS} for section, found in sorted(out.items())}

    def totals(self) -> dict[str, int]:
        found = self.counts().values()
        return {what: sum(counts[what] for counts in found) for what in COUNTS}


def ambiguous_note(choices: Sequence[EtoolsMaster]) -> str:
    """The note of an indicator two or more masters fit as well as each other: "Ambiguous: A or B"."""
    names = [m.name for m in choices]
    joined = " or ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + f" or {names[-1]}"
    return AMBIGUOUS.format(names=joined)


@dataclass
class _Target:
    """An active master as the rules read it."""

    master: EtoolsMaster
    keys: Counter = field(default_factory=Counter)  # its confirmed totals' structure keys -> how many
    titles: set[str] = field(default_factory=set)  # their titles (as linked and as now)
    outputs: set[str] = field(default_factory=set)  # its CP output and theirs, normalised
    ages: set[str] = field(default_factory=set)  # their age groups ("" for none)
    classes: set[str] = field(default_factory=set)  # their types (an average, a maximum or a minimum has one)
    totals: dict[int, list[Row]] = field(default_factory=lambda: defaultdict(list))  # theirs, by PD
    vanished: dict[int, list[EtoolsMasterLink]] = field(default_factory=lambda: defaultdict(list))


@dataclass
class _Match:
    target: _Target
    score: float
    reason: str
    rule: str
    role: str = Role.TOTAL


def pool(year: int, of_year: dict[tuple[int, str], Row] | None = None) -> dict[tuple[int, str], Row]:
    """The PD indicators of the programme documents running in ``year`` or the year before, each once, by
    ``(PD id, ident)`` (:func:`neurodb.datamart.masters.key_of`), with no Reached computed. ``of_year``, when
    given, is filled with those of the programme documents running in ``year`` itself (reporting check 12)."""
    found: dict[tuple[int, str], Row] = {}
    for each in (year, year - 1):
        for row in monitoring.collect(monitoring.Filters(year=each, scope="year")).values():
            found.setdefault(key_of(row), row)
            if each == year and of_year is not None:
                of_year[key_of(row)] = row
    return found


def plan(rows: dict[tuple[int, str], Row], year: int) -> Plan:
    """What a run would write from the PD indicators ``rows`` (:func:`pool`), against the masters and the
    confirmed and rejected links as they are now. Reads the database, writes nothing."""
    every = list(EtoolsMaster.objects.order_by("section", "sequence", "name", "id"))
    by_id = {m.pk: m for m in every}
    links = list(EtoolsMasterLink.objects.exclude(status=Status.SUGGESTED).order_by("sequence", "id"))
    for link in links:
        link.master = by_id[link.master_id]  # one instance per master
    out = Plan(year=year)
    for m in every:
        out.names.setdefault(norm(m.section), m.section)
    for row in rows.values():
        out.names.setdefault(norm(row.section), row.section)
    running = {pd_id for pd_id, _ident in rows}
    structure = {key_of(row): key for key, found in masters.clusters(rows.values()).items() for row in found}

    homes = {(x.pd_id, x.ident) for x in links if x.status == Status.CONFIRMED and x.role == Role.TOTAL}
    attached = {(x.pd_id, x.ident) for x in links if x.status == Status.CONFIRMED}
    blocked: dict[tuple[int, str], set[int]] = defaultdict(set)  # a master holds it already
    for link in links:
        blocked[(link.pd_id, link.ident)].add(link.master_id)

    targets = _targets([m for m in every if m.status == MASTER.ACTIVE], links, rows, structure, running, out)
    out.type_changed = _changed(out.type_changed)
    by_section: dict[str, list[_Target]] = defaultdict(list)
    for target in targets:
        by_section[norm(target.master.section)].append(target)

    out.candidates = sorted(
        (
            row
            for key, row in rows.items()
            if row.section.strip() and row.section.strip() != monitoring.NO_SECTION and key not in homes
        ),
        key=lambda row: (norm(row.section), row.pd.number or "", row.title, row.ident[1]),
    )
    taken: set[tuple[int, str]] = set()  # suggested or ambiguous
    for row in out.candidates:
        key = key_of(row)
        mine = [t for t in by_section.get(norm(row.section), []) if t.master.pk not in blocked.get(key, ())]
        found = _matches(row, mine, structure.get(key, ""))
        if not found:
            continue
        taken.add(key)
        best, close = _best(found)
        if close:
            out.ambiguous[key] = close
            continue
        out.suggestions.append(
            Suggestion(
                row=row,
                score=best.score,
                reason=best.reason,
                rule=best.rule,
                role=best.role,
                master=best.target.master,
            )
        )
    _default_roles(out.suggestions, {t.master.pk: t for t in targets})

    free = [row for row in out.candidates if key_of(row) not in taken and key_of(row) not in attached]
    out.proposals = _proposals(free, every, structure, blocked)
    proposed = {key_of(s.row) for p in out.proposals for s in p.links}
    out.unattached = [row for row in free if key_of(row) not in proposed]
    return out


def _targets(
    active: list[EtoolsMaster],
    links: list[EtoolsMasterLink],
    rows: dict[tuple[int, str], Row],
    structure: dict[tuple[int, str], str],
    running: set[int],
    out: Plan,
) -> list[_Target]:
    """The active masters with what the rules read of their confirmed links; notes the links no longer in
    eTools and those whose type reads as changed (``out``, checked by :func:`_changed`)."""
    by_master: dict[int, list[EtoolsMasterLink]] = defaultdict(list)
    for link in links:
        if link.status == Status.CONFIRMED:
            by_master[link.master_id].append(link)
    targets = []
    for master in active:
        target = _Target(master=master)
        if output_norm(master.cp_output):
            target.outputs.add(output_norm(master.cp_output))
        for link in by_master.get(master.pk, []):
            if link.role == Role.TOTAL and link.unit_class:
                target.classes.add(link.unit_class)
            row = rows.get((link.pd_id, link.ident))
            if row is None:
                if link.pd_id in running:  # its PD runs, its indicator is gone
                    target.vanished[link.pd_id].append(link)
                    out.gone.append(link)
                continue
            if link.unit_class and masters.unit_class(row) != link.unit_class:
                out.type_changed.append(link)
            if link.role != Role.TOTAL:
                continue
            target.totals[link.pd_id].append(row)
            if structure.get((link.pd_id, link.ident)):
                target.keys[structure[(link.pd_id, link.ident)]] += 1
            target.titles.update({link.title, row.title} - {""})
            output = masters.cp_output_key(row)
            if output != masters.NO_CP_OUTPUT:
                target.outputs.add(output)
            target.ages.add(row.tags.get("age_group") or "")
        targets.append(target)
    return targets


def _changed(suspects: list[EtoolsMasterLink]) -> list[EtoolsMasterLink]:
    """Of the confirmed links whose type reads as changed without Reached, those of another type as the pages
    read it: an indicator that is a ratio by its PRP methods alone reads as a number without its Reached, so
    their programme documents, and only theirs, are read again with it."""
    if not suspects:
        return []
    rows = _now_rows(suspects)
    return [
        link
        for link in suspects
        if (row := rows.get((link.pd_id, link.ident))) is not None
        and masters.unit_class(row) != link.unit_class
    ]


def _fits(target: _Target, role: str, unit: str) -> bool:
    """The master's method takes an indicator of type ``unit`` as ``role`` (an average, a maximum or a
    minimum only of its totals' one type)."""
    if masters.misfit(target.master.method, role, unit):
        return False
    one = target.master.method in masters.ONE_CLASS
    return not (role == Role.TOTAL and one and target.classes and unit not in target.classes)


def _matches(row: Row, targets: list[_Target], structure: str) -> list[_Match]:
    """The masters of the indicator's section it may go to, by the first rule that finds any."""
    unit = masters.unit_class(row)
    base = masters.base_title(row.title)
    found = []
    for target in targets:  # a: it replaces a confirmed link of its PD no longer in eTools
        for link in target.vanished.get(row.pd.id, ()):
            if base and masters.base_title(link.title) == base and _fits(target, link.role, unit):
                found.append(_Match(target, REPLACEMENT_SCORE, REPLACES, "replacement", link.role))
                break
    if found:
        return found
    for target in targets:  # b: the structure of a confirmed total
        n = target.keys.get(structure, 0) if structure else 0
        if n and _fits(target, Role.TOTAL, unit):
            reason = SAME_STRUCTURE.format(n=n, s="" if n == 1 else "s")
            found.append(_Match(target, STRUCTURE_SCORE, reason, "structure"))
    if found:
        return found
    age = row.tags.get("age_group") or ""
    for target in targets:  # c: a similar title
        if not _fits(target, Role.TOTAL, unit) or (target.ages and age not in target.ages):
            continue
        value = _title_score(target, row)
        if value >= TITLE_SCORE:
            found.append(_Match(target, value, SIMILAR_TITLE, "title"))
    return found


def _best(found: list[_Match]) -> tuple[_Match, list[EtoolsMaster]]:
    """The best of an indicator's matches, and the masters that fit it as well as each other (within
    :data:`AMBIGUITY` of the best, the best among them) when there are two or more: the indicator is then
    "ambiguous" and gets no suggestion."""
    found.sort(key=lambda m: (-m.score, m.target.master.sequence, m.target.master.name, m.target.master.pk))
    best = found[0]
    if len(found) > 1 and round(best.score - found[1].score, 3) <= AMBIGUITY:
        return best, [m.target.master for m in found if round(best.score - m.score, 3) <= AMBIGUITY]
    return best, []


def ambiguous(
    rows: dict[tuple[int, str], Row],
    active: Sequence[EtoolsMaster],
    links: Sequence[EtoolsMasterLink],
    keys: Iterable[tuple[int, str]],
) -> dict[tuple[int, str], list[EtoolsMaster]]:
    """For the Master indicators tab: the indicators ``keys`` of ``rows`` (the tab's PD indicators of the
    year) that two or more ``active`` masters fit as well as each other by the rules of :func:`plan`, with
    those masters. The job leaves them without a suggestion; the tab says "Ambiguous: A or B"
    (:func:`ambiguous_note`) rather than nothing. ``links``: the masters' links (a master that holds the
    indicator, confirmed or rejected, is never one of them). Reads nothing from the database, writes
    nothing."""
    decided = [link for link in links if link.status != Status.SUGGESTED]
    running = {pd_id for pd_id, _ident in rows}
    structure = {key_of(row): key for key, found in masters.clusters(rows.values()).items() for row in found}
    targets = _targets(list(active), decided, rows, structure, running, Plan(year=0))
    by_section: dict[str, list[_Target]] = defaultdict(list)
    for target in targets:
        by_section[norm(target.master.section)].append(target)
    blocked: dict[tuple[int, str], set[int]] = defaultdict(set)
    for link in decided:
        blocked[(link.pd_id, link.ident)].add(link.master_id)
    out = {}
    for key in keys:
        row = rows.get(key)
        if row is None:
            continue
        mine = [t for t in by_section.get(norm(row.section), []) if t.master.pk not in blocked.get(key, ())]
        found = _matches(row, mine, structure.get(key, ""))
        if len(found) > 1:
            _best_match, close = _best(found)
            if close:
                out[key] = close
    return out


def _title_score(target: _Target, row: Row) -> float:
    """Rule c: the best share of a master's words (its name, its keywords, a confirmed total's title) found in
    the indicator's title and PD output, 0.1 more for the same CP output, at most :data:`TITLE_CAP`."""
    texts = [target.master.name, target.master.keywords, *sorted(target.titles)]
    value = max((linking.score(text, None, row) for text in texts if (text or "").strip()), default=0.0)
    if value and masters.cp_output_key(row) in target.outputs:
        value += CP_OUTPUT_BONUS
    return round(min(value, TITLE_CAP), 3)


# ---------------------------------------------------------------------------------------- default roles
def _untagged(row: Row) -> bool:
    """Not a breakdown: its title names no gender, nationality or disability."""
    return not any(row.tags.get(tag) for tag in SUBSETS)


def _largest(rows: Sequence[Row]) -> Row:
    return min(rows, key=lambda row: (-(row.target or 0.0), row.title, key_of(row)))


def roles(new: Sequence[Row], kept: Sequence[Row] = ()) -> dict[tuple[int, str], str]:
    """What each of one programme document's indicators ``new`` counts as in one master, next to its totals
    confirmed there (``kept``, whose roles stay): an indicator without gender, nationality or disability next
    to ones with them is in the total and they are shown; indicators that cannot count the same people
    (:func:`neurodb.datamart.masters.disjoint`: girls and boys, two nationalities) are all in the total;
    otherwise the largest target is in the total and the others are shown. When the rule would leave out a
    confirmed total, every new one is shown."""
    members = [*kept, *new]
    untagged = [row for row in members if _untagged(row)]
    if len(members) == 1:
        totals = members
    elif len(untagged) == 1:  # a total and its breakdowns
        totals = untagged
    elif not untagged and masters._apart(members):
        totals = members
    else:
        totals = [_largest(members)]
    chosen = {key_of(row) for row in totals}
    if any(key_of(row) not in chosen for row in kept):
        chosen = set()
    return {key_of(row): Role.TOTAL if key_of(row) in chosen else Role.SHOWN for row in new}


def _default_roles(suggestions: list[Suggestion], targets: dict[int, _Target]) -> None:
    groups: dict[tuple[int, int], list[Suggestion]] = defaultdict(list)
    for s in suggestions:
        if s.role == Role.TOTAL:
            groups[(s.master.pk, s.row.pd.id)].append(s)
    for (master_id, pd_id), found in groups.items():
        chosen = roles([s.row for s in found], targets[master_id].totals.get(pd_id, []))
        for s in found:
            s.role = chosen[key_of(s.row)]


# ------------------------------------------------------------------------------------------------ proposals
def _cp_output_text(row: Row) -> str:
    """The CP output a proposal is listed under: the indicator's own, else its PD's only one."""
    found = [str(x).strip() for x in (getattr(row.pd, "cp_outputs", None) or []) if str(x or "").strip()]
    own = (row.cp_output or "").strip()
    if own and own != ", ".join(dict.fromkeys(found)):
        return own
    return found[0] if len(set(found)) == 1 else ""


def _most_common(values: Iterable[str]) -> str:
    found = Counter(v for v in values if v)
    return min(found, key=lambda v: (-found[v], v)) if found else ""


def _proposals(
    free: list[Row],
    every: list[EtoolsMaster],
    structure: dict[tuple[int, str], str],
    blocked: dict[tuple[int, str], set[int]],
) -> list[Proposal]:
    """The structure keys of the free indicators that span two or more programme documents, under a key no
    master holds but a proposal NeuroDB made (which is rebuilt, keeping its master)."""
    held = {m.proposal_key for m in every if m.proposal_key and m.status != MASTER.PROPOSED}
    before = {
        m.proposal_key: m
        for m in every
        if m.status == MASTER.PROPOSED and m.origin == EtoolsMaster.Origin.PROPOSED and m.proposal_key
    }
    taken = {(norm(m.section), norm(m.name)) for m in every if m.status in (MASTER.ACTIVE, MASTER.ARCHIVED)}
    taken |= {(norm(m.section), norm(m.name)) for m in before.values()}
    groups: dict[str, list[Row]] = defaultdict(list)
    for row in free:
        if structure.get(key_of(row)):
            groups[structure[key_of(row)]].append(row)
    out = []
    for key, rows in sorted(groups.items(), key=lambda item: (norm(item[1][0].section), item[0])):
        if key in held:
            continue
        master = before.get(key)
        if master is not None:  # never again where someone rejected it
            rows = [row for row in rows if master.pk not in blocked.get(key_of(row), ())]
        if len({row.pd.id for row in rows}) < 2:
            continue
        proposal = _proposal(key, rows, master, taken)
        taken.add((norm(proposal.section), norm(proposal.name)))
        out.append(proposal)
    return out


def _proposal(key: str, rows: list[Row], master: EtoolsMaster | None, taken: set) -> Proposal:
    rows = sorted(rows, key=lambda row: (row.pd.number or "", row.title, row.ident[1]))
    unit = masters.unit_class(rows[0])
    if master is not None:  # made before: it keeps what someone may have changed
        section, name, method, cp_output = master.section, master.name, master.method, master.cp_output
    else:
        section = _most_common(row.section for row in rows)
        names = sorted({row.title.strip() for row in ([r for r in rows if _untagged(r)] or rows)})
        names.sort(key=len)
        free = [n for n in names if (norm(section), norm(n[:500])) not in taken]
        name = (free or names)[0][:500]
        method = METHOD_OF[unit]
        cp_output = _most_common(_cp_output_text(row) for row in rows)[:500]
    proposal = Proposal(
        key=key, section=section, name=name, method=method, cp_output=cp_output, master=master
    )
    reason = PROPOSED_FROM.format(n=len({row.pd.id for row in rows}))
    by_pd: dict[int, list[Row]] = defaultdict(list)
    for row in rows:
        by_pd[row.pd.id].append(row)
    for found in by_pd.values():
        chosen = roles(found)
        for row in found:
            role = chosen[key_of(row)]
            if masters.misfit(method, role, unit):  # a proposal someone gave another method: shown there
                role = Role.SHOWN
            proposal.links.append(
                Suggestion(row=row, score=STRUCTURE_SCORE, reason=reason, rule="proposal", role=role)
            )
    return proposal


# ------------------------------------------------------------------------------------------------ writing
def _link(master: EtoolsMaster, s: Suggestion) -> EtoolsMasterLink:
    pd_id, ident = key_of(s.row)
    return EtoolsMasterLink(
        master=master,
        pd_id=pd_id,
        ident=ident,
        title=(s.row.title or "")[:1024],
        pd_number=(s.row.pd.number or "")[:64],
        unit_class="",  # taken when it is confirmed (with Reached, as the pages read the type)
        role=s.role,
        status=Status.SUGGESTED,
        score=s.score,
        reason=s.reason[:200],
    )


def write(found: Plan) -> None:
    """Replace every suggested link and rebuild the proposals NeuroDB made, in one transaction. Confirmed and
    rejected links, dismissed masters and a proposal someone confirmed or rejected a link of are kept."""
    with transaction.atomic():
        EtoolsMasterLink.objects.filter(status=Status.SUGGESTED).delete()
        kept = [p.master.pk for p in found.proposals if p.master is not None]
        decided = EtoolsMasterLink.objects.filter(master=OuterRef("pk")).exclude(status=Status.SUGGESTED)
        EtoolsMaster.objects.filter(status=MASTER.PROPOSED, origin=EtoolsMaster.Origin.PROPOSED).exclude(
            pk__in=kept
        ).exclude(Exists(decided)).delete()
        new = [_link(s.master, s) for s in found.suggestions]
        for proposal in found.proposals:
            master = proposal.master or EtoolsMaster.objects.create(
                section=proposal.section,
                name=proposal.name,
                method=proposal.method,
                cp_output=proposal.cp_output,
                status=MASTER.PROPOSED,
                origin=EtoolsMaster.Origin.PROPOSED,
                proposal_key=proposal.key,
            )
            proposal.master = master
            new.extend(_link(master, s) for s in proposal.links)
        # a person may confirm a link while the run works: theirs stays, a clashing suggestion is not written
        EtoolsMasterLink.objects.bulk_create(new, ignore_conflicts=True)


# -------------------------------------------------------------------------------------------------- the job
def _lock() -> bool:
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        return bool(cursor.fetchone()[0])


def _unlock() -> None:
    if connection.vendor != "postgresql":
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
    except DatabaseError:  # the session is gone, and its lock with it
        logger.warning("eTools master indicators: the lock could not be released", exc_info=True)


def run(year: int | None = None, triggered_by: str = "command") -> SyncRun | str:
    """The nightly job: suggest PD indicators for the masters and propose new ones for ``year`` (default this
    year; last year's programme documents are read too) and record the run with its counts for each section
    (:data:`COUNTS`). Returns the run, or :data:`BUSY` when another run holds the lock (then nothing is
    recorded)."""
    from neurodb.core import years
    from neurodb.core.models import SyncRun
    from neurodb.integrations.runs import fail, new_run

    year = year or years.this_year()
    if not _lock():
        return BUSY
    try:
        sync = new_run(SyncRun.Job.ETOOLS_MASTERS, "", triggered_by)
        try:
            found = plan(pool(year), year)
            write(found)
        except Exception as exc:  # recorded as a failed run; nothing was written
            return fail(sync, exc, year=year)
        sync.rows_in = len(found.candidates)
        sync.rows_written = found.written
        sync.finish(SyncRun.Status.SUCCEEDED, year=year, sections=found.counts(), totals=found.totals())
        return sync
    finally:
        _unlock()


# -------------------------------------------------------------------------------------------- the decisions
def _now_rows(links: Sequence[EtoolsMasterLink]) -> dict[tuple[int, str], Row]:
    """The links' PD indicators as the pages read them (with Reached, so their type is the pages'), by key."""
    pd_ids = sorted({link.pd_id for link in links if link.pd_id})
    if not pd_ids:
        return {}
    found = monitoring.indicators(monitoring.Filters(pd_ids=pd_ids, scope="all"))
    return {key_of(row): row for row in found}


@dataclass
class Confirmed:
    """What :func:`confirm` did: how many links it confirmed, and those it left as they were, with why."""

    confirmed: int = 0
    left: list[tuple[EtoolsMasterLink, str]] = field(default_factory=list)


def _why_not(link: EtoolsMasterLink, row: Row | None) -> str:
    if row is None:
        return masters.GONE
    unit = masters.unit_class(row)
    problem = masters.misfit(link.master.method, link.role, unit)
    if not problem and link.role == Role.TOTAL:
        problem = masters._one_class(link.master, unit, exclude=link.pk)
        home = (
            EtoolsMasterLink.objects.filter(
                pd_id=link.pd_id, ident=link.ident, role=Role.TOTAL, status=Status.CONFIRMED
            )
            .exclude(pk=link.pk)
            .select_related("master")
            .first()
        )
        if not problem and home is not None:
            problem = f"It already counts in the total of {home.master.name}."
    return problem


def confirm(links: Iterable[EtoolsMasterLink], by: str) -> Confirmed:
    """Confirm links (a suggestion, a rejected link, or a confirmed one again after its type changed in
    eTools): each PD indicator is read again as the pages read it, and the title, PD number and type it is
    confirmed with are saved. One that is no longer in eTools, of a type its role or its master's method does
    not take, or already in another master's confirmed total, is left as it was and said why. Another
    master's suggestion to count the same indicator in its total gives way."""
    links = list(links)
    rows = _now_rows(links)
    out = Confirmed()
    with transaction.atomic():
        for link in links:
            row = rows.get((link.pd_id, link.ident))
            why = _why_not(link, row)
            if why:
                out.left.append((link, why))
                continue
            if link.role == Role.TOTAL:
                EtoolsMasterLink.objects.filter(
                    pd_id=link.pd_id, ident=link.ident, role=Role.TOTAL, status=Status.SUGGESTED
                ).exclude(pk=link.pk).delete()
            link.status, link.confirmed_by = Status.CONFIRMED, (by or "")[:150]
            link.title, link.pd_number = row.title[:1024], (row.pd.number or "")[:64]
            link.unit_class = masters.unit_class(row)
            link.save(
                update_fields=["status", "confirmed_by", "title", "pd_number", "unit_class", "updated_at"]
            )
            out.confirmed += 1
    return out


def reject(links: Iterable[EtoolsMasterLink], by: str) -> int:
    """Reject links: never counted, and the PD indicator is never suggested for that master again."""
    done = 0
    for link in links:
        link.status, link.confirmed_by = Status.REJECTED, (by or "")[:150]
        link.save(update_fields=["status", "confirmed_by", "updated_at"])
        done += 1
    return done


@dataclass
class Accepted:
    """What :func:`accept` did: the masters made active, those refused (with why) and their links."""

    accepted: list[EtoolsMaster] = field(default_factory=list)
    refused: list[tuple[EtoolsMaster, str]] = field(default_factory=list)
    links: Confirmed = field(default_factory=Confirmed)


def accept(proposals: Iterable[EtoolsMaster], by: str) -> Accepted:
    """Accept proposed masters, in one click each: the master becomes active and its suggested links are
    confirmed (:func:`confirm`). One whose name an active or archived master of its section has already is
    refused: rename it first, or merge it into that one."""
    out = Accepted()
    for master in proposals:
        if master.status != MASTER.PROPOSED:
            out.refused.append((master, NOT_PROPOSED))
            continue
        same = EtoolsMaster.objects.filter(
            section=master.section, name=master.name, status__in=[MASTER.ACTIVE, MASTER.ARCHIVED]
        ).exclude(pk=master.pk)
        if same.exists():
            out.refused.append(
                (
                    master,
                    f"{master.section} already has a master indicator named {master.name}: rename the "
                    "proposal first, or merge it into that one.",
                )
            )
            continue
        with transaction.atomic():
            master.status, master.updated_by = MASTER.ACTIVE, (by or "")[:150]
            master.save(update_fields=["status", "updated_by", "updated_at"])
            links = list(master.links.filter(status=Status.SUGGESTED).order_by("sequence", "id"))
            for link in links:
                link.master = master
            done = confirm(links, by)
        out.accepted.append(master)
        out.links.confirmed += done.confirmed
        out.links.left.extend(done.left)
    return out


def dismiss(proposals: Iterable[EtoolsMaster], by: str) -> int:
    """Dismiss proposed masters: each is kept as dismissed, so NeuroDB never proposes its structure key
    again, and its suggestions go. Returns how many were dismissed."""
    done = 0
    with transaction.atomic():
        for master in proposals:
            if master.status != MASTER.PROPOSED:
                continue
            master.status, master.updated_by = MASTER.DISMISSED, (by or "")[:150]
            master.save(update_fields=["status", "updated_by", "updated_at"])
            master.links.filter(status=Status.SUGGESTED).delete()
            done += 1
    return done
