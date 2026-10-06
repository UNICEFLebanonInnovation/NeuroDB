"""The AI review of completed eTools action points (FMS §10.3, "AI Adequacy Review"), and the job that
makes it (``manage.py fmm_ap_review``).

**One review** is one Responses API call per completed action point (status completed, closed or
resolved) whose action taken is written: the instructions are the published prompt version's
``rule_prompts["ap_adequacy_review"]`` (seeded from FMS Lebanon's prompt file) followed by NeuroDB's fixed
rules (``prompts.SAFETY_AP_REVIEW``); the input is the action point's description (the issue) and its
action taken, each cut to the score settings' ``ai_text_chars`` and cleaned (``privacy.clean``: names,
e-mail addresses, phone numbers and links removed), then checked once more (``privacy.assert_clean``). Who
the action point is assigned to, its office and its partner are never sent. The answer follows a strict
JSON schema, ``{"verdict": "Adequately addressed" | "Partially addressed" | "Not addressed" |
"Generic/vague", "explanation": string}``. The model, temperature (through the sampling guard),
reasoning effort and output limit are the AI checks' (Score settings); nothing is stored at OpenAI.

**The cache** (``ActionPointReview``, per action point): a point is reviewed again only when its
description or action taken (``input_hash``) or the instructions (``prompt_hash``) change; a review out of
date is never shown and is deleted by the next run or refresh (:func:`forget_stale`). The explanation
kept is cleaned and checked against what was sent, like the AI checks' (``checks.grounded``).

**The job** (:func:`run`, one at a time under its own lock, one ``SyncRun`` "Monitoring insights (action
point review)"): the completed action points not reviewed yet or out of date, most recently completed
first, while the day's budget allows (``FMM_AP_REVIEW_DAILY_TOKEN_CAP``, recorded under the AI use
feature ``fmm_ap_review``, and 80% of ``AI_DAILY_TOKEN_SOFT_CAP`` across every feature); ``limit`` (the
page's batch size: 20, 50, 100 or 200) stops it earlier. It stops when the OpenAI credit runs out (the AI
pauses for 6 hours) or after 3 failed reviews in a row. Its status line: "n reviewed, n skipped, n
errors": reviewed are the verdicts made, skipped the completed points with no action taken (or whose
texts could not be sent safely), errors the calls or answers that failed. Nothing runs while the AI,
Monitoring insights' AI or the review (Action point settings) is switched off.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import F
from django.utils import timezone

from neurodb.assistant import usage
from neurodb.integrations.background import FMM_AP_REVIEW_LOCK_ID
from neurodb.watch.budget import SOFT_SHARE

from .. import action_points, lebanon, privacy
from ..models import ActionPointReview, ActionPointSetting, ScoreSetting
from . import budget, checks, prompts, sampling

logger = logging.getLogger(__name__)

LOCK_ID = FMM_AP_REVIEW_LOCK_ID  # 7_140_435
KEY = lebanon.AP_REVIEW_KEY
EFFORT = "low"
FORMAT_NAME = "fmm_ap_review"
VERDICTS = {label: value for value, label in ActionPointReview.Verdict.choices}
SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "explanation": {"type": "string"},
    },
    "required": ["verdict", "explanation"],
    "additionalProperties": False,
}
BATCH_SIZES = (20, 50, 100, 200)  # the page's "Run AI review" choices
DEFAULT_BATCH = 50
FAILURES_IN_A_ROW = 3
CHUNK = 200


# ------------------------------------------------------------------------------------------ inputs
def instructions() -> str | None:
    """The review's instructions in the published prompt version; None when it holds none."""
    from . import profiles

    version = profiles.published()
    text = ((version.rule_prompts if version else None) or {}).get(KEY)
    return text.strip() if isinstance(text, str) and text.strip() else None


def prompt_hash(text: str) -> str:
    """sha256 of what a review asks: its instructions, NeuroDB's fixed text and the answer's format."""
    blob = json.dumps(
        {"prompt": (text or "").strip(), "fixed": prompts.AP_VERSION, "schema": SCHEMA}, sort_keys=True
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def current_prompt_hash() -> str | None:
    text = instructions()
    return prompt_hash(text) if text is not None else None


def input_hash(description: str, action: str) -> str:
    """sha256 of the two texts as read (before cleaning): a changed description makes the verdict out of
    date."""
    blob = json.dumps(
        {
            "issue": " ".join(str(description or "").split()),
            "action_taken": " ".join(str(action or "").split()),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def payload(description: str, action: str, limit: int, names_: frozenset[str]) -> dict[str, str]:
    """What one review sends: the issue and the action taken, each cut and cleaned. Nothing else of the
    action point (never who it is assigned to)."""
    return {
        "issue": privacy.clean(description, limit, names_)[0],
        "action_taken": privacy.clean(action, limit, names_)[0],
    }


# ------------------------------------------------------------------------------------------ one review
def request(prompt: str, sent: dict[str, str], setting: ScoreSetting) -> dict[str, Any]:
    return {
        "model": checks.model_of(setting),
        "instructions": prompts.compose_ap(prompt, "review"),
        "input": [{"role": "user", "content": json.dumps(sent, ensure_ascii=False, sort_keys=True)}],
        "reasoning": {"effort": EFFORT},
        "max_output_tokens": setting.ai_max_output_tokens,
        "store": False,
        "prompt_cache_key": "neurodb-fmm-ap-review",
        "text": {"format": {"type": "json_schema", "name": FORMAT_NAME, "strict": True, "schema": SCHEMA}},
    }


def estimate(sent: dict[str, str], prompt: str, setting: ScoreSetting) -> int:
    chars = len(json.dumps(sent, ensure_ascii=False)) + len(prompts.compose_ap(prompt, "review"))
    return int(chars / checks.CHARS_PER_TOKEN) + setting.ai_max_output_tokens


def allowed(tokens: int) -> bool:
    """Within the review's daily cap and 80% of the shared soft cap, and not paused."""
    if budget.paused_until() is not None:
        return False
    if usage.today_total(usage.FMM_AP_REVIEW) + tokens > settings.FMM_AP_REVIEW_DAILY_TOKEN_CAP:
        return False
    return usage.today_total() + tokens <= settings.AI_DAILY_TOKEN_SOFT_CAP * SOFT_SHARE


def review(api, prompt: str, sent: dict[str, str], setting: ScoreSetting, names_) -> ActionPointReview | None:
    """One review: the answer as an ``ActionPointReview`` not saved yet (no id or hashes); None when the
    answer could not be read. Raises what the call raised."""
    model = checks.model_of(setting)
    plan = sampling.plan(SimpleNamespace(temperature=setting.ai_temperature, top_p=None), model, EFFORT)
    response, _states = sampling.call(api, request(prompt, sent, setting), plan, model, EFFORT)
    answered = getattr(response, "usage", None)
    usage.record(usage.FMM_AP_REVIEW, model, answered)
    input_tokens, cached, output = usage.split(answered)
    if getattr(response, "status", "") == "incomplete":
        return None
    try:
        raw = json.loads(getattr(response, "output_text", "") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("verdict") not in VERDICTS:
        return None
    sent_text = json.dumps(sent, ensure_ascii=False)
    return ActionPointReview(
        verdict=VERDICTS[raw["verdict"]],
        explanation=checks.grounded(raw.get("explanation"), sent_text, names_),
        model=model[:64],
        input_tokens=min(input_tokens + cached, 2_000_000_000),
        output_tokens=min(output, 2_000_000_000),
        reviewed_at=timezone.now(),
    )


# ------------------------------------------------------------------------------------------ the job
@contextmanager
def locked():
    """The job's lock, without waiting: False when another run holds it."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
        got = bool(cursor.fetchone()[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])


def switched_on() -> tuple[bool, str]:
    """Whether the review may run, and why not."""
    if not (settings.FMM_ENABLED and settings.FMM_AI and settings.AI_ASSISTANT_ENABLED):
        return False, "AI is switched off"
    if not ActionPointSetting.load().ai_review:
        return False, "the AI review of action points is switched off (Action point settings)"
    if instructions() is None:
        return False, "the published prompt version holds no instructions for the review"
    if budget.paused_until() is not None:
        return False, budget.REASONS[budget.PAUSED]
    return True, ""


def completed_points():
    """The completed action points, most recently completed first."""
    from neurodb.datamart.models import ActionPoint

    return ActionPoint.objects.filter(action_points.completed_q()).order_by(
        F("date_of_completion").desc(nulls_last=True), "-datamart_id"
    )


def forget_stale() -> int:
    """Delete the reviews that are out of date (the point's description or action taken changed, the
    instructions changed, or the point is gone or no longer completed). Returns how many."""
    from neurodb.datamart.models import ActionPoint

    prompt = current_prompt_hash()
    if prompt is None:  # no instructions published: nothing is shown, and nothing is lost meanwhile
        return 0
    removed = ActionPointReview.objects.exclude(prompt_hash=prompt).delete()[0]
    kept = dict(ActionPointReview.objects.values_list("datamart_id", "input_hash"))
    wrong: list[int] = []
    seen: set[int] = set()
    rows = ActionPoint.objects.filter(datamart_id__in=list(kept)).values_list(
        "datamart_id", "status", "description", "data"
    )
    for datamart_id, status, description, data in rows.iterator(chunk_size=CHUNK):
        seen.add(datamart_id)
        if not action_points.is_completed(status) or kept[datamart_id] != input_hash(
            description, action_points.action_taken(data)
        ):
            wrong.append(datamart_id)
    wrong += [pk for pk in kept if pk not in seen]
    if wrong:
        removed += ActionPointReview.objects.filter(datamart_id__in=wrong).delete()[0]
    return removed


def run(triggered_by: str = "schedule", limit: int | None = None):
    """The review (see the module's notes), as one ``SyncRun``: ``rows_in`` the completed points reached
    that needed a review, ``rows_written`` the verdicts made, ``rows_failed`` the reviews whose call or
    answer failed. ``limit``: the most reviews to make (the page's batch size)."""
    from neurodb.assistant import agent
    from neurodb.core.models import SyncRun
    from neurodb.integrations.runs import fail, finish_by_counts, new_run

    sync_run = new_run(SyncRun.Job.FMM_AP_REVIEW, "review", triggered_by)
    clock = time.monotonic()
    tally = {"reviewed": 0, "skipped": 0, "errors": 0, "up_to_date": 0, "tokens": 0, "stale_removed": 0}
    try:
        tally["stale_removed"] = forget_stale()
    except Exception as exc:
        return fail(sync_run, exc, duration_ms=int((time.monotonic() - clock) * 1000))
    on, why = switched_on()
    if not on:
        sync_run.finish(SyncRun.Status.SUCCEEDED, skipped_reason=why, **tally)
        return sync_run
    stopped = ""
    try:
        api = agent.client().with_options(timeout=settings.FMM_AP_REVIEW_TIMEOUT_SECONDS, max_retries=1)
        stopped = _review_all(api, sync_run, tally, limit)
    except agent.AssistantUnavailable:
        stopped = "AI is switched off"
    except Exception as exc:
        return fail(sync_run, exc, **tally, duration_ms=int((time.monotonic() - clock) * 1000))
    return finish_by_counts(
        sync_run, **tally, limit=limit, stopped=stopped, duration_ms=int((time.monotonic() - clock) * 1000)
    )


def _review_all(api, sync_run, tally: dict[str, int], limit: int | None) -> str:
    """The reviews due, most recently completed first, until the budget, the pause, ``limit`` or 3
    failures in a row stop them; why they stopped ("" when every review due was made)."""
    setting = ScoreSetting.load()
    chars = setting.ai_text_chars
    names_ = privacy.names()
    prompt = instructions() or ""
    wanted_prompt = prompt_hash(prompt)
    stored = dict(ActionPointReview.objects.values_list("datamart_id", "input_hash"))
    failures = 0
    rows = completed_points().values_list("datamart_id", "description", "data")
    for datamart_id, description, data in rows.iterator(chunk_size=CHUNK):
        action = action_points.action_taken(data)
        if not action or not (description or "").strip():
            tally["skipped"] += 1
            continue
        wanted = input_hash(description, action)
        if stored.get(datamart_id) == wanted:
            tally["up_to_date"] += 1
            continue
        sync_run.rows_in += 1
        if limit is not None and tally["reviewed"] >= limit:
            return f"stopped after {limit} reviews"
        sent = payload(description, action, chars, names_)
        if not sent["issue"].strip() or not sent["action_taken"].strip():
            tally["skipped"] += 1
            continue
        tokens = estimate(sent, prompt, setting)
        if not allowed(tokens):
            return "paused" if budget.paused_until() else "budget: the rest waits for the next run"
        try:
            privacy.assert_clean(sent, names_)
        except privacy.PrivacyRefused as exc:
            logger.error("Action points: an AI review was not sent (%s): %s", datamart_id, exc)
            tally["skipped"] += 1
            continue
        try:
            found = review(api, prompt, sent, setting, names_)
        except Exception as exc:
            if budget.trip(exc):
                return "paused: the OpenAI credit ran out"
            logger.warning("Action points: an AI review failed: %s", type(exc).__name__)
            found = None
        if found is None:
            sync_run.rows_failed += 1
            tally["errors"] += 1
            failures += 1
            if failures >= FAILURES_IN_A_ROW:
                return f"{FAILURES_IN_A_ROW} failed reviews in a row"
            continue
        failures = 0
        ActionPointReview.objects.update_or_create(
            datamart_id=datamart_id,
            defaults={
                "input_hash": wanted,
                "prompt_hash": wanted_prompt,
                "verdict": found.verdict,
                "explanation": found.explanation,
                "model": found.model,
                "input_tokens": found.input_tokens,
                "output_tokens": found.output_tokens,
                "reviewed_at": found.reviewed_at,
            },
        )
        stored[datamart_id] = wanted
        sync_run.rows_written += 1
        tally["reviewed"] += 1
        tally["tokens"] += found.input_tokens + found.output_tokens
    return ""


def last_run() -> dict[str, Any] | None:
    """The page's status line: the last run's reviewed, skipped and errors, when and how it ended."""
    from neurodb.core.models import SyncRun

    found = SyncRun.objects.filter(job=SyncRun.Job.FMM_AP_REVIEW).order_by("-started_at", "-pk").first()
    if found is None:
        return None
    details = found.details or {}
    return {
        "run": found,
        "reviewed": details.get("reviewed", found.rows_written),
        "skipped": details.get("skipped", 0),
        "errors": details.get("errors", found.rows_failed),
        "stopped": details.get("stopped") or details.get("skipped_reason") or "",
        "running": found.status == SyncRun.Status.RUNNING,
    }


def counts() -> dict[str, int]:
    """The current verdicts by verdict value (the up-to-date ones of the published instructions)."""
    from django.db.models import Count

    prompt = current_prompt_hash()
    if prompt is None:
        return {}
    rows = (
        ActionPointReview.objects.filter(prompt_hash=prompt)
        .values("verdict")
        .annotate(n=Count("pk"))
        .order_by()
    )
    return {row["verdict"]: row["n"] for row in rows}
