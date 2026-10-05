"""Which NeuroDB section each eTools section name means, for NeuroDB Watch.

eTools spells sections its own way ("WASH / Water, Sanitation and Hygiene") on PDs, action points,
progress reports, daily review findings and section plans; staff belong to a NeuroDB section
(``users.Section``). Nothing stored linked the two, and the pages guessed: a name contained in the
other, or every section when nothing matched. The watch tells people things, so it keeps a stored
map instead (:class:`~neurodb.watch.models.SectionMatch`, one row per eTools name):

1. the **same name** or the **same code** as one NeuroDB section is confirmed automatically;
2. a name **contained** in a section's name or code, or the other way round (3 characters or more),
   is proposed and waits for an administrator to confirm it;
3. otherwise the name has **no section** until an administrator chooses one.

A row an administrator set by hand is never changed. :func:`seed` adds the names not in the map yet
(the morning pass runs it first; ``manage map_watch_sections`` runs it by hand), :func:`resolve` gives
the confirmed sections of a name, and an item whose name has none goes to the administrators only,
never to every section.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import CharField, F, Func, Q, QuerySet
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from neurodb.accounts.models import Section
from neurodb.datamart import models as dm
from neurodb.datamart.monitoring import norm
from neurodb.partnerships.models import PCA
from neurodb.reports.models import SectionPlan
from neurodb.review.models import FindingAssignment, ReviewFinding

from .models import SectionMatch

MIN_CONTAINED = 3  # a shorter name or code ("ED", "CP") is inside too many others to mean anything
NAMES_SHOWN = 3  # names spelled out in a Needs attention line, then "and N more"
How = SectionMatch.How


@dataclass(frozen=True)
class Match:
    """The section an eTools name was matched to (``None``: no section), how, and whether that
    stands without an administrator."""

    section_id: int | None
    how: str
    confirmed: bool


# ---------------------------------------------------------------------------- the eTools names
def _pd_names() -> list[str]:
    return list(
        PCA.objects.exclude(section_names=None)
        .annotate(name=Func(F("section_names"), function="unnest", output_field=CharField()))
        .order_by()
        .values_list("name", flat=True)
        .distinct()
    )


def _distinct(model, field: str = "section") -> list[str]:
    return list(model.objects.exclude(**{field: ""}).order_by().values_list(field, flat=True).distinct())


def _fm_names() -> list[str]:
    """The section names written on field monitoring visits themselves (Monitoring insights), when that
    app is installed: the visits that take their sections from a programme document or an action point
    bring no new spelling."""
    from django.apps import apps

    if not apps.is_installed("neurodb.fmm"):
        return []
    visits = apps.get_model("fmm", "Visit").objects.filter(sections_from="activity")
    return list(
        visits.annotate(name=Func(F("section_names"), function="unnest", output_field=CharField()))
        .order_by()
        .values_list("name", flat=True)
        .distinct()
    )


# Where eTools section names appear: PDs, action points, progress reports, the daily review's
# findings and their assignments, the section plans of the brief and field monitoring visits.
SOURCES = (
    _pd_names,
    lambda: _distinct(dm.ActionPoint),
    lambda: _distinct(dm.ReportedIndicator),
    lambda: _distinct(ReviewFinding),
    lambda: _distinct(FindingAssignment),
    lambda: _distinct(SectionPlan),
    _fm_names,
)


def _spelling(name: str | None) -> str:
    """The name as stored: spaces trimmed and runs of spaces made one, the case kept."""
    return " ".join((name or "").split())


def etools_names() -> list[str]:
    """Every eTools section name in NeuroDB, once: two spellings differing only by case or spaces
    are the same name (the first in alphabetical order is kept)."""
    names: dict[str, str] = {}
    for source in SOURCES:
        for name in filter(None, map(_spelling, source())):
            kept = names.get(norm(name))
            names[norm(name)] = name if kept is None else min(kept, name)
    return sorted(names.values(), key=lambda n: (norm(n), n))


# ---------------------------------------------------------------------------- matching one name
class _Index:
    """The NeuroDB sections, read once per pass: names and codes carried by a single section (a
    name two sections share matches neither exactly), and every name and code for containment."""

    def __init__(self, sections: Iterable[Section]):
        names: dict[str, set[int]] = {}
        codes: dict[str, set[int]] = {}
        self.texts: list[tuple[str, int]] = []
        for section in sections:
            for text, into in ((norm(section.name), names), (norm(section.code), codes)):
                if text:
                    into.setdefault(text, set()).add(section.pk)
                    self.texts.append((text, section.pk))
        self.names = {text: next(iter(ids)) for text, ids in names.items() if len(ids) == 1}
        self.codes = {text: next(iter(ids)) for text, ids in codes.items() if len(ids) == 1}


def _index() -> _Index:
    return _Index(Section.objects.order_by("pk"))


def match(name: str, index: _Index | None = None) -> Match:
    """How one eTools section name matches the NeuroDB sections (see the module's rules).

    Among several contained matches the longest shared text wins ("Child Protection and Gender":
    Child Protection before Gender), then the closest length, then the oldest section; it stays
    unconfirmed either way.
    """
    index = index or _index()
    key = norm(name)
    if key in index.names:
        return Match(index.names[key], How.EXACT, True)
    if key in index.codes:
        return Match(index.codes[key], How.CODE, True)
    best: tuple[int, int, int] | None = None
    for text, section_id in index.texts:
        inner, outer = sorted((key, text), key=len)
        if len(inner) >= MIN_CONTAINED and inner in outer:
            rank = (-len(inner), len(outer) - len(inner), section_id)
            best = min(best, rank) if best else rank
    if best:
        return Match(best[2], How.CONTAINS, False)
    return Match(None, How.NONE, False)


# ---------------------------------------------------------------------------- keeping the map
def _waiting() -> Q:
    """Rows that send nothing to a section: not confirmed yet, or matched automatically to a section
    since deleted. A name an administrator confirmed without a section is settled, not waiting."""
    gone = Q(section__isnull=True) & Q(how__in=(How.EXACT, How.CODE, How.CONTAINS))
    return Q(confirmed=False) | gone


def _rematchable() -> Q:
    """Waiting rows that were matched automatically; a row set by hand is never matched again."""
    return ~Q(how=How.MANUAL) & _waiting()


def waiting() -> QuerySet[SectionMatch]:
    """The names waiting for an administrator (see ``_waiting``), with their proposed section."""
    return SectionMatch.objects.filter(_waiting()).select_related("section")


@transaction.atomic
def seed(rematch: bool = False) -> dict[str, int]:
    """Add every eTools section name not in the map yet, matched to a NeuroDB section.

    Existing rows are not touched, so an administrator's choice stays. With ``rematch``, rows matched
    automatically and still waiting are matched again too (after a section was added, renamed or
    deleted); a row set by hand, or confirmed by an administrator, never is. Returns the counts:
    ``names`` seen, ``added`` with how many ``confirmed``, ``to_confirm`` and ``unmatched``, and
    ``rematched`` rows that changed.
    """
    index = _index()
    known = {norm(name) for name in SectionMatch.objects.values_list("etools_name", flat=True)}
    names = etools_names()
    added = []
    for name in names:
        if norm(name) in known:
            continue
        found = match(name, index)
        added.append(
            SectionMatch(
                etools_name=name, section_id=found.section_id, how=found.how, confirmed=found.confirmed
            )
        )
    SectionMatch.objects.bulk_create(added, ignore_conflicts=True)  # a concurrent seed added it: fine
    rematched = 0
    if rematch:
        for row in SectionMatch.objects.filter(_rematchable()):
            found = match(row.etools_name, index)
            if (row.section_id, row.how, row.confirmed) != (found.section_id, found.how, found.confirmed):
                row.section_id, row.how, row.confirmed = found.section_id, found.how, found.confirmed
                row.save(update_fields=["section", "how", "confirmed", "updated_at"])
                rematched += 1
    return {
        "names": len(names),
        "added": len(added),
        "confirmed": sum(row.confirmed for row in added),
        "to_confirm": sum(row.how == How.CONTAINS for row in added),
        "unmatched": sum(row.how == How.NONE for row in added),
        "rematched": rematched,
    }


# ---------------------------------------------------------------------------- reading the map
def confirmed_map() -> dict[str, int]:
    """normalised eTools name → NeuroDB section id, for the confirmed rows that have a section.
    Read once per pass and passed to :func:`resolve` to save a query per item."""
    rows = SectionMatch.objects.filter(confirmed=True, section__isnull=False)
    return {norm(name): section_id for name, section_id in rows.values_list("etools_name", "section_id")}


def resolve(name: str | None, mapping: dict[str, int] | None = None) -> list[int]:
    """The confirmed NeuroDB section ids of one eTools section name, or ``[]`` when it has none
    (not in the map, not confirmed, or confirmed without a section). Callers send an item with
    ``[]`` to the administrators, never to every section."""
    mapping = confirmed_map() if mapping is None else mapping
    section_id = mapping.get(norm(name))
    return [section_id] if section_id is not None else []


def resolve_names(
    names: Iterable[str | None], mapping: dict[str, int] | None = None
) -> tuple[list[int], list[str]]:
    """The confirmed section ids of several eTools names (a PD's sections), sorted and once each,
    and the names that have none, once each in their first spelling. Blank names are skipped."""
    mapping = confirmed_map() if mapping is None else mapping
    ids: set[int] = set()
    unmapped: dict[str, str] = {}
    for name in filter(None, map(_spelling, names)):
        found = resolve(name, mapping)
        ids.update(found)
        if not found:
            unmapped.setdefault(norm(name), name)
    return sorted(ids), list(unmapped.values())


def unmatched_sections() -> list[Section]:
    """NeuroDB sections with staff (active, not a donor account) that no confirmed eTools name
    points to: their staff would get nothing from the watch. Sorted by name."""
    matched = SectionMatch.objects.filter(confirmed=True, section__isnull=False).values("section_id")
    staffed = (
        get_user_model()
        .objects.filter(is_active=True, donor_account__isnull=True, section__isnull=False)
        .values("section_id")
    )
    return list(Section.objects.filter(pk__in=staffed).exclude(pk__in=matched).order_by("name", "pk"))


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:NAMES_SHOWN])
    if len(names) > NAMES_SHOWN:
        shown += " " + _("and %(n)s more") % {"n": len(names) - NAMES_SHOWN}
    return shown


def needs_attention() -> list[dict[str, Any]]:
    """Lines for the admin home's "Needs attention": eTools names without a confirmed section, and
    sections with staff that no name points to (once the map exists). Nothing while the watch is
    switched off. Each line is ``{"key", "text", "url"}``; the key stays the same from day to day."""
    if not settings.WATCH_ENABLED:
        return []
    url = reverse("admin:watch_sectionmatch_changelist")
    lines = []
    names = list(waiting().values_list("etools_name", flat=True))
    if names:
        lines.append(
            {
                "key": "watch_sections_unconfirmed",
                "text": ngettext(
                    "%(n)s eTools section name has no confirmed NeuroDB section (%(names)s): "
                    "NeuroDB Watch tells what it finds under it to the administrators only.",
                    "%(n)s eTools section names have no confirmed NeuroDB section (%(names)s): "
                    "NeuroDB Watch tells what it finds under them to the administrators only.",
                    len(names),
                )
                % {"n": len(names), "names": _listed(names)},
                "url": url + "?confirmed__exact=0",
            }
        )
    if SectionMatch.objects.exists():
        unmatched = [str(section) for section in unmatched_sections()]
        if unmatched:
            lines.append(
                {
                    "key": "watch_sections_unmatched",
                    "text": ngettext(
                        "%(n)s NeuroDB section with staff has no confirmed eTools section name "
                        "(%(names)s): its staff get nothing from NeuroDB Watch.",
                        "%(n)s NeuroDB sections with staff have no confirmed eTools section name "
                        "(%(names)s): their staff get nothing from NeuroDB Watch.",
                        len(unmatched),
                    )
                    % {"n": len(unmatched), "names": _listed(unmatched)},
                    "url": url,
                }
            )
    return lines
