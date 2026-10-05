"""The AI monitoring brief: its strict answer format.

The brief is one call without tools whose answer must follow :data:`SCHEMA` (strict JSON schema mode):
four sections of sentences, each sentence with the keys of the facts it rests on, and up to six
priority actions with fixed priorities and timeframes. The schema has no ``$defs`` and no ``maxItems``
(strict mode refuses them); the most sentences per section are :data:`LIMITS`, enforced in code.

:data:`SCHEMA_VERSION` is part of every prompt version's content hash: a change of the format gives
every version a new hash. Writing, checking and showing the brief come in a later step.
"""

from __future__ import annotations

from neurodb.integrations.background import FMM_INSIGHTS_LOCK_ID

LOCK_ID = FMM_INSIGHTS_LOCK_ID  # 7_140_433, the nightly briefs
SCHEMA_VERSION = 1

PRIORITIES = ["High", "Medium", "Low"]
TIMEFRAMES = [
    "within 1 week",
    "within 2 weeks",
    "within 4 weeks",
    "within 6 weeks",
    "before the next reporting cycle",
    "ongoing",
]
SECTIONS = ("coverage_quality", "programmatic_findings", "operational_challenges", "recommendations")

SENTENCE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "keys"],
    "properties": {"text": {"type": "string"}, "keys": {"type": "array", "items": {"type": "string"}}},
}
ACTION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["priority", "section", "action", "owner_role", "timeframe", "keys"],
    "properties": {
        "priority": {"type": "string", "enum": PRIORITIES},
        "section": {"type": "string"},
        "action": {"type": "string"},
        "owner_role": {"type": "string"},
        "timeframe": {"type": "string", "enum": TIMEFRAMES},
        "keys": {"type": "array", "items": {"type": "string"}},
    },
}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [*SECTIONS, "priority_actions"],
    "properties": {
        **{name: {"type": "array", "items": SENTENCE} for name in SECTIONS},
        "priority_actions": {"type": "array", "items": ACTION},
    },
}
LIMITS = {
    "coverage_quality": 4,
    "programmatic_findings": 6,
    "operational_challenges": 5,
    "recommendations": 5,
    "priority_actions": 6,
}
