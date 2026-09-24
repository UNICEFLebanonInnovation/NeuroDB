"""Tags read from an indicator's title (gender, age group, nationality, disability).

eTools/PRP carry no such fields; as with the ActivityInfo indicators (v2 ``set_tags``), the title
says whom the indicator counts ("# of girls (12-17) with disabilities reached ..."). The rules are
ordered: the first pattern that matches wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GENDER = (
    ("Girls", r"\bgirls?\b"),
    ("Boys", r"\bboys?\b"),
    ("Women", r"\bwomen\b|\bwoman\b|\bmothers?\b|\bpregnant\b|\blactating\b"),
    ("Men", r"\bmen\b|\bman\b|\bfathers?\b"),
    ("Female", r"\bfemales?\b"),
    ("Male", r"\bmales?\b"),
)
AGE_GROUP = (
    (
        "Under 5",
        r"\bunder[- ]?5\b|\b<\s?5\b|\b0\s?[-–]\s?5\b|\b0\s?[-–]\s?59\s?months?\b|\binfants?\b|\bnewborns?\b",
    ),
    ("Under 18", r"\bunder[- ]?18\b|\b<\s?18\b|\b0\s?[-–]\s?18\b|\b0\s?[-–]\s?17\b"),
    ("Adolescents", r"\badolescents?\b|\b1[0-4]\s?[-–]\s?1[7-9]\b"),
    ("Youth", r"\byouth\b|\byoung (?:people|men|women)\b|\b1[5-8]\s?[-–]\s?(?:2[4-9]|3[0-5])\b"),
    ("Children", r"\bchild(?:ren)?\b|\bgirls?\b|\bboys?\b|\bstudents?\b|\blearners?\b|\bpupils?\b"),
    ("Caregivers", r"\bcaregivers?\b|\bparents?\b|\bmothers?\b|\bfathers?\b"),
    ("Adults", r"\badults?\b|\bwomen\b|\bmen\b|\bteachers?\b|\bstaff\b|\bfrontline workers?\b"),
)
NATIONALITY = (
    ("Syrian", r"\bsyrians?\b|\bsyr\b"),
    ("Palestinian", r"\bpalestinians?\b|\bprs\b|\bprl\b"),
    ("Lebanese", r"\blebanese\b|\bleb\b"),
    ("Migrants", r"\bmigrants?\b"),
    ("Refugees", r"\brefugees?\b|\bdisplaced\b|\bidps?\b"),
)
DISABILITY = r"\bdisabilit(?:y|ies)\b|\bpwds?\b|\bspecial needs\b|\bdisabled\b"

TAG_FIELDS = ("gender", "age_group", "nationality", "disability")


@dataclass(frozen=True)
class Tags:
    gender: str = ""
    age_group: str = ""
    nationality: str = ""
    disability: str = ""  # "Yes" when the title names persons with disabilities

    def as_dict(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in TAG_FIELDS}


def _first(rules: tuple[tuple[str, str], ...], text: str) -> str:
    return next((label for label, pattern in rules if re.search(pattern, text)), "")


def tags_of(title: str | None) -> Tags:
    text = (title or "").lower()
    if not text:
        return Tags()
    return Tags(
        gender=_first(GENDER, text),
        age_group=_first(AGE_GROUP, text),
        nationality=_first(NATIONALITY, text),
        disability="Yes" if re.search(DISABILITY, text) else "",
    )
