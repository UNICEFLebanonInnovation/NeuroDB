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
# a country's ending after a domain's: glued in lower case or in capitals ("example[.]com.lb", "example dot
# gov.lb", "example. net.lb", "example[.]co.UK"), or after a full stop and a space as a phone's keyboard
# writes it ("x@example.org. lb"; only the endings no sentence starts with in lower case): part of the
# domain, never left after the address withheld ("[email withheld].lb")
_COUNTRIES = "lb|fr|uk|de|ch|ca|ae|sa|qa|jo|eg|nl|se|au|io|sy|tr"
_COUNTRY_SPACED = rf"\.\s(?-i:{_COUNTRIES})\b"
_COUNTRY_AFTER = rf"\.(?-i:[a-z]{{2}}|[A-Z]{{2}})(?![\w-])|{_COUNTRY_SPACED}"
# An address, also written with spaces around "@" ("layla.saab @ unicef.org", then only before a
# domain that ends in letters: "2 trainers @ 50 USD" stays), with the sign in brackets ("x(@)y.org",
# "x [@] y.org") or with the full-width or the small sign
EMAIL = re.compile(
    rf"(?<![\w.+'-])[\w.+'-]+(?:{_AT_SIGN}|\s*(?:\[\s*{_AT_SIGN}\s*\]|\(\s*{_AT_SIGN}\s*\))\s*"
    rf"|\s*{_AT_SIGN}\s*(?=[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{{2,}}\b))[\w-]+(?:\.[\w-]+)+(?:{_COUNTRY_SPACED})?"
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
_ENDINGS = "org|com|net|int|gov|edu|lb|fr|uk|de|ch|ca|ae|sa|qa|jo|eg|nl|se|au|info|biz|ngo|io"
_SPELLED_ONLY = (
    "co|invalid|test|example"  # endings of an address written out only ("x-at-example-dot-invalid")
)
_KNOWN = f"{_ENDINGS}|{_SPELLED_ONLY}|mil|app|dev"  # every ending :data:`KNOWN_ENDINGS` holds
# in a domain only (never in a name, where "the period ends" is a sentence): "period" for the dot too, and the
# French "point" before a domain's ending ("x arobase example point org"; "the data point de facto" stays)
_DOMAIN_DOT = (
    rf"(?:{_DOT}|\s+period\s+|\s*[\[(]\s*period\s*[\])]\s*"
    rf"|\s+point\s+(?=(?:{_ENDINGS}|{_SPELLED_ONLY}|mil|app|dev)\b))"
)
_ENDING = rf"(?:{_ENDINGS}|co\.[a-z]{{2}})(?:\.[a-z]{{2}})?"  # with a country's: "unicef.org.lb", "co.uk"
# "example dot org dot lb", and with the French "point" ("x arobase sante point gov point lb"; only the
# countries that are no French word: "point de vue" stays)
_POINT_COUNTRY = r"\s+point\s+(?:lb|fr|uk|ch|ae|qa|jo|eg|nl|au|io|sy|tr)\b"
_SPELLED_ENDING = rf"(?:{_ENDINGS}|co)(?:{_DOT}[a-z]{{2}}|{_POINT_COUNTRY}|{_COUNTRY_AFTER})?"
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
    rf"(?-i:{_SPACED_ENDING})\b(?:{_COUNTRY_AFTER})?"
    rf"|\b{_SPACED}\s*{_AT_SIGN}\s*[\w-]+(?:\s?\.\s?[\w-]+)*\s?\.\s?(?-i:{_SPACED_ENDING})\b(?:{_COUNTRY_AFTER})?"
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
    r"(?:\.|\bdot\b\s*[\]\)\}>'\"\u2018\u2019\u201c\u201d*/-]?|\bperiod\b\s*[\]\)]?|\bpoint\b|\[\.\]|\(\.\)"
    r"|\{\.\}|<\s*\.\s*>|_dot_)\s*(?:org|com|net|int|gov|edu|lb|fr|uk|de|ch|ca|ae|sa|qa|jo|eg|nl|se|au|info"
    r"|biz|ngo|io|co|invalid|test|example)\b",
    re.IGNORECASE,
)
# a word and "at" (or an at sign) before a domain of any case or ending ("x_y at Example.Org", "X AT
# EXAMPLE.ORG", "x_y at example.invalid", "x at example. org", "x at example dot invalid", "x @ example dot
# sy"): an address when the word is its name (:func:`named`) or the dot is spelled out, and the domain ends as
# one does (:func:`_domain_end`). Never a word that ends with a full stop ("e.g. at example.org") nor a number
# ("4.2 at example.org")
_LOCAL_PART = r"(?P<local>(?=[\w.+'-]*[^\W\d_])[\w+'-](?:[\w.+'-]*[\w+'-])?)"
# a dot between the words of a domain the written spec reads: a full stop (a space before or after it too), a
# dot spelled out, "point" or "period" (:data:`WRITTEN_AT`'s ``spec``)
_SPEC_SEP = rf"(?:\s?\.\s?|{_DOT}|\s+(?:point|period)\s+|\s*[\[(]\s*period\s*[\])]\s*)"
_SEPARATOR = rf"(?:\.|\s\.|{_DOMAIN_DOT})"  # a full stop, after a space too ("example .org"), or spelled out
# after a dot and a space ("x at example. org", "x at example. net. lb", "x at example. Org" as a phone's
# keyboard writes it): the endings of a domain that no sentence starts with in lower case ("at Halba. net
# enrolment", "at Halba. info sessions", "at Halba. de facto" stay), and the few that none starts with
# capitalised either ("at Halba. Net enrolment", "at Tyre. De facto", "at Akkar. Gov staff" stay). Those a
# sentence may start with (``SPACED_STARTERS``: "at schools. eg water", "at akkar. uk funded", "at Akkar. NGO
# staff", "at Halba. ORG chart") count only after an address's name (:func:`shaped`) or an "at" no sentence
# writes. ".net", ".info", ".de" and the countries' a sentence may start with in any case (``weak``: "at
# Halba. net enrolment", "at Halba. info sessions", "at Tyre. de facto", "at Tyre. Fr Elias", "at Halba. ca
# 300 children" stay), and ".it" and ".me" in lower case ("at halba. it was closed"), count in what a person
# types after a word that names someone, when no word of the sentence follows them ("x at example. net before
# June", "x at example. Net before June", "x at hotmail. Fr", "x at example. it", "x at example. net is the
# focal point"), and in any text after an address's name or an "at" no sentence writes ("x@example. info",
# "x@example. Net", "x@example. sa"); a space before the dot too ("x at example . Com")
_SPACED_LOWER = "org|com|gov|edu|int|lb|fr|uk|ch|ae|qa|jo|eg|nl|au|io|ngo|biz|invalid"
_SPACED_CAPITAL = "|".join(f"{e.capitalize()}|{e.upper()}" for e in ("org", "com", "lb", "ngo", "invalid"))
_SPACED_ANY = f"{_SPACED_LOWER}|{_SPACED_CAPITAL}"
SPACED_STARTERS = frozenset(
    {"eg", "qa", "uk", "fr", "ngo", "Ngo", "NGO", "Invalid", "INVALID", "LB", "Lb", "Com", "ORG", "Org"}
)
# the capitalised endings of a domain among them that end one after a word that may name someone, whatever
# follows, when the domain's words name no place and no thing ("Rania at example. Lb for the files", "the
# focal point is zfakemira at example. Org and she knows"; "Staff were present at Mt. Lebanon. Org chart
# updated", "Kits were delivered at Tyre. Lb of rice each" stay)
NAMED_SPACED = frozenset({"Org", "ORG", "Lb", "LB", "Com"})
_WEAK = "net|info|de|fr|sa|se|ca"
_SPACED_WORDS = "it|me"  # in lower case only: "at Halba. It was" is a sentence's
WRITTEN_AT = re.compile(
    rf"(?<![\w.+'-]){_LOCAL_PART}(?P<at>{_AT_ANY})"
    rf"(?P<domain>(?=[\w-]*[^\W\d_])[\w-]+(?:{_SEPARATOR}[\w-]+)*{_SEPARATOR}[^\W\d_]{{2,24}}(?![\w-])"
    rf"(?:{_COUNTRY_SPACED})?"
    rf"|[\w-]+(?:\s?\.\s?[\w-]+)*?\s?\.\s(?-i:{_SPACED_ANY})\b(?:{_COUNTRY_AFTER})?"
    rf"|[\w-]+(?:\s?\.\s?[\w-]+)*?\s?\.\s(?:{_WEAK})\b(?:{_COUNTRY_AFTER})?"
    rf"|[\w-]+(?:\s?\.\s?[\w-]+)*?\s?\.\s(?-i:{_SPACED_WORDS})\b(?:{_COUNTRY_AFTER})?"
    # any ending in any case after up to four words joined by dots of any kind ("Rania at example. Gov", "x at
    # Mt. Lebanon.Info", "x at staff point Bebnine point eg"): an address's only after a word that may name
    # someone, that the text writes to or names an address, or a page's word (:func:`written_out`)
    rf"|(?P<spec>[\w-]+(?:{_SPEC_SEP}[\w-]+){{0,3}}{_SPEC_SEP}(?:{_KNOWN})(?![\w-])(?:{_COUNTRY_AFTER})?))"
    # an ending after it, a dot and a space ("St.Joseph" in "x at St.Joseph. Org", "example. net" in "x at
    # example. net. Ca"): read with it by the written spec's rules, and withheld with it
    rf"(?P<tail>(?:\s?\.\s(?:{_KNOWN})(?![\w-]))*)",
    re.IGNORECASE,
)
# what :data:`WRITTEN_AT` needs, besides "at": a dot right before letters, or before a space and an ending, or
# a dot spelled out (most texts have none: "at school", "at 45.5%", "at home. The")
DOTTED = re.compile(
    rf"\.(?:[^\W\d_]{{2}}|\s(?:{_KNOWN}|{_SPACED_WORDS})\b)|\b(?:dot|period|point)\b|_dot_"
    r"|[\[({<'\"\u2018\u2019\u201c\u201d*/]\s*(?:dot|period|\.)\s*[\])}>'\"\u2018\u2019\u201c\u201d*/]",
    re.IGNORECASE,
)
_LABEL_SEP = re.compile(rf"\s?\.\s?|{_DOMAIN_DOT}", re.IGNORECASE)  # between the labels of a domain
_PLAIN_DOT = re.compile(r"\s?\.\s?")
_SPACED_SEP = re.compile(r"\s?\.\s")  # a full stop and a space after it ("example. eg")
_PLAIN_AT = re.compile(r"\s+at\s+", re.IGNORECASE)  # "at" as a sentence writes it
_PLAIN_WORD_AT = re.compile(r"([^\W\d_]+)(\s+at\s+)", re.IGNORECASE)  # one plain word and "at"
# the endings of a domain in any case ("Example.Org", "EXAMPLE.ORG", "example.INVALID"); any other as
# :func:`_ending` says: in what a person typed whatever its case, by the word before "at" ("x at EXAMPLE.NO"
# goes, "at school.it was", "The AP was closed at Akkar.No evidence", "at Mt.Lebanon" stay); in a record's
# text in lower case, or in any case after an address's name (:func:`_record_shaped`: "x.y at EXAMPLE.NO"), in
# capitals as :func:`_capital_ending` says ("AT SITE.NO ISSUES" stays); never a file's ("annex.pdf"); after a
# dot spelled out, of three letters at most ("x at example dot sy"; "the dot plot" stays)
KNOWN_ENDINGS = frozenset(_KNOWN.split("|"))
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
# the measures of education and of a programme, and what a question counts, which name no one before "at"
# ("the enrolment at Halba. Net or gross?", "rates at Saida. Net please", "dropout at Halba. info on
# dropout?", "the GER at Halba. Net?", "How many cases at Halba. Info?")
MEASURE_WORDS = frozenset(
    {"enrolment", "enrolments", "enrollment", "enrollments", "attendance", "absence", "absences", "dropout"}
    | {"dropouts", "absenteeism", "retention", "completion", "repetition", "promotion", "transition", "rate"}
    | {"rates", "ratio", "ratios", "coverage", "access", "participation", "achievement", "performance"}
    | {"distribution", "distributions", "percentage", "proportion", "average", "trend", "trends", "gap"}
    | {"gaps", "increase", "decrease", "growth", "decline", "progress", "outcome", "outcomes", "indicator"}
    | {"indicators", "target", "targets", "baseline", "funding", "spending", "budget", "budgets"}
    | {"ger", "ner", "gpi", "oosc", "finding", "findings", "partner", "partners", "pupil", "pupils", "case"}
    | {"cases", "incident", "incidents", "refugees", "learners", "monitoring", "status", "situation"}
)
# what a report or a question says happened there, and its plain nouns, before "at" ("What happened at
# halba.it was", "Which issues were reported at halba.in 2023?", "how many schools were visited at
# zahle.thanks", "The training took place at halba.it was good"); and a request's verbs ("Compare at ...")
REPORT_WORDS = frozenset(
    {"reported", "happened", "visited", "conducted", "closed", "enrolled", "recorded", "registered"}
    | {"delivered", "distributed", "monitored", "flagged", "raised", "cancelled", "canceled", "attended"}
    | {"opened", "received", "submitted", "completed", "collected", "identified", "observed", "noted", "done"}
    | {"met", "place", "stats", "visite", "visites", "kits", "compare", "show", "give", "tell", "display"}
    | {"explain", "summarise", "summarize", "check", "sort", "rank", "plot"}
)
KEEPS = ORDINARY | LINK_WORDS | MEASURE_WORDS | REPORT_WORDS
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
    | dict(zip("　․﹒。｡аАеЕоОтТԁαοΟτɑⱭ", " ....aAeEoOtTdaoOtaA", strict=True))
)
# a dot that looks like a full stop between two letters of what a person types ("example·org", "example∙org")
_LOOKALIKE_DOT = re.compile(r"(?<=\w)[·∙⋅۔](?=\w)")
# a time, an ordinal, a measure, an item's number written with a full stop, a quarter, a grade, a class, a
# round, a version, "COVID-19" or "H1N1", a field's code (a visit's, a shift's, a camp's, a lab's, a bus's, a
# cohort's, a floor's, an informal tented settlement's, a school's, a centre's, a public school's, a programme
# document's, a fiscal, school or academic year's) before "at" ("9.30am at halba.we met", "9am at halba.no
# delays", "Visit no.2 at akkar.we", "q3 at akkar.it will", "KG2 at school.it was", "room-2 at school.it", "4b
# at school.it", "v2.0 at https://", "Visit3 at halba.no issues"): no address's name in those readings; the
# written spec's E1 reads a code before a domain and an ending as any word that may name someone ("ITS12 at
# Bekaa. NGO staff", "t3 at example. org": initials or a birth date and digits look the same)
_NOT_A_NAME = re.compile(
    r"\d{1,2}(?:[.:h]\d{2})?(?:[ap]\.?m\.?|h|hrs?)|\d{1,2}[.:]\d{2}"  # "9.30am", "9am", "14h30", "10.15"
    r"|\d+(?:st|nd|rd|th|km|kms|kg|cm|mm|ml|usd|lbp|hours|mins|days|yrs|years|x)"  # "2nd", "15km", "3x"
    r"|(?:no|nb|nr|num|vol|pp?|ch|art|sec|fig|tab|item|ref)\.\d{1,4}[a-z]?"
    r"|(?:q|h|s|t|w|p|r|g|kg|eb|sdg|grade|level|phase|round|cycle|wave|week|day|class|room|block|unit|site|zone"
    r"|sector|area|lot|batch|group|team|tent|ward|step|stage|module|session|annex|section|part|year|term"
    r"|visit|shift|camp|lab|bus|cohort|floor|its|school|centre|center|ps|pd|fy|sy|ay"
    r"|cp|ce|cm|ms|gs|bt|ts|lt|bp|bln|alp|ece|nfe)"  # grades, programmes: "CE2", "CM1", "BT2", "BLN2", "ALP1"
    r"-?\d{1,3}[a-z]?|covid-?19|h\d{1,2}n\d{1,2}"  # "KG2", "EB6" (grades), "room-2", "COVID-19", "H1N1"
    r"|lcrp-?\d{4}"  # a plan's year ("LCRP2023")
    # a school, fiscal or academic year, a programme document's, a month's ("SY2024", "PD2023", "Mar2024")
    r"|(?:sy|fy|ay|pd|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)-?(?:19|20)\d{2}"
    r"|\d{1,2}[a-z]|(?-i:[A-Z]\d{1,2})|v\d{1,3}(?:\.\d{1,3})+",  # a class ("4b", "9B", "B2"), "v2.0"
    re.IGNORECASE,
)
# the chat's and a request's words a sentence missing its space after a place goes on with ("at akkar.ok", "at
# Halba.please list them", "at zahle.thanks", "at saida.compare with 2024"): a domain's ending in what a
# person typed only after a word that may name someone (the word before "at" decides: "x at example.ok", "x at
# example.show the files"; "how many visits at akkar.ok" stays), or after two labels ("x at mail.example.ok");
# a word that no domain ends with after a sentence missing its space ("at Halba.Mr Haddad")
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
# the words of a sentence that are also a domain's ending, of three letters or more: a request's (".show",
# ".how", ".who", ".total") and the common words' (".school", ".health", ".group", ".team"), never a word of a
# sentence's grammar ("also", "per", "via")
GOES_ON_ENDINGS = frozenset({"show", "how", "who", "total"})
_GRAMMAR = frozenset("without onto across per via every many would could must due less several".split())
_GRAMMAR |= {"others", "also"}
_NOUN_ENDINGS = frozenset(w for w in COMMON_WORDS if len(w) > 2) - ENGLISH_STOP - _GRAMMAR
# the words after a domain that follow an address, not a sentence ("x at example. net before June", "x at
# example.school, thanks", "contact x at example. net regarding the data"): when it is written to by, about
# what, a thank you, a greeting
ADDRESS_FOLLOWS = frozenset(
    {"before", "after", "until", "till", "asap", "thanks", "thank", "thx", "tnx", "ty", "regards", "cheers"}
    | {"today", "tomorrow", "yesterday", "soon", "urgently", "monday", "tuesday", "wednesday", "thursday"}
    | {"friday", "saturday", "sunday", "regarding", "concerning", "re"}
)
# what a sentence says of an address after a word that may name someone, in what a person typed ("x at
# example. net is the focal point", "x at example. info has the files")
NAMED_FOLLOWS = frozenset({"is", "has", "was", "sent", "holds"})
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
# ".net", ".info", ".de", ".fr", ".it"... after a dot and a space ("at Halba. net enrolment", "x at example.
# net")
SPACED_WORDS = frozenset(_SPACED_WORDS.split("|"))  # words of a sentence: what follows them decides alone
WEAK_ENDINGS = frozenset(_WEAK.split("|")) | SPACED_WORDS
# a place's first word written short with a full stop ("mt.lebanon", "st.elias", "mar.elias", "ain.ebel"):
# before a word of letters, a place, never a domain; and before "at", never an address's name ("Students from
# St.Joseph at Tyre. Info sessions")
_PLACE_FIRST = frozenset({"mt", "st", "mar", "ain"})
_PLACE_SHORT = re.compile(r"(?:mt|st|mar|ain)\.[^\W\d_]+", re.IGNORECASE)
# the endings of a domain the link rule (``redact.LINK``) takes: in any case, and in lower case only (or
# before a "/"), so that a sentence missing its space ("soon.To", "approx.US") is not taken for a link
LINK_ENDINGS = ("com", "org", "net", "int", "gov", "edu", "info", "biz", "app")
LINK_LOWER = ("io", "co", "lb", "uk", "fr", "ch", "us", "me", "ai", "ly", "gl", "to")
# the newer endings a role's address or a site has ("info at example.online", "the guide at example.blog"): a
# web address's after a word that names no one in what a person types (no word a sentence goes on with after a
# place missing its space: ".live", ".news", ".top" are not among them)
NEW_ENDINGS = frozenset(
    {"online", "site", "tech", "blog", "store", "shop", "cloud", "digital", "agency", "academy", "xyz"}
    | {"website", "systems", "solutions", "consulting", "studio"}
)
# words of two letters that no country's ending stands for after a place missing its space ("at akkar.ok", "at
# Halba.Mr Haddad", "at Halba.Mt Lebanon", "at Baalbek.El Ain", "at halba.go to tyre", "at halba.pm")
_NOT_COUNTRY = GOES_ON | HONORIFICS | _PLACE_FIRST | PARTICLES | {"go", "oh", "ah", "pm", "vs"}
# the words before a word and "at" that make a word that names no one a thing ("the data at [link withheld]
# was updated", :func:`_keeps_named`; never a word that may name someone: "Our Rania at Halba. Org" is an
# address)
DETERMINERS = frozenset(
    {"the", "a", "an", "any", "which", "what", "this", "that", "these", "those", "each", "every", "all"}
    | {"some", "no", "our", "their", "its", "whose"}
)
# "eg" then a mark and a word ("at schools. eg, latrines", "at schools. eg: latrines"): "e.g.," as it is typed
_EG_GOES_ON = re.compile(r"[,:.(]\s*([^\W\d_]+)")
# a link's scheme, mistyped, cut short or defanged too ("https:/", "http//", "htps://", "hxxps[:]//", "//",
# "mailto:"): what :data:`_AT_SCHEME` needs
_SCHEME = r"(?:(?:h[tx]{1,2}ps?|ftp)(?::/{1,2}|\[:\]/{1,2}|/{2})|//|mailto:)"
_SCHEME_HINT = re.compile(r":/|//|\[:\]|mailto:", re.IGNORECASE)
# a word and "at" before a link written with its scheme ("x at https://example.org", "x at
# hxxps://example.org" as it is defanged, "x at hxxps[:]//example[.]org", "x at http//example.org" or "x at
# htps://example.org" as it is mistyped, "x at //example.org", "x at mailto:example.org"): a link is never an
# e-mail address, so in what a person types the word stays and the link goes as a link, but an address's name
# (:func:`shaped`) or a word before an "at" no sentence writes goes as an address's would; in a record's text,
# an address's name (:func:`_record_shaped`) goes with the link
_AT_SCHEME = re.compile(
    rf"(?<![\w.+'-]){_LOCAL_PART}(?P<at>{_AT_ANY}){_SCHEME}"
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


# Lebanon's governorates, districts and main towns written as one word that is no person's name (never a
# town's other spelling, which is a family name too: "Saida", "Naameh"): before "at" in what a person typed,
# a place ("Compare Halba at akkar.ok"), and a place wherever the KM Database's texts write it
# (``km.privacy.PLACE_WORDS``)
MAIN_PLACE_WORDS = frozenset(
    "beirut mount akkar baalbek baalbeck hermel bekaa beqaa nabatieh nabatiyeh tripoli sidon tyre zahle "
    "zahleh jbeil byblos keserwan kesrouan metn matn baabda aley chouf shouf zgharta koura batroun bcharre "
    "becharre minieh dinnieh miniyeh danniyeh rashaya rachaya hasbaya marjayoun jezzine halba jounieh "
    "arsal".split()
)


def keeps(word: str) -> bool:
    """Whether ``word``, written before "at" and a domain, names no one (:data:`KEEPS`)."""
    return len(word) > 1 and fold(word) in KEEPS


def _typed_keeps(word: str) -> bool:
    """Whether ``word``, written before "at" and a domain in what a person typed, names no one: :func:`keeps`,
    or one of Lebanon's governorates, districts and main towns that no person's name is
    (:data:`MAIN_PLACE_WORDS`: "Compare Halba at akkar.ok"; its domain still goes as a link: "contact Halba
    at [link withheld] about the data")."""
    return keeps(word) or fold(word) in MAIN_PLACE_WORDS


def shaped(local: str) -> bool:
    """Whether ``local``, the word before "at", has an address's name's shape: a full stop, "_", "+" or a
    digit inside, but no time or item's number (:data:`_NOT_A_NAME`: "9.30am", "no.2", "q3")."""
    return bool(re.search(r"[._+\d]", local)) and not _NOT_A_NAME.fullmatch(local)


def _record_shaped(local: str) -> bool:
    """Whether ``local`` makes a word of a sentence of two letters ("it", "no") a domain's ending in a
    record's text: a full stop, "_" or "+" inside, a digit with a letter in lower case anywhere ("zfakemira2",
    "z2fakemira", "2zfakemira", "mira-2"), or capitals then digits (:func:`_coded`: initials and a digit,
    "ZM2", "KZ3"); but no time, item's number, grade, class, disease or field's
    code (:data:`_NOT_A_NAME`: "Visit no.2 at akkar.we", "KG2 at school.it was", "CE2 at school.it was",
    "COVID-19 at school.no classes", "Class 4B at Tyre", "H1N1 at school", "Visit3 at halba.no issues")."""
    if _NOT_A_NAME.fullmatch(local):
        return False
    if re.search(r"[._+]", local):
        return True
    if not any(c.isdigit() for c in local):
        return False
    return any(c.islower() for c in local) or _coded(local)


def _coded(local: str) -> bool:
    """Whether ``local`` is written in capitals then digits only: three letters or more then up to four
    digits, a dash before them too ("ZFAKEMIRA2", "ZFAKEMIRA1990", "ZFAKEMIRA-2"; "PD2023" is a code), two
    then up to three ("ZM2"). A name of an address, or a code a list of them does not know ("KZ3")."""
    return bool(re.fullmatch(r"[^\W\d_]{3,}-?\d{1,4}|[^\W\d_]{2}\d{1,3}", local)) and not any(
        c.islower() for c in local
    )


def named(local: str, at: str, typed: bool) -> bool:
    """Whether ``local``, the word before "at" (``at``) and a domain, is an address's name: it has a name's
    shape (:func:`shaped`), it and "AT" are in capitals ("X AT EXAMPLE.ORG"), or "at" is written as no
    sentence writes it (an at sign, "[at]", "-at-", "arobase"); in what a person typed (``typed``), any word
    but one that names no one (:func:`keeps`: "children at example.org" keeps "children", "Compare Halba at
    akkar.ok" keeps "Halba") or a time or an item's number ("9.30am", "no.2")."""
    if shaped(local) or (local.isupper() and "AT" in at) or not _PLAIN_AT.fullmatch(at):
        return True
    return typed and not _typed_keeps(local) and not _NOT_A_NAME.fullmatch(local)


def _ending(label: str, spelled: bool, reading: _Reading, labels: int = 1, stop: int = 0) -> bool:
    """Whether ``label`` ends a domain as an address's does (:data:`KNOWN_ENDINGS`, see there), the domain
    read after its name and "at" as ``reading`` says; ``stop``: where the label ends in the domain.

    In what a person typed, the word before "at" decides, whatever the label's case and whatever follows it:
    after a word that may name someone (not one that names no one, :func:`keeps`, nor a code), an ending that
    is also a word of a sentence or of a chat counts ("x at example.It", "x at EXAMPLE.NO", "x at example.ok",
    "x at example.show the files", "x at example.who has the data", "x at Example.Online"); after a word that
    names no one, a word a sentence missing its space goes on with stays ("how many visits at akkar.ok", "What
    was reported at halba.who is responsible?"), and one of two letters is a link's only when what follows it
    follows an address (:func:`_a_link`: "The AP was closed at Akkar.No evidence received" stays).

    In a record's text, the label's case counts as in lower case after an address's name
    (:func:`_record_shaped`: "x.y at EXAMPLE.NO", "x2 at Example.It"), and an ending that is a word of two
    letters of a sentence counts after an address's name with a full stop, "_", "+" or a letter in lower case,
    or in capitals and digits as initials and a digit write one ("KZ3 at school.it was hot", "ZM2 at
    saida.it"; a grade's or a field's code is none: "CE2 at school.it was crowded" stays); after a plain word
    only when both hold ("write to x at example.no before June"), or when the dot is spelled out ("x at
    example dot no"). A common word's ending of three letters or more (".school", ".who") counts after an
    address's name, or after a name in capitals and "AT" when what follows follows an address. An ending in
    capitals after any other word counts as :func:`_capital_ending` says."""
    if not re.fullmatch(r"[^\W\d_]{2,24}", label):
        return False
    folded = fold(label)
    if folded in KNOWN_ENDINGS:
        return True
    if folded in NOT_ENDINGS:
        return False
    if reading.foreign_any(stop):  # "example.it", "rania-fakemira.page", "zaynafakemira.center": a domain's
        return True
    if not label.islower() and not (
        reading.typed or reading.strong or reading.coded or reading.addressed(stop) or reading.foreign(stop)
    ):
        return label.isupper() and _capital_ending(label, folded, spelled, reading, stop)
    if folded in ORDINARY:
        if len(label) == 2:
            return _sentence_word(label, spelled, reading, stop)
        if folded in GOES_ON_ENDINGS:  # ".how", ".who"
            return reading.named or reading.addressy or reading.addressed(stop)
        if folded in _NOUN_ENDINGS:  # ".school", ".health"
            if reading.addressy or reading.addressed(stop) or reading.foreign(stop):
                return True
            return reading.named and (reading.typed or reading.follows(stop))
        # a word of a sentence's grammar (".now", ".all", ".out"): after a word that may name someone in what
        # a person typed, after an address's name, or before a domain no sentence writes (``reading.foreign``)
        return (reading.typed and reading.named) or reading.strong or reading.foreign(stop)
    if reading.plain and labels < 2 and folded in GOES_ON:  # ".ok", ".thanks", ".show", ".total"
        return reading.named
    return not spelled or len(label) <= 3 or (reading.shaped and not reading.plain)


def _sentence_word(label: str, spelled: bool, reading: _Reading, stop: int) -> bool:
    """Whether a word of two letters of a sentence ("it", "no", "be", "in") ends an address's domain (see
    :func:`_ending`)."""
    if reading.typed or reading.strong or (reading.caps and label.islower()) or reading.foreign(stop):
        return True
    if reading.coded:  # "how many ZM2 at Halba.so what", "ZM2 at saida.it was closed": initials and a digit
        return True
    if spelled and not reading.common:
        return True
    return reading.addressed(stop)


def _capital_ending(label: str, folded: str, spelled: bool, reading: _Reading, stop: int) -> bool:
    """Whether ``label``, in capitals, ends an address's domain in a record's text: after a name and "AT" in
    capitals, one of two letters when what follows it follows an address ("X AT EXAMPLE.IT" at a text's end);
    after a name in capitals that names someone (``reading.capitals``, "at" in capitals or not), one of two
    letters, or of three letters or more that is no word of a sentence ("X AT EXAMPLE.ONLINE"; a common
    word's, ".SCHOOL", or a request's, ".SHOW", may be one), when what follows it follows an address or is a
    word in lower case: a text of mixed case, no note written in capitals ("Fake survey: X AT EXAMPLE.IT holds
    the files"; "THE VISIT TOOK PLACE AT BAALBEK.IT WAS A GOOD VISIT", "DISTRIBUTION AT SITE.NO ISSUES"
    stay)."""
    if reading.caps and len(label) == 2 and reading.follows(stop):
        return True
    if not reading.capitals or reading.common:
        return False
    if len(label) > 2:
        if folded not in GOES_ON_ENDINGS and (
            folded in GOES_ON or (folded in ORDINARY and folded not in _NOUN_ENDINGS)
        ):
            return False
        if spelled and len(label) > 3 and not (reading.shaped and not reading.plain):
            return False
    return reading.follows(stop) or reading.mixed(stop)


def _spelled(domain: str) -> bool:
    """Whether a dot of ``domain`` is spelled out ("example dot org", "example[.]org")."""
    return any(not _PLAIN_DOT.fullmatch(sep.group()) for sep in _LABEL_SEP.finditer(domain))


def _domain_end(domain: str, reading: _Reading) -> int:
    """Where the domain an address may have ends in ``domain`` (a :data:`WRITTEN_AT` match's): after the last
    label that ends one (:func:`_ending`; "example.org.It" ends after "org"), and when a dot is spelled out,
    no label is a word of a sentence ("the dot plot"); 0 when none does. After a dot and a space, an ending a
    sentence may start with (:data:`SPACED_STARTERS`: "at schools. eg water"; :data:`WEAK_ENDINGS`: "at Halba.
    net enrolment") counts as ``reading.spaced`` says, or after a name before a domain no sentence writes as
    ``reading.spaced_foreign`` says (never after a plain word that a sentence goes on from: "Volunteers worked
    at night. Net enrolment rose"), or as ``reading.spaced_named`` says (any word the text writes to or names
    an address, a word that names no one too: "write to monitoring at example. net before June"; ". Org" or
    ". Lb" after a word that may name someone: "Rania at example. Lb for the files"). A place's first word
    written short ("mt.lebanon", "st.elias", "Mt. Lebanon") before a word of letters is a place."""
    seps = list(_LABEL_SEP.finditer(domain))
    for i in range(len(seps) - 1, -1, -1):
        stop = seps[i + 1].start() if i + 1 < len(seps) else len(domain)
        spelled = _spelled(domain[:stop])
        label = domain[seps[i].end() : stop]
        folded = fold(label)
        if _SPACED_SEP.fullmatch(seps[i].group()) and (label in SPACED_STARTERS or folded in WEAK_ENDINGS):
            if not (
                reading.spaced(stop, label)
                or reading.spaced_foreign(stop, label)
                or reading.spaced_named(stop, label)
            ):
                continue
        elif (
            i == 0
            and _PLAIN_DOT.fullmatch(seps[0].group())
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


def spelled_out(match: re.Match, typed: bool = False, links: bool = True, link_rule: bool = True) -> str:
    """What a :data:`SPELLED_EMAIL` match is replaced by: :data:`EMAIL_WITHHELD`; or, after one word that
    names no one (:func:`keeps`: "children at example.org", "available at www.unicef.org/lebanon"), in what a
    person typed (``typed``) or in a record's text (``links``), only the domain as a link: withheld here
    ("children at [link withheld]") in typed text or when its dot is spelled out, else left to the link rule,
    which takes its path too ("available at [link withheld]"), or here when no link rule follows
    (``link_rule`` false: a document sent whole). A role's mailbox written to or opening the text
    (:func:`_role`: "write to info at example.org"), and any word the text writes to or calls an address
    (:func:`_names_an_address`: "cc partners at example.org", "From: monitoring at example.org") is an
    address's name, and so is any word after a mailing context (:func:`_written_to`: "Contact us at
    example.org", "Write to the office at example dot org"); a "point" after a sentence's word is no dot
    (:func:`_sentence_point`)."""
    if _sentence_point(match.group(0), text=match.string, start=match.start(), end=match.end()):
        return match.group(0)
    word = _PLAIN_WORD_AT.match(match.group(0))
    # the written spec's E2: a mailing context before the name ("Contact us at example.org"), or an item of a
    # list of addresses ("To: x at example. Org, it at example.org")
    if _written_to(match.string, match.start()) or (
        word and _list_item_named(match.string, match.start(), 0, match.end(), word.group(1))
    ):
        return _withheld(match.group(0))
    if (
        word
        and (typed or links)
        and keeps(word.group(1))
        and not _role(
            word.group(1), match.string, match.start(), end=match.end(), domain=match.group(0)[word.end() :]
        )
        and not _keeps_named(
            word.group(1), match.string, match.start(), match.end(), typed, match.group(0)[word.end() :]
        )
    ):
        if typed or not link_rule or _spelled(match.group(0)[word.end() :]):
            return f"{word.group(1)}{word.group(2)}{LINK_WITHHELD}"
        return match.group(0)
    return _withheld(match.group(0))


@dataclass(frozen=True)
class _Reading:
    """How a :data:`WRITTEN_AT` match's domain is read, after its name and "at": ``typed``, in what a person
    typed; ``shaped``, a word of two letters of a sentence may end it (what a person typed, an address's name:
    :func:`_record_shaped`, "at" written as no sentence writes it); ``plain``, in what a person typed after a
    plain word, any ending counts; ``named``, the word before "at" names someone (in what a person typed, any
    word but one that names no one or a code); ``strong``, it has an address's name's shape that makes the
    label's case count as in lower case in a record's text (a full stop, "_", "+", a digit and a letter in
    lower case; never a place's first word written short: "St.Joseph"), or "at" is written as no sentence
    writes it (not an at sign with a space); ``coded``, it is written in capitals and digits only
    (:func:`_coded`); ``addressy``, ``strong`` or ``coded``; ``caps``, it and "AT" are in capitals;
    ``capitals``, it is in capitals ("at" in capitals or not) and no code (:data:`_NOT_A_NAME`); ``common``,
    it names no one (:func:`keeps`); ``spaced`` and ``follows``, whether an ending after a dot and a space
    counts, and whether what follows a label (where it ends in the domain) follows an address
    (:func:`_follows_address`); ``written_to``, whether a word that writes to the name comes before it
    (:func:`_writes_to`); ``addressed``, in a record's text, whether a plain word that may name someone is
    written to and what follows the label follows an address ("write to x at example.no before June");
    ``mixed``, whether a word in lower case follows the label; ``foreign``, whether a name-like word (in
    capitals, "X AT" or "X at", of capitals and digits, :func:`_coded`, with an address's name's shape, one
    that may name someone in what a person typed, or before an at sign) that names no one known stands before
    a domain whose words before the label no sentence writes (:func:`_foreign`: "X AT EXAMPLE.IT HAS THE
    DATA", "X2 at example.be has the data", "x2 at example. fr sent the report", "x @ example .it if needed";
    "KZ3 at school.it was hot", "QX12 at Halba.No teachers came", "Training @ Halba. Info session" stay);
    ``spaced_foreign``, ``foreign`` after a dot and a space ("x2 at example. fr sent the report"), and in a
    record's text a capitalised word that may name someone before a domain whose words name no place ("Sent by
    Zayna at example. it if needed"); ``spaced_named``, whether an ending after a dot and a space counts
    because the text writes to the word or names it an address (:func:`_names_an_address`, :func:`_writes_to`;
    a word that names no one too: "Contact monitoring at example. de.") or, for ". Org" and ". Lb"
    (:data:`NAMED_SPACED`), because it may name someone; ``foreign_any``, whether the domain's words before
    the label are no sentence's, whatever word stands before "at" (not a code)."""

    typed: bool
    shaped: bool
    plain: bool
    named: bool
    strong: bool
    coded: bool
    addressy: bool
    caps: bool
    capitals: bool
    common: bool
    spaced: Callable[[int, str], bool]
    follows: Callable[[int], bool]
    written_to: Callable[[], bool]
    addressed: Callable[[int], bool]
    mixed: Callable[[int], bool]
    foreign: Callable[[int], bool]
    spaced_foreign: Callable[[int, str], bool]
    spaced_named: Callable[[int, str], bool]
    foreign_any: Callable[[int], bool]


def written_out(match: re.Match, typed: bool = False, links: bool = True, link_rule: bool = True) -> str:
    """What a :data:`WRITTEN_AT` match is replaced by: :data:`EMAIL_WITHHELD` when the domain ends as an
    address's does (:func:`_domain_end`; what is glued after it goes with it: "x.y at example.org.It was", "x
    at example.co.It", "x at mail.example.me") and the word before "at" is a name (:func:`named`; in a
    record's text a plain word that is written to, when what follows follows an address: "write to x at
    example.no before June"), a role's mailbox written to or opening the text (:func:`_role`: "write to info
    at example.it"), a code written to before a domain no sentence writes ("write to KG2 at example.it for the
    files") or the dot is spelled out; the domain as a link after a word that names no one, or a code, as
    :func:`spelled_out`: in what a person typed only when it is a web address's (:func:`_a_link`: "children at
    [link withheld]"; "visits at mt.lebanon", "red flags at akkar.district" stay), in a record's text when the
    link rule does not take it (:func:`_left_to_links`: "Visit us at example.de"), or here when no link rule
    follows (``link_rule`` false, a document sent whole; ``links`` false, a KM Database's download); else the
    match as it is ("published at Example.Org"). A "point" after a sentence's word is no dot
    (:func:`_sentence_point`).

    The written spec's rules come first, with the endings after the domain, a dot and a space, read with it
    (``tail``: "at St.Joseph. Org", "at example. net. Ca"): E3, a page's word after a verb that goes to a page
    (:func:`_page_shape`) keeps its place, the domain goes as a link; E2, after a mailing context
    (:func:`_written_to`) or in a list of addresses (:func:`_list_item_named`) the whole address goes; E4, a
    field note's sentence after a registered place's name stays (:func:`_place_sentence`); E1, after a word
    that may name someone (no word that names no one, no place's; a code too), any domain with an ending goes
    (:func:`_named_end`). A given name, an initial or a title before a capitalised name goes with it
    (:func:`_withheld`). A domain only those rules read (``spec``) is left as it is otherwise, but after a
    word the text names an address (:func:`_names_an_address`); any other match is read as before
    (:func:`_written_rest`).

    Before a domain whose words before its ending no sentence writes (:func:`_foreign`: "example",
    "zaynafakemira", "mail.example"), any ending counts (``reading.foreign_any``), and: a code is an address's
    name whatever follows (:func:`_code_address`: "How many G5 at example.it", "team1 at example. org before
    June"); a plain word in a record's text is one when no link rule takes the domain ("Rania at example.it
    has the files"); after a word that names no one, the domain is a link (:func:`_a_link`: "How many visits
    at [link withheld] were done"), unless the text writes to that word or calls it an address
    (:func:`_keeps_named`: "write to monitoring at example.org", "From: partners at example.org", "Is info at
    example.org the right address?"); in a record's text or a KM Database's download, a code's web address the
    link rule does not take goes here as a link."""
    domain, local, at = match.group("domain"), match.group("local"), match.group("at")
    text, start = match.string, match.start("domain")
    plain_at = bool(_PLAIN_AT.fullmatch(at))
    # the written spec's rules first. A domain with an ending (:func:`_known_end`): E3, a page's word after a
    # verb that goes to a page ("Report to the new portal at zfakemira. org"): its domain goes as a link in
    # every text (whole before what follows an address); E2, a mailing context before the name, up to two
    # words between them (:func:`_written_to`: "Please email Zayna Fakemira at Halba. Lb", "Write to the
    # school at Minyara. Net"), or an item of a list of addresses: the whole address goes, whatever word its
    # name is
    tail = match.group("tail")
    full = f"{domain}{tail}"
    known = _known_end(full)
    # E4, a sentence after a registered place's name ("Teachers trained at Bebnine. Net enrolment rose after
    # that?"): a mailing context before it makes it E2's only when it writes to its word itself ("Email staff
    # at Halba. Org chart updated"; "Contact sessions were held at Matn. Info packs were shared." is E4's)
    sentence = _place_sentence(local, at, full, text, start + len(full))
    if known:
        rest = _GLUED_LABELS.sub("", full[known:], count=1)
        if _page_shape(local, text, match.start()):
            after = _next_word(text, start + len(full))
            if after is not None and fold(after) in ADDRESS_FOLLOWS:  # "... before June": a mailbox
                return f"{EMAIL_WITHHELD}{rest}"
            # (the link rule takes a web address written as one in a record's text: "example.org")
            if links and link_rule and not typed and plain_at and _LINK_TAKES.fullmatch(domain):
                return match.group(0)
            return f"{local}{at}{LINK_WITHHELD}{rest}"
        # (and every item of a list of addresses after one: "To: monitoring at example. Org, IT at example.
        # Org")
        written = _written_to_word(text, match.start()) if sentence else _written_to(text, match.start())
        if written or _list_item_named(text, match.start(), 0, start + known, local):
            return f"{_withheld(local)}{rest}"
    if sentence:  # E4: as it is
        return match.group(0)
    # E1, a word that may name someone (no word that names no one, no place's): before a domain and an ending,
    # an address's name whatever comes before or follows it (:func:`_named_end`); a code too ("t3", "ZM2",
    # "may1990", "ITS12": initials or a birth date and digits, whatever a grade's or a field's code is like)
    if not _typed_keeps(local) and not _PLACE_SHORT.fullmatch(local):
        named_end = _named_end(full, text, start, typed, plain_at and local.isalpha() and local.islower())
        if named_end:
            return f"{_withheld(local)}{_GLUED_LABELS.sub('', full[named_end:], count=1)}"
    # a domain that only those rules read ("at Akkar. UK funded", "at Akkar. Gov staff", "at Mt. Lebanon.De
    # facto"): no address, but after a word the text names an address ("Please email the report to the
    # dashboard at example. Gov")
    if match.group("spec") is not None:
        if known and _names_an_address(local, text, match.start(), start + known):
            return f"{EMAIL_WITHHELD}{_GLUED_LABELS.sub('', full[known:], count=1)}"
        return match.group(0)
    # every other rule reads the domain without the endings after it, which stay as they are written
    as_written = match.group(0)[: match.end("domain") - match.start()]
    replaced = _written_rest(match, typed, links, link_rule, as_written)
    if replaced.startswith(EMAIL_WITHHELD):
        replaced = f"{_withheld(local)}{replaced[len(EMAIL_WITHHELD) :]}"
    return f"{replaced}{tail}"


def _written_rest(match: re.Match, typed: bool, links: bool, link_rule: bool, as_written: str) -> str:
    """What a :data:`WRITTEN_AT` match (``as_written``: without the endings after its domain) is replaced by
    when the written spec's rules leave it to the rules written before it (see :func:`written_out`)."""
    domain, local, at = match.group("domain"), match.group("local"), match.group("at")
    if _sentence_point(
        domain,
        whole=False,
        at=at,
        local=local,
        text=match.string,
        start=match.start(),
        end=match.end("domain"),
    ):
        return as_written
    text, start = match.string, match.start("domain")
    plain_at = bool(_PLAIN_AT.fullmatch(at))
    name_like = shaped(local) or not plain_at
    record_shaped = _record_shaped(local) or not plain_at
    caps = local.isupper() and "AT" in at

    @functools.cache
    def writes_to() -> bool:
        return _writes_to(text, match.start())

    role = _role(local, text, match.start(), writes_to, match.end("domain"), domain)
    common = not role and (_typed_keeps(local) if typed else keeps(local))
    code = bool(_NOT_A_NAME.fullmatch(local))
    coded = _record_shaped(local) and _coded(local)
    odd_at = not (plain_at or at.strip() in _AT_SIGNS)
    strong = (_record_shaped(local) and not coded and not _PLACE_SHORT.fullmatch(local)) or odd_at
    # in what a person typed, a word that may name someone: what a sentence says of an address may follow it
    names = typed and not common and not code

    def follows(stop: int) -> bool:
        return _follows_address(text, start + stop, writes_to, names)

    def addressed(stop: int) -> bool:
        # a word that may name someone, written to, before what follows an address: in a record's text a
        # plain word, in any text a code before a domain no sentence writes ("write to KG2 at example.it")
        if not plain_at or common or not writes_to() or not follows(stop):
            return False
        return _foreign(domain[:stop]) if code else not typed

    def foreign(stop: int) -> bool:
        # a name-like word (in capitals, with a digit, "_" or a full stop, a word that may name someone in
        # what a person typed, or before an at sign) before a domain no sentence writes ("x2 at example. fr
        # sent the report", "x @ example .it if needed"; "Training @ Halba. Info session", "Mar2024 at Akkar.
        # NGO" stay)
        name_like = caps or coded or local.isupper() or record_shaped or names or at.strip() in _AT_SIGNS
        return name_like and not common and not code and _foreign(domain[:stop])

    @functools.cache
    def named_here(stop: int) -> bool:
        # the text writes to the word before "at" or names it an address, whatever word it is ("write to
        # monitoring at example. net", "To: registration at example. de", "email zfakemira at example. net")
        if code:
            return False
        if _names_an_address(local, text, match.start(), start + stop):
            return True
        return not keeps(local) and writes_to()

    def spaced_foreign(stop: int, label: str) -> bool:
        # after a dot and a space, as :func:`foreign`, whatever word comes before the name ("is zfakemira AT
        # example. Info correct?", "x2 at example. fr sent the report"; "Volunteers worked at night. Net
        # enrolment rose" stays: "night" is a sentence's word)
        if not foreign(stop):
            # in a record's text, a capitalised word that may name someone before a domain whose words name
            # no place and no sentence writes ("Sent by Zayna at example. it if needed", "Zfakemira AT example
            # . me")
            return (
                not typed
                and plain_at
                and local[:1].isupper()
                and not common
                and not code
                and _foreign(domain[:stop])
                and not _place_domain(domain[:stop])
            )
        return True

    def spaced_named(stop: int, label: str) -> bool:
        pos = start + stop
        # ". Org", ". Lb" after a name-like word, before a domain whose words name no place ("x2 at example.
        # Lb for the files")
        if label in NAMED_SPACED and foreign(stop) and not _place_domain(domain[:stop]):
            return True
        if not named_here(stop):
            return False
        # a word the text writes to or names an address: any ending, but a capitalised one that a sentence
        # goes on from ("Contact the school at Riverside. Net enrolment is low"); before a place's name
        # ". Org", ". Lb" and ". Com" (:data:`NAMED_SPACED`: "Write to the school at Minyara. Org for the
        # files"), any other only before what follows an address, never before a capitalised word that opens
        # a sentence ("We contacted the school at Bebnine. It was closed", "Send it to the team at Halba.
        # Info: 40 kits" stay)
        if label in NAMED_SPACED and _place_domain(domain[:stop]):
            return True
        opens = label[:1].isupper() and _sentence_goes_on(text, pos)
        if _place_domain(domain[:stop]):
            return follows(stop) and not opens and not (label[:1].isupper() and text.startswith(":", pos))
        return not opens

    def spaced(stop: int, label: str) -> bool:
        goes_on = functools.partial(_sentence_goes_on, text, start + stop)
        counts = _spaced_counts(local, at, typed, goes_on)
        if counts is not None:
            return counts
        folded = fold(label)
        if folded == "eg" and _eg_goes_on(text, start + stop, writes_to):  # "at schools. eg, latrines"
            return False
        if label in SPACED_WORDS:  # ". it", ". me": no word that writes to the name makes them an ending
            return _follows_address(text, start + stop, lambda: False, names)
        # a capitalised word before a colon opens a sentence ("at dawn. Info: 40 kits were delivered")
        if label[:1].isupper() and text.startswith(":", start + stop):
            return False
        return follows(stop)

    def mixed(stop: int) -> bool:
        word = _next_word(text, start + stop)
        return word is not None and any(c.islower() for c in word)

    reading = _Reading(
        typed=typed,
        shaped=typed or record_shaped,
        plain=typed and not name_like,
        named=record_shaped or caps or names,
        strong=strong,
        coded=coded,
        addressy=strong or coded,
        caps=caps,
        capitals=local.isupper() and not code,
        common=common,
        spaced=spaced,
        follows=follows,
        written_to=writes_to,
        addressed=addressed,
        mixed=mixed,
        foreign=foreign,
        spaced_foreign=spaced_foreign,
        spaced_named=spaced_named,
        foreign_any=lambda stop: not code and _foreign(domain[:stop]),
    )
    # no link rule reads the text after this one (what a person typed is read here; a document sent whole and
    # a KM Database's download are read by no link rule): a web address after a word that names no one goes
    # here, as a link, or stays
    here = typed or not link_rule or not links
    end = _domain_end(domain, reading)
    # a code before a domain whose words no sentence writes ("How many G5 at example.it", "team1 at example.
    # org before June", "KG2 at zaynafakemira.no teachers came"): an address's name, as the pushed rules read
    # it, whatever follows; not before a sentence after a dot and a space ("KG2 at night. Net enrolment rose")
    if code and plain_at and _code_address(domain, end, text, start):
        return f"{EMAIL_WITHHELD}{_GLUED_LABELS.sub('', domain[end or len(domain) :], count=1)}"
    short = _PLACE_SHORT.match(domain)
    if not end and short and names:
        # in what a person typed, a word that may name someone, in any case and after any "at", before a
        # place's first word written short and glued to its next ("Met Rania at St.Joseph.", "met zayna at
        # st.joseph.", "ZM2 arobase st.joseph"): ambiguous with an address, so it goes with them, as b40ead2
        # read it ("Classes at St.Joseph" stays: a word that names no one)
        return f"{EMAIL_WITHHELD}{domain[short.end() :]}"
    if not end:
        # a code's web address that the link rule takes ("How many Session2 at SCHOOL.Net"): as a link
        whole = len(domain)
        if here and code and plain_at and _link_rules(domain, text, start + whole, lambda: follows(whole)):
            return f"{local}{at}{LINK_WITHHELD}"
        return as_written
    rest = domain[end:]
    # what is glued after the domain goes with it (".it", ".SHOW")
    glued = _GLUED_LABELS.sub("", rest, count=1)
    spelled = _spelled(domain[:end])

    def link() -> bool:
        return _a_link(domain[:end], text, start + end, lambda: follows(end))

    # a word that names no one, but that the text writes to or calls an address ("write to monitoring at
    # example.org", "cc partners at example.org", "From: registration at example.org", "Is info at example.org
    # the right address?"), before a web address's domain: the address's name
    if (
        common
        and plain_at
        and keeps(local)
        and (spelled or link())
        and _keeps_named(local, text, match.start(), start + end, typed, domain[:end])
    ):
        return f"{EMAIL_WITHHELD}{glued}"
    # a page's word after "to the" in a KM Database's text, which no link rule reads after (``links`` false):
    # its web address goes here as a link ("How do I submit my report to the portal at [link withheld]?")
    if (
        not links
        and common
        and plain_at
        and (spelled or link())
        and _goes_to_page(local, text, match.start(), start + end)
    ):
        return f"{local}{at}{LINK_WITHHELD}{glued}"
    # a code names no one ("How many G5 at Tyre.Gov", "F09 at Example.Org"): its web address goes as a link
    # where no link rule follows, as after a common word; written to before a domain no sentence writes, or
    # with its dot spelled out, it is an address's name ("write to KG2 at example.it for the files")
    code_link = code and here and not spelled and not addressed(end)
    if (typed or links) and plain_at and (common or code_link):
        if here and not spelled and not link():
            return as_written
        if (
            here
            or spelled
            or rest != glued
            or not _left_to_links(domain[:end], text, start + end, lambda: follows(end))
        ):
            return f"{local}{at}{LINK_WITHHELD}{glued}"
        return as_written
    if spelled or role or named(local, at, typed) or addressed(end):
        return f"{EMAIL_WITHHELD}{glued}"
    if (
        plain_at
        and not common
        and not code
        and _foreign(domain[:end])
        and not _left_to_links(domain[:end], text, start + end, lambda: follows(end))
    ):
        # a plain word in a record's text before a domain whose words no sentence writes, that no link rule
        # takes ("Rania at example.it has the files"): an address's name
        return f"{EMAIL_WITHHELD}{glued}"
    if code and plain_at and link():
        # a code's web address in a record's text or a KM Database's download: left to the link rule when it
        # takes it, else withheld here as a link ("F09 at [link withheld]", "KG2 at [link withheld] staff")
        if (
            not here
            and rest == glued
            and _left_to_links(domain[:end], text, start + end, lambda: follows(end))
        ):
            return as_written
        return f"{local}{at}{LINK_WITHHELD}{glued}"
    return as_written


def _code_address(domain: str, end: int, text: str, start: int) -> bool:
    """Whether a code before "at" and ``domain`` (written at ``start`` of ``text``; ``end``: where its ending
    is, 0 when none is) is an address's name: the domain's words before its ending are no sentence's
    (:func:`_foreign`: "example", "zaynafakemira", "mail.example"), and no sentence goes on after a dot and a
    space ("KG2 at night. Net enrolment rose", "Visit2 at dawn. Info sessions followed" stay)."""
    scope = domain[:end] if end else domain
    if not _foreign(scope):
        return False
    seps = list(_LABEL_SEP.finditer(scope))
    if not seps or not _SPACED_SEP.fullmatch(seps[-1].group()):
        return True
    label = scope[seps[-1].end() :]
    starts = label[:1].isupper() or label in SPACED_STARTERS or fold(label) in WEAK_ENDINGS
    return not (starts and _sentence_goes_on(text, start + len(scope)))


def _link_rules(domain: str, text: str, end: int, follows: Callable[[], bool]) -> bool:
    """Whether ``domain`` is a web address (:func:`_a_link`) that the link rule takes (:func:`_left_to_links`:
    "Example.Org", "Tyre.Net", "unicef.org"; not "Halba.Fr", "Tyre. gov")."""
    return _a_link(domain, text, end, follows) and _left_to_links(domain, text, end, follows)


# what is glued after an address's domain withheld (".It", ".me", ".SHOW"): part of it ("x at example.co.It",
# "x at mail.example.me", "x.y at example.org.It was")
_GLUED_LABELS = re.compile(r"^(?:\.[^\W\d_]{2,24}(?![\w-]))+")
# the same left after the placeholder, past where a pattern could read the domain ("x
# @pytest.mark.parts_may_fail" read up to "mark": "[email withheld].parts_may_fail")
_GLUED_AFTER = re.compile(rf"(?<={re.escape(EMAIL_WITHHELD)})(?:\.(?=[\w-]*[^\W\d_])[\w-]+)+")
# a domain's ending left after an address withheld, after a dot spelled out or a space and a dot (a pattern
# read the domain up to an earlier label: "x@Mt.Lebanon dot NET", "x (at) moph point gov point GOV", "x at
# St.Joseph . uk")
_ENDING_AFTER = re.compile(
    rf"(?<={re.escape(EMAIL_WITHHELD)})(?:(?:{_DOT}|\s+(?:point|period)\s+|\s\.\s?|\.\s)(?:{_KNOWN})(?![\w-]))+",
    re.IGNORECASE,
)
# after a link withheld, the labels of its domain the link rule left ("[link withheld].it was closed" from
# "mail.example.it", "more info at [link withheld].it" from "example.org.it")
_GLUED_AFTER_LINK = re.compile(rf"(?<={re.escape(LINK_WITHHELD)})(?:\.[^\W\d_]{{2,24}}(?![\w-]))+")


def _settled(text: str) -> str:
    """``text`` with the title and the given name before an address withheld that is a person's withheld with
    it (:data:`_PERSON`, :func:`_person_before`), and a domain's ending left after an address withheld, a dot
    spelled out or a space before the dot (:data:`_ENDING_AFTER`: "x@Mt.Lebanon dot NET", "x arobase health
    point gov point Co")."""
    if _PERSON in text:
        text = _BEFORE_PERSON.sub(_person_before, text).replace(_PERSON, "")
    return _ENDING_AFTER.sub("", text) if EMAIL_WITHHELD in text else text


def glued_after(text: str) -> str:
    """``text`` without what is glued after an address or a link withheld (:data:`_GLUED_AFTER`,
    :data:`_GLUED_AFTER_LINK`), and a domain's ending after an address withheld, a dot spelled out or a space
    before the dot (:func:`_settled`)."""
    text = _settled(text)
    if f"{EMAIL_WITHHELD}." in text:
        text = _GLUED_AFTER.sub("", text)
    return _GLUED_AFTER_LINK.sub("", text) if f"{LINK_WITHHELD}." in text else text


# the mailboxes of a role, office or service ("info", "data", "office", "contact"): an address's name before
# "at" and a domain when a word that writes to one comes before them ("write to info at example.org", "send it
# to data at example.sy", "contact info at example.sy") or they open the text ("data at example.online"); a
# word of the page after another word ("for more info at [link withheld]", "the data at [link withheld]")
ROLE_NAMES = frozenset(
    {"info", "data", "office", "contact", "contacts", "admin", "support", "hello", "enquiries", "inquiries"}
    | {"enquiry", "inquiry", "mail", "press", "media", "hr", "jobs", "careers", "secretariat", "registry"}
    | {"helpdesk", "noreply", "reception", "accounts", "finance", "procurement", "webmaster", "postmaster"}
    | {"communications", "partnerships"}
)
_OPENS = " \t\n\"'([{:;,-\u2013\u2014\u2018\u201c"


def _role(
    local: str,
    text: str,
    start: int,
    writes_to: Callable[[], bool] | None = None,
    end: int | None = None,
    domain: str = "",
) -> bool:
    """Whether ``local``, written at ``start`` of ``text`` before "at", is a role's mailbox
    (:data:`ROLE_NAMES`): written to, opening the text, or a contact list's item after a phone number ("Tel
    01 234 567 / office at example.org"); before a domain whose words no sentence writes (``domain``,
    :func:`_foreign`), opening a sentence too ("Questions. data at example.org"), or before what follows an
    address ("For more office at example dot org regarding the data"); or that the text names an address
    (:func:`_names_an_address`; ``end``: where the domain ends)."""
    if fold(local) not in ROLE_NAMES:
        return False
    before = text[:start]
    if not before.strip(_OPENS) or _AFTER_PHONE.search(before):
        return True
    if writes_to() if writes_to is not None else _writes_to(text, start):
        return True
    if not _foreign(domain):
        return _names_an_address(local, text, start, end)
    if _CLAUSE_OPENS.search(before):  # "Questions. data at example.org"
        return True
    if end is not None and not _determined(text, start):  # "For more office at example dot org regarding"
        after = _next_word(text, end)
        if after is not None and fold(after) in _FOLLOWS_A_NAME:
            return True
    return _names_an_address(local, text, start, end)


# what makes the word before "at" an address's name, whatever word it is (a common word, a page's or a
# measure's: "write to monitoring at example.org", "cc partners at example.org"): an e-mail's header before it
# ("To:", "From:", "Cc:", "Sender:", "Reply-to:", "Email address:", "Mailbox:", "Mailboxes:"), a sentence that
# calls it an address ("The address is data at ...", "the focal point is monitoring at ...", "The focal points
# are ...", "The mailbox info at ...", "the new address will be info at ..."), a sentence that writes to it or
# hears from it ("Get in touch with partners at ...", "heard back from status at ...", "a reply from partners
# at ...", "The report came from partners at ...", "Please share the list with partners at ...", "Who reads
# monitoring at ...?"), or "the right address" after its domain ("Is info at example.org the right
# address?"); a contact list's item after a phone number ("Tel 01 234 567 / office at ...", "Phone 01 234 567
# - monitoring at ...")
_HEADER_BEFORE = re.compile(
    r"(?:^|[^\w-])(?i:to|from|cc|bcc|sender|reply-?to|e-?mails?(?:\s+address(?:es)?)?|address(?:es)?"
    r"|mailbox(?:es)?|inbox(?:es)?|recipients?|contacts?|focal\s+points?)\s*:\s*$"
)
_NAMED_BEFORE = re.compile(
    r"\b(?i:e-?mails?|address(?:es)?|mailbox(?:es)?|inbox(?:es)?|focal\s+points?|contacts?(?:\s+persons?)?)\b"
    r"(?:\s+[\w'-]+){0,2}?\s+(?i:is|was|are|were|will\s+be|would\s+be|becomes|become|remains|remain)\s*$"
    r"|\b(?i:mailbox|inbox|address|e-?mail\s+address)\s*$"
    # "Questions? data at ...", "For questions, info at ..."
    r"|\b(?i:questions?|queries|enquiries|inquiries|comments|feedback)\s*[?:,]\s*$"
    r"|\b(?i:get|got|gets|getting|keep|keeps|kept|stay|stays|be|been|are)\s+in\s+(?i:touch|contact)\s+with\s+$"
    r"|\b(?i:hear|hears|heard|hearing)\s+(?i:back\s+)?from\s+$"
    r"|\b(?i:reply|replies|response|responses|answer|answers|message|messages|e-?mails?|mails?|note|notes"
    r"|word|news|letter|letters|invitation|update|updates)\s+from\s+$"
    r"|\b(?i:came|come|comes|coming|arrived|arrives|sent|received)\s+from\s+$"
    r"|\b(?i:share|shares|shared|sharing|check|checks|checked|checking|consult|consults|consulted"
    r"|coordinate|coordinated|follow\s+up|followed\s+up|touch\s+base|speak|spoke|talk|talked)\b"
    r"(?:\s+[\w'-]+){0,3}?\s+with\s+$"
    r"|\b(?i:who|whoever)\s+(?i:reads|read|checks|monitors|manages|answers|handles|owns|receives|gets|opens"
    r"|replies\s+to|responds\s+to)\s+$"
)
_ADDRESS_AFTER = re.compile(
    r"\s*,?\s+(?i:(?:is|was|remains)\s+)?(?i:still\s+)?(?i:the|my|our|your|his|her|their|a|an)\s+"
    r"(?i:(?:right|correct|new|old|official|best|main|wrong|only|usual|generic|shared)\s+)?(?i:e-?mail\s+)?"
    r"(?i:address|mailbox|inbox|contact|focal\s+point|contact\s+person)\b"
)
# a question about the address itself: "Is partners at example.org correct?", "Is stats at example.org still
# active?" ("Is" right before the word, the state right after the domain)
_ASKED_BEFORE = re.compile(r"(?:^|[.?!;:]\s*|\b(?i:and|but|so|then)\s+)(?i:is|was|are|were)\s+$")
_STATE_AFTER = re.compile(
    r"\s+(?i:still\s+|really\s+|actually\s+)?(?i:correct|right|valid|active|working|in\s+use|current|up\s+to"
    r"\s+date|monitored|checked|reachable|wrong|outdated|deactivated|disabled|bouncing)\b"
)
_AFTER_PHONE = re.compile(
    rf"(?:\d[\d \t./-]{{5,}}\d|{re.escape(PHONE_WITHHELD)})\s*(?:[/,;|]|[-–—](?=\s))\s*$"
)
# "to" writes to a name only after a verb that sends ("send it to partners at ...", "forward the forms to
# registration at ..."), or after what goes to a mailbox ("Questions go to monitoring at ...", "Responses
# should go to cases at ..."); "Go to registration at [link withheld]" is a page
_SENDS_BEFORE_TO = re.compile(
    r"\b(?i:send|sends|sent|sending|forward|forwards|forwarded|forwarding|write|writes|wrote|writing|written"
    r"|reply|replies|replied|mail|mails|mailed|e-?mail|e-?mails|e-?mailed|submit|submits|submitted|return"
    r"|returned|report|reported|address|addressed|deliver|delivers|delivered|direct|directed|copy|copied"
    r"|cc|bcc|talk|speak|reach)\b(?:\s+[\w'-]+){0,3}\s+$"
    r"|\b(?i:questions?|queries|query|requests?|responses?|replies|reply|answers?|applications?|forms?"
    r"|submissions?|complaints?|comments?|feedback|enquiry|enquiries|inquiry|inquiries|invoices?|messages?"
    r"|e-?mails?|mails?|letters?|copies|documents?|reports?|cvs?|everything|all|it|this|these|they)"
    r"\s+(?:(?:should|must|will|can|may|shall|would|could|need\s+to|needs\s+to|have\s+to|has\s+to|are\s+to"
    r"|is\s+to)\s+)?(?:(?:all|only|directly|straight|then)\s+)?(?i:go|goes|went|going)"
    r"\s+(?:(?:only|directly|straight)\s+)?$"
)
# the persons a sentence writes to, never an address's name ("Contact us at [link withheld]", "email me at";
# "Contact IT at" names the IT mailbox)
_OBJECTS = frozenset({"us", "me", "him", "her", "them", "you", "it", "we", "i"})
# the words between a word that writes to a mailbox and its name ("email the office at ...", "Contact our
# team at ...")
_THE = frozenset({"the", "our", "your", "their", "this", "that"})
# the words of :data:`WRITES_TO` that take the mailbox as their object ("contact", "cc", "email"; "write",
# "forward" and "reply" only with "to": "Who wrote the profile at [link withheld]?")
_MAILS = WRITES_TO - {"write", "writing", "wrote", "forward", "reply", "to"}
# a page's words, which "to the" goes to without writing to them after a verb that goes to a page
# (:data:`_TO_PAGE`: "How do I submit my report to the portal at [link withheld]?", "How do I return to the
# dashboard at [link withheld]?"; "email the portal at ...", "Please email the report to the dashboard at
# ...", "Reply to the survey at ... before June" are a mailbox)
_PAGE_WORDS = frozenset(
    {"portal", "portals", "dashboard", "dashboards", "form", "forms", "platform", "platforms", "survey"}
    | {"surveys", "page", "pages", "site", "sites", "website", "websites", "system", "systems", "link"}
    | {"links", "tool", "tools"}
)
# the verbs that go to a page with "to the" ("submit my report to the portal at", "return to the dashboard
# at", "Teachers report to the platform at"), and those that mail, which make a page's word a mailbox
# wherever they stand in the sentence before it ("Please email the report to the dashboard at", "Forward it
# to the tool at", "Send it to the site at", "Reply to the survey at")
_TO_PAGE = re.compile(
    r"\b(?i:submit|submits|submitted|submitting|upload|uploads|uploaded|uploading|post|posts|posted|posting"
    r"|return|returns|returned|returning|report|reports|reported|reporting|log\s+in|logs\s+in|logged\s+in"
    r"|logging\s+in|sign\s+in|signs\s+in|signed\s+in|signing\s+in|navigate|navigates|navigated|navigating)\b"
    r"(?:\s+[\w'-]+){0,6}\s+$"  # what goes there between them ("the forms for this month", "all the sheets")
)
_MAILS_IN_SENTENCE = re.compile(
    r"\b(?i:send|sends|sent|sending|forward|forwards|forwarded|forwarding|write|writes|wrote|writing|written"
    r"|reply|replies|replied|replying|mail|mails|mailed|mailing|e-?mail|e-?mails|e-?mailed|e-?mailing|cc|bcc"
    r"|copy|copies|copied|copying|deliver|delivers|delivered|address|addressed|direct|directed|talk|speak"
    r"|reach|message|messaged|contact|contacted)\b"
)
_SENTENCE_BREAK = re.compile(r"[.?!;:]\s")
# the verbs that write to a word right after them, without "to" or "the" ("Write partners at ...", "I wrote
# monitoring at ... yesterday"), that copy it on a message ("Copy partners at ... on the reply"; "Copy the
# table at ..." is a page) and that add it to a list or take it off ("Please add monitoring at ... to the
# mailing list")
_WRITES = frozenset({"write", "writes", "wrote"})
_COPIES = frozenset({"copy", "copies", "copied", "copying"})
_LISTS = frozenset(
    {"add", "adds", "added", "adding", "remove", "removes", "removed", "removing", "include", "includes"}
    | {"included", "including", "subscribe", "subscribed", "unsubscribe", "unsubscribed", "drop", "dropped"}
    | {"delete", "deleted", "put", "keep", "kept", "invite", "invited"}
)
_LIST_AFTER = re.compile(
    r"\s+(?i:to|from|on|in|onto|off|into)\s+(?:(?i:the|our|your|this|that|my|a|its|their)\s+)?"
    r"(?:[\w-]+\s+){0,2}?(?i:list|lists|group|groups|loop|thread|threads|distribution|listserv|chain|cc)\b"
)
# a list's join before an item ("To: x at example.org, y at example.org and z at example.org", "Zayna (x at
# example.org), y at ...", "x at example.org / y at ..."), and an address that ends right before it: an
# address withheld, or a word, "at" and a domain written out, its dots with a space too ("To: rania at
# example. org, monitoring at example.org") or spelled out
_LIST_JOIN = re.compile(
    r"\)?(?:\s*[,;/&|]\s*(?:(?:and|or|cc)\s+)?|\s+(?:and|or|and/or|cc)\s+)$", re.IGNORECASE
)
_ITEM = re.compile(
    rf"(?<![\w.+'-]){_LOCAL_PART}{_AT_ANY}(?P<domain>[\w-]+(?:{_SPEC_SEP}[\w-]+)+)$"
    rf"|{re.escape(EMAIL_WITHHELD)}$",
    re.IGNORECASE,
)
# the next item of a list, right after an address's domain ("registration at example.org / Zayna at example.
# Org"), and "the" before an item ("cc monitoring at example.org and the portal at example.org")
_NEXT_ITEM = re.compile(
    r"\)?(?:\s*[,;/&|]\s*(?:(?:and|or|cc)\s+)?|\s+(?:and|or|and/or|cc)\s+)(?:(?:the|our|your|their)\s+)?"
    rf"{_LOCAL_PART}{_AT_ANY}(?P<domain>[\w-]+(?:{_SPEC_SEP}[\w-]+)+)",
    re.IGNORECASE,
)
# where an item may start before its name: right there, or before up to three words ("the monitoring team at",
# "the M&E focal point at")
_ITEM_WORDS = re.compile(r"(?=(?:[^\W\d_][\w'&-]*\s+){0,3}$)")
# "to the" and up to three words before a name, after a verb that sends ("Email it to the Zayna Fakemira
# portal at ...")
_TO_THE_WORDS = re.compile(r"\b(?i:to)\s+(?i:the|our|your|their)\s+(?P<words>(?:[\w'-]+\s+){1,3})$")
_WORDS_BEFORE_BRACKET = re.compile(r"((?:[^\W\d_][\w'-]*\s+){1,3})\(\s*$")


def _names_an_address(local: str, text: str, start: int, end: int | None = None, depth: int = 0) -> bool:
    """Whether ``text`` makes ``local`` (written at ``start`` before "at", the domain ending at ``end``) an
    address's name, whatever word it is: written to (:data:`_MAILS`, "the" or "the data" between them:
    "email the office at"; "to" after a verb that sends or what goes to a mailbox, never a page's word after
    "to the": :data:`_PAGE_WORDS`; "write", "copy", "add ... to the list" right before it), after an
    e-mail's header, in a sentence that calls it an address, writes to it or hears from it, before "the right
    address" or "was the focal point", asked about ("Is partners at example.org correct?"), a contact list's
    item after a phone number (see :data:`_HEADER_BEFORE`), or any item of a list whose first item is one of
    these (:data:`_LIST_JOIN`: "To: monitoring at example.org, partners at example.org"). Never a person a
    sentence writes to ("Contact us at", "email me at"; "Contact IT at" and "Contact it at" name the IT
    mailbox)."""
    before = text[max(0, start - 120) : start]
    if _HEADER_BEFORE.search(before) or _NAMED_BEFORE.search(before) or _AFTER_PHONE.search(before):
        return True
    if end is not None and _ADDRESS_AFTER.match(text, end):
        return True
    if end is not None and _ASKED_BEFORE.search(before) and _STATE_AFTER.match(text, end):
        return True
    word = _WORD_BEFORE.search(before)
    if fold(local) in _OBJECTS and local != "IT":
        # "it" right after a word that mails is the IT mailbox typed in lower case ("Contact it at example.org
        # for access"); any other is a person written to ("Contact us at", "email me at")
        if not (fold(local) == "it" and word and fold(word.group(1)) in _MAILS):
            return False
    skipped = False
    if word and fold(word.group(1)) not in _THE:  # "reach the data team at ...": a word after "the"
        previous = _WORD_BEFORE.search(before, 0, word.start(1))
        if previous and fold(previous.group(1)) in _THE:
            word, skipped = previous, True
    if word and fold(word.group(1)) in _THE:  # "email the office at ...", "Contact the team at ..."
        word, skipped = _WORD_BEFORE.search(before, 0, word.start(1)), True
    if word:
        folded = fold(word.group(1))
        if folded == "to":
            if skipped and _to_page(local, text, start, end, before, word.start(1)):
                return False
            if _SENDS_BEFORE_TO.search(before, 0, word.start(1)):
                return True
        elif folded in _MAILS:
            return True
        elif not skipped and (
            folded in _WRITES
            or folded in _COPIES
            or (end is not None and folded in _LISTS and _LIST_AFTER.match(text, end))
        ):
            return True
    if depth < 10 and _list_item_named(text, start, depth, end, local):
        return True
    # "Email it to the Zayna Fakemira portal at ...": a verb that mails, "to the" and up to three words before
    # the name
    to_the = _TO_THE_WORDS.search(before)
    if not to_the or any(fold(word) in ENGLISH_STOP for word in to_the.group("words").split()):
        return False  # "Talk to the team about the guide at ...": no word of grammar between them
    return bool(_SENDS_BEFORE_TO.search(before, 0, to_the.start())) and bool(
        _MAILS_IN_SENTENCE.search(_SENTENCE_BREAK.split(before[: to_the.start()])[-1])
    )


def _to_page(local: str, text: str, start: int, end: int | None, before: str, to: int) -> bool:
    """Whether ``local``, a page's word after "to the" (``to``: where "to" is in ``before``, the text before
    ``start``), is a page, not a mailbox: a verb that goes to a page comes before "to" (:data:`_TO_PAGE`:
    "submit my report to the portal at"), no verb that mails stands in the sentence before it
    (:data:`_MAILS_IN_SENTENCE`: "Please email the report to the dashboard at"), and no word that follows an
    address comes after its domain (:data:`ADDRESS_FOLLOWS`: "Submit it to the portal at ... before June")."""
    if fold(local) not in _PAGE_WORDS or not _TO_PAGE.search(before, 0, to):
        return False
    sentence = _SENTENCE_BREAK.split(before[:to])[-1]
    if _MAILS_IN_SENTENCE.search(sentence):
        return False
    after = _next_word(text, end) if end is not None else None
    return after is None or fold(after) not in ADDRESS_FOLLOWS


def _ends_as_address(domain: str) -> bool:
    """Whether ``domain``, a list item's after "at", holds an ending of a domain after its first word
    (:data:`KNOWN_ENDINGS`: "example. org", "example.org", "example dot org")."""
    labels = [label for label in _LABEL_SEP.split(domain) if label]
    return any(fold(label) in KNOWN_ENDINGS for label in labels[1:])


# "to the", and one word between "the" and a page's word ("to the new portal", "to the school portal")
_TO_THE_PAGE = re.compile(r"\b(?i:to)\s+(?i:the|our|your|their|this|that)\s+(?:[^\W\d_][\w'-]*\s+)?$")


def _page_shape(local: str, text: str, start: int) -> bool:
    """Whether ``local``, written at ``start`` of ``text``, is a page's word after "to the" (one word between
    them too) and a verb that goes to a page, in a sentence with no verb that mails (the written spec's E3:
    "Report to the new portal at ...", "How do I submit my report to the portal at ..."; "Please email the
    report to the dashboard at ..." is a mailbox), whatever follows."""
    before = text[max(0, start - 120) : start]
    to = _TO_THE_PAGE.search(before)
    if not to or fold(local) not in _PAGE_WORDS or not _TO_PAGE.search(before, 0, to.start()):
        return False
    return not _MAILS_IN_SENTENCE.search(_SENTENCE_BREAK.split(before[: to.start()])[-1])


def _goes_to_page(local: str, text: str, start: int, end: int | None) -> bool:
    """Whether ``local``, written at ``start`` of ``text`` after "to the" (one word between them too), is a
    page's word that the sentence goes to (:func:`_to_page`: "submit my report to the portal at ...", "Report
    to the new portal at ..."): the cases where :func:`_names_an_address` reads no mailbox, so that its domain
    goes as a link in every text."""
    before = text[max(0, start - 120) : start]
    to = _TO_THE_PAGE.search(before)
    return bool(to) and _to_page(local, text, start, end, before, to.start())


def _list_item_named(text: str, start: int, depth: int, end: int | None = None, local: str = "") -> bool:
    """Whether the item at ``start`` of ``text`` (its domain ending at ``end``) is, in a list
    (:data:`_LIST_JOIN`, "the" before it too), an item of a list of addresses: it follows an address that the
    text writes to or names (:func:`_names_an_address`: every item of "To: x at example.org, y at example.org
    and z at example.org" is an address's; an address in brackets after a name is that name's item: "Contact
    Rania (x at example.org) or partners at example.org"), or an address withheld or written out with an
    ending of a domain (:data:`KNOWN_ENDINGS`), before or after it ("zfakemira at example. org, registration
    at example. org", "registration at example.org / Zayna at example. Org", "cc monitoring at example.org and
    the portal at example.org"), up to two words between the join and its name too ("Contacts: partners at
    example. Org, the M&E focal point at example. Org"), whatever its name ("To: monitoring at example. Org,
    IT at example. Org"); a word of a sentence's grammar is never an item because of the item after it
    ("Guidance is at example.org and ...")."""
    grammar = bool(local) and fold(local) in ENGLISH_STOP
    after = _NEXT_ITEM.match(text, end) if end is not None and not grammar else None
    if after and fold(after.group("local")) not in ENGLISH_STOP and _ends_as_address(after.group("domain")):
        return True
    # up to three words between the join and the item's name ("monitoring team at", "the M&E focal point at")
    join = None
    for words in _ITEM_WORDS.finditer(text, max(0, start - 60), start):
        join = _LIST_JOIN.search(text, max(0, words.start() - 12), words.start())
        if join:
            break
    if not join:
        return False
    head = join.start()
    item = _ITEM.search(text, max(0, head - 160), head)
    if not item:
        return False
    previous = item.group("local") or ""
    if item.group(0) == EMAIL_WITHHELD:
        return True
    domain = item.group("domain") or ""
    if fold(previous) not in ENGLISH_STOP and _ends_as_address(domain):
        return True
    # a list the text writes to, whatever the first item's name ("Reach us at example. io, me at ...")
    if _ends_as_address(domain) and _written_to(text, item.start()):
        return True
    if _names_an_address(previous, text, item.start(), item.end(), depth + 1):
        return True
    if (
        depth < 10
        and _ends_as_address(domain)
        and _list_item_named(text, item.start(), depth + 1, None, previous)
    ):
        return True  # an item of a list whose earlier item is one ("To: x at ..., it at ..., all at ...")
    # "Contact Rania (x at example.org) or ...": the name before the bracket is the item ("Rania", "Zayna
    # Fakemira")
    bracket = _WORDS_BEFORE_BRACKET.search(text, max(0, item.start() - 60), item.start())
    if not bracket:
        return False
    starts = [bracket.start(1) + name.start() for name in re.finditer(r"[^\W\d_][\w'-]*", bracket.group(1))]
    return any(_names_an_address("", text, first, None, depth + 1) for first in reversed(starts))


# what follows an address's domain, not a page's: a word of :data:`ADDRESS_FOLLOWS` ("before June",
# "thanks", "regarding"), or what an address does ("has the files", "holds the data", "sent the report"); "is"
# and "was" only before "the focal point", "the right address" (:data:`_ADDRESS_AFTER`)
_FOLLOWS_A_NAME = ADDRESS_FOLLOWS | {"has", "holds", "sent"}
_CLAUSE_OPENS = re.compile(r"(?:^|[.?!:;]\s+)[\s\"'(\[{\-\u2013\u2014\u2018\u201c]*$")


def _keeps_named(local: str, text: str, start: int, end: int, typed: bool, domain: str) -> bool:
    """Whether ``local``, a word that names no one (:func:`keeps`) written at ``start`` of ``text`` before
    "at" and a web address's domain (``domain``, ending at ``end``), is that address's name: when the text
    writes to it or calls it an address (:func:`_names_an_address`); before a domain whose words no sentence
    writes (:func:`_foreign`; "Attendance at Akkar. com?" is a sentence missing a word), a measure's or a
    report's word that opens a record's text ("Monitoring at example dot org"), and in what a person typed, a
    measure's, a report's or a page's word (no common word of a sentence, no person written to: "us", "me")
    that opens the text or a sentence ("status at example.online", "Questions? monitoring at example.be"), or
    that no determiner makes a thing before what follows an address ("dropout at zaynafakemira.plus before
    June", "For more partners at example. org has the files"; "the data at [link withheld] was updated",
    "Show enrolment at [link withheld] for girls" stay)."""
    if _names_an_address(local, text, start, end):
        return True
    folded = fold(local)
    if folded in ORDINARY or (folded in _OBJECTS and _writes_to(text, start)) or not _foreign(domain):
        return False
    if not typed:  # a record's text: a measure's or a report's word that opens it
        opens = _CLAUSE_OPENS.search(text, 0, start)
        return (folded in MEASURE_WORDS or folded in REPORT_WORDS) and bool(opens)
    if _CLAUSE_OPENS.search(text, 0, start):
        return True
    after = _next_word(text, end)
    return after is not None and fold(after) in _FOLLOWS_A_NAME and not _determined(text, start)


# the one-word spellings of towns that are also family names, as a domain's word after "at" in a sentence
# missing its space ("how many ZM2 at saida.it was closed"): a place there (:func:`_foreign`)
TOWN_WORDS = frozenset(
    "saida sayda naameh qana houla anjar khiam majdal saadnayel akar aakar kousba hasroun qobayat bint "
    "jounieh sour batroun nabatiye nabatiyeh marjayoun hermel zgharta koura".split()
)


# the times of a day and the plain nouns a sentence writes after "at" before its full stop ("Volunteers
# worked at night. Net enrolment rose", "We started at dawn. Info: 40 kits were delivered"): no domain's words
SENTENCE_NOUNS = frozenset(
    "dawn dusk noon midday midnight night nights morning mornings afternoon afternoons evening evenings "
    "sunset sunrise daybreak lunch lunchtime weekend weekends home homes camp risk first once twice end "
    "start".split()
)


def _sentence_label(label: str) -> bool:
    """Whether ``label``, a word of a domain before its ending, is a word a sentence writes: a common or a
    request's word (:data:`KEEPS`, :data:`COMMON_WORDS`, :data:`GOES_ON`), a time of day or a plain noun
    (:data:`SENTENCE_NOUNS`: "night", "dawn", "home"), a place (:data:`MAIN_PLACE_WORDS`,
    :data:`TOWN_WORDS`, a place's first word written short), a code (:data:`_NOT_A_NAME`) or a letter."""
    folded = fold(label)
    return (
        len(folded) < 2
        or folded in _PLACE_FIRST
        or folded in KEEPS
        or folded in SENTENCE_NOUNS
        or folded in COMMON_WORDS
        or folded in GOES_ON
        or folded in MAIN_PLACE_WORDS
        or folded in TOWN_WORDS
        or bool(_NOT_A_NAME.fullmatch(label))
    )


_AT_ANY_RE = re.compile(_AT_ANY, re.IGNORECASE)
_WORD_POINT = re.compile(r"([^\W\d_][\w'-]*)\s+point\s", re.IGNORECASE)


def _sentence_point(
    written: str,
    whole: bool = True,
    at: str | None = None,
    local: str | None = None,
    text: str | None = None,
    start: int = 0,
    end: int | None = None,
) -> bool:
    """Whether "point" in ``written`` (a match, or with ``whole`` false its domain only, written after
    ``local`` and ``at``) is a sentence's word, not a dot spelled out in French: the word before it is a
    sentence's (:func:`_sentence_label`: "at this point co-ordination", "at the point eg the gate", "at
    baseline point net of dropouts", "at school point de vue"; "x arobase example point org" is an address),
    "at" is written as a sentence writes it ("x arobase health point org", "x @ health point org", "x [at]
    training point org" are addresses), the text (``text``, the match written from ``start`` to ``end``)
    neither writes to the word before "at" nor names it an address (:func:`_writes_to`, :func:`_written_to`,
    :func:`_names_an_address`: "Contact monitoring at health point org", "To: data at education point gov
    point lb", "Her email is zayna at the point org" are addresses), and that word names no one
    (:func:`keeps`: "Teachers met at the point de rencontre"). A word that may name someone is never before a
    sentence's "point" ("Zayna at the point de", "Rania at this point eg", "x at health point org" are
    addresses)."""
    if "point" not in written.lower():
        return False
    if whole:
        found = _AT_ANY_RE.search(written)
        if found:
            at, local, written = found.group(), written[: found.start()].strip(), written[found.end() :]
    if at is not None and not _PLAIN_AT.fullmatch(at):
        return False
    points = list(_WORD_POINT.finditer(written))
    if not any(_sentence_label(m.group(1)) for m in points):
        return False
    if local is None:
        return True
    if text is not None and (
        _writes_to(text, start) or _written_to(text, start) or _names_an_address(local, text, start, end)
    ):
        return False
    return keeps(local)


def _foreign(domain: str) -> bool:
    """Whether ``domain`` (up to its ending) holds a word before its ending that no sentence writes
    (:func:`_sentence_label`: "example.it", "zaynafakemira.be"; "school.it", "Halba.No", "akkar.so" are a
    sentence missing its space)."""
    labels = _LABEL_SEP.split(domain)[:-1]
    return any(label and not _sentence_label(label) for label in labels)


# More places' names, written as one word, that other apps register when they start (the KM Database adds
# every governorate, district and town of its list, ``km.places``: "Bebnine", "Minyara", "Chtaura"); a source
# that fails is logged and skipped
PLACE_SOURCES: list[Callable[[], Iterable[str]]] = []
_places: tuple[int, frozenset[str]] = (-1, frozenset())


def place_words() -> frozenset[str]:
    """The places' names of :data:`PLACE_SOURCES`, folded, and "lebanon" (read again when a source joins)."""
    global _places
    if _places[0] != len(PLACE_SOURCES):
        found = {"lebanon"}
        for source in PLACE_SOURCES:
            try:
                found |= {fold(word) for word in source() if word and not re.search(r"\s", word)}
            except Exception:
                name = getattr(source, "__name__", source)
                logger.exception("people: the places of %s could not be read", name)
        _places = (len(PLACE_SOURCES), frozenset(found))
    return _places[1]


def _place_domain(domain: str) -> bool:
    """Whether the words of ``domain`` before its ending are a place's or a sentence's, not an address's: each
    a word a sentence writes (:func:`_sentence_label`, a place's first word written short among them: "Mt.
    Lebanon") or a place another app registers (:func:`place_words`: "Bebnine", "Minyara"); a place's first
    word written short counts only before such a word ("Mt. Example", "St. Joseph" are no place's). Only where
    a word after a dot and a space decides (:func:`written_out`: ``spaced_foreign``, ``spaced_named``): "x at
    bebnine.it" stays an address's domain (:func:`_foreign`)."""
    labels = _LABEL_SEP.split(domain)[:-1]
    places = place_words()
    return all(not label or _sentence_label(label) or fold(label) in places for label in labels)


def _a_link(domain: str, text: str, end: int, follows: Callable[[], bool]) -> bool:
    """Whether ``domain``, written after a word that names no one and "at" in what a person typed, is a web
    address's (a role's mailbox written to is an address's name, :func:`_role`): a known ending
    (:data:`KNOWN_ENDINGS`), "www.", a path, a port or a query after it (``end``, where it ends in ``text``),
    a newer ending (:data:`NEW_ENDINGS`: ".online", ".tech", ".blog"), or a country's of two letters (".sy",
    ".tv"; ".it", ".in", ".be" only when what follows it follows an address, ``follows``: "write to info at
    example.it", "children at school.it was crowded" stays), or any ending after words no sentence writes
    (:func:`_foreign`: "rania-fakemira.it", "mail.example.it", "zaynafakemira.page"); "visits at mt.lebanon",
    "red flags at akkar.district", "visits at halba.bekaa" are places."""
    labels = _LABEL_SEP.split(domain)
    last = fold(labels[-1])
    if last in KNOWN_ENDINGS or fold(domain).startswith("www."):
        return True
    if re.match(r"(?::\d{1,5})?[/?#]|:\d{1,5}(?![\w.])", text[end:]):
        return True
    if fold(labels[0]) in _PLACE_FIRST:
        return False
    if _foreign(domain):  # "rania-fakemira.it", "mail.example.it", "zaynafakemira.page": whatever follows
        return True
    if len(last) == 2 and last not in _NOT_COUNTRY:
        return last not in ORDINARY or follows()
    return last in NEW_ENDINGS


def _left_to_links(domain: str, text: str, end: int, follows: Callable[[], bool]) -> bool:
    """Whether ``domain``, written after a word that names no one and "at" in a record's text, is left to the
    link rule (``redact.LINK``: :data:`LINK_ENDINGS`, :data:`LINK_LOWER`, "www."), or is no web address's at
    all ("visits at akkar.district", "Children at school.it was said": :func:`_a_link`); a known ending the
    link rule does not take (".ngo", ".de", ".jo": "Contact us at example.de"), a newer or a country's
    (".online", ".sy") or a domain with a space ("example .org") is withheld here."""
    last = _LABEL_SEP.split(domain)[-1]
    folded = fold(last)
    if folded not in KNOWN_ENDINGS:
        return not _a_link(domain, text, end, follows)
    if re.search(r"\s", domain):
        return False
    if fold(domain).startswith("www.") or folded in LINK_ENDINGS or last in LINK_LOWER:
        return True
    return folded in LINK_LOWER and text[end : end + 1] == "/"


def _spaced_counts(local: str, at: str, typed: bool, goes_on: Callable[[], bool] = bool) -> bool | None:
    """Whether an ending a sentence may start with after a dot and a space (". net", ". info", ". de", ". eg",
    ". NGO", :data:`SPACED_STARTERS`, :data:`WEAK_ENDINGS`) ends an address's domain: always (True) after an
    address's name with a full stop, "_" or "+" inside (:func:`_record_shaped`), an at sign with no space
    around it after a word that names someone ("x@example. info") or "at" written as no sentence writes it
    ("[at]", "(at)"); after a name whose only address's shape is a digit, unless a word of the sentence goes
    on after it (``goes_on``, :func:`_sentence_goes_on`: "z2fakemira at example. info" at the text's end, "x2
    at example. net for the files"; "Mar2024 at Akkar. NGO staff were present" stays); never (False) after a
    plain word in a record's text, or a word that names no one in what a person typed (:func:`keeps`: "the
    enrolment at Halba. Net or gross?", "the GER at Halba. Net?", "visits at Akkar. NGO staff"); else by what
    follows it (None:
    :func:`_follows_address`), after a plain word in what a person typed ("x at example. net before June",
    "dropout at Halba. info on dropout?" stays), an at sign with a space ("Training @ Halba. Info session to
    follow."), a class, a disease, a field's code or a saint's school ("Class 4B at Tyre. Info sessions",
    "Visit2 at Akkar. NGO staff", "Students from St.Joseph at Tyre. Info sessions"), or a name and "AT" in
    capitals."""
    place = bool(_PLACE_SHORT.fullmatch(local))
    if _record_shaped(local) and not place:
        if re.search(r"[._+]", local):
            return True
        if _PLAIN_AT.fullmatch(at):
            return not goes_on()
    if at in _AT_SIGNS:
        return None if keeps(local) else True
    if at.strip() in _AT_SIGNS:
        return None
    if not _PLAIN_AT.fullmatch(at):
        return True
    if place or shaped(local) or (local.isupper() and "AT" in at):
        return None
    return None if typed and not keeps(local) else False


def _determined(text: str, start: int) -> bool:
    """Whether the word before an address's name (at ``start`` of ``text``) makes it a thing, not a name
    (:data:`DETERMINERS`: "the GER at ...", "which partners at ..."; never before a colon: "one of these: x at
    ...")."""
    before = _WORD_BEFORE.search(text, max(0, start - 40), start)
    return bool(before) and fold(before.group(1)) in DETERMINERS and ":" not in before.group(0)


def _eg_goes_on(text: str, pos: int, writes_to: Callable[[], bool]) -> bool:
    """Whether "eg" ends at ``pos`` of ``text`` as "e.g.," is typed: a mark, then a word that does not follow
    an address ("at schools. eg, latrines", "at schools. eg: latrines"; "x at example. eg, thanks" ends one),
    and no word that writes to an address before its name."""
    after = _EG_GOES_ON.match(text, pos)
    if not after or writes_to():
        return False
    word = fold(after.group(1))
    return word not in ADDRESS_FOLLOWS and word not in GOES_ON


def _sentence_goes_on(text: str, pos: int) -> bool:
    """Whether a word of a sentence comes right after ``pos`` of ``text`` (after an ending a sentence may
    start with: "Akkar. NGO staff", "Halba. Net enrolment"): a word of letters, no common word of a sentence's
    grammar nor of a chat, nor one that follows an address ("x2 at example. net for the files", "... before
    June")."""
    word = _next_word(text, pos)
    if word is None or not word.isalpha():
        return False
    word = fold(word)
    return word not in ENGLISH_STOP and word not in GOES_ON and word not in ADDRESS_FOLLOWS


# The written spec's e-mail rules, read before any other (:func:`written_out`). A domain's ending: a label of
# :data:`KNOWN_ENDINGS` in any case after the domain's first word, after a dot (a space before or after it
# too) or a dot spelled out ("example. Gov", "Halba. Org", "the point de", "health point org")
_LETTERS = re.compile(r"[^\W\d_]{2,24}")
_LINK_TAKES = re.compile(rf"[\w-]+(?:\.[\w-]+)*\.(?:{'|'.join(LINK_ENDINGS)})", re.IGNORECASE)
# the capitalised endings after a dot and a space that a sentence opens with ("Net enrolment", "Info
# sessions", "Org chart", "Lb of rice", "NGO staff"), never ". Com" nor a country's where mailboxes are: after
# a sentence's word and a registered place's name only (:func:`_place_sentence`)
_SENTENCE_STARTS = frozenset({"Org", "Lb", "Net", "Info", "Ngo", "NGO"})
# the capitalised endings after a dot and a space that a record's text keeps after a word in lower case and a
# capitalised domain before a sentence in lower case ("Volunteers worked at Riverside. Net enrolment rose")
_SPACED_CAPITALS = frozenset(
    {"Org", "ORG", "Com", "COM", "Lb", "LB", "Net", "NET", "Info", "INFO", "Ngo", "NGO"}
)
# the spec's closed list of a sentence's verbs and nouns in lower case before "at" and a registered place's
# name: a sentence's words in that one shape only (:func:`_place_sentence`), never words that name no one
SENTENCE_LOCAL = frozenset(
    "trained held resumed delivered present arrived worked based located enrolled conducted organised "
    "organized taught studied distributed opened closed started launched hosted gathered stationed recorded "
    "observed visited met stayed lived employed teachers students classes staff children sessions schools "
    "training".split()
)
# a mailing context before an address's name, up to two words between them ("the", a word that describes it,
# a title, a given name): "write to", "email", "mail", "send (it) to", "cc", "copy", "contact", "reply to",
# "forward (it) to", "reach", "get in touch with", "To:", "Cc:", "Bcc:", "Recipients:", "Contacts:",
# "Mailboxes:", "the address is", "her email is"
_MAILING = (
    r"(?:(?:\b(?:write|writes|writing|reply|replies|replying)\s+to"
    r"|\b(?:send|sends|sending|forward|forwards|forwarding)(?:\s+[\w'-]+){0,3}?\s+to"
    r"|\b(?:e-?mail|e-?mails|e-?mailing|mail|mails|mailing|cc|bcc|copy|contact|contacting|reach)"
    r"|\b(?:get|getting)\s+in\s+touch\s+with"
    r"|\bthe\s+address\s+is|\b(?:her|his|their)\s+e-?mail(?:\s+address)?\s+is"
    r")(?![\w-])\s*:?"  # a colon after it too ("The address is: x at ...", "Email: x at ...")
    # a header, the name right after its colon too ("To:x at ...", "Recipients:the info at ...")
    r"|(?:^|[^\w-])(?:to|cc|bcc|recipients?|contacts?|mailbox(?:es)?)\s*:)\s*"
)
_WRITES_BEFORE = re.compile(
    rf"{_MAILING}(?:(?:[^\W\d_][\w'-]*|(?:mr|mrs|ms|mx|dr|prof)\.|[^\W\d_]\.)\s+){{0,2}}$", re.IGNORECASE
)
# the same, with only "the", a possessive or a title between the mailing context and the word ("Email staff
# at", "Write to the staff at", "Contact Ms. Staff at"): what writes to that word itself
_WRITES_WORD_BEFORE = re.compile(
    rf"{_MAILING}(?:(?:the|our|your|their|his|her|my|this|that|a|an|(?:mr|mrs|ms|mx|dr|prof)\.?)\s+){{0,2}}$",
    re.IGNORECASE,
)


def _written_to(text: str, start: int) -> bool:
    """Whether a mailing context comes before the word at ``start`` of ``text``, up to two words between them
    (:data:`_WRITES_BEFORE`: "Please email Zayna Fakemira at", "Write to the school at", "Contact Ms.
    Fakemira at", "To: monitoring at")."""
    return bool(_WRITES_BEFORE.search(text, max(0, start - 80), start))


def _written_to_word(text: str, start: int) -> bool:
    """Whether a mailing context writes to the word at ``start`` of ``text`` itself, only "the", a possessive
    or a title between them (:data:`_WRITES_WORD_BEFORE`: "Email staff at", "Write to the staff at")."""
    return bool(_WRITES_WORD_BEFORE.search(text, max(0, start - 80), start))


def _known_end(domain: str) -> int:
    """Where the last of ``domain``'s labels after its first that is a domain's ending in any case
    (:data:`KNOWN_ENDINGS`) ends; 0 when none is."""
    seps = list(_LABEL_SEP.finditer(domain))
    for i in range(len(seps) - 1, -1, -1):
        stop = seps[i + 1].start() if i + 1 < len(seps) else len(domain)
        label = domain[seps[i].end() : stop]
        if _LETTERS.fullmatch(label) and fold(label) in KNOWN_ENDINGS:
            return stop
    return 0


def _named_end(domain: str, text: str, start: int, typed: bool, lower: bool) -> int:
    """Where the domain an address may have ends after a word that may name someone (written at ``start`` of
    ``text``; ``lower``: in lower case, of letters only, before "at" as a sentence writes it): after its last
    label that is a domain's ending in any case (:func:`_known_end`: "Rania at example. Gov", "HADDAD at
    Halba. Org replied", "Our Rania at Halba. Org has the files", "Zayna at the point de"), whatever comes
    before the name and whatever follows the ending (". De", ". Fr" too); 0 when none is. Not after a domain
    whose words are a sentence's (:func:`_sentence_words`: "at night. Net enrolment rose"); nor, in a record's
    text after a word in lower case, a capitalised ending of :data:`_SPACED_CAPITALS` after a space and a
    domain whose words are capitalised, before a word in lower case ("worked at Riverside. Net enrolment
    rose": the record rule reads it as before, the written spec's known limit)."""
    seps = list(_LABEL_SEP.finditer(domain))
    for i in range(len(seps) - 1, -1, -1):
        stop = seps[i + 1].start() if i + 1 < len(seps) else len(domain)
        label = domain[seps[i].end() : stop]
        if not _LETTERS.fullmatch(label) or fold(label) not in KNOWN_ENDINGS:
            continue
        if _SPACED_SEP.fullmatch(seps[i].group()):
            if _sentence_words(domain[: seps[i].start()]):
                continue
            if (
                lower
                and not typed
                and label in _SPACED_CAPITALS
                and all(_title(word) for word in _LABEL_SEP.split(domain[: seps[i].start()]))
                and _lower_word(_next_word(text, start + stop))
            ):
                continue
        return stop
    return 0


def _sentence_words(domain: str) -> bool:
    """Whether every word of ``domain`` (before a full stop and a space) is a sentence's and no place's: a
    time of day or a plain noun, a common or a request's word (:func:`_sentence_label`: "at night. Net
    enrolment rose", "at home. Net enrolment rose", "at dawn. Info: 40 kits"); no domain's words then. A
    place's name (a registered one, :data:`MAIN_PLACE_WORDS`, a place's first word written short), a word no
    sentence writes ("example", "Fakemira", "Hotmail") or "point" between two words ("the point de") are a
    domain's."""
    places = place_words() | MAIN_PLACE_WORDS | TOWN_WORDS | _PLACE_FIRST
    labels = _LABEL_SEP.split(domain)
    if any(not _PLAIN_DOT.fullmatch(sep.group()) for sep in _LABEL_SEP.finditer(domain)):
        return False
    return all(label and _sentence_label(label) and fold(label) not in places for label in labels)


def _title(word: str) -> bool:
    """Whether ``word`` is written capitalised, each part of it joined by a dash too ("Minieh-Danniyeh")."""
    return all(
        part.isalpha() and part[:1].isupper() and part[1:] == part[1:].lower() for part in word.split("-")
    )


def _lower_word(word: str | None) -> bool:
    return word is not None and word.isalpha() and word[:1].islower()


def _place_sentence(local: str, at: str, domain: str, text: str, end: int) -> bool:
    """Whether a word in lower case of :data:`SENTENCE_LOCAL`, " at ", ``domain`` (ending at ``end`` of
    ``text``) and what follows read as a sentence (the written spec's E4): a registered Lebanese place's name
    written capitalised (:func:`place_words`, :data:`MAIN_PLACE_WORDS`; after a place's first word written
    short too: "Mt. Lebanon"), a full stop, a space, a capitalised ending a sentence opens with
    (:data:`_SENTENCE_STARTS`) and a word in lower case ("Teachers trained at Bebnine. Net enrolment rose
    after that?", "Kits were stationed at Halba. Lb of rice each"). No other word before "at", no place that
    is not registered ("trained at Fakemira. Net enrolment rose" is an address's shape)."""
    if not (local in SENTENCE_LOCAL and at.strip() == "at" and _PLAIN_AT.fullmatch(at)):
        return False
    seps = list(_LABEL_SEP.finditer(domain))
    if not seps or seps[-1].group() != ". " or domain[seps[-1].end() :] not in _SENTENCE_STARTS:
        return False
    if any(not _PLAIN_DOT.fullmatch(sep.group()) for sep in seps):
        return False
    labels = _LABEL_SEP.split(domain[: seps[-1].start()])
    places = place_words() | MAIN_PLACE_WORDS
    if not all(_title(label) for label in labels) or fold(labels[-1]) not in places:
        return False
    if any(fold(label) not in places and fold(label) not in _PLACE_FIRST for label in labels[:-1]):
        return False
    return _lower_word(_next_word(text, end))


def _next_word(text: str, pos: int) -> str | None:
    """The word right after ``pos`` of ``text`` (after spaces), or None: the text's end, a mark."""
    after = _AFTER_DOMAIN.match(text, pos)
    return after.group(1) if after else None


def _writes_to(text: str, start: int) -> bool:
    """Whether the word before an address's name (at ``start`` of ``text``) writes to it (:data:`WRITES_TO`:
    "contact x at ...", "write to x at ...")."""
    before = _WORD_BEFORE.search(text, max(0, start - 40), start)
    return bool(before) and fold(before.group(1)) in WRITES_TO


def _follows_address(text: str, pos: int, writes_to: Callable[[], bool], named: bool = False) -> bool:
    """Whether what comes after ``pos`` of ``text`` follows an address rather than goes on with a sentence:
    the text's end, a mark, a word of :data:`ADDRESS_FOLLOWS` ("before June", "thanks", "regarding"); after a
    word that writes to an address (``writes_to``: "contact x at example. net about the data"), any common
    word of a sentence or a request too; after a word that may name someone in what a person typed
    (``named``), what a sentence says of an address (:data:`NAMED_FOLLOWS`: "x at example. net is the focal
    point"). "at Halba. net enrolment", "at Halba. Net or gross?" go on with a sentence."""
    after = _AFTER_DOMAIN.match(text, pos)
    if not after:
        return False
    word = after.group(1)
    if word is None:
        return True
    word = fold(word)
    if word in ADDRESS_FOLLOWS or (named and word in NAMED_FOLLOWS):
        return True
    return word in _AFTER_WRITES_TO and writes_to()


def without_paths(text: str) -> str:
    """``text`` without what is left of a web address's path after an address or a link withheld
    (:data:`_PATH_LEFT`)."""
    return _PATH_LEFT.sub(r"\1", text) if "withheld]" in text else text


def withhold_addresses(text: str, typed: bool = False, links: bool = True, link_rule: bool = True) -> str:
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
    link (:func:`withhold_schemed`: "x.y at https://example.org"). ``link_rule`` false (a document sent whole,
    which no link rule reads after): a web address after a word that names no one goes here as a link
    ("Contact us at [link withheld]")."""
    if any(sign in text for sign in _AT_SIGNS):
        text = _settled(_MARKED_ENDING.sub("", EMAIL.sub(_email_withheld, text)))
    if not _SPELLED_AT.search(text):
        return without_paths(text)
    if _SPELLED_HINT.search(text):
        text = SPELLED_EMAIL.sub(
            functools.partial(spelled_out, typed=typed, links=links, link_rule=link_rule), text
        )
        text = _settled(text)  # "x arobase health point gov point Co": the ending left goes with it
    if DOTTED.search(text):
        text = WRITTEN_AT.sub(
            functools.partial(written_out, typed=typed, links=links, link_rule=link_rule), text
        )
    return without_paths(glued_after(text if typed else withhold_schemed(text)))


# a mark after an address withheld whose domain ends in no ending ("x@ST.JOSEPH. ORG", "x@Mt.Lebanon dot
# NET"): the ending after it, a dot and a space or a dot spelled out, goes with it
# (:func:`withhold_addresses`)
_MARK = "\ue000"
_MARKED_ENDING = re.compile(rf"{_MARK}(?:{_SPEC_SEP}(?:{_KNOWN})(?![\w-]))*", re.IGNORECASE)


def _email_withheld(match: re.Match) -> str:
    """:data:`EMAIL_WITHHELD` for an :data:`EMAIL` match (:func:`_withheld`), marked when its domain ends in
    no ending of a domain (:data:`_MARK`)."""
    last = re.split(r"[^\w-]", match.group(0).rstrip())[-1]
    withheld = _withheld(match.group(0))
    return withheld if fold(last) in KNOWN_ENDINGS else f"{withheld}{_MARK}"


# a mark after an address withheld whose name is written as a person's, capitalised: the given name, an
# initial or a title right before it goes with it (:func:`_person_before`: "Write to Mira Vantrell at Minyara.
# Com", "Met Rania HADDAD at Bebnine. Info", "Please email Ms. Fakemira at Halba. Lb")
_PERSON = "\ue001"
_TITLE_WORD = "|".join(sorted(HONORIFICS, key=len, reverse=True))
_BEFORE_PERSON = re.compile(
    rf"(?:(?<![\w.'-])(?P<title>(?i:{_TITLE_WORD}))\.?[ \t]+)?"
    rf"(?:(?<![\w.'-])(?P<given>[^\W\d_][\w'-]*|[^\W\d_]\.)[ \t]+)?{re.escape(EMAIL_WITHHELD)}{_PERSON}"
)
# the words before an address's name that are no given name ("Met Rania HADDAD at", "Show Rania Haddad at",
# "Please email Zayna Fakemira at", "Send Rania at")
_NOT_GIVEN = (
    KEEPS | COMMON_WORDS | GOES_ON | WRITES_TO | ADDRESS_FOLLOWS | DETERMINERS | _OBJECTS | HONORIFICS
)
_NOT_GIVEN |= MAIN_PLACE_WORDS | {
    "send",
    "sent",
    "copy",
    "dear",
    "spoke",
    "meet",
    "call",
    "called",
    "asked",
    "told",
}


def _withheld(local: str) -> str:
    """:data:`EMAIL_WITHHELD`, marked (:data:`_PERSON`) when the address's name ``local`` is written
    capitalised as a person's ("Rania", "HADDAD", "Zayna.fakemira") and names no one else's thing."""
    word = local.split()[0] if local.split() else ""
    if not word[:1].isupper() or _typed_keeps(re.split(r"[^\w'-]", word)[0]) or _NOT_A_NAME.fullmatch(word):
        return EMAIL_WITHHELD
    return f"{EMAIL_WITHHELD}{_PERSON}"


def _person_before(match: re.Match) -> str:
    """An address withheld (:data:`_BEFORE_PERSON`) with the title and the given name before it: a given name
    written capitalised that is no word of a sentence (:data:`_NOT_GIVEN`), or an initial, goes with it, and
    so does a title before the address or before such a name ("Please email [email withheld]" from "Please
    email Ms. Fakemira at ...", "Met [email withheld]" from "Met Rania HADDAD at ...")."""
    given = match.group("given")
    if given:
        word = given.rstrip(".")
        initial = given.endswith(".") and len(word) == 1
        if not initial and not (_title(word) and fold(word) not in _NOT_GIVEN):
            return match.group(0).replace(_PERSON, "")
    return EMAIL_WITHHELD


def withhold_schemed(text: str) -> str:
    """A record's text with an address's name before "at" and a link with its scheme withheld with the link
    (:func:`_record_schemed`: "x.y at https://example.org"); the link rule takes any other."""
    return _AT_SCHEME.sub(_record_schemed, text) if _SCHEME_HINT.search(text) else text


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
    return _AT_SCHEME.sub(_schemed, read) if _SCHEME_HINT.search(read) else read


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


def withheld_typed(text: str) -> bool:
    """Whether ``text``, read as a person typed it (:func:`without_addresses`), holds an e-mail address or a
    web address written after a word and "at" that the typed rule withholds ("children at [link withheld]"):
    what a filter's search words and a page's address must not keep."""
    cleaned, raw = without_addresses(text), text or ""
    return any(cleaned.count(mark) > raw.count(mark) for mark in (EMAIL_WITHHELD, LINK_WITHHELD))


# a priest's or an imam's title before a name ("Fr Elias attended", "Father Elias")
_CLERGY_NAME = re.compile(rf"\b(?:Fr|Father|Rev|Revd|Pastor|Imam)\.?\s+{_TITLED}")


def titled_name(text: str) -> bool:
    """Whether ``text`` writes a person's name after a title ("Ms Rania met the principal", "Fr Elias
    attended", "السيدة ليلى صعب"): a page's address that does is never sent (its query is a person's words,
    no placeholder fits)."""
    shown = visible(text or "")
    titles = (PERSON_TITLE_NAME, _CLERGY_NAME, ARABIC_PERSON_TITLE_NAME)
    return any(title.search(shown) for title in titles)


def without_emails(text: str) -> str:
    """A document's or a code-written text sent whole (a document to review or summarise, a report's pages,
    a daily review finding, a What's new line): its e-mail addresses withheld, written out too, by the
    record rule (:func:`withhold_addresses`, read without the characters nobody sees), and a web address
    after a word and "at" as a link (no link rule follows: "Contact us at [link withheld]"); a text that holds
    none is sent exactly as it is."""
    shown = visible(text or "")
    cleaned = withhold_addresses(shown, link_rule=False)
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
