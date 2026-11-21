"""The filters the owner removed from every page on 21 Nov 2026: office, emergency, CP output and partner
type.

No filter bar offers them and no page builds a link with them. The data code still reads them (Ask
NeuroDB's look-ups, Compare with Power BI, the figure trail, the exports), so an address that still carries
one (an old bookmark, a link of the figure trail or of the Power BI comparison sheet) is handled by each
page in one of two ways:

* :func:`without`: the page ignores them, reading its address without them (Programme documents, Donors,
  Funds, Partners, Action points), so its figures, links and downloads are those of the address without
  them;
* :func:`note`: the page applies them, as the figure trail and the comparison sheet built from the same
  address do, and names them on a line with a link that removes them (Progress reports, Partner
  monitoring; ``reports/partials/_old_filters.html``). The filter bar keeps them as hidden fields, so
  Apply never drops them without a word.
"""

from __future__ import annotations

from typing import Any

from django.http import QueryDict
from django.utils.translation import gettext as _

KEYS = ("office", "emergency", "cp_output", "partner_type")


def _labels() -> dict[str, str]:
    return {
        "office": _("Office"),
        "emergency": _("Emergency indicator"),
        "cp_output": _("CP output"),
        "partner_type": _("Partner type"),
    }


def without(params: QueryDict) -> QueryDict:
    """``params`` without the removed filters (a copy, mutable)."""
    out = params.copy()
    for key in KEYS:
        out.pop(key, None)
    return out


def _values(params: QueryDict, key: str) -> list[str]:
    """The values of ``key`` the pages' filters read (``monitoring.Filters``,
    ``reporting.ReportFilters``): the emergency indicator's yes or no, the CP output's words, an office's
    or a partner type's names."""
    if key == "emergency":
        value = str(params.get(key) or "").strip().lower()
        return [value] if value in ("yes", "no") else []
    if key == "cp_output":
        value = str(params.get(key) or "").strip()
        return [value] if value else []
    return [v for v in params.getlist(key) if v]


def note(path: str, params: QueryDict, keys: tuple[str, ...] = KEYS) -> dict[str, Any] | None:
    """The line of a page that applies the removed filters ``keys`` of its address: each one it applies
    with its values (``items``, empty when it applies none of them: the line is then not drawn), the page's
    address without any removed filter (``url``) and the hidden fields of its filter bar (``hidden``: the
    address's own values of every removed filter, so Apply keeps them); None when the address carries
    none."""
    hidden = [(key, value) for key in KEYS for value in params.getlist(key) if value]
    if not hidden:
        return None
    labels = _labels()
    items = []
    for key in keys:
        values = _values(params, key)
        if values:
            shown = [{"yes": _("Yes"), "no": _("No")}[v] for v in values] if key == "emergency" else values
            items.append({"key": key, "label": labels[key], "values": shown})
    kept = without(params)
    for key in ("page", "export"):
        kept.pop(key, None)
    query = kept.urlencode()
    return {"items": items, "url": path + (f"?{query}" if query else ""), "hidden": hidden}
