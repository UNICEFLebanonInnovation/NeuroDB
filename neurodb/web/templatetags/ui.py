"""Small presentation helpers shared by every page: query strings, numbers, status pills, nav state."""

from __future__ import annotations

from typing import Any

from django import template
from django.utils.translation import gettext_lazy as _

from neurodb.indicators.services.tracking import LABELS

register = template.Library()

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
    return f"{number_value:,.{digits}f}"


@register.filter
def percent(value: Any, digits: int = 1) -> str:
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value):,.{digits}f}%"
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
            text = f"{scaled:,.0f}" if scaled >= 100 else f"{scaled:,.1f}".removesuffix(".0")
            return f"{sign}${text}{suffix}"
    return f"{sign}${amount:,.0f}"


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
    return 'aria-current="page"' if active_if(context, *view_names, pk=pk) else ""


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
}


@register.simple_tag
def asset_urls() -> str:
    from django.templatetags.static import static
    from django.utils.html import json_script

    return json_script({key: static(path) for key, path in ASSETS.items()}, "asset-urls")
