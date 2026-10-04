"""How useful the checks of NeuroDB Watch are, from what people said about what they were told.

People react to each point on their For you page: Useful, Not useful, Done, Not mine, Something's
wrong (``WatchReceipt.reaction``: one per person and point, the latest). This module turns those
reactions into:

- **a usefulness score per check** (:func:`scores`), over the last 30 days: how many people were told
  about its points, and how many said Useful, Not useful, Something's wrong and Not mine. The score is
  useful / (useful + not useful + something's wrong). "Done" and "Not mine" are left out of it: one
  is about the work, the other about who was told, not about the check. The admin shows the score
  beside each check (Data and sync → NeuroDB Watch: checks);
- **a check going back to trial by itself** (:func:`demote`, the "usefulness" step of the morning
  pass): a check that is on for staff goes back to trial when, among at least 20 ratings since it was
  switched on (and within the last 30 days), more than 40% said Not useful or Something's wrong. Its
  points then go to the whole-country view only, and the admin home's "Needs attention" says so
  (:func:`needs_attention`) until an administrator switches it on again, keeps it in trial or turns
  it off. Points told to the administrators only (system points, donor accounts, the yearly
  rollover) do not count: a trial changes nothing for them;
- **"Not mine" per check and section** (:func:`not_mine`): who said a point was not theirs, by the
  NeuroDB section they belong to and the eTools section name that sent the point to them. The admin
  shows the counts beside each eTools section name: many "Not mine" from one section on one name point
  to a wrong match;
- **the usefulness gate** (:func:`gate`): at least half of the "Needs you" points people rated over
  the last 4 weeks were useful, with at least 20 ratings, once a check has been on for staff for 4
  weeks. The admin shows it for information only: the background look-ups
  (:mod:`neurodb.watch.investigate`) do not wait for it.

Everything here only reads, except :func:`demote`, which changes ``DetectorSetting`` rows and nothing
else. Who reacted, and their comments, never leave the admin: not to the AI, not by email.
"""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from neurodb.datamart.monitoring import norm

from . import detectors
from .detectors import ADMINS, ON, STALE_DETECTOR, SYSTEM, TRIAL
from .models import DetectorSetting, WatchReceipt

Reaction = WatchReceipt.Reaction

WINDOW_DAYS = 30  # the scores and the "Not mine" counts look this far back
MIN_RATINGS = 20  # a check goes back to trial only with at least this many ratings; the gate too
DEMOTE_SHARE = 0.4  # more than this share of Not useful and Something's wrong: back to trial
GATE_DAYS = 28  # the usefulness gate looks at the last 4 weeks
GATE_SHARE = 0.5  # at least this share of the rated "Needs you" points were useful
RATED = (Reaction.USEFUL, Reaction.NOT_USEFUL, Reaction.WRONG)  # the reactions a score counts
COUNTED = (*RATED, Reaction.NOT_MINE)
DEMOTED_BY = "NeuroDB Watch"  # DetectorSetting.updated_by when a check went back to trial by itself
# Checks whose points go to the administrators only, whatever their mode: never demoted, and not
# what "on for staff" means for the gate
ADMIN_CHECKS = frozenset({"system", STALE_DETECTOR, "openai_quota"})
# Labels of the checks that are not in the registry (see detectors._stale_result and budget.QUOTA_CHECK)
LABELS = {STALE_DETECTOR: "Data sources not refreshed", "openai_quota": "OpenAI credit"}


# ---------------------------------------------------------------------------- the scores
@dataclass(frozen=True)
class Score:
    """One check's numbers over the window: people told about its points (receipts other than
    "known") and the reactions given (the latest one per person and point)."""

    detector: str
    told: int = 0
    useful: int = 0
    not_useful: int = 0
    wrong: int = 0
    not_mine: int = 0

    @property
    def label(self) -> str:
        return label(self.detector)

    @property
    def rated(self) -> int:
        """Ratings that say whether the check is useful: Useful, Not useful, Something's wrong."""
        return self.useful + self.not_useful + self.wrong

    @property
    def unhelpful(self) -> int:
        return self.not_useful + self.wrong

    @property
    def score(self) -> float | None:
        """useful / (useful + not useful + something's wrong); None without a rating."""
        return self.useful / self.rated if self.rated else None

    @property
    def unhelpful_share(self) -> float | None:
        return self.unhelpful / self.rated if self.rated else None


def label(detector_id: str) -> str:
    """A check's name in plain words ("Action points due soon"), else its id."""
    if detector_id in LABELS:
        return LABELS[detector_id]
    check = detectors.get(detector_id)
    return check.label if check is not None else detector_id


def percent(part: int, whole: int) -> int:
    """``part`` of ``whole`` as a whole percentage, rounded half up (0 when ``whole`` is 0)."""
    return (200 * part + whole) // (2 * whole) if whole else 0


def _start_of(day: datetime.date) -> datetime.datetime:
    """Midnight at the start of ``day``, Beirut time."""
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=timezone.get_current_timezone())


def _window(today: datetime.date | None, days: int = WINDOW_DAYS) -> datetime.date:
    return (today or timezone.localdate()) - datetime.timedelta(days=days)


def _staff_points() -> Q:
    """Receipts of points that are not the administrators' only (system, donor accounts, rollover)."""
    return ~Q(item__kind=SYSTEM) & ~Q(item__scope=ADMINS)


def _reactions(since: datetime.date, *filters: Q, **lookups: Any) -> dict[str, Counter]:
    """check -> Counter of the reactions given on its points from ``since`` (Beirut) on."""
    rows = (
        WatchReceipt.objects.filter(
            *filters, reacted_at__gte=_start_of(since), reaction__in=COUNTED, **lookups
        )
        .order_by()
        .values_list("item__detector", "reaction")
        .annotate(n=Count("pk"))
    )
    found: dict[str, Counter] = defaultdict(Counter)
    for detector, reaction, n in rows:
        found[detector][reaction] += n
    return found


def scores(today: datetime.date | None = None) -> dict[str, Score]:
    """Every check's :class:`Score` over the last 30 days, by check id: each check with a setting
    (zeros when nobody was told or reacted yet) and each check people reacted to."""
    since = _window(today)
    told = dict(
        WatchReceipt.objects.filter(last_told_on__gte=since)
        .exclude(told_step=WatchReceipt.KNOWN)
        .order_by()
        .values_list("item__detector")
        .annotate(n=Count("pk"))
        .values_list("item__detector", "n")
    )
    reacted = _reactions(since)
    checks = set(DetectorSetting.objects.values_list("detector", flat=True)) | set(told) | set(reacted)
    return {
        check: Score(
            detector=check,
            told=told.get(check, 0),
            useful=reacted[check][Reaction.USEFUL],
            not_useful=reacted[check][Reaction.NOT_USEFUL],
            wrong=reacted[check][Reaction.WRONG],
            not_mine=reacted[check][Reaction.NOT_MINE],
        )
        for check in sorted(checks)
    }


# ---------------------------------------------------------------------------- back to trial
def demotion(setting: DetectorSetting, today: datetime.date | None = None) -> Score:
    """What counts towards sending one check back to trial: the ratings of its points told to staff
    (not the administrators' only), given since it was switched on and within the last 30 days."""
    since = _window(today)
    if setting.on_since is not None:
        since = max(since, setting.on_since)
    counts = _reactions(since, _staff_points(), item__detector=setting.detector)[setting.detector]
    return Score(
        detector=setting.detector,
        useful=counts[Reaction.USEFUL],
        not_useful=counts[Reaction.NOT_USEFUL],
        wrong=counts[Reaction.WRONG],
        not_mine=counts[Reaction.NOT_MINE],
    )


def too_unhelpful(score: Score) -> bool:
    """At least 20 ratings, more than 40% of them Not useful or Something's wrong."""
    return score.rated >= MIN_RATINGS and score.unhelpful > DEMOTE_SHARE * score.rated


def reason(score: Score) -> str:
    """Why a check went back to trial, in plain words (kept on its setting, shown by the admin home)."""
    pct = str(percent(score.unhelpful, score.rated))
    if int(pct) <= DEMOTE_SHARE * 100:  # just above the threshold: rounding would hide it
        pct = f"{100 * score.unhelpful / score.rated:.1f}"
    return _("%(pct)s%% of %(n)s reactions said not useful or something's wrong") % {
        "pct": pct,
        "n": score.rated,
    }


def demote(today: datetime.date | None = None, now: datetime.datetime | None = None) -> dict[str, Any]:
    """Send back to trial each check that is on for staff and that people found unhelpful (see
    :func:`too_unhelpful`). The checks told to the administrators only are never moved. Returns, for
    the run's details, how many checks were looked at and the reason of each one sent back."""
    today = today or timezone.localdate()
    now = now or timezone.now()
    demoted: dict[str, str] = {}
    looked_at = 0
    for setting in DetectorSetting.objects.filter(mode=ON).exclude(detector__in=ADMIN_CHECKS):
        looked_at += 1
        score = demotion(setting, today)
        if not too_unhelpful(score):
            continue
        setting.mode = TRIAL
        setting.trial_since = today  # what exists today is known to the whole-country view: not announced
        setting.demoted_at = now
        setting.demoted_reason = reason(score)[:300]
        setting.updated_by = DEMOTED_BY
        setting.save(
            update_fields=["mode", "trial_since", "demoted_at", "demoted_reason", "updated_by", "updated_at"]
        )
        demoted[setting.detector] = setting.demoted_reason
    return {"checks": looked_at, "demoted": demoted}


def demoted() -> list[DetectorSetting]:
    """The checks that went back to trial by themselves and are still in trial, as no administrator
    has decided since."""
    return list(
        DetectorSetting.objects.filter(mode=TRIAL, demoted_at__isnull=False).order_by(
            "demoted_at", "detector"
        )
    )


def needs_attention() -> list[dict[str, Any]]:
    """Lines for the admin home's "Needs attention" (``neurodb.web.health``): one per check that went
    back to trial by itself. Each is ``{key, text, url, read, on, value}``; the key and the text stay
    the same while the check stays there. Nothing while the watch is switched off."""
    if not settings.WATCH_ENABLED:
        return []
    lines = []
    for setting in demoted():
        name = label(setting.detector)
        why = setting.demoted_reason or _("people found it unhelpful")
        lines.append(
            {
                "key": f"watch_check_demoted:{setting.detector}",
                "text": _("The check “%(check)s” went back to trial: %(why)s.") % {"check": name, "why": why},
                "url": reverse("admin:watch_detectorsetting_change", args=[setting.pk]),
                "read": _("Usefulness of the check “%(check)s”") % {"check": name},
                "on": timezone.localdate(setting.demoted_at),
                "value": why,
            }
        )
    return lines


# ---------------------------------------------------------------------------- not mine
def not_mine(today: datetime.date | None = None) -> Counter:
    """Count "Not mine" over the last 30 days by (check, NeuroDB section of the person, eTools
    section name of the point, normalised). A point with several eTools section names counts once
    under each; a point without one, under ``""``; a person without a section, under ``None``."""
    rows = WatchReceipt.objects.filter(
        reaction=Reaction.NOT_MINE, reacted_at__gte=_start_of(_window(today))
    ).values_list("item__detector", "user__section_id", "item__etools_sections")
    counts: Counter = Counter()
    for detector, section_id, names in rows:
        for name in {norm(name) for name in names or ()} or {""}:
            counts[(detector, section_id, name)] += 1
    return counts


def not_mine_for(etools_name: str, section_id: int | None, counts: Counter | None = None) -> dict[str, int]:
    """The "Not mine" from the staff of one NeuroDB section on points sent to them under one eTools
    section name, by check (most first): what may show that the name is matched to the wrong section."""
    counts = not_mine() if counts is None else counts
    if section_id is None:
        return {}
    name = norm(etools_name)
    found = Counter(
        {check: n for (check, section, each), n in counts.items() if section == section_id and each == name}
    )
    return dict(found.most_common())


# ---------------------------------------------------------------------------- the gate
@dataclass(frozen=True)
class Gate:
    """The usefulness gate: of the "Needs you" points people rated over the last 4 weeks (Useful, Not
    useful, Something's wrong; system points left out), how many were useful, and whether a check has
    been on for staff for 4 weeks. True when it passes."""

    useful: int = 0
    rated: int = 0
    long_enough: bool = False

    @property
    def share(self) -> float | None:
        return self.useful / self.rated if self.rated else None

    @property
    def passed(self) -> bool:
        return (
            self.long_enough
            and self.rated >= MIN_RATINGS
            and self.share is not None
            and self.share >= GATE_SHARE
        )

    def __bool__(self) -> bool:
        return self.passed

    @property
    def text(self) -> str:
        """The gate in plain words, for the admin."""
        if not self.rated:
            return _("No “Needs you” point was rated in the last 4 weeks.")
        said = _("%(pct)s%% of the %(n)s “Needs you” points people rated in the last 4 weeks were useful") % {
            "pct": percent(self.useful, self.rated),
            "n": self.rated,
        }
        if self.passed:
            return _("%(said)s: the gate is met.") % {"said": said}
        if not self.long_enough:
            why = _("no check has been on for staff for 4 weeks yet")
        elif self.rated < MIN_RATINGS:
            why = _("fewer than %(n)s ratings") % {"n": MIN_RATINGS}
        else:
            why = _("below %(pct)s%%") % {"pct": round(GATE_SHARE * 100)}
        return _("%(said)s: the gate is not met (%(why)s).") % {"said": said, "why": why}


def gate(today: datetime.date | None = None) -> Gate:
    """The usefulness gate (see :class:`Gate`; ``bool(gate())`` says whether it is met). For
    information: nothing waits for it in this release."""
    today = today or timezone.localdate()
    since = _window(today, GATE_DAYS)
    rows = (
        WatchReceipt.objects.filter(
            level=WatchReceipt.Level.NEEDS_YOU,
            reaction__in=RATED,
            reacted_at__gte=_start_of(since),
        )
        .exclude(item__kind=SYSTEM)
        .order_by()
        .values_list("reaction")
        .annotate(n=Count("pk"))
    )
    counts = dict(rows)
    long_enough = (
        DetectorSetting.objects.filter(mode=ON, on_since__lte=since)
        .exclude(detector__in=ADMIN_CHECKS)
        .exists()
    )
    return Gate(
        useful=counts.get(Reaction.USEFUL, 0),
        rated=sum(counts.values()),
        long_enough=long_enough,
    )
