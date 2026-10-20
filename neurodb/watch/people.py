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

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600  # a run reads the names once; a long-lived process reads them again after this
LONGEST = 6  # words of a name compared at most
NAME_WITHHELD = "[name withheld]"
EMAIL_WITHHELD = "[email withheld]"

# Characters a text may hold that nobody sees (zero-width spaces and joiners, the soft hyphen, the byte
# order mark): taken out before anything is looked for (:func:`visible`), so that an address or a name
# with one inside ("layla.saab\u200b@example.org") is found as it reads
INVISIBLE = re.compile("[\u00ad\u200b-\u200d\u2060\ufeff]")
_AT_SIGN = "[@＠﹫]"  # with the full-width and the small at sign
# An address, also written with spaces around "@" ("layla.saab @ unicef.org", then only before a
# domain that ends in letters: "2 trainers @ 50 USD" stays), with the sign in brackets ("x(@)y.org",
# "x [@] y.org") or with the full-width or the small sign
EMAIL = re.compile(
    rf"(?<![\w.+'-])[\w.+'-]+(?:{_AT_SIGN}|\s*(?:\[\s*{_AT_SIGN}\s*\]|\(\s*{_AT_SIGN}\s*\))\s*"
    rf"|\s*{_AT_SIGN}\s*(?=[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{{2,}}\b))[\w-]+(?:\.[\w-]+)+"
)
# An address written out to dodge a filter: "layla.saab at unicef dot org", "layla dot saab at unicef dot
# org", "layla.saab[at]unicef[.]org", "layla.saab{at}unicef.org", "layla.saab<at>unicef.org", "layla.saab
# -at- unicef.org", "layla.saab at unicef.org", "rhaddad at hotmail.fr", "rhaddad at yahoo.co.uk", "r .
# haddad at unicef . org", "layla . saab @ example . org". A plain full stop counts only before a
# domain's ending in lower case (``_ENDING``: never a word of a sentence such as "it" or "we"), so that a
# sentence ("located at Tyre. Net enrolment rose", "located at Tyre.Net", "at school.it was") is not
# taken; in any case after a name with a full stop inside, as an address has ("Layla.Saab at
# Unicef.Org", "LAYLA.SAAB AT UNICEF.ORG")
_AT = r"(?:\s*(?:\[at\]|\(at\)|\{at\}|<at>|-at-)\s*|\s+at\s+)"
_DOT = r"(?:\s*(?:\[dot\]|\(dot\)|\{dot\}|\[\.\]|\(\.\)|\{\.\})\s*|\s+dot\s+)"
_ENDINGS = "org|com|net|int|gov|edu|lb|fr|uk|de|ch|ca|ae|sa|qa|jo|eg|nl|se|au|info|biz|ngo|io"
_ENDING = rf"(?:{_ENDINGS}|co\.[a-z]{{2}})(?:\.[a-z]{{2}})?"  # with a country's: "unicef.org.lb", "co.uk"
_SPELLED_ENDING = rf"(?:{_ENDINGS}|co)(?:{_DOT}[a-z]{{2}})?"  # "unicef dot org dot lb"
_DOMAIN = rf"[\w-]+(?:(?:{_DOT}|\.)[\w-]+)*?"
_LOCAL = rf"[\w.+'-]+(?:{_DOT}[\w.+'-]+)*"  # "layla.saab", "layla dot saab"
_SPACED = r"[\w+'-]+(?:\s?\.\s?[\w+'-]+)*"  # "layla . saab"
# Spaced full stops: before "at", a space on both sides of each ("r . haddad at unicef . org"); and, as
# around "@", only the common endings, so that a sentence ("at Halba. info sessions", "at Halba. de
# facto", "meeting @ halba. info desk") is not taken
_SPACED_DOT = r"(?:\s\.\s|\.)"
_SPACED_ENDING = r"(?:org|com|net|int|gov|edu|lb|fr)"
SPELLED_EMAIL = re.compile(
    rf"\b{_LOCAL}{_AT}{_DOMAIN}(?:{_DOT}{_SPELLED_ENDING}|\.(?-i:{_ENDING}))\b"
    rf"|\b[\w+'-]+(?:\.[\w+'-]+)+{_AT}{_DOMAIN}\.(?:{_ENDING})\b"
    rf"|\b[\w+'-]+(?:{_SPACED_DOT}[\w+'-]+)*{_AT}[\w-]+(?:{_SPACED_DOT}[\w-]+)*?{_SPACED_DOT}"
    rf"(?-i:{_SPACED_ENDING})\b"
    rf"|\b{_SPACED}\s?{_AT_SIGN}\s?[\w-]+(?:\s?\.\s?[\w-]+)*\s?\.\s?(?-i:{_SPACED_ENDING})\b",
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
