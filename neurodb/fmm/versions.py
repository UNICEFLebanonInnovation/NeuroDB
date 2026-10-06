"""Versions of the quality rules: every save is kept, nothing is edited live.

Each save of a rule, of the score settings, of a question pattern or of a key an administrator pins
(Fields found) records a new :class:`~neurodb.fmm.models.RuleSetVersion`: a snapshot of every rule,
the score settings and the pinned keys, with who saved it, when and why (a note is required). Version
1 holds the defaults. Restoring an older version writes its snapshot back and records a new version
that says where it came from (``restored_from``), so the history only grows.

After a save the scores are recomputed in the background (:func:`start_rescore`: a scores-only
refresh, or a full one when a pinned key changed), never in the admin's request; the refresh serves
the request even when another refresh holds its lock. Every visit keeps the rules version it was
scored with, so the page can say "recomputing with rules v8" until it is done.

:func:`preview` scores this year's visits in memory with a change not saved yet, to show its effect
("R2 would flag 7 visits (now 3)") before it is saved; it writes nothing. :func:`pin_question` gives one
checklist question a role ("Use as Q1" in Questions found), recorded like any other save.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from . import fields, parse, rules, score
from .models import FieldMapping, RuleSetting, RuleSetVersion, ScoreSetting, Visit

ROLE_LABELS = {"q1": "Q1", "q2": "Q2", "q3": "Q3", "psea": "PSEA"}
PATTERN_CHARS = 200  # the longest question pattern the score settings take
SCORE_FIELDS = (
    "min_evaluated_points",
    "band_high",
    "band_medium",
    "high_flag_count",
    "urgency_red",
    "urgency_amber",
    "urgency_weights",
    "recency_days",
    "scored_statuses",
    "follow_up_days",
    "report_late_days",
    "question_patterns",
    "role_flag_answers",
)
RULE_FIELDS = ("label", "enabled", "points", "threshold", "params", "description")
NOTE_CHARS = 200


def _threshold(value: Decimal | None) -> int | float | None:
    if value is None:
        return None
    value = Decimal(str(value))
    return int(value) if value == value.to_integral_value() else float(value)


def snapshot_rules() -> dict:
    """The rules, the score settings and the pinned keys as they are now."""
    rows = []
    for rule in RuleSetting.objects.order_by("code"):
        rows.append(
            {
                "code": rule.code,
                "label": rule.label,
                "enabled": rule.enabled,
                "points": rule.points,
                "threshold": _threshold(rule.threshold),
                "params": rule.params,
                "description": rule.description,
            }
        )
    setting = ScoreSetting.load()
    mappings = {
        f"{m.dataset}.{m.field}": m.override_key.strip()
        for m in FieldMapping.objects.exclude(override_key="").order_by("dataset", "field")
        if m.override_key.strip()
    }
    return {
        "rules": rows,
        "score": {name: getattr(setting, name) for name in SCORE_FIELDS},
        "mappings": mappings,
    }


def current_rules_version() -> int:
    """The number of the latest rules version (1 after the defaults are seeded; 0 before)."""
    return RuleSetVersion.objects.aggregate(n=Max("number"))["n"] or 0


def _name(user) -> str:
    if user is None:
        return "NeuroDB"
    full = (user.get_full_name() or "").strip() if hasattr(user, "get_full_name") else ""
    return (full or user.get_username())[:150]


def record_rules(user, note: str, restored_from: RuleSetVersion | None = None) -> RuleSetVersion:
    """Record the rules as they are now as a new version, with who saved them and why."""
    snapshot = snapshot_rules()
    for attempt in range(5):  # two saves at the same moment: the second takes the next number
        number = current_rules_version() + 1
        try:
            with transaction.atomic():
                return RuleSetVersion.objects.create(
                    number=number,
                    snapshot=snapshot,
                    note=(note or "").strip()[:NOTE_CHARS],
                    created_by=user if getattr(user, "pk", None) else None,
                    created_by_name=_name(user),
                    restored_from=restored_from,
                )
        except IntegrityError:
            if attempt == 4:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def start_rescore(user, *, full: bool = False) -> None:
    """Ask for the scores to be recomputed in the background (a full refresh when a pinned key
    changed): the request is kept, and the command starts once the save is committed."""
    from . import refresh

    who = f"admin:{user.pk}" if getattr(user, "pk", None) else "admin"
    refresh.request("full" if full else "scores", who)


def restore_rules(version: RuleSetVersion, user, note: str = "") -> RuleSetVersion:
    """Write ``version``'s snapshot back (rules, score settings and pinned keys), record it as a new
    version and ask for the scores to be recomputed (a full refresh when the pinned keys differ)."""
    snapshot = version.snapshot or {}
    with transaction.atomic():
        for row in snapshot.get("rules") or []:
            code = row.get("code")
            if code not in rules.CODES:
                continue
            values = {name: row[name] for name in RULE_FIELDS if name in row}
            if "threshold" in values and values["threshold"] is not None:
                values["threshold"] = Decimal(str(values["threshold"]))
            RuleSetting.objects.update_or_create(code=code, defaults={**values, "updated_by": _user(user)})
        setting = ScoreSetting.load()
        weights = setting.urgency_weights
        for name, value in (snapshot.get("score") or {}).items():
            if name in SCORE_FIELDS:
                setattr(setting, name, value)
        if not score.valid_weights(setting.urgency_weights):  # a version saved before Release 2's urgency
            setting.urgency_weights = weights
        setting.updated_by = _user(user)
        setting.save()
        changed = _restore_mappings(snapshot.get("mappings") or {}, user)
        text = f"Restored v{version.number}" + (f": {note.strip()}" if (note or "").strip() else "")
        new = record_rules(user, text, restored_from=version)
        start_rescore(user, full=changed)
        transaction.on_commit(fields.forget)
    return new


def _user(user):
    return user if getattr(user, "pk", None) else None


def _restore_mappings(wanted: Mapping[str, str], user) -> bool:
    """Pin the keys of a snapshot (and unpin the others); True when one changed."""
    changed = False
    current = {f"{m.dataset}.{m.field}": m for m in FieldMapping.objects.all()}
    for name, mapping in current.items():
        key = (wanted.get(name) or "").strip()
        if mapping.override_key.strip() != key:
            mapping.override_key, mapping.updated_by = key, _user(user)
            mapping.save(update_fields=["override_key", "updated_by", "updated_at"])
            changed = True
    for name, key in wanted.items():
        dataset, _, field = name.partition(".")
        if name in current or field not in fields.CANDIDATES.get(dataset, {}) or not (key or "").strip():
            continue
        FieldMapping.objects.create(
            dataset=dataset, field=field, override_key=key.strip(), updated_by=_user(user)
        )
        changed = True
    return changed


def question_pattern(question_text: str) -> str:
    """The pattern that pins one checklist question: "=" and its whole folded text; for a text too long
    for a pattern, "^" and its first words (the question starting with them)."""
    folded = parse.fold(question_text)
    if len(folded) + 1 <= PATTERN_CHARS:
        return "=" + folded
    start = ""
    for word in folded.split():
        longer = f"{start} {word}" if start else word
        if len(longer) + 1 > PATTERN_CHARS:
            break
        start = longer
    return "^" + start


def pin_question(role: str, question_text: str, user) -> RuleSetVersion:
    """Give one checklist question a role ("Use as Q1" in Questions found): its pattern
    (:func:`question_pattern`) is added to the role's patterns and taken out of the other roles',
    recorded as a new rules version, and the scores are recomputed in the background. A
    ``ValidationError`` when the settings would not be valid (more than 20 patterns for the role)."""
    if role not in rules.ROLES:
        raise ValueError(f"unknown role {role!r}")
    pattern = question_pattern(question_text)
    if len(pattern) < 2:
        raise ValueError("the question has no letter or digit")
    with transaction.atomic():
        setting = ScoreSetting.objects.select_for_update().get(pk=ScoreSetting.load().pk)
        current = setting.question_patterns if isinstance(setting.question_patterns, dict) else {}
        patterns = {name: [p for p in values if p != pattern] for name, values in current.items()}
        patterns[role] = [*patterns.get(role, []), pattern]
        setting.question_patterns = patterns
        setting.updated_by = _user(user)
        setting.full_clean()
        setting.save()
        version = record_rules(user, f"{ROLE_LABELS[role]} pattern set from Questions found")
        start_rescore(user)
    return version


def saved_message(version: RuleSetVersion, full: bool = False) -> str:
    """What an administrator reads after a save (``full``: a pinned key, which rebuilds the visits)."""
    if full:
        return (
            f"Saved as rules v{version.number}. The visits will be rebuilt with this key in the background; "
            f"the page shows 'recomputing with rules v{version.number}' until they are."
        )
    return (
        f"Saved as rules v{version.number}. Scores will be recomputed in the background; the page shows "
        f"'recomputing with rules v{version.number}' until they are."
    )


# ------------------------------------------------------------------------------------------ preview
def preview(
    rule_changes: Mapping[str, dict],
    score_changes: dict,
    start: date | None = None,
    end: date | None = None,
) -> dict[str, Any]:
    """The effect of a change not saved yet, scored in memory over the visits that ended between
    ``start`` and ``end`` (this calendar year by default, the whole country): per rule, the visits it
    flags now and with the change; the average quality and the visits scored, now and with the
    change. Nothing is written."""
    from datetime import timedelta

    from . import score

    today = timezone.localdate()
    start = start or date(today.year, 1, 1)
    end = end or date(today.year, 12, 31)
    now_book = score.Rulebook.load()
    then_book = now_book.with_changes(rule_changes, score_changes)
    window = max(int(rules.param(book.rules["R4"], "copy_window_days")) for book in (now_book, then_book))
    figures = {}
    for name, book in (("now", now_book), ("then", then_book)):
        visits = list(Visit.objects.filter(end_date__range=(start, end)).order_by("pk"))
        nearby = Visit.objects.filter(
            end_date__range=(start - timedelta(days=window), end + timedelta(days=window))
        ).only("pk", "key", "label", "end_date")
        source = score.StoredSource(visits, copy_visits=nearby)
        score.score_visits(source, book, today)
        figures[name] = {
            "flagged": {code: sum(1 for v in visits if code in v.flags) for code in rules.CODES},
            "avg_quality": score.average_quality(v.quality_score for v in visits),
            "scored": sum(1 for v in visits if v.quality_score is not None),
            "visits": len(visits),
        }
    now, then = figures["now"], figures["then"]
    return {
        "start": start,
        "end": end,
        "visits": now["visits"],
        "rules": {code: {"now": now["flagged"][code], "then": then["flagged"][code]} for code in rules.CODES},
        "avg_quality": {"now": now["avg_quality"], "then": then["avg_quality"]},
        "scored": {"now": now["scored"], "then": then["scored"]},
    }


def describe(result: Mapping[str, Any], codes: list[str] | None = None) -> str:
    """The preview in one sentence: "R2 would flag 7 visits (now 3); average quality 91.2% (now
    94.7%); scored visits 29 (now 29)". ``codes``: the rules to name (else those whose count moves)."""

    def visits(n: int) -> str:
        return f"{n} visit" if n == 1 else f"{n} visits"

    def pct(value) -> str:
        return "—" if value is None else f"{value}%"

    shown = codes or [c for c, n in result["rules"].items() if n["now"] != n["then"]]
    parts = [
        f"{code} would flag {visits(result['rules'][code]['then'])} (now {result['rules'][code]['now']})"
        for code in shown
    ]
    quality = result["avg_quality"]
    parts.append(f"average quality {pct(quality['then'])} (now {pct(quality['now'])})")
    scored = result["scored"]
    parts.append(f"scored visits {scored['then']} (now {scored['now']})")
    return "; ".join(parts)


# ------------------------------------------------------------------------------------------ the diff
def differences(
    snapshot: Mapping[str, Any], current: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Each setting of ``snapshot`` beside its value now: [{"group", "name", "then", "now", "changed"}]."""
    current = current if current is not None else snapshot_rules()
    rows: list[dict[str, Any]] = []
    now_rules = {r["code"]: r for r in current.get("rules") or []}
    for rule in snapshot.get("rules") or []:
        other = now_rules.get(rule.get("code"), {})
        for name in RULE_FIELDS:
            if name == "description":
                continue
            then, now = rule.get(name), other.get(name)
            rows.append(
                {
                    "group": rule.get("code"),
                    "name": name,
                    "then": then,
                    "now": now,
                    "changed": _differ(then, now),
                }
            )
    now_score = current.get("score") or {}
    for name in SCORE_FIELDS:
        then, now = (snapshot.get("score") or {}).get(name), now_score.get(name)
        rows.append({"group": "Score", "name": name, "then": then, "now": now, "changed": _differ(then, now)})
    then_map, now_map = snapshot.get("mappings") or {}, current.get("mappings") or {}
    for name in sorted(set(then_map) | set(now_map)):
        then, now = then_map.get(name, ""), now_map.get(name, "")
        row = {"group": "Pinned keys", "name": name, "then": then or "auto", "now": now or "auto"}
        rows.append({**row, "changed": then != now})
    return rows


def _differ(a: Any, b: Any) -> bool:
    if isinstance(a, int | float) and isinstance(b, int | float) and not isinstance(a, bool):
        return float(a) != float(b)
    return a != b
