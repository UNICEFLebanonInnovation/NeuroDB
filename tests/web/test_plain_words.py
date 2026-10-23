"""The words of Raw eTools data, the check line and the Power BI comparison sheet (SPEC_validation §0.1): the
reviewers and the PD managers are not developers, so the new pages never say a record's "key", "held", "announced",
"census", "duplicates"... nor a word of the code."""

from __future__ import annotations

import re
from pathlib import Path

from neurodb.datamart import comparison, raw, runfacts

ROOT = Path(__file__).resolve().parents[2] / "neurodb"
TEMPLATES = [
    *sorted((ROOT / "datamart/templates/admin/datamart/raw").glob("*.html")),
    ROOT / "reports/templates/reports/partials/_check_link.html",
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
    }
    sentences.append(runfacts._verdict(facts)[1])
    sentences.append(
        runfacts._verdict({**facts, "never_came": 0, "not_written": 3, "written_into": "locations"})[1]
    )
    for sentence in sentences:
        assert not NEVER.findall(sentence), sentence
