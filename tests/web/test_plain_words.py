"""The words of Raw eTools data, the check line, the figure trail and the Power BI comparison sheet
(SPEC_validation §0.1): the reviewers and the PD managers are not developers, so the new pages never say a
record's "key", "held", "announced", "census", "duplicates"... nor a word of the code."""

from __future__ import annotations

import re
from pathlib import Path

from neurodb.core.models import SyncRun
from neurodb.datamart import (
    backfill,
    comparison,
    identity,
    powerbi_words,
    raw,
    runfacts,
    trail,
    trail_monitoring,
)
from neurodb.integrations import background
from neurodb.integrations.etools import datamart_sync, rest_raw

ROOT = Path(__file__).resolve().parents[2] / "neurodb"
TEMPLATES = [
    *sorted((ROOT / "datamart/templates/admin/datamart/raw").glob("*.html")),
    # Compare with Power BI (its sentences: datamart.powerbi_words, below)
    *sorted((ROOT / "datamart/templates/admin/datamart/powerbi").glob("*.html")),
    ROOT / "reports/templates/reports/partials/_check_link.html",
    # the figure trail (its sentences are tested on a trail: tests/reports/test_figure_trail.py)
    ROOT / "reports/templates/reports/check_figures.html",
    ROOT / "reports/templates/reports/parts/check_figures.html",
    ROOT / "reports/templates/reports/parts/check_table.html",
    ROOT / "reports/templates/reports/partials/_trail_block.html",
    # Partner monitoring's (its sentences: tests/reports/test_figure_trail_monitoring.py)
    ROOT / "reports/templates/reports/parts/check_monitoring.html",
    ROOT / "reports/templates/reports/parts/check_monitoring_table.html",
    ROOT / "reports/templates/reports/partials/_trail_stages.html",
]
NEVER = re.compile(
    r"\bkeys?\b|\bcensus\b|\bannounced\b|\bheld\b|removal skipped|record key|came twice|\bduplicates?\b"
    r"|failed rows|\bstage\b|\bhooks?\b|\brecorder\b|\bfingerprint\b|shared_output|two_for_one",
    re.IGNORECASE,
)
TAGS = re.compile(
    r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}|<[^>]+>", re.S
)


def _visible(text: str) -> str:
    """A template's words as a person reads them: no comment, tag, variable or HTML element."""
    return TAGS.sub(" ", text)


def _powerbi_sentences() -> list[str]:
    """Every sentence of Compare with Power BI (datamart.powerbi_words): its strings, lists and tables."""
    found = []
    for name, value in vars(powerbi_words).items():
        if name.startswith("_") or not name.isupper():
            continue
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, tuple | list):
            found += [v for v in value if isinstance(v, str)]
        elif isinstance(value, dict):
            found += [v for v in value.values() if isinstance(v, str)]
    assert len(found) > 80
    return found


def test_the_new_templates_say_none_of_the_words_of_the_code():
    assert TEMPLATES and all(path.exists() for path in TEMPLATES)
    for path in TEMPLATES:
        found = NEVER.findall(_visible(path.read_text(encoding="utf-8")))
        assert not found, (path.name, found)


def test_the_sentences_the_code_writes_on_these_pages_say_none_either():
    sentences = [
        *raw.USES.values(),
        *raw.PD_LINK_WORDS.values(),
        *raw.UPDATED.values(),
        # the partner report rows eTools no longer sends: their choices, sentences, cell and the button's answers
        *raw.UNSENT.values(),
        raw.unsent_words(312, 14, None, None, 3),
        raw.unsent_words(1, 1, None, None, 0),
        raw.NOT_SENT_SINCE,  # the record page's "Updated by:" of a row eTools no longer sends
        raw.Filters(pd="LEB/PD1", updated="unsent").words(),
        # how the funds reservation lines and the funds reservations link to their programme document
        *raw.FR_LINK_WORDS.values(),
        raw.FR_SEVERAL,  # a line's PD in NeuroDB when its number fits several
        raw.fr_link_words(
            {
                "by_number": 9,
                "exact": 5,
                "amended": 1,
                "pd_part": 1,
                "header": 2,
                "twice": 1,
                "by_id": 3,
                "number_not_held": 2,
                "number_not_held_id_held": 1,
                "unclear": 1,
                "id_not_held": 1,
                "id_other": 4,
                "moved": 4,
                **{f"vendor_compared_{kind}": 2 for kind in raw.FR_VENDOR_WORDS},
                **{f"vendor_same_{kind}": 1 for kind in raw.FR_VENDOR_WORDS},
            },
            "funds_reservations",
        ),
        raw.fr_link_words({"by_number": 1, "exact": 1}, "funds_reservation_headers"),
        raw.Filters(fr_link="number_not_held").words(),
        backfill.UNSENT_BUSY,
        backfill.UNSENT_CHANGED,
        backfill.UNSENT_FAILED,
        raw.REFUSED,
        raw.BUSY,
        raw.TOO_SLOW,
        raw.too_many("partner_reports", 30_000),
        *raw.NAMES.values(),
        # the Power BI comparison sheet's own words
        comparison.REFUSED_DAY,
        comparison.OVERDUE_NOTE,
        comparison.PDS_NOTE,
        *(words for line in comparison.WHY_THEY_DIFFER for words in line),
        *(words for page in comparison.NEURODB_ONLY.values() for words in page.values()),
        *comparison.POWERBI_ITEMS.values(),
        *comparison.TAG_WORDS.values(),
        comparison.ONE_COUNTRY,
        comparison.CLEAR_IN_POWERBI,
        comparison.COMPARE_FOR_YOU,
        *comparison.POWERBI_ONLY_SLICERS,
        *(name for names in comparison.POWERBI_TO_CLEAR.values() for name in names),
        # Compare with Power BI's own words
        *_powerbi_sentences(),
        # the figure trail's own words
        *trail.NOTES,
        trail.DIFFERS,
        *trail.FILTER_LABELS.values(),
        # Partner monitoring's, and check 7's ways a row is placed or left out (the trail's Reached fold)
        *trail_monitoring.NOTES,
        *trail_monitoring.TEST_WORDS.values(),
        *trail_monitoring.OUTCOME_WORDS.values(),
        *(trail_monitoring._way_words(way) for way in (*identity.WAY_WORDS, *identity.BREAKERS)),
    ]
    facts = {
        "run": object(),
        "newer_failed": None,
        "never_came": 74,
        "not_saved": 0,
        "not_written": 0,
        "written_into": "",
        "error_lines": (),
        "unexplained": 0,
        "sent_twice": 0,
        "copies_proven": 0,
    }
    sentences.append(runfacts._verdict(facts)[1])
    sentences.append(
        runfacts._verdict({**facts, "never_came": 0, "not_written": 3, "written_into": "locations"})[1]
    )
    # records sent twice, not proven, as many as those that did not come
    sentences.append(runfacts._verdict({**facts, "sent_twice": 74})[1])
    # why a sync or a read failed, and what an eTools REST entity never read says (raw_admin's notes)
    sentences += [*runfacts.STATUS_WORDS.values(), rest_raw.NOT_READ, rest_raw.NEVER_READ, rest_raw.NO_FINAL]
    for error in (
        "HTTP 403 Forbidden",
        "ReadTimeout",
        "ConnectTimeout",
        "ConnectionError",
        "payload",
        background.CUT_OFF,
        f"IntegrationError: {datamart_sync.ENTER_CODE}",
        "x",
    ):
        sentences.append(runfacts.failure_words(SyncRun(job=SyncRun.Job.ETOOLS_REST_RAW, error=error)))
    sentences.append(runfacts.empty_words("rest_partners", SyncRun(job=SyncRun.Job.ETOOLS_REST_RAW)))
    sentences += [
        "being read now",
        "waiting: the read under way reaches it later",
        "stopped before it reached it",
    ]
    for sentence in sentences:
        assert not NEVER.findall(sentence), sentence
