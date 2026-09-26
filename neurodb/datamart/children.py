"""Which indicators count children, for the overview's "children reached".

The rule reads the indicator's title (the age-group tag of :mod:`neurodb.datamart.tags`): an
indicator whose title names children, under 5s, under 18s or adolescents counts, provided it is a
plain number (a percentage or a ratio never adds up to people). Sections correct the rule once per
indicator with an :class:`~neurodb.datamart.models.IndicatorFlag`, which wins over the title.
"""

from __future__ import annotations

from neurodb.datamart.models import IndicatorFlag
from neurodb.datamart.tags import tags_of

CHILD_AGE_GROUPS = frozenset({"Under 5", "Under 18", "Adolescents", "Children"})
NUMBER_TYPES = frozenset({"", "number"})

Overrides = dict[tuple[str, str], bool]


def overrides() -> Overrides:
    """Every flag, as ``{(source, key): counts_children}``."""
    return {
        (source, key): counts
        for source, key, counts in IndicatorFlag.objects.values_list("source", "key", "counts_children")
    }


def counts_children(
    source: str,
    key: str,
    title: str | None,
    age_group: str | None,
    flags: Overrides,
    *,
    display_type: str = "",
    unit: str = "",
) -> bool:
    """Whether the indicator ``key`` of ``source`` counts children (a flag first, then the rule)."""
    flag = flags.get((source, str(key)))
    if flag is not None:
        return flag
    if (display_type or "").lower() not in NUMBER_TYPES or (unit or "").lower() not in NUMBER_TYPES:
        return False
    group = age_group if age_group is not None else tags_of(title).age_group
    return group in CHILD_AGE_GROUPS
