"""The places of Lebanon a question to Ask the KM Database names (:func:`read`), written in code: public
geography (the governorates, their main districts and towns, with the spellings reports use), never the
workbook's text and never another NeuroDB table.

- **A place named** (any spelling, "the" or an article such as "El" before it) is taken out of the words
  searched; all the places of one question make **one** place group (:attr:`Reading.alternatives`): a
  finding qualifies when its own words name any of them. A governorate also finds its districts and towns
  ("South" finds "Saida", "Tyre", "Jezzine"); Mount Lebanon also finds Keserwan-Jbeil, as older reports
  count it in Mount Lebanon. A district, a town or a camp finds its own spellings ("Tripoli (the North)"). A
  spelling inside another governorate's place is not its place ("Jbeil" in "Bint Jbeil", "North" in "North
  Metn" or "North Bekaa", "South" in "South Metn": :func:`shadows`), but the other place's findings still
  count for that other place in the same group ("Mount Lebanon and Nabatieh", a breakdown).
- **"South Lebanon", "the south", "the southern governorates"** mean the South and Nabatieh governorates;
  "South governorate" the South only. "South Metn" is Baabda (Mount Lebanon), "North Bekaa" Baalbek-Hermel.
- **Governorate words** ("governorate", "gov", "mohafaza") and **district words** ("district", "caza") are
  part of the place when a place is named, and otherwise ask for a breakdown (:attr:`Reading.breakdown`:
  "all governorates", "by governorate", "all gov", "per governorate", "across governorates", "broken down
  by governorate", "at governorate level"; "by district", "each caza", "the districts":
  :attr:`Reading.by_district`), the words that ask for it taken out with them. A breakdown keeps the
  findings that name a place: a governorate, or a district or town as its governorate.
- **"All Lebanon", "nationally", "nationwide", "national level"** ask for no place
  (:attr:`Reading.national`). The word "national" alone does so when the question names a place ("the
  national rate and Akkar": every place, the findings naming Akkar first); without one it is a word of the
  subject ("the national education strategy"), read as all of Lebanon only when nothing else is left to
  search (:attr:`Reading.national_word`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache


@dataclass(frozen=True)
class Place:
    key: str
    name: str  # as the line under an answer names it
    spellings: tuple[str, ...]  # lower case, as questions and texts write it
    governorates: tuple[str, ...] = ()  # a district or a town: the governorate(s) it is in
    includes: tuple[str, ...] = ()  # a governorate: other governorates its findings count (older reports)
    camp: bool = False  # a camp: "Shatila camp" is the place, "camp" no word to search


GOVERNORATES: tuple[Place, ...] = (
    Place("akkar", "Akkar", ("akkar", "akar", "aakar", "aakkar", "akkar governorate")),
    Place(
        "baalbek-hermel",
        "Baalbek-Hermel",
        (
            "baalbek-hermel",
            "baalbeck-hermel",
            "baalbak-hermel",
            "baalbek hermel",
            "baalbeck hermel",
            "baalbek el hermel",
            "baalbek-el hermel",
            "north bekaa",
            "northern bekaa",
            "north beqaa",
        ),
    ),
    Place("beirut", "Beirut", ("beirut", "beyrouth", "bayrut", "beyrut", "bairut", "beirut governorate")),
    Place("bekaa", "Bekaa", ("bekaa", "beqaa", "bekka", "beka'a", "beqa'a", "biqa", "bekaa valley")),
    Place(
        "keserwan-jbeil",
        "Keserwan-Jbeil",
        (
            "keserwan-jbeil",
            "kesrouan-jbeil",
            "kesrwan-jbeil",
            "keserwan jbeil",
            "kesrouan jbeil",
            "keserwan-byblos",
        ),
    ),
    Place(
        "mount-lebanon",
        "Mount Lebanon",
        (
            "mount lebanon",
            "mont lebanon",
            "mont liban",
            "mont-liban",
            "mount-lebanon",
            "mt lebanon",
            "mt. lebanon",
            "jabal lubnan",
            "jabal loubnan",
        ),
        includes=("keserwan-jbeil",),
    ),
    Place(
        "nabatieh",
        "Nabatieh",
        (
            "nabatieh",
            "nabatiyeh",
            "nabatiye",
            "nabatiyah",
            "nabatiya",
            "nabatiyé",
            "nabatie",
            "nabatiyeh governorate",
            "nabatieh governorate",
        ),
    ),
    Place(
        "north",
        "North",
        (
            "north governorate",
            "north lebanon",
            "northern lebanon",
            "northern governorate",
            "north",
            "liban-nord",
            "liban nord",
            "nord-liban",
            "nord liban",
            "shamal",
            "chamal",
        ),
    ),
    Place(
        "south",
        "South",
        (
            "south governorate",
            "southern governorate",
            "south lebanon",
            "southern lebanon",
            "southern border",
            "southern borders",
            "south",
            "liban-sud",
            "liban sud",
        ),
    ),
)
# the main districts (cazas) and towns, with their governorate(s)
DISTRICTS: tuple[Place, ...] = (
    Place("halba", "Halba", ("halba",), ("akkar",)),
    Place("qobayat", "Qobayat", ("qobayat", "qoubaiyat", "kobayat"), ("akkar",)),
    Place("wadi-khaled", "Wadi Khaled", ("wadi khaled",), ("akkar",)),
    Place(
        "baalbek", "Baalbek", ("baalbek", "baalbeck", "baalbak", "ba'albek", "baalbeq"), ("baalbek-hermel",)
    ),
    Place("hermel", "Hermel", ("hermel", "hirmil"), ("baalbek-hermel",)),
    Place("arsal", "Arsal", ("arsal", "aarsal", "ersal"), ("baalbek-hermel",)),
    Place("zahle", "Zahle", ("zahle", "zahleh", "zahlé", "zahlah"), ("bekaa",)),
    Place("west-bekaa", "West Bekaa", ("west bekaa", "western bekaa", "west beqaa"), ("bekaa",)),
    Place("rashaya", "Rashaya", ("rashaya", "rachaya", "rashayya", "rachaiya"), ("bekaa",)),
    Place("chtaura", "Chtaura", ("chtaura", "chtoura", "shtaura"), ("bekaa",)),
    Place("bar-elias", "Bar Elias", ("bar elias", "bar elyas"), ("bekaa",)),
    Place(
        "keserwan",
        "Keserwan",
        ("keserwan", "kesrouan", "kesrwan", "kisrawan", "keserouan"),
        ("keserwan-jbeil", "mount-lebanon"),
    ),
    Place("jbeil", "Jbeil", ("jbeil", "jbail", "jubail", "byblos"), ("keserwan-jbeil", "mount-lebanon")),
    Place("jounieh", "Jounieh", ("jounieh", "junieh", "jouniyeh"), ("keserwan-jbeil", "mount-lebanon")),
    Place("aley", "Aley", ("aley", "aaley", "alay"), ("mount-lebanon",)),
    Place("chouf", "Chouf", ("chouf", "shouf", "chuf", "shuf"), ("mount-lebanon",)),
    Place("baabda", "Baabda", ("baabda", "ba'abda", "south metn", "southern metn"), ("mount-lebanon",)),
    Place("metn", "Metn", ("metn", "matn", "maten", "north metn", "el metn"), ("mount-lebanon",)),
    Place("bourj-hammoud", "Bourj Hammoud", ("bourj hammoud", "burj hammoud"), ("mount-lebanon",)),
    Place("damour", "Damour", ("damour",), ("mount-lebanon",)),
    Place(
        "bint-jbeil",
        "Bint Jbeil",
        ("bint jbeil", "bint jbail", "bent jbeil", "bint jubayl", "bint-jbeil"),
        ("nabatieh",),
    ),
    Place("marjayoun", "Marjayoun", ("marjayoun", "marjeyoun", "marjayun", "marjaayoun"), ("nabatieh",)),
    Place("hasbaya", "Hasbaya", ("hasbaya", "hasbaiya", "hasbayya", "hasbeya"), ("nabatieh",)),
    Place("khiam", "Khiam", ("khiam",), ("nabatieh",)),
    Place("tripoli", "Tripoli", ("tripoli", "trablous", "tarablus", "trablus"), ("north",)),
    Place("koura", "Koura", ("koura", "kura"), ("north",)),
    Place("batroun", "Batroun", ("batroun", "batrun"), ("north",)),
    Place("zgharta", "Zgharta", ("zgharta", "zghorta"), ("north",)),
    Place("bcharre", "Bcharre", ("bcharre", "bsharri", "bcharreh", "becharre", "bsharre"), ("north",)),
    Place(
        "minieh-danniyeh",
        "Minieh-Danniyeh",
        (
            "minieh-danniyeh",
            "minieh-dinnieh",
            "miniyeh-danniyeh",
            "minnieh",
            "minieh",
            "miniyeh",
            "dinnieh",
            "danniyeh",
        ),
        ("north",),
    ),
    Place("saida", "Saida", ("saida", "sidon", "sayda", "saïda"), ("south",)),
    Place("tyre", "Tyre", ("tyre", "sour", "tyr"), ("south",)),
    Place("jezzine", "Jezzine", ("jezzine", "jezzin", "jizzin", "jazzine"), ("south",)),
    Place("naqoura", "Naqoura", ("naqoura", "naqura"), ("south",)),
    # the Palestine refugee camps, each in its governorate (Shatila and Bourj el-Barajneh: Beirut's southern
    # suburbs, counted in Beirut and in Mount Lebanon)
    Place(
        "ein-el-hilweh",
        "Ein el-Hilweh",
        (
            "ein el-hilweh",
            "ein el hilweh",
            "ein al-hilweh",
            "ein al hilweh",
            "ain el-hilweh",
            "ain el hilweh",
            "ain al-hilweh",
            "ain al hilweh",
            "ain el-helweh",
            "ein el-helweh",
        ),
        ("south",),
        camp=True,
    ),
    Place("mieh-mieh", "Mieh Mieh", ("mieh mieh", "mieh-mieh", "miyeh miyeh"), ("south",), camp=True),
    Place(
        "rashidieh", "Rashidieh", ("rashidieh", "rashidiyeh", "rachidieh", "rashidiya"), ("south",), camp=True
    ),
    Place(
        "burj-el-shemali",
        "Burj el-Shemali",
        ("burj el-shemali", "burj el shemali", "burj shemali", "bourj el-chemali", "burj al-shamali"),
        ("south",),
        camp=True,
    ),
    Place("el-buss", "El Buss", ("el buss", "el-buss", "al buss", "al-buss"), ("south",), camp=True),
    Place(
        "nahr-el-bared",
        "Nahr el-Bared",
        ("nahr el-bared", "nahr el bared", "nahr al-bared", "nahr al bared"),
        ("north",),
        camp=True,
    ),
    Place("beddawi", "Beddawi", ("beddawi", "baddawi", "beddaoui"), ("north",), camp=True),
    Place("mar-elias", "Mar Elias", ("mar elias",), ("beirut",), camp=True),
    Place("shatila", "Shatila", ("shatila", "chatila", "shateela"), ("beirut", "mount-lebanon"), camp=True),
    Place(
        "bourj-el-barajneh",
        "Bourj el-Barajneh",
        (
            "bourj el-barajneh",
            "bourj el barajneh",
            "burj el-barajneh",
            "burj el barajneh",
            "burj al-barajneh",
            "burj barajneh",
            "bourj barajneh",
        ),
        ("mount-lebanon", "beirut"),
        camp=True,
    ),
    Place("dbayeh", "Dbayeh", ("dbayeh", "dbaye"), ("mount-lebanon",), camp=True),
    Place("wavel", "Wavel", ("wavel", "jalil camp"), ("baalbek-hermel",), camp=True),
)
# what a question means by these words: the South and Nabatieh governorates, as many reports use it
REGIONS: dict[str, tuple[str, ...]] = {
    spelling: ("south", "nabatieh")
    for spelling in (
        "south",
        "south lebanon",
        "southern lebanon",
        "south of lebanon",
        "southern governorates",
        "southern districts",
        "southern region",
        "south region",
        "southern border",
        "southern borders",
        "sud-liban",
        "sud liban",
        "sud du liban",
        "janoub",
        "jnoub",
    )
}
REGION_NAMES = {("south", "nabatieh"): "the South (South and Nabatieh governorates)"}
THE = frozenset({"north", "south"})  # said "the North", "the South"
GOVERNORATE_WORDS = (
    "governorates",
    "governorate",
    "govs",
    "gov",
    "mohafazat",
    "mohafazah",
    "mohafaza",
    "muhafazat",
    "muhafazah",
    "muhafaza",
    "gouvernorats",
    "gouvernorat",
)
# the words for a district (a caza), and the phrases that ask for a breakdown by district
DISTRICT_WORDS = (
    "districts",
    "district",
    "cazas",
    "caza",
    "kazas",
    "kaza",
    "qadaas",
    "qadaa",
    "qada",
    "aqdiya",
)
# the governorate and district words as findings write them (not "gov": texts write it for "government"; not
# "district" alone: "district hospitals")
WRITTEN_GOVERNORATE_WORDS = ("governorate", "governorates", "mohafaza", "mohafazat", "districts", "cazas")
NATIONAL = (
    "all of lebanon",
    "all over lebanon",
    "all lebanon",
    "whole of lebanon",
    "whole lebanon",
    "entire lebanon",
    "across lebanon",
    "lebanon as a whole",
    "the whole country",
    "the entire country",
    "national level",
    "national-level",
    "nationwide",
    "nation-wide",
    "countrywide",
    "country-wide",
    "nationally",
    "national figures",
    "national figure",
    "national average",
    "national averages",
    "national rate",
    "national rates",
    "national data",
    "national scale",
)
ARTICLE = r"(?:(?:the|el|al|an|ash|ech)[\s-]+)?"  # "the North", "El Nabatieh", "al-Koura"

BY_KEY: dict[str, Place] = {p.key: p for p in (*GOVERNORATES, *DISTRICTS)}


@cache
def _aliases() -> list[tuple[re.Pattern, tuple[str, ...], str]]:
    """Every spelling a question may use, the longest first: its pattern, the places it means, and the name
    the line under the answer gives it."""
    found: dict[str, tuple[tuple[str, ...], str, str]] = {}
    for place in (*GOVERNORATES, *DISTRICTS):
        name = said(place.key) if not place.governorates else f"{place.name} ({_names(place.governorates)})"
        for spelling in place.spellings:
            found.setdefault(spelling, ((place.key,), name, r"(?:\s+camps?)?" if place.camp else ""))
    for spelling, keys in REGIONS.items():
        found[spelling] = (keys, REGION_NAMES[keys], "")
    return [
        (
            re.compile(rf"(?<![\w'-]){ARTICLE}{re.escape(spelling)}{suffix}(?![\w'-])", re.IGNORECASE),
            keys,
            name,
        )
        for spelling, (keys, name, suffix) in sorted(
            found.items(), key=lambda item: len(item[0]), reverse=True
        )
    ]


def said(key: str) -> str:
    """A place as the line under an answer names it: "Akkar", "the North"."""
    return f"the {BY_KEY[key].name}" if key in THE else BY_KEY[key].name


def _names(keys: tuple[str, ...]) -> str:
    return " and ".join(said(k) for k in keys)


def _phrases(words: tuple[str, ...]) -> re.Pattern:
    alternation = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![\w'-])(?:{alternation})(?![\w'-])", re.IGNORECASE)


_DISTRICT_WORDS = _phrases(DISTRICT_WORDS)
_NATIONAL = _phrases(NATIONAL)
_NATIONAL_WORD = _phrases(("national",))
# the words before a governorate or district word that ask for a breakdown, taken out with it (never
# searched): "by", "per", "across", "each", "all", "in all", "within each", "across all the", "broken
# down by", "disaggregated by", "a breakdown by"; "at (the)" before the word and its level ("governorate
# level", "district-wise", "governorate by governorate")
_BREAKDOWN_LEAD = (
    r"(?:(?:broken\s+down|break\s+down|breakdowns?|disaggregated|disaggregation|split|divided)\s+)?"
    r"(?:by|each|every|all|per|across|different|various|"
    r"(?:for|in|of|across|at|within|on)\s+(?:each|every|all))(?:\s+of)?\s+(?:the\s+)?"
)
_AT = r"(?:(?:at|on)\s+(?:the\s+)?)"
_LEVEL = r"(?:[\s-]+(?:levels?|wise|breakdowns?|by\s+(?:the\s+)?(?:{words})))"  # "governorate-level"
_DISTRICT = "|".join(re.escape(w) for w in DISTRICT_WORDS)
_GOVERNORATE = "|".join(re.escape(w) for w in GOVERNORATE_WORDS)
# a breakdown by district: "by district", "each caza", "all the districts", "per district", "at district
# level", "district-level", or the plural ("the districts")
_BY_DISTRICT = re.compile(
    rf"(?<![\w'-])(?:{_BREAKDOWN_LEAD}(?:{_DISTRICT}){_LEVEL.format(words=_DISTRICT)}?|"
    rf"{_AT}?(?:{_DISTRICT}){_LEVEL.format(words=_DISTRICT)}|"
    rf"(?:districts|cazas|kazas|qadaas|aqdiya){_LEVEL.format(words=_DISTRICT)}?)(?![\w'-])",
    re.IGNORECASE,
)
# the governorate words, with the words that ask for the breakdown ("per gov", "across governorates", "at
# governorate level"), and "by region" ("region" alone is no breakdown: "the southern region")
_BY_GOVERNORATE = re.compile(
    rf"(?<![\w'-])(?:(?:{_BREAKDOWN_LEAD}|{_AT})?(?:{_GOVERNORATE}){_LEVEL.format(words=_GOVERNORATE)}?|"
    rf"(?:by|per|each|across)\s+(?:the\s+)?regions?)(?![\w'-])",
    re.IGNORECASE,
)


@dataclass
class Reading:
    """What a question says about places: its text without them, the places it names (their keys, and the
    names said under the answer), and whether it asks for all of Lebanon or a breakdown by governorate."""

    text: str
    keys: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    national: bool = False
    breakdown: bool = False
    by_district: bool = False  # the breakdown was asked by district ("by district", "each caza")
    national_word: bool = False  # "national" with no place named: left in the text, a word of the subject

    @property
    def restricted(self) -> bool:
        """Whether the findings must name one of the places (a place named, and not "all Lebanon")."""
        return bool(self.keys) and not self.national

    @property
    def label(self) -> str:
        """The places named: "Akkar", "Akkar or the North", "Akkar, Beirut or the North"."""
        if len(self.names) < 2:
            return "".join(self.names)
        return f"{', '.join(self.names[:-1])} or {self.names[-1]}"

    def alternatives(self) -> list[str]:
        """The spellings a finding's own words may use for any of the places named: one group."""
        return _pruned([s for key in self.keys for s in written(key)])

    def broader(self) -> list[str]:
        """The spellings of the governorates of the districts and towns named (a finding about the North
        when the question names Tripoli), for the ranking of a wider search."""
        governorates = [g for key in self.keys for g in BY_KEY[key].governorates]
        return _pruned([s for key in dict.fromkeys(governorates) for s in written(key)])


def written(key: str) -> list[str]:
    """How findings may write place ``key``: its spellings; for a governorate also its districts and towns,
    and the governorates it includes (Mount Lebanon: Keserwan-Jbeil, as older reports count it)."""
    place = BY_KEY[key]
    found = list(place.spellings)
    if not place.governorates:
        for district in DISTRICTS:
            if key in district.governorates:
                found += district.spellings
        for other in place.includes:
            found += written(other)
    return list(dict.fromkeys(found))


@cache
def shadows(spelling: str) -> tuple[str, ...]:
    """The spellings of places of other governorates that hold ``spelling`` as one of their words ("jbeil"
    in "Bint Jbeil", "north" in "North Metn"): a finding that names one of them does not name ``spelling``'s
    place."""
    owner = [p for p in (*GOVERNORATES, *DISTRICTS) if spelling in p.spellings]
    if not owner or " " in spelling or "-" in spelling:
        return ()
    found = []
    for place in owner:
        mine = {place.key, *place.governorates, *place.includes}
        for other in (*GOVERNORATES, *DISTRICTS):
            theirs = {other.key, *other.governorates}
            if mine & theirs or any(key in BY_KEY[g].includes for key in theirs for g in mine if g in BY_KEY):
                continue
            found += [s for s in other.spellings if s != spelling and spelling in re.split(r"[\s-]+", s)]
    return tuple(dict.fromkeys(found))


def breakdown_alternatives() -> list[str]:
    """A breakdown by governorate: the governorate words and the spellings of every governorate, district and
    town (a district or a town names its governorate)."""
    return _pruned(
        [*WRITTEN_GOVERNORATE_WORDS, *(s for p in (*GOVERNORATES, *DISTRICTS) for s in p.spellings)]
    )


def governorate_alternatives() -> list[list[str]]:
    """Each governorate's spellings with those of its districts and towns (each counted in its first
    governorate, as :func:`compares` counts them): a finding that names more governorates ranks first in a
    breakdown."""
    return [
        _pruned([*g.spellings, *(s for d in DISTRICTS if d.governorates[0] == g.key for s in d.spellings)])
        for g in GOVERNORATES
    ]


def _pruned(spellings: list[str]) -> list[str]:
    """``spellings`` without those that hold another one as a whole word ("akkar governorate" when "akkar" is
    there: the search finds both by the word), but for a word that does not find every text it is in
    (:func:`shadows`: "bint jbeil" stays with "jbeil", "north lebanon" with "north": "In North Lebanon and
    North Bekaa" names the North)."""
    unique = list(dict.fromkeys(spellings))
    singles = {s for s in unique if re.fullmatch(r"[\w'.]+", s)}
    return [
        s
        for s in unique
        if s in singles or not any(w in singles and not shadows(w) for w in re.split(r"[\s-]+", s))
    ]


def _claimed(rest: str) -> list[tuple[int, int, tuple[str, ...], str]]:
    """The places ``rest`` names: (start, end, their keys, the name said), in the order it names them."""
    claimed: list[tuple[int, int, tuple[str, ...], str]] = []
    for pattern, keys, name in _aliases():  # the longest spellings first: "north metn" before "north"
        for match in pattern.finditer(rest):
            start, end = match.span()
            if not any(start < e and s < end for s, e, _k, _n in claimed):
                claimed.append((start, end, keys, name))
    claimed.sort()
    return claimed


_PLURAL = re.compile(
    r"(?<![\w'-])(?:governorates|mohafazat|muhafazat|gouvernorats|districts|cazas)(?![\w'-])", re.IGNORECASE
)


def compares(text: str) -> bool:
    """Whether a finding's ``text`` gives figures by governorate: it names two governorates or more (a
    district or a town as its governorate; "South Lebanon" as one), or speaks of governorates ("in all
    governorates")."""
    if _PLURAL.search(text or ""):
        return True
    named = set()
    for _s, _e, keys, _n in _claimed(f" {text or ''} "):
        place = BY_KEY[keys[0]]
        named.add(place.governorates[0] if place.governorates else place.key)
    return len(named) >= 2


def read(text: str) -> Reading:
    """The places ``text`` names (see the module's notes), in the order it names them, and the text without
    them, without the governorate words and without the words for all of Lebanon. A place a region named
    covers ("Nabatiyeh" with "the south") is not named again under the answer."""
    rest = f" {text or ''} "
    claimed = _claimed(rest)
    regions = [set(k) for _s, _e, k, _n in claimed if len(k) > 1]
    keys: list[str] = []
    names: list[str] = []
    for _s, _e, found, name in claimed:
        keys += [k for k in found if k not in keys]
        covered = len(found) == 1 and any(set(found) <= region for region in regions)
        if not covered and name not in names:
            names.append(name)
    for start, end, _k, _n in sorted(claimed, reverse=True):
        rest = rest[:start] + " " + rest[end:]
    national = bool(_NATIONAL.search(rest))
    rest = _NATIONAL.sub(" ", rest)
    national_word = bool(_NATIONAL_WORD.search(rest))
    if national_word and keys:  # "the national rate and Akkar": every place, Akkar first
        national, national_word = True, False
        rest = _NATIONAL_WORD.sub(" ", rest)
    by_district = not keys and bool(_BY_DISTRICT.search(rest))
    rest = _BY_DISTRICT.sub(" ", rest)
    if keys:  # "Akkar district"
        rest = _DISTRICT_WORDS.sub(" ", rest)
    governorate = bool(_BY_GOVERNORATE.search(rest))
    rest = _BY_GOVERNORATE.sub(" ", rest)
    return Reading(
        text=" ".join(rest.split()),
        keys=keys,
        names=names,
        national=national,
        breakdown=(governorate or by_district) and not keys,
        by_district=by_district and not governorate,
        national_word=national_word,
    )
