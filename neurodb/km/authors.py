"""The KM Database's authors ("Publishing Entity / Authors"): shown on screen as the workbook gives them;
in downloads (and printouts) only the organisations are named, and individual authors are counted, never
named (the user's rule). Never sent to the AI.

The rule (:func:`classify`), piece by piece:

1. Invisible characters and "et al." are taken out.
2. The entry is cut into parts on ";", a new line or "|". A placeholder ("Not reported", "Unknown"...) gives
   nothing.
3. A part an administrator listed under *Organisations named in downloads* (:class:`KMOrganisation`) is
   what they said: an organisation (named), a person (counted) or words (left out).
4. Otherwise the part is cut into pieces on commas and parentheses ("J. Doe (Fakeorg Institute)"), except
   an acronym made of the words before it ("Fake Org (FO)"); initials join the surname before them ("Doe,
   J."); and on "by" ("Report prepared by J. Doe": the generic words before it are left out). A piece is
   also cut on a slash, a spaced dash, a colon, "with" and the full stop that ends a sentence, and on "and"
   and "&", when a person stands in it ("Jane Doe/Fakeorg", "Fakeorg Team: Jane Doe", "author Jane Doe.
   Published in ...") or the part is not an organisation as a whole: an organisation's name stays whole
   ("Fakeorg Institute for Research and Development", "Fakeorg - Lebanon").
5. Words before a name that say what the person did, or how an organisation took part, are taken off
   ("consultant Jane Doe", "led by Jane Doe", "in partnership with the Fakeorg Fund", "on behalf of"), and
   a piece that says where the work was published or what it was part of ("Published in ...", "as part of
   the ... programme") is left out, as a title at the end of an entry ("Fake journal: theory and
   practice"). Each piece is then classed by itself: listed by an administrator; a place → joins the
   organisation before it ("Fakeorg Ministry, Lebanon"); a word of an organisation (:data:`ORG_WORDS`) →
   organisation, except a person's name that holds one ("Jane Cross", "Charity Doe") and words all in lower
   case ("research", counted); initials ("J. Doe", "Doe J") → person; a name in capitals ("DOE Jane") →
   person; acronyms ("FAKEORG", "FaSyR", "F2P") → organisation; capitalised words that could be a name →
   person; anything else (a topic in lower case) → counted, never named.
6. A downloads cell then reads: the organisations ("Fakeorg Institute; Fakeorg University"), "Authors: N",
   or both ("Fakeorg Institute + 2 authors"). An organisation whose text still holds a person's name, a
   citation or a name after "by" or "author" is counted as an author instead.

When the rule is unsure, it counts the entry as a person: it never names someone by mistake. The version's
page in the admin lists every string named as an organisation, with **Count as a person** and **Not an
organisation**, and the entries that look like ordinary words (:func:`look_like_words`).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from django.db import connection
from django.db.models import Count, Max

from neurodb.watch import people as watch_people

from . import privacy
from .privacy import People

ORG_WORDS = frozenset(
    "organisation organisations organization organizations ministry ministere ministère university "
    "universite université universities bank council agency office programme program fund institute "
    "institut institution centre center foundation fondation association committee commission network "
    "group partnership initiative coalition society department directorate government republic federation "
    "alliance consortium cluster sector platform observatory authority union trust forum project school "
    "college academy laboratory secretariat mission services international relief rescue watch aid nations "
    "children ngo team board bureau inc ltd llc consulting consultants consultancy research studies "
    "collective movement municipality administration parliament hospital health education development "
    "policy statistics humanitarian refugee refugees women youth global national regional world worldwide "
    "corps cross crescent lebanon lebanese "
    # their plurals, and publications
    "institutes agencies ministries councils foundations associations networks groups offices programmes "
    "programs funds banks centres centers committees commissions initiatives societies departments "
    "authorities unions trusts forums projects schools colleges academies laboratories missions boards "
    "partners partnerships charity charities journal journals".split()
)
# words of ORG_WORDS that are also people's names ("Jane Cross", "John Banks", "Charity Doe"), and the words
# that make them an organisation's name again ("Red Cross", "Food Banks")
NAME_LIKE_ORG_WORDS = frozenset({"cross", "banks", "charity"})
NOT_NAMES = frozenset({"red", "food", "blood"})
JOINING = frozenset({"of", "the", "for", "and", "de", "du", "des", "la", "le", "et", "&"})
SMALL_WORDS = JOINING | {"to", "in", "on", "at", "a", "an"}  # left out of an acronym ("Fake-To-Policy (F2P)")
PARTICLES = frozenset(
    {"de", "van", "von", "el", "al", "bin", "ibn", "abu", "abou", "bou", "al-", "di", "da", "du", "le", "la"}
    | {"ben"}
)
EMPTY = frozenset(
    {"not reported", "not stated", "not available", "unknown", "anonymous", "n/a", "na", "none", "-", "–"}
    | {"—", ""}
)
_ET_AL = re.compile(r"\bet\.?\s+al\b\.?", re.IGNORECASE)
_PARTS = re.compile(r"[;\n|]")
_PAREN = re.compile(r"\(([^()]*)\)")
_AND = re.compile(r"\s+(?:and|&)\s+|\s*&\s*", re.IGNORECASE)
INITIALS = re.compile(r"^(?:[A-Z]\.?\s?-?){1,4}$")
INITIAL = re.compile(r"\b[A-Z]\.(?=\s|$|,)")
# "Doe J", "Doe, J.": a surname and one initial, apart (so an acronym such as "UNICEF" is not one)
SURNAME_INITIAL = re.compile(r"^[A-Z][\w'’-]+(?:,\s*|\s+)[A-Z]\.?$")
ACRONYM = re.compile(r"^[A-Z][A-Z0-9&-]{1,11}$")
LONG_ACRONYM = re.compile(r"^[A-Z][A-Z0-9&-]{3,11}$")
MIXED_CAPS = re.compile(r"^[A-Za-z][^A-Z]*[A-Z][^A-Z]*[A-Z]")  # "FaSyR"
_TOKEN = re.compile(r"[^\s()\[\],;:]+")
_TRIM = " \t,.;:"
ORGANISATION, PERSON, WORDS = "organisation", "person", "words"
# "by", with the word of what was done before it ("prepared by", "led by"): a piece is always cut there
BY_WORDS = (
    "prepared written led edited compiled conducted authored produced coordinated commissioned funded "
    "supported published reviewed done carried out undertaken".split()
)
_BY = re.compile(rf"\s*\b(?:(?:{'|'.join(BY_WORDS)})\s+)*by\s+", re.IGNORECASE)
# the other signs a piece is cut on when a person stands in it: a slash, a spaced dash, a colon (not of a
# link) and "with" ("Jane Doe/Fakeorg", "Jane Doe - Fakeorg", "Fakeorg Team: Jane Doe", "Jane Doe with ...")
_SIGNS = re.compile(r"\s*/\s*|\s+[-–—]\s+|\s*:(?!//)\s*|\s+(?:together\s+)?with\s+", re.IGNORECASE)
# the full stop that ends a sentence: after a word of three letters or more that is not an abbreviation,
# before a capital ("author Jane Doe. Published in ...", not "J. Doe" or "Fakeorg Inc. Beirut")
_FULL_STOP = re.compile(r"(?<=[^\W\d_])\.\s+(?=[A-Z])")
_LAST_WORD = re.compile(r"[^\W\d_]+$")
ABBREVIATIONS = frozenset(
    "dr mr mrs ms mme mlle prof st ste mt inc ltd co corp univ dept govt gov intl assoc no vol ed eds jr sr "
    "approx".split()
)
# the words before a name that join it ("and", "with", "in partnership with the", "on behalf of", "with
# technical support from") or say what the person did ("consultant Jane Doe", "lead author: Jane Doe")
_JOIN_LEAD = re.compile(
    r"^(?:in\s+(?:close\s+)?(?:partnership|collaboration|coordination|cooperation)\s+with\s+(?:the\s+)?"
    r"|on\s+behalf\s+of\s+(?:the\s+)?"
    r"|(?:with\s+)?(?:the\s+)?(?:(?:technical|financial)\s+(?:(?:and|&)\s+(?:technical|financial)\s+)?)?"
    r"support\s+(?:from|of)\s+(?:the\s+)?"
    r"|(?:and|&|by|with|together\s+with|for)\s+)",
    re.IGNORECASE,
)
_ROLE = (
    r"(?:(?:lead|main|principal|senior|junior|independent|external|local)\s+)?(?:co-?)?"
    r"(?:authors?|researchers?|consultants?|investigators?|editors?|writers?|evaluators?)\b"
)
_ROLE_LEAD = re.compile(rf"^{_ROLE}[\s:]*", re.IGNORECASE)
_ROLE_ONLY = re.compile(_ROLE, re.IGNORECASE)  # "Jane Doe (consultant)": the role of the person before
# a piece that says where the work was published, or what it was part of, not who wrote it: left out
_VENUE = re.compile(
    r"^(?:(?:published|appeared|available|printed)\s+(?:in|on|at)|as\s+part\s+of)\b", re.IGNORECASE
)
# a title at the end of an entry (a journal's: "Fake journal: theory, research and practice"): a colon before
# words in lower case
_TITLE_COLON = re.compile(r":(?!//)\s*(?=[^\W\d_A-Z])")


def fold(text: str) -> str:
    """An author entry as it is compared with the administrators' list: folded, spaces collapsed, no
    comma or full stop at either end."""
    return " ".join(watch_people.fold(text).split()).strip(_TRIM)


def _tokens(piece: str) -> list[str]:
    return [t.strip(".'’\"") or t for t in _TOKEN.findall(piece)]


def _folded_words(piece: str) -> list[str]:
    return [watch_people.fold(w) for w in re.findall(r"[^\W\d_]+", piece)]


def org_test(piece: str) -> bool:
    """Whether ``piece`` reads as an organisation: a word of :data:`ORG_WORDS` (not in a person's name that
    holds one, as "Jane Cross"), or acronyms (all its words upper-case acronyms or joining words; an acronym
    of four letters or more; a word with two capitals after its first letter, as "FaSyR")."""
    return _org_words(piece) or _acronyms(piece)


def _org_words(piece: str) -> bool:
    """Whether ``piece`` holds a word of :data:`ORG_WORDS`, and is not a person's name that holds one."""
    return any(w in ORG_WORDS for w in _folded_words(piece)) and not _name_with_org_word(piece)


def _name_with_org_word(piece: str) -> bool:
    """ "Jane Cross", "John Banks", "Charity Doe", "Banks": a name of one or two capitalised words whose only
    word of an organisation is one people are also named (:data:`NAME_LIKE_ORG_WORDS`), next to no word of
    an organisation, a place or common words ("Red Cross" stays an organisation)."""
    tokens = _tokens(piece)
    if not 1 <= len(tokens) <= 2 or not all(t[:1].isupper() and t[1:].islower() for t in tokens):
        return False
    words = [watch_people.fold(t) for t in tokens]
    if sum(w in ORG_WORDS for w in words) != 1 or not any(w in NAME_LIKE_ORG_WORDS for w in words):
        return False
    other = [w for w in words if w not in ORG_WORDS]
    return all(
        w not in NOT_NAMES and w not in privacy.PLACE_WORDS and w not in watch_people.COMMON_WORDS
        for w in other
    )


def _acronyms(piece: str) -> bool:
    tokens = _tokens(piece)
    if not tokens:
        return False
    if all(ACRONYM.match(t) or t.casefold() in JOINING for t in tokens) and any(
        ACRONYM.match(t) for t in tokens
    ):
        return True
    # a word with two capitals after its first letter ("FaSyR"), each part of a hyphenated name by itself
    mixed = any(MIXED_CAPS.match(p) for t in tokens for p in t.split("-"))
    return mixed or any(LONG_ACRONYM.match(t) and "-" not in t.strip("-") for t in tokens)


def _is_place(piece: str) -> bool:
    found = _folded_words(piece)
    return bool(found) and all(w in privacy.PLACE_WORDS for w in found)


def _capitals_name(piece: str) -> bool:
    """ "DOE Jane", "Jane DOE": two words, one in capitals and one capitalised, neither a word of an
    organisation or a place."""
    tokens = _tokens(piece)
    if len(tokens) != 2:
        return False
    letters = [re.sub(r"['’-]", "", t) for t in tokens]  # "FAKE-DOE", "O'DOE"
    caps = [
        t
        for t, bare in zip(tokens, letters, strict=True)
        if t.isupper() and len(bare) >= 2 and bare.isalpha()
    ]
    titled = [
        t
        for t, bare in zip(tokens, letters, strict=True)
        if t[:1].isupper() and t[1:].islower() and bare.isalpha()
    ]
    if len(caps) != 1 or len(titled) != 1:
        return False
    words = {watch_people.fold(t) for t in tokens}
    return words.isdisjoint(ORG_WORDS) and words.isdisjoint(privacy.PLACE_WORDS)


def _person_shape(piece: str) -> bool:
    """Capitalised words that could be a name: every word starts with a capital letter, or is initials, or
    a particle ("de", "el", "al-"...), and none is a common word."""
    tokens = _tokens(piece)
    if not tokens:
        return False
    for token in tokens:
        low = token.casefold()
        if low in watch_people.COMMON_WORDS:
            return False
        if low in PARTICLES or INITIALS.match(token):
            continue
        if re.match(r"^(?:al|el)-", low) and token[3:4].isupper():
            continue
        if not token[:1].isupper():
            return False
    return True


def _acronym_of(content: str, before: str) -> bool:
    """Whether a parenthesis's ``content`` is the acronym of the words ``before`` it (the parts of a
    hyphenated word too): of their capitalised words ("Fake Org (FO)"), of every word ("Ministry of Fake
    Affairs (MOFA)"), or of every word but the small ones ("Fake-To-Policy (F2P)": its digit stands for
    one)."""
    content = content.strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9&]{1,11}", content):  # "FO", "F2P"
        return False
    letters = re.sub(r"[^A-Z]", "", content)  # its letters: "F2P" is the acronym of "Fake to Policy"
    if len(letters) < 2:
        return False
    found = re.findall(r"[^\W\d_][\w'’]*", before)
    capitals = "".join(w[0] for w in found if w[:1].isupper())
    every = "".join(w[0].upper() for w in found)
    words = "".join(w[0].upper() for w in found if w.casefold() not in SMALL_WORDS)
    return any(
        len(initials) >= len(letters) and initials.endswith(letters) for initials in (capitals, every, words)
    )


def _split_parentheses(part: str) -> str:
    """The part with each parenthesis cut out as pieces of its own (", content ,"), except an acronym of
    the words before it."""
    out, last = [], 0
    for match in _PAREN.finditer(part):
        before = part[last : match.start()]
        out.append(before)
        segment = re.split(r",", before)[-1]
        if _acronym_of(match.group(1), segment):
            out.append(match.group(0))
        else:
            out.append(f", {match.group(1)} ,")
        last = match.end()
    out.append(part[last:])
    return "".join(out)


def pieces(part: str) -> list[str]:
    """The pieces of one part (step 4 of the rule)."""
    found: list[str] = []
    for raw in _split_parentheses(part).split(","):
        piece = " ".join(raw.split()).strip(_TRIM + "-–—")
        if piece.count("(") != piece.count(")"):  # a parenthesis left open: its signs are dropped
            piece = " ".join(piece.replace("(", " ").replace(")", " ").split())
        if not piece:
            continue
        if found and INITIALS.match(piece) and not org_test(found[-1]):
            found[-1] = f"{found[-1]} {piece}"  # "Doe, J." is one piece
            continue
        found += _by(piece)
    whole = org_test(part)
    split: list[str] = []
    for piece in found:
        # an organisation's name stays whole ("Fakeorg Institute for Research and Development", "Fakeorg -
        # Lebanon"), unless a person stands in it ("Jane D. Doe and the Fakeorg Group", "Jane Doe/Fakeorg")
        signs = _signs(piece)
        if len(signs) > 1 and whole and org_test(piece) and not any(_holds_person(p) for p in signs):
            signs = [piece]
        for sign in signs:
            cut = [p.strip(_TRIM) for p in _AND.split(sign) if p.strip(_TRIM)]
            if not whole or not org_test(sign) or any(_holds_person(p) for p in cut):
                for one in cut:
                    split += _person_lead(_bare(one))
            else:
                split += _person_lead(_bare(sign))
    return [p for p in split if p]


def _generic(text: str) -> bool:
    """Whether ``text`` holds only common or generic words ("Report", "This study"), or nothing."""
    words = privacy.GENERIC_WORDS | watch_people.COMMON_WORDS | privacy.STOP
    return all(w in words for w in _folded_words(text))


def _by(piece: str) -> list[str]:
    """``piece`` cut on "by" ("Report prepared by Jane Doe for Fakeorg": the generic words before it are left
    out)."""
    found = [p.strip(_TRIM) for p in _BY.split(f" {piece}")]
    if len(found) > 1 and _generic(found[0]):
        found = found[1:]
    return [p for p in found if p]


def _signs(piece: str) -> list[str]:
    """``piece`` cut on the signs of :data:`_SIGNS` and at the end of a sentence (never a placeholder such as
    "n/a")."""
    if fold(piece) in EMPTY:
        return [piece]
    sentences, last = [], 0
    for match in _FULL_STOP.finditer(piece):
        word = _LAST_WORD.search(piece, 0, match.start())
        if word is None or len(word.group()) < 3 or word.group().casefold() in ABBREVIATIONS:
            continue
        sentences.append(piece[last : match.start()])
        last = match.end()
    sentences.append(piece[last:])
    found = [p.strip(_TRIM) for sentence in sentences for p in _SIGNS.split(sentence)]
    return [p for p in found if p]


def _bare(piece: str) -> str:
    """``piece`` without the words before a name that join it or say what the person did ("and", "for",
    "consultant", "lead author:"). A role word in capitals is taken off only before a person ("Consultants
    Collective" stays whole)."""
    piece = _JOIN_LEAD.sub("", piece).strip(_TRIM)
    found = _ROLE_LEAD.match(piece)
    if found and found.end() < len(piece):
        rest = piece[found.end() :].strip(_TRIM)
        if rest and (piece[:1].islower() or _person_like(rest)):
            return rest
    return piece


def _holds_person(piece: str) -> bool:
    """Whether a person stands in ``piece`` (once the words before a name are taken off, and a person before
    "of"/"for" an organisation is cut from it)."""
    return any(_person_like(p) for p in _person_lead(_bare(piece)))


def _person_like(piece: str) -> bool:
    """Whether a piece reads as a person by itself (rules c, d and g), not as an organisation."""
    if _name_with_org_word(piece):
        return True
    if org_test(piece) or _is_place(piece):
        return False
    return bool(INITIAL.search(piece) or SURNAME_INITIAL.match(piece) or _capitals_name(piece)) or (
        _person_shape(piece) and len(_tokens(piece)) >= 2
    )


_LEAD = re.compile(r"^(?P<lead>.+?)\s+(?:of|for|at|from)\s+(?P<rest>.+)$")


def _person_lead(piece: str) -> list[str]:
    """ "Jane Doe of Fakeorg Systems International" is a person and an organisation; "University of
    Fakeplace" is one organisation."""
    piece = re.sub(r"^(?:and|&)\s+", "", piece, flags=re.IGNORECASE).strip(_TRIM)
    found = _LEAD.match(piece)
    if found and org_test(found.group("rest")) and _person_like(found.group("lead")):
        return [found.group("lead"), found.group("rest")]
    return [piece]


@dataclass
class Authors:
    """One entry, classed: the organisations it names (as written), its person pieces (as written, for the
    name forms), how many pieces are neither (counted, never named) and the pieces an administrator listed
    as words (left out)."""

    orgs: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    unclassed: int = 0
    words: list[str] = field(default_factory=list)

    @property
    def counted(self) -> int:
        return len(self.people) + self.unclassed


def _listed(part_folded: str, known: dict[str, str]) -> str | None:
    """The kind an administrator gave this part (or the start of it, before " (" or ",")."""
    if part_folded in known:
        return known[part_folded]
    for name, kind in known.items():
        if part_folded.startswith((f"{name} (", f"{name},")):
            return kind
    return None


def classify(text: str, known: dict[str, str] | None = None) -> Authors:
    """``text`` (an author entry) classed piece by piece, with ``known`` the administrators' list (folded
    name → kind)."""
    known = known or {}
    found = Authors()
    text = _ET_AL.sub(" ", watch_people.visible(text or ""))
    for raw in _PARTS.split(text):
        part = " ".join(raw.split()).strip(_TRIM)
        folded = fold(part)
        if folded in EMPTY:
            continue
        kind = _listed(folded, known)
        if kind == ORGANISATION:
            found.orgs.append(part)
            continue
        if kind == PERSON:
            found.people.append(part)
            continue
        if kind == WORDS:
            found.words.append(part)
            continue
        if _title(part):  # a journal's title, not an author: left out
            found.words.append(part)
            continue
        last = ""  # the kind of the piece before
        for piece in pieces(part):
            if fold(piece) in EMPTY:
                continue
            kind = _piece_kind(piece, known, last)
            if kind == "place":
                if last == ORGANISATION:
                    found.orgs[-1] = f"{found.orgs[-1]}, {piece}"
                else:
                    found.orgs.append(piece)
                kind = ORGANISATION
            elif kind == ORGANISATION:
                found.orgs.append(piece)
            elif kind == PERSON:
                found.people.append(piece)
            elif kind == WORDS:
                found.words.append(piece)
            else:
                found.unclassed += 1
            last = kind
    return found


def _piece_kind(piece: str, known: dict[str, str], last: str) -> str:
    """The kind of one piece (step 5 of the rule): an organisation, a person, words (left out), "place" (it
    joins the organisation before it) or "" (counted, never named). ``last``: the kind of the piece before."""
    listed = known.get(fold(piece))
    if listed:
        return listed
    if _VENUE.match(piece):  # where it was published: not an author
        return WORDS
    if last == PERSON and _ROLE_ONLY.fullmatch(piece):  # "Jane Doe (consultant)": the same person
        return WORDS
    if _is_place(piece):  # before the words of an organisation, which hold "Lebanon"
        return "place"
    if _org_words(piece):
        # words all in lower case are no organisation's name ("research", "practice and policy"): counted
        return ORGANISATION if any(t[:1].isupper() for t in _tokens(piece)) else ""
    if _name_with_org_word(piece):
        return PERSON
    if INITIAL.search(piece) or SURNAME_INITIAL.match(piece) or _capitals_name(piece):
        return PERSON
    if _acronyms(piece):
        return ORGANISATION
    return PERSON if _person_shape(piece) else ""


def _title(part: str) -> bool:
    """Whether a part is a title, not an author (a journal's at the end of an entry: "Fake journal: theory,
    research and practice"): a colon before words in lower case that say no role ("Fakeorg Team: consultant
    Jane Doe" is a person), and no person in it."""
    found = _TITLE_COLON.search(part)
    if found is None or _ROLE_LEAD.match(part[found.end() :]):
        return False
    return not any(_holds_person(p) for p in re.split(r"[,:]", part) if p.strip())


def cell(orgs: list[str], counted: int) -> str:
    """A downloads cell: the organisations, "Authors: N", or both ("Fakeorg Institute + 2 authors")."""
    named = "; ".join(dict.fromkeys(orgs))
    if named and counted:
        return f"{named} + {counted} author" + ("" if counted == 1 else "s")
    if named:
        return named
    return f"Authors: {counted}" if counted else ""


def safe_orgs(authors: Authors, people: People) -> tuple[list[str], int]:
    """The organisations a download may name, and the count of authors: an organisation whose text still
    holds a person's name or a citation (the final check) is counted as an author instead."""
    orgs, counted = [], authors.counted
    seen: set[str] = set()
    for org in authors.orgs:
        key = fold(org)
        if key in seen:
            continue
        seen.add(key)
        if privacy.for_download(org, people) != org or privacy.leftover(org, people) or _named_after(org):
            counted += 1
        else:
            orgs.append(org)
    return orgs, counted


# a name after "by" or a role word, inside an organisation's string ("Fakeorg Foundation report by Fakename")
_NAMED_AFTER = re.compile(
    rf"(?:\b(?i:by|(?:co-?)?authors?|researchers?|consultants?|investigators?|editors?)\s*:?\s+)"
    rf"(?P<name>{privacy.NAME})"
)


def _named_after(org: str) -> bool:
    return any(not privacy.kept(match.group("name")) for match in _NAMED_AFTER.finditer(org))


def for_downloads(text: str, known: dict[str, str] | None = None, people: People | None = None) -> str:
    """``text`` (an author entry) as a download writes it (see the module's docstring)."""
    authors = classify(text, known)
    orgs, counted = safe_orgs(authors, people or privacy.NOBODY)
    return cell(orgs, counted)


# ------------------------------------------------------------------------------- the people of a version
def _initials(token: str) -> bool:
    """ "J", "J.", "JM", "J.-M.": initials, not a surname in capitals ("DOE")."""
    return bool(INITIALS.match(token)) and (len(re.sub(r"[.\s-]", "", token)) <= 2 or "." in token)


def surname(piece: str) -> str:
    """A person piece's surname: the word before a comma ("Doe, J."), the word in capitals ("DOE Jane"),
    or the last capitalised word ("Jane Doe", "J. Doe"); "" when it is shorter than 3 letters or a word of
    an organisation, a place, a month or a common word."""
    tokens = [t for t in _tokens(piece) if not _initials(t) and t.casefold() not in PARTICLES]
    if not tokens:
        return ""
    if "," in piece:
        word = _tokens(piece.split(",", 1)[0])[-1:] or [""]
        chosen = word[0]
    elif SURNAME_INITIAL.match(piece):
        chosen = tokens[0]
    else:
        caps = [t for t in tokens if t.isupper() and len(t) >= 2]
        titled = [t for t in tokens if t[:1].isupper()]
        chosen = caps[0] if caps and len(caps) < len(tokens) else (titled[-1] if titled else "")
    folded = watch_people.fold(chosen).strip(".'’")
    excluded = (
        ORG_WORDS
        | privacy.PLACE_WORDS
        | privacy.MONTHS
        | privacy.GENERIC_WORDS
        | watch_people.COMMON_WORDS
        | privacy.STOP
    )
    if len(re.sub(r"[^\w]", "", folded)) < 3 or folded in excluded:
        return ""
    return folded


def gather(rows, known: dict[str, str], ordinary=()) -> People:
    """The People of ``rows`` ((Document ID, author entry) pairs). ``ordinary``: the surnames the texts also
    write as ordinary words (:func:`ordinary_words`, kept on the version by its load)."""
    names: set[str] = set()
    surnames: set[str] = set()
    own: dict[str, set[str]] = {}
    bare: dict[str, bool] = {}  # a surname → whether every piece it comes from is one bare word
    cache: dict[str, Authors] = {}
    for document, entity in rows:
        if not entity:
            continue
        authors = cache.get(entity)
        if authors is None:
            authors = cache[entity] = classify(entity, known)
        for piece in authors.people:
            names |= watch_people.name_forms(piece)
            found = surname(piece)
            if found:
                surnames.add(found)
                own.setdefault(document, set()).add(found)
                listed = known.get(fold(piece)) == PERSON  # an administrator said it is a person
                bare[found] = bare.get(found, True) and _one_bare_word(piece) and not listed
    for name, kind in known.items():
        if kind == PERSON:
            names |= watch_people.name_forms(name)
            found = surname(name)
            if found:
                surnames.add(found)
                bare[found] = False  # an administrator said it is a person
    words = frozenset(ordinary) & frozenset(surnames)
    return People(
        names=frozenset(names),
        surnames=frozenset(surnames),
        own={record: frozenset(found) for record, found in own.items()},
        ordinary=words,
        ordinary_alone=frozenset(w for w in words if bare.get(w)),
    )


def _one_bare_word(piece: str) -> bool:
    """ "Fakeword": an author piece of one word, with no first name and no initial."""
    tokens = _tokens(piece)
    return len(tokens) == 1 and not INITIAL.search(piece)


def known_list() -> dict[str, str]:
    """The administrators' list: folded name → kind (one query)."""
    from .models import KMOrganisation

    return dict(KMOrganisation.objects.values_list("folded", "kind"))


def entity_rows() -> list[tuple[str, str]]:
    """Every author entry of the KM Database, with its Document ID: both sheets', each pair once (one
    query)."""
    from .models import KMDocument, KMFinding

    documents = KMDocument.objects.order_by().exclude(entity="").values_list("record_id", "entity")
    findings = KMFinding.objects.order_by().exclude(entity="").values_list("record_id", "entity")
    return list(documents.union(findings))


_MEMO: dict[tuple, tuple[dict[str, str], People]] = {}


def people(version) -> People:
    """The KM Database's own people for the version shown: never None, never NeuroDB's list of the people
    it knows. Kept in this process until the rows or the administrators' list change (two small queries a
    use, so a correction changes downloads at once, and a new version's rows are read at once, whichever
    process loaded them)."""
    return context(version)[1]


def context(version) -> tuple[dict[str, str], People]:
    """The administrators' list and the People of ``version`` (see :func:`people`). The key is read from
    the tables each time (the newest document's pk: every load writes new rows), never from the version
    shown, which a worker keeps 10 seconds: a version loaded by another process is never served with the
    people of the version before it."""
    from .models import KMDocument, KMOrganisation, KMVersion

    state = KMOrganisation.objects.aggregate(n=Count("pk"), last=Max("changed_at"))
    rows = KMDocument.objects.aggregate(top=Max("pk"))["top"]
    key = (version is not None, rows, state["n"], state["last"])
    found = _MEMO.get(key)
    if found is None:
        known = known_list()
        if version is None:
            found = (known, privacy.NOBODY)
        else:
            shown = KMVersion.objects.filter(status=KMVersion.Status.LIVE)
            ordinary = shown.values_list("ordinary_words", flat=True).first() or ()
            found = (known, gather(entity_rows(), known, ordinary))
        if len(_MEMO) >= 4:
            _MEMO.clear()
        _MEMO[key] = found
    return found


def forget() -> None:
    _MEMO.clear()


class Namer:
    """``for_downloads`` of one download, kept per distinct entry (an entry repeats on every finding of
    its document)."""

    def __init__(self, known: dict[str, str], people: People):
        self.known, self.people = known, people
        self._seen: dict[str, str] = {}

    def __call__(self, text: str) -> str:
        found = self._seen.get(text)
        if found is None:
            found = self._seen[text] = for_downloads(text, self.known, self.people) if text else ""
        return found


# --------------------------------------------------------------------------- what the admin reviews
def summary(known: dict[str, str], people_: People) -> dict[str, int]:
    """How the downloads show the documents' authors: organisations only, authors only (counted), both,
    none (one query)."""
    from .models import KMDocument

    counts = Counter({"organisations": 0, "authors_only": 0, "mixed": 0, "empty": 0})
    namer = Namer(known, people_)
    for entity in KMDocument.objects.order_by().values_list("entity", flat=True):
        shown = namer(entity)
        if not shown:
            counts["empty"] += 1
        elif shown.startswith("Authors: "):
            counts["authors_only"] += 1
        elif " + " in shown and shown.rsplit(" + ", 1)[1].split(" ")[0].isdigit():
            counts["mixed"] += 1
        else:
            counts["organisations"] += 1
    return dict(counts)


def named_organisations(known: dict[str, str], people_: People) -> list[str]:
    """Every distinct string the downloads name as an organisation, for the uploader to review."""
    found: dict[str, str] = {}
    cache: dict[str, list[str]] = {}
    for _document, entity in entity_rows():
        orgs = cache.get(entity)
        if orgs is None:
            orgs = cache[entity] = safe_orgs(classify(entity, known), people_)[0]
        for org in orgs:
            found.setdefault(fold(org), org)
    return sorted(found.values(), key=str.casefold)


WORDS_SEEN = 20  # a name form found in more findings than this looks like words, not a name
LOWER_SEEN = 5  # a surname the texts write in lower case this often is also an ordinary word
# the texts of a finding where a surname may also be an ordinary word, as the load reads them
LOWER_FIELDS = ("key_finding", "measure", "analysis", "limitations", "population", "methods")
_LOWER_WORD = re.compile(r"(?<![\w'’\-])[^\W\d_][\w\-]*(?![\w\-])")


def lower_words(values: dict, counts: Counter) -> None:
    """Count in ``counts`` the words that a finding's texts (``values``, by field) write in lower case."""
    for name in LOWER_FIELDS:
        text = values.get(name)
        if text:
            counts.update(w for w in _LOWER_WORD.findall(text) if w.islower())


def ordinary_words(people_: People, counts: Counter) -> list[str]:
    """The surnames of ``people_`` that the texts (``counts``, from :func:`lower_words`) write in lower case
    :data:`LOWER_SEEN` times or more: ordinary words too ("inclusion"). The load keeps them on the version."""
    folded: Counter = Counter()
    for word, n in counts.items():
        folded[watch_people.fold(word)] += n
    return sorted(s for s in people_.surnames if folded[s] >= LOWER_SEEN)


def look_like_words(known: dict[str, str], ordinary: frozenset[str] = frozenset()) -> list[str]:
    """The author pieces whose name looks like ordinary words: found as a phrase in the texts of more than
    :data:`WORDS_SEEN` findings (one statement), or whose surname the texts also write in lower case as an
    ordinary word (``ordinary``: :func:`ordinary_words`, kept on the version by its load). They stay
    withheld until an administrator marks them "Not a name" (or an organisation)."""
    pieces_of: dict[str, set[str]] = {}
    found: dict[str, str] = {}
    cache: dict[str, Authors] = {}
    for _document, entity in entity_rows():
        authors = cache.get(entity)
        if authors is None:
            authors = cache[entity] = classify(entity, known)
        for piece in authors.people:
            if known.get(fold(piece)):
                continue
            if ordinary and surname(piece) in ordinary:
                found.setdefault(fold(piece), piece)
            for form in watch_people.name_forms(piece):
                # a form with an initial or a particle ("al s") is found after any "et al."
                if all(len(w) > 1 and w not in PARTICLES for w in form.split(" ")):
                    pieces_of.setdefault(form, set()).add(piece)
    forms = sorted(pieces_of)
    if forms and connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT f, (SELECT count(*) FROM km_finding "
                "WHERE search_vector @@ phraseto_tsquery('simple', f)) FROM unnest(%s::text[]) AS f",
                [forms],
            )
            frequent = [form for form, n in cursor.fetchall() if n > WORDS_SEEN]
        for form in frequent:
            for piece in pieces_of[form]:
                found.setdefault(fold(piece), piece)
    return sorted(found.values(), key=str.casefold)
