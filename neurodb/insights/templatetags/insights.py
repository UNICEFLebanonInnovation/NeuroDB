from typing import Any

from django import template

register = template.Library()


@register.filter
def share(value: Any) -> str:
    """``0.065`` -> ``"7%"``; ``None`` -> ``"—"``."""
    if value is None or value == "":
        return "—"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value)
