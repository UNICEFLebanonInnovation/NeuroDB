"""The AI checks of the narrative quality rules (FMS's AI rules: R3, R5, R6, R7, R8, R32...), visit by
visit, and the nightly job that makes them (``manage.py fmm_ai_checks``).

**One check** is one Responses API call per visit and rule: the instructions are the rule's prompt (the
published prompt version's ``rule_prompts`` under the rule's ``ai_prompt_key``, seeded from FMS Lebanon's
prompt file) followed by NeuroDB's fixed rules (``prompts.SAFETY_CHECKS``); the input is the rule's
fields for the visit (:func:`payload`): its finding rows (entity, type, rating, narrative, Q1, Q2 and Q3
answers, as the rule lists them) and its own fields (visit goals, objective, action points with their due
dates; who an action point is assigned to only as a count; the team and the person responsible never),
each text cut to the settings' ``ai_text_chars`` and cleaned (``privacy.clean``: names, e-mail
addresses, phone numbers and links removed), then checked once more (``privacy.assert_clean``). The
answer follows a strict JSON schema, ``{"is_coherent": boolean, "detail": string}``. The model is the
score settings' (``ai_model``), else ``AI_ASSISTANT_MODEL``; temperature 0.3 through the sampling guard
(``sampling``: not sent when the model refuses it), low reasoning effort, ``ai_max_output_tokens``
(2,000, the reasoning included), nothing stored at OpenAI, and one prompt cache key per rule.

**The cache** (``VisitAICheck``, per visit key and rule): a check is made again only when the visit's
inputs change (``input_hash``: the rule's fields as read, before cleaning) or the rule's prompt does
(``prompt_hash``: its instructions, fields and NeuroDB's fixed text). The scoring reads only fresh
answers (:func:`fresh`); a visit with a check missing or out of date is provisional
(``score.score_visit``). The explanation kept is cleaned and checked against what was sent: one that
names a figure the report does not hold, or a word NeuroDB never writes, is left out (the verdict stays).

**The job** (:func:`run`, one at a time under its own lock, one ``SyncRun`` "Monitoring insights (AI
checks)"): the scored visits, newest first; for each, the checks missing or out of date, while the day's
budget allows (the checks' own ``FMM_RULES_DAILY_TOKEN_CAP``, recorded under the AI use feature
``fmm_rules``, and 80% of ``AI_DAILY_TOKEN_SOFT_CAP`` across every feature); what is left waits for the
next night (the back-fill). It stops when the OpenAI credit runs out (the AI pauses for 6 hours, as for
the briefs) or after 3 failed checks in a row. When it checked anything it recomputes the scores
(a scores-only refresh). Nothing runs while the AI, Monitoring insights' AI or the AI checks (Score
settings) are switched off.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import F
from django.utils import timezone

from neurodb.assistant import usage
from neurodb.integrations.background import FMM_AI_CHECKS_LOCK_ID
from neurodb.watch.budget import SOFT_SHARE

from .. import fields, parse, privacy, rules
from ..models import QuestionAnswer, Visit, VisitActionPoint, VisitAICheck, VisitEntity
from . import budget, prompts, sampling

logger = logging.getLogger(__name__)

LOCK_ID = FMM_AI_CHECKS_LOCK_ID  # 7_140_434
EFFORT = "low"
FORMAT_NAME = "fmm_rule_check"
SCHEMA = {
    "type": "object",
    "properties": {"is_coherent": {"type": "boolean"}, "detail": {"type": "string"}},
    "required": ["is_coherent", "detail"],
    "additionalProperties": False,
}
DETAIL_CHARS = 400
ACTION_POINTS = 10  # action points sent per visit, at most
BATCH = 50  # visits read at once by the job
FAILURES_IN_A_ROW = 3
CHARS_PER_TOKEN = 3.5
ROW_TEXTS = ("q1_answer", "q2_answer", "q3_answer", "visit_goals", "objective")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_NEVER = re.compile(r"\b(items?|agents?|detectors?|receipts?|llms?)\b", re.IGNORECASE)


# ------------------------------------------------------------------------------------------ inputs
@dataclass
class Row:
    """One finding row of a visit, as the checks read it."""

    finding_id: int | None
    kind: str
    entity: str
    entity_type: str
    rating_raw: str
    partner_id: int | None


@dataclass
class Item:
    """One visit to read the inputs of: its rows (in the order the answers point into), its answered Q1,
    Q2 and Q3 answers ``(record id, role, applies to, row index, partner)``, its action points and the
    values of its own derived fields."""

    key: str
    label: str
    rows: list[Row]
    answers: list[tuple[int, str, str, int | None, int | None]]
    action_points: list[int]
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class Inputs:
    """The texts of one visit's report, as written (never stored): per row and for the visit."""

    label: str
    rows: list[dict[str, Any]]
    visit: dict[str, Any]
    values: dict[str, Any]


def item_of(
    visit: Visit, entities: Sequence[Any], answers: Iterable[tuple], action_points: Iterable[int]
) -> Item:
    """An :class:`Item` from a visit, its entity rows (``VisitEntity``, in order), its answered answers
    and the ids of its action points."""
    return Item(
        key=visit.key,
        label=visit.label,
        rows=[
            Row(
                e.finding_id,
                e.kind,
                e.entity or "",
                e.entity_type_raw or rules.KIND_NAMES.get(e.kind, ""),
                e.rating_raw or "",
                e.partner_id,
            )
            for e in entities
        ],
        answers=[a for a in answers if a[1] in ("q1", "q2", "q3")],
        action_points=list(action_points),
        values={
            "fmq_answered_pct": None if visit.fmq_answered_pct is None else float(visit.fmq_answered_pct),
            "fmq_answered_categories": visit.fmq_answered_categories,
            "method_count": visit.method_count,
            "red_flag_count": visit.red_flag_count,
            "attachments_count": visit.attachments_count,
            "attachment_count": visit.attachments_count,
            "sections_names": "; ".join(visit.section_names or []),
            "field_offices": "; ".join(visit.offices or []),
            "location_pcode": visit.place_pcode or "",
            "monitoring_modality": visit.modality or "",
            "programme_areas": "; ".join(visit.programme_areas or []),
        },
    )


def _answer_texts(document_ids: Sequence[int]) -> dict[int, str]:
    """{record id: the answer as written, its label first, with its summary} of checklist answers."""
    from neurodb.datamart.models import DatamartDocument

    keys = {name: fields.key_for("fm_questions", name) for name in ("answer", "answer_label", "summary")}
    keys = {name: key for name, key in keys.items() if key and not privacy.person_like(key)}
    if not document_ids or not keys:
        return {}
    out: dict[int, str] = {}
    rows = DatamartDocument.objects.filter(dataset="fm_questions", pk__in=list(document_ids)).values_list(
        "pk", "data"
    )
    for pk, data in rows.iterator(chunk_size=2000):
        if not isinstance(data, dict):
            continue
        read = {name: parse.value(data, key) for name, key in keys.items()}
        text = read.get("answer_label") or read.get("answer") or ""
        if read.get("summary"):
            text = f"{text} — {read['summary']}" if text else read["summary"]
        if text:
            out[pk] = str(text)
    return out


def collect(items: Sequence[Item]) -> dict[str, Inputs]:
    """The texts of each visit's report (its narratives, its rows' Q1, Q2 and Q3 answers, its visit
    goals and objective, its action points), read from their sources for ``items``. Kept in memory
    only."""
    from neurodb.datamart.models import ActionPoint, MonitoringFinding

    finding_ids = [row.finding_id for item in items for row in item.rows if row.finding_id]
    narratives = dict(
        MonitoringFinding.objects.filter(pk__in=finding_ids).values_list("pk", "narrative_finding")
    )
    written = parse.row_texts(finding_ids, ROW_TEXTS)
    answers = _answer_texts([a[0] for item in items for a in item.answers])
    ap_ids = [pk for item in items for pk in item.action_points]
    action_points = {
        pk: (description or "", due, bool((assigned or "").strip()))
        for pk, description, due, assigned in ActionPoint.objects.filter(pk__in=ap_ids).values_list(
            "pk", "description", "due_date", "assigned_to_name"
        )
    }
    out: dict[str, Inputs] = {}
    for item in items:
        by_entity: dict[tuple[int, str], list[str]] = defaultdict(list)
        by_partner: dict[tuple[int, str], list[str]] = defaultdict(list)
        by_visit: dict[str, list[str]] = defaultdict(list)
        for document, role, applies_to, index, partner in sorted(item.answers, key=lambda a: a[0]):
            text = answers.get(document)
            if not text:
                continue
            if applies_to == "entity" and index is not None:
                by_entity[(index, role)].append(text)
            elif applies_to == "partner" and partner:
                by_partner[(partner, role)].append(text)
            else:
                by_visit[role].append(text)
        rows = []
        goals: list[str] = []
        objectives: list[str] = []
        for index, row in enumerate(item.rows):
            own = written.get(row.finding_id, {}) if row.finding_id else {}
            entry = {
                "entity": row.entity,
                "entity_type": row.entity_type,
                "overall_finding_rating": row.rating_raw,
                "narrative_finding": narratives.get(row.finding_id) or "",
            }
            for role in ("q1", "q2", "q3"):
                found = (
                    [own[f"{role}_answer"]]
                    if own.get(f"{role}_answer")
                    else by_entity.get((index, role))
                    or by_partner.get((row.partner_id, role))
                    or by_visit.get(role)
                )
                entry[f"hact_{role}_answer"] = " | ".join(dict.fromkeys(found or []))
            rows.append(entry)
            goals += [own["visit_goals"]] if own.get("visit_goals") else []
            objectives += [own["objective"]] if own.get("objective") else []
        points = [action_points[pk] for pk in sorted(item.action_points) if pk in action_points]
        visit = {
            "visit_goals": " | ".join(dict.fromkeys(goals)),
            "objective": " | ".join(dict.fromkeys(objectives)),
            "action_points_text": [description for description, _due, _a in points if description.strip()],
            "action_points_due_dates": sorted({due.isoformat() for _d, due, _a in points if due}),
            "action_points_count": len(points),
            "action_points_assigned": sum(1 for _d, _due, assigned in points if assigned),
        }
        out[item.key] = Inputs(item.label, rows, visit, item.values)
    return out


def _cut(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def payload(inputs: Inputs, rule, limit: int) -> dict[str, Any]:
    """What one check of ``rule`` sends about a visit, as written (cut to ``limit`` characters per text;
    cleaned by :func:`cleaned` before it is sent): its rows with the row fields the rule lists, then the
    visit's own fields it lists. The team and the person responsible are never sent; who action points
    are assigned to only as a count."""
    wanted = list(dict.fromkeys(rules.param(rule, "fields") or []))
    row_fields = [f for f in wanted if f in rules.ROW_COLUMNS and f not in ("entity", "entity_type")]
    out: dict[str, Any] = {
        "visit": inputs.label,
        "entities": [
            {
                "entity": _cut(row["entity"], 300),
                "entity_type": row["entity_type"],
                **{f: _cut(row.get(f), limit) for f in row_fields},
            }
            for row in inputs.rows
        ],
    }
    visit = inputs.visit
    for name in wanted:
        if name in rules.ROW_COLUMNS or name in ("team_members", "person_responsible_email"):
            continue
        if name == "action_points_assigned_to":
            out[name] = (
                f"{visit['action_points_assigned']} of {visit['action_points_count']} action points assigned"
            )
        elif name == "action_points_text":
            texts = visit["action_points_text"][:ACTION_POINTS]
            out[name] = [_cut(text, min(limit, 600)) for text in texts]
        elif name == "action_points_due_dates":
            out[name] = visit["action_points_due_dates"][:ACTION_POINTS]
        elif name == "action_points_count":
            out[name] = visit["action_points_count"]
        elif name in ("visit_goals", "objective"):
            out[name] = _cut(visit[name], limit)
        else:
            out[name] = inputs.values.get(name)
    return out


def input_hash(sent: Mapping[str, Any]) -> str:
    """sha256 of a check's input as read (before cleaning), so a new list of names to remove does not
    make every visit be checked again."""
    blob = json.dumps(sent, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def prompt_hash(rule, text: str) -> str:
    """sha256 of what a check of ``rule`` asks: its instructions, the fields it reads, NeuroDB's fixed
    text and the answer's format."""
    blob = json.dumps(
        {
            "prompt": (text or "").strip(),
            "fields": list(rules.param(rule, "fields") or []),
            "fixed": prompts.CHECKS_VERSION,
            "schema": SCHEMA,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _clean(value: Any, names_: frozenset[str], limit: int) -> Any:
    if isinstance(value, str):
        return privacy.clean(value, limit, names_)[0]
    if isinstance(value, list):
        return [_clean(v, names_, limit) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v, names_, limit) for k, v in value.items()}
    return value


def cleaned(sent: Mapping[str, Any], names_: frozenset[str], limit: int) -> dict[str, Any]:
    """A check's input as it is sent: every text cleaned of names, e-mail addresses, phone numbers and
    links (``privacy.clean``)."""
    return _clean(dict(sent), names_, max(limit, 300))


def fresh(items: Sequence[Item], ai_rules: Sequence[Any], book) -> dict[str, dict[str, tuple[bool, str]]]:
    """{visit key: {rule: (passed, detail)}} of the checks kept whose visit inputs and rule prompt are
    still those they were made with; a check missing or out of date is left out (pending)."""
    if not items or not ai_rules:
        return {}
    limit = book.setting.ai_text_chars
    inputs = collect(items)
    hashes = {
        rule.code: prompt_hash(rule, book.prompts[rules.param(rule, "ai_prompt_key")]) for rule in ai_rules
    }
    kept = VisitAICheck.objects.filter(visit_key__in=list(inputs), rule__in=list(hashes)).values_list(
        "visit_key", "rule", "input_hash", "prompt_hash", "passed", "detail"
    )
    stored = {(key, rule): (inp, prm, passed, detail) for key, rule, inp, prm, passed, detail in kept}
    out: dict[str, dict[str, tuple[bool, str]]] = defaultdict(dict)
    for key, visit_inputs in inputs.items():
        for rule in ai_rules:
            found = stored.get((key, rule.code))
            if found is None or found[1] != hashes[rule.code]:
                continue
            if found[0] == input_hash(payload(visit_inputs, rule, limit)):
                out[key][rule.code] = (found[2], found[3])
    return out


# ------------------------------------------------------------------------------------------ one check
def model_of(setting) -> str:
    return (setting.ai_model or "").strip() or settings.AI_ASSISTANT_MODEL


def request(rule, prompt: str, sent: Mapping[str, Any], setting) -> dict[str, Any]:
    """The parameters of one check (temperature is added by the sampling guard)."""
    return {
        "model": model_of(setting),
        "instructions": prompts.compose_check(prompt),
        "input": [{"role": "user", "content": json.dumps(sent, ensure_ascii=False, sort_keys=True)}],
        "reasoning": {"effort": EFFORT},
        "max_output_tokens": setting.ai_max_output_tokens,
        "store": False,
        "prompt_cache_key": f"neurodb-fmm-check-{rule.code}",
        "text": {"format": {"type": "json_schema", "name": FORMAT_NAME, "strict": True, "schema": SCHEMA}},
    }


def grounded(detail: Any, sent_text: str, names_: frozenset[str]) -> str:
    """The AI's explanation as it is kept: cleaned (``privacy.clean``); left out when it names a figure
    the input does not hold or writes a word NeuroDB never shows."""
    text, _placeholders = privacy.clean(detail if isinstance(detail, str) else "", DETAIL_CHARS, names_)
    if not text:
        return ""
    if _NEVER.search(text):
        return ""
    if any(number not in sent_text for number in _NUMBER.findall(text)):
        return ""
    return text


def estimate(sent: Mapping[str, Any], prompt: str, setting) -> int:
    """The tokens one check may use: its input and instructions (about 3.5 characters a token) and its
    output limit (reasoning included)."""
    chars = len(json.dumps(sent, ensure_ascii=False)) + len(prompts.compose_check(prompt))
    return int(chars / CHARS_PER_TOKEN) + setting.ai_max_output_tokens


def allowed(tokens: int) -> bool:
    """Within the checks' daily cap and 80% of the shared soft cap, and not paused."""
    if budget.paused_until() is not None:
        return False
    if usage.today_total(usage.FMM_RULES) + tokens > settings.FMM_RULES_DAILY_TOKEN_CAP:
        return False
    return usage.today_total() + tokens <= settings.AI_DAILY_TOKEN_SOFT_CAP * SOFT_SHARE


class Stop(Exception):
    """The job stops: the AI is not available, paused or out of budget."""


def check(
    api, rule, prompt: str, sent: dict[str, Any], setting, names_: frozenset[str]
) -> VisitAICheck | None:
    """One check (see the module's notes): the answer as a ``VisitAICheck`` not saved yet; None when the
    answer could not be read (cut off, refused or not the format). Raises what the call raised."""
    model = model_of(setting)
    plan = sampling.plan(SimpleNamespace(temperature=setting.ai_temperature, top_p=None), model, EFFORT)
    response, _states = sampling.call(api, request(rule, prompt, sent, setting), plan, model, EFFORT)
    answered = getattr(response, "usage", None)
    usage.record(usage.FMM_RULES, model, answered)
    input_tokens, cached, output = usage.split(answered)
    if getattr(response, "status", "") == "incomplete":
        return None
    try:
        raw = json.loads(getattr(response, "output_text", "") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("is_coherent"), bool):
        return None
    sent_text = json.dumps(sent, ensure_ascii=False)
    return VisitAICheck(
        rule=rule.code,
        model=model[:64],
        passed=raw["is_coherent"],
        detail=grounded(raw.get("detail"), sent_text, names_),
        input_tokens=min(input_tokens + cached, 2_000_000_000),
        output_tokens=min(output, 2_000_000_000),
        checked_at=timezone.now(),
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


def switched_on(book) -> tuple[bool, str]:
    """Whether the checks may run, and why not."""
    if not book.ai_enabled:
        return False, "AI is switched off"
    if not book.setting.ai_checks:
        return False, "AI checks are switched off (Score settings)"
    if not book.ai_rules():
        return False, "no AI check is on with instructions in the published prompt version"
    if budget.paused_until() is not None:
        return False, budget.REASONS[budget.PAUSED]
    return True, ""


def _items(visits: list[Visit]) -> list[Item]:
    by_pk = {visit.pk: visit for visit in visits}
    entities: dict[int, list[VisitEntity]] = defaultdict(list)
    for entity in VisitEntity.objects.filter(visit_id__in=list(by_pk)).order_by(
        "visit_id", "datamart_id", "pk"
    ):
        entities[entity.visit_id].append(entity)
    index = {e.pk: i for rows in entities.values() for i, e in enumerate(rows)}
    answers: dict[int, list[tuple]] = defaultdict(list)
    for document, visit_id, role, applies_to, entity_id, partner in QuestionAnswer.objects.filter(
        visit_id__in=list(by_pk), answered=True, role__in=("q1", "q2", "q3")
    ).values_list("document_id", "visit_id", "role", "applies_to", "entity_id", "partner_id"):
        answers[visit_id].append((document, role, applies_to, index.get(entity_id), partner))
    links: dict[int, list[int]] = defaultdict(list)
    for visit_id, pk in VisitActionPoint.objects.filter(visit_id__in=list(by_pk)).values_list(
        "visit_id", "action_point_id"
    ):
        links[visit_id].append(pk)
    return [item_of(v, entities[v.pk], answers[v.pk], links[v.pk]) for v in visits]


def run(triggered_by: str = "schedule", limit: int | None = None):
    """The nightly checks (see the module's notes), as one ``SyncRun``: ``rows_in`` the checks due that
    were reached, ``rows_written`` the checks made, ``rows_failed`` those whose call or answer failed.
    ``limit``: the most checks to make (an administrator's trial run)."""
    from neurodb.assistant import agent
    from neurodb.core.models import SyncRun
    from neurodb.integrations.runs import fail, finish_by_counts, new_run

    from .. import score

    sync_run = new_run(SyncRun.Job.FMM_AI_CHECKS, "checks", triggered_by)
    clock = time.monotonic()
    book = score.Rulebook.load()
    on, why = switched_on(book)
    if not on:
        sync_run.finish(SyncRun.Status.SUCCEEDED, skipped=why)
        return sync_run
    tally = {"checked": 0, "passed": 0, "flagged": 0, "up_to_date": 0, "tokens": 0, "visits": 0}
    stopped = ""
    try:
        api = agent.client().with_options(timeout=settings.FMM_RULES_TIMEOUT_SECONDS, max_retries=1)
        stopped = _check_all(api, book, sync_run, tally, limit)
    except agent.AssistantUnavailable:
        stopped = "AI is switched off"
    except Exception as exc:
        return fail(sync_run, exc, **tally, duration_ms=int((time.monotonic() - clock) * 1000))
    rescored = ""
    if tally["checked"]:
        rescored = _rescore(triggered_by)
    return finish_by_counts(
        sync_run,
        **tally,
        stopped=stopped,
        rescored=rescored,
        duration_ms=int((time.monotonic() - clock) * 1000),
    )


def _check_all(api, book, sync_run, tally: dict[str, int], limit: int | None) -> str:
    """The checks due, newest visits first, until the budget, the pause, ``limit`` or 3 failures in a
    row stop them; why they stopped ("" when every check due was made)."""
    ai_rules = book.ai_rules()
    setting = book.setting
    chars = setting.ai_text_chars
    names_ = privacy.names()
    hashes = {
        rule.code: prompt_hash(rule, book.prompts[rules.param(rule, "ai_prompt_key")]) for rule in ai_rules
    }
    visits = Visit.objects.filter(status__in=sorted(book.scored_statuses)).order_by(
        F("end_date").desc(nulls_last=True), "key"
    )
    failures = 0
    batch: list[Visit] = []

    def flush(batch: list[Visit]) -> str:
        nonlocal failures
        items = _items(batch)
        inputs = collect(items)
        stored = {
            (key, rule): (inp, prm)
            for key, rule, inp, prm in VisitAICheck.objects.filter(
                visit_key__in=[item.key for item in items]
            ).values_list("visit_key", "rule", "input_hash", "prompt_hash")
        }
        for item in items:
            tally["visits"] += 1
            for rule in ai_rules:
                raw = payload(inputs[item.key], rule, chars)
                wanted = input_hash(raw)
                if stored.get((item.key, rule.code)) == (wanted, hashes[rule.code]):
                    tally["up_to_date"] += 1
                    continue
                sync_run.rows_in += 1
                if limit is not None and tally["checked"] >= limit:
                    return f"stopped after {limit} checks"
                prompt = book.prompts[rules.param(rule, "ai_prompt_key")]
                sent = cleaned(raw, names_, chars)
                tokens = estimate(sent, prompt, setting)
                if not allowed(tokens):
                    return "paused" if budget.paused_until() else "budget: the rest waits for the next run"
                try:
                    privacy.assert_clean(sent, names_)
                except privacy.PrivacyRefused as exc:
                    logger.error(
                        "Monitoring insights: an AI check was not sent (%s %s): %s", item.key, rule.code, exc
                    )
                    sync_run.rows_failed += 1
                    continue
                try:
                    found = check(api, rule, prompt, sent, setting, names_)
                except Exception as exc:
                    if budget.trip(exc):
                        return "paused: the OpenAI credit ran out"
                    logger.warning("Monitoring insights: an AI check failed: %s", type(exc).__name__)
                    sync_run.rows_failed += 1
                    failures += 1
                    if failures >= FAILURES_IN_A_ROW:
                        return f"{FAILURES_IN_A_ROW} failed checks in a row"
                    continue
                if found is None:
                    sync_run.rows_failed += 1
                    failures += 1
                    if failures >= FAILURES_IN_A_ROW:
                        return f"{FAILURES_IN_A_ROW} failed checks in a row"
                    continue
                failures = 0
                VisitAICheck.objects.update_or_create(
                    visit_key=item.key,
                    rule=rule.code,
                    defaults={
                        "input_hash": wanted,
                        "prompt_hash": hashes[rule.code],
                        "model": found.model,
                        "passed": found.passed,
                        "detail": found.detail,
                        "input_tokens": found.input_tokens,
                        "output_tokens": found.output_tokens,
                        "checked_at": found.checked_at,
                    },
                )
                sync_run.rows_written += 1
                tally["checked"] += 1
                tally["passed" if found.passed else "flagged"] += 1
                tally["tokens"] += found.input_tokens + found.output_tokens
        return ""

    for visit in visits.iterator(chunk_size=BATCH):
        batch.append(visit)
        if len(batch) >= BATCH:
            if why := flush(batch):
                return why
            batch = []
    if batch:
        return flush(batch)
    return ""


def _rescore(triggered_by: str) -> str:
    """The scores recomputed with the new checks: a scores-only refresh now, or asked for when one runs."""
    from .. import refresh

    done = refresh.run(triggered_by=f"ai_checks:{triggered_by}"[:150], scores_only=True)
    if done is None:
        refresh.request("scores", "ai_checks")
        return "asked"
    return str(done.status)


def store_answers(answers: Mapping[tuple[str, str], tuple[bool, str]], model: str) -> int:
    """Keep answers made elsewhere (the demo data's, written without any AI call) as the checks of the
    visits' current inputs and the rules' current prompts: ``answers`` maps (visit key, rule) to (passed,
    detail). Returns the answers kept."""
    from .. import score

    book = score.Rulebook.load()
    ai_rules = {rule.code: rule for rule in book.ai_rules()}
    keys = sorted({key for key, code in answers if code in ai_rules})
    visits = list(Visit.objects.filter(key__in=keys))
    inputs = collect(_items(visits))
    kept = 0
    for (key, code), (passed, detail) in answers.items():
        if code not in ai_rules or key not in inputs:
            continue
        rule = ai_rules[code]
        sent = payload(inputs[key], rule, book.setting.ai_text_chars)
        VisitAICheck.objects.update_or_create(
            visit_key=key,
            rule=code,
            defaults={
                "input_hash": input_hash(sent),
                "prompt_hash": prompt_hash(rule, book.prompts[rules.param(rule, "ai_prompt_key")]),
                "model": model[:64],
                "passed": passed,
                "detail": privacy.clean(detail, DETAIL_CHARS)[0],
                "checked_at": timezone.now(),
            },
        )
        kept += 1
    return kept
