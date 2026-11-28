"""Names of people in the KM Database's texts: withheld from its downloads (:func:`for_download`) and from
what its assistant sends (:func:`for_ai`), and the final check that nothing is left (:func:`leftover`).

Built from NeuroDB Watch's patterns (:mod:`neurodb.watch.people`, :mod:`neurodb.watch.redact`) with the KM
Database's own name sets only (:class:`People`, from :func:`neurodb.km.authors.people`): never NeuroDB's
list of the people it knows, which no KM text is compared with, and nothing is added to that list.

- **Citations** need no list of names: "Doe et al.", "(Doe, 2019; Roe and Poe, 2020)", "(Doe 2019, Roe
  2020)", "(Doe 2002 methodology)", "(FAKEORG, Doe and Roe 2020)", "Roe (2018) found", "Doe, 2019", "Doe and
  Roe 2020", "in Doe 2016", "In Doe 2021," at a sentence's start, "According to Doe 2019", "What did Doe 2019
  say", "By Doe 2019", "Source: Doe 2021", "Sources: Doe 2019; Roe 2020", "Tyre and Halba 2019; Doe 2020",
  "Doe's 2021 survey" lose the names, and so does the same name
  wherever else that text writes it ("Doe 2016 (Lebanon) relates"). A "name" here is one to three
  capitalised words; it is kept when it is an organisation (:func:`neurodb.km.authors.org_test`), a month, a
  season, a word of time, a place (a country, a governorate, a district or a main town; a place's name of
  several words of :mod:`neurodb.km.places` written whole when one of its words marks it a place
  (:data:`PLACE_MARKERS`): "Bab al-Tabbaneh 2024", "Western Bekaa", never "Elias" alone; one made only of
  words a name has, "Saad Nayel", "Tal Abbas", "Wadi Khaled", and a town's or a district's other one-word
  spellings, which are family names too, only written as a place before a bare year, after a word that
  writes a place, every name listed a place, never one of the KM Database's surnames: "in Saida 2023",
  "numbers for Sayda, 2024", "in Tyre, Qana and Houla 2024", while "(Naameh 2021)", "Naameh (2021) found",
  "in Naameh et al.", "Saad Nayel et al.", "from Akar & Naameh 2020", "As found in Naameh 2021", "– Sayda,
  14" and an author "Naameh, R." are names; not the camps) or a common word ("(February 2023)", "(Since
  2019)", "Lebanon (2020)", "(Census 2019)"), holds a generic word of a title ("Response Plan (2020)") or is
  an acronym such as "FaSyR". Outside a parenthesis, one name before a year ("Since 2019") is not taken for a
  citation, but after a citation's or a source's word. The names listed with commas before a citation's two
  ("Doe, Roe and Poe (2019)") are its names too; in a pair each name is judged on its own ("UNICEF & Doe 2020"
  keeps "UNICEF"), its towns kept only where a word that writes a place comes before it.
- **Interview speakers** ("… – Fakename, 34", "(Fakename, 15)", "“…,” said Fakename, 14.", "In an
  interview, Fakename, 14,") lose the first name, not a role ("(Mother, 34)", "said Mother, 34").
- **A document's own authors** by their surname ("J. Doe interviewed", "Doe's survey", "the Doe-led
  survey") are withheld in the texts of that document.
- **The KM Database's authors** by their full name, and names after a title ("Dr …"), anywhere.
- **Other shapes** (:func:`_shapes`): reference lists ("Doe, J.", "Roe Poe, M. C.", and with a year "Doe J,
  Roe PM. Fake title. 2019."), "Doe 2020: …", "Roe 2021
  (Lebanon)" (never an event's or a topic's word: "Cholera 2022 (Bebnine)"; "Storm 2021 (Lebanon)" and
  "Blankenship 2021 (Lebanon)" are names), a speaker at a sentence's start ("Fakename, 11, was …"), "Fakename
  (20, male)", "Fakename, a boy", a name with an office ("Minister Jane Doe", "Jane Doe, Director of"); and,
  by the KM Database's surnames, "Doe J", "Doe J. Fake title", "(Doe, Lebanon)", "studies by Doe, Roe and Poe"
  and "[name withheld] Doe" (and "studies by Roe, Poe and [name withheld]"). A first name left before a name
  withheld goes too ("Jane [name withheld]"). Words that start a sentence or a clause ("However, 2021",
  "Overall, 2021") are never names; a request's verb ("Show", "List", "Rank") is a family name too, read apart
  only in a plain request about places (:data:`REQUEST`: "Compare Chtoura and Zahle 2022"). A place's word
  that is a given or a family name too (:data:`_PLACES_NAMES_TOO`: "Jordan", "Costa") is a name in all these
  shapes, a place only where it is written as one.

Downloads keep links and numbers; the assistant's texts also lose e-mail addresses, links and phone numbers.
The broad phone pattern is never used: it would take sample sizes and years. A question typed on the Ask tab
is cleaned by :func:`clean_question` (the Help assistant's rule first, then the KM Database's, with its
surnames in any case: :func:`typed_for_ai`).
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

from neurodb.watch import people as watch_people
from neurodb.watch import redact

from . import places

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


def _place_names() -> tuple[frozenset[str], frozenset[tuple[str, ...]]]:
    """The places of :mod:`neurodb.km.places` (every governorate, district, town and region in Latin
    letters), folded: the names of one word, and the names of several words as a whole ("Bar Elias", "Jabal
    Mohsen": never "Elias" or "Mohsen" alone, which are names of people too). Not the camps, whose own names
    are also family names ("Shatila")."""
    every = [s for p in (*places.GOVERNORATES, *places.DISTRICTS) if not p.camp for s in p.spellings]
    every += [s for s in places.REGIONS if not places.arabic(s)]
    single, whole = set(), set()
    for spelling in every:
        found = tuple(watch_people.fold(w) for w in re.split(r"[\s-]+", spelling) if w)
        if len(found) == 1:
            single.add(found[0])
        else:
            whole.add(found)
    return frozenset(single), frozenset(whole)


PLACE_NAMES_ONE, PLACE_NAMES = _place_names()
PLACE_LONGEST = max(len(name) for name in PLACE_NAMES)
# the words of places that are never a person's (a name-shaped span of these is kept wherever it is written:
# "Lebanon (2020)", "Tripoli, 2019"); a town's or a district's one-word spelling of places.py
# (:data:`PLACE_NAMES_ONE`: "Saida", "Sayda", "Naameh", "Houla") is a family name too, kept only where it is
# written as a place (:func:`_as_place`)
PLACE_WORDS = watch_people.MAIN_PLACE_WORDS.union(
    # (with Lebanon's governorates, districts and main towns) the area and the donors' countries
    "lebanon syria jordan iraq palestine turkey turkiye egypt cyprus israel gaza yemen libya sudan iran "
    "kuwait qatar saudi arabia emirates uae oman bahrain arab mena middle east west north south africa asia "
    "europe european union united kingdom states america usa us uk britain france germany italy spain "
    "netherlands norway sweden denmark finland switzerland austria belgium ireland luxembourg canada japan "
    "korea australia "
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
# the words of places (:data:`PLACE_WORDS`) that are a given name or a family name too ("Jordan", "West",
# "Costa", "Arab", "Wadi"): a place only where it is written as one ("refugees in Jordan 2019", as a town's
# other spelling: :func:`_as_place`), never kept wherever it is written ("Jordan et al. (2020)", "Tyre and
# Costa 2023", "By West 2019" withhold it). Not the names of Lebanon's governorates ("the North", "the
# South")
_PLACES_NAMES_TOO = frozenset(
    "jordan west france georgia chad israel india ireland kenya camp camps horn salvador costa arab arabs "
    "wadi sierra leone asia mali america jamaica montenegro iran".split()
)
# the words of places kept wherever they are written ("Lebanon (2020)", "Tripoli, 2019")
_KEPT_PLACES = PLACE_WORDS - _PLACES_NAMES_TOO
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
# a name, or one already withheld (an item of a list of names: "studies by Roe, Poe and [name withheld]")
NAME_OR_WITHHELD = rf"(?:{NAME}|{re.escape(NAME_WITHHELD)})"
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
# "pooling Doe 2020 and Roe 2023", "In" that opens a sentence ("In Doe 2021, 12% of girls ..."), and an
# earlier year and ";" (a list of citations: "Tyre and Halba 2019; Doe 2020")
_OPENS = r"(?:^|(?<=\n)|(?<=[.!?…:;]\s)|(?<=[.!?…][\"”’)]\s))"
C6 = re.compile(
    rf"(?:(?<![\w'’\-])(?:in|for|by|from|per|than|with|and|pooling|{_CITING}|(?:19|20)\d\d[a-z]?\)?[ \t]*;)"
    rf"|{_OPENS}In)\s+(?P<a>{NAME})(?=,?\s+{YEAR}\b)"
)
# C5c: a request's names joined by a comma before a year ("Plot Lina, Nabatieh 2017", "Compare Tyre, Quenby
# 2020"), each judged on its own as C5's are (:func:`_pair`)
C5C = re.compile(rf"{_BEFORE}(?P<a>{NAME})[ \t]*,[ \t]*(?P<b>{NAME})(?=\s+{YEAR}\b)")
# C7: a name (or two) before a bare year after a source's words: "Based on Doe 2019", "Source: Doe 2021",
# "References: Doe 2019", "Adapted from Doe and Roe 2019", "Data: Doe 2020" ("According to" and "Per": C9)
C7 = re.compile(
    r"(?<![\w'’\-])(?:[Bb]ased\s+on|[Ss]ources?\s*:|[Rr]eferences?\s*:|[Aa]dapted\s+from|[Dd]ata\s*:)"
    rf"\s+{_TWO}(?=,?\s+{YEAR}\b)"
)
# C9: a name before a bare year after "did", "does", "do", "by", "according to" or "per" ("What did Quenby
# 2019 say about dropout?", "By Quenby 2019, how many ...", "According to Doe 2019") is a citation's; an
# event's or a topic's word stays, as before a year in S2 (:func:`_common_noun`: "How did Covid 2020 change
# enrolment?"), and so does an exam's, a tool's (:data:`NOT_SOURCES`) or a holiday's word there
# (:data:`HOLIDAYS`: "By Brevet 2023", "What did Easter 2023 attendance look like?")
C9 = re.compile(
    rf"(?<![\w'’\-])(?:[Dd]id|[Dd]oes|[Dd]o|[Bb]y|[Aa]ccording\s+to|[Pp]er)\s+(?P<a>{NAME})(?=,?\s+{YEAR}\b)"
)
# a list of sources ("Sources: Doe 2019; Roe 2020", "References: Doe 2019, Roe 2020 and Poe 2021"): each of
# its items is read as a parenthesis's
SOURCES = re.compile(r"(?<![\w'’\-])(?:[Ss]ources?|[Rr]eferences?)[ \t]*:[ \t]*(?P<list>[^\n]{4,600})")
# C8: a possessive name before a year and a study's word: "Doe's 2021 survey" ("Lebanon's 2020 survey" stays)
C8 = re.compile(
    rf"{_BEFORE}(?P<a>{NAME})['’]s\s+{YEAR}\s+(?i:survey|study|report|assessment|review|paper|data)\b"
)
# an interview's speaker by first name and age: "(Fakename, 34)", "“…” – Fakename, 34"
SPEAKER = re.compile(
    r"(?:\(\s*|[\"”]\s*[-–—]\s*)([A-Z][\w'’-]{2,})(?=,\s*(?:aged?\s*)?\d{1,2}\b(?![.,]\d|\s*%))"
)
# the same after a closing quote and a comma ("“…,” said Fakename, 14.", "“…,” Fakename, 14, explained"),
# after "said", "says", "told" or "explained", or after "In an interview," or "interview(ed) with"
SPEAKER_SAID = re.compile(
    r"(?:[,!?…][\"”’][ \t]*(?:(?:said|says|told|explained|added)[ \t]+)?"
    r"|[\"”’],[ \t]*(?:(?:said|says|told|explained|added)[ \t]+)?"
    r"|\b(?:said|says|told|explained)[ \t]+|\b[Ii]n[ \t]+an[ \t]+interview,[ \t]*"
    r"|\binterview(?:ed)?[ \t]+with[ \t]+)"
    r"([A-Z][\w'’-]{2,}),[ \t]*(?:aged?[ \t]*)?\d{1,2}(?![.,]\d|\d|\s*%)(?=[ \t]*(?:[,.;)]|$))",
    re.MULTILINE,
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
    document's own authors (by its Document ID, which stays the same from one version to the next).
    ``ordinary``: the surnames the KM Database's texts also write in lower case, as ordinary words
    ("inclusion"); ``ordinary_alone``: of those, the ones that only an author entry of one bare word gives
    (no first name, no initial: most often an organisation's or a title's word taken for a name)."""

    names: frozenset[str] = frozenset()
    surnames: frozenset[str] = frozenset()
    own: dict[str, frozenset[str]] = field(default_factory=dict)
    ordinary: frozenset[str] = frozenset()
    ordinary_alone: frozenset[str] = frozenset()

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
    return _covered(found)


# the words of :data:`STOP` that are a first name or a family name too ("Said Vantrell", "Said & Zorgwell
# 2020", "According to Said 2021"): a name's wherever a name is read, never trimmed from it nor kept
_NAMES_TOO = frozenset({"said", "say", "tell"})
_EVERY = (
    (MONTHS | SEASONS | TIME_WORDS | _KEPT_PLACES | watch_people.COMMON_WORDS | STOP | CONNECTIVES)
    - _NAMES_TOO
    - _PLACES_NAMES_TOO
)


def _covered(found: list[str]) -> bool:
    """Whether every word of ``found`` (a span's words, folded) is a word of :data:`_EVERY` or a word of a
    place's name of several words written whole that one of its words marks as a place (:data:`PLACE_MARKERS`:
    "Bab al-Tabbaneh 2024", "Jabal Mohsen and Saida, 2023", "Western Bekaa"). A place's name made only of
    words a person's name has ("Saad Nayel", "Tal Abbas", "Wadi Khaled", "Bar Elias", "Mar Mikhael") is kept
    as the towns' one-word spellings are, only where it is written as a place (:func:`_as_place`)."""
    bare = [_POSSESSIVE.sub("", w) for w in found]
    ok = [True] + [False] * len(found)
    for i, word in enumerate(found):
        if not ok[i]:
            continue
        if word in _EVERY or word.strip("'’") in _EVERY or bare[i] in _EVERY:
            ok[i + 1] = True
        for size in range(2, min(PLACE_LONGEST, len(found) - i) + 1):
            place = tuple(bare[i : i + size])
            if place in PLACE_NAMES and not PLACE_MARKERS.isdisjoint(place):
                ok[i + size] = True
    return ok[-1]


_POSSESSIVE = re.compile(r"['’]s?$")  # "Lebanon's", "Syrians'"
# the words that make a place's name of several words a place wherever it is written: a word of places kept
# wherever it is written (:data:`_KEPT_PLACES`, never "wadi", a first name too) and the words of a place's
# kind or a region's that no name is ("Bab al-Tabbaneh", "Iqlim al-Kharroub", "Shebaa Farms", "Liban Nord",
# "the southern governorates")
PLACE_MARKERS = _KEPT_PLACES | frozenset(
    "bab iqlim farms valley liban nord sud gouvernorat governorate governorates region regions district "
    "districts area areas part parts border borders northern southern western eastern".split()
)


# a word that writes what follows it as a place before a bare year ("in Saida 2023", "from Tyre and Qana
# 2024", "across Qana 2023"), with other places between them ("in Majdal Anjar and Saadnayel 2023", "in Tyre,
# Qana and Houla 2024"); never "to" ("According to Naameh (2021)"). Before a comma and a year, as a citation
# writes a name ("in Naameh, 2021", "Data from Kousba, 2019"), only the words no citation is written after
# (``_AS_PLACE_COMMA``: "numbers for Sayda, 2024")
_AS_PLACE = re.compile(
    r"(?:^|[^\w'’\-])(?i:in|for|from|across|among|within|throughout|near|around|outside|inside)\s+"
)
_AS_PLACE_COMMA = re.compile(
    r"(?:^|[^\w'’\-])(?i:for|across|among|within|throughout|near|around|outside|inside)\s+"
)
# a citation's words right before that word ("As reported in Naameh 2021", "(cited in Akar 2018)", "Findings
# in Akar 2016 show", "Similar results were found in Naameh 2021", "Table adapted from Naameh 2021", "(e.g.,
# in Naameh 2021)", "Building on the work in Akar 2020", "The same pattern appears in Hasroun 2020", "See the
# review in Akar 2016", "A study in Naameh 2021", "Estimates from Akar 2016", "Figures from Kousba 2019", "The
# figures given for Akar, 2016"), or "as" within the two words before it ("As also noted in"): what follows
# is a source, never a place
_CITED_BEFORE = re.compile(
    r"(?:\b(?i:as|see|cf\.?|reported|noted|shown|cited|described|discussed|documented|presented|stated|quoted"
    r"|summari[sz]ed|reviewed|published|argued|highlighted|outlined|findings|evidence|research|found|mentioned"
    r"|indicated|explained|observed|suggested|seen|detailed|demonstrated|estimated|confirmed|adapted|taken"
    r"|drawn|derived|obtained|sourced|extracted|reproduced|results|data|work|works|study|studies|review"
    r"|reviews|paper|papers|article|articles|literature|appears?|appeared|figures|estimates|statistics|given"
    # a scholar's verbs ("This was examined in Naameh 2021", "Similar patterns were analysed in Kousba 2019")
    r"|examined|analy[sz]ed|explored|assessed|investigated|addressed|covered|identified|evaluated|considered"
    r"|illustrated|emphasi[sz]ed|stressed|underlined|acknowledged|concluded|claimed|revealed|compiled"
    r"|referenced|echoed|mirrored|corroborated|replicated|quantified|modell?ed|compared|critiqued|questioned"
    r"|debated|defined|characteri[sz]ed|framed|tested|validated|introduced"
    # and a publication's parts ("See the chapter in Hasroun 2021", "The framework in Akar 2020")
    r"|analysis|analyses|chapter|chapters|table|tables|figure|annex|box|section|discussion|argument"
    r"|framework|model|method|methods|approach|definition|report|reports|survey|surveys|assessment"
    r"|evaluation|account|accounts|overview|summary|synthesis|case|example|examples)"
    r"|\b(?i:e\.g|i\.e)\.)[\s,]*$"
    r"|\b(?i:as)\s+[\w'’]+[\s,]*$"  # "as" within the two words before it ("As also found in")
    # a publication's numbered part ("Table 2 in Naameh 2021", "Figure 3 in", "Annex 1 in")
    r"|\b(?i:table|figure|fig\.|annex|appendix|chapter|section|box)\s+[\dIVXivx]+[a-z]?[\s,]*$"
    # a verb in the passive before it ("This was collected in Sayda 2021", "The issue is captured in Qana
    # 2021"), but one that says what happened at a place ("Children were enrolled in Saida 2023")
    r"|\b(?i:is|was|are|were|been|being|be)\s+(?:[a-z]+ly\s+)?(?!(?i:enrolled|registered|located|based|held"
    r"|born|raised|displaced|settled|hosted|stationed|situated|closed|opened|built|damaged|destroyed|housed"
    r"|employed|married|killed|injured|affected|recruited|trained|vaccinated|treated)\b)[a-z]+ed[\s,]*$"
)
# a source's verb after the year ("Akar and Naameh 2020 found", "Girls in Naameh 2021 reported", "Jabal Mohsen
# (2020) suggested"): the names before it are a citation's, never a place's ("Numbers for Sayda, 2024 show a
# rise" is a place's)
_CITED_AFTER = re.compile(
    rf"(?:{ET_AL})?,?\s+\(?\s*(?:19|20)\d\d[a-z]?\s*\)?(?:,?\s+(?i:found|finds|find|reported|reports|suggested"
    r"|suggests|suggest|argued|argues|argue|noted|notes|concluded|concludes|estimated|documented|documents"
    r"|observed|observes|showed|highlighted|highlights|indicated|indicates|demonstrated|demonstrates|stated"
    r"|states|recommended|recommends|described|describes|revealed|reveals|identified|identifies|confirmed"
    r"|confirms|claimed|claims|explained|explains|discussed|discusses|examined|examines|analy[sz]ed|wrote"
    r"|writes|mentioned|mentions|proposed|proposes|calculated|attributed|attributes|linked|links|emphasi[sz]ed"
    r"|stressed|warned|predicted|projected)\b"
    # any verb before "that" ("Girls in Naameh 2021 measured that most worked", "Families from Houla 2021
    # adds that fees rose"), or "which" and a verb after a comma ("in Sayda 2021, which found high dropout")
    r"|\s+[a-z]+\s+that\b|,\s+(?:which|who)\s+(?:also\s+)?[a-z]+(?:ed|s)\b)"
)
# a list that goes on before a name, after an earlier name and year ("reported for Naameh, 2021 and for Akar,
# 2020"): the earlier one's words decide for it
_LIST_BEFORE = re.compile(r"(?:19|20)\d\d[a-z]?,?\s+(?:and|or|&)\s*$", re.IGNORECASE)
# a year with a letter ("2021a") or a page after it ("2021, p. 12", "2019, pp. 4-5"): only a citation's
_CITED_YEAR = re.compile(rf"(?:{ET_AL})?,?\s+(?:19|20)\d\d(?:[a-z]\b|\s*,\s*pp?\.)")
_LISTED = re.compile(r"\s*,\s*(?:(?:and|or|&)\s+)?|\s+(?:and|or|&)\s+", re.IGNORECASE)
# a list that goes on after a name ("Qana and Houla 2024" after "in Tyre, "): what follows it up to a year
_LIST_GOES_ON = re.compile(r"[^\d.;:()\[\]]*?\s(?:and|or|&)\s", re.IGNORECASE)


def _a_place(words: list[str]) -> bool:
    """Whether ``words`` (folded) are a place's: each a word of a place (:data:`PLACE_WORDS`, a town's or a
    district's one-word spelling: :data:`PLACE_NAMES_ONE`), or a place's name of several words."""
    return bool(words) and (
        all(w in PLACE_WORDS or w in PLACE_NAMES_ONE for w in words) or tuple(words) in PLACE_NAMES
    )


def _place_words(name: str) -> list[str]:
    return [fold(w) for w in _WORDS.findall(name)]


def _as_place(text: str, start: int, name: str, places: re.Pattern = _AS_PLACE) -> bool:
    """Whether ``name``, written at ``start`` of ``text``, is a place written as one (:func:`_a_place`, never
    a possessive: "in Naameh's 2021 study"), after the nearest word that writes a place (``places``), other
    places only between them. A list that ends with a comma there ("In Lebanon, ") is one only when it goes
    on with "and", "or" or "&" after the name ("in Tyre, Qana and Houla 2024"): a comma after a place that
    opens a sentence ("In Lebanon, Kousba, 2019 found") lists nothing. A citation's word before that word
    (:data:`_CITED_BEFORE`: "As reported in Naameh 2021", "(cited in Akar 2018)") writes a source."""
    if _POSSESSIVE.search(name) or not _a_place(_place_words(name)):
        return False
    if text.rfind("(", 0, start) > text.rfind(")", 0, start):  # in a parenthesis: "(for Naameh, 2021)"
        return False
    window = text[max(0, start - 120) : start]
    found = list(places.finditer(window))
    if not found or _CITED_BEFORE.search(window, 0, found[-1].start() + 1):
        return False
    # an earlier name and year that the list goes on from: a source's when a citation's word writes it
    for earlier, later in zip(reversed(found[:-1]), reversed(found[1:]), strict=False):
        if not _LIST_BEFORE.search(window, 0, later.start() + 1):
            break
        if _CITED_BEFORE.search(window, 0, earlier.start() + 1):
            return False
    between = window[found[-1].end() :]
    if not between.strip():
        return True
    parts = _LISTED.split(f" {between}")
    if not _LISTED.search(between[-6:] + " ") or not all(
        _a_place([fold(w) for w in _WORDS.findall(part)]) for part in parts if part.strip()
    ):
        return False
    if between.rstrip().endswith(","):
        return _LIST_GOES_ON.match(text, start + len(name), start + len(name) + 120) is not None
    return True


def _kept_as_source(name: str) -> bool:
    """:func:`kept`, where a source's form names it ("Jabal Mohsen et al.", "Deir Ammar (2019) reported"): a
    place's name of several words is kept there only when each of its words is a place's or a common word, not
    by one place's word among a person's names."""
    if not kept(name):
        return False
    from .authors import org_test

    found = [fold(w) for w in _WORDS.findall(name)]
    if org_test(name) or any(w in GENERIC_WORDS for w in found):
        return True
    if any(_MIXED_CAPS.match(w) or _PLURAL_ACRONYM.match(w) for w in _WORDS.findall(name)):
        return True
    return all(w in _EVERY or w.strip("'’") in _EVERY or _POSSESSIVE.sub("", w) in _EVERY for w in found)


def _names(
    match: re.Match,
    places: re.Pattern | None = None,
    surnames: frozenset[str] = frozenset(),
    source: bool = False,
    words: frozenset[str] = frozenset(),
    listed_before: tuple[tuple[int, int], ...] = (),
) -> list[tuple[int, int]]:
    """The spans of the names of a C1-C8 match that are not kept, nor a place written as one where the
    match's shape is a place's before a bare year (``places``, the words that write one there: C4, C5 and
    C6's "in Saida 2023", "numbers for Sayda, 2024", "in Majdal Anjar and Saadnayel 2023", where every name
    listed is a place: :func:`_as_place`), never one of the KM Database's surnames (``surnames``: "Dropout
    rose in Naameh 2021" when an author is named Naameh). A citation's shape ("Naameh et al.", "Naameh
    (2021)", a parenthesis's item, a year with a letter or a page after it: "in Naameh 2021a", "in Naameh
    2021, p. 12") is a person's, whatever word comes before it; where a source's form names it (``source``:
    "et al.", "(2019) reported"), a place's name of several words is kept only as :func:`_kept_as_source`
    says ("Jabal Mohsen et al." goes); a name made only of ``words`` is kept too (:data:`NOT_SOURCES`, after a
    sentence's "In" and a source's words: "In Brevet 2023", "Source: Fieldwork 2023"). ``listed_before``: the
    spans of the names listed with commas before the match's ("Vantrell, " before "Zorgwell and Quenby
    (2019)"), judged as its own."""
    text = match.string
    found = match.groupdict()
    spans_in = [match.span(group) for group in ("a", "b") if found.get(group)] + list(listed_before)
    names = [text[start:end] for start, end in spans_in]
    listed = (
        places is not None
        and all(_a_place(_place_words(name)) for name in names)
        and all(surnames.isdisjoint(_place_words(name)) for name in names)
        and not _CITED_YEAR.match(text, match.end())
        and not _CITED_AFTER.match(text, match.end())
        # "&" joins a citation's authors, never places ("Numbers for Akar & Naameh, 2020")
        and not (found.get("b") and "&" in text[match.end("a") : match.start("b")])
    )
    spans = []
    for (start, end), name in zip(spans_in, names, strict=True):
        if _kept_as_source(name) if source else kept(name):
            continue
        if words and _only(name, words):
            continue
        if listed and _as_place(text, start, name, places):
            continue
        spans.append((start, end))
    return spans


# a name and a comma right before a list's last names ("Vantrell, " before "Zorgwell and Quenby (2019)"),
# "and" after the comma too ("Quenby, and Marlowfake (2019)")
_ITEM_BEFORE = re.compile(rf"{_BEFORE}(?P<a>{NAME})[ \t]*,[ \t]*(?:(?:and|&)[ \t]+)?$")
_OXFORD_BEFORE = re.compile(r",[ \t]*(?:and|&)[ \t]+$")
LISTED_MAX = 8  # names read before a list's last two


def _listed_before(match: re.Match, comma: bool = False) -> tuple[tuple[int, int], ...]:
    """The spans of the names listed with commas right before a C3 or C5 match of two names ("Vantrell, " in
    "Vantrell, Zorgwell and Quenby (2019)"), or of one after a comma and "and" ("Quenby, and Marlowfake
    (2019)"), nearest first; a name that is neither kept nor a place without its first words that are no
    person's ("However Vantrell" → "Vantrell"). A place written after a word that writes a place opens the
    sentence, and the list stops there ("In Saida, Kousba and Naameh (2019) found"). ``comma``: a C3 match's
    one name after a comma too, its year in brackets a citation's ("Hill, Quenvell (2021)", "Describe
    Morvane, Tripoli (2020) argue")."""
    text, start = match.string, match.start("a")
    if (
        not comma
        and not match.groupdict().get("b")
        and not _OXFORD_BEFORE.search(text, max(0, start - 12), start)
    ):
        return ()
    spans: list[tuple[int, int]] = []
    while len(spans) < LISTED_MAX:
        found = _ITEM_BEFORE.search(text, max(0, start - 200), start)
        if found is None:
            break
        begin, end = found.span("a")
        name = found.group("a")
        words = list(_WORDS.finditer(name))
        if (
            len(words) > 1
            and fold(words[0].group()) in _PLACE_LEADS
            and _a_place(_place_words(name[words[1].start() :]))
        ):
            break
        if not kept(name) and not _a_place(_place_words(name)):
            for word, after in zip(words, words[1:], strict=False):
                if not _not_a_name(word.group()):
                    break
                begin = found.start("a") + after.start()
        spans.append((begin, end))
        start = found.start("a")
    return tuple(spans)


# the words a sentence's "In", a source's words ("Source:", "According to", "Data:", "Per") or a possessive
# before a year write that are no person's: an exam, a school's level or term, a subject, a way of collecting
# data, a tool, a sector, an area, a time of day or an event ("In Brevet 2023, how many passed?", "Source:
# Fieldwork 2023.", "Data: ActivityInfo 2023.", "Kobo's 2023 data"); never a holiday, a weekday or a month
# that is a family name too ("In Ramadan 2023", "Eid et al.": those stay withheld)
NOT_SOURCES = frozenset(
    "fieldwork interviews interview kindergarten preschool activityinfo kobo kobotoolbox brevet "
    "baccalaureate bacc midterm midterms finals semester term terms cycle cycles primary intermediate "
    "secondary shelter livelihoods transport district region area city village physics biology chemistry "
    "geography history civics economics morning afternoon evening shift shifts partner partners pandemic "
    "lockdown lockdowns floods flooding earthquake explosion crisis outbreak cholera covid".split()
)
# the holidays a question dates something by ("By Christmas 2023", "What did Easter 2023 attendance look
# like?"): kept only after C9's words, where no citation's name is written so; a family name too elsewhere
# ("Christmas et al. (2019)", "Source: Easter 2023")
HOLIDAYS = frozenset({"christmas", "easter"})
# the words C6 keeps after its word that opens it: a sentence's "In" (:data:`NOT_SOURCES`: "In Brevet 2023"),
# and "by" or "per" in lower case that opens the text or a sentence, as C9 keeps them after "By" and "Per" (a
# holiday too: "by Christmas 2023", "per Kobo 2022"; "A paper by Brevet 2020" is a citation's)
_C6_WORDS = {"In": NOT_SOURCES, "by": NOT_SOURCES | HOLIDAYS, "per": NOT_SOURCES | HOLIDAYS}


def _c6_words(match: re.Match) -> frozenset[str]:
    """The words a C6 match keeps (:data:`_C6_WORDS`): after "by" and "per" only where they open the text or a
    sentence."""
    lead = match.group(0).split()[0]
    if lead in ("by", "per") and not _OPENS_AT.match(match.string, match.start()):
        return frozenset()
    return _C6_WORDS.get(lead, frozenset())


def _only(name: str, words: frozenset[str]) -> bool:
    """Whether every word of ``name`` is one of ``words`` (folded, a possessive left out)."""
    found = [fold(_POSSESSIVE.sub("", w)) for w in _WORDS.findall(name)]
    return bool(found) and all(w in words for w in found)


def _withhold(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = f"{text[:start]}{NAME_WITHHELD}{text[end:]}"
    return text


def _cited(item: str, cited: set[str], words: frozenset[str] = frozenset()) -> str | None:
    """An item (or a part of one) that starts with names and a year (C2_START), its names not kept (nor made
    only of ``words``) withheld, and every other name before a year in it (C2_MORE); None when it does not
    start so."""
    found = C2_START.match(item)
    if found is None:
        return None
    start = found.start("names")
    spans = {
        (start + m.start(), start + m.end())
        for m in NAME_RE.finditer(found.group("names"))
        if not kept(m.group()) and not (words and _only(m.group(), words))
    }
    spans |= {span for m in C2_MORE.finditer(item, found.end()) for span in _names(m, words=words)}
    cited.update(item[a:b] for a, b in spans)
    return _withhold(item, sorted(spans))


def _item(item: str, cited: set[str], words: frozenset[str] = frozenset()) -> str:
    """One item of a parenthesis (between ";") or of a list of sources, its citations' names withheld (but a
    name made only of ``words``): "Doe, 2019" (C2_ITEM), names and a year at its start, or at the start of one
    of its parts between commas."""
    found = C2_ITEM.match(item)
    if found:
        spans = _names(found, words=words)
        cited.update(item[a:b] for a, b in spans)
        return _withhold(item, spans)
    if len(item) > ITEM_MAX or not _HAS_YEAR.search(item):
        return item
    withheld = _cited(item, cited, words)
    if withheld is not None:
        return withheld
    if "," not in item:
        return item
    parts = item.split(",")
    return ",".join(_cited(part, cited, words) or part if _HAS_YEAR.search(part) else part for part in parts)


# the words that write a place before a pair of names ("In Tyre and Qana 2023, fees rose."): trimmed from the
# first name only when what follows them is kept
_PLACE_LEADS = frozenset(
    "in for from across among within throughout near around outside inside between".split()
)
_OPENS_AT = re.compile(_OPENS)


def _trimmed_pair(match: re.Match) -> re.Match:
    """C5's match without the words its first name opens with that are no person's, read again from there: a
    connective that opens a sentence ("However Beirut and Tyre 2023"), or a word that writes a place when what
    follows it is kept ("In Tyre and Qana 2023": "Tyre"); never a word that is a first name too ("Said
    Vantrell", "In Said Vantrell" stay whole)."""
    text = match.string
    words = list(_WORDS.finditer(match.group("a")))
    base = match.start("a")
    skip = 0
    while skip < len(words) - 1:
        word = fold(words[skip].group())
        rest = text[base + words[skip + 1].start() : match.end("a")]
        if word in CONNECTIVES and _OPENS_AT.match(text, base + words[skip].start()):
            skip += 1
        elif word in _PLACE_LEADS and kept(rest):
            skip += 1
        else:
            break
    if not skip:
        return match
    again = C5.match(text, base + words[skip].start())
    return again if again and again.end() == match.end() else match


def _title(name: str) -> bool:
    """Whether ``name`` is a title's name of two words or more ("Child Development Survey", "Fake Response
    Plan"): kept by a title's generic word (:data:`GENERIC_WORDS`)."""
    found = _place_words(name)
    return len(found) >= 2 and kept(name) and not GENERIC_WORDS.isdisjoint(found)


# a determiner right before a pair's first name: the pair is a title's ("the Fakeword and Child Development
# Survey 2023"), never two people's
_DETERMINER_BEFORE = re.compile(r"(?<![\w'’\-])(?i:the|a|an|this|its|their|our)\s+$")


def _pair(
    match: re.Match, surnames: frozenset[str] = frozenset(), listed: bool = True
) -> list[tuple[int, int]]:
    """C5's two names, each judged on its own (:func:`_names`: "from Tyre & Doering 2020" withholds
    "Doering", "UNICEF & Vantrell 2020" keeps "UNICEF", "from Akar & Doering 2020" withholds both, "in Tyre,
    Qana and Houla 2024" none), and the names listed with commas before them ("Vantrell, Zorgwell and Quenby
    2019 found"), without the words the first opens with that are no person's (:func:`_trimmed_pair`). "&"
    joins a citation's authors, never places: only a place kept wherever it is written stays there ("Data
    from Tyre & Doering 2020" keeps "Tyre", "Data from Akar & Naameh 2020" withholds both). With "and", a
    town's other spelling or a place's name of several words stays only where a word that writes a place
    comes before the pair ("Dropout in Saida and Tyre 2023"; never after "by" or with no such word: "Edited by
    Houla and Tyre 2021", "What about Saida and Tyre 2023?"), never with a citation's word before it or a
    source's verb after the year ("Akar and Tyre 2020 found"), never one of the KM Database's surnames. A
    word that is a first name too ("Said") is a name's. With "and", a determiner before it and no citation's
    form, a title stays whole ("the Fakeword and Child Development Survey 2023"; "Quenby and Child Development
    Survey 2023" withholds "Quenby"). ``listed`` false: no names listed before it are read (C5c's pair, joined
    by a comma, is the list)."""
    match = _trimmed_pair(match)
    text = match.string
    before = _listed_before(match) if listed else ()
    names = (match.group("a"), match.group("b"))
    joined = "&" in text[match.end("a") : match.start("b")]
    cited = (
        _CITED_YEAR.match(text, match.end())
        or _CITED_AFTER.match(text, match.end())
        or _cited_before(text, match.start("a"))
    )
    if (
        not joined
        and not cited
        and any(_title(name) for name in names)
        and _DETERMINER_BEFORE.search(text, max(0, match.start("a") - 12), match.start("a"))
        and all(surnames.isdisjoint(_place_words(name)) for name in names)
    ):
        return []
    return _names(match, None if joined else _AS_PLACE, surnames, listed_before=before)


def _everywhere(name: str) -> bool:
    """Whether ``name`` is a place kept wherever it is written (each word a word of :data:`_KEPT_PLACES`)."""
    found = _place_words(name)
    return bool(found) and all(w in _KEPT_PLACES for w in found)


def _cited_before(text: str, start: int) -> bool:
    """Whether a citation's words (:data:`_CITED_BEFORE`) come right before ``start`` of ``text``, or before
    the word that writes a place there ("As reported in Saida and Tyre 2023")."""
    window = text[max(0, start - 120) : start]
    found = list(_AS_PLACE.finditer(window))
    if found and not window[found[-1].end() :].strip():
        window = window[: found[-1].start() + 1]
    return bool(_CITED_BEFORE.search(window))


# a list of names with a year after each, after a word that cites or writes a place ("Rates in Akar 2016,
# Naameh 2018 and Kousba 2019 rose", "This mirrors the rates in Akar 2016 and Kousba 2019"); a name already
# withheld is one of its items ("in Akar 2016, Naameh 2018 and [name withheld] 2019")
_YEAR_LIST = re.compile(
    rf"(?<![\w'’\-])(?:in|for|by|from|than|with|across|among|pooling|{_CITING}|In)\s+"
    rf"(?P<list>{NAME_OR_WITHHELD},?\s+{YEAR}(?:\s*,\s*(?:in\s+)?{NAME_OR_WITHHELD},?\s+{YEAR})*"
    rf"\s*,?\s+(?:and|or|&)\s+(?:in\s+)?{NAME_OR_WITHHELD},?\s+{YEAR})\b"
)
_LIST_ITEM = re.compile(rf"(?<![\w'’\-])(?P<a>{NAME_OR_WITHHELD}),?\s+{YEAR}")


def _year_lists(text: str, surnames: frozenset[str] = frozenset()) -> list[tuple[int, int]]:
    """The names of a list with a year after each (:data:`_YEAR_LIST`): a list of sources, every name not kept
    withheld; a name stays as a place only when it is a place and every other one is a place kept wherever it
    is written, with no citation's words before the list nor a source's verb after it ("Any data on child
    marriage in Saida 2023 and Tyre 2024?" keeps "Saida"), no name of it already withheld, and it is none of
    the KM Database's surnames."""
    spans = []
    for found in _YEAR_LIST.finditer(text):
        items = list(_LIST_ITEM.finditer(text, found.start("list"), found.end("list")))
        if len(items) < 2:
            continue
        cited = (
            _cited_before(text, found.start("list"))
            or bool(_CITED_AFTER.match(text, items[-1].end("a")))
            or any(item.group("a") == NAME_WITHHELD for item in items)
        )
        for i, item in enumerate(items):
            name = item.group("a")
            if name == NAME_WITHHELD or kept(name):
                continue
            others = [other.group("a") for j, other in enumerate(items) if j != i]
            if (
                not cited
                and _a_place(_place_words(name))
                and surnames.isdisjoint(_place_words(name))
                and all(_everywhere(other) for other in others)
            ):
                continue
            spans.append(item.span("a"))
    return spans


def _citations(
    text: str, surnames: frozenset[str] = frozenset(), ordinary: frozenset[str] = frozenset()
) -> str:
    """Step 5: the names of citations (C1-C9, a list of sources), withheld; then the same names wherever else
    the text writes them ("pooling Doe 2020 ...; Doe 2020 (Lebanon) relates ..."). A town's spelling that is
    one of the KM Database's surnames (``surnames``) is never a place there; a word the KM Database's texts
    also write as an ordinary word (``ordinary``) may be a topic's before a year (C9). The towns of a plain
    request about places that makes up a line (:func:`_requests`) all stay."""
    cited: set[str] = set()

    def hold(value: str, spans: list[tuple[int, int]]) -> str:
        towns = _requests(value, surnames)
        spans = [(a, b) for a, b in spans if not any(start <= a and b <= end for start, end in towns)]
        cited.update(value[a:b] for a, b in spans)
        return _withhold(value, spans)

    if _HAS_ET_AL.search(text):
        text = hold(text, [s for m in C1.finditer(text) for s in _names(m, source=True)])
    if not _HAS_YEAR.search(text):
        return _again(text, cited)
    if "(" in text:

        def paren(match: re.Match) -> str:
            return "(" + ";".join(_item(item, cited) for item in match.group(1).split(";")) + ")"

        text = PAREN.sub(paren, text)
        text = hold(
            text,
            [
                s
                for m in C3.finditer(text)
                for s in _names(
                    m,
                    source=bool(_CITED_AFTER.match(text, m.end())),
                    listed_before=_listed_before(m, comma=True),
                )
            ],
        )
    if ":" in text:

        def listed(match: re.Match) -> str:
            items = match.group("list")
            return match.group(0)[: match.start("list") - match.start()] + ";".join(
                _item(item, cited, NOT_SOURCES) for item in items.split(";")
            )

        text = SOURCES.sub(listed, text)
    if "," in text:
        text = hold(text, [s for m in C4.finditer(text) for s in _names(m, _AS_PLACE_COMMA, surnames)])
    if " and " in text or "&" in text:
        text = hold(text, [s for m in C5.finditer(text) for s in _pair(m, surnames)])
        text = hold(text, _year_lists(text, surnames))
    if "," in text:
        text = hold(text, [s for m in C5C.finditer(text) for s in _pair(m, surnames, listed=False)])
    text = hold(
        text,
        [s for m in C6.finditer(text) for s in _names(m, _AS_PLACE, surnames, words=_c6_words(m))],
    )
    text = hold(text, [s for m in C7.finditer(text) for s in _names(m, words=NOT_SOURCES)])
    if "'s" in text or "’s" in text:
        text = hold(
            text,
            [
                span
                for m in C8.finditer(text)
                if (span := _person(m)) and not _only(text[span[0] : span[1]], NOT_SOURCES)
            ],
        )
    text = hold(
        text,
        [
            s
            for m in C9.finditer(text)
            if not _common_noun(_WORDS.findall(m.group("a"))[-1], text, ordinary)
            for s in _names(m, words=NOT_SOURCES | HOLIDAYS)
        ],
    )
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
    """Step 6: an interview's speaker named by a first name and an age ("(Fakename, 15)", "“…” – Fakename,
    34", "“…,” said Fakename, 14.", "In an interview, Fakename, 14, said"), never a role ("said Mother,
    34")."""
    if "," not in text:
        return text
    spans = set()
    for match in (*SPEAKER.finditer(text), *SPEAKER_SAID.finditer(text)):
        name = match.group(1)
        if fold(name) not in ROLE_WORDS and not kept(name):
            spans.add(match.span(1))
    return _withhold(text, sorted(spans))


# a capitalised word, with an initial before it ("J. Doe") and a possessive after it ("Doe's") left out
OWN = re.compile(rf"(?:\b[A-Z]\.\s*)?(?<![\w'’\-])([{_CAP}][\w\-]*)(?=(?:['’]s)?(?![\w\-]))")


def _own(text: str, own: frozenset[str], ordinary: frozenset[str] = frozenset()) -> str:
    """Step 7: the document's own authors by their surname ("J. Doe", "Doe's", "the Doe-led survey",
    "Doe2019", "Doe-2019"), withheld; and in a word joined by digits or "_" in any case ("LCRP_Doe_2019",
    "survey_doe"), but a surname the texts also write as an ordinary word (``ordinary``) in lower case."""
    if not own:
        return text

    def one(match: re.Match) -> str:
        word = match.group(1)
        if fold(word) in own:
            return NAME_WITHHELD
        head = _hyphen_head(word, own)
        if head:
            return NAME_WITHHELD + word[head:]
        return _glued(word, own) or match.group(0)

    text = OWN.sub(one, text)
    lower = own - ordinary
    if not lower:
        return text
    return _GLUED_WORD.sub(lambda match: _glued(match.group(1), lower) or match.group(0), text)


# a word that letters and digits or "_" make ("LCRP_Doe_2019", "survey_doe", "doe2019"), with an initial
# before it ("j.doe2019"): one of them may be a surname written in a file's name or a citation's key
_GLUED_WORD = re.compile(r"(?:(?<![\w.])[A-Za-z]\.\s*)?(?<![\w'’\-])((?=[\w\-]*[\d_])[\w\-]*[^\W\d_][\w\-]*)")
_RUN = re.compile(r"[^\W\d_]+")


def _glued(word: str, surnames: frozenset[str]) -> str:
    """``word`` with each run of its letters that is a surname of ``surnames`` withheld, when digits or "_"
    join it to the rest ("Doe2019", "LCRP_Doe_2019", "doe_survey", "doe2019doe"); "" when it holds none."""
    if not re.search(r"[\d_]", word):
        return ""
    found = False

    def run(match: re.Match) -> str:
        nonlocal found
        if fold(match.group()) in surnames:
            found = True
            return NAME_WITHHELD
        return match.group()

    out = _RUN.sub(run, word)
    return out if found else ""


def _hyphen_head(word: str, surnames: frozenset[str]) -> int:
    """The length of a surname of ``surnames`` that starts ``word`` before a hyphen and a word in lower case
    or a number ("Doe-led", "Doe-Roe-based", "Doe-2019": the "-led", "-based" or "-2019" stays), or 0."""
    parts = word.split("-")
    for n in range(len(parts) - 1, 0, -1):
        head = "-".join(parts[:n])
        if (parts[n][:1].islower() or parts[n][:1].isdigit()) and fold(head) in surnames:
            return len(head)
    return 0


# the titles written with a full stop that NeuroDB's rule (``watch.people``) does not read ("Amb. Show Haddad
# 2023", "Sen. Rank Tyre and Halba 2020", "Pr. Summarize Haddad 2020"): the capitalised words after one, on
# its line, are a name, whatever they also are (a request's verb, a town), unless they are kept (a place, an
# organisation, a common word: "in Eng. and Fr. The report", "Gen. Assembly Resolution")
_MORE_TITLES = "Amb|Sen|Pr|Drs|Rev|Revd|Fr|Sr|Hon|Capt|Col|Gen|Lt|Sgt|Maj"
_SPACE = "[ \t\xa0\u2007\u2009\u202f]"  # a space, a tab, a no-break space or a thin one ("Dr.\u202fShow")
_TITLE_NAME = re.compile(rf"(?<![\w'’\-.])(?:{_MORE_TITLES})\.{_SPACE}*(?P<a>{NAME})")
_TITLE_WORDS = frozenset(_MORE_TITLES.casefold().split("|"))


def _more_titles(text: str) -> str:
    """A name after one of :data:`_MORE_TITLES` withheld ("Amb. [name withheld] 2023")."""

    def one(match: re.Match) -> str:
        if kept(match.group("a")):
            return match.group(0)
        return match.group(0)[: match.start("a") - match.start()] + NAME_WITHHELD

    return _TITLE_NAME.sub(one, text)


def _titles(text: str) -> str:
    """Step 9: a name after a title ("Dr …", "Mrs …", "Amb. …")."""
    text = watch_people.PERSON_TITLE_NAME.sub(NAME_WITHHELD, text)
    text = watch_people.HONORIFIC_NAME.sub(NAME_WITHHELD, text)
    return _more_titles(text)


# ------------------------------------------------------------------- step 10: names in other shapes
# S1: a reference list's "Doe, J." or "Roe Poe, M. C." (initials with their full stop after a comma), ending
# an item (before a comma, ";", "&", "and", "(" or the end) or before the quoted title the person wrote
# ("Doe, J. 'Fake report', 2021")
_ITEM_END = r"(?=[ \t]*(?:[,;&()]|and\b|$))"
_REF_END = r"(?=[ \t]*(?:[,;&()'\"‘“]|and\b|$))"
S1 = re.compile(rf"{_BEFORE}(?P<a>{NAME}),[ \t]+[A-Z]\.(?:[ \t]?-?[A-Z]\.){{0,2}}{_REF_END}", re.MULTILINE)
_HAS_INITIAL = re.compile(r",[ \t]+[A-Z]\.")
# S1b: "Doe J" or "(Doe JM)", initials without a full stop, when Doe is one of the KM Database's surnames
# , or before a full stop and a capitalised word, as a reference written the Vancouver way ends an author
# ("Akar J, Naameh R. Fake title. 2021.")
_INITIALS_END = rf"[A-Z]{{1,2}}(?:\.?{_ITEM_END}|\.(?=[ \t]+[{_CAP}]))"
S1B = re.compile(rf"{_BEFORE}(?P<a>{WORD})[ \t]+{_INITIALS_END}", re.MULTILINE)
_LONE_CAPITALS = re.compile(rf"[ \t]{_INITIALS_END}", re.MULTILINE)
# S1v: a reference list written the Vancouver way, two authors or more, then a full stop, ";", ":" or "et
# al." ("Quenby Q, Morvane J. Fake title. 2019.", "Quenby QM, Morvane JR, Brannock K.", "Quenby AB, Zayna
# C."): every author's surname, with or without a year, whatever the KM Database's authors, and with an
# author already withheld ("Zayna AB, [name withheld] C."); never a word that is no name ("Grade A, Grade
# B.")
_SURNAME = rf"(?:(?:{'|'.join(PARTICLES)})[ \t]+)?{WORD}(?:[ \t]+(?:(?:{'|'.join(PARTICLES)})[ \t]+)?{WORD})?"
_VANCOUVER = rf"(?:{_SURNAME}|{re.escape(NAME_WITHHELD)})[ \t]+[A-Z]{{1,2}}(?![\w'’\-])"
S1V = re.compile(
    rf"{_BEFORE}{_VANCOUVER}(?:[ \t]*[,;][ \t]*{_VANCOUVER})+(?=\.|[ \t]*[;:]|,?[ \t]+et[ \t]+al\b|[ \t]*$)",
    re.MULTILINE,
)
# an author of it, a surname of two words too ("Abou Khalil QK", "Van Orrinvale M", "De Wexborne D")
S1V_ITEM = re.compile(rf"(?P<a>{_SURNAME})[ \t]+[A-Z]{{1,2}}(?![\w'’\-])")
# S2: a name and a year before a colon, a ";" or a place in parentheses, the year not a date's ("Fake
# fieldwork May-June 2020;"): "Doe 2020: 20-60y; Roe 2022: ...", "Roe 2021 (Lebanon) fits"
S2 = re.compile(rf"{_BEFORE}(?P<a>{NAME})[ \t]+{YEAR}(?:[ \t]*[:;]|[ \t]+\((?P<place>[^()]{{2,40}})\))")
_HAS_YEAR_STOP = re.compile(rf"{YEAR}(?:[ \t]*[:;]|[ \t]+\()")
# S3: "(Doe, Lebanon)": a parenthesis that starts with a KM Database surname and a comma; then every other
# parenthesis of that text written so ("(Fakename, Jordan)")
S3 = re.compile(rf"\((?P<a>{WORD}),[ \t]+(?P<b>{NAME})\)")
# S4: a list of names after a word that cites them ("studies by Doe, Roe and Poe") when one of them is a KM
# Database surname or a name already withheld ("studies by Roe, Poe and [name withheld]": a document's own
# author)
S4 = re.compile(
    rf"(?<![\w'’\-])(?:by|from|including|e\.g\.,?|see|cf\.?|such\s+as)[ \t]+"
    rf"(?P<names>{NAME_OR_WITHHELD}(?:[ \t]*,[ \t]*{NAME_OR_WITHHELD})*,?[ \t]+(?:and|&)[ \t]+"
    rf"{NAME_OR_WITHHELD})"
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
# a title right before a name withheld ("Dr. [name withheld]", once a citation's rule withheld "Dr. Jordan
# (2019)"'s name): it goes with the name, as it goes with a name after it
_TITLES_BEFORE = "|".join(sorted(watch_people.TITLE_WORDS | _TITLE_WORDS, key=len, reverse=True))
_TITLE_BEFORE = re.compile(
    rf"(?<![\w'’\-.])(?=[A-Z])(?i:{_TITLES_BEFORE})\.?{_SPACE}*(?={re.escape(NAME_WITHHELD)})"
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


# the events and topics a text writes before a year ("Cholera 2022 (Bebnine)", "Floods 2023 (Saida)",
# "Displacement 2024 (Saida)"): never a name before a year (S2). An event's word that is a family name too in
# the singular ("Storm", "Flood", "Hunger") only in the plural ("Floods") or when the text also writes it in
# lower case
EVENT_WORDS = frozenset(
    "cholera measles polio covid coronavirus ebola floods flooding earthquake earthquakes explosion "
    "explosions blasts wars conflict conflicts crisis crises outbreak outbreaks drought droughts storms "
    "fires wildfires election elections strikes protest protests uprising unrest violence shelling "
    "airstrikes hostilities escalation ceasefire lockdown lockdowns closure closures heatwave famine "
    "inflation devaluation collapse eviction evictions returns arrivals influx dropout attendance enrolment "
    "enrollment displacement".split()
)
_EVENT_NAMES = frozenset("flood storm fire war strike blast hunger".split())
# a common noun's ending: in the plural, a word's ("Evaluations", "Assessments"); in the singular, a family
# name's too ("Blankenship", "Garrity", "Holderness")
_PLURAL_NOUN_END = re.compile(r"(?:tions|sions|ities|isms|ships)$|.{4,}(?:ments|nesses)$")  # never "Clements"
_NOUN_END = re.compile(r"(?:tion|sion|ity|ism|ics|ship|logy|ment|ness)$")


def _common_noun(word: str, text: str = "", ordinary: frozenset[str] = frozenset()) -> bool:
    """Whether ``word`` is an event's or a topic's word (:data:`EVENT_WORDS`) or a common noun in the plural
    ("Evaluations"); in the singular, an event's word that is a family name too ("Storm") or a word with a
    common noun's ending ("-tion", "-ity", "-ship", "-ness") only when it is a common word (``COMMON_WORDS``,
    the KM Database's surnames also written as ordinary words, ``ordinary``) or ``text`` also writes it in
    lower case: "Blankenship", "Garrity", "Holderness" and "Storm" before a year are names."""
    folded = fold(_POSSESSIVE.sub("", word))
    if folded in EVENT_WORDS or (len(folded) >= 7 and _PLURAL_NOUN_END.search(folded)):
        return True
    if folded not in _EVENT_NAMES and not (len(folded) >= 7 and _NOUN_END.search(folded)):
        return False
    if folded in watch_people.COMMON_WORDS or folded in ordinary:
        return True
    bare = _POSSESSIVE.sub("", word)
    return (
        bare.lower() != bare and re.search(rf"(?<![\w'’]){re.escape(bare.lower())}(?!\w)", text) is not None
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
    if _LONE_CAPITALS.search(text):
        for listed in S1V.finditer(text):
            for m in S1V_ITEM.finditer(text, listed.start(), listed.end()):
                name = m.group("a")
                if not (_not_a_name(name, text) if " " not in name and "\t" not in name else kept(name)):
                    spans.append(m.span("a"))
                    found.add(name)
    if _HAS_YEAR_STOP.search(text):
        for m in S2.finditer(text):
            last = _WORDS.findall(m.group("a"))[-1]
            # a date ("Fake fieldwork May-June 2020;"), an event or a common noun ("Cholera 2022 (Bebnine)",
            # "Floods 2023 (Saida)", "Displacement 2024 (Saida)"): no name
            if _not_a_name(last) or _common_noun(last, text, people.ordinary):
                continue
            place = m.group("place")
            if place is not None and not all(
                fold(w) in PLACE_WORDS or fold(w) in PLACE_NAMES_ONE for w in _WORDS.findall(place) or ["-"]
            ):
                continue
            person = _person(m)
            if person:
                spans.append(person)
                found.add(text[person[0] : person[1]])
    if surnames and "(" in text and "," in text:
        matches = list(S3.finditer(text))
        if any(fold(m.group("a")) in people.surnames for m in matches):
            for m in matches:
                if not kept(m.group("a")) and (
                    fold(m.group("a")) in people.surnames
                    or kept(m.group("b"))
                    or _a_place(_place_words(m.group("b")))
                ):
                    spans.append(m.span("a"))
                    found.add(m.group("a"))
    return spans


def _cited_lists(text: str, people: People, found: set[str], surnames: bool) -> list[tuple[int, int]]:
    """S4: the names of a list after a citing word, when one of them is a KM Database surname or a name
    already withheld."""
    spans: list[tuple[int, int]] = []
    if not (surnames or NAME_WITHHELD in text) or not (" and " in text or "&" in text):
        return spans
    for m in S4.finditer(text):
        names = m.group("names")
        if NAME_WITHHELD not in names and not (surnames and _surname_in(names, people)):
            continue
        offset = m.start("names")
        position = 0
        for part in _LIST_SPLIT.split(names):
            at = names.index(part, position)
            position = at + len(part)
            if part and part != NAME_WITHHELD and not kept(part):
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
    text = _TITLE_BEFORE.sub("", text)  # "Dr. [name withheld]" → "[name withheld]"

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


# e-mail addresses, written out too: NeuroDB's one rule for every text it sends to the AI, first written here
# (``watch.people``). A record's or a document's text by the record rule ("published at Example.Org" stays), a
# question by the typed rule (any word but a common one before "at" and a domain goes with it, letters that
# look like those of an address read as the plain ones)
_emails = functools.partial(watch_people.withhold_addresses, links=False)
without_addresses = watch_people.without_addresses


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
    text = watch_people.glued_after(redact.LINK.sub(LINK_WITHHELD, text))  # "[link withheld].it" loses ".it"
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
    text = _citations(text, people.surnames, people.ordinary)
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
        text = _own(text, own, people.ordinary)
    if not (watch_people.TITLE_WORDS.isdisjoint(found) and _TITLE_WORDS.isdisjoint(found)):
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
    if _TITLE_NAME.search(text) and _more_titles(text) != text:
        return True
    if _citations(text, people.surnames, people.ordinary) != text or _speakers(text) != text:
        return True
    return _shapes(text, people, _LETTERS.findall(_folded(text))) != text


QUESTION_CHARS = 1000
# K2, a plain request about places: a request's verb at the start of the text (:data:`REQUEST_VERBS`, in
# lower case once it is read apart: :func:`typed_for_ai`), "What about", "Any data on", "How did" or "Dropout
# in", or nothing; then two to four towns (:func:`_a_request_town`) joined by "," or "and" (never "&": a
# citation's authors); a bare year; then nothing, "?", "." or a measure's word ("dropout", "enrolment",
# "attendance rates", "data"), or "compare?" ("How did Chtoura and Zahle 2022 compare?"). Nothing else: never
# "See" (a citation's signal), a word before the verb (a given name, an initial, a title: "Zayna Q. Show Tyre
# and Halba 2020", "Dr. Show Tyre and Halba 2020"), a place's word that is a name too ("Show Jordan and Tyre
# 2023") nor a citation's tail after the year ("2019: 4-5", "2019 who found", "2019; Zahle 2020", "2019
# findings"). As a line of a text that the rule reads later, its verb in lower case (the question kept;
# "Question: compare Chtoura and Zahle 2022" as the assistant's message writes it; "- ..." in a list of
# earlier questions), every town of it stays (:func:`_requests`)
REQUEST_VERBS = frozenset(
    "show compare summarise summarize describe map chart count list rank plot explain analyse analyze "
    "contrast visualise visualize graph display tabulate".split()
)
# Lebanon's main towns (:data:`neurodb.watch.people.MAIN_PLACE_WORDS`, no person's name) and the towns that a
# request names with them whose spellings are family names too elsewhere ("Chtoura", "Anjar", "Majdal Anjar",
# "Qobayat"); any other town's one-word spelling of :mod:`neurodb.km.places` counts too, but one that is a
# family name's (:data:`neurodb.watch.people.TOWN_WORDS`: "Saida", "Naameh", "Qana", "Houla"; see
# :func:`_a_request_town`)
REQUEST_TOWNS = watch_people.MAIN_PLACE_WORDS | frozenset(
    {"chtoura", "chtaura", "anjar", "majdal anjar", "qobayat"}
)
_LINE_PARTICLE = rf"(?:(?:{'|'.join(PARTICLES)})[ \t]+)"
_LINE_NAME = rf"{_LINE_PARTICLE}?{WORD}(?:[ \t]+{_LINE_PARTICLE}?{WORD}){{0,2}}"
_REQUEST_JOIN = re.compile(r"[ \t]*,[ \t]*(?:and[ \t]+)?|[ \t]+and[ \t]+")
REQUEST = re.compile(
    r"[ \t]*(?:-[ \t]+)?(?:(?:Question|Q)[ \t]*:[ \t]*)?"
    rf"(?:(?P<verb>(?i:{'|'.join(sorted(REQUEST_VERBS))}))[ \t]+"
    r"|(?i:what[ \t]+about|how[ \t]+about|(?:any[ \t]+)?data[ \t]+on|how[ \t]+did"
    r"|(?:dropout|enrolment|enrollment|attendance|repetition)(?:[ \t]+rates?)?[ \t]+in)[ \t]+)?"
    rf"(?P<towns>{_LINE_NAME}(?:(?:[ \t]*,[ \t]*(?:and[ \t]+)?|[ \t]+and[ \t]+){_LINE_NAME}){{1,3}})"
    r"[ \t]+(?:19|20)\d\d"
    r"(?:(?:[ \t]+(?i:dropout|enrolment|enrollment|attendance|data|schools|pass|repetition)"
    r"(?:[ \t]+(?i:rates?))?)?[ \t]*[?.]?"
    r"|[ \t]+compare[ \t]*\?)[ \t]*"
)
REQUEST_LINE = re.compile(rf"^(?:{REQUEST.pattern})$", re.MULTILINE)
# the same request with a subject's words after the year, as people ask it ("Compare Beirut and Bourj
# Hammoud 2023 out of school", "Show Beirut and Tyre 2023 by gender", "List Beirut and Tyre 2023 projects"): a
# request's verb at the start of the text, the towns, a bare year, then words in lower case only, none of them
# a citation's ("who found", "findings", "as reported", "paper", "et al", "p. 4"); its verb is read apart only
# where every item is a town and the rule then keeps the text as it is (:func:`typed_for_ai`)
_CITING_TAIL = (
    "who|which|that|found|finds|find|say|says|said|argue|argued|argues|report|reports|reported|note|notes|noted"
    "|show|shows|showed|conclude|concluded|cite|cites|cited|citing|as|et|al|p|pp|paper|papers|study|studies"
    "|article|articles|review|reviews|model|models|framework|frameworks|finding|findings|survey|surveys|work"
    "|works|book|chapter|edition|vol|volume|ed|eds"
)
REQUEST_HEAD = re.compile(
    rf"[ \t]*(?P<verb>(?i:{'|'.join(sorted(REQUEST_VERBS))}))[ \t]+"
    rf"(?P<towns>{_LINE_NAME}(?:(?:[ \t]*,[ \t]*(?:and[ \t]+)?|[ \t]+and[ \t]+){_LINE_NAME}){{1,3}})"
    rf"[ \t]+(?:19|20)\d\d(?:[ \t]+(?!(?:{_CITING_TAIL})(?![\w\-]))[a-z][a-z\-]*)+[ \t]*[?.]?[ \t]*"
)


def _a_request_town(name: str, surnames: frozenset[str] = frozenset()) -> bool:
    """Whether ``name`` is a town a plain request about places names: one of :data:`REQUEST_TOWNS`, a town's
    one-word spelling of :mod:`neurodb.km.places` (:data:`PLACE_NAMES_ONE`: "Amchit", "Bikfaya", "Tebnine")
    that is no family name's (:data:`neurodb.watch.people.TOWN_WORDS`: "Saida", "Naameh", "Qana", "Houla"
    stay names there), or a place's name of several words there that one of its words marks a place
    ("Western Bekaa", "Bint Jbeil", "Baalbek-Hermel"; never one made only of words a name has: "Bar Elias",
    "Saad Nayel", nor one with a word of places that is a name too: "West Bekaa"), and none of the KM
    Database's surnames."""
    found = _place_words(name)
    if not found or not surnames.isdisjoint(found) or not _PLACES_NAMES_TOO.isdisjoint(found):
        return False
    if " ".join(found) in REQUEST_TOWNS:
        return True
    if len(found) == 1:  # (never a direction's or an area's word: "North", "South" are names too there)
        word = found[0]
        return word in PLACE_NAMES_ONE and word not in watch_people.TOWN_WORDS and word not in PLACE_WORDS
    return len(found) > 1 and tuple(found) in PLACE_NAMES and not PLACE_MARKERS.isdisjoint(found)


def _a_request(found: re.Match | None, surnames: frozenset[str] = frozenset()) -> bool:
    """Whether a :data:`REQUEST` match is a plain request about places: every item a town
    (:func:`_a_request_town`)."""
    return found is not None and all(
        _a_request_town(town, surnames) for town in _REQUEST_JOIN.split(found.group("towns"))
    )


def _requests(text: str, surnames: frozenset[str] = frozenset()) -> list[tuple[int, int]]:
    """The spans of the towns of the plain requests about places (:data:`REQUEST`) that make up a line of
    ``text``, their verb (if any) in lower case ("compare Chtoura and Zahle 2022", "Question: What about
    Chtoura and Zahle 2022?"), none of their towns one of the KM Database's surnames (``surnames``)."""
    if " and " not in text and "," not in text:
        return []
    return [
        found.span("towns")
        for found in REQUEST_LINE.finditer(text)
        if (found.group("verb") or "x").islower() and _a_request(found, surnames)
    ]


# a capitalised word just before a name withheld: its first name ("Jane [name withheld]")
_BEFORE_WITHHELD = re.compile(rf"(?<![\w'’\-])({WORD})\s+(?={re.escape(NAME_WITHHELD)})")
_PLACEHOLDERS = frozenset({NAME_WITHHELD, EMAIL_WITHHELD, LINK_WITHHELD, PHONE_WITHHELD})
_PLACEHOLDER = re.compile("(" + "|".join(re.escape(p) for p in sorted(_PLACEHOLDERS)) + ")")
# any word, whatever its case, with an initial before it ("q. doe") and a possessive after it left out
_ANY_WORD = re.compile(r"(?:(?<![\w.])[A-Za-z]\.\s*)?(?<![\w'’\-])([^\W\d_][\w\-]*)(?=(?:['’]s)?(?![\w\-]))")
# a capitalised word written as a name: a possessive, an initial or "by" before it, a year or "et al."
# after it
_AS_NAME = re.compile(
    rf"(?P<by>\b(?i:by)\s+)?(?P<initial>\b[A-Z]\.\s*)?(?<![\w'’\-])(?P<word>[{_CAP}][\w\-]*)"
    rf"(?P<after>['’]s?(?![\w\-])|,?\s+\(?\s*{YEAR}\b|{ET_AL})?(?![\w\-])"
)


def clean_question(question: str, people: People = NOBODY) -> str:
    """A question typed on the Ask tab as it is sent and kept: the names NeuroDB knows, e-mail addresses,
    links and phone numbers removed (``fmm.privacy.clean``, the Help assistant's rule: it reads NeuroDB's
    list of people only to take names out, and adds nothing to what is sent), then the KM Database's own
    rule (:func:`typed_for_ai`)."""
    from neurodb.fmm import privacy as fmm_privacy

    # an address written out first, on the text as typed ("x at example.org", "x [at] example [dot] org", "x
    # at Example.Org"): the cleaning of links would otherwise take its domain only, and leave its name
    return typed_for_ai(fmm_privacy.clean(without_addresses(question), QUESTION_CHARS)[0], people)


def typed_for_ai(text: str, people: People = NOBODY) -> str:
    """What a person typed or chose (a question, a document type chosen as a filter) as the AI may read it,
    by the KM Database's rule (:func:`for_ai`): its authors by their full name and by their surname,
    citations, names after a title; a surname in lower case too ("what did doe find", "doe's survey", "j.
    doe"), but a surname the KM texts also write as an ordinary word (``people.ordinary``: "inclusion") only
    when it is capitalised; a first name left before a name withheld. A surname that only an author entry of
    one bare word gives (``people.ordinary_alone``) is withheld only when it is written as a name ("Doe's",
    "J. Doe", "by Doe", "Doe 2020", "Doe et al."): "Inclusion of children?" keeps its first word. A plain
    request about places that is the whole text (:data:`REQUEST`: "Show Beirut and Tyre 2023", "Compare
    Chtoura and Zahle 2022"; :data:`REQUEST_HEAD` with a subject's words after the year: "Compare Beirut and
    Bourj Hammoud 2023 out of school") keeps its towns, its verb in lower case ("show Beirut and Tyre 2023"),
    so that every later check (a record's rule, the final check, a download's About sheet) reads it the same
    way; any other text, a request's verb included, is read as the rule reads it ("Show Jordan and Tyre 2023",
    "Thanks. Show Beirut and Tyre 2023", "Summarise Halba and Tyre 2019: 4-5" withhold the verb with the name
    after it)."""
    if not text:
        return text
    request = REQUEST.fullmatch(text)
    if not _a_request(request, people.surnames):
        request = REQUEST_HEAD.fullmatch(text)  # "Compare Beirut and Bourj Hammoud 2023 out of school"
    verb = request.group("verb") if _a_request(request, people.surnames) else None
    if verb and not verb.islower():
        lowered = f"{text[: request.start('verb')]}{verb.lower()}{text[request.end('verb') :]}"
        if _typed(lowered, people) == lowered:
            return lowered
    return _typed(text, people)


def _typed(text: str, people: People) -> str:
    """:func:`typed_for_ai` on ``text`` as it is (no request's verb read apart)."""
    alone = people.ordinary_alone
    cleaned = for_ai(text, people, people.surnames - alone if alone else people.surnames)
    if alone:

        def as_name(match: re.Match) -> str:
            if fold(match.group("word")) not in alone:
                return match.group(0)
            if match.group("initial") or match.group("by") or match.group("after"):
                return (match.group("by") or "") + NAME_WITHHELD + (match.group("after") or "")
            return match.group(0)

        cleaned = _AS_NAME.sub(as_name, cleaned)
    cleaned = any_case(cleaned, people)

    def first_name(match: re.Match) -> str:
        return "" if not kept(match.group(1)) else match.group(0)

    if NAME_WITHHELD not in cleaned:
        return cleaned
    # a surname withheld in any case may stand after a title ("Dr. doe"): the title goes with it, as a
    # record's rule takes it (:data:`_TITLE_BEFORE`), so that the final check reads the question as kept
    return _TITLE_BEFORE.sub("", _BEFORE_WITHHELD.sub(first_name, cleaned))


def any_case(text: str, people: People = NOBODY) -> str:
    """``text`` with the KM Database's surnames withheld whatever their case ("what did doe find", "doe's
    survey", "j. doe", "doe-led", "doe2019", "doe_survey"), except the surnames its texts also write as
    ordinary words (``people.ordinary``). Run again on a text it cleaned, it changes nothing: what a question
    sent to the AI is checked with (an earlier question of a conversation too)."""
    words = people.surnames - people.ordinary
    if not text or not words:
        return text

    def one(match: re.Match) -> str:
        word = match.group(1)
        if fold(word) in words:
            return NAME_WITHHELD
        head = _hyphen_head(word.lower(), words)  # "doe-led", "doe-2019"
        if head:
            return NAME_WITHHELD + word[head:]
        return _glued(word, words) or match.group(0)  # "doe2019", "lcrp_doe_2019", "survey_doe"

    return "".join(  # never inside a placeholder ("[name withheld]")
        part if part in _PLACEHOLDERS else _ANY_WORD.sub(one, part) for part in _PLACEHOLDER.split(text)
    )
