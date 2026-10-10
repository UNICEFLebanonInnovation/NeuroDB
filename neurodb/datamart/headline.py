"""The headline "children / beneficiaries reached": one figure per programme document, never the sum of
its indicators.

Different indicators of one programme document (PD) count overlapping children: the child enrolled in
non-formal education also receives the learning materials, so adding the two indicators counts that
child twice. Each PD therefore contributes the value of one indicator, its **main indicator**:

* the indicator a section flagged as the PD's **unique beneficiaries** indicator
  (``IndicatorFlag.unique_beneficiaries``, set on the PD page or in the admin), when it is a plain
  number;
* until one is flagged, its **largest children indicator** (the children rule of
  :mod:`neurodb.datamart.children`), labelled "largest single indicator": the largest by the figure
  the page shows (Reached in the year on the overview, Reached to date on the PD page).

A total over several PDs (a section, a partner, a donor, the country) is the sum of each PD's main
indicator, labelled :data:`TOTAL_RULE`: partners may reach the same children. Percentages and ratios
are never added. The values themselves come from :mod:`neurodb.datamart.reached`, through the partner
monitoring rows (:class:`neurodb.datamart.monitoring.Indicator`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from django.db import transaction

from .children import NUMBER_TYPES
from .models import IndicatorFlag

FLAGGED = "flagged"
LARGEST = "largest"
RULE_LABELS = {FLAGGED: "unique beneficiaries indicator", LARGEST: "largest single indicator"}
TOTAL_RULE = "sum of each PD's main indicator; partners may reach the same children"
TOTAL_RULE_LONG = (
    "Each programme document counts once: its unique beneficiaries indicator, else its largest children "
    "indicator (largest single indicator). The total is the sum over the programme documents; partners "
    "may reach the same children."
)

Row = Any  # neurodb.datamart.monitoring.Indicator (not imported: monitoring imports nothing from here)


@dataclass(frozen=True)
class Main:
    """A programme document's main indicator and why it is the one."""

    row: Row
    rule: str  # FLAGGED | LARGEST

    @property
    def flagged(self) -> bool:
        return self.rule == FLAGGED

    @property
    def label(self) -> str:
        return RULE_LABELS[self.rule]


def is_number(row: Row) -> bool:
    """A plain count (people, children, items): never a percentage or a ratio, which do not add up."""
    if row.result is not None and row.result.ratio:
        return False
    return (row.display_type or "").lower() in NUMBER_TYPES and (row.unit or "").lower() in NUMBER_TYPES


def children_rule(flags: dict[tuple[str, str], bool] | None = None) -> Callable[[Row], bool]:
    """The children rule of :mod:`neurodb.datamart.children` for partner monitoring rows (a section's flag
    first, else a plain number whose title names children), reading the flags once."""
    from .children import counts_children, overrides

    flags = overrides() if flags is None else flags

    def rule(row: Row) -> bool:
        return counts_children(
            "etools",
            row.key,
            row.title,
            row.tags.get("age_group"),
            flags,
            display_type=row.display_type,
            unit=row.unit,
        )

    return rule


def flagged_keys(pd_ids: Iterable[int] | None = None) -> dict[int, str]:
    """``{PD id: indicator key}`` of the PDs whose unique beneficiaries indicator is set."""
    flags = IndicatorFlag.objects.filter(
        source=IndicatorFlag.Source.ETOOLS, unique_beneficiaries=True
    ).exclude(intervention=None)
    if pd_ids is not None:
        flags = flags.filter(intervention_id__in=list(pd_ids))
    return dict(flags.values_list("intervention_id", "key"))


def choose(
    rows: Iterable[Row],
    *,
    is_children: Callable[[Row], bool],
    value: Callable[[Row], float | None],
    flags: dict[int, str] | None = None,
    exclude: Iterable[Row] = (),
) -> dict[int, Main]:
    """Each PD's main indicator among ``rows`` (every indicator of the PDs, whatever section or place
    the page shows, so a PD has the same main indicator on every page). ``value``: the figure "largest"
    compares (then Reached to date, then the target; ties keep the first row). ``exclude``: rows set
    aside (values too large to be true). A PD with neither a flagged indicator nor a children
    indicator has none."""
    rows = list(rows)
    flags = flagged_keys({r.pd.id for r in rows}) if flags is None else flags
    left_out = {id(r) for r in exclude}
    by_pd: dict[int, list[Row]] = defaultdict(list)
    for row in rows:
        if id(row) not in left_out and is_number(row):
            by_pd[row.pd.id].append(row)
    out: dict[int, Main] = {}
    for pd_id, candidates in by_pd.items():
        key = flags.get(pd_id)
        chosen = next((r for r in candidates if key and r.key == key), None)
        if chosen is not None:
            out[pd_id] = Main(chosen, FLAGGED)
            continue
        kids = [r for r in candidates if is_children(r)]
        if kids:
            best = max(kids, key=lambda r: (value(r) or 0.0, r.reached or 0.0, r.target or 0.0))
            out[pd_id] = Main(best, LARGEST)
    return out


def gap(row: Row) -> float | None:
    """Children still to reach on one indicator: its target minus its Reached (never below 0); None
    without a target."""
    if not row.target:
        return None
    return max(row.target - (row.reached or 0.0), 0.0)


def behind(
    rows: Iterable[Row], main: Main | None, is_children: Callable[[Row], bool]
) -> tuple[float, Row] | None:
    """Children behind target in some indicators of one PD, never added across indicators: the PD's main
    indicator's gap when it is among them, else the largest gap of a single children indicator. None
    when no children indicator has a target."""
    rows = list(rows)
    if main is not None and any(r is main.row for r in rows) and gap(main.row) is not None:
        return gap(main.row), main.row
    gaps = [(gap(r), r) for r in rows if is_number(r) and is_children(r) and gap(r) is not None]
    if not gaps:
        return None
    return max(gaps, key=lambda g: g[0])


# ------------------------------------------------------------------------------------- setting it
def can_set(user, pd) -> bool:
    """May choose the PD's unique beneficiaries indicator: an Administrator, or a Section editor of one
    of the PD's sections (its eTools section names, as NeuroDB matched them to its own sections)."""
    from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, role_of

    if user is None or not user.is_authenticated:
        return False
    role = role_of(user)
    if role == ADMIN:
        return True
    if role != SECTION_EDITOR or not getattr(user, "section_id", None):
        return False
    from neurodb.watch.sections import resolve_names

    names = list(pd.section_names or []) + list(
        pd.pd_indicators.order_by().values_list("section_name", flat=True).distinct()
    )
    ids, _unmapped = resolve_names(names)
    return user.section_id in ids


def set_main(pd, key: str, label: str, user) -> IndicatorFlag:
    """Make indicator ``key`` the PD's unique beneficiaries indicator (the one it had before stops
    being it). Its children flag, if any, is kept."""
    who = _who(user)
    with transaction.atomic():
        _unset(
            IndicatorFlag.objects.select_for_update()
            .filter(intervention=pd, unique_beneficiaries=True)
            .exclude(source=IndicatorFlag.Source.ETOOLS, key=key),
            who,
        )
        flag, _created = IndicatorFlag.objects.select_for_update().get_or_create(
            source=IndicatorFlag.Source.ETOOLS,
            key=key,
            defaults={"label": label[:1024], "counts_children": None, "intervention": pd},
        )
        flag.unique_beneficiaries = True
        flag.intervention = pd
        flag.label = flag.label or label[:1024]
        flag.updated_by = who
        flag.save()
    return flag


def clear_main(pd, user) -> int:
    """The PD has no unique beneficiaries indicator any more (its largest children indicator stands in
    again). Returns how many flags changed."""
    with transaction.atomic():
        return _unset(
            IndicatorFlag.objects.select_for_update().filter(intervention=pd, unique_beneficiaries=True),
            _who(user),
        )


def _unset(flags, who: str) -> int:
    """Take the unique beneficiaries role off ``flags``; a flag that held nothing else goes. Each one is
    saved (not updated in bulk), so its time stamp moves and the pages' caches see the change."""
    changed = 0
    for flag in flags:
        changed += 1
        if flag.counts_children is None and not flag.note:
            flag.delete()
            continue
        flag.unique_beneficiaries = False
        flag.updated_by = who
        flag.save()
    return changed


def _who(user) -> str:
    return (user.get_username() if user is not None and user.is_authenticated else "")[:150]
