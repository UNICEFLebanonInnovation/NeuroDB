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
    "fresh": "success",
    "stale": "warning",
    "unknown": "neutral",
    "succeeded": "success",
    "partial": "warning",
    "failed": "danger",
    "running": "info",
    "active": "success",
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
def get_item(mapping: Any, key: Any) -> Any:
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def month_name(value: Any) -> str:
    names = [_("January"), _("February"), _("March"), _("April"), _("May"), _("June"), _("July"), _("August"), _("September"), _("October"), _("November"), _("December")]
    try:
        return str(names[int(value) - 1])
    except (TypeError, ValueError, IndexError):
        return str(value)
