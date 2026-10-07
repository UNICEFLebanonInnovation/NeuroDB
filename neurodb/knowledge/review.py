"""The document review (FMS §9 "Other Reports"): the documents put in a review batch are read by the AI
into **findings** with evidence, **key statements** and **action points**, and people accept or reject
them. Run by ``manage.py review_documents`` (nightly, the *Run a job* buttons, or a document's *Analyse*
button through :func:`start`).

**Stages per document** (each noted yes, partly or failed in ``Document.review_stage_notes``):

1. **Text**: the text the knowledge base already read; it must be there (a document still being read
   waits for the next run, one that could not be read fails).
2. **Findings**: the text in parts of about ``chunk_size`` characters with page markers
   (``review_locate.parts``), one call each, the answer held to a strict JSON schema (category, a tag
   from the topic list, text, verbatim quote, place, date, reported or interpreted); a tag not in the
   list becomes "Other". A part whose answer is broken is asked again once as two halves; what still
   fails is left out and the document is *partly* analysed ("only n% read"). Nothing read at all: failed.
3. **Locate** (no AI): each quote looked for in the text to give its exact page, else its part's
   pages; the place matched to the gazetteer, the date read, the evidence score worked out
   (``review_locate``).
4. **Summary**: one call: key statements citing the findings' numbers, with an urgency (0-100).
5. **Enrichment**: one call: at most 15 action points (owner, deadline, priority), explicit commitments
   only, citing findings. An action point that no "action point" finding states is *derived*, and a
   derived finding (category action point, the cited finding's quote) records it.

The calls of one document are made first and written together at the end, so a document stopped half-way
(budget, credit, a restart) keeps what it had. **A new analysis** replaces what the AI wrote, but keeps:
the verdicts (and a person's edits) of findings whose AI text and quote are unchanged, the findings a
person added, the verdicts of statements with the same words, and the status of action points with the
same action (words compared normalised).

**Modes**: ``full`` (every document in a batch), ``pending`` (pending, failed or partly analysed: the
nightly run), ``enrich`` (the action points again from the stored findings), ``locate`` (where the
findings are again, no AI: after the gazetteer changed). Archived batches and reference documents are
left out.

**Limits**: the review is switched on in Document review settings (off until an administrator turns it
on) and needs the AI assistant. Its calls are recorded under ``doc_review`` in the AI use ledger, with
its own daily cap (the settings' cap, else ``DOC_REVIEW_DAILY_TOKEN_CAP``), 80% of the shared
``AI_DAILY_TOKEN_SOFT_CAP`` (a background job) and the OpenAI credit pause (Monitoring insights'
``AIState``, shared by every AI feature on the key): when one is reached the run stops cleanly and the
rest waits for the next night (a full run leaves the documents it did not reach waiting). A document
that needs more than a whole day's budget is not started: it could never finish (failed, with the
reason). One run at a time (an advisory lock; a single document's run waits for
it). A document with no progress for 30 minutes is marked failed with the reason.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from neurodb.assistant import usage
from neurodb.integrations.background import DOC_REVIEW_LOCK_ID
from neurodb.watch.budget import SOFT_SHARE

from . import review_locate as where
from . import review_prompts as prompts
from .models import (
    Document,
    DocumentActionPoint,
    DocumentFinding,
    DocumentReviewSettings,
    DocumentStatement,
    FindingCategory,
    Topic,
    Verdict,
)

logger = logging.getLogger(__name__)

LOCK_ID = DOC_REVIEW_LOCK_ID  # 7_140_436
FULL, PENDING, ENRICH, LOCATE = "full", "pending", "enrich", "locate"
MODES = (FULL, PENDING, ENRICH, LOCATE)
TEXT, FINDINGS, PLACES, SUMMARY, ENRICHMENT = "text", "findings", "locate", "summary", "enrichment"
STAGES = (TEXT, FINDINGS, PLACES, SUMMARY, ENRICHMENT)
YES, PARTLY, FAILED = "yes", "partly", "failed"
STALE_MINUTES = 30
TIMEOUT_SECONDS = 180
EFFORT = "low"
CHARS_PER_TOKEN = 3.5
MAX_OUTPUT = {FINDINGS: 8000, SUMMARY: 6000, ENRICHMENT: 5000}
MAX_ACTION_POINTS = 15
TEXT_CHARS, QUOTE_CHARS, SHORT_CHARS = 1200, 1000, 200
FINDING_LINE_CHARS = 400  # of each finding's text sent to the summary and the enrichment
STATEMENTS_PER_FINDINGS = 3  # one statement for about three findings (between 5 and the setting)
CATEGORIES = [value for value, _label in FindingCategory.choices]
PRIORITIES = [value for value, _label in DocumentActionPoint.Priority.choices]

OFF = "the document review is switched off (Document review settings)"
AI_OFF = "AI is switched off"
PAUSED = "AI is paused: the OpenAI credit ran out"
BUDGET = "budget: today's AI budget for the document review is used; the rest waits for the next run"
SHARED_BUDGET = "budget: today's shared AI budget is nearly used; the rest waits for the next run"
NOT_READ_YET = "waiting: the knowledge base is still reading the document"
STALE = "No progress for 30 minutes (the run stopped, e.g. the server restarted): analyse it again."
TOO_LONG = (
    "Too long for one day's budget of the document review (about {tokens:,} tokens; a day gives at most "
    "{cap:,}): raise the Daily token cap in Document review settings, or split the document."
)
TOO_LONG_SHARED = (
    "Too long for one day's budget of the document review (about {tokens:,} tokens; the shared AI "
    "budget, AI_DAILY_TOKEN_SOFT_CAP, gives it at most {cap:,} a day): split the document, or raise the "
    "shared budget."
)
TEXT_CHANGED = "waiting: the knowledge base read a new text while it was analysed; it is analysed again"


def _string(description: str = "") -> dict[str, Any]:
    return {"type": "string", **({"description": description} if description else {})}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


FINDINGS_SCHEMA = _object(
    {
        "findings": {
            "type": "array",
            "items": _object(
                {
                    "category": {"type": "string", "enum": CATEGORIES},
                    "tag": _string("one tag of the list, as written; Other when none fits"),
                    "text": _string("the finding in one to three sentences"),
                    "quote": _string("the supporting words, copied exactly"),
                    "place": _string("as written; Lebanon for the whole country; '' when not said"),
                    "date": _string("the date or period as written; '' when not said"),
                    "kind": {"type": "string", "enum": ["reported", "interpreted"]},
                }
            ),
        }
    }
)
STATEMENTS_SCHEMA = _object(
    {
        "statements": {
            "type": "array",
            "items": _object(
                {
                    "text": _string(),
                    "urgency": {"type": "integer", "description": "0 (background) to 100 (act now)"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "place": _string(),
                    "date": _string(),
                    "cites": {"type": "array", "items": {"type": "integer"}},
                }
            ),
        }
    }
)
ACTIONS_SCHEMA = _object(
    {
        "action_points": {
            "type": "array",
            "items": _object(
                {
                    "action": _string(),
                    "owner": _string("organisation, ministry, team or role; '' when not said"),
                    "deadline": _string("as written; '' when not said"),
                    "priority": {"type": "string", "enum": PRIORITIES},
                    "cites": {"type": "array", "items": {"type": "integer"}},
                }
            ),
        }
    }
)
SCHEMAS = {FINDINGS: FINDINGS_SCHEMA, SUMMARY: STATEMENTS_SCHEMA, ENRICHMENT: ACTIONS_SCHEMA}
ANSWER_KEYS = {FINDINGS: "findings", SUMMARY: "statements", ENRICHMENT: "action_points"}
PROMPT_OF = {FINDINGS: prompts.TAGGING, SUMMARY: prompts.SUMMARY, ENRICHMENT: prompts.ENRICHMENT}


class Stop(Exception):  # noqa: N818 - a reason to end the run, not an error
    """The run stops here (budget, credit pause): the rest waits for the next run."""


# ------------------------------------------------------------------------------------------ switches
def switched_on(setting: DocumentReviewSettings | None = None) -> tuple[bool, str]:
    setting = setting or DocumentReviewSettings.load()
    if not setting.enabled:
        return False, OFF
    if not settings.AI_ASSISTANT_ENABLED:
        return False, AI_OFF
    return True, ""


def blocked(tokens: int, setting: DocumentReviewSettings) -> str:
    """Why a call needing ``tokens`` may not start now ("" when it may): the credit pause, the review's
    daily cap, or 80% of the shared soft cap (a background job comes after people)."""
    from neurodb.fmm.ai import budget

    if budget.paused_until() is not None:
        return PAUSED
    if usage.today_total(usage.DOC_REVIEW) + tokens > setting.token_cap:
        return BUDGET
    if usage.today_total() + tokens > settings.AI_DAILY_TOKEN_SOFT_CAP * SOFT_SHARE:
        return SHARED_BUDGET
    return ""


def estimate(stage: str, payload: str, setting: DocumentReviewSettings) -> int:
    chars = len(payload) + len(setting.prompt(PROMPT_OF[stage])) + len(prompts.FIXED)
    return int(chars / CHARS_PER_TOKEN) + MAX_OUTPUT[stage]


def day_cap(setting: DocumentReviewSettings) -> tuple[int, str]:
    """The most a day can give the review (its own cap, or 80% of the shared cap when lower), and what
    to say of a document that needs more."""
    shared = int(settings.AI_DAILY_TOKEN_SOFT_CAP * SOFT_SHARE)
    return (setting.token_cap, TOO_LONG) if setting.token_cap <= shared else (shared, TOO_LONG_SHARED)


def whole_estimate(ctx: Context, document_parts: list[where.Part]) -> int:
    """About the tokens a whole analysis of the document takes: each part sent with half of its longest
    answer, then the summary's and the enrichment's answers."""
    extra = len(_findings_extra(ctx)) + len(ctx.setting.prompt(prompts.TAGGING)) + len(prompts.FIXED)
    findings = sum(
        int((p.chars + extra) / CHARS_PER_TOKEN) + MAX_OUTPUT[FINDINGS] // 2 for p in document_parts
    )
    return findings + MAX_OUTPUT[SUMMARY] + MAX_OUTPUT[ENRICHMENT]


# ------------------------------------------------------------------------------------------ keys
def norm(text: str) -> str:
    return where.fold(text or "")


def key(*texts: str) -> str:
    return hashlib.sha256("|".join(norm(t) for t in texts).encode()).hexdigest()


def text_hash(document: Document) -> str:
    return hashlib.sha256((document.text or "").encode()).hexdigest()


# ------------------------------------------------------------------------------------------ topics
class Topics:
    """The active tags, as the AI is given them ("Programme › Subtopic › Tag") and as its answer is read
    back: a full path, a subtopic and tag, or a tag alone; anything else is Other."""

    def __init__(self):
        self.other = Topic.other()
        rows = list(
            Topic.objects.filter(active=True, subtopic__active=True, subtopic__programme__active=True)
            .exclude(pk=self.other.pk)
            .select_related("subtopic__programme")
        )
        self.lines = [t.path for t in rows] + [Topic.OTHER]
        self.by_key: dict[str, Topic] = {}
        for topic in rows:  # the first in the topics' order wins a tag name used twice
            for written in (topic.path, f"{topic.subtopic.name} › {topic.name}", topic.name):
                self.by_key.setdefault(self._key(written), topic)

    @staticmethod
    def _key(text: str) -> str:
        parts = [norm(p) for p in str(text or "").replace(">", "›").split("›")]
        return " › ".join(p for p in parts if p)

    def find(self, written: str) -> Topic:
        found = self.by_key.get(self._key(written))
        if found is None and "›" in str(written).replace(">", "›"):  # the tag alone after a wrong path
            found = self.by_key.get(self._key(str(written).replace(">", "›").split("›")[-1]))
        return found or self.other


# ------------------------------------------------------------------------------------------ one call
@dataclass
class Context:
    """What one run shares: the client, the settings, the topics, the gazetteer and its tally."""

    api: Any
    setting: DocumentReviewSettings
    model: str = ""
    tally: dict[str, Any] = field(default_factory=lambda: Counter())
    _topics: Topics | None = None
    _gazetteer: where.Gazetteer | None = None

    @property
    def topics(self) -> Topics:
        if self._topics is None:
            self._topics = Topics()
        return self._topics

    @property
    def gazetteer(self) -> where.Gazetteer:
        if self._gazetteer is None:
            self._gazetteer = where.Gazetteer()
        return self._gazetteer


def ask(ctx: Context, stage: str, payload: str, extra: str = "") -> list[dict[str, Any]] | None:
    """One call of ``stage``: the list the answer holds, or None when the answer is broken (cut off, not
    JSON, not the expected shape). Raises :class:`Stop` when the budget or the credit stops the run, and
    what the call raised otherwise."""
    from neurodb.fmm.ai import budget

    tokens = estimate(stage, payload + extra, ctx.setting)
    if why := blocked(tokens, ctx.setting):
        raise Stop(why)
    instructions = prompts.compose(ctx.setting.prompt(PROMPT_OF[stage]))
    request = {
        "model": ctx.model,
        "instructions": f"{instructions}\n\n{extra}".strip(),
        "input": [{"role": "user", "content": payload}],
        "reasoning": {"effort": EFFORT},
        "max_output_tokens": MAX_OUTPUT[stage],
        "store": False,
        "prompt_cache_key": f"neurodb-doc-review-{stage}",
        "text": {
            "format": {
                "type": "json_schema",
                "name": f"doc_review_{stage}",
                "strict": True,
                "schema": SCHEMAS[stage],
            }
        },
    }
    try:
        response = ctx.api.responses.create(**request)
    except Exception as exc:
        if budget.trip(exc):
            raise Stop(PAUSED) from exc
        raise
    answered = getattr(response, "usage", None)
    usage.record(usage.DOC_REVIEW, ctx.model, answered)
    ctx.tally["calls"] += 1
    ctx.tally["tokens"] += sum(usage.split(answered))
    if getattr(response, "status", "") == "incomplete":
        return None
    try:
        data = json.loads(getattr(response, "output_text", "") or "")
    except (TypeError, ValueError):
        return None
    rows = data.get(ANSWER_KEYS[stage]) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict)]


def _str(row: dict[str, Any], name: str, limit: int) -> str:
    value = row.get(name)
    return " ".join(str(value).split())[:limit] if isinstance(value, str | int | float) else ""


# ------------------------------------------------------------------------------------------ findings
def _findings_extra(ctx: Context) -> str:
    return (
        f"List at most {ctx.setting.max_findings_per_chunk} findings for this part.\n"
        "The tags (Programme › Subtopic › Tag):\n" + "\n".join(ctx.topics.lines)
    )


def _read_part(ctx: Context, document: Document, part: where.Part, extra: str) -> tuple[list, int]:
    """The findings of one part (unsaved), and the characters left unread (the part, or a half of it,
    whose answer stayed broken)."""

    def once(piece: where.Part) -> list[dict[str, Any]] | None:
        try:
            return ask(
                ctx, FINDINGS, f"Document: {document.title}\n\n<document>\n{piece.text}\n</document>", extra
            )
        except Stop:
            raise
        except Exception as exc:  # a failed call counts as a broken answer: tried again as halves
            logger.warning(
                "document review: a call failed for document %s: %s", document.pk, type(exc).__name__
            )
            ctx.tally["call_errors"] += 1
            return None

    found: list[DocumentFinding] = []
    rows = once(part)
    pieces = [(part, rows)]
    if rows is None:
        ctx.tally["retried_as_halves"] += 1
        pieces = [(half, once(half)) for half in where.halves(part)] or [(part, None)]
    unread = 0
    for piece, piece_rows in pieces:
        _touch(document)
        if piece_rows is None:
            unread += piece.chars
            continue
        for row in piece_rows[: ctx.setting.max_findings_per_chunk]:
            finding = _finding(ctx, document, piece, row)
            if finding is not None:
                found.append(finding)
    return found, unread


def _finding(
    ctx: Context, document: Document, piece: where.Part, row: dict[str, Any]
) -> DocumentFinding | None:
    text = _str(row, "text", TEXT_CHARS)
    if not text:
        return None
    quote = _str(row, "quote", QUOTE_CHARS)
    tag = _str(row, "tag", SHORT_CHARS)
    category = row.get("category") if row.get("category") in CATEGORIES else FindingCategory.OBSERVATION
    kind = row.get("kind") if row.get("kind") in ("reported", "interpreted") else "interpreted"
    return DocumentFinding(
        document=document,
        category=category,
        topic=ctx.topics.find(tag),
        tag_text=tag,
        text=text,
        quote=quote,
        kind=kind,
        chunk_from=piece.first,
        chunk_to=piece.last,
        place_text=_str(row, "place", SHORT_CHARS),
        date_text=_str(row, "date", 100),
        model_key=key(text, quote),
    )


def find(
    ctx: Context, document: Document, document_parts: list[where.Part] | None = None
) -> tuple[list[DocumentFinding], dict[str, Any]]:
    """The findings the AI reads in the document (unsaved), and the stage's note."""
    if document_parts is None:
        document_parts = where.parts(document, ctx.setting.chunk_size)
    total = sum(p.chars for p in document_parts) or 1
    extra = _findings_extra(ctx)
    found: list[DocumentFinding] = []
    unread = 0
    for part in document_parts:
        part_found, part_unread = _read_part(ctx, document, part, extra)
        found.extend(part_found)
        unread += part_unread
    read_share = round(100 * (total - unread) / total)
    note: dict[str, Any] = {"parts": len(document_parts), "read_share": read_share, "found": len(found)}
    if unread >= total:
        note.update(state=FAILED, error="No part of the document got a usable answer from the AI.")
    elif unread:
        note.update(state=PARTLY, error=f"Only {read_share}% read: some parts got no usable answer.")
    else:
        note["state"] = YES
    return found, note


# ------------------------------------------------------------------------------------------ locate
def place_all(ctx: Context, document: Document, findings: list[DocumentFinding]) -> dict[str, Any]:
    index = where.TextIndex(document)
    sheets = where.sheet_names(document)
    for finding in findings:
        where.locate(finding, index, ctx.gazetteer, sheets, ctx.topics.other.pk)
    exact = sum(f.quote_found for f in findings)
    return {"state": YES, "quotes_found": exact, "findings": len(findings)}


# ------------------------------------------------------------------------------------------ summary
def _numbered(findings: list[DocumentFinding]) -> str:
    lines = []
    for n, f in enumerate(findings, start=1):
        about = " · ".join(
            p for p in (f.get_category_display(), f.topic.name if f.topic_id else "", f.page_label) if p
        )
        lines.append(f"[{n}] ({about}) {f.text[:FINDING_LINE_CHARS]}")
    return "\n".join(lines)


def statements_limit(setting: DocumentReviewSettings, findings: int) -> int:
    """At most the setting's number (20 by default), fewer for a short document, never under 5."""
    wanted = max(DocumentReviewSettings.MIN_STATEMENTS, -(-findings // STATEMENTS_PER_FINDINGS))
    return min(setting.statements_limit, wanted)


def _cites(row: dict[str, Any], findings: list[DocumentFinding]) -> list[DocumentFinding]:
    numbers = row.get("cites") if isinstance(row.get("cites"), list) else []
    out = []
    for n in numbers:
        if (
            isinstance(n, int)
            and not isinstance(n, bool)
            and 1 <= n <= len(findings)
            and findings[n - 1] not in out
        ):
            out.append(findings[n - 1])
    return out


def _main_topic(cited: list[DocumentFinding], other: Topic) -> Topic | None:
    topics = Counter(f.topic for f in cited if f.topic_id and f.topic_id != other.pk)
    if topics:
        return topics.most_common(1)[0][0]
    return other if cited else None


def summarise(ctx: Context, document: Document, findings: list[DocumentFinding]):
    """The key statements (unsaved, with the findings each cites), and the stage's note."""
    if not findings:
        return [], {"state": YES, "statements": 0, "note": "no findings to summarise"}
    limit = statements_limit(ctx.setting, len(findings))
    payload = f"Document: {document.title}\n\nFindings:\n{_numbered(findings)}"
    try:
        rows = ask(ctx, SUMMARY, payload, f"Write at most {limit} statements.")
    except Stop:
        raise
    except Exception as exc:
        logger.warning(
            "document review: the summary failed for document %s: %s", document.pk, type(exc).__name__
        )
        rows = None
    _touch(document)
    if rows is None:
        return [], {"state": FAILED, "error": "The AI's answer for the key statements was not usable."}
    out = []
    for row in rows[:limit]:
        text = _str(row, "text", TEXT_CHARS)
        if not text:
            continue
        cited = _cites(row, findings)
        urgency = row.get("urgency") if isinstance(row.get("urgency"), int) else 0
        statement = DocumentStatement(
            document=document,
            text=text,
            urgency=max(0, min(100, urgency)),
            category=row.get("category") if row.get("category") in CATEGORIES else "",
            topic=_main_topic(cited, ctx.topics.other),
            place_text=_str(row, "place", SHORT_CHARS),
            date_text=_str(row, "date", 100),
        )
        out.append((statement, cited))
    return out, {"state": YES, "statements": len(out)}


# ------------------------------------------------------------------------------------------ enrichment
def _owner(text: str) -> str:
    """The owner as written, without a person's name, e-mail address or phone number."""
    from neurodb.fmm import privacy

    cleaned = privacy.clean(text, SHORT_CHARS)[0].strip() if text else ""
    return cleaned or DocumentActionPoint.UNASSIGNED


def enrich(ctx: Context, document: Document, findings: list[DocumentFinding]):
    """The action points (unsaved, with their cited findings) and the derived findings they need, and the
    stage's note."""
    if not findings:
        return [], [], {"state": YES, "action_points": 0, "note": "no findings to draw action points from"}
    payload = f"Document: {document.title}\n\nFindings:\n{_numbered(findings)}"
    try:
        rows = ask(ctx, ENRICHMENT, payload, f"List at most {MAX_ACTION_POINTS} action points.")
    except Stop:
        raise
    except Exception as exc:
        logger.warning(
            "document review: the enrichment failed for document %s: %s", document.pk, type(exc).__name__
        )
        rows = None
    _touch(document)
    if rows is None:
        return [], [], {"state": FAILED, "error": "The AI's answer for the action points was not usable."}
    points, derived_findings = [], []
    for row in rows[:MAX_ACTION_POINTS]:
        action = _str(row, "action", TEXT_CHARS)
        if not action:
            continue
        cited = _cites(row, findings)
        deadline = _str(row, "deadline", 100)
        point = DocumentActionPoint(
            document=document,
            action=action,
            action_key=key(action),
            owner_text=_owner(_str(row, "owner", SHORT_CHARS)),
            deadline_text=deadline,
            deadline_date=where.period(deadline, end=True),
            priority=row.get("priority") if row.get("priority") in PRIORITIES else "unrated",
            topic=_main_topic(cited, ctx.topics.other),
        )
        point.derived = not any(f.category == FindingCategory.ACTION_POINT for f in cited)
        if point.derived and cited:  # a finding records the commitment, on the words it was drawn from
            source = cited[0]
            made = DocumentFinding(
                document=document,
                category=FindingCategory.ACTION_POINT,
                topic=source.topic,
                tag_text=source.tag_text,
                text=action,
                quote=source.quote,
                kind=DocumentFinding.Kind.INTERPRETED,
                chunk_from=source.chunk_from,
                chunk_to=source.chunk_to,
                page_from=source.page_from,
                page_to=source.page_to,
                place_text=source.place_text,
                date_text=deadline,
                derived=True,
                model_key=key(action, source.quote),
            )
            derived_findings.append(made)
            cited = [*cited, made]
        points.append((point, cited))
    return points, derived_findings, {"state": YES, "action_points": len(points)}


# ------------------------------------------------------------------------------------------ writing
def _touch(document: Document) -> None:
    now = timezone.now()
    document.review_progress_at = now
    Document.objects.filter(pk=document.pk).update(review_progress_at=now)


@dataclass
class Kept:
    """What people decided, carried over a new analysis."""

    findings: dict[str, DocumentFinding]
    statements: dict[str, tuple[str, Any, Any]]
    actions: dict[str, tuple[str, Any, Any]]

    @classmethod
    def read(cls, document: Document) -> Kept:
        return cls(
            findings={f.model_key: f for f in document.findings.filter(manual=False).exclude(model_key="")},
            statements={
                key(s.text): (s.verdict, s.reviewed_by_id, s.reviewed_at)
                for s in document.statements.exclude(verdict=Verdict.UNREVIEWED)
            },
            actions={
                a.action_key: (a.status, a.status_by_id, a.status_at)
                for a in document.review_action_points.all()
            },
        )

    def apply_finding(self, finding: DocumentFinding) -> None:
        old = self.findings.get(finding.model_key)
        if old is None:
            return
        finding.verdict, finding.reviewed_by_id, finding.reviewed_at = (
            old.verdict,
            old.reviewed_by_id,
            old.reviewed_at,
        )
        if old.edited_by_id or old.edited_at:  # a person's edit of the AI's finding stays
            for name in ("text", "quote", "kind", "category", "topic_id", "place_text", "date_text"):
                setattr(finding, name, getattr(old, name))
            finding.edited_by_id, finding.edited_at = old.edited_by_id, old.edited_at

    def apply_statement(self, statement: DocumentStatement) -> None:
        if old := self.statements.get(key(statement.text)):
            statement.verdict, statement.reviewed_by_id, statement.reviewed_at = old

    def apply_action(self, point: DocumentActionPoint) -> None:
        if old := self.actions.get(point.action_key):
            point.status, point.status_by_id, point.status_at = old


def _save_findings(findings: list[DocumentFinding], start: int = 0) -> None:
    for n, finding in enumerate(findings, start=start):
        finding.position = n
    DocumentFinding.objects.bulk_create(findings)


def _save_statements(statements) -> None:
    for n, (statement, _cited) in enumerate(statements):
        statement.position = n
    DocumentStatement.objects.bulk_create([s for s, _ in statements])
    links = [
        DocumentStatement.cites.through(documentstatement_id=s.pk, documentfinding_id=f.pk)
        for s, cited in statements
        for f in cited
        if f.pk
    ]
    DocumentStatement.cites.through.objects.bulk_create(links)


def _save_actions(points) -> None:
    for n, (point, _cited) in enumerate(points):
        point.position = n
    DocumentActionPoint.objects.bulk_create([p for p, _ in points])
    links = [
        DocumentActionPoint.cites.through(documentactionpoint_id=p.pk, documentfinding_id=f.pk)
        for p, cited in points
        for f in cited
        if f.pk
    ]
    DocumentActionPoint.cites.through.objects.bulk_create(links)


def _usable(findings: list[DocumentFinding]) -> list[DocumentFinding]:
    """The findings the summary and the enrichment read: not rejected, not derived."""
    return [f for f in findings if f.verdict != Verdict.REJECTED and not f.derived]


def _status(notes: dict[str, Any]) -> str:
    states = {stage: (notes.get(stage) or {}).get("state") for stage in STAGES}
    if states[TEXT] == FAILED or states[FINDINGS] == FAILED:
        return Document.ReviewStatus.FAILED
    if any(state in (PARTLY, FAILED) for state in states.values()):
        return Document.ReviewStatus.PARTLY
    return Document.ReviewStatus.DONE


def _finish(document: Document, notes: dict[str, Any], status: str | None = None) -> None:
    notes["at"] = timezone.now().isoformat(timespec="minutes")
    document.review_stage_notes = notes
    document.review_status = status or _status(notes)
    document.reviewed_at = timezone.now()
    document.save(
        update_fields=[
            "review_stage_notes",
            "review_status",
            "reviewed_at",
            "reviewed_text_hash",
            "updated_at",
        ]
    )


def _text_note(document: Document) -> dict[str, Any] | None:
    """The text stage: None while the knowledge base is still reading the document."""
    if document.status in (Document.Status.PENDING, Document.Status.INDEXING):
        return None
    if document.status == Document.Status.FAILED or not (document.text or "").strip():
        reason = document.error or "The document holds no text."
        return {"state": FAILED, "error": f"The text could not be read: {reason}"[:500]}
    return {"state": YES, "characters": document.characters, "pages": document.pages}


def analyse(ctx: Context, document: Document) -> str:
    """Every stage of one document (see the module's notes); its review status after. Raises
    :class:`Stop` (the document is left as it was) when the budget or the credit stops the run."""
    before = document.review_status
    text = _text_note(document)
    if text is None:
        notes = {**(document.review_stage_notes or {}), "waiting": NOT_READ_YET}
        Document.objects.filter(pk=document.pk).update(review_stage_notes=notes)
        return before
    if text["state"] == FAILED:
        _finish(document, {TEXT: text})
        return document.review_status
    document_parts = where.parts(document, ctx.setting.chunk_size)
    tokens, (cap, too_long) = whole_estimate(ctx, document_parts), day_cap(ctx.setting)
    if tokens > cap:  # it would spend the whole day's budget each night and never be finished
        note = {"state": FAILED, "error": too_long.format(tokens=tokens, cap=cap)}
        _finish(document, {**(document.review_stage_notes or {}), TEXT: text, FINDINGS: note})
        return document.review_status
    document.review_status, document.review_progress_at = Document.ReviewStatus.RUNNING, timezone.now()
    document.save(update_fields=["review_status", "review_progress_at", "updated_at"])
    notes: dict[str, Any] = {TEXT: text}
    try:
        found, notes[FINDINGS] = find(ctx, document, document_parts)
        if notes[FINDINGS]["state"] == FAILED:  # nothing read: what an earlier analysis found stays
            _finish(document, notes)
            return document.review_status
        kept = Kept.read(document)
        for finding in found:
            kept.apply_finding(finding)
        manual = list(document.findings.filter(manual=True).select_related("topic"))
        notes[PLACES] = place_all(ctx, document, found + manual)
        usable = _usable(manual + found)
        statements, notes[SUMMARY] = summarise(ctx, document, usable)
        points, derived, notes[ENRICHMENT] = enrich(ctx, document, usable)
    except Stop as reason:
        waiting = {**(document.review_stage_notes or {}), "waiting": str(reason)}
        Document.objects.filter(pk=document.pk, review_status=Document.ReviewStatus.RUNNING).update(
            review_status=before, review_stage_notes=waiting
        )  # unless a person took it out of the review meanwhile
        document.review_status = before
        raise
    if derived:
        place_all(ctx, document, derived)
    with transaction.atomic():
        current = (
            Document.objects.select_for_update()
            .filter(pk=document.pk)
            .values("review_batch_id", "review_status", "text")
            .first()
        )
        if left_review(current):  # taken out or made a reference while the AI was reading: not kept
            if current is not None and current["review_status"] == Document.ReviewStatus.RUNNING:
                Document.objects.filter(pk=document.pk).update(
                    review_status=Document.ReviewStatus.NOT_IN_REVIEW
                )  # its batch was deleted
            logger.info("document review: document %s left the review while it was analysed", document.pk)
            return Document.ReviewStatus.NOT_IN_REVIEW
        alive = set(document.findings.filter(manual=True).values_list("pk", flat=True))
        if len(alive) != len(manual):  # a person deleted one of their findings meanwhile
            manual = [f for f in manual if f.pk in alive]
            statements = [(s, _alive(cited, alive)) for s, cited in statements]
            points = [(p, _alive(cited, alive)) for p, cited in points]
        kept = Kept.read(document)  # again: what people decided while the AI was reading
        for finding in found + derived:
            kept.apply_finding(finding)
        for statement, _cited in statements:
            kept.apply_statement(statement)
        for point, _cited in points:
            kept.apply_action(point)
        document.findings.filter(manual=False).delete()  # their statements' and action points' links too
        document.statements.all().delete()
        document.review_action_points.all().delete()
        DocumentFinding.objects.bulk_update(
            manual, ["page_from", "page_to", "page_label", "quote_found", "exact_page", "place_match",
                     "governorate_id", "governorate_name", "district_id", "district_name", "finding_date",
                     "evidence"],
        )  # fmt: skip
        _save_findings(found + derived, start=len(manual))
        _save_statements(statements)
        _save_actions(points)
        document.reviewed_text_hash = text_hash(document)
        if hashlib.sha256((current["text"] or "").encode()).hexdigest() != document.reviewed_text_hash:
            notes["waiting"] = TEXT_CHANGED  # read again meanwhile: kept for now, analysed again next
            _finish(document, notes, status=Document.ReviewStatus.PENDING)
        else:
            _finish(document, notes)
    return document.review_status


def left_review(current: dict[str, Any] | None) -> bool:
    """A document (its batch and status, read again) no longer in the review: deleted, out of its batch
    or a reference."""
    return (
        current is None
        or not current["review_batch_id"]
        or current["review_status"] in (Document.ReviewStatus.NOT_IN_REVIEW, Document.ReviewStatus.REFERENCE)
    )


def _alive(cited: list[DocumentFinding], manual_ids: set[int]) -> list[DocumentFinding]:
    """The cited findings without the manual ones deleted meanwhile."""
    return [f for f in cited if not f.manual or f.pk in manual_ids]


def enrich_again(ctx: Context, document: Document) -> str:
    """The action points again from the stored findings (one call), statuses kept."""
    stored = list(document.findings.select_related("topic").order_by("position", "pk"))
    usable = _usable(stored)
    points, derived, note = enrich(ctx, document, usable)
    if note["state"] == FAILED:
        notes = {**(document.review_stage_notes or {}), ENRICHMENT: note}
        _finish(document, notes)
        return document.review_status
    if derived:
        place_all(ctx, document, derived)
    with transaction.atomic():
        kept = Kept.read(document)
        for finding in derived:
            kept.apply_finding(finding)
        for point, _cited in points:
            kept.apply_action(point)
        document.findings.filter(derived=True, manual=False).delete()
        document.review_action_points.all().delete()
        _save_findings(derived, start=len(stored))
        _save_actions(points)
        _finish(document, {**(document.review_stage_notes or {}), ENRICHMENT: note})
    return document.review_status


def locate_again(ctx: Context, document: Document) -> int:
    """Where each finding is, its place and evidence again (no AI); how many findings."""
    findings = list(document.findings.all())
    place_all(ctx, document, findings)
    DocumentFinding.objects.bulk_update(
        findings,
        ["page_from", "page_to", "page_label", "quote_found", "exact_page", "place_match", "governorate_id",
         "governorate_name", "district_id", "district_name", "finding_date", "evidence"],
        batch_size=500,
    )  # fmt: skip
    return len(findings)


# ------------------------------------------------------------------------------------------ the job
@contextmanager
def locked(wait: bool = False):
    """The review's lock: True when held. ``wait``: wait for a run in progress (a single document's
    run), else give up at once (False)."""
    if connection.vendor != "postgresql":
        yield True
        return
    with connection.cursor() as cursor:
        if wait:
            cursor.execute("SELECT pg_advisory_lock(%s)", [LOCK_ID])
            got = True
        else:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            got = bool(cursor.fetchone()[0])
    try:
        yield got
    finally:
        if got:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])


def in_review():
    """The documents the review reads: in a batch not archived, and not a reference."""
    return Document.objects.filter(review_batch__isnull=False, review_batch__archived=False).exclude(
        review_status=Document.ReviewStatus.REFERENCE
    )


def left_behind(now: datetime.datetime | None = None) -> int:
    """Mark failed, with the reason, the documents being analysed with no progress for 30 minutes."""
    now = now or timezone.now()
    since = now - datetime.timedelta(minutes=STALE_MINUTES)
    stale = Document.objects.filter(review_status=Document.ReviewStatus.RUNNING).filter(
        Q(review_progress_at__lt=since) | Q(review_progress_at__isnull=True)
    )
    count = 0
    for document in stale.only("pk", "review_stage_notes"):  # not the text: it runs at each Documents tab
        notes = {
            **(document.review_stage_notes or {}),
            "stopped": STALE,
            "at": now.isoformat(timespec="minutes"),
        }
        Document.objects.filter(pk=document.pk).update(
            review_status=Document.ReviewStatus.FAILED, review_stage_notes=notes
        )
        count += 1
    return count


WAITING = (
    Document.ReviewStatus.PENDING,
    Document.ReviewStatus.FAILED,
    Document.ReviewStatus.PARTLY,
    Document.ReviewStatus.NOT_IN_REVIEW,
)


def documents_for(mode: str, batch: int | None = None, document: int | None = None):
    """The documents a run of ``mode`` reaches; ``document``: that one (any status in full and pending
    mode, as its *Analyse* button asks), when it is in a batch and not a reference."""
    if document is not None:
        qs = Document.objects.filter(pk=document, review_batch__isnull=False).exclude(
            review_status=Document.ReviewStatus.REFERENCE
        )
    else:
        qs = in_review()
        if batch is not None:
            qs = qs.filter(review_batch_id=batch)
        if mode == PENDING:
            qs = qs.filter(review_status__in=WAITING)
    if mode == ENRICH:  # the action points again needs findings from an analysis
        qs = qs.filter(review_status__in=[Document.ReviewStatus.DONE, Document.ReviewStatus.PARTLY])
    elif mode == LOCATE:
        qs = qs.filter(findings__isnull=False).distinct()
    return qs.order_by("pk")


def run(
    mode: str = PENDING, batch: int | None = None, document: int | None = None, triggered_by: str = "schedule"
):
    """The review (see the module's notes) as one ``SyncRun`` (target: the mode): ``rows_in`` the
    documents reached, ``rows_written`` those analysed (fully or partly), ``rows_failed`` those that
    failed; its details say how many of each, the calls and tokens, and why it stopped."""
    from neurodb.assistant import agent
    from neurodb.core.models import SyncRun
    from neurodb.integrations.runs import fail, finish_by_counts, new_run

    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    sync_run = new_run(
        SyncRun.Job.DOC_REVIEW, mode if document is None else f"{mode} #{document}", triggered_by
    )
    clock = time.monotonic()
    setting = DocumentReviewSettings.load()
    ctx = Context(api=None, setting=setting, model=settings.AI_ASSISTANT_MODEL)

    def done(**extra):
        details = {k: v for k, v in ctx.tally.items()}
        return {**details, **extra, "duration_ms": int((time.monotonic() - clock) * 1000)}

    try:
        ctx.tally["stale_failed"] = left_behind()
    except Exception as exc:
        return fail(sync_run, exc, **done())
    if mode != LOCATE:
        on, why = switched_on(setting)
        if on:
            try:
                ctx.api = agent.client().with_options(timeout=TIMEOUT_SECONDS, max_retries=1)
            except agent.AssistantUnavailable:
                on, why = False, AI_OFF
        if not on:
            sync_run.finish(SyncRun.Status.SUCCEEDED, skipped_reason=why, **done())
            return sync_run
    stopped = ""
    try:
        stopped = _run_all(ctx, sync_run, mode, batch, document)
    except Exception as exc:
        return fail(sync_run, exc, **done())
    return finish_by_counts(sync_run, **done(stopped=stopped))


def _run_all(ctx: Context, sync_run, mode: str, batch: int | None, document: int | None) -> str:
    """Each document of the mode in turn, read again when its turn comes (a long run: it may have been
    taken out, made a reference or read again meanwhile); why the run stopped ("" when every one was
    reached). A full run stopped by the budget leaves the documents it did not reach waiting, so the
    nightly run goes on with them."""
    chosen = list(documents_for(mode, batch, document).values_list("pk", flat=True))
    for n, pk in enumerate(chosen):
        doc = documents_for(mode, batch, document).filter(pk=pk).first()
        if doc is None:
            continue
        sync_run.rows_in += 1
        if mode == LOCATE:
            ctx.tally["findings_located"] += locate_again(ctx, doc)
            sync_run.rows_written += 1
            continue
        try:
            status = enrich_again(ctx, doc) if mode == ENRICH else analyse(ctx, doc)
        except Stop as reason:
            if mode == FULL:
                ctx.tally["left_waiting"] = (
                    in_review()
                    .filter(pk__in=chosen[n:])
                    .exclude(review_status=Document.ReviewStatus.RUNNING)
                    .update(review_status=Document.ReviewStatus.PENDING)
                )
            return str(reason)
        except Exception:
            logger.exception("document review: document %s failed", doc.pk)
            Document.objects.filter(pk=doc.pk, review_batch__isnull=False).exclude(
                review_status=Document.ReviewStatus.REFERENCE
            ).update(
                review_status=Document.ReviewStatus.FAILED,
                review_stage_notes={
                    **(doc.review_stage_notes or {}),
                    "error": "An unexpected error stopped it.",
                },
            )
            status = Document.ReviewStatus.FAILED
        if status == Document.ReviewStatus.FAILED:
            sync_run.rows_failed += 1
            ctx.tally["failed"] += 1
        elif status in (Document.ReviewStatus.DONE, Document.ReviewStatus.PARTLY):
            sync_run.rows_written += 1
            ctx.tally["partly" if status == Document.ReviewStatus.PARTLY else "analysed"] += 1
        else:
            ctx.tally["waiting"] += 1
    return ""


# ------------------------------------------------------------------------------------------ for the pages
def start(document: Document, triggered_by: str = "page") -> None:
    """Analyse one document now, in the background (its *Analyse* / *Re-analyse* button)."""
    from neurodb.integrations import background

    Document.objects.filter(pk=document.pk).update(review_status=Document.ReviewStatus.PENDING)
    background.start_command(
        "review_documents", "--document", str(document.pk), "--triggered-by", triggered_by
    )


def put_in_batch(documents, batch) -> int:
    """Put knowledge base documents in a batch, waiting to be analysed (a reference stays one)."""
    count = 0
    for document in documents:
        document.review_batch = batch
        if document.review_status in (Document.ReviewStatus.NOT_IN_REVIEW, ""):
            document.review_status = Document.ReviewStatus.PENDING
        document.save(update_fields=["review_batch", "review_status", "updated_at"])
        count += 1
    return count


def text_read(document: Document) -> bool:
    """After the knowledge base read a document again: when it is in the review and its text is not the
    one analysed, it waits to be analysed again (the next run). True when it does."""
    analysed = (Document.ReviewStatus.DONE, Document.ReviewStatus.PARTLY, Document.ReviewStatus.FAILED)
    # as they are now: the knowledge base may have read the document for minutes
    document.refresh_from_db(fields=["review_batch", "review_status", "reviewed_text_hash"])
    if not document.review_batch_id or document.review_status not in analysed:
        return False
    if document.reviewed_text_hash and document.reviewed_text_hash == text_hash(document):
        return False
    document.review_status = Document.ReviewStatus.PENDING
    document.save(update_fields=["review_status", "updated_at"])
    return True


def take_out(document: Document) -> None:
    """Out of its batch: no longer analysed; what was found stays with it, out of every view."""
    document.review_batch = None
    document.review_status = Document.ReviewStatus.NOT_IN_REVIEW
    document.save(update_fields=["review_batch", "review_status", "updated_at"])


def set_reference(document: Document, reference: bool) -> None:
    """Mark a document as a reference (kept in its batch, never analysed, out of every count) or not."""
    if reference:
        document.review_status = Document.ReviewStatus.REFERENCE
    elif document.review_status == Document.ReviewStatus.REFERENCE:
        document.review_status = (
            Document.ReviewStatus.DONE if document.findings.exists() else Document.ReviewStatus.PENDING
        )
    document.save(update_fields=["review_status", "updated_at"])


# ------------------------------------------------------------------------------------------ Ask NeuroDB
SEARCH_CANDIDATES = 400  # findings matching a word, ranked in Python


def shown_findings():
    """The findings the views and Ask NeuroDB use: of documents still in a batch and not a reference,
    and not rejected by a person."""
    return DocumentFinding.objects.filter(document__review_batch__isnull=False).exclude(
        Q(verdict=Verdict.REJECTED) | Q(document__review_status=Document.ReviewStatus.REFERENCE)
    )


def search_findings(query: str, batch: str | int | None = None, limit: int = 15) -> list[DocumentFinding]:
    """The findings whose words, quote, topic, place or document title hold the query's words, the most
    words matched first, then the best evidence; ``batch``: a batch's id or (part of) its name."""
    from .search import words

    found = shown_findings().select_related("document__review_batch", "topic__subtopic__programme")
    if batch not in (None, ""):
        text = str(batch).strip()
        named = Q(document__review_batch__name__icontains=text)
        found = found.filter(
            named | Q(document__review_batch_id=int(text)) if text.isdigit() and len(text) < 10 else named
        )
    terms = words(query or "")
    if not terms:
        return list(found.order_by("-evidence", "-pk")[:limit])
    fields = ("text", "quote", "topic__name", "topic__subtopic__name", "topic__subtopic__programme__name",
              "place_text", "document__title")  # fmt: skip
    match = Q()
    for term in terms:
        for name in fields:
            match |= Q(**{f"{name}__icontains": term})
    candidates = list(found.filter(match).order_by("-evidence", "-pk")[:SEARCH_CANDIDATES])

    def score(finding: DocumentFinding) -> tuple[int, int]:
        blob = " ".join(
            (finding.text, finding.quote, finding.topic.path, finding.place_text, finding.document.title)
        ).lower()
        return sum(term in blob for term in terms), finding.evidence

    candidates.sort(key=score, reverse=True)
    return candidates[:limit]
