"""The facts of an AI monitoring brief: what is sent, built by code from the stored visits of a filter.

:func:`build` gathers, for one :class:`~neurodb.fmm.scope.Scope`, the figures the page shows (the key
figures and the previous period, the quality rules, the recurring issues, sections, field offices,
governorates, follow-up and HACT), up to ``comp`` visits "in full" (their structured cards, never a
narrative) and up to ``narr`` monitors' notes, each cleaned (``privacy.clean``). Every entry carries a
``key`` the brief's sentences cite, and :attr:`Facts.citable` maps each key to its entry, so that every
number and date of a kept sentence can be checked against the entries it cites.

Everything here is computed by code: shares and changes are worked out before they are sent, floats are
rounded to one decimal, and the selection of notes and visits is deterministic, so the same data gives
the same payload, the same :attr:`Facts.input_hash` and a cached brief instead of a new call.

Nothing here reads an eTools record's raw data or the people who made a visit: the notes come from the
findings' narrative column, the visits from :func:`neurodb.fmm.privacy.visit_card` (an allow-list).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Count, Max
from django.utils.dateformat import format as date_format
from django.utils.text import slugify

from neurodb.watch import redact

from .. import metrics, privacy
from ..scope import KIND_LABELS, NONE, RATING_LABELS, STATUS_LABELS, Scope
from . import prompts

NARRATIVE_MIN_CHARS = 40  # a shorter note says too little to be worth a place among the few sent
NARRATIVE_MAX_PLACEHOLDERS = 3  # a note with more names, contacts or links removed is never sent
TEXT_BATCH = 200  # narratives read from the findings at once
ISSUES = 10  # recurring issues sent
ISSUE_VISITS = 6  # visit keys listed per issue
FILTER_CHARS = 300
NAME_CHARS = 200
DEFAULT_COMP = 15  # the visit cards of a code-written brief (no version to read them from)


@dataclass
class Facts:
    """What one brief sends (``payload``), its entries by key (``citable``), the visits whose notes were
    sent, what was sent against what was allowed (``sent``) and the hash that says whether a brief
    written earlier was written from the very same input."""

    payload: dict[str, Any]
    citable: dict[str, dict[str, Any]]
    narrative_keys: list[str]
    sent: dict[str, int]
    input_hash: str
    limits: dict[str, int] = field(default_factory=dict)  # the urgency and band thresholds


# ------------------------------------------------------------------------------------------ helpers
def _num(value: Any) -> float | int | None:
    """A figure as it is sent: a whole number stays whole, any other is rounded to one decimal."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    from ..rules import half_up

    rounded = half_up(Decimal(str(value)), 1)
    return float(rounded)


def _share(part: int, whole: int) -> float | None:
    return _num(Decimal(100 * part) / whole) if whole else None


def _change(now: Any, before: Any) -> float | int | None:
    if now is None or before is None:
        return None
    return _num(Decimal(str(now)) - Decimal(str(before)))


def _day(value: datetime.date) -> str:
    return date_format(value, "j M Y")


def _slug(name: str, taken: set[str]) -> str:
    """A short key part for a name ("Child Protection" -> "child-protection"), unique among ``taken``."""
    base = slugify(str(name or ""))[:30] or "x" + hashlib.sha256(str(name).encode()).hexdigest()[:8]
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug)
    return slug


def _text(value: Any, names_: frozenset[str]) -> str:
    """A name written by eTools (partner, place, section), with e-mail addresses, links and known person
    names removed."""
    return redact.text(value, NAME_CHARS, names_)


def _not_rated_yet(rating: str, status_group: str) -> bool:
    return rating == "not_monitored" and status_group in ("planned", "in_progress")


# ------------------------------------------------------------------------------------------ the scope
def filters_text(scope: Scope, names_: frozenset[str]) -> str:
    """The filters of ``scope`` in words ("Section: Education · Governorate: Bekaa"), cleaned."""
    from neurodb.partnerships.models import PCA, PartnerOrganization

    parts: list[str] = []
    if scope.sections:
        parts.append("Section: " + ", ".join("No section" if s == NONE else s for s in scope.sections))
    if scope.governorate == NONE:
        parts.append("Governorate: not located")
    elif scope.governorate:
        from neurodb.reports.overview import governorate_names

        parts.append("Governorate: " + governorate_names().get(scope.governorate, scope.governorate.title()))
    if scope.offices:
        parts.append(
            "Field office: " + ", ".join("office not known" if o == NONE else o for o in scope.offices)
        )
    if scope.partners:
        found = PartnerOrganization.objects.filter(pk__in=scope.partners).order_by("pk")
        names = [p.short_name or p.name for p in found]
        parts.append("Partner: " + ", ".join(names or [str(p) for p in scope.partners]))
    if scope.pd is not None:
        number = PCA.objects.filter(pk=scope.pd).values_list("number", flat=True).first()
        parts.append(f"Programme document: {number or scope.pd}")
    if scope.entity_types:
        parts.append("Entity type: " + ", ".join(KIND_LABELS.get(k, k) for k in scope.entity_types))
    if scope.ratings:
        parts.append("Rating: " + ", ".join(RATING_LABELS.get(r, r) for r in scope.ratings))
    if scope.statuses:
        parts.append("Status: " + ", ".join(STATUS_LABELS.get(s, s) for s in scope.statuses))
    if scope.programmatic:
        parts.append("Programmatic visits only")
    if scope.q:
        parts.append(f"Search: {scope.q}")
    for key, value in scope.drill:
        parts.append(f"{key.replace('_', ' ')}: {value}")
    text = " · ".join(parts) or "All visits"
    return privacy.clean(text, FILTER_CHARS, names_)[0]


def _scope_entry(scope: Scope, names_: frozenset[str]) -> dict[str, Any]:
    return {
        "key": "scope",
        "period": f"{_day(scope.start)} – {_day(scope.end)}",
        "from": scope.start.isoformat(),
        "to": scope.end.isoformat(),
        "filters": filters_text(scope, names_),
        "country": getattr(settings, "ETOOLS_DATAMART_COUNTRY", "") or "",
    }


# ------------------------------------------------------------------------------------------ figures
def _kpi(scope: Scope, when: str, limits: dict[str, int]) -> dict[str, Any]:
    k = metrics.kpis(scope, when, limits)
    data = metrics.summary(scope, when, limits)
    covered = metrics.coverage(scope, when, limits)
    groups = {row["group"]: row["n"] for row in k["by_status"]}
    by_rating = data["by_rating"]
    return {
        "key": "kpi",
        "visits": k["visits"],
        "visits_reported": groups.get("reported", 0),
        "visits_in_progress": groups.get("in_progress", 0),
        "visits_planned": groups.get("planned", 0),
        "visits_cancelled": groups.get("cancelled", 0),
        "visits_status_unknown": groups.get("unknown", 0),
        "entities": k["entities"],
        "entities_rated": k["entities_rated"],
        "entities_not_monitored": k["entities_not_monitored"],
        "entities_not_monitored_share": _share(k["entities_not_monitored"], k["entities"]),
        "avg_quality": _num(k["avg_quality"]),
        "scored_visits": k["scored"],
        "high_urgency": k["high_urgency"],
        "amber_urgency": k["amber"],
        "off_track_visits": by_rating["off_track"]["visits"],
        "constrained_visits": by_rating["constrained"]["visits"],
        "not_monitored_visits": data["gaps"],  # reported visits none of whose entities is rated (§0.3)
        "psea_flagged_visits": data["psea_flagged"],
        "governorates_covered": covered["covered"],
        "governorates_total": covered["total"],
        "rules_version": k["rules_version"],
    }


def _previous(scope: Scope, kpi: dict[str, Any], when: str, limits: dict[str, int]) -> dict[str, Any]:
    before = scope.previous()
    k = metrics.kpis(before, when, limits)
    return {
        "key": "previous",
        "from": before.start.isoformat(),
        "to": before.end.isoformat(),
        "visits": k["visits"],
        "avg_quality": _num(k["avg_quality"]),
        "visits_change": kpi["visits"] - k["visits"],
        "avg_quality_change": _change(kpi["avg_quality"], _num(k["avg_quality"])),
    }


def _rules(scope: Scope, when: str, rules: list) -> dict[str, dict[str, Any]]:
    points = {row["code"]: row for row in metrics.dimension_breakdown(scope, rules, when)["rows"]}
    out = {}
    for row in metrics.rule_analysis(scope, rules, when):
        if row["state"] == "off":
            continue
        key = f"rule:{row['code']}"
        earned = points.get(row["code"])
        out[key] = {
            "key": key,
            "label": row["label"],
            "flagged": row["flagged"],
            "evaluated": row["evaluated"],
            "not_available": row["na"],
            "flagged_share": _num(row["share"]),
            "avg_points": _num(earned["earned"]) if earned else None,
            "max_points": row["points"],
        }
    return out


def _issues(scope: Scope, when: str, rules: list, carded: set[str]) -> dict[str, dict[str, Any]]:
    """The most frequent issues, each with the keys of its visits among those sent in full
    (``carded``): a visit key the payload names is always one a sentence may cite."""
    out = {}
    for row in metrics.top_issues(scope, ISSUES, when, rules):
        if not row["drill"]:
            continue
        key = f"issue:{row['drill']}"
        out[key] = {
            "key": key,
            "label": row["label"],
            "visits": row["visits"],
            "mean_urgency": row["urgency"],
            "lowest": _num(row["lowest"]),
            "visit_keys": [key for key in (f"visit:{chip['key']}" for chip in row["chips"]) if key in carded][
                :ISSUE_VISITS
            ],
        }
    return out


def _sections(scope: Scope, when: str, limits: dict[str, int], names_) -> dict[str, dict[str, Any]]:
    out, taken = {}, set()
    for row in metrics.sections(scope, when, limits):
        name = "No section" if row["name"] == NONE else _text(row["name"], names_)
        key = f"section:{'none' if row['name'] == NONE else _slug(row['name'], taken)}"
        ratings = row["ratings"]
        out[key] = {
            "key": key,
            "name": name,
            "visits": row["visits"],
            "avg_quality": _num(row["avg"]),
            "on_track": ratings["on_track"],
            "constrained": ratings["constrained"],
            "off_track": ratings["off_track"],
            "not_monitored": ratings["not_monitored"],
            "flagged": row["flagged"],
        }
    return out


def _offices(scope: Scope, when: str, limits: dict[str, int], names_) -> dict[str, dict[str, Any]]:
    found = metrics.offices(scope, when, limits)
    out, taken = {}, set()
    rows = [*found["rows"], *([found["unknown"]] if found["unknown"] else [])]
    for row in rows:
        unknown = row["name"] == NONE
        key = f"office:{'none' if unknown else _slug(row['name'], taken)}"
        out[key] = {
            "key": key,
            "name": "Office not known" if unknown else _text(row["name"], names_),
            "visits": row["visits"],
            "avg_quality": _num(row["avg"]),
        }
    return out


def _places(scope: Scope, when: str, limits: dict[str, int], names_) -> dict[str, dict[str, Any]]:
    from neurodb.reports.overview import governorate_names

    gaps = metrics.governorate_gaps(scope, when, limits)
    out: dict[str, dict[str, Any]] = {
        "gap:governorates": {
            "key": "gap:governorates",
            "not_visited": [_text(name, names_) for name in gaps["names"]],
            "visits_without_governorate": gaps["unlocated"],
        }
    }
    known = governorate_names()
    rows = (
        scope.visits()
        .exclude(governorate_key="")
        .order_by()
        .values("governorate_key")
        .annotate(n=Count("pk"), last=Max("end_date"), name=Max("governorate_name"))
        .order_by("-n", "governorate_key")
    )
    for row in rows:
        key = f"gov:{row['governorate_key']}"
        out[key] = {
            "key": key,
            "name": _text(known.get(row["governorate_key"]) or row["name"], names_),
            "visits": row["n"],
            "last_visit": row["last"].isoformat() if row["last"] else None,
        }
    return out


def _action_points(scope: Scope, when: str, limits: dict[str, int]) -> dict[str, Any]:
    found = metrics.action_points(scope, when, limits)
    return {
        "key": "ap:summary",
        "fm_linked": found["linked"],
        "fm_open": found["open"],
        "fm_overdue": found["overdue"],
        "fm_high_open": found["high_open"],
        "visits_without_follow_up": found["without"]["n"],
    }


def _hact(scope: Scope, when: str, limits: dict[str, int]) -> dict[str, Any] | None:
    found = metrics.hact_programmatic(scope, when, limits)
    rows = found["rows"]
    if not rows:
        return None
    return {
        "key": f"hact:{found['year']}",
        "year": found["year"],
        "partners_required": len(rows),
        "pv_required": sum(r["required"] or 0 for r in rows),
        "pv_completed_etools": sum(r["completed"] or 0 for r in rows),
        "fm_programmatic_visits": sum(r["fm"] or 0 for r in rows),
    }


def _notes(scope: Scope, kpi: dict[str, Any], when: str) -> list[str]:
    """What the data does not cover, in words (not facts to cite)."""
    lines = []
    asked = scope.visits().filter(questions_asked__isnull=False).count() if kpi["visits"] else 0
    lines.append(f"Question answers are available for {asked} of {kpi['visits']} visits.")
    for note in metrics.notes(scope, when):
        if note["key"] == "no_date":
            lines.append(f"{note['n']} visits have no end date and are left out of the period.")
        elif note["key"] == "no_reference":
            lines.append(
                f"{note['n']} finding rows have no activity reference and count as their own visits."
            )
        elif note["key"] == "entity_filter":
            lines.append("An entity filter is on: the entity figures count the matching entities only.")
    if kpi["visits_planned"] or kpi["visits_in_progress"]:
        lines.append("Planned and in-progress visits are not rated yet; they are not monitoring gaps.")
    return lines


# ------------------------------------------------------------------------------------------ visits
CARD_COLUMNS = ("pk", "key", "urgency", "end_date", "section_names", "rating", "hact_q1")


def _pick(
    rows: list[dict[str, Any]], limit: int, chosen: list[int], taken: set[int], cap: int | None
) -> None:
    """Add the rows' visits to ``chosen`` in their order, at most ``cap`` per section (the first
    section of each visit), until ``limit`` visits are chosen."""
    per_section: Counter = Counter()
    for row in rows:
        if len(chosen) >= limit:
            return
        if row["pk"] in taken:
            continue
        section = (row["section_names"] or [""])[0]
        if cap is not None and per_section[section] >= cap:
            continue
        per_section[section] += 1
        taken.add(row["pk"])
        chosen.append(row["pk"])


def _by_urgency(row: dict[str, Any]) -> tuple:
    return (-row["urgency"], -row["end_date"].toordinal(), row["key"])


def _newest(row: dict[str, Any]) -> tuple:
    return (-row["end_date"].toordinal(), row["key"])


def card_order(scope: Scope, limit: int, limits: dict[str, int] | None = None) -> list[int]:
    """The visits sent in full, in order (pks): every red visit, then amber, then off track or
    constrained, then the newest; each pass takes at most ⌈limit/3⌉ visits per section, for diversity,
    and a last pass fills what is left with the newest."""
    if limit <= 0:
        return []
    limits = limits or metrics.thresholds()
    rows = [dict(zip(CARD_COLUMNS, row, strict=True)) for row in scope.visits().values_list(*CARD_COLUMNS)]
    cap = math.ceil(limit / 3)
    bad = ("off_track", "constrained")
    passes = [
        sorted((r for r in rows if r["urgency"] >= limits["red"]), key=_by_urgency),
        sorted((r for r in rows if limits["amber"] <= r["urgency"] < limits["red"]), key=_by_urgency),
        sorted((r for r in rows if r["rating"] in bad or r["hact_q1"] in bad), key=_by_urgency),
        sorted(rows, key=_newest),
    ]
    chosen: list[int] = []
    taken: set[int] = set()
    for found in passes:
        _pick(found, limit, chosen, taken, cap)
    _pick(passes[-1], limit, chosen, taken, None)
    return chosen


def cards(scope: Scope, limit: int, names_: frozenset[str] | None = None, limits=None) -> list[dict]:
    """Up to ``limit`` visits as the AI reads them "in full" (``privacy.visit_card``), in the order of
    :func:`card_order`. Deterministic."""
    from ..models import Visit

    names_ = privacy.names() if names_ is None else names_
    order = card_order(scope, limit, limits)
    found = {v.pk: v for v in Visit.objects.filter(pk__in=order).select_related("partner", "pd")}
    return [privacy.visit_card(found[pk], names_) for pk in order if pk in found]


# ------------------------------------------------------------------------------------------ narratives
NARRATIVE_COLUMNS = (
    "finding_id",
    "rating",
    "datamart_id",
    "visit__key",
    "visit__urgency",
    "visit__end_date",
    "visit__section_names",
    "visit__governorate_key",
    "visit__status_group",
)


def _round_robin(groups: Iterable[list]) -> list:
    """The first of each group, then the second of each, and so on."""
    queues = [list(g) for g in groups]
    out = []
    while any(queues):
        for queue in queues:
            if queue:
                out.append(queue.pop(0))
    return out


def narrative_order(scope: Scope, limits: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Every finding row of the scope with a narrative, in the order notes are sampled: off-track and
    constrained entities (most urgent visit first), then the entities of red and amber visits, then
    round-robin across sections, and within a section across governorates, newest first."""
    limits = limits or metrics.thresholds()
    rows = [
        dict(zip(NARRATIVE_COLUMNS, row, strict=True))
        for row in scope.entities()
        .filter(finding_id__isnull=False, narrative_words__gt=0)
        .values_list(*NARRATIVE_COLUMNS)
    ]

    def urgent(row):
        return (
            -row["visit__urgency"],
            -row["visit__end_date"].toordinal(),
            row["visit__key"],
            row["datamart_id"],
        )

    def newest(row):
        return (-row["visit__end_date"].toordinal(), row["visit__key"], row["datamart_id"])

    first = sorted((r for r in rows if r["rating"] in ("off_track", "constrained")), key=urgent)
    used = {id(r) for r in first}
    second = sorted(
        (r for r in rows if id(r) not in used and r["visit__urgency"] >= limits["amber"]), key=urgent
    )
    used |= {id(r) for r in second}
    sections: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for row in sorted((r for r in rows if id(r) not in used), key=newest):
        section = (row["visit__section_names"] or [""])[0]
        sections[section][row["visit__governorate_key"]].append(row)
    third = _round_robin(_round_robin(sections[s][g] for g in sorted(sections[s])) for s in sorted(sections))
    return [*first, *second, *third]


def _sample(
    scope: Scope, limit: int, names_: frozenset[str], limits: dict[str, int]
) -> tuple[list[dict[str, Any]], int]:
    """The notes sent (at most ``limit``, never the same text twice), and how many were withheld (more
    than 3 placeholders after cleaning, or too short)."""
    from neurodb.datamart.models import MonitoringFinding

    if limit <= 0:
        return [], 0
    order = narrative_order(scope, limits)
    out: list[dict[str, Any]] = []
    withheld = 0
    seen_texts: set[str] = set()
    per_visit: Counter = Counter()
    chars = settings.FMM_NARRATIVE_CHARS
    for start in range(0, len(order), TEXT_BATCH):
        batch = order[start : start + TEXT_BATCH]
        texts = dict(
            MonitoringFinding.objects.filter(pk__in=[r["finding_id"] for r in batch]).values_list(
                "pk", "narrative_finding"
            )
        )
        for row in batch:
            raw = " ".join(str(texts.get(row["finding_id"]) or "").split())
            if not raw:
                continue
            text, placeholders = privacy.clean(raw, chars, names_)
            if len(raw) < NARRATIVE_MIN_CHARS or placeholders > NARRATIVE_MAX_PLACEHOLDERS:
                withheld += 1
                continue
            folded = text.casefold()
            if folded in seen_texts:
                continue
            seen_texts.add(folded)
            visit_key = row["visit__key"]
            per_visit[visit_key] += 1
            key = f"narr:{visit_key}:{per_visit[visit_key]}"
            rating = row["rating"] or "not_monitored"
            section = (row["visit__section_names"] or [""])[0]
            out.append(
                {
                    "key": key,
                    "visit": f"visit:{visit_key}",
                    "section": _text(section, names_) or None,
                    "rating": rating,
                    "rated_on": None
                    if _not_rated_yet(rating, row["visit__status_group"])
                    else row["visit__end_date"].isoformat(),
                    "text": text,
                }
            )
            if len(out) >= limit:
                return out, withheld
    return out, withheld


def sample_narratives(scope: Scope, limit: int, names_: frozenset[str] | None = None) -> list[dict]:
    """Up to ``limit`` monitors' notes of the scope, cleaned, in the order of :func:`narrative_order`.
    Deterministic."""
    names_ = privacy.names() if names_ is None else names_
    return _sample(scope, limit, names_, metrics.thresholds())[0]


# ------------------------------------------------------------------------------------------ the facts
def input_hash(payload: dict[str, Any], version=None) -> str:
    """sha256 of what decides the answer: the whole prompt, the model, effort, output limit, the
    sampling asked, the answer format's version and the payload."""
    from . import insights, profiles

    parts = [""] * 5
    if version is not None:
        parts = [
            prompts.compose(version, "insights"),
            profiles.model_of(version),
            version.effort,
            str(version.max_output_tokens),
            json.dumps(
                {"temperature": _num(version.temperature), "top_p": _num(version.top_p)}, sort_keys=True
            ),
        ]
    parts.append(str(insights.SCHEMA_VERSION))
    parts.append(dump(payload))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def dump(payload: dict[str, Any]) -> str:
    """The payload as it is sent: JSON, keys sorted."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _citable(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        if "key" in value:
            out[value["key"]] = value
        else:
            out.update({key: entry for key, entry in value.items() if isinstance(entry, dict)})
    return out


def build(
    scope: Scope,
    version=None,
    today: datetime.date | None = None,
    *,
    narratives: bool = True,
    names_: frozenset[str] | None = None,
) -> Facts:
    """The facts of a brief of ``scope`` written with ``version`` (its ``narr`` and ``comp``; without a
    version, for a code-written brief: no notes and 15 visits). ``narratives=False`` sends no note."""
    from ..models import RuleSetting

    names_ = privacy.names() if names_ is None else names_
    when = metrics.stamp()
    limits = metrics.thresholds()
    rules = list(RuleSetting.objects.order_by("code"))
    narr = version.narratives_sampled if version is not None and narratives else 0
    comp = version.comparison_visits if version is not None else DEFAULT_COMP

    kpi = _kpi(scope, when, limits)
    visit_cards = cards(scope, comp, names_, limits)
    payload: dict[str, Any] = {
        "scope": _scope_entry(scope, names_),
        "kpi": kpi,
        "previous": _previous(scope, kpi, when, limits),
        "rules": _rules(scope, when, rules),
        "issues": _issues(scope, when, rules, {card["key"] for card in visit_cards}),
        "sections": _sections(scope, when, limits, names_),
        "offices": _offices(scope, when, limits, names_),
        "places": _places(scope, when, limits, names_),
        "action_points": _action_points(scope, when, limits),
    }
    hact = _hact(scope, when, limits)
    if hact is not None:
        payload["hact"] = hact
    payload["visits"] = {card["key"]: card for card in visit_cards}
    notes, withheld = _sample(scope, narr, names_, limits)
    payload["narratives"] = {note["key"]: note for note in notes}
    payload["notes"] = _notes(scope, kpi, when)
    payload = json.loads(dump(payload))  # plain JSON values, keys sorted: exactly what is sent
    return Facts(
        payload=payload,
        citable=_citable(payload),
        narrative_keys=list(dict.fromkeys(note["visit"].split(":", 1)[1] for note in notes)),
        sent={
            "narratives": len(notes),
            "narratives_allowed": narr,
            "narratives_withheld": withheld,
            "visits": len(visit_cards),
            "visits_allowed": comp,
        },
        input_hash=input_hash(payload, version),
        limits=limits,
    )
