"""The code-written brief: what Monitoring insights shows when the AI is not used or wrote nothing that
passed the checks.

:func:`brief` writes, from the same facts the AI would read, the parts the prompt version lists (each
by what it is for: coverage, findings, challenges, recommendations, action points): a few sentences on
coverage and quality (the shares of ratings over the rated visits only, the Not monitored visits a
count apart), the most frequent quality flags, one fixed piece of advice per rule that flagged visits,
and a priority action point for each of the most urgent visits. Every sentence cites the entries it
rests on and passes the same checks as the AI's (``watch.grounding``). Findings about programme
delivery need the notes themselves, so that part stays empty, with a note; so does a part the
administrators added that NeuroDB has nothing to write in.
"""

from __future__ import annotations

from typing import Any

from . import sections as sections_module
from .facts import Facts

FINDINGS_NOTE = "Findings need the AI or the narratives themselves; open the visits below."
OTHER_NOTE = "Written by the AI only."
ACTION = "Follow up the visit's findings and record an action point in eTools"
OWNER = "Section lead"
TIMEFRAME = "within 2 weeks"
ALL_SECTIONS = "All sections"
ACTIONS = 3
ISSUES = 3
COVERAGE = 5  # sentences
RATINGS = (("on_track", "were On track"), ("constrained", "Constrained"), ("off_track", "Off track"))

# One sentence of advice per rule that flagged visits (no figure in them: nothing to check)
RULE_ADVICE = {
    "R1": "Complete the general observation, the rating and Q1 and Q2 before submitting a visit report.",
    "R2": "Answer every checklist question that applies before submitting a visit report.",
    "R3": "Describe in Q2 what the monitor verified, with figures by sex and age where people are served.",
    "R5": "Check that Q1 (implementation status) agrees with the activities reported in Q2.",
    "R6": "Check that the general observation agrees with Q1, Q2 and the rating, and adds to them.",
    "R7": "Record specific observations and assigned, time-bound action points in Q3.",
    "R8": "Raise an action point for each problem the narrative, Q1 or Q2 describes.",
    "R32": "Make sure each key challenge of the visit has a matching action point.",
    "R19": "Check that each visit's monitor is on the staff list of its field office.",
    "R20": "Check that programme document visits take place at the document's registered locations.",
    "R21": "Check that each CP output visit names the sections that work on the output.",
    "R23": "Check that each visit's place is among the registered locations of its programme document.",
}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _coverage(facts: Facts) -> list[dict[str, Any]]:
    """Coverage and quality. Shares of ratings are over the rated visits only; the Not monitored
    visits (planned, not conducted) are a count apart, never a share of all visits."""
    k = facts.payload["kpi"]
    out = [
        {
            "text": f"{_plural(k['visits'], 'visit', 'visits')} and "
            f"{_plural(k['entities'], 'entity', 'entities')} in the period.",
            "keys": ["kpi"],
        }
    ]
    if k["rated_visits"]:
        on, con, off = (f"{k[f'{c}_visits']} {label} ({k[f'{c}_share_of_rated']}%)" for c, label in RATINGS)
        rated = _plural(k["rated_visits"], "rated visit", "rated visits")
        out.append({"text": f"Of the {rated}, {on}, {con} and {off}.", "keys": ["kpi"]})
    if k["not_monitored_visits"]:
        n = k["not_monitored_visits"]
        verb = "was" if n == 1 else "were"
        out.append(
            {
                "text": f"{_plural(n, 'visit', 'visits')} {verb} Not monitored (planned, not conducted), "
                "counted apart from the rated visits.",
                "keys": ["kpi"],
            }
        )
    out.append(
        {
            "text": f"{_plural(k['visits_reported'], 'visit was', 'visits were')} reported, "
            f"{_plural(k['visits_in_progress'], 'is', 'are')} in progress and "
            f"{_plural(k['visits_planned'], 'is', 'are')} planned.",
            "keys": ["kpi"],
        }
    )
    if k["avg_quality"] is not None:
        out.append(
            {
                "text": f"The average quality score was {k['avg_quality']}% on "
                f"{_plural(k['scored_visits'], 'scored visit', 'scored visits')}.",
                "keys": ["kpi"],
            }
        )
    else:
        out.append({"text": "No visit in this filter could be scored for quality.", "keys": ["kpi"]})
    previous = facts.payload.get("previous") or {}
    if previous.get("visits"):
        change = previous["visits_change"]
        if change:
            how = f"{abs(change)} more" if change > 0 else f"{abs(change)} fewer"
            text = f"That is {how} visits than the {previous['visits']} of the previous period."
        else:
            text = f"The previous period had as many visits, {previous['visits']}."
        out.append({"text": text, "keys": ["kpi", "previous"]})
    return out[:COVERAGE]


def _challenges(facts: Facts) -> list[dict[str, Any]]:
    out = []
    issues = sorted(facts.payload.get("issues", {}).values(), key=lambda i: (-i["visits"], i["key"]))
    for issue in issues[:ISSUES]:
        rule, _sep, words = issue["label"].partition(": ")
        if not words:
            rule, words = issue["key"].split(":")[1], issue["label"]
        out.append(
            {
                "text": f"{rule} flagged {_plural(issue['visits'], 'visit', 'visits')}: {words}.",
                "keys": [issue["key"]],
            }
        )
    ap = facts.payload.get("action_points") or {}
    overdue = _plural(ap.get("fm_overdue", 0), "follow-up action point is", "follow-up action points are")
    if ap.get("fm_overdue") or ap.get("visits_without_follow_up"):
        out.append(
            {
                "text": f"{overdue} overdue, and {ap['visits_without_follow_up']} visits rated off track or "
                "constrained have no follow-up action point.",
                "keys": [ap["key"]],
            }
        )
    return out[:5]


def _recommendations(facts: Facts) -> list[dict[str, Any]]:
    """The advice of the rules that flagged visits, the most flagged first (then in the order of the
    rule ids)."""
    from ..rules import code_order

    out = []
    rules = facts.payload.get("rules", {})
    for key, rule in sorted(
        rules.items(), key=lambda kv: (-kv[1]["flagged"], code_order(kv[0].split(":", 1)[1]))
    ):
        code = key.split(":", 1)[1]
        if rule["flagged"] and code in RULE_ADVICE:
            out.append({"text": RULE_ADVICE[code], "keys": [key]})
    return out[:5]


def _section_of(card: dict[str, Any], facts: Facts) -> str:
    known = {
        entry["name"].casefold()
        for group in ("sections", "offices")
        for entry in facts.payload.get(group, {}).values()
    }
    return next((name for name in card.get("sections") or [] if name.casefold() in known), ALL_SECTIONS)


def _actions(facts: Facts) -> list[dict[str, Any]]:
    red = facts.limits.get("red", 70)
    amber = facts.limits.get("amber", 40)
    urgent = sorted(
        (card for card in facts.payload.get("visits", {}).values() if (card.get("urgency") or 0) >= amber),
        key=lambda card: (-(card.get("urgency") or 0), card["key"]),
    )
    return [
        {
            "priority": "High" if card["urgency"] >= red else "Medium",
            "section": _section_of(card, facts),
            "partner": card.get("partner") or "",
            "action": ACTION,
            "owner_role": OWNER,
            "timeframe": TIMEFRAME,
            "keys": [card["key"]],
        }
        for card in urgent[:ACTIONS]
    ]


def brief(facts: Facts, parts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The code-written brief of ``facts`` in ``parts`` (a prompt version's; the defaults when not
    given): ``{"sections": {key: [{"text", "keys"}], "notes": {key: why it is empty}}, "actions":
    [...]}``, each part cut to its ``limit``."""
    parts = parts if parts is not None else sections_module.of(None)
    writers = {
        "coverage": lambda: _coverage(facts),
        "findings": list,
        "challenges": lambda: _challenges(facts),
        "recommendations": lambda: _recommendations(facts),
    }
    out: dict[str, Any] = {}
    notes: dict[str, str] = {}
    for part in sections_module.text_parts(parts):
        role = sections_module.ROLES.get(part["key"], "")
        out[part["key"]] = writers[role]()[: part["limit"]] if role in writers else []
        if not out[part["key"]]:
            notes[part["key"]] = FINDINGS_NOTE if role == "findings" else OTHER_NOTE if not role else ""
    action = sections_module.action_part(parts)
    actions = _actions(facts)[: action["limit"]] if action is not None else []
    out["notes"] = {key: note for key, note in notes.items() if note}
    return {"sections": out, "actions": actions}
