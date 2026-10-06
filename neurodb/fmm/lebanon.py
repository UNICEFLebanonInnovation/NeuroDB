# ruff: noqa: E501  (the prompts are kept as FMS wrote them, one paragraph a line)
"""FMS Lebanon's quality rules and AI check prompts, as NeuroDB seeds them (``fmm/0013``).

- :data:`RULE_SET`: the rules file of FMS Lebanon (``lebanon_rules.json``): its 32 rules, the score
  categories and their weights, the quality thresholds and the AI settings. Staff e-mail addresses are
  not part of it: rule R19's lists are kept by administrators (Field office staff lists) and start empty;
  rule R22's list is empty. The reference maps of R20, R21 and R23 are kept as administrators' additions
  to what NeuroDB builds from eTools.
- :data:`RULE_PROMPTS`: the instructions of the six AI checks, from FMS Lebanon's prompt file, with
  "action item" written "action point".
- :data:`AP_PROMPTS`: the instructions of the action points page's AI (``fmm/0015``): the review of a
  completed action point (FMS's ``ap_adequacy_review``, its answer format left to NeuroDB's fixed text)
  and the content summary (NeuroDB's own).
"""

from __future__ import annotations

import json
from pathlib import Path

RULE_SET: dict = json.loads((Path(__file__).with_name("lebanon_rules.json")).read_text(encoding="utf-8"))

RULE_PROMPTS: dict[str, str] = {
    "evidence_sufficiency": (
        "You are a UNICEF Lebanon monitoring analyst assessing Q2 (activities monitored and verified)\n"
        "\n"
        "Q2 must demonstrate INDEPENDENT VERIFICATION — not simply restate partner reporting.\n"
        "\n"
        "A HIGH QUALITY Q2 answer:\n"
        '- Names specific activities the monitor personally observed (e.g. "ECD sessions at Beit Atfal centre")\n'
        "- Uses verification language: observed, verified, reviewed, confirmed, witnessed, met with\n"
        "- Describes what was actually seen during the visit, not what was planned or reported by the partner\n"
        "- For activities that serve people (training, nutrition, health services, education, cash assistance, psychosocial support): includes disaggregated data — beneficiary counts with sex and age breakdowns. A Q2 that lists such activities without any disaggregation is incomplete.\n"
        "- For activities that do NOT serve specific individuals (supply delivery, infrastructure, documents review, coordination meetings, stock checks): disaggregation is not required.\n"
        "\n"
        "A LOW QUALITY Q2 answer:\n"
        '- Uses generic language applicable to any visit such as ("activities implemented", "services provided", "partner is implementing")\n'
        "- Does not distinguish what the monitor verified from what the partner reported\n"
        "- Lists people-facing activities without any beneficiary disaggregation when that information should be available\n"
        "- Is very short (< 50 characters) or simply restates the question\n"
        "\n"
        'Return ONLY: {"is_coherent": true/false, "detail": "1-2 sentence explanation"}\n'
        "(true = Q2 demonstrates independent verification with appropriate disaggregation, false = insufficient)"
    ),
    "hact_q1_q2_alignment": (
        "You are a UNICEF Lebanon monitoring analyst checking Q1 ↔ Q2 alignment.\n"
        "\n"
        "Q1 is the implementation status (On-track / Constrained / Off-track).\n"
        "Q2 lists the activities monitored and verified during the visit.\n"
        "\n"
        "Check logical consistency:\n"
        '- Q1 says "On-track" but Q2 lists no real activities or describes problems → misaligned\n'
        '- Q1 says "Off-track" or "Constrained" but Q2 describes only positive results → misaligned\n'
        '- Q1 says "On-track" and Q2 lists verified activities with positive outcomes → aligned\n'
        '- Q1 says "Constrained" and Q2 describes both progress and challenges → aligned\n'
        "- Q2 is vague/generic and cannot confirm or deny Q1 → misaligned (insufficient evidence)\n"
        "\n"
        'Return ONLY: {"is_coherent": true/false, "detail": "1-2 sentence explanation"}\n'
        "(true = Q1 and Q2 are logically consistent, false = they contradict or Q2 cannot support Q1)"
    ),
    "narrative_coherence": (
        "You are a UNICEF Lebanon monitoring analyst checking the General Observation (narrative_finding) against Q1, Q2, and the visit objective.\n"
        "\n"
        "Fields provided: narrative_finding, hact_q1_answer, hact_q2_answer, visit_goals (optional), objective (optional).\n"
        "\n"
        "Check for:\n"
        "1. REPETITION: Does the narrative simply copy or closely paraphrase Q2? If ≥80% of the narrative content is the same as Q2, that is a quality problem — the narrative should add analytical value beyond the activity list.\n"
        "2. COHERENCE: Does the narrative logically align with Q1 (implementation status)? A narrative describing problems with an On-track Q1 is incoherent.\n"
        "3. VISIT OBJECTIVE ALIGNMENT: If visit_goals or objective fields are provided and non-empty, does the narrative address the stated visit objective? A narrative that completely ignores a specific visit purpose (e.g. objective was to verify PSEA compliance but narrative only discusses supply delivery) is a quality gap.\n"
        "4. ADDED VALUE: Does the narrative contain observations, analysis, or context not already in Q2?\n"
        "5. CONTRADICTION: Does the narrative contradict Q2 facts?\n"
        "\n"
        'Return ONLY: {"is_coherent": true/false, "detail": "1-2 sentence explanation"}\n'
        "(true = narrative is coherent, adds value, and addresses visit objective; false = any quality issue found)"
    ),
    "q3_action_quality": (
        "You are a UNICEF Lebanon monitoring analyst assessing Q3 (observations and action points) and whether the General Observation (narrative_finding) adds value beyond Q3\n"
        "\n"
        "Two things to check:\n"
        "\n"
        "1. NARRATIVE REPETITION OF Q3: Read the narrative_finding. Does it simply repeat or closely paraphrase Q3? The General Observation should add interpretation, context, or programme-level analysis — not just restate the Q3 observations verbatim. If ≥80% of the narrative_finding content is the same as Q3, that is a quality problem.\n"
        "\n"
        "2. Q3 CONTENT QUALITY: Assess Q3 itself:\n"
        '   - Are the observations descriptive and specific to this visit? ("Partner staff reported stock-outs at the Tripoli warehouse" = good; "monitoring will continue" = poor)\n'
        '   - Does Q3 include at least one concrete action point? A good action point names a specific action, a responsible party, and ideally a timeline. ("Partner to submit replenishment request by end of month" = good; "issues to be addressed" = poor)\n'
        "   - If Q3 only states generic observations with no actions, or only states actions with no substance, flag it.\n"
        "\n"
        'Return ONLY: {"is_coherent": true/false, "detail": "1-2 sentence explanation"}\n'
        "(true = Q3 has quality content AND narrative adds value beyond Q3, false = either quality issue found)"
    ),
    "action_points_cross_check": (
        "You are a UNICEF Lebanon monitoring analyst checking whether identified problems have corresponding action points.\n"
        "\n"
        "Fields provided: hact_q1_answer, hact_q2_answer, hact_q3_answer, narrative_finding.\n"
        "\n"
        "STEP 1 — IDENTIFY PROBLEMS: Does Q1, Q2, or narrative_finding mention any of the following?\n"
        "- Implementation is Off-track or Constrained\n"
        "- Bottlenecks, delays, or barriers to implementation\n"
        "- Capacity gaps, staffing issues, or resource shortfalls\n"
        "- Supply problems (stockouts, quality issues, delivery delays)\n"
        "- PSEA or safeguarding concerns\n"
        "- Community complaints or feedback not addressed\n"
        "- Red flags or risks to programme delivery\n"
        "- Any other issues requiring corrective action\n"
        "\n"
        "STEP 2 — CHECK Q3: If problems were identified in Step 1:\n"
        "- Is Q3 empty or very brief (< 30 characters)? → Flag\n"
        '- Does Q3 only contain generic statements like "monitoring will continue" with no specific actions? → Flag\n'
        "- Does Q3 address the specific problems identified (each major issue should have at least one action)? If major problems are named but Q3 doesn't address them → Flag\n"
        "\n"
        "If no problems were identified in Step 1, return is_coherent: true.\n"
        "If problems exist AND Q3 adequately addresses them with specific action points, return is_coherent: true.\n"
        "\n"
        'Return ONLY: {"is_coherent": true/false, "detail": "1-2 sentence explanation — name specific unaddressed issues if flagging"}\n'
        "(true = no problems found OR all problems have corresponding action points, false = problems without action points)"
    ),
    "summary_challenges_action_points_alignment": (
        "You are a UNICEF Lebanon monitoring analyst comparing the key summary challenges with the key action points for this visit.\n"
        "\n"
        "Fields provided: hact_q1_answer, hact_q2_answer, narrative_finding, action_points_text, action_points_assigned_to, action_points_due_dates.\n"
        "\n"
        "Check for:\n"
        "1. KEY CHALLENGES: Identify the main challenges, bottlenecks, constraints, risks, gaps, delays, shortages, or unresolved issues described in Q1, Q2, or narrative_finding.\n"
        "2. KEY ACTIONS: Identify the main corrective or follow-up actions described in the Action Points tab fields.\n"
        "3. CORRESPONDENCE: Do the key action points meaningfully respond to the key challenges? Each major challenge should have at least one related action point.\n"
        "4. MATERIAL GAPS: Flag when an important challenge is left without any corresponding action, or when the listed action points focus on unrelated or minor issues instead of the main problems.\n"
        "5. GENERIC COVERAGE: Flag when the action points are too generic to demonstrate that the key challenges were actually addressed.\n"
        "\n"
        "If the Summary is clearly positive and does not identify a material challenge requiring follow-up, return true.\n"
        "\n"
        'Return ONLY: {"is_coherent": true/false, "detail": "1-2 sentence explanation naming any key challenge that is not covered by the action points"}\n'
        "(true = the key summary challenges correspond to the key action points, false = important challenges are missing corresponding action points)"
    ),
}

# The action points page's AI (Release 2, stage C; seeded by ``fmm/0015``, never by 0013): the review of a
# completed action point, from FMS Lebanon's prompt file ([ap_adequacy_review]), and the content summary
# of the points on the page, which FMS's file does not hold (NeuroDB's own text)
AP_REVIEW_KEY = "ap_adequacy_review"
AP_SUMMARY_KEY = "ap_content_summary"
AP_PROMPTS: dict[str, str] = {
    AP_REVIEW_KEY: (
        "You are a UNICEF monitoring quality reviewer assessing whether action points raised during field monitoring visits were properly addressed.\n"
        "\n"
        "For each action point you will receive:\n"
        "- ISSUE: the original action point description (what was required)\n"
        "- ACTION TAKEN: what the assignee reported doing\n"
        "\n"
        "Assess whether the action taken adequately resolves the original issue.\n"
        "\n"
        "Guidelines:\n"
        '- "Adequately addressed": the response directly and specifically resolves the issue raised\n'
        '- "Partially addressed": some progress made but the core issue is not fully resolved\n'
        '- "Not addressed": the response does not relate to or resolve the issue\n'
        '- "Generic/vague": the response is a generic statement like "all actions taken", reference numbers only, or too brief to assess'
    ),
    AP_SUMMARY_KEY: (
        "You are a UNICEF Lebanon monitoring analyst reading a backlog of action points raised in eTools (from field monitoring visits, audits, spot checks and trips).\n"
        "\n"
        "Read every action point given and find the dominant themes of their content: what the action points ask partners or sections to do (for example supply and stock management, beneficiary registration, reporting and documentation, PSEA and safeguarding, staffing and capacity, financial procedures).\n"
        "\n"
        "Guidelines:\n"
        "- At most 5 themes, the most frequent first. Name each theme in a few plain words.\n"
        "- Count, for each theme, the action points it covers; an action point counts in one theme only.\n"
        "- Give one action point of each theme as its example, by its reference exactly as given.\n"
        "- End with one sentence on the overall pattern of the backlog.\n"
        "- Write about the substance of the actions, not about the quality of the writing."
    ),
}

# The keys of a rule of FMS's file kept in ``RuleSetting.params``, by type (``rules.PARAMS``)
PARAM_KEYS = {
    "completeness": ("fields", "entity_type_filter"),
    "deterministic": (
        "field",
        "field_type",
        "scoring",
        "missing_value_deduction",
        "list_separator",
        "entity_type_field",
        "entity_type_scoring",
        "entity_type_filter",
    ),
    "narrative": ("fields", "ai_prompt_key", "entity_type_filter"),
    "reference_check": (
        "check_type",
        "field",
        "key_field",
        "reference_map",
        "reference_list",
        "contains",
        "list_separator",
        "entity_type_filter",
    ),
}


def _deduction(rule: dict) -> float:
    if rule["type"] == "completeness":
        return float(sum(f.get("deduction") or 0 for f in rule.get("fields") or []))
    if rule.get("deduction") is not None:
        return float(rule["deduction"])
    bands = [b.get("deduction") or 0 for b in rule.get("scoring") or []]
    return float(max(bands or [0]))


def model_values(rule: dict) -> dict:
    """One rule of FMS's file as ``RuleSetting`` keeps it (its id is the code)."""
    keys = PARAM_KEYS[rule["type"]]
    return {
        "label": rule["name"][:80],
        "description": rule.get("description", ""),
        "type": rule["type"],
        "category": rule["category"],
        "group": "core" if rule.get("group") == "core" else "additional",
        "hact_spec": (rule.get("hact_spec") or "")[:120],
        "enabled": bool(rule.get("enabled")),
        "deduction": _deduction(rule),
        "flag_template": (rule.get("flag_template") or "")[:400],
        "params": {key: rule[key] for key in keys if key in rule and rule[key] not in ("", None)},
    }


def categories() -> list[dict]:
    """The score categories of FMS Lebanon's file, with their weights."""
    labels = {"q3_quality": "Q3 quality"}
    return [
        {"key": key, "label": labels.get(key, key.replace("_", " ").capitalize()), "weight": value["weight"]}
        for key, value in RULE_SET["score_categories"].items()
    ]


# The field offices of R19's map, seeded with empty lists (administrators add the addresses)
STAFF_OFFICES = tuple(next(r for r in RULE_SET["rules"] if r["id"] == "R19").get("reference_map", {}).keys())
