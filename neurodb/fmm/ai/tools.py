"""The four field monitoring look-ups of Chat with Data, also offered to Ask NeuroDB.

A visit is one eTools monitoring activity; it holds one **record** per entity assessed (a partner,
programme document or CP output), each rated, scored, flagged and given an urgency on its own, as in FMS.

- ``fm_summary`` counts the visits of the filter and their records (visits by status and rating, records
  by rating, not monitored, the average quality per record, the records of high and amber urgency),
  optionally grouped: each group counts its records, their distinct visits and their average;
- ``fm_visits`` lists visits as cards (``privacy.visit_card``: never a narrative, an answer or a
  person), each with its records (entity, type, score, flags);
- ``fm_visit`` reads one visit: its records with their notes, scores, urgency and rule results, the
  checks of the whole visit, action points, HACT context and checklist answers;
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
import re
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
from ..rules import code_order
from ..scope import KIND_LABELS, NONE, RATING_LABELS, RATINGS, STATUS_GROUPS, STATUS_LABELS, Scope

ASK_CARDS = 20  # visit cards per look-up when no chat is bound (Ask NeuroDB)
CHAT_CARDS = 15  # visit cards per look-up of Chat with Data (comp is the brief's compliance depth)
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
    """The records of one group, their distinct visits, their average quality and ratings (each record
    under its own) and how many are of high urgency."""

    __slots__ = ("high", "q_n", "q_sum", "ratings", "records", "visits")

    def __init__(self) -> None:
        self.records = 0
        self.visits: set[int] = set()
        self.q_sum = Decimal(0)
        self.q_n = 0
        self.high = 0
        self.ratings: Counter = Counter()

    def add(self, visit: int, quality: Any, rating: str, urgent: bool) -> None:
        self.records += 1
        self.visits.add(visit)
        if quality is not None:
            self.q_sum += Decimal(str(quality))
            self.q_n += 1
        self.ratings[rating] += 1  # metrics.counted_rating: Not monitored on reported visits only
        self.high += urgent


GROUP_COLUMNS = (  # a record, with the columns of its visit it follows
    "visit_id",
    "visit__visit_date",
    "visit__section_names",
    "visit__governorate_key",
    "visit__governorate_name",
    "visit__offices",
    "partner_id",
    "rating",
    "kind",
    "visit__status_group",
    "quality_score",
    "urgency",
)


def _group_codes(group_by: str, row: dict[str, Any]) -> list[tuple[str, str]]:
    """(code, label) of each group a record counts in: its visit's sections, governorate, offices,
    month and status (a visit with two sections counts its records in both), its own partner, rating
    and entity type."""
    if group_by == "section":
        return [(name, name) for name in row["visit__section_names"] or ()] or [(NONE, "No section")]
    if group_by == "governorate":
        key = row["visit__governorate_key"]
        return [(key, row["visit__governorate_name"] or key)] if key else [(NONE, "Not located")]
    if group_by == "office":
        return [(name, name) for name in row["visit__offices"] or ()] or [(NONE, "Office not known")]
    if group_by == "partner":
        pk = row["partner_id"]
        return [(str(pk), str(pk))] if pk else [(NONE, "No partner")]
    if group_by == "month":
        day = row["visit__visit_date"]
        return [(day.strftime("%Y-%m"), date_format(day, "M Y"))] if day else [(NONE, "No date")]
    if group_by == "rating":
        code = metrics.counted_rating(row["rating"] or "not_monitored", row["visit__status_group"])
        return [(code, RATING_LABELS.get(code, code))] if code else [(NONE, "Not rated yet")]
    if group_by == "entity_type":
        kind = row["kind"]
        return [(kind, KIND_LABELS.get(kind, kind))] if kind else [(NONE, "None")]
    if group_by == "status":
        code = row["visit__status_group"] or "unknown"
        return [(code, STATUS_LABELS.get(code, code))]
    return []


def _groups(scope: Scope, group_by: str, limits: dict[str, int]) -> tuple[list[dict[str, Any]], int]:
    """The records of ``scope`` counted per group, with their distinct visits and their average quality
    (each record scored on its own), most records first (months in order), at most GROUPS_MAX; and how
    many groups there are in all."""
    if group_by == "rule":
        return _rule_groups(scope)
    groups: dict[str, _Group] = {}
    labels: dict[str, str] = {}
    for values in scope.records().order_by().values_list(*GROUP_COLUMNS).iterator(chunk_size=2000):
        row = dict(zip(GROUP_COLUMNS, values, strict=True))
        urgent = row["urgency"] is not None and row["urgency"] >= limits["red"]
        rating = metrics.counted_rating(row["rating"] or "not_monitored", row["visit__status_group"])
        for code, label in _group_codes(group_by, row):
            groups.setdefault(code, _Group()).add(row["visit_id"], row["quality_score"], rating, urgent)
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
            "records": g.records,
            "visits": len(g.visits),
            "avg_quality": _quality(metrics.mean_quality(g.q_sum, g.q_n)),
            "scored_records": g.q_n,
            **{rating: g.ratings[rating] for rating in RATINGS},
            "high_urgency_records": g.high,
        }
        for code, g in groups.items()
    ]
    if group_by == "month":
        out.sort(key=lambda g: g["code"])
    else:
        out.sort(key=lambda g: (-g["records"], -g["visits"], str(g["group"]).casefold()))
    return out[:GROUPS_MAX], len(out)


def _rule_groups(scope: Scope) -> tuple[list[dict[str, Any]], int]:
    """Each quality rule switched on: the records it flagged, passed, could not check, did not apply to
    and is still checking (R19, read once per visit, counts on each of its records)."""
    from ..models import RuleSetting

    stats = metrics.rule_stats(scope)
    labels = dict(RuleSetting.objects.filter(enabled=True).values_list("code", "label"))
    out = []
    for code in sorted(labels, key=code_order):
        row = stats.get(code) or {}
        out.append(
            {
                "group": f"{code} {labels.get(code, '')}".strip(),
                "code": code,
                "records_flagged": row.get("fail", 0),
                "records_passed": row.get("pass", 0),
                "not_available": row.get("na", 0),
                "does_not_apply": row.get("nap", 0),
                "pending": row.get("pending", 0),
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
    data = metrics.summary(scope, limits=limits)
    rated = {code: data["visit_ratings"][code] for code in metrics.RATED}  # each visit under its own
    total = sum(rated.values())
    out: dict[str, Any] = {
        "label": "Monitoring visits",
        "filter": scope.label(),
        "period_from": scope.start.isoformat(),
        "period_to": scope.end.isoformat(),
        "visits": k["visits"],
        "visits_by_status": {row["group"]: row["n"] for row in k["by_status"]},
        # every share of ratings is over the rated visits; Not monitored (planned, not conducted) apart
        "rated_visits": total,
        "rated_visits_by_rating": rated,
        "rating_shares_of_rated": {
            code: (float(metrics._pct(n, total)) if total else None) for code, n in rated.items()
        },
        "not_monitored_visits": data["gaps"],
        # per record, as FMS: each record (an entity assessed) is rated, scored and given an urgency on
        # its own; the average quality is the records'
        "records": k["records"],
        "records_rated": k["records_rated"],
        "records_by_rating": dict(k["record_ratings"]),
        "records_not_monitored": k["records_not_monitored"],
        "avg_quality": _quality(k["avg_quality"]),
        "scored_records": k["scored"],
        "scored_visits": k["scored_visits"],
        "high_urgency_records": k["high_urgency"],
        "amber_urgency_records": k["amber"],
        "high_urgency_from": limits["red"],
        "amber_urgency_from": limits["amber"],
        "rules_version": k["rules_version"],
        "url": _dashboard(scope),
    }
    if scope.empty:
        out["hint"] = "What was asked is outside the page's filter, so no visit matches."
    if group_by and group_by != "none":
        rows, total = _groups(scope, group_by, limits)
        out["grouping"] = group_by  # not "group_by": the look-up filter drops keys ending in "_by"
        out["groups"] = rows
        if total > len(rows):
            out["groups_total"] = total
        if group_by in ("section", "office"):
            out["hint"] = "A visit with several of these counts its records in each of them."
        elif group_by in ("partner", "entity_type", "rating"):
            out["hint"] = "Each record counts under its own; a visit may count in several groups."
    return out


# ------------------------------------------------------------------------------------------ fm_visits
SORTS = {
    "urgency": (F("urgency").desc(nulls_last=True), F("visit_date").desc(nulls_last=True), "key"),
    "date": (F("visit_date").desc(nulls_last=True), "key"),
    "quality": (F("quality_score").asc(nulls_last=True), F("visit_date").desc(nulls_last=True), "key"),
}


def card(visit, names_: frozenset[str]) -> dict[str, Any]:
    """A visit as the AI reads it in a list: its card (``privacy.visit_card``) and its url."""
    return {**privacy.visit_card(visit, names_), "url": _visit_url(visit.key)}


def _record_entity(row, names_: frozenset[str]) -> str:
    """A record's entity in words (``metrics.record_name``: the CP output, the partner's name or the PD
    number, else the entity as eTools wrote it), cleaned."""
    pd_number = row.pd.number if row.pd else ""
    name = metrics.record_name(
        row.kind, row.entity, row.cp_output, pd_number, row.partner.name if row.partner else ""
    )
    return privacy.clean(name, 255, names_)[0] if name else ""


def _record_line(row, names_: frozenset[str]) -> dict[str, Any]:
    """A record as a list shows it: its id, entity, type, rating, score, band, urgency and flags."""
    return {
        "record": row.datamart_id,
        "entity": _record_entity(row, names_),
        "type": KIND_LABELS.get(row.kind, row.kind),
        "rating": row.rating,
        "score": _quality(row.quality_score),
        "band": row.score_band or None,
        "urgency": row.urgency,
        "flags": list(row.flags or ()),
    }


def _records_of(scope: Scope, visit_ids: list[int], names_: frozenset[str]) -> dict[int, list[dict]]:
    """{visit pk: its records the scope keeps (a record filter keeps the matching ones), as lines}."""
    out: dict[int, list[dict]] = {pk: [] for pk in visit_ids}
    rows = (
        scope.records()
        .filter(visit_id__in=visit_ids)
        .select_related("pd", "partner")
        .order_by("visit_id", "datamart_id")
    )
    for row in rows:
        out[row.visit_id].append(_record_line(row, names_))
    return out


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
    records = _records_of(scope, [v.pk for v in visits], names_)
    cards = [{**card(v, names_), "records": records.get(v.pk, [])} for v in visits]
    ctx.seen.update(v.key for v in visits)
    out: dict[str, Any] = {
        "label": "Monitoring visits",
        "filter": scope.label(),
        "total": total,
        "shown": len(cards),
        # each with its records: a visit's quality is the mean of its records, its urgency the most
        # urgent record's
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


CARD_KEY = re.compile(r"^\s*visit\s*:\s*", re.IGNORECASE)  # a card's "key" ("visit:1722", "visit:r-…")


def _find(text: str):
    from ..views import _find as find  # "1722", "#1722", "Visit 1722", a key, a reference

    return find(CARD_KEY.sub("", text))


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
        "year": found["year"],  # the HACT year: that of the visit's end date
        "quarter": found["quarter"],  # the programme documents' quarter: that of the visit date
        "quarter_year": found["quarter_year"],
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

    from ..models import RecordRuleResult, RuleSetting, Visit
    from ..parse import visit_answer_rows
    from ..score import visit_level

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
    rows = list(v.entity_rows.select_related("pd", "partner").order_by("datamart_id"))
    narratives: dict[int, str] = {}
    if bound():
        ids = [row.finding_id for row in rows if row.finding_id]
        narratives = dict(MonitoringFinding.objects.filter(pk__in=ids).values_list("pk", "narrative_finding"))
    once = {rule.code for rule in RuleSetting.objects.all() if visit_level(rule)}  # R19: the visit's own
    results: dict[int, list] = {row.pk: [] for row in rows}
    for r in RecordRuleResult.objects.filter(entity__visit=v):
        results.setdefault(r.entity_id, []).append(r)
    visit_checks: dict[str, dict[str, Any]] = {}  # read once for the whole visit, the same on each record
    records = []
    for row in rows:
        raw = narratives.get(row.finding_id, "") if bound() else ("x" if row.narrative_words else "")
        rated = row.rating in ("on_track", "constrained", "off_track")
        own = []
        for r in sorted(results.get(row.pk, ()), key=lambda r: code_order(r.rule)):
            if r.status == "off":
                continue
            line = _rule_line(r)
            if r.rule in once:
                visit_checks.setdefault(r.rule, line)
            else:
                own.append(line)
        records.append(
            {
                "record": row.datamart_id,
                "url": f"{_visit_url(v.key)}#{row.anchor}",
                "kind": row.kind,
                "type": KIND_LABELS.get(row.kind, row.kind),
                "entity": _record_entity(row, names_),
                "rating": row.rating,
                "rated_on": v.end_date.isoformat() if v.end_date and (rated or not not_rated_yet) else None,
                "hact_q1": row.hact_q1 or None,
                "quality": _quality(row.quality_score),
                "score_band": row.score_band or None,
                "provisional_score": _quality(row.provisional_score),
                "ai_checks_pending": row.ai_pending,
                "not_scored_reason": row.not_scored_reason or None,
                "urgency": row.urgency,
                "urgency_band": row.urgency_band or None,
                "urgency_parts": dict(row.urgency_parts or {}),
                "flags": list(row.flags or ()),
                "rules": own,
                "narrative": _text(ctx, raw, NARRATIVES_ELSEWHERE, names_),
            }
        )
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
        # the visit's quality: the mean of its scored records, with its lowest; its urgency its most
        # urgent record's
        "score_band": v.score_band or None,
        "lowest_score": _quality(v.lowest_score),
        "records_count": len(records),
        "records_scored": v.records_scored,
        "not_scored_reason": v.not_scored_reason or None,
        "urgency_band": v.urgency_band or None,
        "urgency_parts": dict(v.urgency_parts or {}),
        "action_points_total": v.action_points,
        "action_points_high_open": v.action_points_high_open,
        "cp_outputs": [privacy.clean(name, 300, names_)[0] for name in v.cp_outputs or ()],
        "records": records,
        "visit_checks": sorted(visit_checks.values(), key=lambda line: code_order(line["rule"])),
        "hact": _hact(v),
        "answers": answers,
    }


def _rule_line(r) -> dict[str, Any]:
    """A record's rule result as the AI reads it: the rule, its state, the points it took off the record
    (when it flagged it), its maximum and its detail."""
    return {
        "rule": r.rule,
        "status": r.status,
        "points_off": float(r.deducted) if r.status == "fail" else 0.0,
        "max_points": float(r.max_points),
        "detail": r.detail,
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
    "period_from": {
        "type": "string",
        "description": (
            "YYYY-MM-DD: only visits that started on or after it (the end date when no start date)."
        ),
    },
    "period_to": {
        "type": "string",
        "description": (
            "YYYY-MM-DD: only visits that started on or before it (the end date when no start date)."
        ),
    },
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
        "Field monitoring visits (eTools) and their records counted. A visit holds one record per entity "
        "assessed (partner, programme document or CP output); each record is rated, scored, flagged and "
        "given an urgency on its own, as in FMS. Gives the visits by status, the rated visits by rating "
        "with each rating's share of the rated visits, the Not monitored visits (planned, not conducted: a "
        "count apart, never in a share), the records by rating, the average report quality score per "
        "record, and the records of high and amber urgency, for the filter, optionally grouped by section, "
        "governorate, office, partner, month, rating, quality rule, entity type or status (each group: its "
        "records, their visits and their average). Arguments can only narrow the filter.",
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
        "sections, rating (its worst record's) and its date, HACT Q1, quality score (the mean of its "
        "records), flags, urgency (its most urgent record's) and action points, with the visit's url, and "
        "its records (one per entity assessed: entity, type, rating, score, band, urgency, flags). Sorted "
        "by urgency (default), date (newest first) or quality (lowest first). Arguments can only narrow "
        "the filter.",
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
                    "description": "Visits with a record this quality rule flagged, by its id (R1 ... R32).",
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
        'One field monitoring visit in full, by its id (1722), "Visit 1722", its reference or the key a '
        "list returned (visit:1722): its card, its records (one per entity assessed) each with its rating, "
        "note, quality score, urgency, flags and quality rules' results, the checks read once for the "
        "whole visit, action points, HACT context and checklist answers. A visit outside the filter is not "
        "read.",
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
