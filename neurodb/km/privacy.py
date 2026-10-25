"""Names of people in the KM Database's texts: withheld from its downloads (:func:`for_download`) and from
what its assistant sends (:func:`for_ai`), and the final check that nothing is left (:func:`leftover`).

Built from NeuroDB Watch's patterns (:mod:`neurodb.watch.people`, :mod:`neurodb.watch.redact`) with the KM
Database's own name sets only (:class:`People`, from :func:`neurodb.km.authors.people`): never NeuroDB's
list of the people it knows, which no KM text is compared with, and nothing is added to that list.

- **Citations** need no list of names: "Doe et al.", "(Doe, 2019; Roe and Poe, 2020)", "(Doe 2019, Roe
  2020)", "(Doe 2002 methodology)", "(FAKEORG, Doe and Roe 2020)", "Roe (2018) found", "Doe, 2019", "Doe and
  Roe 2020", "in Doe 2016" lose the names, and so does the same name wherever else that text writes it
  ("Doe 2016 (Lebanon) relates"). A "name" here is one to three capitalised words; it is kept when it is an
  organisation (:func:`neurodb.km.authors.org_test`), a month, a season, a word of time, a place or a common
  word ("(February 2023)", "(Since 2019)", "Lebanon (2020)", "(Census 2019)"), holds a generic word of a
  title ("Response Plan (2020)") or is an acronym such as "FaSyR". Outside a parenthesis, one name before a
  year ("Since 2019") is not taken for a citation.
- **Interview speakers** ("… – Fakename, 34", "(Fakename, 15)") lose the first name, not a role ("(Mother,
  34)").
- **A document's own authors** by their surname ("J. Doe interviewed", "Doe's survey") are withheld in the
  texts of that document.
- **The KM Database's authors** by their full name, and names after a title ("Dr …"), anywhere.

Downloads keep links and numbers; the assistant's texts also lose e-mail addresses, links and phone numbers.
The broad phone pattern is never used: it would take sample sizes and years. A question typed on the Ask tab
is cleaned by :func:`clean_question` (the Help assistant's rule first, then the KM Database's).
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

from neurodb.watch import people as watch_people
from neurodb.watch import redact

NAME_WITHHELD = watch_people.NAME_WITHHELD
EMAIL_WITHHELD = watch_people.EMAIL_WITHHELD
LINK_WITHHELD = redact.LINK_WITHHELD
PHONE_WITHHELD = watch_people.PHONE_WITHHELD

# ------------------------------------------------------------------------------------------- word lists
# generic words written in code, never names; compared folded (lower case, no accents)
MONTHS = frozenset(
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec "
    "janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre".split()
)
SEASONS = frozenset("spring summer autumn fall winter printemps ete automne hiver".split())
# words of time before a year ("(Since 2019)", "(Early 2020)"), never names
TIME_WORDS = frozenset(
    "since until till early late mid end start beginning around circa ca approx approximately year years "
    "fiscal academic school calendar".split()
)
GENERIC_WORDS = frozenset(
    "census survey surveys round wave phase baseline endline midline figure table annex chapter section "
    "report reports plan strategy framework law decree article resolution assessment assessments analysis "
    "update edition version dataset data study evaluation review index profile brief note appeal response "
    "results volume issue part page box model toolkit guidance guidelines bulletin snapshot factsheet "
    "overview summary monitor monitoring outlook handbook pillar tool tools tracker card items field "
    "campaign mechanism portal system systems scheme package".split()
)
PLACE_WORDS = frozenset(
    # the area and the donors' countries
    "lebanon syria jordan iraq palestine turkey turkiye egypt cyprus israel gaza yemen libya sudan iran "
    "kuwait qatar saudi arabia emirates uae oman bahrain arab mena middle east west north south africa asia "
    "europe european union united kingdom states america usa us uk britain france germany italy spain "
    "netherlands norway sweden denmark finland switzerland austria belgium ireland luxembourg canada japan "
    "korea australia "
    # Lebanon's governorates, districts and main towns
    "beirut mount akkar baalbek baalbeck hermel bekaa beqaa nabatieh nabatiyeh tripoli sidon tyre zahle "
    "zahleh jbeil byblos keserwan kesrouan metn matn baabda aley chouf shouf zgharta koura batroun bcharre "
    "becharre minieh dinnieh miniyeh danniyeh rashaya rachaya hasbaya marjayoun jezzine halba jounieh arsal "
    # camps and settlements (not the camps' own names, which are also family names)
    "camp camps settlement settlements informal tented "
    # other countries (a citation-shaped "Chile (2011)" or "Peru, 2016" is a place, not a name)
    "afghanistan albania algeria angola argentina armenia azerbaijan bangladesh belarus benin bhutan "
    "bolivia bosnia herzegovina botswana brazil bulgaria burkina faso burundi cambodia cameroon chad "
    "chile china colombia congo costa rica croatia cuba czechia djibouti dominican ecuador salvador "
    "eritrea estonia eswatini ethiopia fiji gabon gambia georgia ghana greece guatemala guinea haiti "
    "honduras hungary iceland india indonesia jamaica kazakhstan kenya kosovo kyrgyzstan laos latvia "
    "lesotho liberia lithuania madagascar malawi malaysia maldives mali malta mauritania mauritius mexico "
    "moldova mongolia montenegro morocco mozambique myanmar namibia nepal zealand nicaragua niger nigeria "
    "macedonia pakistan panama papua paraguay peru philippines poland portugal romania russia rwanda "
    "senegal serbia sierra leone singapore slovakia slovenia somalia sri lanka tajikistan tanzania "
    "thailand timor togo tunisia turkmenistan uganda ukraine uruguay uzbekistan venezuela vietnam zambia "
    "zimbabwe latin caribbean pacific sahel horn "
    # nationalities
    "syrian syrians lebanese palestinian palestinians iraqi iraqis jordanian egyptian turkish kurdish "
    "armenian sudanese ethiopian arab arabs migrant migrants refugee refugees".split()
)
ROLE_WORDS = frozenset(
    "mother father parent caregiver teacher principal student pupil participant respondent interviewee "
    "girl boy woman man adolescent youth refugee female male child worker nurse doctor official fgd kii "
    "mothers fathers parents caregivers teachers students participants respondents girls boys women men "
    "adolescents children workers officials grandmother grandfather sister brother daughter son wife "
    "husband widow orphan farmer midwife counsellor counselor volunteer facilitator coordinator "
    "director manager headteacher".split()
)
STOP = frozenset(
    "the and for with what which who how did does about from that this are was were has have any all our "
    "say said tell into there their".split()
)
PARTICLES = ("de", "van", "von", "el", "al", "bin", "ibn", "abu", "abou", "bou")

# ------------------------------------------------------------------------------------------- patterns
_CAP = "A-ZÀ-ÖØ-ÞĀĂĄĆČĎĐĒĘĚĞĪİŁŃŇŌŐŒŘŚŞŠŢŤŪŮŰŸŹŻŽ"
WORD = rf"(?:(?:al|el)-)?[{_CAP}][\w'’\-]+"
_PARTICLE = rf"(?:(?:{'|'.join(PARTICLES)})\s+)"
# a name: one to three capitalised words, with the particles of names between them
NAME = rf"{_PARTICLE}?{WORD}(?:\s+{_PARTICLE}?{WORD}){{0,2}}"
YEAR = r"(?:19|20)\d\d[a-z]?"
LOC = r"(?:,\s*pp?\.\s*\d+(?:\s*[-–]\s*\d+)?|:\s*\d+(?:\s*[-–]\s*\d+)?)?"
_BEFORE = r"(?<![\w'’\-])"
ET_AL = r"\s+et\.?\s+al\b\.?"
_TWO = rf"(?P<a>{NAME})(?:\s+(?:and|&)\s+(?P<b>{NAME}))?"
# C1: "Doe et al.", "Jane Doe and John Roe et al."
C1 = re.compile(rf"{_BEFORE}{_TWO}(?={ET_AL})")
# C2: a parenthesis that lists citations: "(Fakeorg Institute, 2020; Doe, 2019)", "(Poe, 2017, p. 4)"
PAREN = re.compile(r"\(([^()]{4,600})\)")
C2_ITEM = re.compile(rf"^\s*{_TWO}(?:{ET_AL})?,?\s+{YEAR}(?:\s*,\s*{YEAR})*{LOC}\s*$")
# C3: "Roe (2018) found", "Doe and Roe (2019: 4)"
C3 = re.compile(rf"{_BEFORE}{_TWO}(?=(?:{ET_AL})?\s*\(\s*{YEAR}(?:\s*,\s*{YEAR})*{LOC}\s*\))")
# C2, an item that starts with names and a year, after a programme's name and a slash (or a colon, or "e.g.")
# or not: "(Doe 2002 methodology)", "(Doe, Roe and Poe 2020)", "(FAKEORG, Doe and Roe 2020)", "(Fake
# Programme/Doe 2015, 10-13y)"; then every other name before a year in that item ("(Doe 2010, Roe 2020)",
# "(Doe 2020 and Doe 2023)")
_NAMES = rf"(?P<names>{NAME}(?:\s*,\s*{NAME})*(?:\s*,?\s+(?:and|&)\s+{NAME})?)"
_CITING = r"(?:e\.g\.|i\.e\.|see|cf\.?|including|such\s+as)"  # words before the names of a citation
C2_START = re.compile(
    rf"^\s*(?:[^/:;()]{{1,80}}[/:]\s*)?(?:{_CITING},?\s+)?{_NAMES}(?:{ET_AL})?,?\s+{YEAR}\b"
)
C2_MORE = re.compile(rf"{_BEFORE}{_TWO}(?=(?:{ET_AL})?,?\s+{YEAR}\b)")
NAME_RE = re.compile(NAME)
ITEM_MAX = 400  # characters of a parenthesis's item read for names before a year
# C4: "Doe, 2019"
C4 = re.compile(rf"{_BEFORE}{_TWO}(?=(?:{ET_AL})?,\s+{YEAR}\b)")
# C5: outside a parenthesis, two names before a year: "Doe and Roe 2020" (not one: "Since 2019")
C5 = re.compile(rf"{_BEFORE}(?P<a>{NAME})\s+(?:and|&)\s+(?P<b>{NAME})(?=\s+{YEAR}\b)")
# C6: outside a parenthesis, a name before a year after a word that cites it: "in Doe 2016", "for Doe 2017",
# "pooling Doe 2020 and Roe 2023"
C6 = re.compile(
    rf"(?<![\w'’\-])(?:in|for|by|from|per|than|with|and|pooling|{_CITING})\s+(?P<a>{NAME})"
    rf"(?=,?\s+{YEAR}\b)"
)
# an interview's speaker by first name and age: "(Fakename, 34)", "“…” – Fakename, 34"
SPEAKER = re.compile(
    r"(?:\(\s*|[\"”]\s*[-–—]\s*)([A-Z][\w'’-]{2,})(?=,\s*(?:aged?\s*)?\d{1,2}\b(?![.,]\d|\s*%))"
)
_HAS_YEAR = re.compile(r"(?:19|20)\d\d")
_HAS_ET_AL = re.compile(r"\bet\.?\s+al\b", re.IGNORECASE)
_MIXED_CAPS = re.compile(r"^.[^A-Z]*[A-Z][^A-Z]*[A-Z]")  # two capitals after the first letter: "FaSyR"
_PLURAL_ACRONYM = re.compile(r"^[A-Z]{2,}s$")  # "NGOs", "CSOs"
_WORDS = re.compile(r"[\w'’]+")


@dataclass(frozen=True, eq=False)
class People:
    """The KM Database's own people: the name forms of its authors (two words or more, as
    :func:`neurodb.watch.people.name_forms` writes them), their surnames, and the surnames of each
    document's own authors (by the document's pk)."""

    names: frozenset[str] = frozenset()
    surnames: frozenset[str] = frozenset()
    own: dict[int, frozenset[str]] = field(default_factory=dict)

    @functools.cached_property
    def firsts(self) -> frozenset[str]:
        """The first word of every name (a text holding none of them names nobody of ``names``)."""
        return frozenset(name.split(" ", 1)[0] for name in self.names if "@" not in name)


NOBODY = People()


def fold(text: str) -> str:
    return watch_people.fold(text)


@functools.lru_cache(maxsize=65536)
def kept(name: str) -> bool:
    """Whether a name-shaped span is not a person: an organisation, a month, a season, a place, a common
    word, a title's generic word ("Plan", "Survey"), or an acronym of mixed case ("FaSyR")."""
    from .authors import org_test

    found = [fold(w) for w in _WORDS.findall(name)]
    if not found:
        return True
    if org_test(name):
        return True
    if any(w in GENERIC_WORDS for w in found):
        return True
    if any(_MIXED_CAPS.match(w) or _PLURAL_ACRONYM.match(w) for w in _WORDS.findall(name)):
        return True
    every = MONTHS | SEASONS | TIME_WORDS | PLACE_WORDS | watch_people.COMMON_WORDS | STOP
    return all(w in every or w.strip("'’") in every for w in found)


def _names(match: re.Match) -> list[tuple[int, int]]:
    """The spans of the names of a C1-C6 match that are not kept."""
    spans = []
    found = match.groupdict()
    for group in ("a", "b"):
        if found.get(group) and not kept(found[group]):
            spans.append(match.span(group))
    return spans


def _withhold(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = f"{text[:start]}{NAME_WITHHELD}{text[end:]}"
    return text


def _cited(item: str, cited: set[str]) -> str | None:
    """An item (or a part of one) that starts with names and a year (C2_START), its names not kept withheld,
    and every other name before a year in it (C2_MORE); None when it does not start so."""
    found = C2_START.match(item)
    if found is None:
        return None
    start = found.start("names")
    spans = {
        (start + m.start(), start + m.end())
        for m in NAME_RE.finditer(found.group("names"))
        if not kept(m.group())
    }
    spans |= {span for m in C2_MORE.finditer(item, found.end()) for span in _names(m)}
    cited.update(item[a:b] for a, b in spans)
    return _withhold(item, sorted(spans))


def _item(item: str, cited: set[str]) -> str:
    """One item of a parenthesis (between ";"), its citations' names withheld: "Doe, 2019" (C2_ITEM), names
    and a year at its start, or at the start of one of its parts between commas."""
    found = C2_ITEM.match(item)
    if found:
        spans = _names(found)
        cited.update(item[a:b] for a, b in spans)
        return _withhold(item, spans)
    if len(item) > ITEM_MAX or not _HAS_YEAR.search(item):
        return item
    withheld = _cited(item, cited)
    if withheld is not None:
        return withheld
    if "," not in item:
        return item
    parts = item.split(",")
    return ",".join(_cited(part, cited) or part if _HAS_YEAR.search(part) else part for part in parts)


def _pair(match: re.Match) -> list[tuple[int, int]]:
    """C5's two names, when neither is kept ("Fakeword and Child Development Survey 2023" is a title)."""
    if kept(match.group("a")) or kept(match.group("b")):
        return []
    return [match.span("a"), match.span("b")]


def _citations(text: str) -> str:
    """Step 5: the names of citations (C1-C6), withheld; then the same names wherever else the text writes
    them ("pooling Doe 2020 ...; Doe 2020 (Lebanon) relates ...")."""
    cited: set[str] = set()

    def hold(value: str, spans: list[tuple[int, int]]) -> str:
        cited.update(value[a:b] for a, b in spans)
        return _withhold(value, spans)

    if _HAS_ET_AL.search(text):
        text = hold(text, [s for m in C1.finditer(text) for s in _names(m)])
    if not _HAS_YEAR.search(text):
        return _again(text, cited)
    if "(" in text:

        def paren(match: re.Match) -> str:
            return "(" + ";".join(_item(item, cited) for item in match.group(1).split(";")) + ")"

        text = PAREN.sub(paren, text)
        text = hold(text, [s for m in C3.finditer(text) for s in _names(m)])
    if "," in text:
        text = hold(text, [s for m in C4.finditer(text) for s in _names(m)])
    if " and " in text or "&" in text:
        text = hold(text, [s for m in C5.finditer(text) for s in _pair(m)])
    text = hold(text, [s for m in C6.finditer(text) for s in _names(m)])
    return _again(text, cited)


def _again(text: str, cited: set[str]) -> str:
    """The names a citation of ``text`` withheld, and their last words (a surname), withheld wherever else
    the text writes them, capitalised."""
    if not cited:
        return text
    found = set(cited)
    for name in cited:
        last = name.rsplit(" ", 1)[-1]
        if len(last) >= 3 and not kept(last):
            found.add(last)
    alternation = "|".join(re.escape(name) for name in sorted(found, key=len, reverse=True))
    return re.sub(rf"(?<![\w'’\-])(?:{alternation})(?![\w\-])", NAME_WITHHELD, text)


def _speakers(text: str) -> str:
    """Step 6: an interview's speaker named by a first name and an age."""
    if "," not in text:
        return text
    spans = []
    for match in SPEAKER.finditer(text):
        name = match.group(1)
        if fold(name) not in ROLE_WORDS and not kept(name):
            spans.append(match.span(1))
    return _withhold(text, spans)


# a capitalised word, with an initial before it ("J. Doe") and a possessive after it ("Doe's") left out
OWN = re.compile(rf"(?:\b[A-Z]\.\s*)?(?<![\w'’\-])([{_CAP}][\w\-]*)(?=(?:['’]s)?(?![\w\-]))")


def _own(text: str, own: frozenset[str]) -> str:
    """Step 7: the document's own authors by their surname ("J. Doe", "Doe's"), withheld."""
    if not own:
        return text

    def one(match: re.Match) -> str:
        return NAME_WITHHELD if fold(match.group(1)) in own else match.group(0)

    return OWN.sub(one, text)


def _titles(text: str) -> str:
    """Step 9: a name after a title ("Dr …", "Mrs …")."""
    text = watch_people.PERSON_TITLE_NAME.sub(NAME_WITHHELD, text)
    return watch_people.HONORIFIC_NAME.sub(NAME_WITHHELD, text)


_AT_SIGNS = ("@", "＠", "﹫")
# what every spelled-out address holds: an "at" (or an at sign) and a domain's ending after a dot ("x at
# example dot org", "x [at] example.org"): a text without both is not searched for one (most texts)
_SPELLED_AT = re.compile(r"\bat\b|[@＠﹫]", re.IGNORECASE)
_SPELLED_ENDING = re.compile(
    r"(?:\.|\bdot\b|\[\.\]|\(\.\)|\{\.\})\s*"
    r"(?:org|com|net|int|gov|edu|lb|fr|uk|de|ch|ca|ae|sa|qa|jo|eg|nl|se|au|info|biz|ngo|io|co)\b",
    re.IGNORECASE,
)


def _emails(text: str) -> str:
    if any(sign in text for sign in _AT_SIGNS):
        text = watch_people.EMAIL.sub(EMAIL_WITHHELD, text)
    if _SPELLED_ENDING.search(text) and _SPELLED_AT.search(text):
        text = watch_people.SPELLED_EMAIL.sub(EMAIL_WITHHELD, text)
    return text


def for_download(text: str, people: People = NOBODY, own: frozenset[str] = frozenset()) -> str:
    """``text`` as a download writes it: e-mail addresses, the names of citations and interview speakers,
    the document's own authors (``own``), the KM Database's authors and names after a title withheld.
    Links and numbers are kept."""
    if not text:
        return text
    text = watch_people.visible(text)
    text = _emails(text)
    return _people(text, people, own)


def for_ai(text: str, people: People = NOBODY, own: frozenset[str] = frozenset()) -> str:
    """``text`` as the assistant may send it: as :func:`for_download`, and links and phone numbers withheld
    (never the broad phone pattern, which would take sample sizes)."""
    if not text:
        return text
    text = watch_people.visible(text)
    text = _emails(text)
    text = redact.LINK.sub(LINK_WITHHELD, text)
    text = watch_people.PREFIXED_PHONE.sub(PHONE_WITHHELD, text)
    text = watch_people.INTL_PHONE.sub(PHONE_WITHHELD, text)
    return _people(text, people, own)


_LETTERS = re.compile(r"[^\W\d_]+")
# punctuation a word processor writes, as plain ASCII: a text that is ASCII once they are replaced is folded
# by lower-casing it (most texts), without the slower accent-stripping walk
_PUNCTUATION = str.maketrans(
    {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": ".", "\xa0": " "}
)


def _folded(text: str) -> str:
    plain = text.translate(_PUNCTUATION)
    return plain.casefold() if plain.isascii() else fold(plain)


def _may_name(words: list[str], people: People) -> bool:
    """Whether ``words`` (a text's, folded) may hold a name of ``people``: a known name, or a known
    two-word name with up to two names between its words (as :func:`neurodb.watch.people.scrub` finds
    them). Only then is the text scrubbed."""
    firsts, names = people.firsts, people.names
    for i, word in enumerate(words):
        if word not in firsts:
            continue
        for size in range(2, min(watch_people.LONGEST, len(words) - i) + 1):
            if " ".join(words[i : i + size]) in names:
                return True
        for gap in range(1, watch_people.MIDDLE + 1):
            if i + gap + 1 < len(words) and f"{word} {words[i + gap + 1]}" in names:
                return True
    return False


def _people(text: str, people: People, own: frozenset[str]) -> str:
    text = _citations(text)
    text = _speakers(text)
    folded = _folded(text)
    # the text's words, folded once: a name is looked for only where it may be written, and a name after a
    # title only where a title's word is. Full names go before the document's own surnames, so that no first
    # name is left before a surname withheld ("consultant John [name withheld]")
    found = _LETTERS.findall(folded)
    if people.names and _may_name(found, people):
        text = watch_people.scrub(text, names=people.names)
        folded = _folded(text)
    if own and any(surname in folded for surname in own):
        text = _own(text, own)
    if not watch_people.TITLE_WORDS.isdisjoint(found):
        text = _titles(text)
    return text


def leftover(text: str, people: People = NOBODY) -> list[str]:
    """What a final check still finds in ``text``: "email", "link", "phone", "name" (a KM author's name, a
    name after a title, a citation or a speaker whose name is not kept). Empty when nothing is left."""
    text = watch_people.visible(text or "")
    found = []
    if watch_people.emails_in(text) or watch_people.SPELLED_EMAIL.search(text):
        found.append("email")
    if redact.LINK.search(text):
        found.append("link")
    if watch_people.PREFIXED_PHONE.search(text) or watch_people.INTL_PHONE.search(text):
        found.append("phone")
    if _names_left(text, people):
        found.append("name")
    return found


def _names_left(text: str, people: People) -> bool:
    if people.names and any("@" not in found for found in watch_people.mentions(text, names=people.names)):
        return True
    if watch_people.PERSON_TITLE_NAME.search(text) or watch_people.HONORIFIC_NAME.search(text):
        return True
    return _citations(text) != text or _speakers(text) != text


QUESTION_CHARS = 1000
# a capitalised word just before a name withheld: its first name ("Jane [name withheld]")
_BEFORE_WITHHELD = re.compile(rf"(?<![\w'’\-])({WORD})\s+(?={re.escape(NAME_WITHHELD)})")


def clean_question(question: str, people: People = NOBODY) -> str:
    """A question typed on the Ask tab as it is sent and kept: the names NeuroDB knows, e-mail addresses,
    links and phone numbers removed (``fmm.privacy.clean``, the Help assistant's rule: it reads NeuroDB's
    list of people only to take names out, and adds nothing to what is sent), then the KM Database's own
    rule (:func:`for_ai`): its authors by their full name and by their surname, citations, names after a
    title; and a first name left before a name withheld."""
    from neurodb.fmm import privacy as fmm_privacy

    cleaned = fmm_privacy.clean(question, QUESTION_CHARS)[0]
    cleaned = for_ai(cleaned, people, people.surnames)

    def first_name(match: re.Match) -> str:
        return "" if not kept(match.group(1)) else match.group(0)

    return _BEFORE_WITHHELD.sub(first_name, cleaned) if NAME_WITHHELD in cleaned else cleaned
