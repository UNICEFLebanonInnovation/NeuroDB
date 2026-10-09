"""The AI checks of the narrative quality rules (FMS's AI rules: R3, R5, R6, R7, R8, R32...), record by
record, and the nightly job that makes them (``manage.py fmm_ai_checks``).

**One check** is one Responses API call per record (an entity row of a visit, as FMS checks each record)
and rule: the instructions are the rule's prompt (the published prompt version's ``rule_prompts`` under
the rule's ``ai_prompt_key``, seeded from FMS Lebanon's prompt file) followed by NeuroDB's fixed rules
(``prompts.SAFETY_CHECKS``); the input is the rule's fields for the record (:func:`payload`): its entity
type and the row fields the rule lists (rating, narrative, Q1, Q2 and Q3 answers: its own, else its
entity's, its partner's or the visit's), then the visit's own fields it lists (the record's visit goals
and objective, else the visit's; the visit's action points with their due dates, repeated on each record
as FMS does; who an action point is assigned to only as a count; the team and the person responsible
never). Neither the visit's label nor the entity's name is sent: the rules judge the coherence of the
texts, and identical texts then ask the same question. Each text is cut to the settings'
``ai_text_chars`` and cleaned (``privacy.clean``: names, e-mail addresses, phone numbers and links
removed), then checked once more (``privacy.assert_clean``). The answer follows a strict JSON schema,
``{"is_coherent": boolean, "detail": string}``. The model is the score settings' (``ai_model``), else
``AI_ASSISTANT_MODEL``; temperature 0.3 through the sampling guard (``sampling``: not sent when the model
refuses it), low reasoning effort, ``ai_max_output_tokens`` (2,000, the reasoning included), nothing
stored at OpenAI, and one prompt cache key per rule.

**The answers** (``AICheckAnswer``) are kept by what was asked: the rule, its prompt (``prompt_hash``: its
instructions, fields and NeuroDB's fixed text, ``prompts.CHECKS_VERSION``) and the record's inputs as
read, before cleaning (``input_hash``). A record's answer is the one whose three match, so records with
the same texts share one answer, on one visit or across visits, and a record is checked again only when
its inputs or the rule's prompt change. The scoring reads only those (:func:`fresh`, which marks them
used once a day; the refresh deletes the answers no scoring has read for 120 days); a record with a check
missing or out of date is provisional (``score.score_outcome``). The explanation kept is cleaned and
checked against what was sent: one that names a figure the record does not hold, or a word NeuroDB never
writes, is left out (the verdict stays).

**Carry-over** (:func:`carry_over`, once, by the refresh before it scores and by the job, with no command
to run, so it needs neither the AI nor its budget): the checks Release 2 made per visit (``VisitAICheck``)
are copied for the visits with a single record, whose record was then the whole visit: a verdict still up
to date against what the visit sent then (:func:`legacy_payload`) becomes that record's answer
(``carried``). A visit with several records inherits nothing (a verdict on merged texts cannot be given
to one row): its records stay provisional until they are checked, first in the job's order. Each visit
check dealt with is deleted; ``legacy_checks_left`` in the run details counts those left (of rules not
on now). Re-check carried answers (Score settings) deletes carried answers a batch at a time, so they
are checked properly when the budget allows.

**The job** (:func:`run`, one at a time under its own lock, one ``SyncRun`` "Monitoring insights (AI
checks)"): the carry-over, then the records of the scored visits, this calendar year's first, those of
visits with several records first, newest first; for each, the checks missing or out of date, one call
for every record whose input is the same (``shared``), while the day's budget allows (the checks' own
``FMM_RULES_DAILY_TOKEN_CAP``, recorded under the AI use feature ``fmm_rules``, and 80% of
``AI_DAILY_TOKEN_SOFT_CAP`` across every feature); what is left waits for the next night (the back-fill:
``records_pending``, ``checks_pending`` and ``nights_estimate`` in the run details). It stops when the
OpenAI credit runs out (the AI pauses for 6 hours, as for the briefs) or after 3 failed checks in a row.
When it checked or carried anything it recomputes the scores (a scores-only refresh). Nothing runs while
the AI, Monitoring insights' AI or the AI checks (Score settings) are switched off.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
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
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.utils import timezone

from neurodb.assistant import usage
from neurodb.integrations.background import FMM_AI_CHECKS_LOCK_ID
from neurodb.watch.budget import SOFT_SHARE

from .. import fields, parse, privacy, rules
from ..models import AICheckAnswer, QuestionAnswer, Visit, VisitActionPoint, VisitAICheck, VisitEntity
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
# what the AI checks made per visit hashed (``VisitAICheck``): kept until those are all carried over
LEGACY_CHECKS_VERSION = 1
RECHECK_BATCH = 500  # carried answers deleted by one "Re-check carried answers"
ROW_VALUES = (
    "fmq_answered_pct",
    "fmq_answered_categories",
    "method_count",
    "red_flag_count",
    "attachments_count",
)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_NEVER = re.compile(r"\b(items?|agents?|detectors?|receipts?|llms?)\b", re.IGNORECASE)


# ------------------------------------------------------------------------------------------ inputs
@dataclass
class Row:
    """One record of a visit (a finding row), as the checks read it."""

    finding_id: int | None
    kind: str
    entity: str
    entity_type: str
    rating_raw: str
    partner_id: int | None
    datamart_id: int | None = None  # the record's stable id
    values: dict[str, Any] = field(default_factory=dict)  # its own derived columns and place


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


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


def _row_values(visit: Visit, entity: Any) -> dict[str, Any]:
    """A record's own derived columns and place, as its checks may send them (its visit's while the
    build has not measured the record yet)."""
    measured = getattr(entity, "questions_asked", None) is not None or visit.questions_asked is None
    source = entity if measured else visit
    values = {
        "fmq_answered_pct": _number(getattr(source, "fmq_answered_pct", None)),
        "fmq_answered_categories": getattr(source, "fmq_answered_categories", None),
        "method_count": getattr(source, "method_count", None),
        "red_flag_count": getattr(source, "red_flag_count", None),
        "attachments_count": getattr(entity, "attachments_count", None),
    }
    values["attachment_count"] = values["attachments_count"]
    values["location_pcode"] = (
        (getattr(entity, "location_pcode", "") or "").strip() or visit.place_pcode or ""
    )
    return values


def item_of(
    visit: Visit, entities: Sequence[Any], answers: Iterable[tuple], action_points: Iterable[int]
) -> Item:
    """An :class:`Item` from a visit, its records (``VisitEntity``, in order), its answered answers and
    the ids of its action points."""
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
                getattr(e, "datamart_id", None),
                _row_values(visit, e),
            )
            for e in entities
        ],
        answers=[a for a in answers if a[1] in ("q1", "q2", "q3")],
        action_points=list(action_points),
        values={
            "fmq_answered_pct": _number(visit.fmq_answered_pct),
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
    """The texts of each visit's report (its records' narratives and Q1, Q2 and Q3 answers, their visit
    goals and objectives, its action points), read from their sources for ``items``, by visit key: each
    row keeps its record's id (``datamart_id``), its own visit goals and objective and its derived
    values. Kept in memory only."""
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
                "datamart_id": row.datamart_id,
                "entity": row.entity,
                "entity_type": row.entity_type,
                "overall_finding_rating": row.rating_raw,
                "narrative_finding": narratives.get(row.finding_id) or "",
                "own_visit_goals": own.get("visit_goals") or "",
                "own_objective": own.get("objective") or "",
                "values": dict(row.values),
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


def _visit_field(name: str, visit: Mapping[str, Any], limit: int) -> Any:
    """One of the visit's own fields as a check sends it (``visit_goals`` and ``objective`` aside)."""
    if name == "action_points_assigned_to":
        return f"{visit['action_points_assigned']} of {visit['action_points_count']} action points assigned"
    if name == "action_points_text":
        return [_cut(text, min(limit, 600)) for text in visit["action_points_text"][:ACTION_POINTS]]
    if name == "action_points_due_dates":
        return visit["action_points_due_dates"][:ACTION_POINTS]
    return visit["action_points_count"]


VISIT_FIELDS = (
    "action_points_assigned_to",
    "action_points_text",
    "action_points_due_dates",
    "action_points_count",
)


def payload(inputs: Inputs, index: int, rule, limit: int) -> dict[str, Any]:
    """What one check of ``rule`` sends about the record at ``index`` of a visit, as written (cut to
    ``limit`` characters per text; cleaned by :func:`cleaned` before it is sent): its entity type and the
    row fields the rule lists, then the visit's own fields it lists (the record's visit goals and
    objective, else the visit's). Never the visit's label or the entity's name; the team and the person
    responsible are never sent; who action points are assigned to only as a count."""
    wanted = list(dict.fromkeys(rules.param(rule, "fields") or []))
    row = inputs.rows[index]
    row_fields = [f for f in wanted if f in rules.ROW_COLUMNS and f not in ("entity", "entity_type")]
    out: dict[str, Any] = {
        "entity_type": row["entity_type"],
        **{f: _cut(row.get(f), limit) for f in row_fields},
    }
    values = row.get("values") or {}
    for name in wanted:
        if name in rules.ROW_COLUMNS or name in ("team_members", "person_responsible_email"):
            continue
        if name in VISIT_FIELDS:
            out[name] = _visit_field(name, inputs.visit, limit)
        elif name in ("visit_goals", "objective"):
            out[name] = _cut(row.get(f"own_{name}") or inputs.visit[name], limit)
        else:
            out[name] = values[name] if name in values else inputs.values.get(name)
    return out


def legacy_payload(inputs: Inputs, rule, limit: int) -> dict[str, Any]:
    """What a check of ``rule`` sent about a whole visit before records were checked one by one
    (Release 2, ``VisitAICheck``): its label and every row with its entity's name. Read only to know
    whether a visit check is still up to date for :func:`carry_over`; kept until none is left."""
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
        if name in VISIT_FIELDS:
            out[name] = _visit_field(name, visit, limit)
        elif name in ("visit_goals", "objective"):
            out[name] = _cut(visit[name], limit)
        else:
            out[name] = inputs.values.get(name)
    return out


def input_hash(sent: Mapping[str, Any]) -> str:
    """sha256 of a check's input as read (before cleaning), so a new list of names to remove does not
    make every record be checked again."""
    blob = json.dumps(sent, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def prompt_hash(rule, text: str, version: int | None = None) -> str:
    """sha256 of what a check of ``rule`` asks: its instructions, the fields it reads, NeuroDB's fixed
    text (``version``: ``prompts.CHECKS_VERSION``) and the answer's format."""
    blob = json.dumps(
        {
            "prompt": (text or "").strip(),
            "fields": list(rules.param(rule, "fields") or []),
            "fixed": prompts.CHECKS_VERSION if version is None else version,
            "schema": SCHEMA,
        },
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def legacy_prompt_hash(rule, text: str) -> str:
    """The prompt hash a visit check (``VisitAICheck``) was kept with."""
    return prompt_hash(rule, text, LEGACY_CHECKS_VERSION)


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


def _prompt_hashes(ai_rules: Sequence[Any], book) -> dict[str, str]:
    return {
        rule.code: prompt_hash(rule, book.prompts[rules.param(rule, "ai_prompt_key")]) for rule in ai_rules
    }


def fresh(
    items: Sequence[Item], ai_rules: Sequence[Any], book, touch: bool = True, today=None
) -> dict[int, dict[str, tuple[bool, str]]]:
    """{record (``datamart_id``): {rule: (passed, detail)}} of the answers kept whose inputs and prompt
    are those of each record now; a check missing or out of date is left out (pending). The answers
    read are marked used on ``today`` (the scoring's day; once a day; not with ``touch`` off, for a
    preview)."""
    if not items or not ai_rules:
        return {}
    limit = book.setting.ai_text_chars
    inputs = collect(items)
    hashes = _prompt_hashes(ai_rules, book)
    wanted: dict[tuple[str, str], list[int]] = defaultdict(list)
    for item in items:
        for index, row in enumerate(item.rows):
            if row.datamart_id is None:
                continue
            for rule in ai_rules:
                found = input_hash(payload(inputs[item.key], index, rule, limit))
                wanted[(rule.code, found)].append(row.datamart_id)
    if not wanted:
        return {}
    kept = AICheckAnswer.objects.filter(
        rule__in=list(hashes), input_hash__in=list({h for _rule, h in wanted})
    ).values_list("pk", "rule", "input_hash", "prompt_hash", "passed", "detail", "last_used")
    today = today or timezone.localdate()
    out: dict[int, dict[str, tuple[bool, str]]] = defaultdict(dict)
    used: list[int] = []
    for pk, rule, inp, prm, passed, detail, last_used in kept:
        if prm != hashes.get(rule) or (rule, inp) not in wanted:
            continue
        for record in wanted[(rule, inp)]:
            out[record][rule] = (passed, detail)
        if last_used is None or last_used < today:
            used.append(pk)
    if touch and used:
        AICheckAnswer.objects.filter(pk__in=used).update(last_used=today)
    return dict(out)


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
) -> AICheckAnswer | None:
    """One check (see the module's notes): the answer as an ``AICheckAnswer`` not saved yet (without its
    hashes); None when the answer could not be read (cut off, refused or not the format). Raises what
    the call raised."""
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
    return AICheckAnswer(
        rule=rule.code,
        model=model[:64],
        passed=raw["is_coherent"],
        detail=grounded(raw.get("detail"), sent_text, names_),
        input_tokens=min(input_tokens + cached, 2_000_000_000),
        output_tokens=min(output, 2_000_000_000),
        checked_at=timezone.now(),
        last_used=timezone.localdate(),
    )


def keep(found: AICheckAnswer, input_hash_: str, prompt_hash_: str, carried: bool = False) -> AICheckAnswer:
    """Keep an answer under its hashes (an answer kept for the same question is replaced)."""
    row, _created = AICheckAnswer.objects.update_or_create(
        rule=found.rule,
        input_hash=input_hash_,
        prompt_hash=prompt_hash_,
        defaults={
            "model": found.model,
            "passed": found.passed,
            "detail": found.detail,
            "input_tokens": found.input_tokens,
            "output_tokens": found.output_tokens,
            "checked_at": found.checked_at,
            "last_used": found.last_used or timezone.localdate(),
            "carried": carried,
        },
    )
    return row


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


def carry_over(book) -> dict[str, int]:
    """The checks made per visit before records (``VisitAICheck``) carried over, once (see the module's
    notes): for a visit with a single record, a verdict still up to date against what the visit sent
    then (:func:`legacy_payload`, the prompt hash of then) is kept as that record's answer (``carried``);
    the checks of a visit with several records, of a visit gone, or out of date are dropped. Each check
    dealt with is deleted; the checks of a rule not on now are left for when it is (and not read again
    until then: the refresh calls this before every scoring while any is left). Returns
    ``{"carried", "dropped", "left"}``."""
    out = {"carried": 0, "dropped": 0, "left": 0}
    if not VisitAICheck.objects.exists():
        return out
    ai_rules = {rule.code: rule for rule in book.ai_rules()}
    prompts_ = {code: book.prompts[rules.param(rule, "ai_prompt_key")] for code, rule in ai_rules.items()}
    new_hashes = {code: prompt_hash(rule, prompts_[code]) for code, rule in ai_rules.items()}
    old_hashes = {code: legacy_prompt_hash(rule, prompts_[code]) for code, rule in ai_rules.items()}
    limit = book.setting.ai_text_chars
    today = timezone.localdate()
    # what can be dealt with now: a check of a rule on, or of a visit gone or with several records (the
    # checks of a rule off on a single-record visit wait, unread)
    single_keys = Visit.objects.filter(entities=1).values("key")
    due = VisitAICheck.objects.filter(Q(rule__in=list(ai_rules)) | ~Q(visit_key__in=single_keys))
    keys = sorted(set(due.values_list("visit_key", flat=True)))
    for start in range(0, len(keys), BATCH):
        batch = keys[start : start + BATCH]
        found = list(VisitAICheck.objects.filter(visit_key__in=batch).order_by("pk"))
        single = {v.key: v for v in Visit.objects.filter(key__in=batch, entities=1).order_by("key")}
        # the texts are read only for the single-record visits with a check of a rule on
        asked = [
            single[key] for key in sorted({r.visit_key for r in found if r.rule in ai_rules} & set(single))
        ]
        inputs = collect(_items(asked)) if asked else {}
        done: list[int] = []
        carried: list[AICheckAnswer] = []
        for row in found:
            rule = ai_rules.get(row.rule)
            if rule is None and row.visit_key in single:
                continue  # a rule not on now: kept for when it is
            visit_inputs = inputs.get(row.visit_key)
            alone = visit_inputs is not None and len(visit_inputs.rows) == 1
            if (
                not alone
                or row.prompt_hash != old_hashes[row.rule]
                or row.input_hash != input_hash(legacy_payload(visit_inputs, rule, limit))
            ):
                done.append(row.pk)
                out["dropped"] += 1
                continue
            carried.append(
                AICheckAnswer(
                    rule=row.rule,
                    prompt_hash=new_hashes[row.rule],
                    input_hash=input_hash(payload(visit_inputs, 0, rule, limit)),
                    model=row.model,
                    passed=row.passed,
                    detail=row.detail,
                    input_tokens=row.input_tokens,
                    output_tokens=row.output_tokens,
                    checked_at=row.checked_at,
                    last_used=today,
                    carried=True,
                )
            )
            done.append(row.pk)
            out["carried"] += 1
        AICheckAnswer.objects.bulk_create(carried, ignore_conflicts=True)
        VisitAICheck.objects.filter(pk__in=done).delete()
    out["left"] = VisitAICheck.objects.count()
    return out


def recheck_carried(limit: int = RECHECK_BATCH) -> tuple[int, int]:
    """Re-check carried answers (Score settings): delete up to ``limit`` answers carried over from the
    visit checks, the oldest first, so the next runs check those records properly while the budget
    allows. Returns (deleted, carried answers left)."""
    pks = list(
        AICheckAnswer.objects.filter(carried=True)
        .order_by("checked_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
    deleted = AICheckAnswer.objects.filter(pk__in=pks).delete()[0] if pks else 0
    return deleted, AICheckAnswer.objects.filter(carried=True).count()


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
    tally = {
        "checked": 0,
        "passed": 0,
        "flagged": 0,
        "up_to_date": 0,
        "shared": 0,
        "tokens": 0,
        "visits": 0,
        "records": 0,
        "records_pending": 0,
        "checks_pending": 0,
    }
    legacy = {"carried": 0, "dropped": 0, "left": 0}
    stopped = ""
    try:
        legacy = carry_over(book)
        api = agent.client().with_options(timeout=settings.FMM_RULES_TIMEOUT_SECONDS, max_retries=1)
        stopped = _check_all(api, book, sync_run, tally, limit)
    except agent.AssistantUnavailable:
        stopped = "AI is switched off"
    except Exception as exc:
        return fail(sync_run, exc, **tally, duration_ms=int((time.monotonic() - clock) * 1000))
    rescored = ""
    if tally["checked"] or legacy["carried"]:
        rescored = _rescore(triggered_by)
    return finish_by_counts(
        sync_run,
        **tally,
        stopped=stopped,
        rescored=rescored,
        carried=legacy["carried"],
        legacy_dropped=legacy["dropped"],
        legacy_checks_left=legacy["left"],
        nights_estimate=_nights(tally, sync_run),
        duration_ms=int((time.monotonic() - clock) * 1000),
    )


def _nights(tally: Mapping[str, int], sync_run) -> int | None:
    """The nights the checks left would take at the pace of the last run that made any: 0 when none is
    left; None when no run has made any yet."""
    from neurodb.core.models import SyncRun

    pending = tally["checks_pending"]
    if not pending:
        return 0
    pace = tally["checked"]
    if not pace:
        earlier = (
            SyncRun.objects.filter(job=SyncRun.Job.FMM_AI_CHECKS)
            .exclude(pk=sync_run.pk)
            .order_by("-started_at")
            .values_list("details", flat=True)[:30]
        )
        pace = next(
            (int(d.get("checked") or 0) for d in earlier if isinstance(d, dict) and d.get("checked")), 0
        )
    return math.ceil(pending / pace) if pace else None


def _ordered(book):
    """The scored visits in the job's order: this calendar year's first, then those with several
    records (their records inherit no visit check), then the newest (by the visit date)."""
    year = timezone.localdate().year
    return (
        Visit.objects.filter(status__in=sorted(book.scored_statuses))
        .annotate(
            this_year=Case(
                When(visit_date__year=year, then=Value(1)), default=Value(0), output_field=IntegerField()
            ),
            several=Case(When(entities__gt=1, then=Value(1)), default=Value(0), output_field=IntegerField()),
        )
        .order_by("-this_year", "-several", F("visit_date").desc(nulls_last=True), "key")
    )


def _check_all(api, book, sync_run, tally: dict[str, int], limit: int | None) -> str:
    """The checks due, record by record in the job's order (:func:`_ordered`), one call for every
    record whose input is the same, until the budget, the pause, ``limit`` or 3 failures in a row stop
    them; the records left are still read, to count what waits (``records_pending``,
    ``checks_pending``). Returns why it stopped ("" when every check due was made)."""
    ai_rules = book.ai_rules()
    setting = book.setting
    chars = setting.ai_text_chars
    names_ = privacy.names()
    hashes = _prompt_hashes(ai_rules, book)
    state = {"failures": 0, "stopped": ""}
    made: set[tuple[str, str]] = set()  # the answers this run made: a later record reusing one is shared
    waiting: set[tuple[str, str]] = set()
    waiting_records: set[int] = set()

    def stop(why: str) -> None:
        state["stopped"] = state["stopped"] or why

    def failed() -> None:
        sync_run.rows_failed += 1
        state["failures"] += 1
        if state["failures"] >= FAILURES_IN_A_ROW:
            stop(f"{FAILURES_IN_A_ROW} failed checks in a row")

    def flush(batch: list[Visit]) -> None:
        items = _items(batch)
        inputs = collect(items)
        planned = []
        for item in items:
            tally["visits"] += 1
            for index, row in enumerate(item.rows):
                tally["records"] += 1
                for rule in ai_rules:
                    raw = payload(inputs[item.key], index, rule, chars)
                    planned.append((item.key, row.datamart_id, rule, raw, input_hash(raw)))
        stored = {
            (rule, inp)
            for rule, inp, prm in AICheckAnswer.objects.filter(
                rule__in=list(hashes), input_hash__in=list({p[4] for p in planned})
            ).values_list("rule", "input_hash", "prompt_hash")
            if hashes.get(rule) == prm
        }
        for key, record, rule, raw, wanted in planned:
            asked = (rule.code, wanted)
            if asked in stored:
                tally["shared" if asked in made else "up_to_date"] += 1
                continue
            if not state["stopped"]:
                sync_run.rows_in += 1
                found = _one(key, rule, raw, wanted)
                if found is not None:
                    stored.add(asked)
                    made.add(asked)
                    continue
            waiting.add(asked)
            if record is not None:
                waiting_records.add(record)

    def _one(key: str, rule, raw: dict[str, Any], wanted: str) -> AICheckAnswer | None:
        """One check made and kept; None when it was not (stopped, refused or failed)."""
        if limit is not None and tally["checked"] >= limit:
            stop(f"stopped after {limit} checks")
            return None
        prompt = book.prompts[rules.param(rule, "ai_prompt_key")]
        sent = cleaned(raw, names_, chars)
        tokens = estimate(sent, prompt, setting)
        if not allowed(tokens):
            stop("paused" if budget.paused_until() else "budget: the rest waits for the next run")
            return None
        try:
            privacy.assert_clean(sent, names_)
        except privacy.PrivacyRefused as exc:
            logger.error("Monitoring insights: an AI check was not sent (%s %s): %s", key, rule.code, exc)
            sync_run.rows_failed += 1
            return None
        try:
            found = check(api, rule, prompt, sent, setting, names_)
        except Exception as exc:
            if budget.trip(exc):
                stop("paused: the OpenAI credit ran out")
                return None
            logger.warning("Monitoring insights: an AI check failed: %s", type(exc).__name__)
            failed()
            return None
        if found is None:
            failed()
            return None
        state["failures"] = 0
        kept = keep(found, wanted, hashes[rule.code])
        sync_run.rows_written += 1
        tally["checked"] += 1
        tally["passed" if found.passed else "flagged"] += 1
        tally["tokens"] += found.input_tokens + found.output_tokens
        return kept

    batch: list[Visit] = []
    for visit in _ordered(book).iterator(chunk_size=BATCH):
        batch.append(visit)
        if len(batch) >= BATCH:
            flush(batch)
            batch = []
    if batch:
        flush(batch)
    tally["checks_pending"] = len(waiting)
    tally["records_pending"] = len(waiting_records)
    return state["stopped"]


def _rescore(triggered_by: str) -> str:
    """The scores recomputed with the new checks: a scores-only refresh now, or asked for when one runs."""
    from .. import refresh

    done = refresh.run(triggered_by=f"ai_checks:{triggered_by}"[:150], scores_only=True)
    if done is None:
        refresh.request("scores", "ai_checks")
        return "asked"
    return str(done.status)


def store_answers(answers: Mapping[tuple[int, str], tuple[bool, str]], model: str) -> int:
    """Keep answers made elsewhere (the demo data's, written without any AI call) as the checks of the
    records' current inputs and the rules' current prompts: ``answers`` maps (record ``datamart_id``,
    rule) to (passed, detail); records with the same inputs share one answer (the last one given).
    Returns the answers given that were kept."""
    from .. import score

    book = score.Rulebook.load()
    ai_rules = {rule.code: rule for rule in book.ai_rules()}
    records = sorted({record for record, code in answers if code in ai_rules})
    visit_ids = set(VisitEntity.objects.filter(datamart_id__in=records).values_list("visit_id", flat=True))
    items = _items(list(Visit.objects.filter(pk__in=visit_ids).order_by("pk")))
    inputs = collect(items)
    where = {row.datamart_id: (item.key, index) for item in items for index, row in enumerate(item.rows)}
    hashes = _prompt_hashes(list(ai_rules.values()), book)
    kept = 0
    for (record, code), (passed, detail) in answers.items():
        if code not in ai_rules or record not in where:
            continue
        key, index = where[record]
        sent = payload(inputs[key], index, ai_rules[code], book.setting.ai_text_chars)
        found = AICheckAnswer(
            rule=code,
            model=model[:64],
            passed=passed,
            detail=privacy.clean(detail, DETAIL_CHARS)[0],
            checked_at=timezone.now(),
            last_used=timezone.localdate(),
        )
        keep(found, input_hash(sent), hashes[code])
        kept += 1
    return kept
