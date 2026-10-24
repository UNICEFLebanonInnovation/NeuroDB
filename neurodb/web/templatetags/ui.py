"""Small presentation helpers shared by every page: query strings, numbers, status pills, nav state."""

from __future__ import annotations

import datetime
import functools
import json
import math
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

from django import template
from django.template.defaultfilters import date as date_filter
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

from neurodb.indicators.services.tracking import LABELS

register = template.Library()


def half_up(value: float, digits: int = 0) -> float | int:
    """``value`` rounded with halves away from zero (12.5 -> 13, 0.45 -> 0.5), the rule of Django's
    ``floatformat``; Python's ``round`` and ``f"{x:.0f}"`` round halves to even (12.5 -> 12). An int
    when ``digits`` is 0."""
    if not math.isfinite(value):
        return value
    rounded = Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return int(rounded) if digits <= 0 else float(rounded)


def fixed(value: float, digits: int = 0) -> str:
    """``value`` with a thousands separator and ``digits`` decimals, halves rounded up (see
    :func:`half_up`): every number the pages show goes through here or ``floatformat``."""
    if not math.isfinite(value):
        return str(value)
    rounded = Decimal(str(value)).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return f"{rounded:,.{max(digits, 0)}f}"


STATUS_VARIANTS = {
    "on_track": "success",
    "off_track": "danger",
    "over_target": "warning",
    "no_target": "neutral",
    "not_reported": "neutral",
    "fresh": "success",
    "stale": "warning",
    "unknown": "neutral",
    "succeeded": "success",
    "partial": "warning",
    "failed": "danger",
    "running": "info",
    "active": "success",
    "open": "warning",
    "completed": "success",
    "final": "success",
    "approved": "success",
    "report_submitted": "info",
    "cancelled": "neutral",
    # field monitoring (Monitoring insights): HACT ratings, visit status groups and review marks
    "constrained": "warning",
    "not_monitored": "neutral",
    "reviewed": "success",
    "follow_up": "warning",
    "data_issue": "danger",
    "planned": "neutral",
    "in_progress": "info",
    "reported": "success",
    # action points: the AI's verdict, the PME verification, a NeuroDB action point's status
    "adequate": "success",
    "not_addressed": "danger",
    "vague": "warning",
    "verified": "success",
    "rejected": "danger",
    "pending": "neutral",
    "done": "success",
    "dropped": "neutral",
    # eTools progress report statuses (neurodb.datamart.reached)
    "accepted": "success",
    "submitted": "info",
    "sent_back": "warning",
    "due": "neutral",
    "overdue": "danger",
    "not_yet_due": "neutral",
}


@register.simple_tag(takes_context=True)
def qs_replace(context: dict[str, Any], **updates: Any) -> str:
    """``?{% qs_replace page=2 %}`` keeps the current query string and updates/removes the given keys.

    Passing ``None`` or ``""`` removes a key; lists are expanded to repeated keys.
    """
    request = context.get("request")
    params = request.GET.copy() if request is not None else {}
    for key, value in updates.items():
        if value in (None, ""):
            params.pop(key, None)
        elif isinstance(value, list | tuple):
            params.setlist(key, [str(v) for v in value])
        else:
            params[key] = str(value)
    return params.urlencode() if params else ""


@register.filter
def number(value: Any, digits: int = 1) -> str:
    """Thousands separator; integers stay integers, other numbers get one decimal."""
    if value is None or value == "":
        return "—"
    try:
        number_value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number_value.is_integer():
        return f"{int(number_value):,}"
    return fixed(number_value, digits)


@register.filter
def percent(value: Any, digits: int = 1) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{fixed(float(value), digits)}%"
    except (TypeError, ValueError):
        return str(value)


@register.filter
def money(value: Any) -> str:
    """``1234567`` -> ``"$1.2M"``, ``820000`` -> ``"$820k"``, ``950`` -> ``"$950"``; ``None`` -> ``"—"``."""
    if value is None or value == "":
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if amount >= threshold:
            scaled = amount / threshold
            text = fixed(scaled, 0) if scaled >= 100 else fixed(scaled, 1).removesuffix(".0")
            return f"{sign}${text}{suffix}"
    return f"{sign}${fixed(amount, 0)}"


@register.inclusion_tag("components/status_pill.html")
def status_pill(status: str | None, label: str | None = None, size: str = "") -> dict[str, Any]:
    status = status or "unknown"
    return {
        "status": status,
        "variant": STATUS_VARIANTS.get(status, "neutral"),
        "label": label or LABELS.get(status) or str(status).replace("_", " ").capitalize(),
        "size": size,
    }


@register.simple_tag(takes_context=True)
def active_if(context: dict[str, Any], *view_names: str, pk: int | None = None, css: str = "active") -> str:
    """Return ``css`` when the current route matches one of ``view_names`` (and ``pk`` if given)."""
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    if match is None:
        return ""
    if match.view_name not in view_names:
        return ""
    if pk is not None and str(match.kwargs.get("pk")) != str(pk):
        return ""
    return css


@register.simple_tag(takes_context=True)
def aria_current(context: dict[str, Any], *view_names: str, pk: int | None = None) -> str:
    return mark_safe('aria-current="page"') if active_if(context, *view_names, pk=pk) else ""


@register.filter
def code_label(value: Any) -> str:
    """``"report_submitted"`` -> ``"Report submitted"``: an eTools status code as text."""
    text = str(value or "").strip().replace("_", " ")
    return text[:1].upper() + text[1:] if text else "—"


@register.filter
def status_key(value: Any) -> str:
    """``"On Track"`` -> ``"on_track"``: an eTools label as a status pill key."""
    return str(value or "unknown").strip().lower().replace(" ", "_").replace("-", "_")


@register.filter
def get_item(mapping: Any, key: Any) -> Any:
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def paired(keys: Any, values: Any) -> list[tuple[Any, Any]]:
    """``keys|paired:values``: each key with its value, in order (``{% for key, n in keys|paired:n %}``)."""
    return list(zip(keys or (), values or (), strict=False))


@register.filter
def has_id(items, value) -> bool:
    """True when one of ``items`` has ``id == value`` (opens the sidebar group of the current database)."""
    return value is not None and any(getattr(item, "id", None) == value for item in items or [])


@register.filter
def month_name(value: Any) -> str:
    names = [
        _("January"),
        _("February"),
        _("March"),
        _("April"),
        _("May"),
        _("June"),
        _("July"),
        _("August"),
        _("September"),
        _("October"),
        _("November"),
        _("December"),
    ]
    try:
        return str(names[int(value) - 1])
    except (TypeError, ValueError, IndexError):
        return str(value)


@register.filter
def day(value: Any) -> str:
    """``value|date:"j M Y"`` ("12 May 2026"; "" without a date), each day written once per language: a
    table of a thousand visits repeats a few hundred days, and the ``date`` filter formats every one. A
    date and time is formatted each time (in the current time zone)."""
    if value in (None, ""):
        return ""
    if type(value) is datetime.date:
        return _day(value, get_language())
    return date_filter(value, "j M Y")


@functools.lru_cache(maxsize=4096)
def _day(value: datetime.date, language: str | None) -> str:
    return date_filter(value, "j M Y")


# Vendor scripts and page modules loaded on demand by static/js/app.js. Hashed URLs come from the
# staticfiles manifest, so a deploy never serves a stale library from a browser cache.
ASSETS = {
    "plotly": "vendor/plotly/plotly-basic.min.js",
    "jquery": "vendor/jquery/jquery.min.js",
    "jqueryui": "vendor/jquery-ui/jquery-ui.min.js",
    "pivot": "vendor/pivottable/pivot.min.js",
    "pivotCss": "vendor/pivottable/pivot.min.css",
    "pivotPlotly": "vendor/pivottable/plotly_renderers.min.js",
    "pivotExport": "vendor/pivottable/export_renderers.min.js",
    "maplibre": "vendor/maplibre-gl/maplibre-gl-csp.js",
    "maplibreWorker": "vendor/maplibre-gl/maplibre-gl-csp-worker.js",
    "maplibreCss": "vendor/maplibre-gl/maplibre-gl.css",
    "tomSelect": "vendor/tom-select/tom-select.complete.min.js",
    "tomSelectCss": "vendor/tom-select/tom-select.bootstrap5.min.css",
    "lib": "js/lib.js",
    "charts": "js/charts.js",
    "pivotModule": "js/pivot.js",
    "mapModule": "js/map.js",
    "pdMapModule": "js/pdmap.js",
    "eduMapModule": "js/edumap.js",
    "askModule": "js/ask.js",
}


@register.simple_tag
def asset_urls() -> str:
    from django.templatetags.static import static
    from django.utils.html import json_script

    return json_script({key: static(path) for key, path in ASSETS.items()}, "asset-urls")


# The project's own modules (static/js) import one another by relative path (app.js: from
# "./parts.js"), which collectstatic leaves as it is: the browser fetched them under their plain names,
# which WhiteNoise lets it keep for a minute only, so for that minute around a release a new page could
# run with a module of the old one (review R2-critic-1: the old parts.js never marked the page
# "parts-ready", and base.html's fallback hid Try again on its placeholders; a module whose exports
# changed would stop app.js). The import map sends each plain name to this release's hashed one (kept for
# good), so the modules of one release always load together, and a module loaded by its hashed name
# (asset()) is the one the others import. Without hashed names (no manifest: development, tests) no map
# is written.
OWN_MODULES = Path(__file__).resolve().parent.parent / "static" / "js"


@functools.cache
def _own_modules() -> tuple[str, ...]:
    return tuple(sorted(f"js/{path.name}" for path in OWN_MODULES.glob("*.js")))


@register.simple_tag(takes_context=True)
def module_map(context) -> str:
    from django.conf import settings
    from django.templatetags.static import static
    from django.utils.encoding import filepath_to_uri
    from django.utils.html import format_html

    imports = {}
    for path in _own_modules():
        plain, hashed = urljoin(settings.STATIC_URL, filepath_to_uri(path)), static(path)
        if hashed != plain:
            imports[plain] = hashed
    if not imports:
        return ""
    data = json.dumps({"imports": imports}, sort_keys=True)
    data = data.replace("<", "\\u003C").replace(">", "\\u003E").replace("&", "\\u0026")  # as json_script does
    nonce = getattr(context.get("request"), "csp_nonce", "")
    return format_html('<script type="importmap" nonce="{}">{}</script>', nonce, mark_safe(data))


# The header's year menu (base.html), drawn only on the pages that show one year. Calendar years
# (``years.available()``) on the pages that read ``?year=`` as a calendar year; ActivityInfo years (the
# reporting years) on the ActivityInfo pages. Not drawn elsewhere: Home, the daily review and the search show
# no year, and Population and Monitoring insights have a year or period control of their own, which the
# header would contradict.
YEAR_PAGES = frozenset(
    {
        "reports:country_overview",
        "reports:brief",
        "reports:assurance",
        "reports:funds",
        "reports:partner_reporting",
        "reports:monitoring",
        "reports:pd_monitoring",
        "reports:pd_monitoring_map",
        "knowledge:index",
    }
)
# The pages that show every year while no ``?year=`` is given: their label then reads "All years"
ALL_YEARS_PAGES = frozenset(
    {
        "reports:assurance",
        "reports:funds",
        "reports:monitoring",
        "reports:partner_reporting",
        "knowledge:index",
    }
)
# Their tabs that show one year all the same (this year by default): Field monitoring's HACT visit plan
ONE_YEAR_TABS = {"reports:monitoring": frozenset({"plan"})}
# The pages of one ActivityInfo database or report: a year there goes to that year's list of databases
YEAR_LIST_PAGES = frozenset(
    {
        "reports:database_dashboard",
        "reports:database_analytical",
        "reports:database_snapshot",
        "reports:database_map",
        "reports:database_raw_data",
        "reports:indicator_detail",
        "reports:report_dashboard",
        "reports:report_analytical",
        "reports:report_hpm",
    }
)
AI_YEAR_PAGES = YEAR_LIST_PAGES | {"reports:databases"}


def _view_name(context) -> str:
    match = getattr(context.get("request"), "resolver_match", None)
    return match.view_name if match else ""


def _shows_every_year(context, name: str) -> bool:
    """Whether the page shows every year while no ``?year=`` is given (its label then reads "All years", and
    its menu has an "All years" entry): one of :data:`ALL_YEARS_PAGES`, unless the tab open shows one year
    (:data:`ONE_YEAR_TABS`)."""
    if name not in ALL_YEARS_PAGES:
        return False
    request = context.get("request")
    tab = request.GET.get("tab", "") if request is not None else ""
    return tab not in ONE_YEAR_TABS.get(name, ())


@register.simple_tag(takes_context=True)
def year_url(context: dict[str, Any], year: Any) -> str:
    """Where the year menu goes for ``year``: the same page in that year (its other filters kept, back to the
    first page), or, from one ActivityInfo database or report, that year's list of databases."""
    request = context.get("request")
    name = _view_name(context)
    if name in YEAR_LIST_PAGES or request is None:
        return f"{reverse('reports:databases')}?{urlencode({'year': year})}"
    params = request.GET.copy()
    params["year"] = str(year)
    params.pop("page", None)
    return f"{request.path}?{params.urlencode()}"


def _ai_page_year(context) -> str:
    """The reporting year an ActivityInfo page shows: the view's own, else the menu's (the ``?year=``, or the
    year of the database or report open, else the year ticked current)."""
    year = context.get("year")
    if getattr(year, "name", None):
        return str(year.name)
    nav = context.get("nav")
    shown = getattr(nav, "year", None) if nav is not None else None
    return str(shown.name) if shown is not None else ""


@register.simple_tag(takes_context=True)
def page_year_label(context: dict[str, Any]) -> str:
    """The year the page shows, as the header's year menu names it: "All years" (the page says so, or
    ``?year=all``); else the year asked for (four digits) or, on an ActivityInfo page, its reporting year;
    else "All years" on a page that shows every year by default (not on a tab of it that shows one year,
    such as Field monitoring's HACT visit plan); else this year."""
    from neurodb.core import years

    request = context.get("request")
    raw = request.GET.get("year", "").strip() if request is not None else ""
    if context.get("year_all_active") or raw.lower() == "all":
        return str(_("All years"))
    name = _view_name(context)
    if name in AI_YEAR_PAGES:
        return _ai_page_year(context) or str(years.this_year())
    asked = years.parse(raw)
    if asked is not None:
        return str(asked)
    if not raw and _shows_every_year(context, name):
        return str(_("All years"))
    return str(years.this_year())


@register.simple_tag(takes_context=True)
def year_menu(context: dict[str, Any]) -> dict[str, Any] | None:
    """The header's year menu for this page, or None where it is not drawn: ``{"header", "label", "all",
    "items"}``; ``all`` is the "All years" entry (None where the page has none), each item ``{"label",
    "url", "note", "active"}``. The active entry is the one whose value is the label (none when the label's
    year is not listed)."""
    from neurodb.core import years

    request = context.get("request")
    user = getattr(request, "user", None)
    if request is None or not getattr(user, "is_authenticated", False) or context.get("is_donor"):
        return None
    name = _view_name(context)
    label = page_year_label(context)
    all_years = str(_("All years"))
    if name in AI_YEAR_PAGES:
        nav = context.get("nav")
        rows = list(getattr(nav, "years", None) or []) if nav is not None else []
        if not rows:
            return None
        ai = context.get("ai")
        in_use = bool(getattr(ai, "in_use", False)) if ai is not None else False
        items = [
            {
                "label": row.name,
                "url": year_url(context, row.name),
                "note": _("current") if row.current and in_use else "",
                "active": str(row.name) == label,
            }
            for row in rows
        ]
        return {"header": _("ActivityInfo year"), "label": label, "all": None, "items": items}
    if name not in YEAR_PAGES:
        return None
    this_year = years.this_year()
    items = [
        {
            "label": str(number),
            "url": year_url(context, number),
            "note": _("this year") if number == this_year else "",
            "active": str(number) == label,
        }
        for number in years.available()
    ]
    all_url = context.get("year_all_url")
    if not all_url and _shows_every_year(context, name):
        params = request.GET.copy()
        params.pop("year", None)
        params.pop("page", None)
        all_url = f"{request.path}?{params.urlencode()}" if params else request.path
    entry = {"url": all_url, "active": label == all_years} if all_url else None
    return {"header": _("Year"), "label": label, "all": entry, "items": items}
