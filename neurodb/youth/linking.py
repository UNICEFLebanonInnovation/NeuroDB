"""Suggesting which eTools indicator reports on each youth indicator.

A Compiler programme document carries the project code of the eTools programme document it funds
(``LEB/PCA2026005``); the eTools one has the same reference number, possibly with an amendment
suffix (``LEB/PCA2026005-1``). Within that programme document, the youth indicator (a sub indicator,
or the master indicator when no sub is used) is matched to the eTools indicator whose title holds most
of its words (at least 60%), with a bonus when both carry the same target; within a programme
document each eTools indicator goes to one youth indicator, the best match first. A suggestion stays
a suggestion until someone confirms it in the admin; the next sync replaces suggestions and never
touches confirmed links.
"""

from __future__ import annotations

import datetime
import re
from collections import defaultdict
from typing import Any

from django.db import transaction
from django.db.models import Q

from neurodb.datamart import monitoring
from neurodb.partnerships.models import PCA

from .figures import Figures, indicator_label
from .models import YouthIndicatorLink

MIN_SCORE = 0.6  # share of the youth indicator's words found in the eTools title
TARGET_BONUS = 0.25
STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "who",
    "are",
    "have",
    "has",
    "from",
    "their",
    "through",
    "into",
    "number",
    "percentage",
    "total",
    "including",
    "among",
    "per",
    "that",
    "which",
    "other",
    "all",
}


def year_number(year: str) -> int:
    head = str(year or "")[:4]
    return int(head) if head.isdigit() else datetime.date.today().year


def _segments(number: str | None) -> list[str]:
    """``"LEB/PCA2019123/PD2020001-2"`` -> ``["LEB", "PCA2019123", "PD2020001"]``."""
    text = re.sub(r"\s+", "", (number or "").upper())
    return [re.sub(r"-\d+$", "", part) for part in text.split("/") if part]


def match_pds(codes: set[str], year: int) -> dict[str, PCA]:
    """The eTools programme document of each Compiler project code: the same reference number
    (amendment suffix ignored), else a PD under the same agreement number running in ``year``."""
    wanted = {code: _segments(code) for code in codes if _segments(code)}
    if not wanted:
        return {}
    last_parts = {parts[-1] for parts in wanted.values()}
    query = Q()
    for part in last_parts:
        query |= Q(number__icontains=part)
    first, last = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    candidates = list(
        PCA.objects.filter(query)
        .exclude(status__in=("draft", "cancelled"))
        .select_related("partner")
        .order_by("-start", "-id")
    )
    found: dict[str, PCA] = {}
    for code, parts in wanted.items():
        exact = [pd for pd in candidates if _segments(pd.number) == parts]
        if exact:
            found[code] = exact[0]
            continue
        running = [
            pd
            for pd in candidates
            if parts[-1] in _segments(pd.number)
            and (pd.start is None or pd.start <= last)
            and (pd.end is None or pd.end >= first)
        ]
        if len(running) == 1:  # several PDs under one agreement: no guess
            found[code] = running[0]
    return found


def words(text: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) >= 3} - STOP_WORDS


def score(youth_name: str, youth_target: float | None, indicator: monitoring.Indicator) -> float:
    mine = words(youth_name)
    if not mine:
        return 0.0
    theirs = words(indicator.title) | words(indicator.output)
    value = len(mine & theirs) / len(mine)
    if youth_target and indicator.target and abs(float(youth_target) - float(indicator.target)) < 0.5:
        value += TARGET_BONUS
    return round(min(value, 1.0), 3)


def youth_indicators_by_pd(figures: Figures) -> dict[int, dict[tuple[str, int], float | None]]:
    """For each Compiler programme document, the youth indicators it reports on (the sub indicator,
    or the master when no sub is set) and their target there."""
    out: dict[int, dict[tuple[str, int], float | None]] = defaultdict(dict)
    for t in figures.targets:
        if t["program_document"] is None:
            continue
        ident = ("sub", t["sub"]) if t["sub"] else ("master", t["master"]) if t["master"] else None
        if ident:
            previous = out[t["program_document"]].get(ident)
            out[t["program_document"]][ident] = (previous or 0) + (t["target"] or 0) or None
    for (sub, pd_id), _count in (figures.table({}, "sub", "program_document") or {}).items():
        if pd_id is not None and sub is not None:
            out[pd_id].setdefault(("sub", sub), None)
    return out


def etools_indicators(pd_ids: list[int], year: int) -> dict[int, list[monitoring.Indicator]]:
    if not pd_ids:
        return {}
    rows = monitoring.indicators(monitoring.Filters(pd_ids=pd_ids, scope="all", year=year))
    out: dict[int, list[monitoring.Indicator]] = defaultdict(list)
    for row in rows:
        out[row.pd.id].append(row)
    return out


def suggest(figures: Figures) -> dict[str, int]:
    """Replace the year's suggestions; confirmed links stay as they are. Returns counts."""
    year = figures.year
    documents = figures.program_documents
    wanted = youth_indicators_by_pd(figures)
    codes = {documents[pd_id]["project_code"] for pd_id in wanted if pd_id in documents}
    pds = match_pds({c for c in codes if c}, year_number(year))
    by_pd = etools_indicators(sorted({pd.id for pd in pds.values()}), year_number(year))
    confirmed = set(
        YouthIndicatorLink.objects.filter(year=year, source=YouthIndicatorLink.Source.CONFIRMED).values_list(
            "level", "youth_indicator_id", "compiler_pd_id"
        )
    )
    links = []
    for pd_id, indicators in wanted.items():
        document = documents.get(pd_id)
        pd = pds.get(document["project_code"]) if document else None
        if pd is None:
            continue
        pairs = []
        for (level, youth_id), target in indicators.items():
            item = figures.indicators.get((level, youth_id))
            if item is None or (level, youth_id, pd_id) in confirmed:
                continue
            for candidate in by_pd.get(pd.id, []):
                value = score(item["name"], target, candidate)
                if value >= MIN_SCORE:
                    pairs.append((value, (level, youth_id), item, candidate))
        # best pairs first; each youth indicator and each eTools indicator is used once per PD
        taken_youth, taken_etools = set(), set()
        for value, ident, item, best in sorted(pairs, key=lambda p: (-p[0], p[3].title, p[1])):
            if ident in taken_youth or best.key in taken_etools:
                continue
            taken_youth.add(ident)
            taken_etools.add(best.key)
            links.append(
                YouthIndicatorLink(
                    year=year,
                    level=ident[0],
                    youth_indicator_id=ident[1],
                    youth_indicator=indicator_label(item)[:300],
                    compiler_pd_id=pd_id,
                    compiler_pd_code=(document["project_code"] or "")[:64],
                    pd=pd,
                    etools_key=best.key,
                    etools_title=best.title[:512],
                    score=value,
                )
            )
    with transaction.atomic():
        removed, _ = YouthIndicatorLink.objects.filter(
            year=year, source=YouthIndicatorLink.Source.SUGGESTED
        ).delete()
        YouthIndicatorLink.objects.bulk_create(links, ignore_conflicts=True)
    return {"matched_pds": len(pds), "suggested": len(links), "confirmed": len(confirmed)}


def link_rows(year: str) -> list[YouthIndicatorLink]:
    return list(YouthIndicatorLink.objects.filter(year=year).select_related("pd"))


def describe(links: list[YouthIndicatorLink], year: str) -> dict[tuple[int, str], Any]:
    """The eTools indicator of each link, with what the partner reported: (pd id, key) -> Indicator."""
    ids = sorted({link.pd_id for link in links if link.pd_id})
    return {(i.pd.id, i.key): i for rows in etools_indicators(ids, year_number(year)).values() for i in rows}
