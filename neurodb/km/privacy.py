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
- **Other shapes** (:func:`_shapes`): reference lists ("Doe, J.", "Roe Poe, M. C."), "Doe 2020: …", "Roe 2021
  (Lebanon)", a speaker at a sentence's start ("Fakename, 11, was …"), "Fakename (20, male)", "Fakename, a
  boy", a name with an office ("Minister Jane Doe", "Jane Doe, Director of"); and, by the KM Database's
  surnames, "Doe J", "(Doe, Lebanon)", "studies by Doe, Roe and Poe" and "[name withheld] Doe". A first name
  left before a name withheld goes too ("Jane [name withheld]"). Words that start a sentence or a clause
  ("However, 2021", "Overall, 2021") are never names.

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
    "campaign mechanism portal system systems scheme package target targets achieved actual total planned "
    "estimate estimates projection projections forecast budget appendix cohort sample".split()
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
    # camps and settlements (not the camps' own names, which are also family names), and the words of places
    "camp camps settlement settlements informal tented nahr wadi bourj burj kfar deir jabal "
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
    "director manager headteacher adult adults infant infants toddler toddlers baby babies newborn newborns "
    "household households people persons individuals pupils patients survivors beneficiaries".split()
)
STOP = frozenset(
    "the and for with what which who how did does about from that this are was were has have any all our "
    "say said tell into there their".split()
)
# words that start a sentence or a clause ("However, 2021 data show", "Overall, 2021"), never names
CONNECTIVES = frozenset(
    "however overall similarly meanwhile moreover additionally furthermore notably likewise conversely "
    "nevertheless nonetheless consequently therefore thus hence instead finally previously currently "
    "recently historically specifically together altogether importantly interestingly surprisingly "
    "unfortunately fortunately generally typically usually accordingly subsequently initially firstly "
    "secondly thirdly lastly first second third next then later earlier now today still yet indeed further "
    "otherwise alternatively comparatively equally especially particularly mainly mostly largely partly "
    "broadly critically crucially significantly clearly evidently apparently reportedly ultimately "
    "eventually collectively separately respectively correspondingly simultaneously thereafter afterwards "
    "nowadays elsewhere here nationally regionally locally globally both either neither whereas although "
    "though because unless whether where why such so yes "
    # prepositions that are not in the common words
    "among amongst within beyond throughout despite following according regarding concerning including "
    "excluding unlike like besides towards toward upon versus compared".split()
)
# offices written with a person's name ("Minister Jane Doe", "Jane Doe, Director of"), never names themselves
OFFICES = frozenset(
    "minister ministers director directors president dean chair chairman chairwoman chairperson mayor "
    "governor ambassador head deputy secretary senator representative coordinator founder spokesperson "
    "spokesman commissioner rapporteur ceo mp professor prof lecturer researcher advisor adviser consultant "
    "prime general vice acting former executive assistant".split()
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

    @functools.cached_property
    def surname_words(self) -> frozenset[str]:
        """The words of every surname (a text holding none of them holds no surname of ``surnames``)."""
        return frozenset(w for surname in self.surnames for w in _LETTERS.findall(surname))


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
    return all(w in _EVERY or w.strip("'’") in _EVERY or _POSSESSIVE.sub("", w) in _EVERY for w in found)


_EVERY = MONTHS | SEASONS | TIME_WORDS | PLACE_WORDS | watch_people.COMMON_WORDS | STOP | CONNECTIVES
_POSSESSIVE = re.compile(r"['’]s?$")  # "Lebanon's", "Syrians'"


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


# ------------------------------------------------------------------- step 10: names in other shapes
# S1: a reference list's "Doe, J." or "Roe Poe, M. C." (initials with their full stop after a comma), ending
# an item (before a comma, ";", "&", "and", "(" or the end)
_ITEM_END = r"(?=[ \t]*(?:[,;&()]|and\b|$))"
S1 = re.compile(rf"{_BEFORE}(?P<a>{NAME}),[ \t]+[A-Z]\.(?:[ \t]?-?[A-Z]\.){{0,2}}{_ITEM_END}", re.MULTILINE)
_HAS_INITIAL = re.compile(r",[ \t]+[A-Z]\.")
# S1b: "Doe J" or "(Doe JM)", initials without a full stop, when Doe is one of the KM Database's surnames
S1B = re.compile(rf"{_BEFORE}(?P<a>{WORD})[ \t]+[A-Z]{{1,2}}\.?{_ITEM_END}", re.MULTILINE)
_LONE_CAPITALS = re.compile(rf"[ \t][A-Z]{{1,2}}\.?{_ITEM_END}", re.MULTILINE)
# S2: a name and a year before a colon, a ";" or a place in parentheses, the year not a date's ("Fake
# fieldwork May-June 2020;"): "Doe 2020: 20-60y; Roe 2022: ...", "Roe 2021 (Lebanon) fits"
S2 = re.compile(rf"{_BEFORE}(?P<a>{NAME})[ \t]+{YEAR}(?:[ \t]*[:;]|[ \t]+\((?P<place>[^()]{{2,40}})\))")
_HAS_YEAR_STOP = re.compile(rf"{YEAR}(?:[ \t]*[:;]|[ \t]+\()")
# S3: "(Doe, Lebanon)": a parenthesis that starts with a KM Database surname and a comma; then every other
# parenthesis of that text written so ("(Fakename, Jordan)")
S3 = re.compile(rf"\((?P<a>{WORD}),[ \t]+(?P<b>{NAME})\)")
# S4: a list of names after a word that cites them ("studies by Doe, Roe and Poe") when one of them is a KM
# Database surname
S4 = re.compile(
    rf"(?<![\w'’\-])(?:by|from|including|e\.g\.,?|see|cf\.?|such\s+as)[ \t]+"
    rf"(?P<names>{NAME}(?:[ \t]*,[ \t]*{NAME})*,?[ \t]+(?:and|&)[ \t]+{NAME})"
)
_LIST_SPLIT = re.compile(r"[ \t]*,[ \t]*(?:(?:and|&)[ \t]+)?|[ \t]+(?:and|&)[ \t]+")
# S5: a sentence that starts with a speaker's first name and age: "Fakename, 11, was hit ..."
S5 = re.compile(
    r"(?:^|(?<=[.!?…])[ \t]+|(?<=[.!?…][\"”’])[ \t]+)(?P<a>[A-Z][\w'’-]{2,}),[ \t]*(?:aged?[ \t]*)?"
    r"\d{1,2}(?:,[ \t]|[.;)](?!\d)|$)",
    re.MULTILINE,
)
_HAS_AGE = re.compile(r",[ \t]*(?:aged?[ \t]*)?\d{1,2}(?:,[ \t]|[.;)](?!\d)|$)", re.MULTILINE)
# S5b: a first name and an age with a sex or "years" in parentheses ("Fakename (20, male)", "Fakename (15
# years)"); S5c: a first name and a role after it ("Fakename, a boy from a camp")
S5B = re.compile(
    r"(?<![\w'’\-])(?P<a>[A-Z][\w'’-]{2,})[ \t]+\((?:aged?[ \t]*\d{1,2}\b[^()]{0,30}"
    r"|\d{1,2}(?:[ \t-]*(?:years?|yrs?|y)(?:[ \t-]*old)?)?[ \t]*[,;]?[ \t]*"
    r"(?:male|female|boy|girl|man|woman|M|F)\b[^()]{0,20})\)"
)
_AGE_IN_PARENTHESES = re.compile(r"\((?:aged?[ \t]*)?\d{1,2}\b")
_ROLES = "|".join(sorted(ROLE_WORDS, key=len, reverse=True))
S5C = re.compile(
    rf"(?<![\w'’\-])(?P<a>[A-Z][\w'’-]{{2,}}),[ \t]+an?[ \t]+(?:\d{{1,2}}[ \t-]*years?[ \t-]*old[ \t]+)?"
    rf"(?:(?:young|little|teenage|adolescent|syrian|lebanese|palestinian|iraqi|refugee)[ \t]+)?(?:{_ROLES})"
    r"(?![\w\-])"  # not "Fakeword, a female-led fake trade"
)
# S6: a name after an office ("Minister Jane Doe", "Centre Director Jane Doe") or before one ("Jane Doe,
# Director of the Fake Centre", "Jane Doe, the former Minister")
_OFFICE = (
    r"(?:Prime[ \t]+|Deputy[ \t]+|Vice[ \t]+|Acting[ \t]+)?(?:Minister|Director|President|Dean"
    r"|Chair(?:man|woman|person)?|Mayor|Governor|Ambassador|Head|Secretary|Senator|Representative"
    r"|Coordinator|Founder|Spokesperson|Spokesman|Commissioner|Rapporteur|CEO|MP)"
)
S6_BEFORE = re.compile(rf"(?<![\w'’\-]){_OFFICE}[ \t]+(?P<a>{NAME})")
S6_AFTER = re.compile(
    rf"{_BEFORE}(?P<a>{NAME}),[ \t]+(?i:(?:(?:the|an?)[ \t]+)?(?:(?:former|then|acting|deputy|assistant|"
    r"executive|managing|regional|country|general)[ \t]+)?(?:minister|director|president|dean|"
    r"chair(?:man|woman|person)?|mayor|governor|ambassador|head[ \t]+of|secretary|senator|representative|"
    r"coordinator|founder|spokesperson|spokesman|commissioner|professor|lecturer|researcher)\b)"
)
_PLACE_BEFORE = frozenset("in at from across within to into near around throughout outside inside".split())
# S7: a KM Database surname right after a name withheld ("[name withheld] Doe"); S8: a first name right
# before one ("Jane [name withheld]")
S7 = re.compile(rf"{re.escape(NAME_WITHHELD)}[ \t]+(?P<a>{_PARTICLE}?{WORD})")
S8 = re.compile(rf"{_BEFORE}(?P<a>{WORD}(?:[ \t]+{WORD}){{0,2}})[ \t]+(?={re.escape(NAME_WITHHELD)})")
_INITIALS_BEFORE = re.compile(
    rf"(?<![\w'’\-.])(?:[A-Z]\.[ \t]?-?){{1,3}}[ \t]*(?={re.escape(NAME_WITHHELD)})"
)


def _not_a_name(word: str, text: str = "") -> bool:
    """Whether one word is no person's: kept, an office or a role, or written in lower case elsewhere in
    ``text`` (a word, then: "Fakeword" when the text also writes "fakeword")."""
    folded = fold(_POSSESSIVE.sub("", word))
    if kept(word) or folded in OFFICES or folded in ROLE_WORDS or any(c.isdigit() for c in word):
        return True
    lower = word.lower()
    return (
        bool(text) and lower != word and re.search(rf"(?<![\w'’]){re.escape(lower)}(?!\w)", text) is not None
    )


def _person(match: re.Match, group: str = "a") -> tuple[int, int] | None:
    """The span of a match's name without its first words that are no person's ("In Fakeword" → "Fakeword",
    "Centre Jane Doe" → "Jane Doe"), or None when nothing is left or what is left is kept."""
    start, end = match.span(group)
    span = match.group(group)
    for word in _WORDS.finditer(span):
        if not _not_a_name(word.group()):
            begin = start + word.start()
            rest = match.string[begin:end]
            return None if kept(rest) else (begin, end)
    return None


def _surname_in(span: str, people: People) -> bool:
    return any(fold(_POSSESSIVE.sub("", w)) in people.surnames for w in _WORDS.findall(span))


def _listed(text: str, people: People, found: set[str], surnames: bool) -> list[tuple[int, int]]:
    """S1, S1b, S2 and S3: their names' spans (S1's with the initials). ``surnames``: whether the text may
    hold one of the KM Database's surnames (S1b and S3 need one)."""
    spans: list[tuple[int, int]] = []
    if _HAS_INITIAL.search(text):
        for m in S1.finditer(text):
            person = _person(m)
            if person:
                spans.append((person[0], m.end()))
                found.add(text[person[0] : person[1]])
    if surnames and _LONE_CAPITALS.search(text):
        for m in S1B.finditer(text):
            if fold(m.group("a")) in people.surnames:
                spans.append(m.span("a"))
                found.add(m.group("a"))
    if _HAS_YEAR_STOP.search(text):
        for m in S2.finditer(text):
            if _not_a_name(_WORDS.findall(m.group("a"))[-1]):  # a date: "Fake fieldwork May-June 2020;"
                continue
            place = m.group("place")
            if place is not None and not all(fold(w) in PLACE_WORDS for w in _WORDS.findall(place) or ["-"]):
                continue
            person = _person(m)
            if person:
                spans.append(person)
                found.add(text[person[0] : person[1]])
    if surnames and "(" in text and "," in text:
        matches = list(S3.finditer(text))
        if any(fold(m.group("a")) in people.surnames for m in matches):
            for m in matches:
                if not kept(m.group("a")) and (fold(m.group("a")) in people.surnames or kept(m.group("b"))):
                    spans.append(m.span("a"))
                    found.add(m.group("a"))
    return spans


def _cited_lists(text: str, people: People, found: set[str], surnames: bool) -> list[tuple[int, int]]:
    """S4: the names of a list after a citing word, when one of them is a KM Database surname."""
    spans: list[tuple[int, int]] = []
    if not surnames or not (" and " in text or "&" in text):
        return spans
    for m in S4.finditer(text):
        names = m.group("names")
        if not _surname_in(names, people):
            continue
        offset = m.start("names")
        position = 0
        for part in _LIST_SPLIT.split(names):
            at = names.index(part, position)
            position = at + len(part)
            if part and not kept(part):
                spans.append((offset + at, offset + at + len(part)))
                found.add(part)
    return spans


def _speakers_and_offices(text: str, found: set[str], words: list[str]) -> list[tuple[int, int]]:
    """S5, S5b, S5c and S6: a speaker by first name and age, or by a role after the name, and a name before
    or after an office."""
    spans: list[tuple[int, int]] = []
    speakers = []
    if _HAS_AGE.search(text):
        speakers += S5.finditer(text)
    if "(" in text and _AGE_IN_PARENTHESES.search(text):
        speakers += S5B.finditer(text)
    if ", a" in text:
        speakers += S5C.finditer(text)
    for m in speakers:
        if not _not_a_name(m.group("a")):
            spans.append(m.span("a"))
            found.add(m.group("a"))
    if OFFICES.isdisjoint(words):
        return spans
    for m in S6_BEFORE.finditer(text):
        person = _person(m)
        if person:
            spans.append(person)
            found.add(text[person[0] : person[1]])
    for m in S6_AFTER.finditer(text):
        person = _person(m)
        before = text[: person[0]].split() if person else []
        if person and not (before and fold(before[-1]) in _PLACE_BEFORE):  # "In Fakeplace, the head of"
            spans.append(person)
            found.add(text[person[0] : person[1]])
    return spans


def _around(text: str, people: People, found: set[str]) -> str:
    """S7 and S8: a KM Database surname right after a name withheld, and a first name right before one, join
    it ("[name withheld] Doe", "Jane [name withheld]" → "[name withheld]")."""
    if NAME_WITHHELD not in text:
        return text
    if people.surnames:

        def after(m: re.Match) -> str:
            word = _WORDS.findall(m.group("a"))[-1]
            if fold(word) in people.surnames:
                found.add(word)
                return NAME_WITHHELD
            return m.group(0)

        text = S7.sub(after, text)
    text = _INITIALS_BEFORE.sub("", text)  # "Jane J. [name withheld]" → "Jane [name withheld]"

    def before(m: re.Match) -> str:
        words = m.group("a").split()
        n = 0
        for word in reversed(words):  # the words next to the name withheld, up to one that is no person's
            if _not_a_name(word, text):
                break
            n += 1
        if not n:
            return m.group(0)
        found.update(words[-n:])
        return " ".join(words[:-n]) + (" " if n < len(words) else "")

    return S8.sub(before, text)


def _shapes(text: str, people: People, words: list[str]) -> str:
    """Step 10: names written in the shapes the citations' patterns do not take: reference lists ("Doe, J.",
    "Doe J"), "Doe 2020: ...", "(Doe, Lebanon)", "studies by Doe, Roe and Poe", "Fakename, 11, was ...",
    "Fakename (20, male)", "Fakename, a boy", a name with an office ("Minister Jane Doe", "Jane Doe, Director
    of"); then the same names wherever else the text writes them, and the words of a name next to a name
    withheld ("[name withheld] Doe", "Jane [name withheld]"). A name these shapes need a list for is one of
    the KM Database's surnames (``people.surnames``) only."""
    found: set[str] = set()
    surnames = bool(people.surnames) and not people.surname_words.isdisjoint(words)
    spans = (
        _listed(text, people, found, surnames)
        + _cited_lists(text, people, found, surnames)
        + _speakers_and_offices(text, found, words)
    )
    if spans:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        text = _again(_withhold(text, merged), found)
    for _ in range(3):  # a name withheld again may stand next to another word of a name
        found = set()
        joined = _again(_around(text, people, found), found)
        if joined == text:
            break
        text = joined
    return text


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
    return _shapes(text, people, found)


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
    if _citations(text) != text or _speakers(text) != text:
        return True
    return _shapes(text, people, _LETTERS.findall(_folded(text))) != text


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
