"""The people NeuroDB knows by name, so that NeuroDB Watch never sends them to the AI and never shows a
sentence of the AI that names one.

The morning note's input is an allow-list that never reads a person's field
(:mod:`neurodb.watch.redact`). A name can still slip into a text written from upstream data, and the AI
could write one, so the watch also keeps the list of the names it knows and looks for them in what goes
in and what comes out:

- names of **two words or more** (a first name alone is too common to tell from other words) from the
  fields that hold a person: action point assignees, TPM report authors, field monitoring visit leads,
  progress report submitters and staff travellers (eTools Datamart), PD focal points, partner staff;
- NeuroDB users' first and last names, and their full name;
- the owners typed on finding assignments (meant to be a role or a team, but a name may be typed);
- the names other apps register in :data:`EXTRA_SOURCES` (Monitoring insights: the members of the
  field monitoring teams);
- the email addresses found in those fields. Any email address is treated as a person's, listed or not.

A name is compared in lower case and without accents, word by word: "Jane Doe", "JANE DOE" and "Jane
Doé" are the same name; "Doe, Jane" is also looked for as "jane doe", a title (Ms, Dr) is dropped, and
a name of three words or more is also looked for as its first and last word. A name that is also a
section's name is not a person and is left out.

:func:`known_names` reads them once per run (kept for 10 minutes, :func:`forget` empties it);
:func:`mentions` finds them in a text and :func:`scrub` replaces them.
"""

from __future__ import annotations

import functools
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600  # a run reads the names once; a long-lived process reads them again after this
LONGEST = 6  # words of a name compared at most
NAME_WITHHELD = "[name withheld]"
EMAIL_WITHHELD = "[email withheld]"

# Characters a text may hold that nobody sees: taken out before anything is looked for (:func:`visible`), so
# that an address or a name with one inside ("layla.saab\u200b@example.org", "layla\u200f@example.org") is
# found as it reads. Every Unicode format character (the zero-width spaces and joiners, the soft hyphen, the
# byte order mark, the left-to-right and right-to-left marks and isolates that text copied from Arabic
# carries, the invisible operators, the tags), the combining grapheme joiner, the Mongolian vowel separator
# and free variation selectors, the variation selectors, the Khmer inherent vowels and the Hangul fillers
INVISIBLE = re.compile(
    "[\u00ad\u034f\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u115f\u1160\u17b4\u17b5"
    "\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\u3164\ufe00-\ufe0f\ufeff"
    "\uffa0\ufff9-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3"
    "\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f\U000e0100-\U000e01ef]"
)
_AT_SIGN = "[@＠﹫]"  # with the full-width and the small at sign
# An address, also written with spaces around "@" ("layla.saab @ unicef.org", then only before a
# domain that ends in letters: "2 trainers @ 50 USD" stays), with the sign in brackets ("x(@)y.org",
# "x [@] y.org") or with the full-width or the small sign
EMAIL = re.compile(
    rf"(?<![\w.+'-])[\w.+'-]+(?:{_AT_SIGN}|\s*(?:\[\s*{_AT_SIGN}\s*\]|\(\s*{_AT_SIGN}\s*\))\s*"
    rf"|\s*{_AT_SIGN}\s*(?=[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{{2,}}\b))[\w-]+(?:\.[\w-]+)+"
)
# An address written out to dodge a filter: "x.fake at example dot org", "x dot fake at example dot org",
# "x.fake[at]example[.]org", "x.fake{at}example.org", "x.fake<at>example.org", "x.fake -at- example -dot-
# org", "x.fake [ at ] example < dot > org", "x.fake at example.org", "zfake at example.fr", "zfake at
# example.co.uk", "x . fake at example . org", "x . fake @ example . org", "zfake @ example dot org", "zfake
# [@] example [dot] org", "zfake at example .org", "zfake arobase example.org", "zfake 'at' example.org",
# "zfake *at* example.org", "zfake at-sign example.org", "zfake at at example.org", "zfake at example period
# org", "zfake-at-example-dot-org". A plain full stop counts only before a domain's ending in lower case
# (``_ENDING``: never a word of a sentence such as "it" or "we"), so that a sentence ("located at Tyre. Net
# enrolment rose", "located at Tyre.Net", "at school.it was") is not taken; in any case after a name with a
# full stop inside, as an address has ("X.Fake at Example.Org", "X.FAKE AT EXAMPLE.ORG")
_AT = (
    r"(?:\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|<\s*at\s*>|-at-|['\"\u2018\u2019\u201c\u201d]at"
    r"['\"\u2018\u2019\u201c\u201d]|\*at\*|\(\s*a\s*\))\s*|\s+/at/\s*|\s+(?:at(?:\s+at)?|arobase|at-sign)\s+)"
)
# "at", or an at sign before a domain written out ("x @ example dot org", "x [@] example [dot] org")
_AT_ANY = rf"(?:{_AT}|\s*(?:{_AT_SIGN}|\[\s*{_AT_SIGN}\s*\]|\(\s*{_AT_SIGN}\s*\))\s*)"
_DOT = (
    r"(?:\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|<\s*dot\s*>|\[\.\]|\(\.\)|\{\.\}|<\s*\.\s*>"
    r"|['\"\u2018\u2019\u201c\u201d]dot['\"\u2018\u2019\u201c\u201d]|\*dot\*)\s*"
    r"|\s+/dot/\s*|\s+-dot-\s*|\s+dot\s+)"  # "-dot-" after a space: never inside a word that holds dashes
)
# in a domain only (never in a name, where "the period ends" is a sentence): "period" for the dot too
_DOMAIN_DOT = rf"(?:{_DOT}|\s+period\s+|\s*[\[(]\s*period\s*[\])]\s*)"
_ENDINGS = "org|com|net|int|gov|edu|lb|fr|uk|de|ch|ca|ae|sa|qa|jo|eg|nl|se|au|info|biz|ngo|io"
_SPELLED_ONLY = (
    "co|invalid|test|example"  # endings of an address written out only ("x-at-example-dot-invalid")
)
_ENDING = rf"(?:{_ENDINGS}|co\.[a-z]{{2}})(?:\.[a-z]{{2}})?"  # with a country's: "unicef.org.lb", "co.uk"
_SPELLED_ENDING = rf"(?:{_ENDINGS}|co)(?:{_DOT}[a-z]{{2}})?"  # "unicef dot org dot lb"
_DOMAIN = rf"[\w-]+(?:(?:{_DOMAIN_DOT}|\.)[\w-]+)*?"
_LOCAL = rf"[\w.+'-]+(?:{_DOT}[\w.+'-]+)*"  # "layla.saab", "layla dot saab"
_SPACED = r"[\w+'-]+(?:\s?\.\s?[\w+'-]+)*"  # "layla . saab"
# Spaced full stops: before "at", a space on both sides of each ("r . haddad at unicef . org"); and, as
# around "@", only the common endings, so that a sentence ("at Halba. info sessions", "at Halba. de
# facto", "meeting @ halba. info desk") is not taken
_SPACED_DOT = r"(?:\s\.\s|\.)"
_SPACED_ENDING = r"(?:org|com|net|int|gov|edu|lb|fr)"
SPELLED_EMAIL = re.compile(
    rf"\b{_LOCAL}{_AT_ANY}{_DOMAIN}{_DOMAIN_DOT}{_SPELLED_ENDING}\b"
    rf"|\b{_LOCAL}{_AT}{_DOMAIN}\s?\.(?-i:{_ENDING})\b"  # a space before the dot too ("x at example .org")
    rf"|\b[\w+'-]+(?:\.[\w+'-]+)+{_AT}{_DOMAIN}\.(?:{_ENDING})\b"
    rf"|\b[\w+'-]+(?:{_SPACED_DOT}[\w+'-]+)*{_AT}[\w-]+(?:{_SPACED_DOT}[\w-]+)*?{_SPACED_DOT}"
    rf"(?-i:{_SPACED_ENDING})\b"
    rf"|\b{_SPACED}\s?{_AT_SIGN}\s?[\w-]+(?:\s?\.\s?[\w-]+)*\s?\.\s?(?-i:{_SPACED_ENDING})\b"
    # joined by dashes or by "_": "x-at-example-dot-org", "x_at_example_dot_org"
    rf"|(?<![\w.+'-])[\w.+']+(?:-[\w.+']+)*?-at-\w+(?:-dot-\w+)*-dot-(?:{_ENDINGS}|{_SPELLED_ONLY})\b"
    rf"|(?<![\w.+'-])[^\W_][\w.+']*?_at_[^\W_]+(?:_dot_[^\W_]+)*_dot_(?:{_ENDINGS}|{_SPELLED_ONLY})(?![^\W_])",
    re.IGNORECASE,
)
# Phone numbers written inside a text: Lebanese numbers with or without +961/00961/961/(961) and the
# leading 0 ("03-123456", "+961 3 123 456", "961-3-123456", "01/345678", "(01) 345678", "03 - 123456",
# "03 – 123456", "03 12 34 56", "71 12 34 56", "961 3 12 34 56"), with an extension ("03 123456/7"),
# right after a word and its full stop ("Tel.01-345678", "Mob.03-123456"), several joined by a slash or
# a dash ("03123456/71123456", "03-123456/71-123456"), in Arabic-Indic digits too ("٠٣١٢٣٤٥٦"), Syrian
# mobile numbers written the national way ("0944 123 456", "0933-123-456": 093 to 099, so that a
# reference padded with zeros, "0900012345", stays) and any international one. One definition for every
# reader: Ask NeuroDB, NeuroDB Watch, Monitoring insights.
_DASHES = "\\-\u2013\u2014"  # a dash, an en dash or an em dash (as a word processor writes it)
_SEP = rf"[\s./{_DASHES}]?"  # between groups of digits: a space, a full stop, a slash or a dash
_FIRST_SEP = rf"(?:\s?[./{_DASHES}]\s?|\s)?"  # after the prefix, also with spaces around it ("03 - 123456")
_961 = r"\(?(?:(?:\+|00)\s?)?961\)?"
_MOBILE = r"(?:7[016-9]|81)"
# Not inside a word or a number, but right after a word's full stop ("Tel.01-345678")
_START = rf"(?:(?<![\w/.{_DASHES}])|(?<=[^\W\d_]\.))"
_END = rf"(?:[/{_DASHES}]\d{{1,2}})?(?![\w/{_DASHES}]|\.\d)"  # an extension, then no more digits
_JOIN = rf"\s?[/{_DASHES}]\s?"  # between two numbers written together ("03123456/71123456")
# Pairs of digits, with one kind of separator throughout ("71 12 34 56", "03.12.34.56"; a time range such
# as "08.30-10.30" is not a phone)
_PAIRS = (
    rf"(?:{_961}{_FIRST_SEP}(?:0?\d|{_MOBILE})|\(?0\d\)?|{_MOBILE})"
    rf"(?:(?:\s\d\d){{3}}|(?:\.\d\d){{3}}|(?:[{_DASHES}]\d\d){{3}})"
)
_SYRIAN = rf"\(?09[3-9]\d\)?{_FIRST_SEP}\d{{3}}{_SEP}\d{{3}}"
_ANY = (
    rf"(?:(?:{_961}{_FIRST_SEP})?\(?0?(?:{_MOBILE}|[1-9])\)?{_FIRST_SEP}\d{{3}}{_SEP}\d{{3}}"
    rf"|{_PAIRS}|{_SYRIAN})"
)
# A mobile prefix and six decimals is a figure ("76.923077%", "score 70.123456"), not a phone
_DECIMAL = rf"\.\d{{6}}(?![\s./{_DASHES}]?\d)"
_PREFIXED = (
    rf"(?:(?:{_961}{_FIRST_SEP}\(?0?(?:{_MOBILE}|[1-9])|\(?0(?:{_MOBILE}|[1-9])|{_MOBILE}(?!{_DECIMAL}))\)?"
    rf"{_FIRST_SEP}\d{{3}}{_SEP}\d{{3}}|{_PAIRS}|{_SYRIAN})"
)
# Arabic-Indic and Persian digits are read as 0-9: one character for one, so that a match is in the same
# place in the text as written
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)


class AnyDigits:
    """A compiled pattern whose ``search``, ``finditer`` and ``sub`` read every kind of digit as 0-9."""

    def __init__(self, pattern: str) -> None:
        self.regex = re.compile(pattern)
        self.pattern = pattern

    def search(self, text: str) -> re.Match | None:
        return self.regex.search(text if text.isascii() else text.translate(DIGITS))

    def finditer(self, text: str) -> Iterable[re.Match]:
        return self.regex.finditer(text if text.isascii() else text.translate(DIGITS))

    def sub(self, replace: str | Callable[[re.Match], str], text: str) -> str:
        if text.isascii():
            return self.regex.sub(replace, text)
        out, last = [], 0
        for match in self.regex.finditer(text.translate(DIGITS)):
            out += [text[last : match.start()], replace(match) if callable(replace) else replace]
            last = match.end()
        return "".join([*out, text[last:]])


# Every Lebanese number, prefixed or not (field monitoring texts: any 7 or 8 digits in groups)
PHONE = AnyDigits(rf"{_START}{_ANY}(?:{_JOIN}{_ANY})*{_END}")
# The numbers with a phone's prefix only: 961, the leading 0, or a mobile prefix (70, 71, 76, 78, 79,
# 81). Texts of the other eTools datasets, where "USD 1250000" or "1 500 000" is an amount, not a phone;
# a number with a phone's prefix is never taken for an amount (see query._phones)
PREFIXED_PHONE = AnyDigits(rf"{_START}{_PREFIXED}(?:{_JOIN}{_PREFIXED})*{_END}")
# An international number: "+" or "00" (then a space, or a space after the country code: "00 44 20...",
# "0044 20 ...", never a reference such as "PR 0010123456", nor the decimals of an amount: "1500.00
# 1200.00"), and nine digits or more ("+1 250 000" and "+2023-2024" are an amount and a period)
INTL_PHONE = AnyDigits(
    rf"(?<![\w/])(?:\+\s?|(?<![.,])00(?=[\s.{_DASHES}]|\d{{1,3}}[\s().{_DASHES}])[\s.{_DASHES}]?)"
    rf"(?=(?:[\s().{_DASHES}]*\d){{9}})\(?[1-9][\d\s().{_DASHES}]*\d(?![\w/])"
)
PHONE_WITHHELD = "[phone withheld]"
# A name written after a title ("Mrs Layla Saab", "Dr. Haddad", "Dr.Haddad", "Mme Saab", "Dr. Karim el
# Khoury", "Hajj Omar", "Mr Élie Khoury", "Doctor Rami Haddad"), known to NeuroDB or not: replaced by
# NAME_WITHHELD. Up to four capitalised words (any capital: "Élise", "Ömer"), with the particles of
# Lebanese surnames (el, bou, abi...). The upper-case titles need their full stop, so that "MS Teams",
# "DR Congo" and "HACT MR" stay; "M." is not a title in a degree ("M.Sc", "M. Ed"), a time ("9 A.M.
# Teachers", "2 P.M. The") or a figure ("USD 2.5 M. The", "3 M. Beirut"). The titles that are also words
# (``_WORD_TITLES``: "No Child Will Miss Out", "Eng Language Classes", "Prof Development", "Hajj Season",
# "Doctor Visits") take a name without their full stop only before a word that is not a common word of a
# sentence or of a programme's title (``COMMON_WORDS``). Sheikh, Dr and Doctor also start the names of
# places and organisations ("Sheikh Taba", "Dr. Mohamad Khaled Social Foundation"): PERSON_TITLE_NAME
# leaves them, for the keys that hold a place or an organisation.
COMMON_WORDS = frozenset(
    {"a", "an", "the", "and", "or", "but", "nor", "of", "in", "on", "at", "to", "for", "from", "with"}
    | {"without", "by", "as", "into", "onto", "out", "up", "down", "over", "under", "off", "about"}
    | {"after", "before", "during", "between", "through", "across", "against", "per", "via", "no", "not"}
    | {"any", "all", "some", "each", "every", "many", "more", "most", "few", "this", "that", "these"}
    | {"those", "their", "our", "your", "its", "it", "they", "we", "you", "is", "are", "was", "were", "be"}
    | {"been", "has", "have", "had", "do", "does", "did", "would", "can", "could", "should", "must"}
    | {"again", "due", "less", "several", "other", "others", "only", "also", "if", "when", "while", "than"}
    | {"school", "schools", "schooling", "class", "classes", "classroom", "classrooms", "course", "courses"}
    | {"lesson", "lessons", "language", "languages", "english", "french", "arabic", "literacy", "numeracy"}
    | {"math", "maths", "mathematics", "science", "sciences", "subject", "subjects", "curriculum", "exam"}
    | {"exams", "examination", "examinations", "test", "tests", "grade", "grades", "level", "levels"}
    | {"degree", "degrees", "diploma", "certificate", "certification", "studies", "study", "student"}
    | {"students", "teacher", "teachers", "teaching", "training", "trainings", "trainer", "trainers"}
    | {"development", "skills", "skill", "practice", "practices", "standards", "standard", "ethics"}
    | {"conduct", "learning", "education", "educational", "child", "children", "youth", "adolescents"}
    | {"girls", "boys", "women", "men", "families", "parents", "staff", "workers", "team", "unit", "units"}
    | {"office", "offices", "centre", "centres", "center", "centers", "department", "faculty", "committee"}
    | {"council", "board", "club", "clubs", "network", "group", "groups", "association", "foundation"}
    | {"programme", "programmes", "program", "programs", "project", "projects", "plan", "plans"}
    | {"planning", "activity", "activities", "session", "sessions", "meeting", "meetings", "workshop"}
    | {"workshops", "visit", "visits", "visiting", "day", "days", "week", "weeks", "month", "months"}
    | {"year", "years", "season", "seasons", "period", "periods", "time", "times", "deadline", "deadlines"}
    | {"opportunity", "opportunities", "appointment", "appointments", "payment", "payments", "fees", "fee"}
    | {"costs", "cost", "services", "service", "support", "supervision", "inspection", "inspections"}
    | {"assessment", "assessments", "survey", "surveys", "report", "reports", "reporting", "review"}
    | {"reviews", "work", "works", "health", "healthcare", "care", "clinic", "clinics", "hospital"}
    | {"hospitals", "medical", "medicine", "nutrition", "vaccination", "vaccinations", "vaccine"}
    | {"vaccines", "dose", "doses", "treatment", "treatments", "consultation", "consultations", "shortage"}
    | {"shortages", "availability", "water", "hygiene", "sanitation", "protection", "response", "emergency"}
    | {"relief", "pilgrimage", "pilgrims", "travel", "trip", "trips", "holiday", "holidays", "break"}
    | {"vacation", "visa", "visas", "kit", "kits", "package", "packages", "supplies", "materials"}
    | {"equipment", "engineering", "design", "designs", "construction", "rehabilitation", "repairs"}
    | {"maintenance"}
)
_COMMON = "|".join(sorted(COMMON_WORDS, key=len, reverse=True))
_NOT_COMMON = rf"(?!(?i:{_COMMON})\b)"
_DEGREES = r"Sc|Sci|Ed|A|Eng|Phil|Arch|Com|Tech|Pharm|Res|Litt|Mus|Div"
_M = rf"(?<![.\d]\s)(?<!\.)M\.(?!\s*(?:{_DEGREES})\b)"
_CLEAR_TITLES = (
    r"Mrs|Mr|Ms|Mx|Madam|Mme|Mlle|Monsieur|Mister|Hajja|Hajjeh|Hajji|Ustaza|Ustaz|Ustadh|Sayyed|Sayyid"
    r"|Sayed"
)
_WORD_TITLES = r"Miss|Sir|Madame|Engineer|Eng|Professor|Prof|Hajj|Haj"
_PERSON_TITLES = (
    rf"(?:{_CLEAR_TITLES})(?:\.\s*|\s+)|(?:{_WORD_TITLES})(?:\.\s*|\s+{_NOT_COMMON})"
    rf"|(?:{_M}|MR\.|MRS\.|MS\.)\s*"
)
_PLACE_TITLES = rf"(?:Sheikh|Dr)(?:\.\s*|\s+)|Doctor(?:\.\s*|\s+{_NOT_COMMON})|DR\.\s*"
# Never a name: these words, and a title, which starts a name of its own ("Mrs Layla Saab Mrs Hiba
# Karam" is two names, never one that leaves the second's out)
_NOT_NAMES = (
    r"(?!(?:Teams|Forms|Excel|Word|Office|Outlook|Access|Project|Congo|Programmatic)\b)"
    rf"(?!(?:{_CLEAR_TITLES}|{_WORD_TITLES}|Sheikh|Dr|Doctor|MR|MRS|MS|DR)\b)"
)


def _capitals() -> str:
    """The capital letters of the scripts that have them (A-Z, "É", "Ö", "Ş", Greek, Cyrillic...), as the
    ranges of a character class."""
    found = [c for c in range(0x41, 0x2000) if unicodedata.category(chr(c)) == "Lu"]
    ranges: list[list[int]] = []
    for code in found:
        if ranges and code == ranges[-1][1] + 1:
            ranges[-1][1] = code
        else:
            ranges.append([code, code])
    return "".join(
        re.escape(chr(a)) if a == b else f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b in ranges
    )


_CAPITAL = f"[{_capitals()}]"
_NAME_WORD = rf"{_NOT_NAMES}(?:(?:el|al|bou|abou|abu|abi|ben|bin|de|di)[\s-])?{_CAPITAL}[\w'-]+"
_TITLED = rf"{_NAME_WORD}(?:[\s-]+{_NAME_WORD}){{0,3}}"
HONORIFIC_NAME = re.compile(rf"\b(?:{_PERSON_TITLES}|{_PLACE_TITLES}){_TITLED}")
PERSON_TITLE_NAME = re.compile(rf"\b(?:{_PERSON_TITLES}){_TITLED}")
# The words those titles are written with, in lower case ("mrs", "dr", the "m" of "M."): a text that
# holds none of them holds no name written after a Latin title
TITLE_WORDS = frozenset(
    word.casefold() for word in re.findall(r"[A-Za-z]+", f"{_CLEAR_TITLES}|{_WORD_TITLES}|Sheikh|Dr|Doctor|M")
)
# The same in Arabic: Mr, Mrs, Miss, Madam, Ustaz, Hajj, Eng (and "د.", "م." and "أ.", not after a
# number: "5 م."), and the Sheikh and Dr that also start a place's name; with "ال" or "لل" ("to Mr":
# "للسيد"), and "و" or "ب" before them ("والسيد"), with ة or ه at the end of the feminine ("السيدة",
# "السيده") and before a slash or a colon ("السيد/ كريم"). Up to three names follow (a first, a
# father's and a family name; "عبد الله", "أبو علي" count as one), never a word that no name is
# (``ARABIC_NOT_NAMES``: "في", "على", "الذي", "وافق", "الرسمية") nor one after a punctuation mark, so that
# "الحاجة إلى المياه" (the need for water) and "مدرسة الشيخ زايد الرسمية" keep their words.
_ARABIC_MARKS = "[\u064b-\u0652]*"  # vowel marks a title may be written with ("الحاجّ")
_AL = "(?:ال|لل)"
_ARABIC_PERSON = (
    rf"{_AL}(?:سيد[ةه]?|[آا]نس[ةه]|[أا]ستاذ[ةه]?|حاج[ةه]?|مهندس[ةه]?){_ARABIC_MARKS}"
    r"|(?<![^\W\d_])(?:[وفبل]|ال|لل|وال|بال)?مدام"
)
_ARABIC_PLACE = rf"{_AL}(?:دكتور[ةه]?|شيخ[ةه]?){_ARABIC_MARKS}"
ARABIC_NOT_NAMES = frozenset(
    {"في", "فى", "عن", "على", "من", "إلى", "الى", "مع", "و", "أو", "او", "ثم", "لكن", "بل", "حتى", "منذ"}
    | {"عند", "لدى", "خلال", "حول", "ضمن", "بين", "بعد", "قبل", "دون", "نحو", "عبر", "ضد", "الذي", "التي"}
    | {"الذين", "اللذين", "اللتين", "اللواتي", "اللاتي", "الذى", "أن", "إن", "ان", "أنه", "انه", "أنها"}
    | {"انها", "قد", "لقد", "لم", "لن", "لا", "ما", "هل", "هو", "هي", "هم", "هن", "نحن", "هذا", "هذه"}
    | {"ذلك", "تلك", "هناك", "هنا", "كما", "كذلك", "أيضا", "أيضاً", "ايضا", "حيث", "إذ", "إذا", "اذا"}
    | {"كل", "بعض", "جميع", "غير", "يوم", "اليوم", "أمس", "لأن", "لان", "كي", "كان", "كانت", "يكون"}
    | {"تكون", "ليس", "ليست", "تم", "قام", "قامت", "قال", "قالت", "أكد", "أكدت", "اكد", "اكدت", "أفاد"}
    | {"أفادت", "افاد", "افادت", "وافق", "وافقت", "طلب", "طلبت", "حضر", "حضرت", "زار", "زارت", "أشار"}
    | {"أشارت", "اشار", "اشارت", "أوضح", "أوضحت", "اوضح", "اوضحت", "شارك", "شاركت", "يعمل", "تعمل"}
    | {"الرسمية", "الرسمي", "الخاصة", "الخاص", "الابتدائية", "المتوسطة", "الثانوية", "المختلطة"}
)
_ARABIC_STOP = r"[\s،,.:;؛!?()\[\]{}\"«»/]"
_ARABIC_NOT_NAME = rf"(?!(?:{'|'.join(sorted(ARABIC_NOT_NAMES, key=len, reverse=True))})(?:{_ARABIC_STOP}|$))"
_ARABIC_PARTICLE = r"(?:(?:عبد|أبو|ابو|أبي|ابي|بن|بنت|ابن|آل)\s+)?"
# a title is never a name either: it starts one of its own ("السيد كريم السيدة ليلى صعب" is two)
_ARABIC_NOT_TITLE = rf"(?![وفب]?(?:{_ARABIC_PERSON}|{_ARABIC_PLACE})(?:{_ARABIC_STOP}|$)|[دمأ]\.)"
_ARABIC_WORD = rf"{_ARABIC_NOT_NAME}{_ARABIC_NOT_TITLE}{_ARABIC_PARTICLE}(?=[^\W\d_])(?:(?!{_ARABIC_STOP}).)+"
_ARABIC_NAME = rf"{_ARABIC_WORD}(?:\s+{_ARABIC_WORD}){{0,2}}"
_ARABIC_AFTER = r"(?:\s*[/:]\s*|\s+)"  # a space, or a slash or a colon ("السيد/ كريم", "السيد/كريم")
_ARABIC_SHORT = rf"(?<![\w.])(?<!\d\s)(?:د|م|أ)\.\s*{_ARABIC_NAME}"
ARABIC_TITLE_NAME = re.compile(
    rf"(?:{_ARABIC_PERSON}|{_ARABIC_PLACE}){_ARABIC_AFTER}{_ARABIC_NAME}|{_ARABIC_SHORT}"
)
ARABIC_PERSON_TITLE_NAME = re.compile(rf"(?:{_ARABIC_PERSON}){_ARABIC_AFTER}{_ARABIC_NAME}|{_ARABIC_SHORT}")
# A word: letters, with the accents and Arabic vowel marks that may sit inside it
WORD = re.compile(r"(?:[^\W\d_]|[̀-ًͯ-ٰٟ])+")
HONORIFICS = frozenset(
    {"mr", "mrs", "ms", "miss", "mx", "dr", "eng", "prof", "sir", "madam", "madame", "mme", "mlle"}
    | {"monsieur", "hajj", "haj", "hajja", "hajjeh", "ustaz", "ustaza", "ustadh", "mister", "doctor"}
    | {"professor", "engineer"}
)
LIST_SEPARATORS = re.compile(r"[;/&|\n]|\band\b", re.IGNORECASE)
# The particles of Lebanese surnames, which may stand between a first and a last name ("Karim el Nassar")
PARTICLES = frozenset({"el", "al", "bou", "abou", "abu", "abi", "ben", "bin", "de", "di"})
MIDDLE = 2  # names (a father's, a grandfather's) that may stand between a known first and last name

# The fields of the eTools Datamart that hold a person's name (or email)
DATAMART_FIELDS = (
    ("ActionPoint", "assigned_to_name"),
    ("TPMVisit", "author_name"),
    ("MonitoringFinding", "visit_lead"),
    ("ReportedIndicator", "submitted_by"),
    ("ProgrammaticVisit", "primary_traveler"),
)

# More places that hold people's names, added by other apps when they start (Monitoring insights adds
# the field monitoring teams). Each gives raw names or e-mail addresses; one that fails is logged and
# skipped, so a missing table never empties the list.
EXTRA_SOURCES: list[Callable[[], Iterable[str]]] = []

_cache: tuple[float, frozenset[str], frozenset[str]] | None = None


# ---------------------------------------------------------------------------- comparing names
def fold(text: str) -> str:
    """``text`` in lower case and without accents, for comparing names."""
    text = str(text or "")
    if text.isascii():  # nothing to take off (the common case, and a character-by-character walk)
        return text.casefold()
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def visible(text: str) -> str:
    """``text`` without the characters nobody sees (``INVISIBLE``)."""
    text = str(text or "")
    return text if text.isascii() else INVISIBLE.sub("", text)


def words(text: str) -> list[str]:
    """The words of ``text``, folded (see :func:`fold`)."""
    return [fold(w) for w in WORD.findall(str(text or ""))]


def name_forms(raw: str) -> set[str]:
    """The forms a written name is looked for in: "Ms Jane Doe (UNICEF)" gives "jane doe"; "Doe,
    Jane" gives "doe jane" and "jane doe"; "Jane Mary Doe" gives "jane mary doe" and "jane doe".
    Several names in one field ("Jane Doe; John Roe") give each. Fewer than two words give nothing."""
    text = re.sub(r"\([^)]*\)", " ", EMAIL.sub(" ", visible(raw)))
    forms: set[str] = set()
    for piece in LIST_SEPARATORS.split(text):
        parts = [p for p in piece.split(",") if p.strip()]
        candidates = list(parts)
        if len(parts) == 2:  # "Doe, Jane": also both ways round
            candidates += [f"{parts[1]} {parts[0]}", f"{parts[0]} {parts[1]}"]
        for candidate in candidates:
            found = words(candidate)
            while found and found[0] in HONORIFICS:
                found = found[1:]
            if len(found) < 2:
                continue
            forms.add(" ".join(found[:LONGEST]))
            if len(found) >= 3:
                forms.add(f"{found[0]} {found[-1]}")
    return forms


def emails_in(text: str) -> set[str]:
    return {e.lower().strip(".") for e in EMAIL.findall(visible(text))}


# ---------------------------------------------------------------------------- e-mail addresses written out
# One rule for every text NeuroDB sends to the AI (first written for the KM Database's questions):
# :func:`withhold_addresses` takes an address in any form, written out too, from a record's or a document's
# text (the record rule), and :func:`without_addresses` from what a person types (the typed rule, stronger).
LINK_WITHHELD = "[link withheld]"  # ``redact.LINK_WITHHELD`` (redact reads models: it reads this one)
# PostgreSQL's English stop words (its english.stop, copied): the KM Database's search drops them
# (``km.search.ENGLISH_STOP``), and a question's word before "at" and a domain that is one is no name
ENGLISH_STOP = frozenset(
    "i me my myself we our ours ourselves you your yours yourself yourselves he him his himself she her hers "
    "herself it its itself they them their theirs themselves what which who whom this that these those am is "
    "are was were be been being have has had having do does did doing a an the and but if or because as "
    "until while of at by for with about against between into through during before after above below to "
    "from up down in out on off over under again further then once here there when where why how all any "
    "both each few more most other some such no nor not only own same so than too very s t can will just "
    "don should now".split()
)
_AT_SIGNS = ("@", "＠", "﹫")
# what every spelled-out address holds: an "at" (or an at sign) and a domain's ending after a dot ("x at
# example dot org", "x [at] example.org", "x [ at ] example [ dot ] org", "x -at- example -dot- org"): a text
# without both is not searched for one (most texts)
_SPELLED_AT = re.compile(r"\bat\b|[@＠﹫]|\barobase\b|_at_|\(\s*a\s*\)", re.IGNORECASE)
_SPELLED_HINT = re.compile(
    r"(?:\.|\bdot\b\s*[\]\)\}>'\"\u2018\u2019\u201c\u201d*/-]?|\bperiod\b\s*[\]\)]?|\[\.\]|\(\.\)|\{\.\}|<\s*\.\s*>"
    r"|_dot_)\s*(?:org|com|net|int|gov|edu|lb|fr|uk|de|ch|ca|ae|sa|qa|jo|eg|nl|se|au|info|biz|ngo|io|co|invalid"
    r"|test|example)\b",
    re.IGNORECASE,
)
# a word and "at" (or an at sign) before a domain of any case or ending ("x_y at Example.Org", "X AT
# EXAMPLE.ORG", "x_y at example.invalid", "x at example. org", "x at example dot invalid", "x @ example dot
# sy"): an address when the word is its name (:func:`named`) or the dot is spelled out, and the domain ends as
# one does (:func:`_domain_end`). Never a word that ends with a full stop ("e.g. at example.org") nor a number
# ("4.2 at example.org")
_LOCAL_PART = r"(?P<local>(?=[\w.+'-]*[^\W\d_])[\w+'-](?:[\w.+'-]*[\w+'-])?)"
_SEPARATOR = rf"(?:\.|\s\.|{_DOMAIN_DOT})"  # a full stop, after a space too ("example .org"), or spelled out
# after a dot and a space ("x at example. org", "x at example. net. lb", "x at example. Org" as a phone's
# keyboard writes it): the endings of a domain that no sentence starts with in lower case ("at Halba. net
# enrolment", "at Halba. info sessions", "at Halba. de facto" stay), and the few that none starts with
# capitalised either ("at Halba. Net enrolment", "at Tyre. De facto", "at Akkar. Gov staff" stay). Those a
# sentence may start with (``SPACED_STARTERS``: "at schools. eg water", "at akkar. uk funded", "at Akkar. NGO
# staff", "at Halba. ORG chart") count only after an address's name (:func:`shaped`) or an "at" no sentence
# writes. ".net", ".info" and ".de" (``weak``: "at Halba. net enrolment", "at Halba. info sessions", "at
# Tyre. de facto" stay) count in what a person types after a word that names someone, when no word of the
# sentence follows them ("x at example. net before June", "x at example. Net before June"), and in any text
# after an address's name or an "at" no sentence writes ("x@example. info", "x@example. Net")
_SPACED_LOWER = "org|com|gov|edu|int|lb|fr|uk|ch|ae|qa|jo|eg|nl|au|io|ngo|biz|invalid"
_SPACED_CAPITAL = "|".join(f"{e.capitalize()}|{e.upper()}" for e in ("org", "com", "lb", "ngo", "invalid"))
_SPACED_ANY = f"{_SPACED_LOWER}|{_SPACED_CAPITAL}"
SPACED_STARTERS = frozenset(
    {"eg", "qa", "uk", "fr", "ngo", "Ngo", "NGO", "Invalid", "INVALID", "LB", "Com", "ORG"}
)
_WEAK = "net|info|de"
WRITTEN_AT = re.compile(
    rf"(?<![\w.+'-]){_LOCAL_PART}(?P<at>{_AT_ANY})"
    rf"(?P<domain>(?=[\w-]*[^\W\d_])[\w-]+(?:{_SEPARATOR}[\w-]+)*{_SEPARATOR}[^\W\d_]{{2,24}}(?![\w-])"
    rf"|[\w-]+(?:\.\s?[\w-]+)*?\.\s(?-i:{_SPACED_ANY})\b"
    rf"|[\w-]+(?:\.\s?[\w-]+)*?\.\s(?:{_WEAK})\b)",
    re.IGNORECASE,
)
# what :data:`WRITTEN_AT` needs, besides "at": a dot right before letters, or before a space and an ending, or
# a dot spelled out (most texts have none: "at school", "at 45.5%", "at home. The")
DOTTED = re.compile(
    rf"\.(?:[^\W\d_]{{2}}|\s(?:{_SPACED_LOWER}|{_WEAK})\b)|\b(?:dot|period)\b|_dot_"
    r"|[\[({<'\"\u2018\u2019\u201c\u201d*/]\s*(?:dot|period|\.)\s*[\])}>'\"\u2018\u2019\u201c\u201d*/]",
    re.IGNORECASE,
)
_LABEL_SEP = re.compile(rf"\s?\.\s?|{_DOMAIN_DOT}", re.IGNORECASE)  # between the labels of a domain
_PLAIN_DOT = re.compile(r"\s?\.\s?")
_SPACED_SEP = re.compile(r"\s?\.\s")  # a full stop and a space after it ("example. eg")
_PLAIN_AT = re.compile(r"\s+at\s+", re.IGNORECASE)  # "at" as a sentence writes it
_PLAIN_WORD_AT = re.compile(r"([^\W\d_]+)(\s+at\s+)", re.IGNORECASE)  # one plain word and "at"
# the endings of a domain in any case ("Example.Org", "EXAMPLE.ORG", "example.INVALID"); any other only in
# lower case and when it is no word of a sentence ("at school.it was", "at Halba.We met", "AT SITE.NO
# ISSUES", "at Mt.Lebanon" stay; a country's that is one, "x.y at example.it", in a record's text only after
# a name with a full stop, "_" or "+", or of letters then digits: "x2 at example.it", :func:`_record_shaped`),
# nor a file's ("annex.pdf"); after a dot spelled out, of three letters at most ("x at example dot sy"; "the
# dot plot" stays)
KNOWN_ENDINGS = frozenset(_ENDINGS.split("|")) | {"co", "invalid", "test", "example", "mil", "app", "dev"}
NOT_ENDINGS = frozenset(
    {"pdf", "doc", "docx", "xls", "xlsx", "csv", "txt", "ppt", "pptx", "jpg", "jpeg", "png", "gif", "zip"}
    | {"htm", "html", "php", "asp", "aspx", "json", "xml", "mp3", "mp4", "map"}
)
# the words a text writes before "at" and a domain that name no one: the common words of a sentence ("children
# at example.org", "more at unicef.org"; never one letter, which may be a name's, nor "will" or "don", which
# are names too), and the words a web address follows ("available at", "the dashboard at")
ORDINARY = frozenset(w for w in COMMON_WORDS | ENGLISH_STOP if len(w) > 1) - {"will", "don"}
LINK_WORDS = frozenset(
    {"available", "online", "posted", "published", "hosted", "located", "found", "held", "open", "shared"}
    | {"uploaded", "listed", "accessible", "website", "websites", "site", "sites", "portal", "dashboard"}
    | {"dashboards", "page", "pages", "link", "links", "guide", "guides", "guidance", "platform", "form"}
    | {"forms", "tool", "tools", "document", "documents", "file", "files", "resources", "data", "database"}
    | {"info", "information", "details", "profile", "registration", "register", "apply", "read", "see"}
    | {"find", "library", "repository", "folder", "drive"}
    # a citation's, a footer's or a page's words ("Report accessed at", "Contact us at", "the focal point at",
    # "the methodology at", "the chart at")
    | {"accessed", "retrieved", "downloaded", "viewed", "obtained", "archived", "advertised", "us", "point"}
    | {"methodology", "guidelines", "statistics", "results", "report", "reports", "survey", "study", "annex"}
    | {"chart", "charts", "map", "maps", "table", "tables", "graph", "error", "errors", "export", "section"}
    # what a question says of a page before its address ("the red flag at", "the page is blank at", "I am
    # stuck at", "the export fails at")
    | {"flag", "flags", "score", "scores", "blank", "empty", "wrong", "stuck", "fails", "failed", "failing"}
    | {"broken", "missing", "slow", "crash", "crashes", "crashed", "button", "buttons", "filter", "filters"}
    | {"tab", "tabs", "column", "columns", "row", "rows", "number", "numbers", "figure", "figures", "value"}
    | {"values", "total", "totals", "count", "counts", "icon", "icons", "warning", "warnings", "message"}
    | {"messages", "screen", "screens", "view", "views", "list", "lists", "menu", "field", "fields", "box"}
    | {"label", "labels", "legend", "card", "cards", "panel", "line", "lines", "bar", "bars", "problem"}
    | {"problems", "issue", "issues", "bug", "bugs", "mistake", "mistakes", "shown", "showing", "look"}
    # and what a page does there ("the map hangs at", "the page freezes at", "what does it mean at")
    | {"hangs", "hang", "freezes", "freeze", "frozen", "loading", "loads", "differ", "differs", "mean"}
    | {"means", "save", "saving", "disagree", "disagrees"}
)
# the measures of education and of a programme, which name no one before "at" ("the enrolment at Halba. Net or
# gross?", "rates at Saida. Net please", "dropout at Halba. info on dropout?")
MEASURE_WORDS = frozenset(
    {"enrolment", "enrolments", "enrollment", "enrollments", "attendance", "absence", "absences", "dropout"}
    | {"dropouts", "absenteeism", "retention", "completion", "repetition", "promotion", "transition", "rate"}
    | {"rates", "ratio", "ratios", "coverage", "access", "participation", "achievement", "performance"}
    | {"distribution", "distributions", "percentage", "proportion", "average", "trend", "trends", "gap"}
    | {"gaps", "increase", "decrease", "growth", "decline", "progress", "outcome", "outcomes", "indicator"}
    | {"indicators", "target", "targets", "baseline", "funding", "spending", "budget", "budgets"}
)
KEEPS = ORDINARY | LINK_WORDS | MEASURE_WORDS
# letters and marks that look like those of an address in what a person types ("ａｔ", "example．org", the
# Cyrillic "а" of "аt", the mathematical "𝚊𝚝", the circled "ⓐⓣ", the small capitals "ᴀᴛ"): read as the plain
# ones, so that an address written with them is found as it reads. Only letters, "." and "@" ("10³ km²" stays)
_PLAIN_SIGNS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.@")
_STYLED_BLOCKS = (
    (0x02B0, 0x02FF),  # modifier letters
    (0x1D00, 0x1DBF),  # phonetic extensions (their modifier letters)
    (0x2070, 0x209F),  # superscript and subscript letters
    (0x2100, 0x214F),  # letterlike symbols
    (0x2460, 0x24FF),  # circled letters
    (0xFE50, 0xFE6F),  # small form variants
    (0x1D400, 0x1D7FF),  # mathematical letters
    (0x1F100, 0x1F1FF),  # squared letters
)
_STYLED = {
    chr(c): plain
    for first, last in _STYLED_BLOCKS
    for c in range(first, last + 1)
    if (plain := unicodedata.normalize("NFKC", chr(c))) in _PLAIN_SIGNS
}
_SMALL_CAPITALS = dict(zip("ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘʀꜱᴛᴜᴠᴡʏᴢ", "abcdefghijklmnoprstuvwyz", strict=True))
LOOKALIKES = str.maketrans(
    _STYLED
    | _SMALL_CAPITALS
    | {chr(0xFF01 + n): chr(0x21 + n) for n in range(94)}
    | dict(zip("　․﹒。｡аАеЕоОтТԁαοΟτɑ", " ....aAeEoOtTdaoOta", strict=True))
)
# a dot that looks like a full stop between two letters of what a person types ("example·org", "example∙org")
_LOOKALIKE_DOT = re.compile(r"(?<=\w)[·∙⋅۔](?=\w)")
# a time, an ordinal, a measure, an item's number written with a full stop, a quarter, a grade, a class, a
# round, a version, "COVID-19" or "H1N1" before "at" ("9.30am at halba.we met", "9am at halba.no delays",
# "Visit no.2 at akkar.we", "q3 at akkar.it will", "KG2 at school.it was", "room-2 at school.it", "4b at
# school.it", "v2.0 at https://"): no address's name
_NOT_A_NAME = re.compile(
    r"\d{1,2}(?:[.:h]\d{2})?(?:[ap]\.?m\.?|h|hrs?)|\d{1,2}[.:]\d{2}"  # "9.30am", "9am", "14h30", "10.15"
    r"|\d+(?:st|nd|rd|th|km|kms|kg|cm|mm|ml|usd|lbp|hours|mins|days|yrs|years|x)"  # "2nd", "15km", "3x"
    r"|(?:no|nb|nr|num|vol|pp?|ch|art|sec|fig|tab|item|ref)\.\d{1,4}[a-z]?"
    r"|(?:q|h|s|t|w|p|r|g|kg|eb|sdg|grade|level|phase|round|cycle|wave|week|day|class|room|block|unit|site|zone"
    r"|sector|area|lot|batch|group|team|tent|ward|step|stage|module|session|annex|section|part|year|term)"
    r"-?\d{1,3}[a-z]?|covid-?19|h\d{1,2}n\d{1,2}"  # "KG2", "EB6" (grades), "room-2", "COVID-19", "H1N1"
    r"|\d{1,2}[a-z]|(?-i:[A-Z]\d{1,2})|v\d{1,3}(?:\.\d{1,3})+",  # a class ("4b", "9B", "B2"), "v2.0"
    re.IGNORECASE,
)
# the chat's and a request's words a sentence missing its space after a place goes on with ("at akkar.ok", "at
# Halba.please list them", "at zahle.thanks", "at saida.compare with 2024"): never a domain's ending after a
# plain word (the common words of a sentence, ``ORDINARY``, never are either). Never an ending a domain has
# (".today", ".plus", ".cool", ".lol", ".np"); those of ``GOES_ON_ENDINGS`` (".show", ".how", ".who",
# ".total") only when a word of the sentence follows them ("at akkar.show me by district")
GOES_ON = frozenset(
    {"ok", "hi", "ty", "yo", "okay", "please", "pls", "plz", "thanks", "thank", "thx", "tnx", "merci"}
    | {"shukran", "sorry", "yes", "yeah", "yep", "nope", "hello", "next", "btw", "compare", "show"}
    | {"list", "give", "tell", "display", "explain", "summarise", "summarize", "find", "see", "check", "sort"}
    | {"include", "exclude", "add", "remove", "break", "split", "rank", "plot", "chart", "send", "need"}
    | {"want", "les", "des", "une", "svp", "same", "kindly", "what", "how", "why", "which", "who"}
    | {"cheers", "great", "perfect", "nice", "good", "regards", "bye", "maybe", "since", "last"}
    | {"make", "count", "total", "number", "numbers", "anything", "everything", "something"}
    | {"nothing", "especially", "particularly", "mainly", "mostly", "even", "still", "yet", "already"}
    | {"really", "actually", "otherwise", "instead", "else"}
)
GOES_ON_ENDINGS = frozenset({"show", "how", "who", "total"})
# the common words of a sentence that are also a domain's ending (".school", ".health", ".group", ".team"):
# never a word of a sentence's grammar ("also", "per", "via")
_GRAMMAR = frozenset("without onto across per via every many would could must due less several".split())
_GRAMMAR |= {"others", "also"}
_NOUN_ENDINGS = frozenset(w for w in COMMON_WORDS if len(w) > 2) - ENGLISH_STOP - _GRAMMAR
# the words after a domain that follow an address, not a sentence ("x at example. net before June", "x at
# example.school, thanks"): when it is written to by, a thank you, a greeting
ADDRESS_FOLLOWS = frozenset(
    {"before", "after", "until", "till", "asap", "thanks", "thank", "thx", "tnx", "ty", "regards", "cheers"}
    | {"today", "tomorrow", "soon", "urgently"}
)
# the words before an address's name that write to it ("contact x at example. net about the data", "write to x
# at hotmail. fr please"): after one, a common word of a sentence or a request follows an address too
WRITES_TO = frozenset(
    {"contact", "contacts", "contacting", "contacted", "email", "emails", "emailing", "emailed", "e-mail"}
    | {"mail", "write", "writing", "wrote", "message", "reach", "cc", "bcc", "forward", "reply", "ask", "to"}
    | {"ping", "notify", "inform"}
)
_AFTER_WRITES_TO = ENGLISH_STOP | GOES_ON | ADDRESS_FOLLOWS
# what comes after a domain: the text's end, a mark ("-", "(", "&" too), or a word (group 1)
_AFTER_DOMAIN = re.compile(r"\s*(?:$|[.,;:!?)\]}/\"'\u2019\u201d(&]|[-\u2013\u2014](?=\s|$)|(\w+))")
_WORD_BEFORE = re.compile(r"([^\W\d_][\w-]*)[\s:,]*$")  # the word before an address's name
# ".net", ".info" and ".de" after a dot and a space ("at Halba. net enrolment", "x at example. net")
WEAK_ENDINGS = frozenset({"net", "info", "de"})
# a place's first word written short with a full stop ("mt.lebanon", "st.elias", "mar.elias", "ain.ebel"):
# before a word of letters, a place, never a domain; and before "at", never an address's name ("Students from
# St.Joseph at Tyre. Info sessions")
_PLACE_FIRST = frozenset({"mt", "st", "mar", "ain"})
_PLACE_SHORT = re.compile(r"(?:mt|st|mar|ain)\.[^\W\d_]+", re.IGNORECASE)
# a word and "at" before a link written with its scheme ("x at https://example.org", "x at
# hxxps://example.org" as it is defanged): a link is never an e-mail address, so in what a person types the
# word stays and the link goes as a link, but an address's name (:func:`shaped`) or a word before an "at" no
# sentence writes goes as an address's would; in a record's text, an address's name (:func:`_record_shaped`)
# goes with the link
_AT_SCHEME = re.compile(
    rf"(?<![\w.+'-]){_LOCAL_PART}(?P<at>{_AT_ANY})(?:h[tx]{{2}}ps?|ftp)://"
    r"(?:[^\s<>\"]*[^\s<>\".,;:!?)'*\]])?",
    re.IGNORECASE,
)
# a question kept before the typed rule, whose address lost only its domain (as a link): its name is still
# written before "at" ("x at [link withheld]", "x(at)[link withheld]")
_BEFORE_LINK_WITHHELD = re.compile(
    rf"(?<![\w.+'-]){_LOCAL_PART}(?P<at>{_AT_ANY}){re.escape(LINK_WITHHELD)}", re.IGNORECASE
)
# what is left of a web address's path, its port or its query after an address or a link withheld ("x at
# example.org/staff/name", "the guide at example.org/people/name"): it may hold a name, so it goes with them;
# one without a letter stays ("x at example.org/71 123 456": a phone's, which its rule takes)
_PATH_LEFT = re.compile(
    r"(\[(?:email|link) withheld\])(?::\d{1,5})?[/?#](?=[^\s\[\]]*[^\W\d_])"
    r"[^\s\[\]<>\"]*[^\s\[\]<>\".,;:!?)'*]"
)


def keeps(word: str) -> bool:
    """Whether ``word``, written before "at" and a domain, names no one (:data:`KEEPS`)."""
    return len(word) > 1 and fold(word) in KEEPS


def shaped(local: str) -> bool:
    """Whether ``local``, the word before "at", has an address's name's shape: a full stop, "_", "+" or a
    digit inside, but no time or item's number (:data:`_NOT_A_NAME`: "9.30am", "no.2", "q3")."""
    return bool(re.search(r"[._+\d]", local)) and not _NOT_A_NAME.fullmatch(local)


def _record_shaped(local: str) -> bool:
    """Whether ``local`` makes a word of a sentence of two letters ("it", "no") a domain's ending in a
    record's text: a full stop, "_" or "+" inside, a digit with a letter in lower case anywhere ("zfakemira2",
    "z2fakemira", "2zfakemira", "mira-2"), or three letters or more then a few digits ("ZFAKEMIRA2 at
    example.be"; "PD2023" is a code), but no time, item's number, grade, class or disease ("Visit no.2 at
    akkar.we", "KG2 at school.it was", "COVID-19 at school.no classes", "Class 4B at Tyre", "H1N1 at
    school")."""
    if _NOT_A_NAME.fullmatch(local):
        return False
    if re.search(r"[._+]", local):
        return True
    if not any(c.isdigit() for c in local):
        return False
    return any(c.islower() for c in local) or bool(re.fullmatch(r"[^\W\d_]{3,}\d{1,3}", local))


def named(local: str, at: str, typed: bool) -> bool:
    """Whether ``local``, the word before "at" (``at``) and a domain, is an address's name: it has a name's
    shape (:func:`shaped`), it and "AT" are in capitals ("X AT EXAMPLE.ORG"), or "at" is written as no
    sentence writes it (an at sign, "[at]", "-at-", "arobase"); in what a person typed (``typed``), any word
    but one that names no one (:func:`keeps`: "children at example.org" keeps "children") or a time or an
    item's number ("9.30am", "no.2")."""
    if shaped(local) or (local.isupper() and "AT" in at) or not _PLAIN_AT.fullmatch(at):
        return True
    return typed and not keeps(local) and not _NOT_A_NAME.fullmatch(local)


def _ending(label: str, spelled: bool, reading: _Reading, labels: int = 1, stop: int = 0) -> bool:
    """Whether ``label`` ends a domain as an address's does (:data:`KNOWN_ENDINGS`, see there), the domain
    read after its name and "at" as ``reading`` says; ``stop``: where the label ends in the domain. An ending
    that is a word of a sentence counts after an address's name only: of two letters ("x2 at example.it", "X
    AT example.it"; in capitals after a name and "AT" in capitals, when what follows it follows an address:
    "X AT EXAMPLE.IT" at a text's end); a common word's of three letters or more (".school", ".health",
    ".who") after an address's name ("x.y at example.school for the data") or a word that names someone,
    when what follows it follows an address ("x at example.school before June"). In what a person typed
    after a plain word and "at" (``reading.plain``), an ending that is no known one counts ("x at
    example.sy", "x at example.ogr", "x at example.online"), but a word a sentence missing its space goes on
    with after one label (:data:`GOES_ON`: "at Halba.please", "at zahle.thanks", "at akkar.ok" stay; those of
    :data:`GOES_ON_ENDINGS` count when what follows them follows an address: "x at example.show before
    June"; after two labels or more, ``labels``, it counts: "x at mail.example.ok")."""
    if not re.fullmatch(r"[^\W\d_]{2,24}", label):
        return False
    folded = fold(label)
    if folded in KNOWN_ENDINGS:
        return True
    if folded in NOT_ENDINGS:
        return False
    if not label.islower():
        return reading.caps and len(label) == 2 and label.isupper() and reading.follows(stop)
    if folded in ORDINARY:
        if len(label) == 2:
            return reading.shaped or reading.caps
        ending = folded in _NOUN_ENDINGS or folded in GOES_ON_ENDINGS  # ".school", ".who"
        return ending and (reading.addressy or (reading.named and reading.follows(stop)))
    if reading.plain and labels < 2 and folded in GOES_ON:
        return folded in GOES_ON_ENDINGS and reading.follows(stop)
    return not spelled or len(label) <= 3 or (reading.shaped and not reading.plain)


def _spelled(domain: str) -> bool:
    """Whether a dot of ``domain`` is spelled out ("example dot org", "example[.]org")."""
    return any(not _PLAIN_DOT.fullmatch(sep.group()) for sep in _LABEL_SEP.finditer(domain))


def _domain_end(domain: str, reading: _Reading) -> int:
    """Where the domain an address may have ends in ``domain`` (a :data:`WRITTEN_AT` match's): after the last
    label that ends one (:func:`_ending`; "example.org.It" ends after "org"), and when a dot is spelled out,
    no label is a word of a sentence ("the dot plot"); 0 when none does. After a dot and a space, an ending a
    sentence may start with (:data:`SPACED_STARTERS`: "at schools. eg water"; :data:`WEAK_ENDINGS`: "at Halba.
    net enrolment") counts as ``reading.spaced`` says. A place's first word written short ("mt.lebanon",
    "st.elias") before a word of letters is a place."""
    seps = list(_LABEL_SEP.finditer(domain))
    for i in range(len(seps) - 1, -1, -1):
        stop = seps[i + 1].start() if i + 1 < len(seps) else len(domain)
        spelled = _spelled(domain[:stop])
        label = domain[seps[i].end() : stop]
        folded = fold(label)
        if _SPACED_SEP.fullmatch(seps[i].group()) and (label in SPACED_STARTERS or folded in WEAK_ENDINGS):
            if not reading.spaced(stop):
                continue
        elif (
            i == 0
            and seps[0].group() == "."
            and fold(domain[: seps[0].start()]) in _PLACE_FIRST
            and folded not in KNOWN_ENDINGS
        ):
            continue
        if not _ending(label, spelled, reading, labels=i + 1, stop=stop):
            continue
        starts = [0, *(sep.end() for sep in seps[:i])]
        labels = [domain[a : sep.start()] for a, sep in zip(starts, seps[: i + 1], strict=True)]
        if not spelled or not any(fold(label) in ORDINARY for label in labels):
            return stop
    return 0


def spelled_out(match: re.Match, typed: bool = False, links: bool = True) -> str:
    """What a :data:`SPELLED_EMAIL` match is replaced by: :data:`EMAIL_WITHHELD`; or, after one word that
    names no one (:func:`keeps`: "children at example.org", "available at www.unicef.org/lebanon"), in what a
    person typed (``typed``) or in a record's text (``links``), only the domain as a link: withheld here
    ("children at [link withheld]") in typed text or when its dot is spelled out, else left to the link rule,
    which takes its path too ("available at [link withheld]")."""
    word = _PLAIN_WORD_AT.match(match.group(0))
    if word and (typed or links) and keeps(word.group(1)):
        if typed or _spelled(match.group(0)[word.end() :]):
            return f"{word.group(1)}{word.group(2)}{LINK_WITHHELD}"
        return match.group(0)
    return EMAIL_WITHHELD


@dataclass(frozen=True)
class _Reading:
    """How a :data:`WRITTEN_AT` match's domain is read, after its name and "at": ``shaped``, a word of two
    letters of a sentence may end it (what a person typed, an address's name: :func:`_record_shaped`, "at"
    written as no sentence writes it); ``plain``, in what a person typed after a plain word, any ending
    counts; ``named``, the word before "at" names someone; ``addressy``, it has an address's name's shape
    (:func:`_record_shaped`, never a place's first word written short: "St.Joseph") or "at" is written as no
    sentence writes it (not an at sign with a space); ``caps``, it and "AT" are in capitals; ``spaced`` and
    ``follows``, whether an ending after a dot and a space counts, and whether what follows a label
    (where it ends in the domain) follows an address (:func:`_follows_address`)."""

    shaped: bool
    plain: bool
    named: bool
    addressy: bool
    caps: bool
    spaced: Callable[[int], bool]
    follows: Callable[[int], bool]


def written_out(match: re.Match, typed: bool = False, links: bool = True) -> str:
    """What a :data:`WRITTEN_AT` match is replaced by: :data:`EMAIL_WITHHELD` when the domain ends as an
    address's does (:func:`_domain_end`; what follows it stays: "x.y at example.org.It was") and the word
    before "at" is a name (:func:`named`) or the dot is spelled out; the domain as a link after a word that
    names no one, as :func:`spelled_out`, in what a person typed only when it has a known ending, a path or
    "www." ("visits at mt.lebanon", "red flags at akkar.district" stay); else the match as it is ("published
    at Example.Org")."""
    domain, local, at = match.group("domain"), match.group("local"), match.group("at")
    text, start = match.string, match.start("domain")
    plain_at = bool(_PLAIN_AT.fullmatch(at))
    name_like = shaped(local) or not plain_at
    record_shaped = _record_shaped(local) or not plain_at
    caps = local.isupper() and "AT" in at

    @functools.cache
    def writes_to() -> bool:
        return _writes_to(text, match.start())

    def follows(stop: int) -> bool:
        return _follows_address(text, start + stop, writes_to)

    def spaced(stop: int) -> bool:
        counts = _spaced_counts(local, at, typed)
        return follows(stop) if counts is None else counts

    reading = _Reading(
        shaped=typed or record_shaped,
        plain=typed and not name_like,
        named=record_shaped or caps or (typed and not keeps(local) and not _NOT_A_NAME.fullmatch(local)),
        addressy=(_record_shaped(local) and not _PLACE_SHORT.fullmatch(local))
        or not (plain_at or at.strip() in _AT_SIGNS),
        caps=caps,
        spaced=spaced,
        follows=follows,
    )
    end = _domain_end(domain, reading)
    if not end:
        return match.group(0)
    rest = domain[end:]
    spelled = _spelled(domain[:end])
    if (typed or links) and plain_at and keeps(local):
        if typed and not spelled and not _a_link(domain[:end], text, start + end):
            return match.group(0)
        return f"{local}{at}{LINK_WITHHELD}{rest}" if typed or spelled else match.group(0)
    if spelled or named(local, at, typed):
        return f"{EMAIL_WITHHELD}{rest}"
    return match.group(0)


def _a_link(domain: str, text: str, end: int) -> bool:
    """Whether ``domain``, written after a word that names no one and "at" in what a person typed, is a web
    address's: a known ending (:data:`KNOWN_ENDINGS`), "www." or a path, a port or a query after it (``end``,
    where it ends in ``text``); "visits at mt.lebanon", "red flags at akkar.district" are places."""
    last = _LABEL_SEP.split(domain)[-1]
    if fold(last) in KNOWN_ENDINGS or fold(domain).startswith("www."):
        return True
    return bool(re.match(r"(?::\d{1,5})?[/?#]", text[end:]))


def _spaced_counts(local: str, at: str, typed: bool) -> bool | None:
    """Whether an ending a sentence may start with after a dot and a space (". net", ". info", ". de", ".
    eg", ". NGO", :data:`SPACED_STARTERS`) ends an address's domain: always (True) after an address's name
    (:func:`_record_shaped`), an at sign with no space around it after a word that names someone ("x@example.
    info") or "at" written as no sentence writes it ("[at]", "(at)"); never (False) after a plain word in a
    record's text, or a word that names no one in what a person typed (:func:`keeps`: "the enrolment at
    Halba. Net or gross?", "visits at Akkar. NGO staff"); else by what follows it (None:
    :func:`_follows_address`), after a plain word in what a person typed ("x at example. net before June",
    "dropout at Halba. info on dropout?" stays), an at sign with a space ("Training @ Halba. Info session to
    follow."), a class, a disease or a saint's school ("Class 4B at Tyre. Info sessions", "Students from
    St.Joseph at Tyre. Info sessions"), or a name and "AT" in capitals."""
    place = bool(_PLACE_SHORT.fullmatch(local))
    if _record_shaped(local) and not place:
        return True
    if at in _AT_SIGNS:
        return None if keeps(local) else True
    if at.strip() in _AT_SIGNS:
        return None
    if not _PLAIN_AT.fullmatch(at):
        return True
    if place or shaped(local) or (local.isupper() and "AT" in at):
        return None
    return None if typed and not keeps(local) else False


def _writes_to(text: str, start: int) -> bool:
    """Whether the word before an address's name (at ``start`` of ``text``) writes to it (:data:`WRITES_TO`:
    "contact x at ...", "write to x at ...")."""
    before = _WORD_BEFORE.search(text, max(0, start - 40), start)
    return bool(before) and fold(before.group(1)) in WRITES_TO


def _follows_address(text: str, pos: int, writes_to: Callable[[], bool]) -> bool:
    """Whether what comes after ``pos`` of ``text`` follows an address rather than goes on with a sentence:
    the text's end, a mark, a word of :data:`ADDRESS_FOLLOWS` ("before June", "thanks"); after a word that
    writes to an address (``writes_to``: "contact x at example. net about the data"), any common word of a
    sentence or a request too. "at Halba. net enrolment", "at Halba. Net or gross?" go on with a sentence."""
    after = _AFTER_DOMAIN.match(text, pos)
    if not after:
        return False
    word = after.group(1)
    if word is None:
        return True
    word = fold(word)
    return word in ADDRESS_FOLLOWS or (word in _AFTER_WRITES_TO and writes_to())


def without_paths(text: str) -> str:
    """``text`` without what is left of a web address's path after an address or a link withheld
    (:data:`_PATH_LEFT`)."""
    return _PATH_LEFT.sub(r"\1", text) if "withheld]" in text else text


def withhold_addresses(text: str, typed: bool = False, links: bool = True) -> str:
    """``text`` with its e-mail addresses withheld (``EMAIL``), written out too (``SPELLED_EMAIL`` and
    :data:`WRITTEN_AT`: :func:`spelled_out`, :func:`written_out`), and what is left of a path after one
    (:func:`without_paths`: "x at example.org/staff/name"). Without ``typed``, the record rule: a sentence
    that only looks like an address stays ("published at Example.Org", "located at Tyre.Net", "at school.it
    was", "at 2pm at Halba.We met"), and so does a web address after a word that names no one ("available
    at www.unicef.org", "more at unicef.org": the link rule takes it), unless ``links`` is false (the KM
    Database's rule, as it was written: any address the record rule's patterns find goes). ``typed`` (what a
    person typed: use :func:`without_addresses`): any word but one that names no one goes with the domain
    after "at", and such a word keeps its place, the domain withheld as a link ("children at [link
    withheld]"). In a record's text, an address's name before "at" and a link with its scheme goes with the
    link (:func:`withhold_schemed`: "x.y at https://example.org")."""
    if any(sign in text for sign in _AT_SIGNS):
        text = EMAIL.sub(EMAIL_WITHHELD, text)
    if not _SPELLED_AT.search(text):
        return without_paths(text)
    if _SPELLED_HINT.search(text):
        text = SPELLED_EMAIL.sub(functools.partial(spelled_out, typed=typed, links=links), text)
    if DOTTED.search(text):
        text = WRITTEN_AT.sub(functools.partial(written_out, typed=typed, links=links), text)
    return without_paths(text if typed else withhold_schemed(text))


def withhold_schemed(text: str) -> str:
    """A record's text with an address's name before "at" and a link with its scheme withheld with the link
    (:func:`_record_schemed`: "x.y at https://example.org"); the link rule takes any other."""
    return _AT_SCHEME.sub(_record_schemed, text) if "://" in text else text


def _record_schemed(match: re.Match) -> str:
    """A word and "at" before a link with its scheme in a record's text (:data:`_AT_SCHEME`): an address's
    name (:func:`_record_shaped`: "x.y at https://example.org", "x2 at https://example.org") or a word before
    an "at" no sentence writes ("x (at) https://example.org") goes with the link as an address; any other
    word stays, the link left to the link rule ("Register @ https://example.org", "Release v2.0 at
    https://")."""
    local, at = match.group("local"), match.group("at")
    if _record_shaped(local) or not (_PLAIN_AT.fullmatch(at) or at.strip() in _AT_SIGNS):
        return EMAIL_WITHHELD
    return match.group(0)


def without_addresses(text: str) -> str:
    """What a person typed (a question, a page's title, search words) as it is read for an address or a
    name: no character nobody sees (:func:`visible`), the letters and marks that look like those of an
    address as the plain ones (:data:`LOOKALIKES`), and every e-mail address withheld, written out too, by
    the typed rule (:func:`withhold_addresses`)."""
    read = _LOOKALIKE_DOT.sub(".", visible(text or "").translate(LOOKALIKES))
    read = withhold_addresses(read, typed=True)
    return _AT_SCHEME.sub(_schemed, read) if "://" in read else read


def _schemed(match: re.Match) -> str:
    """A word and "at" before a link with its scheme in what a person typed (:data:`_AT_SCHEME`): a link is
    never an e-mail address, so the word stays and the link is withheld as a link ("the red flag at [link
    withheld]"); an address's name (:func:`shaped`) or a word before an "at" no sentence writes goes with it
    as an address's would."""
    local, at = match.group("local"), match.group("at")
    if shaped(local) or not _PLAIN_AT.fullmatch(at):
        return EMAIL_WITHHELD
    return f"{local}{at}{LINK_WITHHELD}"


def withheld_address(text: str) -> bool:
    """Whether ``text``, read as a person typed it (:func:`without_addresses`), holds an e-mail address."""
    return without_addresses(text).count(EMAIL_WITHHELD) > (text or "").count(EMAIL_WITHHELD)


def without_emails(text: str) -> str:
    """A document's or a code-written text sent whole (a document to review or summarise, a report's pages,
    a daily review finding, a What's new line): its e-mail addresses withheld, written out too, by the
    record rule (:func:`withhold_addresses`, read without the characters nobody sees); a text that holds
    none is sent exactly as it is."""
    shown = visible(text or "")
    cleaned = withhold_addresses(shown)
    return text if cleaned == shown else cleaned


def without_kept_names(text: str) -> str:
    """A typed text kept by an earlier cleaning, which took an address's domain as a link and left its name
    ("x at [link withheld]"): that name withheld with it, by the typed rule; a word that names no one stays
    ("children at [link withheld]", "the guidance at [link withheld]")."""
    if LINK_WITHHELD not in text:
        return text

    def kept(match: re.Match) -> str:
        return EMAIL_WITHHELD if named(match.group("local"), match.group("at"), True) else match.group(0)

    return _BEFORE_LINK_WITHHELD.sub(kept, text)


# ---------------------------------------------------------------------------- the names known
def known_names(refresh: bool = False) -> frozenset[str]:
    """Every person name (folded, two words or more) and email address NeuroDB holds, read once per
    run: kept for ``CACHE_SECONDS``, or read again with ``refresh``."""
    global _cache
    now = time.monotonic()
    if refresh or _cache is None or now - _cache[0] > CACHE_SECONDS:
        known, not_people_ = _read()
        _cache = (now, frozenset(known), frozenset(not_people_))
    return _cache[1]


def not_people() -> frozenset[str]:
    """The names NeuroDB never takes for people (sections, donors), read with :func:`known_names`: a
    reader that learns names elsewhere (a record's own person keys) leaves them out too."""
    known_names()
    return _cache[2] if _cache else frozenset()


def forget() -> None:
    """Empty the names read (the next :func:`known_names` reads them again)."""
    global _cache
    _cache = None


def _read() -> tuple[set[str], set[str]]:
    from django.db import transaction

    from neurodb.accounts.models import Section, User
    from neurodb.datamart import models as dm
    from neurodb.donors.models import DonorAccount
    from neurodb.partnerships.models import PCA, PartnerStaffMember
    from neurodb.review.models import FindingAssignment

    raw: list[str] = []
    for model_name, field in DATAMART_FIELDS:
        model = getattr(dm, model_name)
        raw += model.objects.exclude(**{field: ""}).order_by().values_list(field, flat=True).distinct()
    for points in (
        PCA.objects.exclude(unicef_focal_points=None).order_by().values_list("unicef_focal_points", flat=True)
    ):
        raw += [p for p in points or () if p]
    for first, last, name, email in User.objects.order_by().values_list(
        "first_name", "last_name", "name", "email"
    ):
        raw += [f"{first or ''} {last or ''}", name or "", email or ""]
    for first, last, email in PartnerStaffMember.objects.order_by().values_list(
        "first_name", "last_name", "email"
    ):
        raw += [f"{first or ''} {last or ''}", email or ""]
    raw += FindingAssignment.objects.exclude(owner="").order_by().values_list("owner", flat=True).distinct()
    for source in EXTRA_SOURCES:
        try:
            with transaction.atomic():  # a failed query must not break the caller's transaction
                raw += [str(name) for name in source() if name]
        except Exception:
            logger.exception("people: the names of %s could not be read", getattr(source, "__name__", source))
    known: set[str] = set()
    for text in set(raw):
        known |= name_forms(text) | emails_in(text)
    sections = {" ".join(words(n)) for n in Section.objects.values_list("name", flat=True)}
    # a donor's sign-in carries the donor's name ("European Union") as its first name: an organisation
    donors: set[str] = set()
    for name, etools_names in DonorAccount.objects.values_list("name", "donors"):
        for donor in (name, *(etools_names or ())):
            donors |= name_forms(donor)
    return known - sections - donors, sections | donors


def _shape_of(names: frozenset[str]) -> tuple[int, frozenset[str]]:
    """The words of the longest name, and the first word of every name (a name is only looked for where
    one of them is written)."""
    split = [n.split(" ") for n in names if "@" not in n]
    return min(LONGEST, max((len(n) for n in split), default=0)), frozenset(n[0] for n in split if len(n) > 1)


def _longest_of(names: frozenset[str]) -> int:
    return _shape_of(names)[0]


# The words of the longest name and the first words, per set of names. Kept by the set itself, not by
# what it holds: a set read again holds the same names in a new object, and comparing two large sets name
# by name on every text would cost more than counting their words.
_LONGEST: dict[int, tuple[frozenset[str], int, frozenset[str]]] = {}


def _shape(names: frozenset[str]) -> tuple[int, frozenset[str]]:
    if len(names) < 100:  # a record's own names, an empty set: by what they hold
        return _small_shape(names)
    found = _LONGEST.get(id(names))
    if found is None or found[0] is not names:
        if len(_LONGEST) >= 8:
            _LONGEST.clear()
        found = _LONGEST[id(names)] = (names, *_shape_of(names))
    return found[1], found[2]


@functools.lru_cache(maxsize=4096)
def _small_shape(names: frozenset[str]) -> tuple[int, frozenset[str]]:
    """:func:`_shape_of` of a few names (a record's own: the same people are named in many records)."""
    return _shape_of(names)


def _longest(names: frozenset[str]) -> int:
    return _shape(names)[0]


def _as_set(names: Iterable[str] | None) -> frozenset[str]:
    if names is None:
        return known_names()
    return names if isinstance(names, frozenset) else frozenset(names)


def _name_spans(
    text: str, names: frozenset[str], extra: frozenset[str] = frozenset()
) -> list[tuple[int, int, str]]:
    """Where known names (and the ``extra`` names of one record, kept apart so that the cached set of
    known names is used as it is) are written in ``text``: (start, end, folded name), the longest from
    each word, in order; two names may overlap ("Hiba Saad Karam" for "hiba saad" and "saad karam"), so
    that every word of every name is found whatever is written around it. A known name of two words is
    also found with up to ``MIDDLE`` names between them ("Karim Ahmad Nassar" for "karim nassar": Arabic
    names often carry the father's name), each a capitalised word, a word of a script without capitals
    or a particle (never "Karim met Nassar")."""
    longest, firsts = _shape(names)
    more, extra_firsts = _shape(extra) if extra else (0, frozenset())
    longest = max(longest, more)
    if longest < 2:
        return []
    found = WORD.findall(text.lower()) if text.isascii() else [fold(w) for w in WORD.findall(text)]
    if firsts.isdisjoint(found) and extra_firsts.isdisjoint(found):  # no name starts anywhere: the usual case
        return []
    tokens = [(m.start(), m.end(), fold(m.group(0))) for m in WORD.finditer(text)]
    spans = []
    for i, (start, _, word) in enumerate(tokens):
        if word not in firsts and word not in extra_firsts:  # no name starts with this word
            continue
        for size in range(min(longest, len(tokens) - i), 1, -1):
            form = " ".join(t[2] for t in tokens[i : i + size])
            if form in names or form in extra:
                spans.append((start, tokens[i + size - 1][1], form))
                break
        else:
            size = _with_middle(text, tokens, i, names, extra)
            if size:
                last = tokens[i + size - 1]
                spans.append((start, last[1], f"{word} {last[2]}"))
    return spans


def _with_middle(
    text: str, tokens: list[tuple[int, int, str]], i: int, names: frozenset[str], extra: frozenset[str]
) -> int:
    """How many words from ``tokens[i]`` make a known two-word name with middle names between (0: none)."""
    for middle in range(1, MIDDLE + 1):
        last = i + middle + 1
        if last >= len(tokens):
            return 0
        start, end, word = tokens[last - 1]
        if text[start:end][:1].islower() and word not in PARTICLES:
            return 0
        form = f"{tokens[i][2]} {tokens[last][2]}"
        if form in names or form in extra:
            return middle + 2
    return 0


# ---------------------------------------------------------------------------- finding and removing
def mentions(text: str, names: Iterable[str] | None = None, extra: frozenset[str] = frozenset()) -> list[str]:
    """The person names and email addresses written in ``text`` (any email address counts)."""
    text = visible(text)
    found = sorted(emails_in(text))
    names = _as_set(names)
    return found + [form for _, _, form in _name_spans(EMAIL.sub(" ", text), names, extra)]


def scrub(text: str, names: Iterable[str] | None = None, extra: frozenset[str] = frozenset()) -> str:
    """``text`` with every email address and known person name (and ``extra`` name) replaced by a
    placeholder."""
    text = EMAIL.sub(EMAIL_WITHHELD, visible(text))
    merged: list[list[int]] = []  # names that overlap are withheld together
    for start, end, _ in _name_spans(text, _as_set(names), extra):
        if merged and start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = f"{text[:start]}{NAME_WITHHELD}{text[end:]}"
    return text
