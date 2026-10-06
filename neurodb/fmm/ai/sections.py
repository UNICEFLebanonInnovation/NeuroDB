"""The parts of an AI brief, as each prompt version lists them (``PromptVersion.sections``), like FMS's
``[meta:insights_sections]``: a key (what the AI writes under; never shown), a label (the heading on the
page), a format (``paragraph``: sentences read as one paragraph; ``bullets``: a list) and the most
sentences or bullets it may hold.

The brief's strict JSON schema is built from the list (:func:`schema`): the keys are exactly the list's.
Every part is a list of sentences, each with the keys of the facts it rests on, but the priority action
points (key ``action_points``; ``priority_actions`` in the versions of Release 1), which are structured:
priority (High or Medium), programme section, partner (or none), action, responsible party (a role or a
section), timeframe and the keys of the facts. NeuroDB, not the AI, writes them out as
"[PRIORITY: High] Section / Partner — action — responsible — timeframe" (:func:`action_line`).

The versions published before Release 2 keep their own four parts (:data:`LEGACY`), so their briefs are
shown as they were written.
"""

from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import ValidationError

from ..models import default_insight_sections

FORMATS = ("paragraph", "bullets")
ACTION_KEYS = ("action_points", "priority_actions")  # the structured part (Release 2, Release 1)
MAX_SECTIONS = 8
MAX_ITEMS = 30
LABEL_CHARS = 80
KEY = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
MAX_EXAMPLES = 8
EXAMPLE_CHARS = 200

# The parts of the briefs written before Release 2 (prompt version 1 and its copies)
LEGACY = [
    {"key": "coverage_quality", "label": "Coverage and quality", "format": "paragraph", "max_items": 4},
    {"key": "programmatic_findings", "label": "Programmatic findings", "format": "bullets", "max_items": 6},
    {"key": "operational_challenges", "label": "Operational challenges", "format": "bullets", "max_items": 5},
    {"key": "recommendations", "label": "Recommendations", "format": "bullets", "max_items": 5},
    {"key": "priority_actions", "label": "Priority action points", "format": "bullets", "max_items": 6},
]
# What the brief NeuroDB writes from the figures puts in each part, by key (Release 2's and Release 1's)
ROLES = {
    "coverage_summary": "coverage",
    "coverage_quality": "coverage",
    "key_findings": "findings",
    "programmatic_findings": "findings",
    "challenges": "challenges",
    "operational_challenges": "challenges",
    "recommendations": "recommendations",
    "action_points": "actions",
    "priority_actions": "actions",
}

PRIORITIES = ["High", "Medium"]
SHOWN_PRIORITIES = ("High", "Medium", "Low")  # Low: the briefs of Release 1
TIMEFRAMES = [
    "within 1 week",
    "within 10 working days",
    "within 2 weeks",
    "within 4 weeks",
    "within 6 weeks",
    "before the next reporting cycle",
    "ongoing",
]
SENTENCE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "keys"],
    "properties": {"text": {"type": "string"}, "keys": {"type": "array", "items": {"type": "string"}}},
}
ACTION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["priority", "section", "partner", "action", "owner_role", "timeframe", "keys"],
    "properties": {
        "priority": {"type": "string", "enum": PRIORITIES},
        "section": {"type": "string"},
        "partner": {"type": "string"},  # "" when the action is about no one partner
        "action": {"type": "string"},
        "owner_role": {"type": "string"},  # the responsible party: a role or a section, never a person
        "timeframe": {"type": "string", "enum": TIMEFRAMES},
        "keys": {"type": "array", "items": {"type": "string"}},
    },
}


def is_action(section: dict[str, Any]) -> bool:
    return section.get("key") in ACTION_KEYS


def of(version) -> list[dict[str, Any]]:
    """The parts of ``version`` (the defaults without a version, or when a version holds none)."""
    found = getattr(version, "sections", None) if version is not None else None
    if isinstance(found, list) and found:
        return found
    return default_insight_sections()


def text_parts(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The parts written as sentences (all but the action points)."""
    return [s for s in sections if not is_action(s)]


def action_part(sections: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((s for s in sections if is_action(s)), None)


def schema(sections: list[dict[str, Any]]) -> dict[str, Any]:
    """The brief's strict JSON schema: one array per part, keys fixed by the list. No ``maxItems``
    (strict mode refuses it): the limits are enforced in code."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [s["key"] for s in sections],
        "properties": {
            s["key"]: {"type": "array", "items": ACTION if is_action(s) else SENTENCE} for s in sections
        },
    }


def instructions(sections: list[dict[str, Any]]) -> str:
    """The parts to write, in words, as the prompt gives them to the AI after the editable text."""
    lines = ["The brief's parts (JSON keys), in order:"]
    for s in sections:
        n = s["max_items"]
        if is_action(s):
            what = f"at most {n} priority action points, most urgent first"
        elif s["format"] == "paragraph":
            what = f"one paragraph of at most {n} sentences"
        else:
            what = f"at most {n} bullets, one sentence each"
        lines.append(f"- {s['key']} ({s['label']}): {what}.")
    return "\n".join(lines)


def action_line(action: dict[str, Any]) -> str:
    """ "[PRIORITY: High] Cash / Partner X — action — responsible — timeframe": an action point as the
    page and the copy write it (the partner left out when there is none)."""
    where = " / ".join(part for part in (action.get("section"), action.get("partner")) if part)
    owner = action.get("owner_role") or action.get("responsible") or ""
    parts = [where, action.get("action") or "", owner, action.get("timeframe") or ""]
    return f"[PRIORITY: {action.get('priority', '')}] " + " — ".join(p for p in parts if p)


# ------------------------------------------------------------------------------------------ checks
def validate(value: Any) -> list[dict[str, Any]]:
    """The parts of a draft checked and written in one form; a ``ValidationError`` naming each problem
    in plain words otherwise."""
    if not isinstance(value, list) or not value:
        raise ValidationError('List the parts of the brief, e.g. [{"key": "key_findings", ...}].')
    if len(value) > MAX_SECTIONS:
        raise ValidationError(f"At most {MAX_SECTIONS} parts.")
    out, seen, problems = [], set(), []
    for n, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            problems.append(f"Part {n} must be an object with key, label, format and max_items.")
            continue
        unknown = sorted(set(raw) - {"key", "label", "format", "max_items"})
        if unknown:
            problems.append(f"Part {n} has unknown settings: {', '.join(unknown)}.")
        key, label = str(raw.get("key") or "").strip(), " ".join(str(raw.get("label") or "").split())
        form, items = raw.get("format"), raw.get("max_items")
        if not KEY.match(key):
            problems.append(f"Part {n}: the key is 2 to 40 lower-case letters, digits or _ (not “{key}”).")
        elif key in seen:
            problems.append(f"The key {key} is used twice.")
        if not 1 <= len(label) <= LABEL_CHARS:
            problems.append(f"Part {n}: the label has 1 to {LABEL_CHARS} characters.")
        if form not in FORMATS:
            problems.append(f"Part {n}: the format is paragraph or bullets.")
        elif key in ACTION_KEYS and form != "bullets":
            problems.append("The action points are bullets.")
        if isinstance(items, bool) or not isinstance(items, int) or not 1 <= items <= MAX_ITEMS:
            problems.append(f"Part {n}: max_items is a whole number from 1 to {MAX_ITEMS}.")
        seen.add(key)
        out.append({"key": key, "label": label, "format": form, "max_items": items})
    if sum(1 for s in out if s["key"] in ACTION_KEYS) > 1:
        problems.append("Only one part holds the action points.")
    if problems:
        raise ValidationError(problems)
    return out


def validate_examples(value: Any) -> list[str]:
    """The chat's starter questions checked: at most 8 texts of 1 to 200 characters."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValidationError("The starter questions are a list of texts.")
    out = [" ".join(v.split()) for v in value if v.strip()]
    if len(out) > MAX_EXAMPLES:
        raise ValidationError(f"At most {MAX_EXAMPLES} starter questions.")
    if any(len(v) > EXAMPLE_CHARS for v in out):
        raise ValidationError(f"A starter question has at most {EXAMPLE_CHARS} characters.")
    return out
