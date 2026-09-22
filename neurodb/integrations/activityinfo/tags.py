"""Indicator tag derivation from the indicator name.

Ports ``pivoting/models.py`` (``age_groups``, ``nationalities``, ``map_tags`` and
``IndicatorNew.set_tags``) verbatim: the spelling tables and their order are the v2 ones,
because the first matching entry wins. Pure functions, no I/O.
"""

from __future__ import annotations

from typing import TypedDict

# (substrings, value) in v2 order. Matching is case-insensitive on both sides.
AGE_GROUPS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("<=18", "0-18", "<18 or below18"), "<=18"),
    (("1-18 years",), "1-18"),
    (("3-7 years", "3-7"), "3-7"),
    (("Under18", "Under 18", "Under-18", "Age<18", "U18", "<18"), "<18"),
    (("Under 5", "Under5", "<5"), "<5"),
    (("<60",), "<60"),
    (("12+", "+12"), ">12"),
    (("+14", "14+"), ">14"),
    ((">=18", "18+", "+18", ">18 or above18"), ">=18"),
    (("5 and above", "+5", "5+", ">=5"), ">=5"),
    (("60 and above", ">=60"), ">=60"),
    ((">14", "+14", "14+", "Above 14", "Above14"), ">14"),
    (("Above18", "Above 18", "Above-18", "Age>18", ">18"), ">18"),
    (("Above5", "Above 5", ">5"), ">5"),
    ((">60", "Above60", "Above 60"), ">60"),
    ((" 0-17 ", "_0-17_"), "0-17"),
    (("10-14",), "10-14"),
    (("12-17",), "12-17"),
    (("12-18",), "12-18"),
    (("14-17",), "14-17"),
    (("14-18",), "14-18"),
    (("15-17",), "15-17"),
    (("15-18",), "15-18"),
    (("15-19",), "15-19"),
    (("15-20",), "15-20"),
    (("18-24",), "18-24"),
    (("18-25",), "18-25"),
    (("18-59",), "18-59"),
    (("20-24",), "20-24"),
    (("25-49",), "25-49"),
    (("26-59",), "26-59"),
    ((" 3-5 ", "_3-5_", "(3-5)"), "3-5"),
    (("50-64",), "50-64"),
    (("5-17",), "5-17"),
    (("5-18",), "5-18"),
    (("6-10",), "6-10"),
    (("6-11",), "6-11"),
    (("6-13",), "6-13"),
    (("6-14",), "6-14"),
    (("6-15",), "6-15"),
    (("6-18",), "6-18"),
    (("0-59 months",), "0-59 months"),
    (("6-59 months",), "6-59 months"),
    (("0-23 months",), "0-23 months"),
    (("6-23 months",), "6-23 months"),
    (("6-9",), "6-9"),
    ((" 0-1 ", "_0-1_"), "0-1"),
    ((" 0-19 ", "_0-19_"), "<=19"),
    ((" 0-3 ", "_0-3_"), "0-3"),
    ((" 0-4 ", "_0-4_"), "0-4"),
    ((" 0-5 ", "_0-5_", "<=5"), "0-5"),
)

NATIONALITIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("non_leb", "non-leb", "non lebanese"), "NONLEB"),
    (("_syr",), "SYR"),
    (("_leb", "of lebanese"), "LEB"),
    (("_prs",), "PRS"),
    (("_prl",), "PRL"),
    (("_oth", "_mig"), "OTH"),
)

# (substring in the lower-cased name, value), first match wins - v2 IndicatorNew.set_tags.
DISABILITIES: tuple[tuple[str, str], ...] = (
    ("_speaking", "Speaking"),
    ("_intellectual", "Intellectual"),
    ("_audio", "Audio"),
    ("_visual", "Visual"),
    ("_motor", "Motor"),
    ("_mobility", "Motor"),
)

# Case-sensitive on the original name in v2.
PROGRAMMES: tuple[tuple[str, str], ...] = (("_BLN", "BLN"), ("_ALP", "ALP"), ("_CBECE", "CBECE"))


class Tags(TypedDict):
    gender: str | None
    nationality: str | None
    disability: str | None
    programme: str | None
    age_group: str | None


def map_tags(name: str, mappings: tuple[tuple[tuple[str, ...], str], ...]) -> str | None:
    """Return the value of the first mapping whose substring occurs in ``name`` (v2 ``map_tags``)."""
    haystack = name.lower().strip()
    for substrings, value in mappings:
        for substring in substrings:
            if substring.lower().strip() in haystack:
                return value
    return None


def _first_match(haystack: str, pairs: tuple[tuple[str, str], ...]) -> str | None:
    for needle, value in pairs:
        if needle in haystack:
            return value
    return None


def parse_gender(name: str) -> str | None:
    """``"Male"``/``"Female"`` exactly as v2 stored them (the model choices are lower-case)."""
    lcname = name.lower()
    if "_male" in lcname:
        return "Male"
    if "_female" in lcname:
        return "Female"
    return None


def parse_tags(name: str) -> Tags:
    """Derive the five indicator tags from an indicator name.

    ``None`` means "no spelling matched". Nationality and age group are re-derived on every call
    in v2 (a non-match cleared the stored value) whereas gender, programme and disability were only
    written on a match; ``structure.apply_tags`` reproduces that difference.
    """
    lcname = name.lower()
    return Tags(
        gender=parse_gender(name),
        nationality=map_tags(lcname, NATIONALITIES),
        disability=_first_match(lcname, DISABILITIES),
        programme=_first_match(name, PROGRAMMES),
        age_group=map_tags(lcname, AGE_GROUPS),
    )
