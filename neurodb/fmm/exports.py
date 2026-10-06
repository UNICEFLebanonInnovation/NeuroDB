"""The exports of Monitoring insights (FMS §13): the Excel workbook of a filter (``/fmm/export.xlsx``), the
Power BI package (``/fmm/export-powerbi.zip``: the same tables as CSV files, a Power Query script and its
instructions) and the tables the Power BI live feed serves (:mod:`neurodb.fmm.powerbi`).

Every figure comes from the functions that draw the page: the visits of a
:class:`~neurodb.fmm.scope.Scope`, the key figures (:func:`metrics.kpis`), the partner, field office and
section aggregates (:func:`metrics.breakdown`, with the page's own ratings and average quality) and the flag
frequency (:func:`metrics.flag_frequency`), so a file and the page never disagree for the same filter.

No file holds a person: never the team, the visit lead, a monitor's e-mail address or who an action point
is assigned to (a count, ``action_points_assigned``, stands in its place). The narratives, the HACT
answers and the action point descriptions are cleaned of names, e-mail addresses, links and phone numbers
(:func:`privacy.clean`) before they are written. The visits are read in chunks of ``CHUNK``: a handful of
queries per chunk, whatever the number of visits in it.
"""

from __future__ import annotations

import csv
import datetime
import io
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import F, QuerySet
from django.urls import reverse
from django.utils import timezone

from . import metrics, privacy
from .scope import KIND_LABELS, RATING_LABELS, Scope

CHUNK = 500  # visits read at once
TEXT_CHARS = 32_000  # a cell's text at most (Excel holds 32,767 characters in a cell)
AP_TEXT_CHARS = 1_000  # one action point's description at most, in a visit's action_points_text
DETAIL_CHARS = 300  # a rule result's detail at most
ROLES = ("q1", "q2", "q3")
# The order of the visits in every file: the visits table's own (most urgent first)
ORDER = (F("urgency").desc(nulls_last=True), F("end_date").desc(nulls_last=True), "key")
BAND_WORDS = {"high": "High", "medium": "Medium", "low": "Low"}
RESULT_WORDS = {  # a rule result in the words of FMS's exports
    "pass": "passed",
    "fail": "flagged",
    "na": "not checked",  # the data it needs is not there
    "pending": "not checked",  # its AI check is not done yet
    "nap": "skipped",  # does not apply to the visit
    "off": "skipped",  # switched off
}
NOT_IN_NEURODB = (  # FMS §13.2 columns these files leave out, and why
    (
        "action_points_assigned_to",
        "never exported: action_points_assigned gives how many have someone assigned",
    ),
    ("dim_supplies", "not exported: the supplies answer is read on the visit page"),
)
PRIVACY_NOTE = (
    "No names: the team, the visit lead, monitors' e-mail addresses and the people action points are "
    "assigned to are never in this file. Narratives, HACT answers and action point descriptions are "
    "cleaned of names, e-mail addresses, links and phone numbers."
)

# ------------------------------------------------------------------------------------------ columns
VISIT_HEAD = (
    "id",
    "country_name",
    "monitoring_activity_id",
    "reference_number",
    "monitoring_activity_start_date",
    "monitoring_activity_end_date",
    "entity",
    "entity_type",
    "vendor_number",
    "programme_documents",
    "field_offices",
    "sections_names",
    "programme_areas",
    "overall_finding_rating",
    "status",
    "monitoring_modality",
    "quality_score",
    "quality_status",
    "urgency",
    "quality_flags",
)
VISIT_TAIL = (
    "location_name",
    "location_lat",
    "location_lon",
    "location_type",
    "location_pcode",
    "governorate",
    "district",
    "narrative_finding",
    "hact_q1_answer",
    "hact_q2_answer",
    "hact_q3_answer",
    "action_points_count",
    "action_points_open",
    "action_points_overdue",
    "action_points_text",
    "action_points_due_dates",
    "action_points_assigned",
    "_ai_used",
    "neurodb_url",
)
RULE_COLUMNS = ("visit_id", "rule_id", "rule_name", "category", "result", "points_lost", "ai_used", "detail")
GROUP_TAIL = (
    "visits",
    "rated",
    "on_track",
    "constrained",
    "off_track",
    "on_track_pct",
    "constrained_pct",
    "off_track_pct",
    "avg_quality_score",
    "scored",
    "high",
    "medium",
    "low",
    "flagged_visits",
    "flags",
    "open_action_points",
)
PARTNER_COLUMNS = ("partner", "partner_full_name", "vendor_number", *GROUP_TAIL)
OFFICE_COLUMNS = ("field_office", *GROUP_TAIL)
SECTION_COLUMNS = ("section", *GROUP_TAIL)
FLAG_COLUMNS = ("rule_id", "rule_name", "flagged_visits", "evaluated_visits", "flagged_pct")
AP_COLUMNS = (
    "reference",
    "description",
    "partner",
    "section",
    "office",
    "priority",
    "due_date",
    "status",
    "visit_ids",
    "link_confidence",
    "ai_verdict",
    "pme_verification",
)
ABOUT_COLUMNS = ("name", "value")
DATASETS = ("visits", "rule_results", "action_points", "partners")

# The type of each column in the Power Query script (every other column is text)
NUMBER_COLUMNS = frozenset(
    {
        "quality_score",
        "location_lat",
        "location_lon",
        "points_lost",
        "on_track_pct",
        "constrained_pct",
        "off_track_pct",
        "avg_quality_score",
        "flagged_pct",
    }
)
WHOLE_COLUMNS = frozenset(
    {
        "monitoring_activity_id",
        "urgency",
        "action_points_count",
        "action_points_open",
        "action_points_overdue",
        "action_points_assigned",
        "visits",
        "rated",
        "on_track",
        "constrained",
        "off_track",
        "scored",
        "high",
        "medium",
        "low",
        "flagged_visits",
        "flags",
        "open_action_points",
        "evaluated_visits",
    }
)
DATE_COLUMNS = frozenset({"monitoring_activity_start_date", "monitoring_activity_end_date", "due_date"})
LOGICAL_COLUMNS = frozenset({"_ai_used"})


def category_column(key: str) -> str:
    """The column of a score category: FMS's ``completeness_score``, then ``_evidence_score``..."""
    return "completeness_score" if key == "completeness" else f"_{key}_score"


def visit_columns(categories: Iterable[str]) -> list[str]:
    """The Visits columns (FMS §13.2 names), with one column per score category."""
    return [*VISIT_HEAD, *(category_column(k) for k in categories), *VISIT_TAIL]


# ------------------------------------------------------------------------------------------ context
@dataclass
class Context:
    """What every row of an export reads once: the names removed from texts, the score categories and
    rules, the country and the site's address."""

    names: frozenset[str]
    weights: dict[str, Decimal]
    used: set[str]  # the categories a rule switched on uses
    labels: dict[str, str]  # category labels
    rules: dict[str, Any]  # code -> RuleSetting
    ai_rules: set[str]  # the codes of the AI checks
    country: str
    site_url: str
    limits: dict[str, int] = field(default_factory=dict)

    @classmethod
    def read(cls) -> Context:
        from neurodb.watch import people

        from .models import RuleSetting, ScoreSetting
        from .score import categories_of, category_labels

        setting = ScoreSetting.objects.filter(pk=1).first() or ScoreSetting()
        rules = {r.code: r for r in RuleSetting.objects.all()}
        return cls(
            # read afresh: a file holds whole narratives, and the team names of visits built since the
            # names were last read must be removed too
            names=people.known_names(refresh=True),
            weights=categories_of(setting),
            used={r.category for r in rules.values() if r.enabled},
            labels=category_labels(setting),
            rules=rules,
            ai_rules={code for code, r in rules.items() if r.type == RuleSetting.Type.NARRATIVE},
            country=getattr(settings, "FMM_COUNTRY_NAME", "") or "Lebanon",
            site_url=(getattr(settings, "SITE_URL", "") or "").rstrip("/"),
            limits=metrics.thresholds(setting),
        )

    def clean(self, text: Any, limit: int = TEXT_CHARS) -> str:
        return privacy.clean(text, limit, self.names)[0]


# ------------------------------------------------------------------------------------------ visits
def rating_word(rating: str, status_group: str) -> str:
    """The visit's overall finding rating in words, as the page counts it: Not monitored only for a
    reported visit (planned, not conducted), "Not rated yet" for a planned or in-progress visit, and
    nothing for another visit without a rating (``metrics.counted_rating``)."""
    rating = rating or "not_monitored"
    if rating == "not_monitored" and status_group in ("planned", "in_progress"):
        return "Not rated yet"
    counted = metrics.counted_rating(rating, status_group)
    return RATING_LABELS.get(counted, "") if counted else ""


def _joined(texts: Iterable[str]) -> list[str]:
    """``texts`` without blanks or repeats, in their order."""
    return list(dict.fromkeys(t.strip() for t in texts if t and str(t).strip()))


def _texts(visit_ids: list[int], ctx: Context) -> dict[int, dict[str, str]]:
    """{visit pk: {"narrative", "q1", "q2", "q3"}} of the visits given, cleaned: the narratives of its
    finding rows (each after its entity when there are several), and each HACT answer as the finding rows
    write it (``hact_q1_answer``...), else as the checklist answers give it."""
    from neurodb.datamart.models import MonitoringFinding

    from . import fields, parse
    from .models import QuestionAnswer, VisitEntity

    entities = list(
        VisitEntity.objects.filter(visit_id__in=visit_ids)
        .order_by("visit_id", "datamart_id")
        .values_list("visit_id", "finding_id", "entity")
    )
    finding_ids = [finding for _visit, finding, _entity in entities if finding]
    narratives = dict(
        MonitoringFinding.objects.filter(pk__in=finding_ids).values_list("pk", "narrative_finding")
    )
    written = parse.row_texts(finding_ids, [f"{role}_answer" for role in ROLES])
    found: dict[int, dict[str, list]] = defaultdict(lambda: {"narrative": [], "q1": [], "q2": [], "q3": []})
    for visit_id, finding, entity in entities:
        text = (narratives.get(finding) or "").strip() if finding else ""
        if text:
            found[visit_id]["narrative"].append((entity or "", text))
        for role in ROLES:
            answer = written.get(finding, {}).get(f"{role}_answer") if finding else None
            if answer:
                found[visit_id][role].append(answer)
    rows = list(
        QuestionAnswer.objects.filter(visit_id__in=visit_ids, role__in=ROLES, answered=True)
        .order_by("visit_id", "question_order", "document_id")
        .values_list("visit_id", "role", "document_id", "question_key")
    )
    answers = parse.answer_texts(
        [row[2] for row in rows],
        {name: fields.key_for("fm_questions", name) for name in ("answer", "answer_label")},
    )
    # an answer given as an option's code ("2") is written as its label ("Constrained"), as on the visit page
    options = parse.option_labels({row[3] for row in rows}) if rows else {}
    checklist: dict[tuple[int, str], list[str]] = defaultdict(list)
    for visit_id, role, document, question in rows:
        text = answers.get(document)
        if text:
            checklist[(visit_id, role)].append(options.get((question, text), text))
    out: dict[int, dict[str, str]] = {}
    for visit_id in visit_ids:
        texts = found.get(visit_id, {"narrative": [], "q1": [], "q2": [], "q3": []})
        narrative = texts["narrative"]
        if len(narrative) > 1:
            joined = " | ".join(f"{entity}: {text}" if entity else text for entity, text in narrative)
        else:
            joined = narrative[0][1] if narrative else ""
        entry = {"narrative": ctx.clean(joined) if joined else ""}
        for role in ROLES:
            chosen = _joined(texts[role]) or _joined(checklist.get((visit_id, role), ()))
            entry[role] = ctx.clean(" | ".join(chosen)) if chosen else ""
        out[visit_id] = entry
    return out


def _visit_points(visit_ids: list[int], ctx: Context) -> dict[int, tuple[list[str], list[str]]]:
    """{visit pk: (descriptions, due dates)} of the FM action points linked to the visits, cleaned, in
    the order of their due dates; a ";" inside a description becomes "," (the column is split on it)."""
    from .models import VisitActionPoint

    rows = (
        VisitActionPoint.objects.filter(visit_id__in=visit_ids)
        .order_by("visit_id", F("action_point__due_date").asc(nulls_last=True), "action_point_id")
        .values_list("visit_id", "action_point__description", "action_point__due_date")
    )
    out: dict[int, tuple[list[str], list[str]]] = defaultdict(lambda: ([], []))
    for visit_id, description, due in rows:
        text = ctx.clean(description, AP_TEXT_CHARS).replace(";", ",") if description else ""
        out[visit_id][0].append(text or "(no description)")
        out[visit_id][1].append(due.isoformat() if due else "no date")
    return out


def _ai_used(visit_ids: list[int], ctx: Context) -> set[int]:
    """The visits an AI check of the quality rules was applied to (it passed or flagged them)."""
    from .models import VisitRuleResult

    if not ctx.ai_rules:
        return set()
    return set(
        VisitRuleResult.objects.filter(
            visit_id__in=visit_ids, rule__in=ctx.ai_rules, status__in=("pass", "fail")
        ).values_list("visit_id", flat=True)
    )


def _location_type(v) -> str:
    if v.located_by == "site" and v.site_id:
        return "Monitoring site"
    location = v.location
    if location is not None and location.type is not None:
        return location.type.name
    return ""


def _category_scores(v, ctx: Context) -> dict[str, Decimal | None]:
    """The points a scored visit kept of each score category's weight (the weight less the category's
    deductions); none for a visit without a score or a category no rule switched on uses."""
    from .rules import half_up

    out: dict[str, Decimal | None] = {}
    deductions = v.category_deductions or {}
    for key, weight in ctx.weights.items():
        column = category_column(key)
        if v.quality_score is None or key not in ctx.used or not weight:
            out[column] = None
            continue
        lost = Decimal(str(deductions.get(key) or 0))
        out[column] = half_up(max(Decimal(0), weight - lost), 1)
    return out


def visit_rows(visits: QuerySet, ctx: Context | None = None) -> Iterator[dict[str, Any]]:
    """One row per visit of ``visits`` with the FMS §13.2 columns (:func:`visit_columns`), most urgent
    first."""
    from .models import Visit

    ctx = ctx or Context.read()
    template = reverse("fmm:visit", args=["visit-key"])
    ids = list(visits.order_by(*ORDER).values_list("pk", flat=True))
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start : start + CHUNK]
        found = {
            v.pk: v for v in Visit.objects.filter(pk__in=chunk).select_related("partner", "location__type")
        }
        texts = _texts(chunk, ctx)
        points = _visit_points(chunk, ctx)
        ai = _ai_used(chunk, ctx)
        for pk in chunk:
            v = found.get(pk)
            if v is None:  # rebuilt by a refresh since the ids were read
                continue
            partner = v.partner
            text = texts.get(pk, {})
            descriptions, dues = points.get(pk, ([], []))
            yield {
                "id": v.key,
                "country_name": ctx.country,
                "monitoring_activity_id": v.activity_id,
                "reference_number": v.reference_number or v.reference,
                "monitoring_activity_start_date": v.start_date,
                "monitoring_activity_end_date": v.end_date,
                "entity": partner.name if partner else "",
                "entity_type": "; ".join(KIND_LABELS.get(k, k) for k in v.entity_kinds or ()),
                "vendor_number": (partner.vendor_number or "") if partner else "",
                "programme_documents": "; ".join(v.pd_numbers or ()),
                "field_offices": "; ".join(v.offices or ()),
                "sections_names": "; ".join(v.section_names or ()),
                "programme_areas": "; ".join(v.programme_areas or ()),
                "overall_finding_rating": rating_word(v.rating, v.status_group),
                "status": v.status or v.status_group,
                "monitoring_modality": v.modality,
                "quality_score": v.quality_score,
                "quality_status": BAND_WORDS.get(v.score_band, "")
                if v.quality_score is not None
                else "Skipped",
                "urgency": v.urgency,
                "quality_flags": "; ".join(v.flags or ()),
                **_category_scores(v, ctx),
                "location_name": v.place_name,
                "location_lat": v.latitude,
                "location_lon": v.longitude,
                "location_type": _location_type(v),
                "location_pcode": v.place_pcode,
                "governorate": v.governorate_name,
                "district": v.district_name,
                "narrative_finding": text.get("narrative", ""),
                "hact_q1_answer": text.get("q1", ""),
                "hact_q2_answer": text.get("q2", ""),
                "hact_q3_answer": text.get("q3", ""),
                "action_points_count": v.action_points,
                "action_points_open": v.action_points_open,
                "action_points_overdue": v.action_points_overdue,
                "action_points_text": "; ".join(descriptions)[:TEXT_CHARS],
                "action_points_due_dates": "; ".join(dues)[:TEXT_CHARS],
                "action_points_assigned": v.action_points_assigned,
                "_ai_used": pk in ai,
                "neurodb_url": ctx.site_url + template.replace("visit-key", v.key),
            }


# ------------------------------------------------------------------------------------------ other tables
def rule_rows(visits: QuerySet, ctx: Context | None = None) -> Iterator[dict[str, Any]]:
    """One row per rule result of ``visits``: the visit, the rule, its category, the result in words, the
    points it took off and whether an AI check gave it. The detail only for a rule whose detail NeuroDB
    writes itself (its flag template): an AI check's explanation may quote a narrative, and the flag of
    a "text contains" check writes the whole text it read, so both are left out."""
    from .models import VisitRuleResult
    from .rules import code_order, param

    ctx = ctx or Context.read()
    quoting = {code for code, r in ctx.rules.items() if param(r, "check_type") == "string_contains"}
    rows = (
        VisitRuleResult.objects.filter(visit__in=visits.values("pk"))
        .order_by("visit__key", "rule")
        .values_list("visit__key", "rule", "status", "points", "max_points", "detail")
    )
    batch: list[tuple] = []
    last = None

    def flush() -> Iterator[dict[str, Any]]:
        for key, rule, status, points, max_points, detail in sorted(batch, key=lambda r: code_order(r[1])):
            setting = ctx.rules.get(rule)
            ai = rule in ctx.ai_rules
            evaluated = status in ("pass", "fail")
            lost = Decimal(max_points or 0) - Decimal(points or 0) if evaluated else Decimal(0)
            yield {
                "visit_id": key,
                "rule_id": rule,
                "rule_name": setting.label if setting else rule,
                "category": ctx.labels.get(setting.category, setting.category) if setting else "",
                "result": RESULT_WORDS.get(status, status),
                "points_lost": max(lost, Decimal(0)),
                "ai_used": "yes" if ai and evaluated else "no",
                "detail": ""
                if ai or not detail or (rule in quoting and status == "fail")
                else ctx.clean(detail, DETAIL_CHARS),
            }

    for row in rows.iterator(chunk_size=5000):
        if row[0] != last and batch:
            yield from flush()
            batch = []
        last = row[0]
        batch.append(row)
    if batch:
        yield from flush()


def _group_row(row: dict[str, Any], first: str, by: str) -> dict[str, Any]:
    name = row["name"] or {"partner": "No partner", "office": "Office not known", "section": "No section"}[by]
    out = {
        first: name,
        "visits": row["visits"],
        "rated": row["rated"],
        **{code: row["ratings"][code] for code in metrics.RATED},
        **{f"{code}_pct": row["shares"][code] for code in metrics.RATED},
        "avg_quality_score": row["avg"],
        "scored": row["scored"],
        **{band: row["bands"][band] for band in metrics.BANDS},
        "flagged_visits": row["flagged"],
        "flags": row["flags"],
        "open_action_points": row["open_action_points"],
    }
    if by == "partner":
        out["partner_full_name"] = row["full_name"]
        out["vendor_number"] = row["vendor_number"]
    return out


def group_rows(scope: Scope, by: str, when: str | None = None) -> list[dict[str, Any]]:
    """The Partners, Field offices or Sections sheet (``by``: partner, office, section)."""
    first = {"partner": "partner", "office": "field_office", "section": "section"}[by]
    return [_group_row(row, first, by) for row in metrics.breakdown(scope, by, when)]


def flag_rows(scope: Scope, ctx: Context, when: str | None = None) -> list[dict[str, Any]]:
    """The Flags sheet: each rule switched on, the visits it flagged out of those it evaluated (the
    flag frequency chart), most flagged first."""
    found = metrics.flag_frequency(scope, list(ctx.rules.values()), when)
    return [
        {
            "rule_id": row["code"],
            "rule_name": ctx.rules[row["code"]].label if row["code"] in ctx.rules else row["code"],
            "flagged_visits": row["n"],
            "evaluated_visits": row["evaluated"],
            "flagged_pct": row["pct"],
        }
        for row in found["rows"]
    ]


def action_point_rows(visits: QuerySet, ctx: Context | None = None) -> Iterator[dict[str, Any]]:
    """The FM action points linked to ``visits`` (each once, with every visit it is linked to): the
    reference, the description cleaned, partner, section, office, priority, due date, status, the link
    confidence, the AI verdict and the PME verification; never who it is assigned to."""
    from neurodb.datamart.models import ActionPoint

    from . import action_points as ap_module
    from .models import VisitActionPoint

    ctx = ctx or Context.read()
    links: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for point_id, key, how in (
        VisitActionPoint.objects.filter(visit__in=visits.values("pk"))
        .order_by("action_point_id", "visit__key")
        .values_list("action_point_id", "visit__key", "matched_by")
    ):
        links[point_id].append((key, how))
    ids = sorted(links)
    for start in range(0, len(ids), CHUNK):
        chunk = list(
            ActionPoint.objects.filter(pk__in=ids[start : start + CHUNK])
            .select_related("partner")
            .order_by(F("due_date").asc(nulls_last=True), "datamart_id")
        )
        reviews = ap_module.current_reviews(chunk)
        checks = ap_module.latest_verifications([p.datamart_id for p in chunk])
        for p in chunk:
            visits_of = links[p.pk]
            confidence = ap_module.CONFIDENCE.get(visits_of[0][1], "medium") if visits_of else "unmatched"
            review = reviews.get(p.datamart_id)
            check = checks.get(p.datamart_id)
            yield {
                "reference": p.reference_number,
                "description": ctx.clean(p.description) if p.description else "",
                "partner": p.partner_name or (p.partner.name if p.partner else ""),
                "section": p.section,
                "office": p.office,
                "priority": "High" if p.high_priority else "Normal",
                "due_date": p.due_date,
                "status": p.status,
                "visit_ids": "; ".join(key for key, _how in visits_of),
                "link_confidence": ap_module.CONFIDENCE_LABELS.get(confidence, ""),
                "ai_verdict": review.get_verdict_display() if review else "",
                "pme_verification": check.get_state_display() if check else "",
            }


def _filters_text(scope: Scope) -> str:
    from .ai.facts import filters_text

    return filters_text(scope, frozenset())


def about_rows(scope: Scope, ctx: Context, when: str | None = None) -> list[dict[str, Any]]:
    """The About sheet: the filter in words, the reference date, the visit counts, and what the columns
    mean (quality score, bands, urgency, Not monitored), what is left out, and the privacy note."""
    from . import status

    snap = status.snapshot()
    kpis = metrics.kpis(scope, when, ctx.limits)
    refresh = snap.last_refresh
    as_of = timezone.localtime(refresh.finished_at) if refresh and refresh.finished_at else None
    from .models import ScoreSetting
    from .score import weights_of

    setting = ScoreSetting.objects.filter(pk=1).first() or ScoreSetting()
    weights = weights_of(setting)
    rows = [
        ("Monitoring insights", "eTools field monitoring visits, exported from NeuroDB"),
        ("Exported on", timezone.localtime().replace(tzinfo=None)),
        ("Data as of (last refresh)", as_of.replace(tzinfo=None) if as_of else "not refreshed yet"),
        ("Rules version", (refresh.details or {}).get("rules_version", "") if refresh else ""),
        ("Period", scope.period_label()),
        ("Period from", scope.start),
        ("Period to", scope.end),
        ("Filters", _filters_text(scope)),
        ("Country", ctx.country),
        ("Visits", kpis["visits"]),
    ]
    rows += [(f"Visits {row['label']}", row["n"]) for row in kpis["by_status"]]
    rows += [
        ("Scored visits", kpis["scored"]),
        ("Average quality score", kpis["avg_quality"]),
        ("Monitored entities", kpis["entities"]),
        ("High urgency visits", kpis["high_urgency"]),
        (
            "quality_score",
            "0-100: 100 less the points the quality rules took off (each score category loses at most "
            "its weight). Blank for a visit that is not scored (its status is not scored, or AI checks "
            "are pending).",
        ),
        (
            "quality_status",
            f"High from {ctx.limits['band_high']}, Medium from {ctx.limits['band_medium']}, Low below; "
            "Skipped: not scored.",
        ),
        (
            "Category scores (completeness_score, _evidence_score...)",
            "The points the visit kept of each category's weight ("
            + ", ".join(f"{ctx.labels.get(k, k)} {w}" for k, w in ctx.weights.items())
            + "). Blank when the visit is not scored or no rule switched on uses the category.",
        ),
        (
            "urgency",
            "0-100, 100 the most urgent: "
            + " + ".join(
                f"{round(100 * share)}% × {part.replace('_', ' ')}" for part, share in weights.items()
            )
            + f". High from {ctx.limits['red']}, Medium from {ctx.limits['amber']}. Blank when not scored.",
        ),
        (
            "overall_finding_rating",
            "The visit's worst entity rating: On track, Constrained, Off track. Not monitored: a reported "
            "visit with nothing rated (planned, not conducted). Not rated yet: planned or in progress. "
            "Blank: cancelled or of unknown status without a rating.",
        ),
        ("quality_flags", "The quality rules the visit failed, separated by ;"),
        ("_ai_used", "TRUE when an AI check of the quality rules was applied to the visit."),
        (
            "Lists",
            "field_offices, sections_names, programme_areas, programme_documents, quality_flags, "
            "action_points_text "
            "and action_points_due_dates hold several values separated by ;",
        ),
        ("Texts", f"Narratives and HACT answers are cut to {TEXT_CHARS:,} characters."),
        ("Privacy", PRIVACY_NOTE),
    ]
    rows += [(f"Left out: {name}", why) for name, why in NOT_IN_NEURODB]
    rows += [
        (
            "Rule results",
            "One row per visit and rule: passed, flagged, not checked (data missing or AI check "
            "pending) or skipped (does not apply, or switched off). The detail is NeuroDB's own wording "
            "only.",
        ),
        (
            "Partners, Field offices, Sections",
            "A visit counts in each of its partners, offices or sections. "
            "Shares of On track, Constrained and Off track are over the rated visits.",
        ),
    ]
    return [{"name": name, "value": value} for name, value in rows]


# ------------------------------------------------------------------------------------------ the workbook
def workbook_sheets(scope: Scope, when: str | None = None) -> list[tuple[str, Sequence[str], Iterable]]:
    """The sheets of the Excel workbook of ``scope``: About, Visits, Rule results, Partners, Field
    offices, Sections, Flags and Action points."""
    ctx = Context.read()
    visits = scope.visits()
    return [
        ("About", ABOUT_COLUMNS, about_rows(scope, ctx, when)),
        ("Visits", visit_columns(ctx.weights), visit_rows(visits, ctx)),
        ("Rule results", RULE_COLUMNS, rule_rows(visits, ctx)),
        ("Partners", PARTNER_COLUMNS, group_rows(scope, "partner", when)),
        ("Field offices", OFFICE_COLUMNS, group_rows(scope, "office", when)),
        ("Sections", SECTION_COLUMNS, group_rows(scope, "section", when)),
        ("Flags", FLAG_COLUMNS, flag_rows(scope, ctx, when)),
        ("Action points", AP_COLUMNS, action_point_rows(visits, ctx)),
    ]


def workbook(scope: Scope, when: str | None = None):
    """``monitoring-insights-YYYY-MM-DD.xlsx`` of ``scope`` (write-only, numbers and dates typed)."""
    from neurodb.reports.exports import xlsx_response

    filename = f"monitoring-insights-{timezone.localdate().isoformat()}.xlsx"
    return xlsx_response(filename, workbook_sheets(scope, when), typed=True)


# ------------------------------------------------------------------------------------------ CSV
def csv_value(value: Any) -> Any:
    """A value as the CSV files write it: ISO dates, "." decimals, true/false."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime.datetime):
        return (timezone.localtime(value) if timezone.is_aware(value) else value).isoformat(
            timespec="seconds"
        )
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, list | tuple):
        return "; ".join(str(v) for v in value)
    return value


class _Echo:
    def write(self, value: str) -> str:
        return value


def csv_lines(columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> Iterator[str]:
    """The CSV text of ``rows``, line by line, after a byte-order mark (Excel then reads UTF-8)."""
    writer = csv.writer(_Echo())
    yield "\ufeff"
    yield writer.writerow(list(columns))
    for row in rows:
        yield writer.writerow([csv_value(row.get(c)) for c in columns])


def dataset(name: str, scope: Scope, ctx: Context, when: str | None = None) -> tuple[list[str], Iterable]:
    """The columns and rows of one table of the Power BI package and feed (``DATASETS``)."""
    visits = scope.visits()
    if name == "visits":
        return visit_columns(ctx.weights), visit_rows(visits, ctx)
    if name == "rule_results":
        return list(RULE_COLUMNS), rule_rows(visits, ctx)
    if name == "action_points":
        return list(AP_COLUMNS), action_point_rows(visits, ctx)
    if name == "partners":
        return list(PARTNER_COLUMNS), group_rows(scope, "partner", when)
    raise ValueError(f"no dataset {name!r}")


# ------------------------------------------------------------------------------------------ Power Query
def _m_type(column: str) -> str:
    if column in DATE_COLUMNS:
        return "type date"
    if column in WHOLE_COLUMNS:
        return "Int64.Type"
    if column in LOGICAL_COLUMNS:
        return "type logical"
    if column in NUMBER_COLUMNS or column.endswith("_score"):
        return "type number"
    return "type text"


def _m_text(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def m_script(columns: dict[str, Sequence[str]], base_url: str | None = None) -> str:
    """The Power Query (M) script that loads the four tables with their types, and splits the ";"
    columns into ``visit_sections``, ``visit_offices`` and ``visit_flags``. From the unzipped folder
    (``RootFolder``), or with ``base_url`` from the live feed (``Web.Contents`` with ``ApiKeyName``: Power
    BI asks for the key once, as a "Web API" key, and sends it as ``?key=``)."""
    today = timezone.localdate().isoformat()
    if base_url is None:
        head = [
            f"// NeuroDB Monitoring insights for Power BI (package of {today}).",
            "// Set RootFolder to the folder you unzipped (the one that holds the data folder), ending",
            "// with \\.",
            "let",
            '    RootFolder = "C:\\NeuroDB\\monitoring-insights\\",',
            '    Source = (name as text) as binary => File.Contents(RootFolder & "data\\" & name & ".csv"),',
        ]
    else:
        head = [
            f"// NeuroDB Monitoring insights for Power BI: the live feed of {base_url}.",
            "// Power BI asks for the key once: choose Web API and paste the key NeuroDB showed you.",
            "let",
            f"    BaseUrl = {_m_text(base_url.rstrip('/') + '/')},",
            "    Source = (name as text) as binary => Web.Contents(BaseUrl, "
            '[RelativePath = "powerbi/fmm/" & name & ".csv", ApiKeyName = "key"]),',
        ]
    lines = [
        *head,
        "    Load = (name as text, types as list) as table =>",
        "        let",
        "            Raw = Csv.Document(",
        '                Source(name), [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]',
        "            ),",
        "            Promoted = Table.PromoteHeaders(Raw, [PromoteAllScalars = true]),",
        '            Typed = Table.TransformColumnTypes(Promoted, types, "en-US")',
        "        in",
        "            Typed,",
        "    Split = (visits as table, column as text, name as text) as table =>",
        "        let",
        '            Kept = Table.SelectColumns(visits, {"id", column}),',
        "            Lists = Table.TransformColumns(Kept, {{column, each List.Select(List.Transform("
        'Text.Split(if _ = null then "" else _, ";"), Text.Trim), each _ <> ""), type list}}),',
        "            Rows = Table.ExpandListColumn(Lists, column),",
        "            Kept2 = Table.SelectRows(Rows, each Record.Field(_, column) <> null),",
        '            Named = Table.RenameColumns(Kept2, {{"id", "visit_id"}, {column, name}})',
        "        in",
        "            Named,",
    ]
    for name in DATASETS:
        types = ", ".join("{" + _m_text(c) + ", " + _m_type(c) + "}" for c in columns[name])
        lines.append(f"    {name} = Load({_m_text(name)}, {{{types}}}),")
    lines += [
        '    visit_sections = Split(visits, "sections_names", "section"),',
        '    visit_offices = Split(visits, "field_offices", "field_office"),',
        '    visit_flags = Split(visits, "quality_flags", "rule_id")',
        "in",
        "    [visits = visits, rule_results = rule_results, action_points = action_points,",
        "     partners = partners,",
        "     visit_sections = visit_sections, visit_offices = visit_offices, visit_flags = visit_flags]",
    ]
    return "\n".join(lines) + "\n"


def all_columns(ctx: Context) -> dict[str, list[str]]:
    return {
        "visits": visit_columns(ctx.weights),
        "rule_results": list(RULE_COLUMNS),
        "action_points": list(AP_COLUMNS),
        "partners": list(PARTNER_COLUMNS),
    }


README = """NeuroDB Monitoring insights for Power BI
=========================================

Exported from NeuroDB on {today}, for this filter: {label}{filters}.
Data as of the last Monitoring insights refresh: {as_of}.

What is in this folder
----------------------
data/visits.csv          one row per visit, with the column names of FMS's FMM output (§13.2)
data/rule_results.csv    one row per visit and quality rule
data/action_points.csv   the field monitoring action points linked to these visits
data/partners.csv        visits, ratings, average quality, flags and open action points per partner
NeuroDB_monitoring.pq    the Power Query script that loads the four files with their types
README.txt               this file

The files are UTF-8 with a byte-order mark, dates are written 2026-05-31 and decimals with a point.
Columns that hold several values (field_offices, sections_names, quality_flags, action_points_text,
action_points_due_dates, programme_documents) separate them with ";". The script also makes three helper
tables from them, one row per value: visit_sections, visit_offices and visit_flags.

{privacy}

Setting it up in Power BI Desktop
---------------------------------
1. Unzip this file into a folder, keeping the data folder, e.g. C:\\NeuroDB\\monitoring-insights\\
2. Power BI Desktop: Get data > Blank query, then Advanced editor.
3. Paste the whole of NeuroDB_monitoring.pq and click Done.
4. In the first lines, set RootFolder to your folder (it ends with \\) and click Done.
5. The query shows the seven tables. Right-click each one > Add as new query, name the new queries
   visits, rule_results, action_points, partners, visit_sections, visit_offices and visit_flags, then
   right-click the first query > uncheck Enable load. Close & Apply.
6. Relationships: visits[id] to rule_results[visit_id], visit_sections[visit_id], visit_offices[visit_id]
   and visit_flags[visit_id] (one to many).
7. File > Save as .pbix. To refresh later, download a new package, unzip it into the same folder and
   click Refresh.

There is no .pbit template: a template cannot be built or tested without Power BI, so this script stands
in its place. For data that refreshes on its own (Power BI Service scheduled refresh), ask an
Administrator for a Power BI key (NeuroDB admin > Power BI keys): its script reads the live feed.

Recommended visuals (FMS §13.3, with these columns)
---------------------------------------------------
- Map: location_lat, location_lon; bubble size = quality_score.
- Bar chart: entity on the axis, average of quality_score: quality by partner.
- Donut: overall_finding_rating, count of id: On track, Constrained, Off track, Not monitored.
- Column chart: monitoring_activity_end_date by month, count of id: visit volume over time.
- Matrix: rows = entity, columns = visit_sections[section], values = average of quality_score.
- Slicers: country_name, visit_offices[field_office], quality_status, monitoring_modality.
- KPI card: average of quality_score.
- Scatter: X = quality_score, Y = urgency, details = id, legend = monitoring_modality, tooltips =
  location_name, status.
- Table: visit_flags[rule_id] with count of visit_id, or rule_results filtered on result = flagged.
- Action points: sum of action_points_count per entity; action_points_text as a tooltip.
"""


def powerbi_package(scope: Scope, when: str | None = None) -> tuple[str, bytes]:
    """``monitoring-insights-powerbi-YYYY-MM-DD.zip`` of ``scope``: the four CSV files, the Power Query
    script and README.txt. Built in memory."""
    from . import status

    ctx = Context.read()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name in DATASETS:
            columns, rows = dataset(name, scope, ctx, when)
            text = "".join(csv_lines(columns, rows))
            package.writestr(f"data/{name}.csv", text.encode("utf-8"))
        package.writestr("NeuroDB_monitoring.pq", m_script(all_columns(ctx)).encode("utf-8"))
        refresh = status.snapshot().last_refresh
        filters = _filters_text(scope)
        readme = README.format(
            today=timezone.localdate().isoformat(),
            label=scope.label(),
            filters=f" ({filters})" if filters and filters != "All visits" else "",
            as_of=timezone.localtime(refresh.finished_at).strftime("%Y-%m-%d %H:%M")
            if refresh and refresh.finished_at
            else "not refreshed yet",
            privacy=PRIVACY_NOTE,
        )
        package.writestr("README.txt", readme.replace("\n", "\r\n").encode("utf-8"))
    return f"monitoring-insights-powerbi-{timezone.localdate().isoformat()}.zip", buffer.getvalue()
