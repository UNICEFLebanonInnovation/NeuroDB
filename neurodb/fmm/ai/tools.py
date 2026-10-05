"""The four field monitoring look-ups of Chat with Data, also offered to Ask NeuroDB.

- ``fm_summary`` counts the visits of the filter (visits, entities, rated, not monitored, average
  quality, high and amber urgency), optionally grouped;
- ``fm_visits`` lists visits as cards (``privacy.visit_card``: never a narrative, an answer or a
  person);
- ``fm_visit`` reads one visit: its entities with their notes, rule results, urgency, action points,
  HACT context and checklist answers;
- ``fm_search`` finds the visits whose notes or answers hold a word, with a short snippet.

**Scope.** Every look-up works within a :class:`ChatContext`: the page's filter, how many texts (notes,
answers, snippets) the answer may still read and how many visit cards one look-up returns. The chat binds
its context for each tool call (``RunOptions.tool_context`` → :func:`bind`), whatever thread runs the
answer, so the model can only narrow the page's filter (``Scope.narrow``), never widen it. Without a bound
context (Ask NeuroDB) the look-ups cover this calendar year in the whole country and **read no text at
all**: no narrative, no answer, no snippet; those are read in Monitoring insights.

Every result carries a ``label`` and a ``url`` (``/fmm/...``), and each look-up adds the visit keys it
returns to the context's ``seen``, which the chat's citation check reads. Texts are cleaned
(``privacy.clean``) and cut to ``FMM_NARRATIVE_CHARS`` here; the chat's filter (``privacy.chat_filter``)
cleans every string once more and makes the last check before the model reads a result. Nothing here
reads an eTools record's raw data or the people of a visit.
"""

from __future__ import annotations

import datetime
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import F
from django.http import QueryDict
from django.urls import reverse
from django.utils.dateformat import format as date_format

from neurodb.assistant.tools import ToolInputError
from neurodb.watch import people

from .. import metrics, privacy
from ..scope import KIND_LABELS, NONE, RATING_LABELS, RATINGS, RULES, STATUS_GROUPS, STATUS_LABELS, Scope

ASK_CARDS = 20  # visit cards per look-up when no chat is bound (Ask NeuroDB)
GROUPS_MAX = 40  # groups one summary returns
SNIPPET_CHARS = 200  # characters of a search snippet, cut around the word found
SEARCH_MIN, SEARCH_MAX = 3, 80  # characters of a search text
MAX_PLACEHOLDERS = 3  # a text naming more people or contacts than this is not sent

LIMIT_REACHED = "(not included: the limit of texts for one answer was reached)"
NARRATIVES_ELSEWHERE = "(narratives are available in Monitoring insights)"
ANSWERS_ELSEWHERE = "(answers are available in Monitoring insights)"
TOO_MANY_NAMES = "(not included: this text names people or contacts)"
SWITCHED_OFF = "Monitoring insights is switched off."
NO_PEOPLE = "Searching for people is not supported."


# ------------------------------------------------------------------------------------------ context
@dataclass
class ChatContext:
    """What one answer's look-ups may read: the page's filter (``scope``), the texts still allowed
    (``texts_left``; 0 when the AI's texts are off or for Ask NeuroDB), the most visit cards per look-up
    (``cards_max``), and what the look-ups returned so far: the visit keys (``seen``), every number
    (``numbers``, in ``watch.grounding``'s form with its roundings) and every url (``urls``)."""

    scope: Scope
    texts_left: int
    cards_max: int
    seen: set[str] = field(default_factory=set)
    numbers: set[str] = field(default_factory=set)
    urls: set[str] = field(default_factory=set)
    texts_sent: int = 0  # texts read in this answer (ChatQuestion.texts_sent)
    privacy_blocked: int = 0  # look-ups stopped by the last check (privacy.chat_filter)

    def take_text(self) -> bool:
        """One text may be read (and is counted); False when the answer's limit is reached."""
        if self.texts_left <= 0:
            return False
        self.texts_left -= 1
        self.texts_sent += 1
        return True


_CTX: ContextVar[ChatContext | None] = ContextVar("fmm_chat", default=None)


@contextmanager
def bind(ctx: ChatContext) -> Iterator[None]:
    """Make ``ctx`` the context of the look-ups run inside the block (one tool call of the chat)."""
    token = _CTX.set(ctx)
    try:
        yield
    finally:
        _CTX.reset(token)


def bound() -> bool:
    """A chat's context is bound (the look-up runs for Chat with Data, not for Ask NeuroDB)."""
    return _CTX.get() is not None


def current() -> ChatContext:
    """The bound context, else Ask NeuroDB's: this calendar year in the whole country, no text, at most
    ``ASK_CARDS`` visit cards per look-up."""
    ctx = _CTX.get()
    if ctx is not None:
        return ctx
    return ChatContext(scope=Scope.from_params(QueryDict("section=")), texts_left=0, cards_max=ASK_CARDS)


# ------------------------------------------------------------------------------------------ arguments
def _switched_off() -> dict[str, str] | None:
    return None if settings.FMM_ENABLED else {"error": SWITCHED_OFF}


def _folded(text: Any) -> str:
    return " ".join(people.fold(str(text or "")).split())


def _choice(given: str, names: list[str], what: str) -> str:
    """The name of ``names`` that ``given`` means (case and accents ignored; "none" for those without
    one); an input error listing the names otherwise."""
    wanted = _folded(given)
    if wanted in ("none", f"no {what}", f"without {what}"):
        return NONE
    for name in names:
        if _folded(name) == wanted:
            return name
    within = [name for name in names if wanted and wanted in _folded(name)]
    if len(within) == 1:
        return within[0]
    shown = ", ".join(names[:40]) or "none"
    raise ToolInputError(f"No {what} named {given!r} has visits. The {what}s with visits: {shown}.")


def _section(given: str | None) -> tuple[str, ...]:
    if not given:
        return ()
    from ..scope import options

    return (_choice(given, options()["sections"], "section"),)


def _office(given: str | None) -> tuple[str, ...]:
    if not given:
        return ()
    from ..scope import options

    return (_choice(given, options()["offices"], "office"),)


def _governorate(given: str | None) -> str:
    if not given:
        return ""
    from neurodb.reports.overview import governorate_key, governorate_names

    if _folded(given) in ("none", "not located"):
        return NONE
    known = governorate_names()
    key = governorate_key(given)
    if key not in known:
        raise ToolInputError(
            f"No governorate named {given!r}. The governorates: {', '.join(known.values())}."
        )
    return key


def _partner(given: str | None) -> tuple[int, ...]:
    """The partner a name, short name, vendor number or id names, among the partners with visits."""
    if not given:
        return ()
    from neurodb.partnerships.models import PartnerOrganization

    from ..scope import options

    ids = [pk for pk, _name in options()["partners"]]
    rows = list(
        PartnerOrganization.objects.filter(pk__in=ids).values_list(
            "pk", "name", "short_name", "vendor_number"
        )
    )
    text = str(given).strip()
    if text.isdigit():
        for pk, _name, _short, vendor in rows:
            if str(pk) == text or (vendor or "") == text:
                return (pk,)
    wanted = _folded(text)
    exact = [
        pk for pk, name, short, vendor in rows if wanted in (_folded(name), _folded(short), _folded(vendor))
    ]
    if exact:
        return (exact[0],)
    within = [pk for pk, name, short, _vendor in rows if wanted and wanted in _folded(f"{name} {short}")]
    if len(within) == 1:
        return (within[0],)
    if within:
        names = sorted(name for pk, name, _s, _v in rows if pk in within)
        raise ToolInputError(f"Several partners match {given!r}: {', '.join(names[:10])}. Give one name.")
    raise ToolInputError(f"No partner with visits matches {given!r}.")


def _date(given: str | None, name: str) -> datetime.date | None:
    if not given:
        return None
    try:
        return datetime.date.fromisoformat(str(given).strip()[:10])
    except ValueError:
        raise ToolInputError(f"Argument '{name}' must be a date written YYYY-MM-DD.") from None


def _narrowed(ctx: ChatContext, **args: Any) -> Scope:
    """The bound scope narrowed by the look-up's arguments (never widened)."""
    return ctx.scope.narrow(
        start=_date(args.get("period_from"), "period_from"),
        end=_date(args.get("period_to"), "period_to"),
        sections=_section(args.get("section")),
        governorate=_governorate(args.get("governorate")),
        offices=_office(args.get("office")),
        partners=_partner(args.get("partner")),
        ratings=args.get("rating"),
        statuses=args.get("status"),
        flag=args.get("flag"),
    )


def _dashboard(scope: Scope, **extra: str) -> str:
    from urllib.parse import urlencode

    return f"{reverse('fmm:dashboard')}?{urlencode([*scope.pairs(), *extra.items()])}"


def _visit_url(key: str) -> str:
    return reverse("fmm:visit", args=[key])


def _quality(value: Any) -> float | None:
    return None if value is None else float(value)


# ------------------------------------------------------------------------------------------ fm_summary
class _Group:
    __slots__ = ("high", "q_n", "q_sum", "ratings", "visits")

    def __init__(self) -> None:
        self.visits = 0
        self.q_sum = Decimal(0)
        self.q_n = 0
        self.high = 0
        self.ratings: Counter = Counter()

    def add(self, quality: Any, rating: str, urgent: bool) -> None:
        self.visits += 1
        if quality is not None:
            self.q_sum += Decimal(str(quality))
            self.q_n += 1
        self.ratings[rating or "not_monitored"] += 1
        self.high += urgent


GROUP_COLUMNS = (
    "end_date",
    "section_names",
    "governorate_key",
    "governorate_name",
    "offices",
    "partner_ids",
    "rating",
    "entity_kinds",
    "status_group",
    "quality_score",
    "urgency",
)


def _group_codes(group_by: str, row: dict[str, Any]) -> list[tuple[str, str]]:
    """(code, label) of each group a visit counts in (a visit with two sections counts in both)."""
    if group_by == "section":
        return [(name, name) for name in row["section_names"] or ()] or [(NONE, "No section")]
    if group_by == "governorate":
        key = row["governorate_key"]
        return [(key, row["governorate_name"] or key)] if key else [(NONE, "Not located")]
    if group_by == "office":
        return [(name, name) for name in row["offices"] or ()] or [(NONE, "Office not known")]
    if group_by == "partner":
        return [(str(pk), str(pk)) for pk in row["partner_ids"] or ()] or [(NONE, "No partner")]
    if group_by == "month":
        day = row["end_date"]
        return [(day.strftime("%Y-%m"), date_format(day, "M Y"))]
    if group_by == "rating":
        code = row["rating"] or "not_monitored"
        return [(code, RATING_LABELS.get(code, code))]
    if group_by == "entity_type":
        return [(kind, KIND_LABELS.get(kind, kind)) for kind in row["entity_kinds"] or ()] or [(NONE, "None")]
    if group_by == "status":
        code = row["status_group"] or "unknown"
        return [(code, STATUS_LABELS.get(code, code))]
    return []


def _groups(scope: Scope, group_by: str, limits: dict[str, int]) -> tuple[list[dict[str, Any]], int]:
    """The visits of ``scope`` counted per group, most visits first (months in order), at most
    GROUPS_MAX; and how many groups there are in all."""
    if group_by == "rule":
        return _rule_groups(scope)
    groups: dict[str, _Group] = {}
    labels: dict[str, str] = {}
    for values in scope.visits().order_by().values_list(*GROUP_COLUMNS).iterator(chunk_size=2000):
        row = dict(zip(GROUP_COLUMNS, values, strict=True))
        urgent = row["urgency"] >= limits["red"]
        for code, label in _group_codes(group_by, row):
            groups.setdefault(code, _Group()).add(row["quality_score"], row["rating"], urgent)
            labels[code] = label
    if group_by == "partner":
        from neurodb.partnerships.models import PartnerOrganization

        ids = [int(code) for code in groups if code.isdigit()]
        for pk, name in PartnerOrganization.objects.filter(pk__in=ids).values_list("pk", "name"):
            labels[str(pk)] = name
    out = [
        {
            "group": labels[code],
            "code": code,
            "visits": g.visits,
            "avg_quality": _quality(metrics.mean_quality(g.q_sum, g.q_n)),
            "scored_visits": g.q_n,
            **{rating: g.ratings[rating] for rating in RATINGS},
            "high_urgency": g.high,
        }
        for code, g in groups.items()
    ]
    if group_by == "month":
        out.sort(key=lambda g: g["code"])
    else:
        out.sort(key=lambda g: (-g["visits"], str(g["group"]).casefold()))
    return out[:GROUPS_MAX], len(out)


def _rule_groups(scope: Scope) -> tuple[list[dict[str, Any]], int]:
    from ..models import RuleSetting

    stats = metrics.rule_stats(scope)
    labels = dict(RuleSetting.objects.values_list("code", "label"))
    out = []
    for code in RULES:
        row = stats.get(code) or {}
        out.append(
            {
                "group": f"{code} {labels.get(code, '')}".strip(),
                "code": code,
                "flagged": row.get("fail", 0),
                "passed": row.get("pass", 0),
                "not_available": row.get("na", 0),
                "does_not_apply": row.get("nap", 0),
                "switched_off": row.get("off", 0),
            }
        )
    return out, len(out)


def fm_summary(
    group_by: str = "none",
    period_from: str | None = None,
    period_to: str | None = None,
    section: str | None = None,
    governorate: str | None = None,
) -> dict[str, Any]:
    if (off := _switched_off()) is not None:
        return off
    ctx = current()
    scope = _narrowed(
        ctx, period_from=period_from, period_to=period_to, section=section, governorate=governorate
    )
    limits = metrics.thresholds()
    k = metrics.kpis(scope, limits=limits)
    out: dict[str, Any] = {
        "label": "Monitoring visits",
        "filter": scope.label(),
        "period_from": scope.start.isoformat(),
        "period_to": scope.end.isoformat(),
        "visits": k["visits"],
        "visits_by_status": {row["group"]: row["n"] for row in k["by_status"]},
        "entities": k["entities"],
        "entities_rated": k["entities_rated"],
        "entities_not_monitored": k["entities_not_monitored"],
        "avg_quality": _quality(k["avg_quality"]),
        "scored_visits": k["scored"],
        "high_urgency": k["high_urgency"],
        "amber_urgency": k["amber"],
        "high_urgency_from": limits["red"],
        "amber_urgency_from": limits["amber"],
        "rules_version": k["rules_version"],
        "url": _dashboard(scope),
    }
    if scope.empty:
        out["hint"] = "What was asked is outside the page's filter, so no visit matches."
    if group_by and group_by != "none":
        rows, total = _groups(scope, group_by, limits)
        out["group_by"] = group_by
        out["groups"] = rows
        if total > len(rows):
            out["groups_total"] = total
        if group_by in ("section", "office", "partner", "entity_type"):
            out["hint"] = "A visit with several of these counts in each of them."
    return out


# ------------------------------------------------------------------------------------------ fm_visits
SORTS = {
    "urgency": (F("urgency").desc(), F("end_date").desc(nulls_last=True), "key"),
    "date": (F("end_date").desc(nulls_last=True), "key"),
    "quality": (F("quality_score").asc(nulls_last=True), F("end_date").desc(nulls_last=True), "key"),
}


def card(visit, names_: frozenset[str]) -> dict[str, Any]:
    """A visit as the AI reads it in a list: its card (``privacy.visit_card``) and its url."""
    return {**privacy.visit_card(visit, names_), "url": _visit_url(visit.key)}


def fm_visits(
    section: str | None = None,
    governorate: str | None = None,
    office: str | None = None,
    partner: str | None = None,
    rating: str | None = None,
    status: str | None = None,
    flag: str | None = None,
    min_urgency: int | None = None,
    sort: str = "urgency",
    limit: int = 10,
    period_from: str | None = None,
    period_to: str | None = None,
) -> dict[str, Any]:
    if (off := _switched_off()) is not None:
        return off
    ctx = current()
    scope = _narrowed(
        ctx,
        section=section,
        governorate=governorate,
        office=office,
        partner=partner,
        rating=rating,
        status=status,
        flag=flag,
        period_from=period_from,
        period_to=period_to,
    )
    qs = scope.visits()
    if min_urgency:
        qs = qs.filter(urgency__gte=min_urgency)
    total = qs.count()
    shown = max(1, min(limit or 10, ctx.cards_max or ASK_CARDS))
    visits = list(qs.select_related("partner", "pd").order_by(*SORTS.get(sort, SORTS["urgency"]))[:shown])
    names_ = privacy.names()
    cards = [card(v, names_) for v in visits]
    ctx.seen.update(v.key for v in visits)
    out: dict[str, Any] = {
        "label": "Monitoring visits",
        "filter": scope.label(),
        "total": total,
        "shown": len(cards),
        "visits": cards,
        "url": _dashboard(scope, tab="visits"),
    }
    if total > len(cards):
        out["hint"] = f"Only the first {len(cards)} of {total} are listed; narrow the question to see others."
    return out


# ------------------------------------------------------------------------------------------ fm_visit
def _text(ctx: ChatContext, raw: str, elsewhere: str, names_: frozenset[str]) -> str:
    """A note or an answer as this answer may read it: cleaned and cut, while texts are left; a short
    text saying why not otherwise (and for Ask NeuroDB, always)."""
    if not str(raw or "").strip():
        return ""
    if not bound():
        return elsewhere
    cleaned, placeholders = privacy.clean(raw, settings.FMM_NARRATIVE_CHARS, names_)
    if placeholders > MAX_PLACEHOLDERS:
        return TOO_MANY_NAMES
    if not ctx.take_text():
        return LIMIT_REACHED
    return cleaned


def _find(text: str):
    from ..views import _find as find  # "1722", "#1722", "Visit 1722", a key, a reference

    return find(text)


def _hact(visit) -> dict[str, Any] | None:
    """The year's HACT programmatic visits of the visit's partners (required and completed in eTools,
    NeuroDB's count) and the visits planned this quarter for its programme documents."""
    from neurodb.partnerships.models import PCA, PartnerOrganization

    from ..views import _hact as hact

    partners = list(PartnerOrganization.objects.filter(pk__in=visit.partner_ids))
    pds = list(PCA.objects.filter(pk__in=visit.pd_ids))
    found = hact(visit, partners, pds)
    if not found:
        return None
    return {
        "year": found["year"],
        "quarter": found["quarter"],
        "partners": [
            {
                "partner": line["partner"],
                "programmatic_visits_required": line["required"],
                "programmatic_visits_completed_etools": line["completed"],
                "fm_programmatic_visits_neurodb": line["neurodb"],
            }
            for line in found["partners"]
        ],
        "programme_documents": [
            {
                "pd": line["pd"].number,
                "planned_this_quarter": line["planned"],
                "visited_this_quarter": line["done"],
            }
            for line in found["pds"]
        ],
    }


def fm_visit(visit: str) -> dict[str, Any]:
    from neurodb.datamart.models import MonitoringFinding

    from ..models import Visit, VisitRuleResult
    from ..parse import visit_answer_rows

    if (off := _switched_off()) is not None:
        return off
    ctx = current()
    found = _find(str(visit))
    if not found:
        return {"error": f"No visit {visit!s} in NeuroDB; eTools field monitoring syncs nightly."}
    within = ctx.scope.visits()
    chosen = next((v for v in found if within.filter(pk=v.pk).exists()), None)
    if chosen is None:
        return {"error": f"{found[0].label} is not in the current filter ({ctx.scope.label()})."}
    v = Visit.objects.select_related("partner", "pd").get(pk=chosen.pk)
    names_ = privacy.names()
    ctx.seen.add(v.key)
    not_rated_yet = v.status_group in ("planned", "in_progress")
    rows = list(v.entity_rows.order_by("datamart_id"))
    narratives: dict[int, str] = {}
    if bound():
        ids = [row.finding_id for row in rows if row.finding_id]
        narratives = dict(MonitoringFinding.objects.filter(pk__in=ids).values_list("pk", "narrative_finding"))
    entities = []
    for row in rows:
        raw = narratives.get(row.finding_id, "") if bound() else ("x" if row.narrative_words else "")
        rated = row.rating in ("on_track", "constrained", "off_track")
        entities.append(
            {
                "kind": row.kind,
                "entity": privacy.clean(row.entity, 255, names_)[0],
                "rating": row.rating,
                "rated_on": v.end_date.isoformat() if v.end_date and (rated or not not_rated_yet) else None,
                "hact_q1": row.hact_q1 or None,
                "narrative": _text(ctx, raw, NARRATIVES_ELSEWHERE, names_),
            }
        )
    rules = [
        {
            "rule": r.rule,
            "status": r.status,
            "points": float(r.points),
            "max_points": r.max_points,
            "detail": r.detail,
        }
        for r in VisitRuleResult.objects.filter(visit=v).order_by("rule")
    ]
    answers = []
    if bound():
        for answer, shown, summary in visit_answer_rows(v):
            written = " — ".join(part for part in (shown, summary) if str(part or "").strip())
            answers.append(
                {
                    "question": privacy.clean(answer.question_text, 500, names_)[0],
                    "role": answer.role or None,
                    "applies_to": answer.applies_to,
                    "answered": answer.answered,
                    "code": answer.answer_code or None,
                    "answer": _text(ctx, written, ANSWERS_ELSEWHERE, names_),
                }
            )
    else:
        for answer in v.answers.order_by("question_order", "question_key"):
            answers.append(
                {
                    "question": privacy.clean(answer.question_text, 500, names_)[0],
                    "role": answer.role or None,
                    "applies_to": answer.applies_to,
                    "answered": answer.answered,
                    "code": answer.answer_code or None,
                    "answer": ANSWERS_ELSEWHERE if answer.answered else "",
                }
            )
    return {
        **card(v, names_),
        "programmatic": v.is_programmatic,
        "psea_flag": v.psea_flag,
        "score_band": v.score_band or None,
        "not_scored_reason": v.not_scored_reason or None,
        "urgency_band": v.urgency_band or None,
        "urgency_parts": dict(v.urgency_parts or {}),
        "action_points_total": v.action_points,
        "action_points_high_open": v.action_points_high_open,
        "cp_outputs": [privacy.clean(name, 300, names_)[0] for name in v.cp_outputs or ()],
        "entities": entities,
        "rules": rules,
        "hact": _hact(v),
        "answers": answers,
    }


# ------------------------------------------------------------------------------------------ fm_search
def _snippet(text: str, needle: str) -> str:
    """At most SNIPPET_CHARS characters of ``text`` around ``needle`` (cut from that value only)."""
    flat = " ".join(str(text or "").split())
    at = flat.casefold().find(" ".join(needle.split()).casefold())
    start = max(0, at - SNIPPET_CHARS // 3) if at >= 0 else 0
    piece = flat[start : start + SNIPPET_CHARS]
    return ("…" if start else "") + piece + ("…" if start + SNIPPET_CHARS < len(flat) else "")


def fm_search(text: str, limit: int = 5) -> dict[str, Any]:
    from ..models import Visit
    from ..parse import search_texts

    if (off := _switched_off()) is not None:
        return off
    needle = " ".join(str(text or "").split())
    if not SEARCH_MIN <= len(needle) <= SEARCH_MAX:
        raise ToolInputError(f"Argument 'text' must be {SEARCH_MIN} to {SEARCH_MAX} characters.")
    names_ = privacy.names()
    if people.mentions(needle, names_) or people.EMAIL.search(needle):
        return {"error": NO_PEOPLE}
    ctx = current()
    keys = list(ctx.scope.visits().values_list("key", flat=True))
    hits = search_texts(keys, needle, max(1, min(limit or 5, 10)))
    labels = dict(Visit.objects.filter(key__in={hit.visit_key for hit in hits}).values_list("key", "label"))
    matches = []
    for hit in hits:
        match: dict[str, Any] = {
            "visit": f"visit:{hit.visit_key}",
            "label": labels.get(hit.visit_key, hit.visit_key),
            "url": _visit_url(hit.visit_key),
            "where": hit.where,
        }
        if bound():
            cleaned, placeholders = privacy.clean(_snippet(hit.text, needle), SNIPPET_CHARS + 100, names_)
            if placeholders <= MAX_PLACEHOLDERS and ctx.take_text():
                match["snippet"] = cleaned
        matches.append(match)
        ctx.seen.add(hit.visit_key)
    out: dict[str, Any] = {"label": "Visits whose notes mention the text", "filter": ctx.scope.label()}
    out["matches"] = matches
    if not bound():
        out["hint"] = "The notes themselves are read in Monitoring insights."
    elif matches and not any("snippet" in m for m in matches):
        out["hint"] = LIMIT_REACHED
    return out


# ------------------------------------------------------------------------------------------ registry
_PERIOD = {
    "period_from": {"type": "string", "description": "YYYY-MM-DD: only visits that ended on or after it."},
    "period_to": {"type": "string", "description": "YYYY-MM-DD: only visits that ended on or before it."},
}


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


FMM_TOOLS: dict[str, tuple[Any, str, dict, str]] = {
    "fm_summary": (
        fm_summary,
        "Field monitoring visits (eTools) counted: visits by status, monitored entities, entities rated and "
        "not monitored, the average report quality score, and visits of high and amber urgency, for the "
        "filter, optionally grouped by section, governorate, office, partner, month, rating, quality rule, "
        "entity type or status. Arguments can only narrow the filter.",
        _schema(
            {
                "group_by": {
                    "type": "string",
                    "enum": [
                        "none",
                        "section",
                        "governorate",
                        "office",
                        "partner",
                        "month",
                        "rating",
                        "rule",
                        "entity_type",
                        "status",
                    ],
                },
                **_PERIOD,
                "section": {"type": "string", "description": "An eTools section name."},
                "governorate": {"type": "string"},
            }
        ),
        "Counting monitoring visits",
    ),
    "fm_visits": (
        fm_visits,
        "Field monitoring visits (eTools) listed as cards: dates, partner, programme document, place, "
        "sections, rating and its date, HACT Q1, quality score, flags, urgency and action points, with the "
        "visit's url. Sorted by urgency (default), date (newest first) or quality (lowest first). "
        "Arguments can only narrow the filter.",
        _schema(
            {
                "section": {"type": "string"},
                "governorate": {"type": "string"},
                "office": {"type": "string", "description": "A field office."},
                "partner": {"type": "string", "description": "A partner's name, short name or id."},
                "rating": {"type": "string", "enum": list(RATINGS)},
                "status": {"type": "string", "enum": list(STATUS_GROUPS)},
                "flag": {
                    "type": "string",
                    "enum": list(RULES),
                    "description": "Visits flagged by this rule.",
                },
                "min_urgency": {"type": "integer", "minimum": 0, "maximum": 100},
                "sort": {"type": "string", "enum": list(SORTS)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                **_PERIOD,
            }
        ),
        "Listing monitoring visits",
    ),
    "fm_visit": (
        fm_visit,
        'One field monitoring visit in full, by its id (1722), "Visit 1722" or its reference: its card, '
        "its entities with their ratings and notes, the quality rules' results, its urgency, action points, "
        "HACT context and checklist answers. A visit outside the filter is not read.",
        _schema({"visit": {"type": "string"}}, ["visit"]),
        "Reading a monitoring visit",
    ),
    "fm_search": (
        fm_search,
        "Find the field monitoring visits of the filter whose notes or checklist answers contain a word or "
        "phrase (3 to 80 characters), newest first, with a short snippet where texts may be read. Searching "
        "for people is not supported.",
        _schema(
            {"text": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            ["text"],
        ),
        "Searching visit notes",
    ),
}


def definitions() -> list[dict[str, Any]]:
    """The four look-ups exactly as the chat offers them to the model (the admin's Preview shows these);
    empty when they are not registered (Monitoring insights switched off)."""
    from neurodb.assistant import tools as assistant_tools

    return assistant_tools.definitions(tuple(FMM_TOOLS))
