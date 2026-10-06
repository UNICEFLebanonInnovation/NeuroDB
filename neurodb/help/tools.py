"""The Help assistant's own look-ups, offered to it alone (``RunOptions.registry``: Ask NeuroDB never sees
them).

- ``search_help`` / ``read_help``: the help guide (:mod:`.guide`), searched in memory;
- ``list_quality_rules`` / ``get_quality_rule``: the live quality rules of Monitoring insights, the score
  settings (categories, bands, scored statuses) and urgency's weights, window and thresholds;
- ``explain_visit_score``: why one visit scored what it scored (its rule results, deductions per
  category and urgency parts), with the same access as the visit page;
- ``list_jobs``: the scheduled jobs, what they do, when they run and how their last run went.

**What never comes out.** No person: not who ran a job, who changed a rule, a visit's team, lead or
monitors, nor who an action point is assigned to. No text people wrote: a visit's narratives and
answers, an AI check's explanation (it may quote a narrative) or the flag of a "text contains" check (it
writes the text it read); the other flags are NeuroDB's own wording, cleaned once more
(``fmm.privacy.clean``). No reference list a rule compares with (R19's lists hold e-mail addresses): only
how many entries it has. No error text of a run (it may name a server). A rule's AI instructions only for
an Administrator, for that one rule; the admin page a setting lives on only for an Administrator.

The person asking is bound for each tool call (:func:`bind`, through ``RunOptions.tool_context``),
whatever thread runs the answer.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.urls import NoReverseMatch, reverse

from neurodb.assistant.tools import ToolInputError, _schema

from . import guide

READ_CHARS = 6000  # characters of a section read_help returns
PARAM_TEXT = 300  # characters of one parameter value shown
_asker: ContextVar[Any] = ContextVar("help_asker", default=None)


@contextmanager
def bind(user) -> Iterator[None]:
    """The person asking, for the look-ups made inside the block (what an Administrator may see)."""
    token = _asker.set(user)
    try:
        yield
    finally:
        _asker.reset(token)


def _is_admin() -> bool:
    from neurodb.fmm.access import is_admin

    user = _asker.get()
    return bool(user is not None and getattr(user, "is_authenticated", False) and is_admin(user))


def _admin_url(name: str, *args) -> str | None:
    """The admin page of a setting, for an Administrator only."""
    if not _is_admin():
        return None
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return None


def _number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


# ------------------------------------------------------------------------------------------ the guide
def search_help(query: str) -> dict[str, Any]:
    found = guide.search(query, limit=5)
    return {
        "query": query,
        "sections": [
            {
                "page": s.page,
                "page_title": s.page_title,
                "heading": s.heading,
                "slug": s.slug,
                "url": s.url,
                "text": s.plain(guide.SECTION_CHARS),
            }
            for s in found
        ],
        "note": "Cite the sections you use by their url. When none answers the question, say the guide "
        "does not cover it."
        if found
        else "The guide has no section about this: say so and do not guess.",
    }


def read_help(page: str, slug: str | None = None) -> dict[str, Any]:
    found = guide.page(page)
    if found is None:
        raise ToolInputError(f"No guide page '{page}'. Pages: {', '.join(guide.ORDER)}.")
    section = guide.section(page, slug or "")
    if section is None and slug:
        raise ToolInputError(
            f"No section '{slug}' on page '{page}'. Sections: "
            + ", ".join(s.slug for s in found.sections if s.slug)
        )
    return {
        "page": found.key,
        "page_title": found.title,
        "heading": section.heading if section else found.title,
        "slug": section.slug if section else "",
        "url": section.url if section else found.url,
        "text": section.plain(READ_CHARS) if section else "",
        "sections": [{"heading": s.heading, "slug": s.slug, "url": s.url} for s in found.sections if s.slug],
    }


# ------------------------------------------------------------------------------------------ rules
def _safe_params(params: dict[str, Any]) -> dict[str, Any]:
    """A rule's parameters as a person may read them: the reference lists and maps as their size only,
    every other value as it is (cut when long)."""
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if key in ("reference_map", "reference_list", "contains"):
            out[f"{key}_entries"] = len(value) if hasattr(value, "__len__") else 0
        elif key == "ai_prompt_key":
            continue  # the instructions are given (to Administrators) by get_quality_rule
        elif isinstance(value, str) and len(value) > PARAM_TEXT:
            out[key] = value[:PARAM_TEXT] + " …"
        else:
            out[key] = value
    return out


def _settings():
    from neurodb.fmm.models import ScoreSetting

    return ScoreSetting.objects.filter(pk=1).first() or ScoreSetting()


def _rule_entry(rule, labels: dict[str, str], ai_on: bool) -> dict[str, Any]:
    from neurodb.fmm import rules

    ai = rule.type == "narrative"
    return {
        "id": rule.code,
        "name": rule.label,
        "type": rules.TYPE_LABELS.get(rule.type, rule.type),
        "category": labels.get(rule.category, rule.category),
        "group": rule.get_group_display(),
        "hact_rule": rule.hact_spec or None,
        "enabled": rule.enabled,
        "ai_check": ai,
        "ai_checks_switched_on": ai_on if ai else None,
        "deduction": _number(rule.deduction),
        "max_points": _number(rules.max_deduction(rule)),
        "flag_only": rules.max_deduction(rule) == 0,
    }


def _score_settings() -> dict[str, Any]:
    from neurodb.fmm import score

    s = _settings()
    return {
        "score": {
            "formula": "100 less the deductions of the rules that fired; each category's deductions count "
            "at most its weight; never below 0; rounded half up to one decimal",
            "categories": [
                {"category": c.get("label") or c.get("key"), "weight": c.get("weight")}
                for c in (s.categories or [])
            ],
            "bands": {
                "high_from": s.band_high,
                "medium_from": s.band_medium,
                "low": f"below {s.band_medium}",
            },
            "high_flag_count": s.high_flag_count,
            "scored_statuses": [k.replace("_", " ") for k in sorted(score.scored_statuses_of(s))],
            "ai_checks_switched_on": bool(s.ai_checks),
        },
        "urgency": {
            "formula": "quality_gap x (100 - quality score) + recency x recency part + red_flags x flags "
            "part; rounded half up, kept within 0-100; scored visits only",
            "weights": score.weights_of(s),
            "recency_days": s.recency_days,
            "recency_part": f"100 on the day the visit ended, falling in a straight line to 0 at "
            f"{s.recency_days} days after it",
            "flags_part": f"{score.FLAG_POINTS} per rule that fired, at most 100",
            "red_from": s.urgency_red,
            "amber_from": s.urgency_amber,
        },
        "follow_up_days": s.follow_up_days,
        "report_late_days": s.report_late_days,
    }


def list_quality_rules() -> dict[str, Any]:
    from neurodb.fmm import rules
    from neurodb.fmm.models import RuleSetting
    from neurodb.fmm.score import category_labels

    s = _settings()
    labels = category_labels(s)
    found = sorted(RuleSetting.objects.all(), key=lambda r: rules.code_order(r.code))
    return {
        "rules": [_rule_entry(r, labels, bool(s.ai_checks)) for r in found],
        **_score_settings(),
        "admin_pages": {
            "quality_rules": _admin_url("admin:fmm_rulesetting_changelist"),
            "score_settings": _admin_url("admin:fmm_scoresetting_changelist"),
        }
        if _is_admin()
        else None,
        "guide": "/help/monitoring-insights/#quality-rules",
    }


def get_quality_rule(rule_id: str) -> dict[str, Any]:
    from neurodb.fmm import rules
    from neurodb.fmm.models import RuleSetting
    from neurodb.fmm.score import category_labels

    code = str(rule_id or "").strip().upper()
    if code.isdigit():
        code = f"R{code}"
    rule = RuleSetting.objects.filter(code=code).first()
    if rule is None:
        known = ", ".join(sorted(RuleSetting.objects.values_list("code", flat=True), key=rules.code_order))
        raise ToolInputError(f"No rule {rule_id}. Rules: {known}.")
    s = _settings()
    out = _rule_entry(rule, category_labels(s), bool(s.ai_checks))
    out.update(
        {
            "what_it_checks": rule.description,
            "flag_wording": rule.flag_template,
            "parameters": _safe_params(rule.params),
            "entity_types": rules.param(rule, "entity_type_filter") or None,
            "admin_page": _admin_url("admin:fmm_rulesetting_change", rule.code),
            "guide": "/help/monitoring-insights/#quality-rules",
        }
    )
    if rule.type == "narrative":
        out["ai_instructions"] = _prompt_text(rule) if _is_admin() else None
        out["ai_instructions_note"] = (
            "The rule's AI instructions, shown because the person asking is an Administrator."
            if _is_admin()
            else "The rule's AI instructions are shown to Administrators only."
        )
    return out


def _prompt_text(rule) -> str | None:
    """The AI instructions of a narrative rule in the published prompt version."""
    from neurodb.fmm import rules
    from neurodb.fmm.ai import profiles

    version = profiles.published()
    key = rules.param(rule, "ai_prompt_key")
    if version is None or not key:
        return None
    text = (version.rule_prompts or {}).get(key)
    return str(text)[:READ_CHARS] if text else None


# ------------------------------------------------------------------------------------------ one visit
AI_FLAGGED = "The AI check found the report not coherent on this rule (its explanation is on the visit page)."
RESULT_WORDS = {
    "pass": "passed",
    "fail": "flagged",
    "na": "not available (the data it needs is missing)",
    "nap": "does not apply",
    "off": "switched off",
    "pending": "AI check pending",
}


def explain_visit_score(visit: str) -> dict[str, Any]:
    from neurodb.fmm import metrics, privacy, rules
    from neurodb.fmm.action_points import find_visit
    from neurodb.fmm.models import RuleSetting, VisitRuleResult
    from neurodb.fmm.score import categories_of, category_labels

    if not getattr(settings, "FMM_ENABLED", False):
        return {"error": "Monitoring insights is switched off, so no visit can be explained."}
    found = find_visit(visit)
    if found is None:
        raise ToolInputError(
            f"No visit '{visit}'. Give the visit's eTools activity id (e.g. 1722), its key or its reference."
        )
    s = _settings()
    labels = category_labels(s)
    weights = categories_of(s)
    settings_by_code = {r.code: r for r in RuleSetting.objects.all()}
    quoting = {
        code for code, r in settings_by_code.items() if rules.param(r, "check_type") == "string_contains"
    }
    names = None
    results = []
    for row in sorted(VisitRuleResult.objects.filter(visit=found), key=lambda r: rules.code_order(r.rule)):
        rule = settings_by_code.get(row.rule)
        ai = rule is not None and rule.type == "narrative"
        evaluated = row.status in ("pass", "fail")
        lost = (Decimal(row.max_points or 0) - Decimal(row.points or 0)) if evaluated else Decimal(0)
        detail = None
        if row.detail and not ai and not (row.rule in quoting and row.status == "fail"):
            if names is None:
                from neurodb.watch import people

                names = people.known_names()
            detail = privacy.clean(row.detail, 300, names)[0]
        elif ai and row.status == "fail":
            detail = AI_FLAGGED
        results.append(
            {
                "rule": row.rule,
                "name": rule.label if rule else row.rule,
                "category": labels.get(rule.category, rule.category) if rule else None,
                "result": RESULT_WORDS.get(row.status, row.status),
                "points_kept": _number(row.points) if evaluated else None,
                "max_points": _number(row.max_points),
                "points_lost": _number(max(lost, Decimal(0))),
                "ai_check": ai,
                "detail": detail,
            }
        )
    deductions = [
        {
            "category": labels.get(key, key),
            "deducted": value,
            "weight": _number(weights.get(key)) if key in weights else None,
        }
        for key, value in (found.category_deductions or {}).items()
    ]
    limits = metrics.thresholds(s)
    return {
        "visit": found.label,
        "key": found.key,
        "url": found.get_absolute_url(),
        "end_date": found.end_date.isoformat() if found.end_date else None,
        "status": found.status or found.status_group,
        "status_group": found.status_group,
        "rating": found.rating,
        "scored": found.quality_score is not None,
        "quality_score": _number(found.quality_score),
        "band": found.score_band or None,
        "provisional_score": _number(found.provisional_score),
        "ai_checks_pending": found.ai_pending,
        "not_scored_reason": found.not_scored_reason or None,
        "how": "100 less the deductions of the rules that fired, each category at most its weight",
        "deductions_by_category": deductions,
        "flags": found.flag_count,
        "rule_results": results,
        "urgency": found.urgency,
        "urgency_band": found.urgency_band or None,
        "urgency_parts": found.urgency_parts or {},
        "urgency_thresholds": {"red_from": limits["red"], "amber_from": limits["amber"]},
        "rules_version": found.rules_version,
        "guide": "/help/monitoring-insights/#the-quality-score",
    }


# ------------------------------------------------------------------------------------------ jobs
def list_jobs() -> dict[str, Any]:
    from neurodb.core.admin_jobs import BACKGROUND_JOBS
    from neurodb.core.cron import describe
    from neurodb.core.jobs import COMMANDS
    from neurodb.core.models import ScheduledJob, SyncRun

    descriptions = {spec.job: str(spec.description) for spec in BACKGROUND_JOBS}
    jobs = []
    for job in ScheduledJob.objects.order_by("next_run_at", "key"):
        command = COMMANDS.get(job.command)
        last = None
        if command is not None and command.sync_job:
            run = SyncRun.objects.filter(job=command.sync_job).order_by("-started_at").first()
            if run is not None:
                last = {
                    "status": run.get_status_display(),
                    "started": run.started_at.isoformat(timespec="minutes"),
                    "finished": run.finished_at.isoformat(timespec="minutes") if run.finished_at else None,
                }
        jobs.append(
            {
                "job": job.key,
                "does": str(command.label) if command else job.command,
                "details": descriptions.get(command.sync_job) if command and command.sync_job else None,
                "schedule": f"{describe(job.schedule)} (Beirut time)",
                "switched_on": job.enabled,
                "next_run": job.next_run_at.isoformat(timespec="minutes") if job.next_run_at else None,
                "last_run": last,
            }
        )
    return {
        "jobs": jobs,
        "note": f"Every sync and job run is listed on the Data health page "
        f"({reverse('reports:data_health')}). Monitoring insights is also refreshed after every eTools "
        "Datamart sync, and the knowledge hub after every sync.",
        "admin_page": _admin_url("admin:core_scheduledjob_changelist"),
        "guide": "/help/overview/#when-the-data-is-refreshed",
    }


_PAGES = {"type": "string", "enum": list(guide.ORDER)}

HELP_TOOLS: dict[str, tuple[Any, str, dict, str]] = {
    # name: (function, description, input schema, progress label shown to the user)
    "search_help": (
        search_help,
        "Search NeuroDB's help guide (how the pages, figures, rules, AI features, exports and limits work). "
        "Returns the best matching sections with their page, heading, slug, url and text. Give words to "
        "look for, not a whole question. Start here for every question.",
        _schema({"query": {"type": "string", "description": "Words to look for."}}, ["query"]),
        "Searching the help guide",
    ),
    "read_help": (
        read_help,
        "Read one section of the help guide in full (by page and slug), or list a page's sections (page "
        "only).",
        _schema({"page": _PAGES, "slug": {"type": "string", "description": "The section's slug."}}, ["page"]),
        "Reading the help guide",
    ),
    "list_quality_rules": (
        list_quality_rules,
        "The live quality rules of Monitoring insights (id, name, type, category, on or off, AI check or "
        "not, deduction), the score settings (categories and weights, bands, scored statuses) and "
        "urgency's weights, recency window and thresholds, as they are set now.",
        _schema({}),
        "Reading the quality rules",
    ),
    "get_quality_rule": (
        get_quality_rule,
        "One quality rule as it is set now: what it checks, its flag wording, its parameters, its "
        "deduction and category.",
        _schema({"rule_id": {"type": "string", "description": "The rule id, e.g. R7."}}, ["rule_id"]),
        "Reading a quality rule",
    ),
    "explain_visit_score": (
        explain_visit_score,
        "Why one field monitoring visit scored what it scored: its quality score and band (or why it is "
        "not scored), the deductions per score category, each rule's result and points lost, and its "
        "urgency with its parts. Give the visit's eTools activity id (e.g. 1722), key or reference.",
        _schema({"visit": {"type": "string"}}, ["visit"]),
        "Explaining a visit's score",
    ),
    "list_jobs": (
        list_jobs,
        "The scheduled jobs that refresh NeuroDB's data and pages: what each does, its schedule (Beirut "
        "time), whether it is switched on, its next run and how its last run went.",
        _schema({}),
        "Reading the scheduled jobs",
    ),
}
