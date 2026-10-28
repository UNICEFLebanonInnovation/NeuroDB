"""Curating the eTools master indicators by hand, in the administration and on the Master indicators tab of
Partner monitoring: adding PD indicators to a master (*Add PD indicators*, *Attach to…*), changing what a link
counts as, taking one out, renaming a master and setting its year's code and target, merging masters, moving
masters to a renamed eTools section, and making masters from the ActivityInfo archive's master indicators.
Accepting or dismissing a proposal and confirming or rejecting a link are
:mod:`neurodb.datamart.master_links`'s.

Each of these is a person's decision: a PD indicator added here is confirmed at once (it counts), saved as
eTools has it now (its title, PD number and type, read with its Reached as the pages read it). Nothing is
moved automatically, no figure is stored and no AI is used. The ActivityInfo archive is only read: no
``pivoting_*`` row is ever changed.
"""

from __future__ import annotations

import itertools
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Max, Q

from . import master_links, masters, monitoring
from .masters import Row, key_of
from .models import EtoolsMaster, EtoolsMasterLink, EtoolsMasterYear, PDIndicator
from .reached import norm

Method, Role, Status = EtoolsMaster.Method, EtoolsMasterLink.Role, EtoolsMasterLink.Status
MASTER = EtoolsMaster.Status
HOLDS_NAME = (MASTER.ACTIVE, MASTER.ARCHIVED)  # the statuses of one name per section

# the ActivityInfo methods as an eTools master combines its programme documents (COUNT counted ActivityInfo
# records: it has no eTools meaning)
FROM_ACTIVITYINFO = {
    "SUM": Method.SUM,
    "SUM_OVER_SUM": Method.PERCENT,
    "AVERAGE": Method.AVERAGE,
    "MAXIMUM": Method.MAXIMUM,
    "MINIMUM": Method.MINIMUM,
}
# neurodb.facts.queries: an ActivityInfo average is over the master's monthly totals
AVERAGED = (
    "Made from an ActivityInfo master indicator, which averaged its monthly totals: this one averages the "
    "programme documents' figures."
)
ALREADY_HERE = "Already in this master indicator"
REJECTED_BEFORE = "Rejected for this master before"
IDENT = re.compile(r"s\d{1,18}|t[0-9a-f]{40}")  # a link's ident code (masters.ident_code)


# ------------------------------------------------------------------------------------- Add PD indicators
@dataclass
class Offer:
    """A PD indicator *Add PD indicators* offers, and what to know before ticking it."""

    row: Row
    key: tuple[int, str]  # (PD id, ident code)
    unit: str  # its type (read without Reached)
    note: str = ""  # "Suggested for this master", or the total it counts in elsewhere
    parts_only: bool = False  # counted in another master's total: a numerator or a denominator only

    @property
    def value(self) -> str:
        return f"{self.key[0]}|{self.key[1]}"

    @property
    def partner(self) -> str:
        pd = self.row.pd
        return pd.partner_name or (pd.partner.name if getattr(pd, "partner", None) else "") or ""


def parse_key(value: str) -> tuple[int, str] | None:
    """An :attr:`Offer.value` (or the tab's ``key``) back into its ``(PD id, ident code)``; None when it is
    not one. The ident is the code a link stores already (``s<eTools id>``, or ``t`` and the 40 hex digits of
    a title's sha1, :func:`neurodb.datamart.masters.key_of`), taken as it is: coding it again would hash the
    hash, and the indicator would never be found."""
    pd_id, _sep, ident = str(value or "").partition("|")
    if not pd_id.isdecimal() or len(pd_id) > 18 or not IDENT.fullmatch(ident):  # "²" is a digit, no number
        return None
    return int(pd_id), ident


def _rows_of(section: str, year: int) -> list[Row]:
    """The PD indicators of ``section`` (as eTools spells it, whatever the case) in the programme documents
    running in ``year`` (:func:`neurodb.datamart.monitoring.collect`: no Reached is computed)."""
    section = norm(section)
    if not section:
        return []
    return [
        row
        for row in monitoring.collect(monitoring.Filters(year=year, scope="year")).values()
        if norm(row.section) == section
    ]


def section_named(section: str, year: int) -> bool:
    """Whether a PD indicator of a programme document running in ``year`` names ``section``: a master whose
    section none names (mistyped by hand, or kept after eTools renamed it) is offered nothing."""
    return bool(_rows_of(section, year))


def offered(master: EtoolsMaster, year: int) -> list[Offer]:
    """The PD indicators *Add PD indicators* offers for ``master``: those of its section in the programme
    documents running in ``year`` (:func:`neurodb.datamart.monitoring.collect`: no Reached is computed), of a
    type it takes (in its total, or as a numerator or a denominator of a percentage or a ratio), that it does
    not hold yet (its own suggestions aside, and those rejected for it before, which a person may change their
    mind on) and that no master counts in a confirmed total. A percentage or a ratio master also offers the
    numbers another master counts in its total, as a numerator or a denominator only. Sorted by PD number,
    then title."""
    rows = _rows_of(master.section, year)
    if not rows:
        return []
    held = {(link.pd_id, link.ident): link.status for link in master.links.all()}
    homes = {
        (pd_id, ident): name
        for pd_id, ident, name in EtoolsMasterLink.objects.filter(
            role=Role.TOTAL, status=Status.CONFIRMED, pd_id__in={row.pd.id for row in rows}
        )
        .exclude(master=master)
        .values_list("pd_id", "ident", "master__name")
    }
    classes = {
        unit
        for role, status, unit in master.links.values_list("role", "status", "unit_class")
        if role == Role.TOTAL and status != Status.REJECTED and unit
    }
    out = []
    for row in rows:
        key = key_of(row)
        if held.get(key) == Status.CONFIRMED:
            continue
        unit = masters.unit_class(row)
        as_total = not masters.misfit(master.method, Role.TOTAL, unit) and not (
            master.method in masters.ONE_CLASS and classes and unit not in classes
        )
        as_part = master.method in masters.WITH_PARTS and not masters.misfit(
            master.method, Role.NUMERATOR, unit
        )
        home = homes.get(key)
        if home is not None:
            if not as_part:
                continue
            out.append(
                Offer(row, key, unit, f"In the total of {home}: a numerator or a denominator only", True)
            )
        elif as_total or as_part:
            note = {Status.SUGGESTED: "Suggested for this master", Status.REJECTED: REJECTED_BEFORE}
            out.append(Offer(row, key, unit, note.get(held.get(key), "")))
    out.sort(key=lambda offer: (offer.row.pd.number or "", offer.row.title, offer.key))
    return out


@dataclass
class Added:
    """What :func:`add` did: how many PD indicators it added, and those it left out, with why."""

    added: int = 0
    left: list[tuple[str, str]] = field(default_factory=list)  # (PD number and title, why)


def _now_rows(pd_ids: Iterable[int]) -> dict[tuple[int, str], Row]:
    """The PD indicators of these programme documents as the pages read them (with Reached), by key."""
    pd_ids = sorted(set(pd_ids))
    if not pd_ids:
        return {}
    found = monitoring.indicators(monitoring.Filters(pd_ids=pd_ids, scope="all"))
    return {key_of(row): row for row in found}


def _why_left(master: EtoolsMaster, row: Row | None, key: tuple[int, str], role: str) -> str:
    if row is None:  # gone from eTools, or left out of the pages
        return masters.LEFT_OUT if key in masters.left_out([key[0]]) else masters.GONE
    if norm(row.section) != norm(master.section):
        return f"It is in section {row.section or 'none'}, not in {master.section}."
    if master.links.filter(pd_id=key[0], ident=key[1], status=Status.CONFIRMED).exists():
        return ALREADY_HERE
    unit = masters.unit_class(row)
    problem = masters.misfit(master.method, role, unit)
    if not problem and role == Role.TOTAL:
        problem = masters._one_class(master, unit)
        home = (
            EtoolsMasterLink.objects.filter(
                pd_id=key[0], ident=key[1], role=Role.TOTAL, status=Status.CONFIRMED
            )
            .exclude(master=master)
            .select_related("master")
            .first()
        )
        if not problem and home is not None:
            problem = f"It already counts in the total of {home.master.name}."
    return problem


def add(master: EtoolsMaster, keys: Iterable[tuple[int, str]], role: str, by: str) -> Added:
    """Add PD indicators (``(PD id, ident code)``) to ``master``, all with one role, confirmed at once: a
    person chose them. Each is read again as the pages read it and saved with its title, PD number and type.
    One that is no longer in eTools, of another section, already confirmed in the master, of a type the role
    or the method does not take, or (in the total) counted in another master's confirmed total is left out
    and said why; nothing is added to a master that is not active
    (:func:`neurodb.datamart.masters.not_active`). The master's own suggestion of it, and another master's
    suggestion to count it in its total, give way; its own link rejected before is confirmed with the new
    role (a person changed their mind)."""
    keys = list(dict.fromkeys(keys))
    rows = _now_rows(pd_id for pd_id, _ident in keys)
    out = Added()
    refused = masters.not_active(master)
    with transaction.atomic():
        sequence = master.links.aggregate(last=Max("sequence"))["last"] or 0
        for key in keys:
            row = rows.get(key)
            why = refused or _why_left(master, row, key, role)
            if why:
                label = f"{row.pd.number or 'PD'}: {row.title}" if row is not None else key[1]
                out.left.append((label, why))
                continue
            master.links.filter(pd_id=key[0], ident=key[1], status=Status.SUGGESTED).delete()
            if role == Role.TOTAL:
                EtoolsMasterLink.objects.filter(
                    pd_id=key[0], ident=key[1], role=Role.TOTAL, status=Status.SUGGESTED
                ).delete()
            rejected = master.links.filter(pd_id=key[0], ident=key[1], status=Status.REJECTED).first()
            if rejected is not None:
                rejected.title, rejected.pd_number = row.title[:1024], (row.pd.number or "")[:64]
                rejected.unit_class, rejected.role = masters.unit_class(row), role
                rejected.status, rejected.confirmed_by = Status.CONFIRMED, (by or "")[:150]
                rejected.save(
                    update_fields=[
                        "title",
                        "pd_number",
                        "unit_class",
                        "role",
                        "status",
                        "confirmed_by",
                        "updated_at",
                    ]
                )
                out.added += 1
                continue
            sequence += 1
            EtoolsMasterLink.objects.create(
                master=master,
                pd_id=key[0],
                ident=key[1],
                title=row.title[:1024],
                pd_number=(row.pd.number or "")[:64],
                unit_class=masters.unit_class(row),
                role=role,
                status=Status.CONFIRMED,
                confirmed_by=(by or "")[:150],
                sequence=min(sequence, 32767),
            )
            out.added += 1
    return out


# ------------------------------------------------------------------------------------------------- one link
def set_role(link: EtoolsMasterLink, role: str, by: str) -> str:
    """Change what a link counts as; "" when done, else why not. The role must fit the master's method and the
    indicator's type (as it was confirmed); in the total, the indicator may count in no other master's
    confirmed total (another master's suggestion to count it gives way), and an average, a maximum or a
    minimum keeps one type. A confirmed link stays confirmed, a suggestion a suggestion (it counts only once
    confirmed); either keeps who set it (``confirmed_by``), so the nightly job keeps the role a person gave a
    suggestion rather than its own default (:func:`neurodb.datamart.master_links.write`)."""
    if role not in Role.values:
        return "Choose what it counts as."
    if role == link.role:
        return ""
    master, unit = link.master, link.unit_class
    problem = masters.misfit(master.method, role, unit) if unit else ""
    if not problem and role == Role.TOTAL and unit:
        problem = masters._one_class(master, unit, exclude=link.pk)
    if not problem and role == Role.TOTAL:
        home = (
            EtoolsMasterLink.objects.filter(
                pd_id=link.pd_id, ident=link.ident, role=Role.TOTAL, status=Status.CONFIRMED
            )
            .exclude(pk=link.pk)
            .select_related("master")
            .first()
        )
        if home is not None:
            problem = f"It already counts in the total of {home.master.name}."
    if problem:
        return problem
    with transaction.atomic():
        if role == Role.TOTAL:
            EtoolsMasterLink.objects.filter(
                pd_id=link.pd_id, ident=link.ident, role=Role.TOTAL, status=Status.SUGGESTED
            ).exclude(pk=link.pk).delete()
        link.role = role
        fields = ["role", "updated_at"]
        if link.status in (Status.CONFIRMED, Status.SUGGESTED):
            link.confirmed_by = (by or "")[:150]
            fields.append("confirmed_by")
        link.save(update_fields=fields)
    return ""


def remove(link: EtoolsMasterLink) -> None:
    """Take a PD indicator out of its master: the link is deleted. NeuroDB may suggest it again (it never
    counts until confirmed); rejecting a suggestion is what stops that."""
    link.delete()


def save_settings(
    master: EtoolsMaster, year: int, *, name: str, code: str, target: str, by: str
) -> dict[str, str]:
    """Rename a master and set its settings for ``year``: the AWP/RWP code and the target its status is
    measured against (blank: no target, and the status reads "No target"). Returns the problems by field
    ({} when saved); nothing is saved while there is one."""
    errors: dict[str, str] = {}
    name = " ".join(str(name or "").split())
    if not name:
        errors["name"] = "Give the master indicator a name."
    elif len(name) > 500:
        errors["name"] = "The name is at most 500 characters."
    elif (
        name != master.name
        and EtoolsMaster.objects.filter(section=master.section, name=name, status__in=HOLDS_NAME)
        .exclude(pk=master.pk)
        .exists()
    ):
        errors["name"] = f"{master.section} already has a master indicator named {name}."
    code = str(code or "").strip()
    if len(code) > 32:
        errors["code"] = "The code is at most 32 characters."
    value = None
    text = str(target or "").strip().replace(",", "")
    if text:
        try:
            value = float(text)
        except ValueError:
            value = None
        if value is None or not math.isfinite(value) or value <= 0:
            errors["target"] = "The target is a number above 0 (leave it blank for no target)."
    if errors:
        return errors
    by = (by or "")[:150]
    with transaction.atomic():
        if name != master.name:
            master.name, master.updated_by = name, by
            master.save(update_fields=["name", "updated_by", "updated_at"])
        settings, created = EtoolsMasterYear.objects.get_or_create(
            master=master, year=year, defaults={"code": code, "target": value, "updated_by": by}
        )
        if not created and (settings.code, settings.target) != (code, value):
            settings.code, settings.target, settings.updated_by = code, value, by
            settings.save(update_fields=["code", "target", "updated_by", "updated_at"])
    return {}


# ------------------------------------------------------------------------------------------------- merging
@dataclass
class Merged:
    """What :func:`merge` did."""

    kept: EtoolsMaster
    merged: list[EtoolsMaster] = field(default_factory=list)
    moved: int = 0  # links moved to the master kept
    dropped: int = 0  # links it held already (the same PD indicator)
    years: int = 0  # years' settings it did not have
    shown: int = 0  # of them, years a merged master was not shown in (shown this year unticked): shown now
    confirmed: int = 0  # a proposal's PD indicators confirmed in it (merging a proposal accepts it there)
    left: list[tuple[EtoolsMasterLink, str]] = field(default_factory=list)  # those left as suggestions, why


def merge_problem(chosen: Sequence[EtoolsMaster]) -> str:
    """Why these masters cannot be merged ("" when they can): two or more, of one section and one method, none
    dismissed, at least one active (the one kept), and for an average, a maximum or a minimum totals of one
    type."""
    if len(chosen) < 2:
        return "Choose two master indicators or more to merge."
    sections = sorted({m.section for m in chosen})
    if len(sections) > 1:
        return f"Only master indicators of one section can be merged: these are in {' and '.join(sections)}."
    methods = sorted({m.get_method_display() for m in chosen})
    if len(methods) > 1:
        return (
            "Only master indicators combined the same way can be merged: these are "
            f"{' and '.join(methods)}. Change the method of one first."
        )
    if any(m.status == MASTER.DISMISSED for m in chosen):
        return "A dismissed proposal cannot be merged."
    if not any(m.status == MASTER.ACTIVE for m in chosen):
        if any(m.status == MASTER.ARCHIVED for m in chosen):
            return (
                "None of these is active. To keep the archived one, make it active again first (its Status "
                "on its page); to keep a proposal, accept it first. Then merge the others into it."
            )
        return "None of these is active: accept the proposal to keep first, then merge the others into it."
    if chosen[0].method in masters.ONE_CLASS:
        found = set(
            EtoolsMasterLink.objects.filter(master__in=chosen, role=Role.TOTAL)
            .exclude(status=Status.REJECTED)
            .exclude(unit_class="")
            .values_list("unit_class", flat=True)
        )
        if len(found) > 1:
            return masters._one_type(chosen[0], found)
    return ""


def _wins(incoming: EtoolsMasterLink, mine: EtoolsMasterLink) -> bool:
    """Whether a merged master's link replaces the kept master's link of the same PD indicator: a person's
    decision beats a suggestion, and a confirmed link (it counts) beats a rejected one."""
    if mine.status == Status.SUGGESTED:
        return incoming.status != Status.SUGGESTED
    return mine.status == Status.REJECTED and incoming.status == Status.CONFIRMED


def merge(keep: EtoolsMaster, others: Sequence[EtoolsMaster], by: str) -> Merged | str:
    """Merge ``others`` into ``keep``, or say why not (:func:`merge_problem`). Their links move to it; a PD
    indicator it holds already is dropped, unless it holds only a suggestion and a person decided the other
    (confirmed or rejected), or it rejected the indicator and the other counts it (confirmed): the other then
    wins, so nothing counted before the merge stops counting. The years' settings it lacks move to it; a
    year a merged master was not shown in (*shown this year* unticked: it did not go on) is shown, since
    ``keep`` goes on (else it would leave ``keep`` out of that year, its own PD indicators counting nowhere).
    The others are archived (a proposal is dismissed, so it is never proposed again), keeping their other
    years' settings. Merging a proposal accepts it into ``keep``: its suggestions are confirmed there, each
    read again as Accept does (:func:`neurodb.datamart.master_links.confirm`), and one that cannot count
    there stays a suggestion, said why. Left as suggestions, the nightly job would take them all back (it
    makes none of them for ``keep``), and the dismissed proposal would keep its family from being proposed
    again."""
    others = [m for m in others if m.pk != keep.pk]
    problem = merge_problem([keep, *others])
    if not problem and keep.status != MASTER.ACTIVE:
        problem = "The master indicator kept must be active."
    if problem:
        return problem
    out = Merged(kept=keep)
    by = (by or "")[:150]
    with transaction.atomic():
        held = {(link.pd_id, link.ident): link for link in keep.links.all()}
        sequence = keep.links.aggregate(last=Max("sequence"))["last"] or 0
        have = set(keep.years.values_list("year", flat=True))
        accepted: dict[tuple[int, str], EtoolsMasterLink] = {}  # a proposal's suggestions, now in ``keep``
        for other in others:
            proposal = other.status == MASTER.PROPOSED
            for link in other.links.order_by("sequence", "id"):
                mine = held.get((link.pd_id, link.ident))
                if mine is not None and not _wins(link, mine):
                    link.delete()
                    out.dropped += 1
                    if proposal and mine.status == Status.SUGGESTED:
                        accepted[(mine.pd_id, mine.ident)] = mine
                    continue
                if mine is not None:
                    mine.delete()
                    accepted.pop((mine.pd_id, mine.ident), None)
                    out.dropped += 1
                sequence += 1
                link.master, link.sequence = keep, min(sequence, 32767)
                link.save(update_fields=["master", "sequence", "updated_at"])
                held[(link.pd_id, link.ident)] = link
                out.moved += 1
                if proposal and link.status == Status.SUGGESTED:
                    accepted[(link.pd_id, link.ident)] = link
            for year_row in other.years.order_by("year"):
                if year_row.year in have:
                    continue
                if not year_row.is_active:
                    year_row.is_active = True
                    out.shown += 1
                year_row.master, year_row.updated_by = keep, by
                year_row.save(update_fields=["master", "is_active", "updated_by", "updated_at"])
                have.add(year_row.year)
                out.years += 1
            other.status = MASTER.DISMISSED if other.status == MASTER.PROPOSED else MASTER.ARCHIVED
            other.updated_by = by
            other.save(update_fields=["status", "updated_by", "updated_at"])
            out.merged.append(other)
        keep.updated_by = by
        keep.save(update_fields=["updated_by", "updated_at"])
        for link in accepted.values():
            link.master = keep
        done = master_links.confirm(list(accepted.values()), by)
        out.confirmed, out.left = done.confirmed, done.left
    return out


# ---------------------------------------------------------------------------------------- a section renamed
def sections() -> list[str]:
    """The eTools section names on the PD indicators, each once (in its most common spelling), sorted."""
    found: dict[str, Counter] = {}
    rows = PDIndicator.objects.exclude(section_name="").values_list("section_name", flat=True)
    for name in rows.iterator():
        name = (name or "").strip()
        if name and name != monitoring.NO_SECTION:
            found.setdefault(norm(name), Counter())[name] += 1
    return sorted((min(names, key=lambda n: (-names[n], n)) for names in found.values()), key=norm)


def move(chosen: Iterable[EtoolsMaster], section: str, by: str) -> tuple[int, list[tuple[EtoolsMaster, str]]]:
    """Move masters to ``section`` (after eTools renamed theirs): their links and years go with them. One
    whose name the section has already (an active or archived master) stays where it is, and is said why.
    Returns how many moved and those left."""
    section = (section or "").strip()
    moved, left = 0, []
    with transaction.atomic():
        for master in chosen:
            if master.section == section:
                left.append((master, f"It is in {section} already."))
                continue
            same = EtoolsMaster.objects.filter(section=section, name=master.name, status__in=HOLDS_NAME)
            if master.status in HOLDS_NAME and same.exclude(pk=master.pk).exists():
                left.append((master, f"{section} already has a master indicator named {master.name}."))
                continue
            master.section, master.updated_by = section, (by or "")[:150]
            master.save(update_fields=["section", "updated_by", "updated_at"])
            moved += 1
    return moved, left


# ------------------------------------------------------------------------------------ from ActivityInfo
@dataclass
class Seeded:
    """What :func:`from_activityinfo` did: the masters made, and the ActivityInfo masters left out, by why."""

    created: list[EtoolsMaster] = field(default_factory=list)
    counts: list[str] = field(default_factory=list)  # COUNT masters (no eTools meaning)
    existing: list[str] = field(default_factory=list)  # the section has a master of the name
    no_section: list[str] = field(default_factory=list)  # no confirmed eTools section name


def etools_section(section_id: int | None) -> str:
    """The eTools section name a NeuroDB section's ActivityInfo masters go to: its confirmed eTools section
    name (:class:`neurodb.watch.models.SectionMatch`), the one with the most PD indicators when there are
    several, spelled as on the PD indicators; "" when it has none."""
    from neurodb.watch.models import SectionMatch

    if section_id is None:
        return ""
    names = sorted(
        {
            name.strip()
            for name in SectionMatch.objects.filter(confirmed=True, section_id=section_id).values_list(
                "etools_name", flat=True
            )
            if (name or "").strip()
        }
    )
    if not names:
        return ""
    wanted = {norm(name): name for name in names}
    found: dict[str, set] = {key: set() for key in wanted}
    spelled: dict[str, Counter] = {key: Counter() for key in wanted}
    query = Q()
    for name in names:
        query |= Q(section_name__iexact=name)
    rows = PDIndicator.objects.filter(query).values_list(
        "section_name", "intervention_id", "source_id", "title"
    )
    for name, pd_id, source_id, title in rows.iterator():
        key = norm(name)
        if key in found:
            found[key].add(monitoring.ident_of(pd_id, source_id, title))
            spelled[key][(name or "").strip()] += 1
    best = min(wanted, key=lambda key: (-len(found[key]), key))
    common = spelled[best]
    return min(common, key=lambda n: (-common[n], n)) if common else wanted[best]


def from_activityinfo(chosen: Iterable, year: int, by: str) -> Seeded:
    """Make eTools masters from ActivityInfo master indicators (``pivoting.MasterIndicator``), reading them
    only: the name and unit are copied, the method mapped (:data:`FROM_ACTIVITYINFO`; a COUNT is left out),
    the AWP code goes to ``year``'s settings with no target (staff type it), and the master remembers the
    ActivityInfo master it came from. Its section is :func:`etools_section` of its database's NeuroDB section:
    without one the master is left out, and so is one whose section has a master of its name already."""
    out = Seeded()
    by = (by or "")[:150]
    found_sections: dict[int | None, str] = {}
    for source in chosen:
        name = " ".join(str(source.name or "").split())[:500]
        method = FROM_ACTIVITYINFO.get(str(source.aggregation_method or "SUM").upper())
        if method is None:
            out.counts.append(name)
            continue
        section_id = source.database.section_id if source.database_id else None
        if section_id not in found_sections:
            found_sections[section_id] = etools_section(section_id)
        section = found_sections[section_id]
        if not section:
            out.no_section.append(name)
            continue
        same = EtoolsMaster.objects.filter(section=section, name__iexact=name).exclude(
            status=MASTER.DISMISSED
        )
        if not name or same.exists():
            out.existing.append(name)
            continue
        with transaction.atomic():
            master = EtoolsMaster.objects.create(
                section=section,
                name=name,
                unit=str(source.unit or "").strip()[:64],
                method=method,
                status=MASTER.ACTIVE,
                origin=EtoolsMaster.Origin.ACTIVITYINFO,
                activityinfo_master_id=source.pk,
                note=AVERAGED if method == Method.AVERAGE else "",
                updated_by=by,
            )
            EtoolsMasterYear.objects.create(
                master=master,
                year=year,
                code=str(source.awp_code or "").strip()[:32],
                target=None,
                updated_by=by,
            )
        out.created.append(master)
    return out


def listed(names: Sequence[str], shown: int = 3) -> str:
    """Names for a message: the first ``shown``, then "and N more"."""
    names = [n for n in names if n]
    text = ", ".join(itertools.islice(names, shown))
    return f"{text} and {len(names) - shown} more" if len(names) > shown else text
