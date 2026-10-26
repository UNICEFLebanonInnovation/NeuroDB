"""Field monitoring (FM) rules shared by every page that counts eTools field monitoring visits.

eTools writes one ``MonitoringFinding`` row per monitored entity (a programme document, a CP output, a
partner) of a monitoring activity, so a visit is a group of rows. One definition of that group is used
everywhere: :func:`visit_key` in Python and :func:`visit_key_expr`, its SQL twin, for counting. The
key is the eTools activity id ("1722"), else a hash of the activity reference, else the row itself
("f-<pk>"). It is computed, never stored on the finding, so rows written by the sync, the demo or a
test are grouped the same way.

Also here: the rating, status and answer vocabularies (eTools spells them several ways), the entity
kind of a row, the programme document a row is about (:class:`PDResolver`, by the PCA/PD reference
pair so that the ``LEBA/`` or ``LEB/`` prefix and the ``-2`` amendment suffix do not matter; the Datamart sync
links the funds reservations by the number they carry with it, :meth:`PDResolver.candidates`), and the
completed programmatic visits per partner that the assurance page sets against HACT, and per programme
document for the Field monitoring page's HACT visit plan (the same rows: :func:`completed_programmatic_rows`).

This module imports nothing from ``neurodb.fmm``: the Datamart sync, Monitoring insights, the
knowledge hub, NeuroDB Watch, the assurance page and the HACT visit plan all read it.
"""

from __future__ import annotations

import datetime
import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable
from typing import Any

from django.db.models import CharField, Count, F, Max, Min, Q, QuerySet, Value
from django.db.models.expressions import Case, Expression, Func, When
from django.db.models.functions import Cast, Coalesce, Concat, Trim, Upper

VISIT_KEY_MAX = 40
KINDS = ("pd", "cp_output", "partner", "other")
RATINGS = ("on_track", "constrained", "off_track", "not_monitored", "other")
RATING_ORDER = {"off_track": 4, "constrained": 3, "on_track": 2, "other": 1, "not_monitored": 0}
STATUSES = (
    "draft",
    "checklist",
    "review",
    "assigned",
    "data_collection",
    "report_finalization",
    "submitted",
    "completed",
    "cancelled",
)
STATUS_RANK = {
    "cancelled": 0,
    "draft": 1,
    "checklist": 2,
    "review": 3,
    "assigned": 4,
    "data_collection": 5,
    "report_finalization": 6,
    "submitted": 7,
    "completed": 8,
}
STATUS_GROUP = {
    "draft": "planned",
    "checklist": "planned",
    "review": "planned",
    "assigned": "planned",
    "data_collection": "in_progress",
    "report_finalization": "in_progress",
    "submitted": "reported",
    "completed": "reported",
    "cancelled": "cancelled",
}
# "LEBA/PCA2023597/PD2025123-2: ..." -> ("PCA2023597", "PD2025123")
PD_TOKEN = re.compile(r"(PCA\d{4,})\W+((?:S?H?PD|SSFA|GDD)\d{4,})", re.I)
# a PD part alone, without its PCA: "PD2025123", "SPD2025123", "SSFA2024001"
PD_PART = re.compile(r"(?:S?H?PD|SSFA|GDD)\d{4,}")
AMENDMENT = re.compile(r"-\d+$")
COUNTRY_PREFIX = re.compile(r"^[A-Z]{2,5}/")  # "LEB/" or "LEBA/" before an eTools reference

_RATING_WORDS = {
    "on track": "on_track",
    "ontrack": "on_track",
    "green": "on_track",
    "off track": "off_track",
    "offtrack": "off_track",
    "red": "off_track",
    "constrained": "constrained",
    "partially": "constrained",
    "partially on track": "constrained",
    "amber": "constrained",
    "yellow": "constrained",
    "": "not_monitored",
    "none": "not_monitored",
    "n/a": "not_monitored",
    "na": "not_monitored",
    "not monitored": "not_monitored",
    "not applicable": "not_monitored",
}
_STATUS_ALIASES = {
    "canceled": "cancelled",
    "complete": "completed",
    "report_finalisation": "report_finalization",
    "datacollection": "data_collection",
    "reportfinalization": "report_finalization",
}
_YES = frozenset({"yes", "y", "true", "oui", "نعم"})
_NO = frozenset({"no", "n", "false", "non", "لا"})
_PD_TYPE = re.compile(r"\b(pd|spd|hpd|shpd|ssfa|intervention|programme document)\b|pd/s?sfa", re.I)
_CP_OUTPUT_TEXT = re.compile(r"^\d+\.\d+\s")


# ---------------------------------------------------------------------------------- visits
def norm_reference(value: Any) -> str:
    """Strip, collapse runs of whitespace to one space, upper-case. '' for None."""
    if value is None:
        return ""
    return " ".join(str(value).split()).upper()


def _activity_id(value: Any) -> int | None:
    """A positive eTools activity id from an int or a digit string; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value or "").strip()
    # isdecimal, not isdigit: "²" is a digit that int() cannot read
    if text.isdecimal() and int(text) > 0:
        return int(text)
    return None


def visit_key(activity_id: Any, reference: Any, finding_pk: int) -> str:
    """'1722' when activity_id is a positive int (or a digit string); else 'r-' + sha1(norm_reference)[:12]
    when the reference is not blank; else f'f-{finding_pk}'. Always <= VISIT_KEY_MAX, slug-safe."""
    number = _activity_id(activity_id)
    if number is not None:
        return str(number)[:VISIT_KEY_MAX]
    folded = norm_reference(reference)
    if folded:
        return "r-" + hashlib.sha1(folded.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"f-{finding_pk}"


def visit_key_expr() -> Expression:
    """SQL expression with the same partition as :func:`visit_key` over ``MonitoringFinding``: the
    activity id, else "r:" + the normalised reference, else "f-<pk>". The values differ from
    :func:`visit_key` (no hash in SQL), the grouping does not."""
    collapsed = Func(
        F("monitoring_activity"),
        Value(r"\s+"),
        Value(" "),
        Value("g"),
        function="REGEXP_REPLACE",
        output_field=CharField(),
    )
    return Case(
        When(monitoring_activity_id__gt=0, then=Cast("monitoring_activity_id", CharField())),
        # a reference with at least one non-blank character, as norm_reference() sees it
        When(
            Q(monitoring_activity__regex=r"\S"),
            then=Concat(Value("r:"), Upper(Trim(collapsed)), output_field=CharField()),
        ),
        default=Concat(Value("f-"), Cast("id", CharField()), output_field=CharField()),
        output_field=CharField(),
    )


def count_visits(findings: QuerySet) -> int:
    """Distinct visits among finding rows (:func:`visit_key_expr`)."""
    return findings.order_by().annotate(vk=visit_key_expr()).values("vk").distinct().count()


def finding_year_q(year: int) -> Q:
    """The finding rows of a calendar year: start date in the year, else (eTools left the start blank)
    end date in the year. Every page that counts field monitoring visits by year reads it, as
    Monitoring insights dates a visit (``fmm.Visit.visit_date``: its start date, else its end date)."""
    return Q(start_date__year=year) | Q(start_date=None, end_date__year=year)


def finding_date() -> Expression:
    """A finding row's date, in SQL: its start date, else its end date (:func:`finding_year_q`)."""
    return Coalesce("start_date", "end_date")


def visits_by_year(findings: QuerySet) -> dict[int, int]:
    """Visits per year of their date: the earliest start date of their rows, else the latest end date
    (as ``fmm.Visit.visit_date``); rows with neither are left out."""
    rows = (
        findings.exclude(start_date=None, end_date=None)
        .order_by()
        .annotate(vk=visit_key_expr())
        .values("vk")
        .annotate(first=Min("start_date"), last=Max("end_date"))
        .values_list("first", "last")
    )
    return dict(sorted(Counter((first or last).year for first, last in rows if first or last).items()))


# ---------------------------------------------------------------------------------- vocabularies
def _fold(raw: Any) -> str:
    """Lower case, stripped, with '-', '_' and runs of spaces as one space."""
    text = str(raw or "").strip().lower().replace("-", " ").replace("_", " ")
    return " ".join(text.split())


def normalize_rating(raw: Any) -> str:
    """An eTools rating word as a code of :data:`RATINGS` ("On Track", "on-track", "ONTRACK" and
    "green" are all ``on_track``; a blank is ``not_monitored``; a word not listed is ``other``)."""
    return _RATING_WORDS.get(_fold(raw), "other")


def normalize_status(raw: Any) -> str:
    """'Data Collection' -> 'data_collection', 'canceled' -> 'cancelled'; '' when not an eTools status."""
    code = _fold(raw).replace(" ", "_")
    code = _STATUS_ALIASES.get(code, code)
    return code if code in STATUS_RANK else ""


def status_group(status: str) -> str:
    """planned, in_progress, reported or cancelled; unknown for anything else."""
    return STATUS_GROUP.get(status, "unknown")


def normalize_answer(raw: Any) -> str:
    """The code of an answer (after its option label is looked up): a rating code (on_track,
    constrained, off_track) when it is one, else yes or no, else ''. Never the answer's text."""
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    rating = normalize_rating(raw)
    if rating in ("on_track", "constrained", "off_track"):
        return rating
    folded = _fold(raw)
    if folded in _YES:
        return "yes"
    if folded in _NO:
        return "no"
    return ""


def entity_kind(entity_type: Any, entity: Any = "") -> str:
    """What a finding row is about: 'pd', 'cp_output', 'partner' or 'other', from its entity type,
    else from the entity's text (a PCA/PD reference, or an output numbered like "2.2 ...")."""
    kind_text = str(entity_type or "")
    if _PD_TYPE.search(kind_text):
        return "pd"
    lowered = kind_text.lower()
    if "output" in lowered:
        return "cp_output"
    if "partner" in lowered:
        return "partner"
    text = str(entity or "").strip()
    if PD_TOKEN.search(text):
        return "pd"
    if _CP_OUTPUT_TEXT.match(text):
        return "cp_output"
    return "other"


def pd_token(text: Any) -> tuple[str, str] | None:
    """('PCA2023597', 'PD2025123') upper-cased, from 'LEBA/PCA2023597/PD2025123-2: …'; None without one."""
    match = PD_TOKEN.search(str(text or ""))
    if not match:
        return None
    return match.group(1).upper(), match.group(2).upper()


def _base(number: Any) -> str:
    """A reference without its country prefix and amendment suffix: 'LEBA/SSFA2024001-2' and
    'LEB/SSFA2024001' are both 'SSFA2024001'."""
    return AMENDMENT.sub("", COUNTRY_PREFIX.sub("", norm_reference(number)))


def pd_part(number: Any) -> str:
    """The PD part of a reference, its amendment removed: 'PD2025123' of 'LEBA/PCA2023597/PD2025123-2', of
    ' pd2025123 ' and of 'PD2025123-1'; an SSFA or GDD number alone the same way. '' when it has none."""
    token = pd_token(number)
    if token:
        return token[1]
    base = _base(number)
    return base if PD_PART.fullmatch(base) else ""


# ---------------------------------------------------------------------------------- programme documents
class PDResolver:
    """entity text -> PCA pk. Built once per run from PCA(pk, number, title, partner_id, start, end)."""

    def __init__(self, rows: Iterable[tuple] | None = None) -> None:
        if rows is None:
            from neurodb.partnerships.models import PCA

            rows = PCA.objects.values_list("pk", "number", "title", "partner_id", "start", "end")
        self._end: dict[int, tuple[datetime.date | None, datetime.date | None]] = {}
        self._exact: dict[str, list[int]] = defaultdict(list)
        self._token: dict[tuple[str, str], list[int]] = defaultdict(list)
        self._base: dict[str, list[int]] = defaultdict(list)
        self._title: dict[tuple[int, str], list[int]] = defaultdict(list)
        self._part: dict[str, list[int]] = defaultdict(list)
        for pk, number, title, partner_id, start, end in rows:
            self._end[pk] = (start, end)
            folded = norm_reference(number)
            if folded:
                self._exact[folded].append(pk)
                self._base[_base(folded)].append(pk)
                token = pd_token(folded)
                if token:
                    self._token[token].append(pk)
                if part := pd_part(folded):
                    self._part[part].append(pk)
            if partner_id and norm_reference(title):
                self._title[(partner_id, norm_reference(title))].append(pk)

    def pick(self, candidates: list[int], visit_date: datetime.date | None) -> int:
        """Of several programme documents (amendments, mostly): the one whose dates cover the visit (or the
        day of a funds reservation line), else the one that ends last, else the highest pk."""
        if len(candidates) == 1:
            return candidates[0]

        def covers(pk: int) -> bool:
            start, end = self._end[pk]
            return bool(visit_date and start and end and start <= visit_date <= end)

        pool = [pk for pk in candidates if covers(pk)] or candidates
        return max(pool, key=lambda pk: (self._end[pk][1] or datetime.date.min, pk))

    def resolve(
        self, entity: str, kind: str, partner_id: int | None, visit_date: datetime.date | None
    ) -> tuple[int | None, str]:
        """First hit wins; returns (pk, how):
        'exact'  folded PCA.number == folded entity;
        'token'  pd_token(entity) == pd_token(PCA.number) (country prefix and amendment ignored);
        'base'   the numbers without country prefix and amendment suffix are equal (SSFA references
                 without a PCA token);
        'title'  folded PCA.title == folded entity, only among PCAs of partner_id;
        several candidates at one step -> the PCA whose start..end covers visit_date, else the latest
        end, else the highest pk. kind != 'pd' resolves only by 'exact' or 'token'. Never 'the
        partner's only PD'. (None, '') otherwise."""
        folded = norm_reference(entity)
        if not folded:
            return None, ""
        if found := self._exact.get(folded):
            return self.pick(found, visit_date), "exact"
        token = pd_token(folded)
        if token and (found := self._token.get(token)):
            return self.pick(found, visit_date), "token"
        if kind != "pd":
            return None, ""
        if found := self._base.get(_base(folded)):
            return self.pick(found, visit_date), "base"
        if partner_id and (found := self._title.get((partner_id, folded))):
            return self.pick(found, visit_date), "title"
        return None, ""

    def resolve_reference(self, reference: str, visit_date: datetime.date | None) -> tuple[int | None, str]:
        """A programme document *reference* written in a record (not an entity): by the PCA/PD pair first
        (a base number then finds the amendment that covers the visit), then 'exact', then 'base'.
        (None, '') when none."""
        folded = norm_reference(reference)
        if not folded:
            return None, ""
        token = pd_token(folded)
        if token and (found := self._token.get(token)):
            return self.pick(found, visit_date), "token"
        if found := self._exact.get(folded):
            return self.pick(found, visit_date), "exact"
        if found := self._base.get(_base(folded)):
            return self.pick(found, visit_date), "base"
        return None, ""

    def candidates(self, reference: Any) -> tuple[list[int], str]:
        """The programme documents a reference number written in a record may name, at the first step that
        finds any, and how: 'exact' (the same number, case and spaces aside), 'token' (the same PCA/PD pair:
        another amendment), 'base' (the same number without country prefix and amendment), 'pd_part' (the same
        PD part alone, the PCA part ignored: a bare 'PD2025123'). Never picks one of several: the caller
        decides (``datamart_sync.Links.by_number_first``, Raw eTools data's "names no PD" filter). ([], '')
        when none."""
        folded = norm_reference(reference)
        if not folded:
            return [], ""
        if found := self._exact.get(folded):
            return list(found), "exact"
        token = pd_token(folded)
        if token and (found := self._token.get(token)):
            return list(found), "token"
        if found := self._base.get(_base(folded)):
            return list(found), "base"
        part = pd_part(folded)
        if part and (found := self._part.get(part)):
            return list(found), "pd_part"
        return [], ""


def update_rows(model: type, rows: list, names: Iterable[str], batch_size: int = 2000) -> None:
    """Write the fields ``names`` of ``rows`` (saved model instances): ``bulk_update`` without its CASE
    expressions, which grow too slow with thousands of rows and several columns. One prepared UPDATE
    per row, sent in batches (``executemany``)."""
    from django.db import connection

    fields = [model._meta.get_field(name) for name in names]
    quote = connection.ops.quote_name
    sql = "UPDATE {} SET {} WHERE {} = %s".format(  # noqa: S608 - quoted model names; values are parameters
        quote(model._meta.db_table),
        ", ".join(f"{quote(f.column)} = %s" for f in fields),
        quote(model._meta.pk.column),
    )
    with connection.cursor() as cursor:
        for start in range(0, len(rows), batch_size):
            cursor.executemany(
                sql,
                [
                    [f.get_db_prep_save(getattr(row, f.attname), connection) for f in fields] + [row.pk]
                    for row in rows[start : start + batch_size]
                ],
            )


def relink_findings(rows: QuerySet | None = None) -> dict[str, int]:
    """Resolve intervention/pd_match for rows with intervention_id NULL (or `rows`), written in 2,000s.
    Returns {'exact','token','base','title','unresolved','pd_kind_rows'} counts."""
    from neurodb.datamart.models import MonitoringFinding

    if rows is None:
        rows = MonitoringFinding.objects.filter(intervention_id=None)
    resolver = PDResolver()
    counts = dict.fromkeys(("exact", "token", "base", "title", "unresolved", "pd_kind_rows"), 0)
    changed: list[MonitoringFinding] = []
    fields = ("pk", "entity", "entity_type", "partner_id", "end_date", "intervention_id", "pd_match")
    for row in rows.order_by("pk").only(*fields).iterator(chunk_size=2000):
        kind = entity_kind(row.entity_type, row.entity)
        pk, how = resolver.resolve(row.entity, kind, row.partner_id, row.end_date)
        if kind == "pd":
            counts["pd_kind_rows"] += 1
            if pk is None:
                counts["unresolved"] += 1
        if how:
            counts[how] += 1
        if (row.intervention_id, row.pd_match) != (pk, how):
            row.intervention_id, row.pd_match = pk, how
            changed.append(row)
        if len(changed) >= 2000:
            update_rows(MonitoringFinding, changed, ["intervention", "pd_match"])
            changed = []
    if changed:
        update_rows(MonitoringFinding, changed, ["intervention", "pd_match"])
    return counts


# ---------------------------------------------------------------------------------- assurance, places
def programmatic_visits_by_partner(year: int) -> dict[int, int]:
    """{partner_id: distinct visit keys} over rows with is_programmatic_visit, status__iexact='completed',
    end_date__year=year, partner_id not null (§0.3). A visit with several partners counts for each."""
    rows = (
        completed_programmatic_rows(year)
        .exclude(partner_id=None)
        .order_by()
        .annotate(vk=visit_key_expr())
        .values("partner_id")
        .annotate(n=Count("vk", distinct=True))
    )
    return {r["partner_id"]: r["n"] for r in rows}


def in_location_subtree(location_ids: Collection[int]) -> Q:
    """Q(location_id__in=ids) | Q(location_id=None, monitoring_site__parent_id__in=ids). Used by fmm only;
    the overview keeps its own location-only rule."""
    ids = list(location_ids)
    return Q(location_id__in=ids) | Q(location_id=None, monitoring_site__parent_id__in=ids)


# ---------------------------------------------------------------------------------- HACT visit plan
# A finding row of a completed programmatic visit: the one rule the assurance page (per partner) and the
# HACT visit plan (per programme document) count by.
COMPLETED_PROGRAMMATIC = Q(is_programmatic_visit=True, status__iexact="completed")
# shown next to the count so that a zero is explained, never counted: a report awaiting approval, and a
# completed visit eTools does not mark programmatic
SUBMITTED_PROGRAMMATIC = Q(is_programmatic_visit=True, status__iexact="submitted")
COMPLETED_NOT_PROGRAMMATIC = Q(is_programmatic_visit=False, status__iexact="completed")


def completed_programmatic_rows(year: int) -> QuerySet:
    """Finding rows HACT counts as completed programmatic FM visits of `year`: is_programmatic_visit,
    status iexact "completed", end date in the year. The Assurance page (per partner) and the HACT visit
    plan (per PD) both count over these rows."""
    from neurodb.datamart.models import MonitoringFinding

    return MonitoringFinding.objects.filter(COMPLETED_PROGRAMMATIC, end_date__year=year)


def visits_by_pd_for_hact(year: int, pd_ids: Collection[int] | None = None) -> dict[int, dict[str, int]]:
    """{pd_id: {"completed": n, "submitted": n, "not_programmatic": n}}, distinct visits per programme
    document over the rows that name it (``intervention``) and end in `year`, in one query:

    * ``completed``: the rows of :func:`completed_programmatic_rows`, the visits HACT counts;
    * ``submitted``: programmatic, report submitted and awaiting approval (not counted);
    * ``not_programmatic``: completed but not marked programmatic in eTools (not counted).

    A visit with two rows about the same PD counts once; a visit about two PDs counts for each.
    ``pd_ids`` keeps those PDs only. PDs with none of the three are left out."""
    from neurodb.datamart.models import MonitoringFinding

    rows = MonitoringFinding.objects.filter(end_date__year=year).exclude(intervention_id=None)
    if pd_ids is not None:
        rows = rows.filter(intervention_id__in=list(pd_ids))
    counted = (
        rows.order_by()
        .annotate(vk=visit_key_expr())
        .values("intervention_id")
        .annotate(
            completed=Count("vk", distinct=True, filter=COMPLETED_PROGRAMMATIC),
            submitted=Count("vk", distinct=True, filter=SUBMITTED_PROGRAMMATIC),
            not_programmatic=Count("vk", distinct=True, filter=COMPLETED_NOT_PROGRAMMATIC),
        )
    )
    return {
        r["intervention_id"]: {k: r[k] for k in ("completed", "submitted", "not_programmatic")}
        for r in counted
        if r["completed"] or r["submitted"] or r["not_programmatic"]
    }


def partner_visits_naming_no_pd(year: int, partner_ids: Collection[int] | None = None) -> dict[int, int]:
    """{partner_id: n}: completed programmatic FM visits of `year` (the rows of
    :func:`completed_programmatic_rows`) none of whose finding rows names a programme document, only the
    partner or a CP output. The Assurance page counts them for the partner; the HACT visit plan counts
    them for no PD and shows them beside the partner's PDs, "not counted". One query (the visits that
    name a PD are a subquery); a visit with several partners counts for each. ``partner_ids`` keeps
    those partners only."""
    naming = (
        completed_programmatic_rows(year)
        .exclude(intervention_id=None)
        .order_by()
        .annotate(vk=visit_key_expr())
        .values("vk")
    )
    rows = completed_programmatic_rows(year).exclude(partner_id=None)
    if partner_ids is not None:
        rows = rows.filter(partner_id__in=list(partner_ids))
    counted = (
        rows.order_by()
        .annotate(vk=visit_key_expr())
        .exclude(vk__in=naming)
        .values("partner_id")
        .annotate(n=Count("vk", distinct=True))
    )
    return {r["partner_id"]: r["n"] for r in counted}


def search_reference(number: Any) -> str:
    """The part of a programme document's reference that every spelling of it holds, for a text search
    of its findings and TPM activities: the PCA/PD pair ('PCA2026001/PD2026001' of
    'TEST/PCA2026001/PD2026001-2', the pair :class:`PDResolver` links findings by), else the number
    without its country prefix and amendment suffix ('SSFA2024001'); '' for a blank reference."""
    token = pd_token(number)
    if token:
        return "/".join(token)
    return _base(number)
