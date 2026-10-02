"""Charts in Ask NeuroDB's answers: the model asks for one (``make_chart``) and the page draws it with
the dashboards' chart code (Plotly), under the answer.

A chart only shows numbers the answer's lookups returned: every value is checked against the numbers
in this answer's tool results (to the rounding of one decimal), so a chart cannot show a figure the
model made up. Shares of a whole (a pie) are worked out by the chart itself from the counts.
"""

from __future__ import annotations

import math
import re
from typing import Any


def _refused(message: str) -> Exception:
    from .tools import ToolInputError  # here: tools.py registers this module's tool

    return ToolInputError(message)


KINDS = {"line": "lines", "column": "grouped", "bar": "bars", "pie": "pie"}
MAX_LABELS = 60
MAX_SERIES = 6
_NUMERIC = re.compile(r"^[+-]?\d[\d,]*(?:\.\d+)?%?$")


def numbers_in(obj: Any, out: set[float] | None = None) -> set[float]:
    """Every number in a tool result (numbers, and strings such as "4,386" or "82%")."""
    out = set() if out is None else out
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, int | float):
        if math.isfinite(obj):
            out.add(float(obj))
    elif isinstance(obj, str):
        text = obj.strip()
        if len(text) <= 24 and _NUMERIC.match(text):
            out.add(float(text.replace(",", "").rstrip("%")))
    elif isinstance(obj, dict):
        for value in obj.values():
            numbers_in(value, out)
    elif isinstance(obj, list | tuple):
        for value in obj:
            numbers_in(value, out)
    return out


def _looked_up(value: float, seen: set[float]) -> bool:
    return any(abs(value - s) <= max(0.051, abs(s) * 0.0005) for s in seen)


def build(args: dict[str, Any], seen: set[float]) -> dict[str, Any]:
    """The chart to draw, checked; ToolInputError (sent back to the model) when it cannot be."""
    kind = args["kind"]
    labels = [str(label)[:80] for label in args["labels"]]
    raw_series = args["series"]
    if not labels or len(labels) > MAX_LABELS:
        raise _refused(f"Give 1 to {MAX_LABELS} labels.")
    if not raw_series or len(raw_series) > MAX_SERIES:
        raise _refused(f"Give 1 to {MAX_SERIES} series.")
    if kind in ("pie", "bar") and len(raw_series) > 1:
        raise _refused(f"A {kind} chart shows one series; use 'column' to compare several.")
    series = []
    for item in raw_series:
        if not isinstance(item, dict) or not isinstance(item.get("values"), list):
            raise _refused("Each series is an object with 'name' and 'values' (numbers).")
        values = item["values"]
        if len(values) != len(labels):
            raise _refused("Each series needs one value per label (use null for a missing one).")
        clean = []
        for value in values:
            if value is None:
                clean.append(None)
                continue
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                raise _refused("Values must be numbers.")
            if not _looked_up(float(value), seen):
                raise _refused(
                    f"{value} was not returned by a lookup in this answer. A chart shows only figures looked "
                    "up (look them up first, or ask the lookup for the changes)."
                )
            clean.append(float(value))
        if kind == "pie" and any(v is not None and v < 0 for v in clean):
            raise _refused("A pie chart cannot show negative values.")
        series.append({"name": str(item.get("name") or "")[:80], "values": clean})
    if kind in ("bar", "pie"):
        data: Any = [[label, value] for label, value in zip(labels, series[0]["values"], strict=True)]
    else:
        data = {
            "labels": labels,
            "series": {s["name"] or f"Series {i + 1}": s["values"] for i, s in enumerate(series)},
        }
    return {
        "chart": KINDS[kind],
        "title": str(args.get("title") or "")[:160],
        "unit": str(args.get("unit") or "")[:20],
        "data": data,
        "orientation": "h" if kind == "bar" else "v",
    }


def make_chart(**_: Any) -> dict[str, Any]:
    raise _refused("Charts are drawn while answering.")  # run by agent._run_tools, not tools.run


CHART_TOOLS = {
    "make_chart": (
        make_chart,
        "Draw a chart under the answer from figures looked up in this answer (a chart cannot show other "
        "numbers): 'line' for values over time, 'column' to compare categories or periods (several "
        "series side by side, e.g. before and after), 'bar' for one series of many categories, 'pie' for "
        "the parts of a whole (give the counts; the chart works out the shares). Labels are the dates "
        "or categories; each series has a name and one value per label.",
        {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS)},
                "title": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
                "series": {"type": "array", "items": {"type": "object"}},
                "unit": {"type": "string", "description": "e.g. people, %"},
            },
            "required": ["kind", "title", "labels", "series"],
            "additionalProperties": False,
        },
        "Drawing a chart",
    ),
}
