"""The places of Lebanon a question to Ask the KM Database names (:func:`read`), written in code: public
geography (the governorates, their main districts and towns, with the spellings reports use), never the
workbook's text and never another NeuroDB table.

- **A place named** (any spelling, "the" or an article such as "El" before it) is taken out of the words
  searched; all the places of one question make **one** place group (:attr:`Reading.alternatives`): a
  finding qualifies when its own words name any of them. A governorate also finds its districts and towns
  ("South" finds "Saida", "Tyre", "Jezzine"); Mount Lebanon also finds Keserwan-Jbeil, as older reports
  count it in Mount Lebanon. A district or a town finds its own spellings ("Tripoli (North)").
- **"South Lebanon", "the south"** mean the South and Nabatieh governorates; "South governorate" the South
  only.
- **Governorate words** ("governorate", "gov", "mohafaza") are part of the place when a place is named, and
  otherwise ask for a breakdown by governorate (:attr:`Reading.breakdown`: "all governorates", "by
  governorate", "all gov").
- **"All Lebanon", "national", "nationwide"** ask for no place (:attr:`Reading.national`): a place named
  with them is not a restriction.
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
        ("north governorate", "north lebanon", "northern lebanon", "northern governorate", "north"),
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
    Place("baabda", "Baabda", ("baabda", "ba'abda"), ("mount-lebanon",)),
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
)
# what a question means by these words: the South and Nabatieh governorates, as many reports use it
REGIONS: dict[str, tuple[str, ...]] = {
    spelling: ("south", "nabatieh")
    for spelling in (
        "south",
        "south lebanon",
        "southern lebanon",
        "south of lebanon",
        "southern border",
        "southern borders",
    )
}
REGION_NAMES = {("south", "nabatieh"): "the South (South and Nabatieh governorates)"}
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
# the governorate words as findings write them (not "gov": texts write it for "government")
WRITTEN_GOVERNORATE_WORDS = ("governorate", "governorates", "mohafaza", "mohafazat")
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
    "national",
)
ARTICLE = r"(?:(?:the|el|al|an|ash|ech)[\s-]+)?"  # "the North", "El Nabatieh", "al-Koura"

BY_KEY: dict[str, Place] = {p.key: p for p in (*GOVERNORATES, *DISTRICTS)}


@cache
def _aliases() -> list[tuple[re.Pattern, tuple[str, ...], str]]:
    """Every spelling a question may use, the longest first: its pattern, the places it means, and the name
    the line under the answer gives it."""
    found: dict[str, tuple[tuple[str, ...], str]] = {}
    for place in (*GOVERNORATES, *DISTRICTS):
        name = place.name if not place.governorates else f"{place.name} ({_names(place.governorates)})"
        for spelling in place.spellings:
            found.setdefault(spelling, ((place.key,), name))
    for spelling, keys in REGIONS.items():
        found[spelling] = (keys, REGION_NAMES[keys])
    return [
        (re.compile(rf"(?<![\w'-]){ARTICLE}{re.escape(spelling)}(?![\w'-])", re.IGNORECASE), keys, name)
        for spelling, (keys, name) in sorted(found.items(), key=lambda item: len(item[0]), reverse=True)
    ]


def _names(keys: tuple[str, ...]) -> str:
    return " and ".join(BY_KEY[k].name for k in keys)


def _phrases(words: tuple[str, ...]) -> re.Pattern:
    alternation = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![\w'-])(?:{alternation})(?![\w'-])", re.IGNORECASE)


_GOVERNORATE_WORDS = _phrases(GOVERNORATE_WORDS)
_NATIONAL = _phrases(NATIONAL)


@dataclass
class Reading:
    """What a question says about places: its text without them, the places it names (their keys, and the
    names said under the answer), and whether it asks for all of Lebanon or a breakdown by governorate."""

    text: str
    keys: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    national: bool = False
    breakdown: bool = False

    @property
    def restricted(self) -> bool:
        """Whether the findings must name one of the places (a place named, and not "all Lebanon")."""
        return bool(self.keys) and not self.national

    @property
    def label(self) -> str:
        return ", ".join(self.names)

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


def breakdown_alternatives() -> list[str]:
    """A breakdown by governorate: the governorate words and every governorate's own spellings."""
    return _pruned([*WRITTEN_GOVERNORATE_WORDS, *(s for g in GOVERNORATES for s in g.spellings)])


def governorate_alternatives() -> list[list[str]]:
    """Each governorate's own spellings: a finding that names more governorates ranks first in a
    breakdown."""
    return [_pruned(list(g.spellings)) for g in GOVERNORATES]


def _pruned(spellings: list[str]) -> list[str]:
    """``spellings`` without those that hold another one as a whole word ("south lebanon" when "south" is
    there: the search finds both by the word)."""
    unique = list(dict.fromkeys(spellings))
    singles = {s for s in unique if re.fullmatch(r"[\w'.]+", s)}
    return [s for s in unique if s in singles or not any(w in singles for w in re.split(r"[\s-]+", s))]


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


_PLURAL = re.compile(r"(?<![\w'-])(?:governorates|mohafazat|muhafazat|gouvernorats)(?![\w'-])", re.IGNORECASE)


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
    governorate = bool(_GOVERNORATE_WORDS.search(rest))
    rest = _GOVERNORATE_WORDS.sub(" ", rest)
    return Reading(
        text=" ".join(rest.split()),
        keys=keys,
        names=names,
        national=national,
        breakdown=governorate and not keys,
    )
