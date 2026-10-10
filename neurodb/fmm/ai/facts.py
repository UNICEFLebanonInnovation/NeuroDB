"""The facts of an AI monitoring brief: what is sent, built by code from the stored visits of a filter.

:func:`build` gathers, for one :class:`~neurodb.fmm.scope.Scope`, the figures the page shows (the key
figures: the visits with their rating distribution over the rated visits, and the records, each scored,
rated, flagged and given an urgency on its own as in FMS, with the average quality per record; the
previous period; the quality rules, counting the records each flagged; the breakdowns per section, field
office, partner, modality and governorate, each counting records with their visits as the page does;
follow-up and HACT); the ``comp`` most frequent quality flags (the compliance depth: rule, records,
visits, example visits); the cards of the visits of the most urgent records and of the flags' examples
(structured, never a narrative); and up to ``narr`` monitors' notes (one record's narrative each) with
their Q1, Q2 and Q3 answers, each cleaned (``privacy.clean``). Every share of ratings is over the rated
visits (for the records, the rated records) only; Not monitored (planned, not conducted) is a count
apart. Every
entry carries a ``key`` the brief's sentences cite, and :attr:`Facts.citable` maps each key to its
entry, so that every number and date of a kept sentence can be checked against the entries it cites.

Everything here is computed by code: shares and changes are worked out before they are sent, floats are
rounded to one decimal, and the selection of notes and visits is deterministic, so the same data gives
the same payload, the same :attr:`Facts.input_hash` and a cached brief instead of a new call.

Nothing here sends the people who made a visit: the notes come from the findings' narrative column and
their answers from the finding rows' HACT answer keys (a record read for those keys only, without
contact keys) or the checklist answers, all cleaned; the visits from
:func:`neurodb.fmm.privacy.visit_card` (an allow-list).
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
from django.db.models import Count, Max, Q
from django.utils.dateformat import format as date_format
from django.utils.text import slugify

from neurodb.watch import redact

from .. import metrics, privacy
from ..scope import (
    KIND_LABELS,
    NONE,
    QUALITY_LABELS,
    RATING_LABELS,
    STATUS_LABELS,
    URGENCY_LABELS,
    Scope,
)
from . import prompts

RATED = ("on_track", "constrained", "off_track")
# what the facts are made of: part of every brief's input hash, so a change to what is sent (2: the
# records' figures, Release 2 step 5) writes every brief again at its next request
FACTS_VERSION = 2
NARRATIVE_MIN_CHARS = 40  # a shorter note says too little to be worth a place among the few sent
NARRATIVE_MAX_PLACEHOLDERS = 3  # a note with more names, contacts or links removed is never sent
TEXT_BATCH = 200  # narratives read from the findings at once
ISSUE_VISITS = 3  # example visits listed per quality flag
EXAMPLE_CARDS = 30  # visit cards added for the flags' examples, at most (beyond VISIT_CARDS)
VISIT_CARDS = 15  # the most urgent visits sent as cards
PARTNERS = 30  # partners in the breakdown, most records first
ANSWER_CHARS = 400  # characters of a Q1, Q2 or Q3 answer sent with a narrative
FILTER_CHARS = 300
NAME_CHARS = 200
DEFAULT_COMP = 15  # the quality flags of a code-written brief (no version to read them from)


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
    if scope.modalities:
        parts.append("Modality: " + ", ".join("not known" if m == NONE else m for m in scope.modalities))
    if scope.quality_bands:
        parts.append("Quality: " + ", ".join(QUALITY_LABELS.get(b, b) for b in scope.quality_bands))
    if scope.urgency_levels:
        parts.append("Urgency: " + ", ".join(URGENCY_LABELS.get(u, u) for u in scope.urgency_levels))
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
    """The key figures: the visits (each once, under its worst record's rating) and the records (each
    under its own rating, scored and given an urgency on its own, as in FMS): the average quality is the
    records', and the low-quality and high-urgency figures count records. Every share of ratings is over
    the rated visits (or records) only; "Not monitored" (planned, not conducted) is a count apart, never
    a share of all visits."""
    k = metrics.kpis(scope, when, limits)
    data = metrics.summary(scope, when, limits)
    covered = metrics.coverage(scope, when, limits)
    groups = {row["group"]: row["n"] for row in k["by_status"]}
    # each visit under its own rating (its worst record's); each record under its own below (records_...)
    by_rating = {code: data["visit_ratings"][code] for code in RATED}
    rated = sum(by_rating.values())
    record_ratings = k["record_ratings"]
    out = {
        "key": "kpi",
        "visits": k["visits"],
        "visits_reported": groups.get("reported", 0),
        "visits_in_progress": groups.get("in_progress", 0),
        "visits_planned": groups.get("planned", 0),
        "visits_cancelled": groups.get("cancelled", 0),
        "visits_status_unknown": groups.get("unknown", 0),
        "rated_visits": rated,
        "not_monitored_visits": data["gaps"],  # reported, nothing rated: planned, not conducted
    }
    for code in RATED:
        out[f"{code}_visits"] = by_rating[code]
        out[f"{code}_share_of_rated"] = _share(by_rating[code], rated)
    out["off_track_or_constrained_share_of_rated"] = _share(
        by_rating["off_track"] + by_rating["constrained"], rated
    )
    out.update(
        {
            "records": k["records"],
            "records_rated": k["records_rated"],
            "records_not_monitored": k["records_not_monitored"],
            "records_not_rated_yet": k["records_not_rated_yet"],
        }
    )
    for code in RATED:
        out[f"records_{code}"] = record_ratings[code]
        out[f"records_{code}_share_of_rated"] = _share(record_ratings[code], k["records_rated"])
    out.update(
        {
            # per record, as FMS: the mean of the scored records (each scored on its own)
            "avg_quality": _num(k["avg_quality"]),
            "scored_records": k["scored"],
            "scored_visits": k["scored_visits"],  # the visits whose records are all scored
            "low_quality_records": data["bands"]["low"],  # scored below the Medium band
            "high_urgency_records": k["high_urgency"],  # at or above red
            "amber_urgency_records": k["amber"],
            "psea_flagged_visits": data["psea_flagged"],
            "governorates_covered": covered["covered"],
            "governorates_total": covered["total"],
            "rules_version": k["rules_version"],
        }
    )
    return out


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
    """One entry per quality rule switched on: its category, the records it flagged out of those it
    checked (as the page's rule analysis counts them), and the mean points the records kept of its
    deduction."""
    stats = metrics.rule_stats(scope, when)
    out = {}
    for row in metrics.rule_analysis(scope, rules, when):
        if row["state"] in ("off", "ai_off"):  # switched off, or an AI check while they are
            continue
        key = f"rule:{row['code']}"
        s = stats.get(row["code"]) or {}
        kept = None
        if s.get("points_n") and s.get("earned") is not None:
            kept = Decimal(str(s["earned"])) / s["points_n"]
        out[key] = {
            "key": key,
            "label": row["label"],
            "category": row["category"],
            "flagged": row["flagged"],
            "evaluated": row["evaluated"],
            "not_available": row["na"],
            "pending": row["pending"],
            "flagged_share": _num(row["share"]),
            "avg_points": _num(kept),
            "max_points": _num(row["points"]),
        }
    return out


def _issues(scope: Scope, when: str, rules: list, comp: int) -> dict[str, dict[str, Any]]:
    """The ``comp`` most frequent quality flags (the compliance depth), each with its rule, label, the
    records it flagged and their visits (the page's Top recurring issues), and up to three example
    visits (``visit:<key>``: their cards are sent too)."""
    out = {}
    for row in metrics.top_issues(scope, comp, when, rules):
        if not row["drill"]:
            continue
        key = f"issue:{row['drill']}"
        out[key] = {
            "key": key,
            "rule": row["rule"],
            "label": row["label"],
            "records": row["records"],
            "visits": row["visits"],
            "mean_urgency": row["urgency"],
            "lowest": _num(row["lowest"]),
            "visit_keys": [f"visit:{chip['key']}" for chip in row["chips"][:ISSUE_VISITS]],
        }
    return out


class _Tally:
    """Records of a group (each under its own rating; Not monitored counted apart), their distinct
    visits and their average quality per record: the figures the page shows for the same filter."""

    def __init__(self, name: str) -> None:
        self.name, self.records, self.q_sum, self.q_n = name, 0, Decimal(0), 0
        self.visits: set[int] = set()
        self.ratings: Counter = Counter()

    def add(self, visit: int, quality: Any, rating: str) -> None:
        self.records += 1
        self.visits.add(visit)
        if quality is not None:  # a DecimalField reads as a Decimal already
            self.q_sum += quality if isinstance(quality, Decimal) else Decimal(str(quality))
            self.q_n += 1
        self.ratings[rating] += 1

    def entry(self, key: str) -> dict[str, Any]:
        rated = sum(self.ratings[code] for code in RATED)
        return {
            "key": key,
            "name": self.name,
            "records": self.records,
            "visits": len(self.visits),
            "avg_quality": _num(metrics.mean_quality(self.q_sum, self.q_n)),
            "rated": rated,
            **{code: self.ratings[code] for code in RATED},
            "off_track_or_constrained_share_of_rated": _share(
                self.ratings["off_track"] + self.ratings["constrained"], rated
            ),
            "not_monitored": self.ratings["not_monitored"],
        }


BREAKDOWN_COLUMNS = (
    "visit_id",
    "partner_id",
    "visit__modality",
    "visit__governorate_key",
    "quality_score",
    "rating",
    "visit__status_group",
)


def _breakdowns(scope: Scope, names_) -> dict[str, dict[str, dict[str, Any]]]:
    """Per partner (each record under its own partner, as the page's entity table and the exports count
    it; the most records first, at most ``PARTNERS``), per monitoring modality and per governorate (its
    visit's): the records and their distinct visits, the average quality per record (the governorate
    chips' and the key figure's, for the same filter), the rated records by rating with the share Off
    track or Constrained of the rated ones, and the Not monitored ones apart. One pass over the records."""
    from neurodb.partnerships.models import PartnerOrganization

    partners: dict[int, _Tally] = {}
    modalities: dict[str, _Tally] = {}
    governorates: dict[str, _Tally] = {}
    for row in scope.records().order_by().values_list(*BREAKDOWN_COLUMNS):
        visit, pid, modality, gov, quality, rating, group = row
        counted = metrics.counted_rating(rating or "not_monitored", group)
        if pid:
            tally = partners.get(pid)
            if tally is None:
                tally = partners[pid] = _Tally("")  # named below: the partners sent only
            tally.add(visit, quality, counted)
        tally = modalities.get(modality or "")
        if tally is None:
            tally = modalities[modality or ""] = _Tally(_text(modality, names_) or "Modality not known")
        tally.add(visit, quality, counted)
        if gov:
            tally = governorates.get(gov)
            if tally is None:
                tally = governorates[gov] = _Tally("")
            tally.add(visit, quality, counted)
    for pk, name, short in PartnerOrganization.objects.filter(pk__in=list(partners)).values_list(
        "pk", "name", "short_name"
    ):
        partners[pk].name = _text(short or name, names_)
    top = sorted(partners.items(), key=lambda kv: (-kv[1].records, kv[1].name.casefold()))[:PARTNERS]
    taken: set[str] = set()
    return {
        "partners": {f"partner:{pid}": tally.entry(f"partner:{pid}") for pid, tally in top},
        "modalities": {
            (key := f"modality:{_slug(name, taken) if name else 'none'}"): tally.entry(key)
            for name, tally in sorted(modalities.items(), key=lambda kv: (-kv[1].records, kv[0]))
        },
        "governorates": {gov: tally.entry(f"gov:{gov}") for gov, tally in governorates.items()},
    }


def _sections(scope: Scope, when: str, limits: dict[str, int], names_) -> dict[str, dict[str, Any]]:
    out, taken = {}, set()
    for row in metrics.sections(scope, when, limits):
        name = "No section" if row["name"] == NONE else _text(row["name"], names_)
        key = f"section:{'none' if row['name'] == NONE else _slug(row['name'], taken)}"
        ratings = row["ratings"]
        out[key] = {
            "key": key,
            "name": name,
            "records": row["records"],
            "visits": row["visits"],
            "avg_quality": _num(row["avg"]),
            "rated": sum(ratings[code] for code in RATED),
            "on_track": ratings["on_track"],
            "constrained": ratings["constrained"],
            "off_track": ratings["off_track"],
            "not_monitored": ratings["not_monitored"],  # planned, not conducted: never in a share
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
            "records": row["records"],
            "visits": row["visits"],
            "avg_quality": _num(row["avg"]),
        }
    return out


def _places(
    scope: Scope, when: str, limits: dict[str, int], names_, governorates: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
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
        .annotate(n=Count("pk"), last=Max("visit_date"), name=Max("governorate_name"))
        .order_by("-n", "governorate_key")
    )
    for row in rows:
        key = f"gov:{row['governorate_key']}"
        figures = governorates.get(row["governorate_key"]) or {}
        out[key] = {
            **figures,
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


# what the figures count, said once (a visit holds one record per entity assessed; the brief must not call
# a count of records a count of visits)
RECORDS_NOTE = (
    "A visit holds one record per entity assessed (partner, CP output or PD/SSFA), each scored, flagged "
    "and given an urgency on its own, as in FMS. The average quality is the mean of the scored records; "
    "low quality, high and amber urgency count records; a rule's flagged and evaluated counts are "
    "records, and so are a quality flag's (with the distinct visits of those records). Sections, field "
    "offices, partners, modalities and governorates count records too (their ratings, shares and average "
    "quality are the records' own), with the distinct visits of those records. The rated, on track, "
    "constrained, off track and Not monitored visits of the key figures count each visit once, under its "
    "worst record's rating; the records_ figures count each record under its own. A visit card's "
    "quality is the mean of its records and its urgency its most urgent record's."
)


def _notes(scope: Scope, kpi: dict[str, Any], when: str) -> list[str]:
    """What the data does not cover, in words (not facts to cite), and what the breakdowns count."""
    lines = [RECORDS_NOTE]
    asked = scope.visits().filter(questions_asked__isnull=False).count() if kpi["visits"] else 0
    lines.append(f"Question answers are available for {asked} of {kpi['visits']} visits.")
    for note in metrics.notes(scope, when):
        if note["key"] == "no_date":
            lines.append(f"{note['n']} visits have no start or end date and are left out of the period.")
        elif note["key"] == "dated_by_end":
            lines.append(f"{note['n']} visits have no start date and are dated by their end date.")
        elif note["key"] == "no_reference":
            lines.append(f"{note['n']} records have no activity reference and count as their own visits.")
        elif note["key"] == "record_filter":
            lines.append("A record filter is on: the record figures count the matching records only.")
    if kpi["visits_planned"] or kpi["visits_in_progress"]:
        lines.append(
            "Planned and in-progress visits are not rated yet; they are not counted as Not monitored."
        )
    return lines


# ------------------------------------------------------------------------------------------ visits
CARD_COLUMNS = ("pk", "key", "urgency", "visit_date", "section_names", "rating", "hact_q1")
CONCERN = ("off_track", "constrained")


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


def _urgency(value: int | None) -> int:
    """An urgency to sort by: a visit without one (not scored) after every visit that has one."""
    return -1 if value is None else value


def _by_urgency(row: dict[str, Any]) -> tuple:
    return (-_urgency(row["urgency"]), -row["visit_date"].toordinal(), row["key"])


def _newest(row: dict[str, Any]) -> tuple:
    return (-row["visit_date"].toordinal(), row["key"])


def _card_rows(scope: Scope) -> list[dict[str, Any]]:
    """The visits of the scope, each with its most urgent record's urgency (a visit's own urgency is its
    most urgent record's; a provisional visit, another of whose records waits for its AI checks, has none
    of its own: its scored records' are read) and its worst record's rating and Q1."""
    rows = [dict(zip(CARD_COLUMNS, row, strict=True)) for row in scope.visits().values_list(*CARD_COLUMNS)]
    waiting = dict(
        scope.records()
        .filter(visit__urgency__isnull=True, urgency__isnull=False)
        .order_by()
        .values("visit_id")
        .annotate(top=Max("urgency"))
        .values_list("visit_id", "top")
    )
    for row in rows:
        if row["urgency"] is None:
            row["urgency"] = waiting.get(row["pk"])
        row["concern"] = row["rating"] in CONCERN or row["hact_q1"] in CONCERN
    return rows


def card_order(scope: Scope, limit: int, limits: dict[str, int] | None = None) -> list[int]:
    """The visits sent in full, in order (pks), each picked by its most urgent record (and its records'
    ratings): every visit with a red record, then amber, then with a record off track or constrained,
    then the newest; each pass takes at most ⌈limit/3⌉ visits per section, for diversity, and a last pass
    fills what is left with the newest."""
    if limit <= 0:
        return []
    limits = limits or metrics.thresholds()
    rows = _card_rows(scope)
    cap = math.ceil(limit / 3)
    passes = [
        sorted((r for r in rows if _urgency(r["urgency"]) >= limits["red"]), key=_by_urgency),
        sorted(
            (r for r in rows if limits["amber"] <= _urgency(r["urgency"]) < limits["red"]), key=_by_urgency
        ),
        sorted((r for r in rows if r["concern"]), key=_by_urgency),
        sorted(rows, key=_newest),
    ]
    chosen: list[int] = []
    taken: set[int] = set()
    for found in passes:
        _pick(found, limit, chosen, taken, cap)
    _pick(passes[-1], limit, chosen, taken, None)
    return chosen


def cards(
    scope: Scope,
    limit: int,
    names_: frozenset[str] | None = None,
    limits=None,
    examples: Iterable[str] = (),
) -> list[dict]:
    """Up to ``limit`` visits as the AI reads them "in full" (``privacy.visit_card``), in the order of
    :func:`card_order`, then the visits ``examples`` names (the quality flags' examples, keys) not among
    them, at most ``EXAMPLE_CARDS``. Deterministic."""
    from ..models import Visit

    names_ = privacy.names() if names_ is None else names_
    order = card_order(scope, limit, limits)
    wanted = [key for key in dict.fromkeys(examples)]
    extra = list(scope.visits().filter(key__in=wanted).exclude(pk__in=order).values_list("pk", "key"))
    rank = {key: i for i, key in enumerate(wanted)}
    order += [pk for pk, _key in sorted(extra, key=lambda e: rank[e[1]])][:EXAMPLE_CARDS]
    found = {v.pk: v for v in Visit.objects.filter(pk__in=order).select_related("partner", "pd")}
    return [privacy.visit_card(found[pk], names_) for pk in order if pk in found]


# ------------------------------------------------------------------------------------------ narratives
NARRATIVE_COLUMNS = (
    "finding_id",
    "rating",
    "datamart_id",
    "urgency",
    "visit__key",
    "visit__visit_date",
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
    """Every record of the scope with a narrative, in the order notes are sampled: the off-track and
    constrained records (the most urgent record first, by its own urgency), then the red and amber
    records, then round-robin across sections, and within a section across governorates, newest
    first."""
    limits = limits or metrics.thresholds()
    rows = [
        dict(zip(NARRATIVE_COLUMNS, row, strict=True))
        for row in scope.records()
        .filter(finding_id__isnull=False, narrative_words__gt=0)
        .values_list(*NARRATIVE_COLUMNS)
    ]

    def urgent(row):
        return (
            -_urgency(row["urgency"]),
            -row["visit__visit_date"].toordinal(),
            row["visit__key"],
            row["datamart_id"],
        )

    def newest(row):
        return (-row["visit__visit_date"].toordinal(), row["visit__key"], row["datamart_id"])

    first = sorted((r for r in rows if r["rating"] in ("off_track", "constrained")), key=urgent)
    used = {id(r) for r in first}
    second = sorted(
        (r for r in rows if id(r) not in used and _urgency(r["urgency"]) >= limits["amber"]),
        key=urgent,
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
    """The notes sent (at most ``limit``, never the same text twice), each with the Q1, Q2 and Q3
    answers of its finding row (:func:`_with_answers`), and how many were withheld (more than 3
    placeholders after cleaning, or too short)."""
    if limit <= 0:
        return [], 0
    out, findings, withheld = _notes_sampled(scope, limit, names_, limits)
    return _with_answers(out, findings, names_), withheld


def _notes_sampled(
    scope: Scope, limit: int, names_: frozenset[str], limits: dict[str, int]
) -> tuple[list[dict[str, Any]], list[int], int]:
    from neurodb.datamart.models import MonitoringFinding

    order = narrative_order(scope, limits)
    out: list[dict[str, Any]] = []
    findings: list[int] = []
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
                    "rated_on": None  # the end date: a rating is given when the visit ends
                    if _not_rated_yet(rating, row["visit__status_group"]) or row["visit__end_date"] is None
                    else row["visit__end_date"].isoformat(),
                    "text": text,
                }
            )
            findings.append(row["finding_id"])
            if len(out) >= limit:
                return out, findings, withheld
    return out, findings, withheld


ROLES = ("q1", "q2", "q3")


def _with_answers(notes: list[dict[str, Any]], findings: list[int], names_) -> list[dict[str, Any]]:
    """Each note with its finding row's Q1, Q2 and Q3 answers (``q1``...), cleaned like the note and
    cut to ``ANSWER_CHARS``: the answers written on the row (``hact_q1_answer``...) first, else the
    checklist answers of that question given for that row, else for the whole visit. An answer with more
    than three names, contacts or links removed is left out."""
    from .. import fields, parse
    from ..models import QuestionAnswer

    if not notes:
        return notes
    written = parse.row_texts(findings, [f"{role}_answer" for role in ROLES])
    found = {
        pk: {name.split("_", 1)[0]: text for name, text in texts.items()} for pk, texts in written.items()
    }
    visits = {pk: note["visit"].split(":", 1)[1] for pk, note in zip(findings, notes, strict=True)}
    answers = QuestionAnswer.objects.filter(role__in=ROLES, answered=True).filter(
        Q(entity__finding_id__in=findings) | Q(visit__key__in=set(visits.values()), applies_to="visit")
    )
    rows = list(answers.values_list("document_id", "role", "entity__finding_id", "visit_key", "applies_to"))
    texts = parse.answer_texts(
        [r[0] for r in rows], {f: fields.key_for("fm_questions", f) for f in ("answer", "answer_label")}
    )
    by_entity: dict[tuple[int, str], str] = {}
    by_visit: dict[tuple[str, str], str] = {}
    for document, role, finding, visit_key, applies_to in sorted(rows):
        text = texts.get(document)
        if not text:
            continue
        if finding is not None and applies_to == "entity":
            by_entity.setdefault((finding, role), text)
        elif applies_to == "visit":
            by_visit.setdefault((visit_key, role), text)
    out = []
    for pk, note in zip(findings, notes, strict=True):
        note = dict(note)
        for role in ROLES:
            raw = found[pk].get(role) or by_entity.get((pk, role)) or by_visit.get((visits[pk], role))
            if not raw:
                continue
            text, placeholders = privacy.clean(raw, ANSWER_CHARS, names_)
            if text and placeholders <= NARRATIVE_MAX_PLACEHOLDERS:
                note[role] = text
        out.append(note)
    return out


def sample_narratives(scope: Scope, limit: int, names_: frozenset[str] | None = None) -> list[dict]:
    """Up to ``limit`` monitors' notes of the scope, cleaned, in the order of :func:`narrative_order`.
    Deterministic."""
    names_ = privacy.names() if names_ is None else names_
    return _sample(scope, limit, names_, metrics.thresholds())[0]


# ------------------------------------------------------------------------------------------ the facts
def input_hash(payload: dict[str, Any], version=None) -> str:
    """sha256 of what decides the answer: the whole prompt, the model, effort, output limit, the
    sampling asked, the answer format's version, what the facts are made of (``FACTS_VERSION``) and the
    payload."""
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
    parts.append(f"facts:{FACTS_VERSION}")
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
    """The facts of a brief of ``scope`` written with ``version``: its ``narr`` (the narratives, each with
    its Q1, Q2 and Q3 answers) and its ``comp`` (the compliance depth: how many of the most frequent
    quality flags); without a version, for a code-written brief: no notes and 15 flags.
    ``narratives=False`` sends no note."""
    from ..models import RuleSetting

    names_ = privacy.names() if names_ is None else names_
    when = metrics.stamp()
    limits = metrics.thresholds()
    rules = list(RuleSetting.objects.order_by("code"))
    narr = version.narratives_sampled if version is not None and narratives else 0
    comp = version.comparison_visits if version is not None else DEFAULT_COMP

    kpi = _kpi(scope, when, limits)
    issues = _issues(scope, when, rules, comp)
    examples = [k.split(":", 1)[1] for issue in issues.values() for k in issue["visit_keys"]]
    visit_cards = cards(scope, VISIT_CARDS, names_, limits, examples)
    carded = {card["key"] for card in visit_cards}
    for issue in issues.values():  # a visit key the payload names is always one a sentence may cite
        issue["visit_keys"] = [k for k in issue["visit_keys"] if k in carded]
    breakdowns = _breakdowns(scope, names_)
    payload: dict[str, Any] = {
        "scope": _scope_entry(scope, names_),
        "kpi": kpi,
        **({"previous": _previous(scope, kpi, when, limits)} if scope.preset != "all_time" else {}),
        "rules": _rules(scope, when, rules),
        "issues": issues,
        "sections": _sections(scope, when, limits, names_),
        "offices": _offices(scope, when, limits, names_),
        "partners": breakdowns["partners"],
        "modalities": breakdowns["modalities"],
        "places": _places(scope, when, limits, names_, breakdowns["governorates"]),
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
            "flags": len(issues),
            "flags_allowed": comp,
            "visits": len(visit_cards),
        },
        input_hash=input_hash(payload, version),
        limits=limits,
    )
