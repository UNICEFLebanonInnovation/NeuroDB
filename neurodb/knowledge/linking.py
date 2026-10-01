"""What in NeuroDB a text mentions: partners (name, short name, vendor number), programme documents
(reference number), sections, governorates and districts.

Matching is literal and whole-word, so a link is only made when the name is written in the text;
names the AI read in the document (``resolve``) go through the same lookups, so a link always points
at something that exists. Short or common words are left out (a partner called "North" would match
every compass point).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .models import Link

MIN_NAME = 6  # a full name shorter than this is too likely to be a common word
MIN_SHORT = 3
AMBIGUOUS_PLACES = {
    "north",
    "south",
    "centre",
    "center",
    "mount",
}  # need "North Lebanon", "South governorate"


@dataclass
class Found:
    kind: str
    object_id: int
    label: str
    mentions: int


def _norm(text: str) -> str:
    return " ".join(text.split())


def _words(name: str, flags: int = re.IGNORECASE) -> re.Pattern:
    # spaces and hyphens in a name match any run of spaces or a hyphen ("Baalbek-Hermel", "Baalbek Hermel")
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", name.strip()) if p]
    return re.compile(r"(?<!\w)" + r"[\s\-]+".join(parts) + r"(?!\w)", flags)


def _count(pattern: re.Pattern, text: str) -> int:
    return len(pattern.findall(text))


def _programmes(text: str, low: str) -> list[Found]:
    from neurodb.partnerships.models import PCA

    found: dict[int, Found] = {}
    if "/" not in text:
        return []
    for pk, number, title in (
        PCA.objects.exclude(number__isnull=True).exclude(number="").values_list("pk", "number", "title")
    ):
        ref = number.strip().lower()
        base = re.sub(r"-\d+$", "", ref)  # the amendment suffix ("…-1") is often left out
        n = low.count(ref) or (low.count(base) if len(base) >= 8 else 0)
        if n:
            found[pk] = Found(Link.Kind.PROGRAMME, pk, f"{number} {title or ''}".strip()[:300], n)
    return list(found.values())


def _partners(text: str) -> list[Found]:
    from neurodb.partnerships.models import PartnerOrganization

    found = []
    for pk, name, short, alternate, vendor in PartnerOrganization.objects.values_list(
        "pk", "name", "short_name", "alternate_name", "vendor_number"
    ):
        n = 0
        for full in {_norm(name or ""), _norm(alternate or "")}:
            if len(full) >= MIN_NAME:
                n += _count(_words(full), text)
        short = (short or "").strip()
        if len(short) >= MIN_SHORT and short.lower() not in {(name or "").strip().lower()}:
            # an acronym as written ("AMEL") or in capitals; never ignoring case ("care" is a word)
            for variant in {short, short.upper()}:
                n += _count(_words(variant, 0), text)
        vendor = (vendor or "").strip()
        if len(vendor) >= 6:
            n += _count(_words(vendor, 0), text)
        if n:
            found.append(Found(Link.Kind.PARTNER, pk, (name or short)[:300], n))
    return found


def _sections(text: str) -> list[Found]:
    from neurodb.accounts.models import Section

    found = []
    for pk, name in Section.objects.values_list("pk", "name"):
        name = _norm(name or "")
        if len(name) >= 4 and (n := _count(_words(name), text)):
            found.append(Found(Link.Kind.SECTION, pk, name, n))
    return found


def _places(text: str) -> list[Found]:
    from neurodb.geo.models import DistrictLocation, GovernorateLocation

    found = []
    for kind, model in ((Link.Kind.GOVERNORATE, GovernorateLocation), (Link.Kind.DISTRICT, DistrictLocation)):
        for pk, name in model.objects.values_list("pk", "name"):
            name = _norm(name or "")
            if len(name) < 4:
                continue
            if name.lower() in AMBIGUOUS_PLACES:
                pattern = re.compile(_words(name).pattern + r"[\s\-]+(?:lebanon|governorate)(?!\w)", re.I)
            else:
                pattern = _words(name)
            if n := _count(pattern, text):
                found.append(Found(kind, pk, name, n))
    return found


def detect(text: str) -> list[Found]:
    """Everything in NeuroDB ``text`` names, with how often."""
    return [*_programmes(text, text.lower()), *_partners(text), *_sections(text), *_places(text)]


def resolve(organisations: Iterable[str], places: Iterable[str], references: Iterable[str]) -> list[Found]:
    """Names the AI found in a document, matched to NeuroDB records (exact names first, then a
    partner whose name contains the mention, when only one does)."""
    from django.db.models import Q

    from neurodb.geo.models import DistrictLocation, GovernorateLocation
    from neurodb.partnerships.models import PCA, PartnerOrganization

    found: dict[tuple[str, int], Found] = {}

    def add(kind: str, pk: int, label: str) -> None:
        found.setdefault((kind, pk), Found(kind, pk, label[:300], 0))

    for mention in {_norm(m) for m in organisations if m and len(_norm(m)) >= MIN_SHORT}:
        exact = PartnerOrganization.objects.filter(
            Q(name__iexact=mention) | Q(short_name__iexact=mention) | Q(alternate_name__iexact=mention)
        )[:2]
        matches = list(exact)
        if not matches and len(mention) >= 8:
            matches = list(PartnerOrganization.objects.filter(name__icontains=mention)[:2])
        if len(matches) == 1:
            add(Link.Kind.PARTNER, matches[0].pk, matches[0].name)
    for mention in {_norm(m) for m in places if m and len(_norm(m)) >= 4}:
        for kind, model in (
            (Link.Kind.GOVERNORATE, GovernorateLocation),
            (Link.Kind.DISTRICT, DistrictLocation),
        ):
            row = model.objects.filter(name__iexact=mention).first()
            if row:
                add(kind, row.pk, row.name)
                break
    for mention in {_norm(m) for m in references if m and len(_norm(m)) >= 8}:
        pd = (
            PCA.objects.filter(number__iexact=mention).first()
            or PCA.objects.filter(number__istartswith=mention).first()
        )
        if pd:
            add(Link.Kind.PROGRAMME, pd.pk, f"{pd.number} {pd.title or ''}".strip())
    return list(found.values())
