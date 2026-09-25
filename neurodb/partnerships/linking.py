"""Link the ActivityInfo partner names to the eTools partners.

Partners reported in ActivityInfo (2017 → 2026) under whatever name the database held for them; they
report in eTools/PRP under their vendor record. Nothing in either system points at the other, so the
partner page needs a bridge: :class:`PartnerLink`, one row per ActivityInfo partner label, resolved by

1. a decision **taken by hand** in the admin (a partner, or "none": never overwritten);
2. the **same name** (accents, case, underscores and punctuation ignored) as an eTools partner's
   name, short name or alternate name — when only one partner carries it;
3. the **programme document** the records name (``project_label`` → ``PCA.number``): the partner
   of the PD most of the label's records fall under; a tie is left for the admin to decide.

Runs after every ActivityInfo import and eTools sync (``manage link_partners``) and from the admin.
A failure is recorded on the run and never stops the import that called it.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any

from django.db import transaction
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.facts import queries
from neurodb.integrations.runs import fail
from neurodb.partnerships.models import PCA, PartnerLink, PartnerOrganization

logger = logging.getLogger(__name__)

IGNORED_LABELS = {"unicef"}  # UNICEF's own records are not a partner's reporting
_NOISE = re.compile(r"[^a-z0-9 ]+")
_STOP_PREFIXES = ("the ",)


def normalise_name(text: str | None) -> str:
    """``"Amel_Association"``, ``"AMEL Association"`` and ``"Amel association (Lebanon)"`` compare equal."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    text = _NOISE.sub(" ", text.replace("_", " ").replace("-", " ").replace("/", " "))
    text = " ".join(text.split())
    for prefix in _STOP_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def _name_index(partners: list[PartnerOrganization]) -> dict[str, int]:
    """normalised name → partner id, without the names two partners share."""
    seen: dict[str, set[int]] = defaultdict(set)
    for p in partners:
        for name in (p.name, p.short_name, p.alternate_name):
            key = normalise_name(name)
            if len(key) >= 3:
                seen[key].add(p.id)
    return {key: next(iter(ids)) for key, ids in seen.items() if len(ids) == 1}


def _pd_index(names: dict[str, int], live: set[int]) -> dict[str, int]:
    """PD number without its amendment suffix → id of a live partner.

    A PD synced without its partner foreign key (v2 left some with the partner's name only), or
    still pointing at a partner since deleted in eTools, counts when its partner name resolves.
    """
    index: dict[str, int] = {}
    for number, partner_id, partner_name in PCA.objects.exclude(number=None).values_list(
        "number", "partner_id", "partner_name"
    ):
        if partner_id not in live:
            partner_id = names.get(normalise_name(partner_name))
        if partner_id:
            index.setdefault(number.split("-")[0], partner_id)
    return index


def _labels() -> dict[str, dict[str, Any]]:
    """The activity records folded per partner label (kept exactly as the records spell it)."""
    labels: dict[str, dict[str, Any]] = {}
    for row in queries.activityinfo_partner_rows():
        label = row["label"] or ""
        if not label.strip() or normalise_name(label) in IGNORED_LABELS:
            continue
        entry = labels.setdefault(
            label,
            {"records": 0, "first_month": "", "last_month": "", "database_ids": set(), "pds": Counter()},
        )
        entry["records"] += row["records"]
        if row["first_month"] and (not entry["first_month"] or row["first_month"] < entry["first_month"]):
            entry["first_month"] = row["first_month"]
        if row["last_month"] and row["last_month"] > entry["last_month"]:
            entry["last_month"] = row["last_month"]
        entry["database_ids"].update(row["database_ids"] or [])
        if row["pd_number"]:
            entry["pds"][row["pd_number"]] += row["records"]
    return labels


def _match(
    label: str, entry: dict[str, Any], names: dict[str, int], pds: dict[str, int]
) -> tuple[int | None, str, list[int]]:
    """(partner id, method, tied partner ids): the tie list is filled when the PDs disagree."""
    partner_id = names.get(normalise_name(label))
    if partner_id:
        return partner_id, PartnerLink.Method.NAME, []
    votes: Counter[int] = Counter()
    for number, records in entry["pds"].items():
        if number in pds:
            votes[pds[number]] += records
    ranked = sorted(votes.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None, PartnerLink.Method.NONE, [pid for pid, n in ranked if n == ranked[0][1]]
    if ranked:
        return ranked[0][0], PartnerLink.Method.PD, []
    return None, PartnerLink.Method.NONE, []


def _refresh(run: SyncRun) -> None:
    partners = list(PartnerOrganization.objects.filter(deleted_flag=False))
    names = _name_index(partners)
    pds = _pd_index(names, {p.id for p in partners})
    existing = {link.label: link for link in PartnerLink.objects.all()}
    stats: Counter[str] = Counter()
    unlinked: list[str] = []
    ambiguous: dict[str, list[int]] = {}
    for label, entry in sorted(_labels().items()):
        link = existing.pop(label, None) or PartnerLink(label=label)
        if link.method == PartnerLink.Method.MANUAL:  # a partner, or "none", chosen by hand
            partner_id, method = link.partner_id, link.method
        else:
            partner_id, method, tied = _match(label, entry, names, pds)
            if tied:
                ambiguous[label] = tied
        link.partner_id, link.method = partner_id, method
        link.records = entry["records"]
        link.first_month, link.last_month = entry["first_month"], entry["last_month"]
        link.database_ids = sorted(entry["database_ids"])
        link.save()
        stats[method or "unlinked"] += 1
        if not partner_id:
            unlinked.append(label)
    # names no longer in the records: forget the automatic rows, empty the hand-made ones
    stale = list(existing.values())
    PartnerLink.objects.filter(pk__in=[s.pk for s in stale if s.method != PartnerLink.Method.MANUAL]).delete()
    PartnerLink.objects.filter(pk__in=[s.pk for s in stale if s.method == PartnerLink.Method.MANUAL]).update(
        records=0, first_month="", last_month="", database_ids=[]
    )
    run.rows_in = sum(stats.values())
    run.rows_written = run.rows_in - stats["unlinked"]
    run.finish(
        SyncRun.Status.SUCCEEDED,
        labels=run.rows_in,
        linked_by_name=stats[PartnerLink.Method.NAME],
        linked_by_pd=stats[PartnerLink.Method.PD],
        set_by_hand=stats[PartnerLink.Method.MANUAL],
        unlinked=stats["unlinked"],
        unlinked_examples=unlinked[:20],
        ambiguous=len(ambiguous),
        ambiguous_examples=dict(list(ambiguous.items())[:20]),
        removed=len(stale),
        partners=len(partners),
    )


def link_activityinfo_partners(*, triggered_by: str = "schedule") -> SyncRun:
    """Refresh the link table from the activity records and the eTools partners; one ``SyncRun``.

    Returns the run, FAILED when the refresh raised (the table is then unchanged): the imports that
    call this at their end report it and go on.
    """
    run = SyncRun.objects.create(job=SyncRun.Job.PARTNER_LINKS, triggered_by=triggered_by)
    try:
        with transaction.atomic():
            _refresh(run)
    except Exception as exc:
        return fail(run, exc)
    logger.info("partner links: %s", dict(run.details))
    return run


def _live_links():
    return PartnerLink.objects.filter(partner__isnull=False, partner__deleted_flag=False)


def partner_labels(partner: PartnerOrganization) -> list[str]:
    """The ActivityInfo names under which this eTools partner reported, most records first."""
    links = PartnerLink.objects.filter(partner=partner, records__gt=0).order_by("-records", "label")
    return list(links.values_list("label", flat=True))


def links_for(labels: list[str]) -> dict[str, PartnerOrganization]:
    """ActivityInfo label → eTools partner, for the labels linked to a partner still in eTools."""
    links = _live_links().filter(label__in=labels).select_related("partner")
    return {link.label: link.partner for link in links}


def last_run() -> SyncRun | None:
    return SyncRun.last_success(SyncRun.Job.PARTNER_LINKS)


def stale(hours: int = 48) -> bool:
    run = last_run()
    return run is None or run.finished_at < timezone.now() - timezone.timedelta(hours=hours)
