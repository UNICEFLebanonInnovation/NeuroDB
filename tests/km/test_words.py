"""The KM Database's plain words (SPEC_km §0.1): its sentences and templates say none of the words of the code,
and the administrator's messages take the workbook's column names from km.columns, never write one."""

from __future__ import annotations

import re
from pathlib import Path

from django.utils.functional import Promise

from neurodb.km import columns as cols
from neurodb.km import words
from tests.web.test_plain_words import NEVER, _visible

TEMPLATES = sorted((Path(words.__file__).parent / "templates" / "km").rglob("*.html"))
CODE_WORDS = re.compile(
    r"\b(?:agent|item|record|excerpt|prompt|token|vector|chunk|garbage|invalid|LLM|SEM)s?\b", re.IGNORECASE
)


def _forms(value) -> list[str]:
    """A sentence, or both forms of a sentence with a count (``ngettext_lazy``: its count picks the form)."""
    if isinstance(value, str):
        return [value]
    one, other = str(value % {"n": 1}), str(value % {"n": 2})
    return [one] if one == other else [one, other]


def _sentences() -> list[str]:
    found = []
    for value in vars(words).values():
        if isinstance(value, Promise | str) and not callable(value):
            found += _forms(value)
        elif isinstance(value, tuple):
            found += [str(v) for v in value]
    return found


def test_a_sentence_with_a_count_has_a_form_for_one():
    assert _forms(words.ROWS_WITHOUT) == [
        "{count} row of “{sheet}” has no “{column}”: row {rows}.",
        "{count} rows of “{sheet}” have no “{column}”, for example rows {rows}.",
    ]
    assert words.counted(1, words.FINDINGS_N) == "1 finding"
    assert words.counted(14_869, words.FINDINGS_N) == "14,869 findings"
    assert words.counted(0, words.DOCUMENTS_N) == "0 documents"
    plural = [
        name for name, value in vars(words).items() if isinstance(value, Promise) and len(_forms(value)) == 2
    ]
    assert len(plural) >= 14, plural


def test_the_sentences_and_templates_say_none_of_the_words_of_the_code():
    sentences = _sentences()
    assert len(sentences) > 60
    for sentence in sentences:
        assert not CODE_WORDS.search(sentence), sentence
        assert not NEVER.search(sentence), sentence
    assert TEMPLATES
    for path in TEMPLATES:
        visible = _visible(path.read_text(encoding="utf-8"))
        assert not CODE_WORDS.search(visible), (path.name, CODE_WORDS.findall(visible))
        assert not NEVER.search(visible), (path.name, NEVER.findall(visible))


def test_no_sentence_holds_a_column_of_the_workbook():
    headers = {c.header for c in (*cols.DOCUMENT_COLUMNS, *cols.FINDING_COLUMNS)}
    for sentence in _sentences():
        for header in headers:
            # a few headers are plain words too ("Pillar", "Measure", "Population"...): only those of two words
            if " " in header:
                assert header not in sentence, (header, sentence)
