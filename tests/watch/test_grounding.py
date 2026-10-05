"""The grounding check's ``named_fields`` (stage 6a of Monitoring insights): which fields of the cited
facts may hold a system word ("items") that a sentence repeats. NeuroDB Watch's default is unchanged;
Monitoring insights also lets a narrative's text, a partner and a place name hold one."""

import inspect

from neurodb.fmm.ai import FMM_NAMED_FIELDS
from neurodb.watch import grounding

FACT = {"key": "narr:1722:1", "text": "The partner handed out non-food items in both centres.", "visits": 3}
NAMES = frozenset()


def test_the_default_is_watchs_own_list():
    assert grounding.NAMED_FIELDS == (
        "title",
        "name",
        "check",
        "about",
        "connected",
        "sections",
        "section",
        "audience",
    )
    for function in (grounding.check, grounding.validate, grounding._names_of):
        assert inspect.signature(function).parameters["named_fields"].default == grounding.NAMED_FIELDS
    assert set(grounding.NAMED_FIELDS) < set(FMM_NAMED_FIELDS)
    assert {"text", "label", "partner", "place", "pd", "issue", "cp_outputs", "programme_activities"} <= set(
        FMM_NAMED_FIELDS
    )


def test_a_system_word_in_a_cited_text_is_kept_only_with_fmm_named_fields():
    sentence = "A visit noted that non-food items were handed out."
    verdict = grounding.check(sentence, [FACT], names=NAMES)
    assert not verdict and verdict.reason == grounding.WORDING
    assert grounding.check(sentence, [FACT], names=NAMES, named_fields=FMM_NAMED_FIELDS)
    # a word the facts do not hold is still dropped
    assert grounding.check(
        "The agent noted it.", [FACT], names=NAMES, named_fields=FMM_NAMED_FIELDS
    ).reason == (grounding.WORDING)


def test_validate_passes_named_fields_to_every_sentence():
    raw = [{"text": "A visit noted that non-food items were handed out.", "keys": ["narr:1722:1"]}]
    citable = {"narr:1722:1": FACT}
    kept, dropped = grounding.validate(raw, citable, names=NAMES)
    assert kept == [] and dropped == [grounding.WORDING]
    kept, dropped = grounding.validate(raw, citable, names=NAMES, named_fields=iter(FMM_NAMED_FIELDS))
    assert [k["keys"] for k in kept] == [["narr:1722:1"]] and dropped == []
