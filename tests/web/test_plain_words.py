"""The words of Raw eTools data, the check line, the figure trail and the Power BI comparison sheet
(SPEC_validation §0.1): the reviewers and the PD managers are not developers, so the new pages never say a
record's "key", "held", "announced", "census", "duplicates"... nor a word of the code."""

from __future__ import annotations

import re
from pathlib import Path

from neurodb.core.models import SyncRun
from neurodb.datamart import (
    backfill,
    checks,
    comparison,
    identity,
    owed,
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
    ROOT / "reports/templates/reports/partials/_owed_line.html",  # PRP's own reports still owed
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


# the verdicts the sync records on PRP's progress reports (datamart_sync.recorded_pd_link), one per reason
PRP_VERDICTS = [
    {
        "shown": why == "",
        "why": why,
        "since": "2026-10-14",
        "passes": why in ("", "one_day"),
        "proven": why != "not_proven",
        "link_why": "not_clear" if why == "not_proven" else "",
        "linked": 14,
        "agrees": {"compared": 12, "same": 11, "elsewhere": 1, "dated": 11, "same_day": 10},
        "owed": 3,
        "stale": 2,
        "checked_on": "2026-10-14",
        "read_on": "2026-10-06",
    }
    for why in ("", *datamart_sync.SHOWN_WHY)
] + [{"shown": False, "why": "not_read"}]


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
        raw.FR_OTHER_PARTNER,  # and when, with no number, its eTools id names another partner's PD
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
                "by_id_no_vendor": 1,
                "by_id_other_partner": 2,
                "by_id_vendor_unknown": 1,
                "id_other": 4,
                "moved": 4,
                **{f"vendor_compared_{kind}": 2 for kind in raw.FR_VENDOR_WORDS},
                **{f"vendor_same_{kind}": 1 for kind in raw.FR_VENDOR_WORDS},
            },
            "funds_reservations",
        ),
        raw.fr_link_words({"by_number": 1, "exact": 1}, "funds_reservation_headers"),
        raw.fr_link_words(
            {"by_id": 3, "by_id_other_partner": 2, "by_id_no_vendor": 2, "by_id_vendor_unknown": 2},
            "funds_reservation_headers",
        ),
        raw.Filters(fr_link="number_not_held").words(),
        raw.Filters(fr_link="by_id_other_partner").words(),
        # PRP's progress reports: tied to their programme documents? may the pages show their reports still owed?
        *raw.PRP_LINK_WHY.values(),
        *datamart_sync.SHOWN_WHY.values(),
        *(raw.prp_link_words(verdict) for verdict in PRP_VERDICTS),
        *(checks._shown_sentence(verdict) for verdict in PRP_VERDICTS),
        *(label for label, _ in checks._proof_figures(PRP_VERDICTS[0])),
        *(f"reports still owed shown on the pages: {datamart_sync.shown_words(v)}" for v in PRP_VERDICTS),
        raw.Filters(updated="unsent_word").words(),
        # PRP's own reports still owed on the pages: the line while hidden (by why) and while shown, the trail's
        *owed.HIDDEN.values(),
        owed.SHOWN,
        owed.SHOWN_ONE,
        owed.SOURCE_WORDS,
        owed.ROWS_WORDS,
        powerbi_words.NOTE_FROM_PRP,
        # the line on the trail page while they are hidden, and the CSV's step
        "Not counted: {line} See eTools reporting checks, check 11. The figures count the partner reports only.",
        "PRP's own reports still owed (a second source)",
        "Of PRP's own reports still owed: 2 progress reports left out.",
        # the PD indicators eTools marks inactive in every row, and the budget records of a sync
        raw.inactive_words({"indicators": 280, "without_rows": 182, "pds": 40}),
        raw.inactive_words({"indicators": 1, "without_rows": 1, "pds": 1}),
        raw.Filters(active="no").words(),
        raw.budget_link_words({"records": 120, "number_other": 7, "several": 3}),
        raw.budget_link_words({"records": 1, "number_other": 1, "several": 1}),
        # the budget records of a sync by the number they carry
        raw.budget_link_words(
            {
                "records": 120,
                "by_number": 100,
                "by_id": 3,
                "number_not_held": 9,
                "number_not_held_amended": 2,
                "id_not_held": 1,
                "names_none": 1,
                "set_aside": 6,
                "several": 5,
                "twice": 1,
                "number_other": 70,
                "source_id_compared": 100,
                "source_id_same": 30,
                "intervention_id_compared": 100,
                "intervention_id_same": 99,
                "cleared": 40,
            }
        ),
        raw.budget_link_words({"records": 1, "by_number": 1, "number_other": 0, "several": 0, "cleared": 1}),
        raw.budget_link_words(
            {"records": 1, "by_number": 1, "number_other": 0, "several": 0, "clear_failed": 2}
        ),
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
        comparison.NO_REACHED_NOTE,
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


def test_every_rule_a_comparison_line_names_is_said_in_words():
    """A line of Compare with Power BI names its rule in words (``powerbi_words.RULE_WORDS``), never its code:
    every rule the comparison writes. A comparison made before the user's decision of 26 Oct 2026 wrote no line
    of the rules decided then (it stated them in notes, which its pages now say in the decision's words): those
    words, and the others of that decision, say none of the code's words either."""
    from neurodb.datamart import powerbi_results

    source = (ROOT / "datamart/powerbi_kinds.py").read_text(encoding="utf-8")
    written = set(re.findall(r'\("rule", "([a-z_]+)"', source))
    assert len(written) > 15
    named = written | set(powerbi_results.RULE_CHOICES)
    assert not named - set(powerbi_words.RULE_WORDS), named - set(powerbi_words.RULE_WORDS)
    for sentence in (
        *powerbi_words.NOTES_DECIDED.values(),
        powerbi_words.CHOICES_DECIDED,
        powerbi_words.COUNTED_DECIDED,
    ):
        assert not NEVER.findall(sentence), sentence
        assert "_" not in sentence, sentence
