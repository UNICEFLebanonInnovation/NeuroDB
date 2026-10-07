"""Building the visits: the eTools field monitoring finding rows grouped into visits and linked to the
rest of NeuroDB (step 5 of the refresh, with the reading of the answer, option and programme activity
records it needs).

A visit is the finding rows that share a ``datamart.fm.visit_key``. For each visit:

- **partners**: the rows' partners (linked by vendor number at the sync); the most frequent one first;
- **programme documents**: each row's own link (``datamart.fm.PDResolver`` at the sync and at the
  refresh's first step), else the PD reference its record or its programme activities give;
- **CP outputs and programme activities**: the rows about an output, their records, and the visit's
  programme activity records;
- **place**: the most frequent location (a row whose location is not linked is matched to the
  gazetteer by its P-code) and monitoring site; the governorate and district from the gazetteer
  (starting at the site's location when the visit has none); the point of the site, else the
  coordinates eTools wrote on the rows (``location_lat``/``location_lon``), else the location's own
  point, else its nearest ancestor's (approximate). Only a site's point, coordinates at the lowest
  admin level or away from the gazetteer's centre, or a location's own point at the gazetteer's lowest
  level is precise (``fmm.place``);
- **modality and programme areas**: the most frequent monitoring modality of the rows ("UNICEF Staff",
  "TPM - iAPS"...), and every programme area they name;
- **sections**: written on the visit (its records or its answers), else its programme documents',
  else its action points', else, with no programme document, those of the partner's programme
  documents running on the visit date ("inferred from the partner"); NeuroDB sections through the
  confirmed section map only;
- **field offices**: written on the visit, else its programme documents', else its action points';
- **team**: the members' display names and the visit lead's, never an e-mail address
  (``privacy.person_display``);
- **action points**: eTools action points raised from field monitoring, matched by the activity id,
  else the activity reference, else its reference number;
- **checklist answers**: joined by the activity id, else by the activity reference; each applies to
  one entity row (same entity text or programme document), to every row of a partner (the answer names
  the partner), or to the visit as a whole. Only whether an answer was given, its code (a rating, yes
  or no) and word counts are kept, never its text;
- **HACT answers of a row** (``hact_q1_answer``, ``hact_q2_answer``, ``hact_q3_answer`` in eTools' FMM
  export): measured like a checklist answer, never kept as text (``VisitEntity.row_answers``); the
  scoring prefers them to the checklist answers of the same question for that row.

Several field offices, sections or programme areas written as one text ("Zahle; Tripoli") are split at
their semicolons.

Data problems are recorded on the visit (``Visit.issues``) as counts and codes, never as free text.
Every string written from eTools data is cut to its column (:func:`fit`, :func:`fit_list`), so an
over-long value of an unknown key can never fail the refresh.

Records are read one at a time (``iterator``), and only small parsed values are kept: a record's
``data`` is dropped as soon as it is read. Quality scores, HACT Q1, the PSEA flag and urgency are
worked out afterwards, by the scoring step.
"""

from __future__ import annotations

import datetime
import hashlib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from django.db import models
from django.utils import timezone

from neurodb.datamart import catalogue, fm
from neurodb.integrations.etools.fields import coerce
from neurodb.watch import people

from . import fields, parse, place, privacy
from .models import QuestionAnswer, Visit, VisitActionPoint, VisitEntity

NARRATIVE_MIN_WORDS = 25  # a narrative this long gets a hash (copies: quality rule R4's default minimum)
ROW_ROLES = ("q1", "q2", "q3")  # the HACT answers a finding row may carry (fields q1_answer...)
SHORT_WORDS = 12  # a row answer this short keeps the hash of its folded text (R5's placeholder list)
MAX_ITEMS = 50  # elements kept in a list column (sections, team...)
RAW_CHARS = 40  # characters of a raw rating or status kept in Visit.issues
FINDING_FIELDS = (
    "pk",
    "datamart_id",
    "monitoring_activity_id",
    "monitoring_activity",
    "reference_number",
    "entity",
    "entity_type",
    "status",
    "overall_finding_rating",
    "narrative_finding",
    "start_date",
    "end_date",
    "last_modify_date",
    "location_id",
    "location_name",
    "location_pcode",
    "monitoring_site_id",
    "partner_id",
    "intervention_id",
    "pd_match",
    "is_programmatic_visit",
    "is_remote_monitoring",
    "visit_lead",
    "data",
)
RATED = ("on_track", "constrained", "off_track")
LIKERT_SCALES = (3, 5)  # the answer scales whose bottom tier is a red flag (FMS: <= 2 of 5, 1 of 3)


# ------------------------------------------------------------------------------------------ helpers
def fit(model: type[models.Model], field_name: str, value: Any) -> Any:
    """``value`` as ``model.field_name`` stores it: text stripped and cut to the column's length,
    None as "" for a text column (``integrations.etools.fields.coerce``)."""
    return coerce(model._meta.get_field(field_name), value)


def fit_list(
    model: type[models.Model], field_name: str, values: Iterable[Any], max_items: int = MAX_ITEMS
) -> list:
    """``values`` as the array column ``model.field_name`` stores them: each element cut to the
    column's length, blanks left out, at most ``max_items``."""
    base = model._meta.get_field(field_name).base_field
    out: list = []
    for value in values:
        if len(out) >= max_items:
            break
        element = coerce(base, value)
        if element not in (None, ""):
            out.append(element)
    return out


def _unique(values: Iterable[Any]) -> list:
    """Each value once, blanks left out, in the order first seen (case and spaces ignored for texts)."""
    seen: set = set()
    out = []
    for value in values:
        if value in (None, ""):
            continue
        marker = " ".join(value.split()).casefold() if isinstance(value, str) else value
        if marker not in seen:
            seen.add(marker)
            out.append(" ".join(value.split()) if isinstance(value, str) else value)
    return out


def _most_frequent(values: Iterable[Any]) -> Any:
    """The value given most often (the first given among equals); None when there is none."""
    counts = Counter(v for v in values if v not in (None, ""))
    return counts.most_common(1)[0][0] if counts else None


def _name(text: Any) -> str:
    """A name as names are compared: lower case, no accents, single spaces."""
    return " ".join(people.fold(str(text or "")).split())


def search_text(*parts: Any) -> str:
    """The folded text a visit is searched by."""
    return " ".join(_unique(_name(p) for p in parts if p))


def visit_rating(codes: Iterable[str]) -> str:
    """The rating of a visit: its worst rated entity (off track, then constrained, then on track);
    ``not_monitored`` when no entity is rated."""
    rated = [code for code in codes if code in RATED]
    return max(rated, key=fm.RATING_ORDER.get) if rated else "not_monitored"


def row_answer(raw: Any) -> dict[str, Any]:
    """What NeuroDB keeps of a HACT answer written on a finding row: whether it was given (a placeholder
    such as "n/a" is not), its rating or yes/no code, its words and, for a short answer, the sha1 of its
    folded text (so that R5's placeholder list can be checked without the text). Never the text."""
    parsed = parse.read_answer(raw)
    text = parse.as_kind(raw, "text") or ""
    short = ""
    if text and parsed.answer_words <= SHORT_WORDS:
        short = hashlib.sha1(parse.fold(text).encode(), usedforsecurity=False).hexdigest()
    return {
        "answered": parsed.answered,
        "placeholder": parsed.placeholder,
        "rating": parsed.rating,
        "code": parsed.answer_code,
        "words": min(parsed.answer_words, 1_000_000),
        "short": short,
    }


def coordinates(latitude: Any, longitude: Any) -> tuple[float, float] | None:
    """A point written on a row, when both values are numbers on the globe (0, 0 is no point)."""
    lat, lon = parse.as_kind(latitude, "number"), parse.as_kind(longitude, "number")
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat, lon) == (0, 0):
        return None
    return lat, lon


def narrative_facts(text: Any, min_words: int = NARRATIVE_MIN_WORDS) -> tuple[int, str, bool]:
    """(words, hash, placeholder) of a narrative: the hash (sha1 of the folded text) only for one of
    ``min_words`` words or more, so that copies can be found without keeping the text."""
    words = parse.word_count(text)
    placeholder = bool(str(text or "").strip()) and parse.is_placeholder(text)
    digest = ""
    if words >= min_words and not placeholder:
        digest = hashlib.sha1(parse.fold(text).encode(), usedforsecurity=False).hexdigest()
    return words, digest, placeholder


@dataclass(frozen=True)
class ActionPointFacts:
    """What the counts of a visit's action points need, and where the action point says it belongs."""

    id: int
    status: str
    due_date: datetime.date | None
    high_priority: bool
    section: str = ""
    office: str = ""
    assigned: bool = False  # assigned to someone (who is never kept)


def ap_counts(links: Iterable[ActionPointFacts], today: datetime.date) -> dict[str, int]:
    """The action points of a visit: all, open (``ActionPoint.OPEN_STATUSES``), overdue (open and due
    before ``today``) and open with high priority."""
    from neurodb.datamart.models import ActionPoint

    links = list(links)
    open_ = [a for a in links if a.status in ActionPoint.OPEN_STATUSES]
    return {
        "action_points": len(links),
        "action_points_open": len(open_),
        "action_points_overdue": sum(1 for a in open_ if a.due_date and a.due_date < today),
        "action_points_high_open": sum(1 for a in open_ if a.high_priority),
    }


# ------------------------------------------------------------------------------------------ the run
@dataclass
class Context:
    """What one build reads besides the records: the day, the keys chosen for each field (by the
    probe, ``(dataset, field) -> key``), and where a record that cannot be read is reported."""

    today: datetime.date
    keys: Mapping[tuple[str, str], str | None]
    on_error: Callable[[str, BaseException], None] = lambda label, exc: None
    now: datetime.datetime = field(default_factory=timezone.now)

    def key(self, dataset: str, name: str) -> str | None:
        return self.keys.get((dataset, name)) or None


@dataclass
class _Row:
    """One finding row, reduced to what the visit needs (no narrative, no record)."""

    pk: int
    datamart_id: int
    activity_id: int | None
    reference: str
    reference_number: str
    entity: str
    entity_type: str
    status_raw: str
    rating_raw: str
    start_date: datetime.date | None
    end_date: datetime.date | None
    last_modified: datetime.datetime | None
    location_id: int | None
    location_name: str
    location_pcode: str
    site_id: int | None
    partner_id: int | None
    pd_id: int | None
    pd_match: str
    programmatic: bool
    remote: bool
    words: int
    narrative_hash: str
    placeholder: bool
    sections: list[str]
    offices: list[str]
    pd_reference: str
    cp_output: str
    team: list[str]
    team_unnamed: int
    modality: str = ""
    programme_areas: list[str] = field(default_factory=list)
    point: tuple[float, float] | None = None  # location_lat / location_lon
    location_type: str = ""
    row_answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    attachments: int | None = None  # attachments_count (rule R28); None: not in the record


class _Answer:
    """One checklist answer record, parsed (small: no answer text)."""

    __slots__ = (
        "document_id",
        "visit_key",
        "entity_text",
        "question_key",
        "question_text",
        "question_order",
        "is_hact",
        "parsed",
        "method",
        "entity",
        "partner_id",
        "applies_to",
        "role",
        "category",
        "likert",
        "scale",
    )

    def __init__(
        self,
        document_id,
        visit_key,
        entity_text,
        question_key,
        question_text,
        order,
        is_hact,
        parsed,
        method,
        category="",
        likert=None,
        scale=None,
    ):
        self.document_id = document_id
        self.visit_key = visit_key
        self.entity_text = entity_text
        self.question_key = question_key
        self.question_text = question_text
        self.question_order = order
        self.is_hact = is_hact
        self.parsed = parsed
        self.method = method
        self.entity: VisitEntity | None = None
        self.partner_id: int | None = None
        self.applies_to = "visit"
        self.role = ""  # given by the scoring step (score.score_visits)
        self.category = category
        self.likert, self.scale = likert, scale  # a Likert answer's value (1 = the bottom tier) and scale

    def as_model(self, visit: Visit | None) -> QuestionAnswer:
        parsed = self.parsed
        return QuestionAnswer(
            document_id=self.document_id,
            visit=visit,
            visit_key=self.visit_key if visit is not None else "",
            entity=self.entity,
            partner_id=self.partner_id,
            question_key=self.question_key,
            question_text=self.question_text,
            question_order=self.question_order,
            is_hact=self.is_hact,
            role=self.role,
            applies_to=self.applies_to if visit is not None else "",
            answer_code=parsed.answer_code,
            answered=parsed.answered,
            placeholder=parsed.placeholder,
            answer_words=min(parsed.answer_words, 2_000_000_000),
            summary_words=min(parsed.summary_words, 2_000_000_000),
            rating=parsed.rating,
            method=self.method,
            category=self.category,
            likert=self.likert,
            scale=self.scale,
        )


@dataclass
class _Extra:
    """What the answer and programme activity records add to one visit."""

    sections: list[str] = field(default_factory=list)
    offices: list[str] = field(default_factory=list)
    cp_outputs: list[str] = field(default_factory=list)
    programme_activities: list[str] = field(default_factory=list)
    pd_references: list[str] = field(default_factory=list)


@dataclass
class BuildResult:
    """The visits built, ready to be written (``refresh``), and what the run details say about them."""

    visits: list[Visit] = field(default_factory=list)
    entities: list[VisitEntity] = field(default_factory=list)
    answers: list[_Answer] = field(default_factory=list)
    links: list[VisitActionPoint] = field(default_factory=list)
    # the action points of each visit (by key), for urgency
    action_point_facts: dict[str, list[ActionPointFacts]] = field(default_factory=dict)
    findings: int = 0  # finding rows read
    questions: int = 0  # answer records read
    failed: int = 0  # rows, records or visits skipped by an error
    details: dict[str, Any] = field(default_factory=dict)

    def question_answers(self, visits_by_key: Mapping[str, Visit]) -> Iterable[QuestionAnswer]:
        for answer in self.answers:
            yield answer.as_model(visits_by_key.get(answer.visit_key))


def build_visits(ctx: Context) -> BuildResult:
    """Read the findings and the answer, option and programme activity records, and build every visit
    with its entity rows, answers and action point links (nothing is written here)."""
    return _Builder(ctx).build()


class _Builder:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.result = BuildResult()
        self.rows: dict[str, list[_Row]] = defaultdict(list)
        self.extra: dict[str, _Extra] = defaultdict(_Extra)
        self.by_reference: dict[str, set[str]] = defaultdict(set)  # folded reference -> visit keys
        self.answers_by_key: dict[str, list[_Answer]] = defaultdict(list)
        # one object per distinct value (question texts, keys, entity texts, parsed answers): the
        # answers of 100,000 records repeat a few hundred of them
        self.interned: dict[Any, Any] = {}
        self.counts: dict[str, Any] = {
            "location": Counter(),
            "governorate": Counter(),
            "sections_from": Counter(),
            "offices_from": Counter(),
            "action_points": Counter(),
            "questions": Counter(),
        }

    def failed(self, label: str, exc: BaseException) -> None:
        self.result.failed += 1
        self.ctx.on_error(label, exc)

    # ------------------------------------------------------------------ the run
    def build(self) -> BuildResult:
        self._read_findings()
        self._read_programme_activities()
        options = self._read_options()
        self._read_questions(options)
        self._load()
        self._build_all()
        self._match_action_points()
        self._finish_details()
        return self.result

    # ------------------------------------------------------------------ reading the findings
    def _read_findings(self) -> None:
        from neurodb.datamart.models import MonitoringFinding

        rows = MonitoringFinding.objects.order_by("pk").values_list(*FINDING_FIELDS)
        for values in rows.iterator(chunk_size=2000):
            record = dict(zip(FINDING_FIELDS, values, strict=True))
            self.result.findings += 1
            try:
                row = self._row(record)
            except Exception as exc:
                self.failed(f"field_monitoring {record['pk']}", exc)
                continue
            key = fm.visit_key(row.activity_id, row.reference, row.pk)
            self.rows[key].append(row)
            for reference in (row.reference, row.reference_number):
                if fm.norm_reference(reference):
                    self.by_reference[fm.norm_reference(reference)].add(key)

    def _row(self, r: dict[str, Any]) -> _Row:
        raw = r["data"] if isinstance(r["data"], dict) else {}
        data = catalogue.scrub(raw)  # contact keys out, as everywhere else
        ctx = self.ctx

        def read(name: str) -> Any:
            key = ctx.key("field_monitoring", name)
            return parse.walk(data, key) if key else None

        team_key = ctx.key("field_monitoring", "team")
        # the team is read from the record as received: its members' e-mail addresses are counted as
        # members known by e-mail only (``team_unnamed``), never kept
        team, unnamed = privacy.person_display(parse.walk(raw, team_key) if team_key else None)
        lead, lead_unnamed = privacy.person_display(r["visit_lead"] or None)
        words, digest, placeholder = narrative_facts(r["narrative_finding"])
        status_raw = (r["status"] or "").strip() or (parse.as_kind(read("status"), "text") or "")
        answers = {
            role: row_answer(read(f"{role}_answer"))
            for role in ROW_ROLES
            if ctx.key("field_monitoring", f"{role}_answer")
        }
        return _Row(
            pk=r["pk"],
            datamart_id=r["datamart_id"],
            activity_id=r["monitoring_activity_id"],
            reference=r["monitoring_activity"] or "",
            reference_number=r["reference_number"] or "",
            entity=r["entity"] or "",
            entity_type=r["entity_type"] or "",
            status_raw=status_raw,
            rating_raw=r["overall_finding_rating"] or "",
            start_date=r["start_date"],
            end_date=r["end_date"],
            last_modified=r["last_modify_date"],
            location_id=r["location_id"],
            location_name=r["location_name"] or parse.as_kind(read("location_name"), "text") or "",
            location_pcode=r["location_pcode"] or parse.as_kind(read("location_pcode"), "text") or "",
            site_id=r["monitoring_site_id"],
            partner_id=r["partner_id"],
            pd_id=r["intervention_id"],
            pd_match=r["pd_match"] or "",
            programmatic=bool(r["is_programmatic_visit"]),
            remote=bool(r["is_remote_monitoring"]),
            words=words,
            narrative_hash=digest,
            placeholder=placeholder,
            sections=parse.split_list(read("sections"))[:MAX_ITEMS],
            offices=parse.split_list(read("offices"))[:MAX_ITEMS],
            pd_reference=parse.as_kind(read("pd_reference"), "text") or "",
            cp_output=parse.as_kind(read("cp_output"), "text") or "",
            team=_unique(lead + team),
            team_unnamed=lead_unnamed + unnamed,
            modality=parse.as_kind(read("modality"), "text") or "",
            programme_areas=parse.split_list(read("programme_areas"))[:MAX_ITEMS],
            point=coordinates(read("latitude"), read("longitude")),
            location_type=parse.as_kind(read("location_type"), "text") or "",
            row_answers=answers,
            attachments=parse.as_kind(read("attachments"), "count")
            if ctx.key("field_monitoring", "attachments")
            else None,
        )

    # ------------------------------------------------------------------ joining records to visits
    def _visit_of(self, activity_id: Any, reference: Any) -> str:
        """The visit a record belongs to: by its activity id, else by its activity reference (one that
        several visits share is left out); "" when none."""
        number = parse.as_kind(activity_id, "id")
        if number is not None and str(number) in self.rows:
            return str(number)
        keys = self.by_reference.get(fm.norm_reference(parse.as_kind(reference, "text")))
        if keys and len(keys) == 1:
            return next(iter(keys))
        return ""

    def _read_programme_activities(self) -> None:
        ctx = self.ctx
        keys = {
            name: ctx.key("fm_programme_activities", name)
            for name in fields.CANDIDATES["fm_programme_activities"]
        }
        if not (keys["activity_id"] or keys["activity_ref"]):
            return
        for pk, record in fields.records("fm_programme_activities"):
            try:
                read = {
                    name: (parse.walk(record, key) if key and isinstance(record, dict) else None)
                    for name, key in keys.items()
                }
                visit_key = self._visit_of(read["activity_id"], read["activity_ref"])
                if not visit_key:
                    continue
                extra = self.extra[visit_key]
                _add(extra.programme_activities, parse.text_list(read["programme_activity"]))
                _add(extra.cp_outputs, parse.text_list(read["cp_output"]))
                _add(extra.pd_references, parse.text_list(read["pd_reference"]))
                _add(extra.sections, parse.split_list(read["section"]))
            except Exception as exc:
                self.failed(f"fm_programme_activities {pk}", exc)

    def _read_options(self) -> dict[tuple[str, str], str]:
        ctx = self.ctx
        question, code, label = (ctx.key("fm_options", n) for n in ("question_id", "value", "label"))
        options: dict[tuple[str, str], str] = {}
        if not (question and code and label):
            return options
        for pk, record in fields.records("fm_options"):
            try:
                parse.options_entry(record, question, code, label, options)
            except Exception as exc:
                self.failed(f"fm_options {pk}", exc)
        return options

    def _read_questions(self, options: Mapping[tuple[str, str], str]) -> None:
        ctx = self.ctx
        keys = {name: ctx.key("fm_questions", name) for name in fields.CANDIDATES["fm_questions"]}
        if not (keys["question_id"] or keys["question_text"]):
            return
        counts = self.counts["questions"]
        self.scales = Counter(question for question, _value in options)  # options per question
        for pk, record in fields.records("fm_questions"):
            self.result.questions += 1
            try:
                answer = self._answer(pk, record, keys, options)
            except Exception as exc:
                self.failed(f"fm_questions {pk}", exc)
                continue
            if answer is None:
                counts["no_question"] += 1
                continue
            self.result.answers.append(answer)  # counted as linked once its visit is built
            if answer.visit_key:
                self.answers_by_key[answer.visit_key].append(answer)

    def _answer(self, pk: int, record: Any, keys: Mapping[str, str | None], options) -> _Answer | None:
        if not isinstance(record, dict):
            return None

        def read(name: str, kind: str = "text") -> Any:
            key = keys.get(name)
            return parse.value(record, key, kind) if key else None

        def raw(name: str) -> Any:
            key = keys.get(name)
            return parse.walk(record, key) if key else None

        text = read("question_text") or ""
        question_key = parse.question_key(read("question_id", "id"), text)
        if not question_key:
            return None
        visit_key = self._visit_of(read("activity_id", "id"), read("activity_ref"))
        parsed = parse.read_answer(
            raw("answer"), raw("answer_label"), raw("summary"), options=options, question_key=question_key
        )
        same = self.interned.setdefault
        is_hact = read("is_hact", "bool")
        if visit_key:
            extra = self.extra[visit_key]
            _add(extra.sections, parse.split_list(raw("sections")))
            _add(extra.offices, parse.split_list(raw("offices")))
        order = read("order", "int")
        scale = self.scales.get(question_key) if hasattr(self, "scales") else None
        likert = None
        if scale in LIKERT_SCALES and parsed.answered and not parsed.answer_code:
            value = parse.as_kind(raw("answer"), "int")
            likert = value if value is not None and 1 <= value <= scale else None
        category = read("category") or ""
        return _Answer(
            document_id=pk,
            visit_key=same(visit_key, visit_key),
            entity_text=same(entity := _name(read("entity")), entity),
            question_key=same(key := fit(QuestionAnswer, "question_key", question_key), key),
            question_text=same(text := fit(QuestionAnswer, "question_text", text), text),
            order=order if order is not None and abs(order) < 2**31 else None,
            is_hact=is_hact,
            parsed=same(parsed, parsed),
            method=same(method := fit(QuestionAnswer, "method", read("method")), method),
            category=same(category := fit(QuestionAnswer, "category", category), category),
            likert=likert,
            scale=scale if likert is not None else None,
        )

    # ------------------------------------------------------------------ what the visits link to
    def _load(self) -> None:
        """The programme documents, partners, places, sites and section map, read once."""
        from neurodb.datamart.models import MonitoringSite
        from neurodb.datamart.monitoring import _gazetteer
        from neurodb.partnerships.models import PCA, PartnerOrganization
        from neurodb.watch import sections

        self.pcas: dict[int, dict[str, Any]] = {
            row["pk"]: row
            for row in PCA.objects.values(
                "pk",
                "number",
                "title",
                "partner_id",
                "start",
                "end",
                "section_names",
                "offices_set",
                "offices",
                "offices_names",
            )
        }
        self.resolver = fm.PDResolver(
            (p["pk"], p["number"], p["title"], p["partner_id"], p["start"], p["end"])
            for p in self.pcas.values()
        )
        self.pcas_by_partner: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for pca in self.pcas.values():
            if pca["partner_id"]:
                self.pcas_by_partner[pca["partner_id"]].append(pca)
        partner_ids = {row.partner_id for rows in self.rows.values() for row in rows if row.partner_id}
        self.partners = {
            p["pk"]: p
            for p in PartnerOrganization.objects.filter(pk__in=partner_ids).values(
                "pk", "name", "short_name", "vendor_number"
            )
        }
        self._locations_by_pcode()
        site_ids = {row.site_id for rows in self.rows.values() for row in rows if row.site_id}
        self.sites = {
            s["pk"]: s
            for s in MonitoringSite.objects.filter(pk__in=site_ids).values(
                "pk", "name", "p_code", "latitude", "longitude", "parent_id"
            )
        }
        location_ids = {row.location_id for rows in self.rows.values() for row in rows if row.location_id}
        location_ids |= {s["parent_id"] for s in self.sites.values() if s["parent_id"]}
        self.gazetteer = _gazetteer(location_ids)
        self.lowest = place.lowest_admin_level()
        self.lowest_types = place.lowest_type_names(self.lowest)
        self.section_map = sections.confirmed_map()
        self.action_points = self._read_action_points()

    def _locations_by_pcode(self) -> None:
        """Link to the gazetteer, by their P-code, the rows whose location eTools did not link (the
        FMM export writes ``location_pcode`` beside the location)."""
        from neurodb.geo.models import Location

        wanted = {
            row.location_pcode.strip()
            for rows in self.rows.values()
            for row in rows
            if row.location_id is None and row.location_pcode.strip()
        }
        if not wanted:
            return
        found = dict(
            Location.objects.filter(p_code__in=wanted, is_active=True)
            .order_by("p_code", "pk")
            .values_list("p_code", "pk")
        )
        for rows in self.rows.values():
            for row in rows:
                if row.location_id is None and row.location_pcode.strip() in found:
                    row.location_id = found[row.location_pcode.strip()]
                    self.counts["location"]["by_pcode"] += 1

    def _read_action_points(self) -> dict[int, tuple[ActionPointFacts, Any, str]]:
        from neurodb.datamart.models import ActionPoint

        rows = ActionPoint.objects.filter(related_module__iexact="fm").values_list(
            "pk",
            "related_module_id",
            "module_reference_number",
            "status",
            "due_date",
            "high_priority",
            "section",
            "office",
            "assigned_to_name",
        )
        return {
            pk: (
                ActionPointFacts(
                    pk,
                    status or "",
                    due,
                    bool(high),
                    section or "",
                    office or "",
                    bool((assigned or "").strip()),
                ),
                related,
                reference or "",
            )
            for pk, related, reference, status, due, high, section, office, assigned in rows
        }

    def _match_action_points(self) -> None:
        """Each FM action point to one visit: by the activity id, else the visit's reference, else its
        reference number (a reference two visits share matches neither)."""
        by_id = {str(v.activity_id): v for v in self.result.visits if v.activity_id}
        by_reference: dict[str, list[Visit]] = defaultdict(list)
        by_number: dict[str, list[Visit]] = defaultdict(list)
        for visit in self.result.visits:
            if fm.norm_reference(visit.reference):
                by_reference[fm.norm_reference(visit.reference)].append(visit)
            if fm.norm_reference(visit.reference_number):
                by_number[fm.norm_reference(visit.reference_number)].append(visit)
        counts = self.counts["action_points"]
        counts["fm_total"] = len(self.action_points)
        for pk, (facts, related, reference) in sorted(self.action_points.items()):
            folded = fm.norm_reference(reference)
            if related and str(related) in by_id:
                visit, how = by_id[str(related)], "related_id"
            elif folded and len(by_reference.get(folded, ())) == 1:
                visit, how = by_reference[folded][0], "reference"
            elif folded and len(by_number.get(folded, ())) == 1:
                visit, how = by_number[folded][0], "reference_number"
            else:
                counts["unlinked"] += 1
                continue
            counts[how] += 1
            self.result.links.append(VisitActionPoint(visit=visit, action_point_id=pk, matched_by=how))
            self.links_by_key[visit.key].append(facts)
        today = self.ctx.today
        self.result.action_point_facts = dict(self.links_by_key)
        for visit in self.result.visits:
            for name, n in ap_counts(self.links_by_key.get(visit.key, ()), today).items():
                setattr(visit, name, min(n, 32767))
            visit.action_points_assigned = min(
                sum(1 for a in self.links_by_key.get(visit.key, ()) if a.assigned), 32767
            )
        self._sections_and_offices_from_action_points()

    # ------------------------------------------------------------------ building each visit
    def _build_all(self) -> None:
        self.links_by_key: dict[str, list[ActionPointFacts]] = defaultdict(list)
        for key in sorted(self.rows, key=_key_order):
            try:
                visit, entities = self._visit(key, self.rows[key])
            except Exception as exc:
                self.failed(f"visit {key}", exc)
                # its answers are kept as joining no visit: none may point at an entity row of a visit
                # that is not written, or the whole swap would fail on it
                for answer in self.answers_by_key.get(key, ()):
                    answer.entity, answer.partner_id, answer.applies_to = None, None, "visit"
                continue
            self.result.visits.append(visit)
            self.result.entities += entities
        self.rows.clear()

    def _visit(self, key: str, rows: list[_Row]) -> tuple[Visit, list[VisitEntity]]:
        rows = sorted(rows, key=lambda r: r.datamart_id)
        extra = self.extra.get(key) or _Extra()
        visit = Visit(key=key, refreshed_at=self.ctx.now)
        issues: dict[str, Any] = {}
        first = rows[0]
        visit.activity_id = first.activity_id if key.isdecimal() else None
        visit.reference = fit(Visit, "reference", _most_frequent(r.reference.strip() for r in rows) or "")
        visit.reference_number = fit(
            Visit, "reference_number", _most_frequent(r.reference_number.strip() for r in rows) or ""
        )
        if visit.activity_id:
            label = f"Visit {visit.activity_id}"
        elif key.startswith("r-"):
            label = visit.reference or key
        else:
            label = f"Finding {first.datamart_id}"
        visit.label = fit(Visit, "label", label)
        visit.start_date = min((r.start_date for r in rows if r.start_date), default=None)
        visit.end_date = max((r.end_date for r in rows if r.end_date), default=None)
        # the date every period reads: the start, else the end when eTools left the start blank
        visit.visit_date = visit.start_date or visit.end_date
        visit.last_modified = max((r.last_modified for r in rows if r.last_modified), default=None)
        if visit.visit_date is None:
            issues["no_date"] = True
        self._status(visit, rows, issues)
        entities = self._entities(visit, rows, issues)
        self._partners_and_pds(visit, rows, entities, extra, issues)
        self._place(visit, rows, issues)
        self._sections_and_offices(visit, rows, extra)
        team = _unique(name for r in rows for name in r.team)
        visit.team = fit_list(Visit, "team", team)
        visit.team_unnamed = min(max((r.team_unnamed for r in rows), default=0), 32767)
        visit.is_programmatic = any(r.programmatic for r in rows)
        visit.is_remote = any(r.remote for r in rows)
        visit.modality = fit(
            Visit, "modality", _most_frequent(" ".join(r.modality.split()) for r in rows) or ""
        )
        visit.programme_areas = fit_list(
            Visit, "programme_areas", _unique(a for r in rows for a in r.programme_areas)
        )
        shared = max(
            (len(self.by_reference.get(fm.norm_reference(r.reference), ())) for r in rows), default=0
        )
        if shared > 1:
            issues["reference_conflict"] = shared
        self._answers(visit, entities)
        self._derived(visit, rows)
        visit.issues = issues
        partners = [self.partners.get(pid) or {} for pid in visit.partner_ids]
        visit.search = search_text(
            visit.label,
            visit.key,
            visit.reference,
            visit.reference_number,
            *(p.get(n) for p in partners for n in ("name", "short_name", "vendor_number")),
            *visit.pd_numbers,
            visit.place_name,
            visit.district_name,
            visit.governorate_name,
        )
        return visit, entities

    def _status(self, visit: Visit, rows: list[_Row], issues: dict[str, Any]) -> None:
        """The most advanced status of the rows; disagreeing rows are recorded."""
        codes = [(fm.normalize_status(r.status_raw), r.status_raw) for r in rows]
        known = [(code, raw) for code, raw in codes if code]
        if known:
            code, raw = max(known, key=lambda pair: fm.STATUS_RANK[pair[0]])
        else:
            code, raw = "", _most_frequent(raw for _, raw in codes) or ""
        visit.status = fit(Visit, "status", code)
        visit.status_raw = fit(Visit, "status_raw", raw)
        visit.status_group = fm.status_group(code)
        written = [(c, raw) for c, raw in codes if raw]  # a row without a status disagrees with nothing
        if len({c or raw for c, raw in written}) > 1:
            issues["status_conflict"] = dict(Counter(raw[:RAW_CHARS] for _, raw in written))

    def _entities(self, visit: Visit, rows: list[_Row], issues: dict[str, Any]) -> list[VisitEntity]:
        entities = []
        unknown: Counter[str] = Counter()
        for row in rows:
            kind = fm.entity_kind(row.entity_type, row.entity)
            rating = fm.normalize_rating(row.rating_raw)
            if rating == "other":
                unknown[row.rating_raw[:RAW_CHARS]] += 1
            pd_id, pd_match = row.pd_id, row.pd_match if row.pd_id else ""
            if pd_id is None and row.pd_reference:  # the record names its programme document
                pd_id, pd_match = self.resolver.resolve_reference(row.pd_reference, row.end_date)
            cp_output = row.entity if kind == "cp_output" else row.cp_output
            entities.append(
                VisitEntity(
                    visit=visit,
                    finding_id=row.pk,
                    datamart_id=row.datamart_id,
                    entity=fit(VisitEntity, "entity", row.entity),
                    entity_type_raw=fit(VisitEntity, "entity_type_raw", row.entity_type),
                    kind=kind,
                    pd_id=pd_id,
                    pd_match=fit(VisitEntity, "pd_match", pd_match),
                    partner_id=row.partner_id,
                    cp_output=fit(VisitEntity, "cp_output", cp_output),
                    rating=rating,
                    rating_raw=fit(VisitEntity, "rating_raw", row.rating_raw),
                    narrative_words=row.words,
                    narrative_hash=row.narrative_hash,
                    narrative_placeholder=row.placeholder,
                    row_answers=row.row_answers,
                )
            )
        if unknown:
            issues["rating_unknown"] = dict(unknown)
        codes = [e.rating for e in entities]
        visit.rating = visit_rating(codes)
        visit.rating_counts = dict(Counter(codes))
        visit.entities = min(len(entities), 32767)
        visit.entities_rated = min(sum(1 for c in codes if c in RATED), 32767)
        visit.entity_kinds = [k for k in fm.KINDS if any(e.kind == k for e in entities)]
        return entities

    def _partners_and_pds(self, visit, rows, entities, extra: _Extra, issues) -> None:
        partners = [r.partner_id for r in rows if r.partner_id]
        visit.partner_id = _most_frequent(partners)
        visit.partner_ids = sorted(set(partners))
        if len(partners) < len(rows):
            issues["rows_without_partner"] = len(rows) - len(partners)
        pds = [e.pd_id for e in entities if e.pd_id]
        for reference in _unique(extra.pd_references):
            pd_id, _how = self.resolver.resolve_reference(reference, visit.end_date)
            if pd_id:
                pds.append(pd_id)
        pd_ids = _unique(pds)
        unresolved = sum(1 for e in entities if e.kind == "pd" and not e.pd_id)
        if unresolved:
            issues["pd_unresolved"] = unresolved
        visit.pd_id = pd_ids[0] if pd_ids else None
        visit.pd_ids = pd_ids[:MAX_ITEMS]
        visit.pd_numbers = fit_list(
            Visit, "pd_numbers", (self.pcas.get(pk, {}).get("number") for pk in pd_ids)
        )
        outputs = [e.entity for e in entities if e.kind == "cp_output"]
        outputs += [r.cp_output for r in rows] + extra.cp_outputs
        visit.cp_outputs = fit_list(Visit, "cp_outputs", _unique(outputs))
        visit.programme_activities = fit_list(
            Visit, "programme_activities", _unique(extra.programme_activities)
        )

    def _place(self, visit: Visit, rows: list[_Row], issues: dict[str, Any]) -> None:
        from neurodb.datamart.monitoring import _place
        from neurodb.reports.overview import governorate_key

        locations = [r.location_id for r in rows if r.location_id]
        if len(set(locations)) > 1:
            issues["location_conflict"] = len(set(locations))
        visit.location_id = _most_frequent(locations)
        visit.site_id = _most_frequent(r.site_id for r in rows)
        site = self.sites.get(visit.site_id) if visit.site_id else None
        node = self.gazetteer.get(visit.location_id) if visit.location_id else None
        start = visit.location_id or (site or {}).get("parent_id")
        placed = _place(start, self.gazetteer)
        if site:
            name, pcode = site["name"], site["p_code"]
        elif node:
            name, pcode = node["name"], node["p_code"]
        else:
            name = _most_frequent(r.location_name for r in rows) or ""
            pcode = _most_frequent(r.location_pcode for r in rows) or ""
        visit.place_name = fit(Visit, "place_name", name)
        visit.place_pcode = fit(Visit, "place_pcode", pcode)
        visit.governorate_id = placed["governorate_id"]
        visit.governorate_name = fit(Visit, "governorate_name", placed["governorate"])
        visit.governorate_key = fit(
            Visit, "governorate_key", governorate_key(placed["governorate"]) if placed["governorate"] else ""
        )
        visit.district_id = placed["district_id"]
        visit.district_name = fit(Visit, "district_name", placed["district"])
        if not visit.governorate_id:
            self.counts["governorate"]["unlinked"] += 1
        elif visit.location_id:
            self.counts["governorate"]["linked"] += 1
        else:
            self.counts["governorate"]["via_site_only"] += 1
        self._point(visit, site, start, rows)

    def _point(self, visit: Visit, site: dict | None, start: int | None, rows: list[_Row]) -> None:
        """The visit's point: its site's, else the coordinates written on its rows, else its location's
        own, else its nearest ancestor's."""
        located_by, level = "", None
        written = _most_frequent(r.point for r in rows)
        if site and site["latitude"] is not None and site["longitude"] is not None:
            visit.latitude, visit.longitude, located_by = site["latitude"], site["longitude"], "site"
        elif written is not None:
            visit.latitude, visit.longitude = written
            located_by = "location"
            node = self.gazetteer.get(start) if start else None
            level = node["type__admin_level"] if node else None
            kind = _most_frequent(r.location_type for r in rows if r.point == written) or ""
            visit.located_by, visit.located_level = located_by, level
            visit.point_precise = place.written_point_precise(
                written, kind, node, self.gazetteer, self.lowest, self.lowest_types
            )
            self.counts["location"]["location"] += 1
            self.counts["location"]["written"] += 1
            if visit.point_precise:
                self.counts["location"]["precise"] += 1
            return
        else:
            node, hops = self.gazetteer.get(start) if start else None, 0
            first = node
            while node is not None and hops < 8:
                if node["latitude"] is not None and node["longitude"] is not None:
                    visit.latitude, visit.longitude = node["latitude"], node["longitude"]
                    level = node["type__admin_level"]
                    if node is first:
                        located_by = "location"
                    else:
                        located_by = "ancestor"
                        visit.approximate = True
                        visit.approximate_from = fit(Visit, "approximate_from", node["name"])
                    break
                node, hops = self.gazetteer.get(node["parent_id"]) if node["parent_id"] else None, hops + 1
        visit.located_by = located_by
        visit.located_level = level
        visit.point_precise = place.precise(located_by, level, self.lowest)
        self.counts["location"][located_by or "none"] += 1
        if visit.point_precise:
            self.counts["location"]["precise"] += 1

    def _sections_and_offices(self, visit: Visit, rows: list[_Row], extra: _Extra) -> None:
        """Sections and offices written on the visit, else its programme documents'. The visits that
        have neither yet get their action points' (or, for sections, the partner's) afterwards."""
        written = _unique([s for r in rows for s in r.sections] + extra.sections)
        pds = [self.pcas[pk] for pk in visit.pd_ids if pk in self.pcas]
        if written:
            names, source = written, "activity"
        else:
            names, source = _unique(s for pca in pds for s in (pca["section_names"] or ())), "pd"
        visit.section_names, visit.sections_from = (
            (fit_list(Visit, "section_names", names), source) if names else ([], "")
        )
        offices = _unique([o for r in rows for o in r.offices] + extra.offices)
        source = "activity"
        if not offices:
            offices, source = _unique(o for pca in pds for o in _pd_offices(pca)), "pd"
        visit.offices, visit.offices_from = (
            (fit_list(Visit, "offices", offices), source) if offices else ([], "")
        )

    def _sections_and_offices_from_action_points(self) -> None:
        from neurodb.watch.sections import resolve_names

        for visit in self.result.visits:
            links = self.links_by_key.get(visit.key, ())
            if not visit.section_names:
                names = _unique(a.section for a in links)
                if names:
                    visit.section_names, visit.sections_from = (
                        fit_list(Visit, "section_names", names),
                        "action_point",
                    )
                elif not visit.pd_ids and visit.end_date:
                    names = _unique(
                        s
                        for pid in visit.partner_ids
                        for pca in self.pcas_by_partner.get(pid, ())
                        if _running(pca, visit.end_date)
                        for s in (pca["section_names"] or ())
                    )
                    if names:
                        visit.section_names, visit.sections_from = (
                            fit_list(Visit, "section_names", names),
                            "partner",
                        )
            if not visit.offices:
                offices = _unique(a.office for a in links)
                if offices:
                    visit.offices, visit.offices_from = fit_list(Visit, "offices", offices), "action_point"
            visit.section_ids, _unmapped = resolve_names(visit.section_names, self.section_map)
            self.counts["sections_from"][visit.sections_from or "none"] += 1
            self.counts["offices_from"][visit.offices_from or "none"] += 1

    # ------------------------------------------------------------------ the answers of a visit
    def _answers(self, visit: Visit, entities: list[VisitEntity]) -> None:
        """Which entity, partner or the whole visit each answer applies to, and how many questions were
        asked and answered (one per question and entity, partner or visit)."""
        answers = self.answers_by_key.get(visit.key) or []
        if not answers:
            visit.questions_asked = visit.questions_answered = None
            return
        by_text: dict[str, VisitEntity] = {}
        by_token: dict[tuple[str, str], VisitEntity] = {}
        partner_rows: dict[int, VisitEntity] = {}
        for entity in entities:
            by_text.setdefault(_name(entity.entity), entity)
            token = fm.pd_token(entity.entity)
            if token:
                by_token.setdefault(token, entity)
            if entity.kind == "partner" and entity.partner_id:
                partner_rows.setdefault(entity.partner_id, entity)
        partner_names: dict[str, int] = {}
        for pid in visit.partner_ids:
            partner = self.partners.get(pid) or {}
            for name in ("name", "short_name", "vendor_number"):
                if partner.get(name):
                    partner_names.setdefault(_name(partner[name]), pid)
        index = {id(e): i for i, e in enumerate(entities)}
        asked: dict[tuple[str, str], bool] = {}
        for answer in answers:
            text = answer.entity_text
            entity = by_text.get(text) if text else None
            if entity is None and text:
                token = fm.pd_token(text)
                entity = by_token.get(token) if token else None
            pid = partner_names.get(text) if text and entity is None else None
            if entity is None and pid is not None and pid in partner_rows:
                entity = partner_rows[pid]  # the partner's own row, under another spelling
            if entity is not None:
                answer.entity, answer.applies_to, unit = entity, "entity", f"e{index[id(entity)]}"
            elif pid is not None:
                answer.partner_id, answer.applies_to, unit = pid, "partner", f"p{pid}"
            else:
                answer.applies_to, unit = "visit", "v"
            pair = (answer.question_key, unit)
            asked[pair] = asked.get(pair, False) or answer.parsed.answered
        visit.questions_asked = min(len(asked), 32767)
        visit.questions_answered = min(sum(asked.values()), 32767)

    def _derived(self, visit: Visit, rows: list[_Row]) -> None:
        """FMS's derived columns of a visit (rules R1, R2, R10, R11, R14, R28, R31...): the share of the
        questions answered, the categories with an answer, the collection methods used, the red-flag
        Likert answers (2 or less of 5, 1 of 3) and the attachments. None when the data cannot tell."""
        from .rules import half_up

        answers = self.answers_by_key.get(visit.key) or []
        asked, answered_n = visit.questions_asked, visit.questions_answered
        visit.fmq_answered_pct = (
            half_up(100 * answered_n / asked, 1) if asked and answered_n is not None else None
        )
        answered = [a for a in answers if a.parsed.answered]
        category_key = self.ctx.key("fm_questions", "category")
        method_key = self.ctx.key("fm_questions", "method")
        if answers and category_key:
            categories = _unique(a.category for a in answered if a.category)
            visit.fmq_answered_categories = fit(
                Visit, "fmq_answered_categories", "; ".join(sorted(categories, key=str.casefold))
            )
        else:
            visit.fmq_answered_categories = None
        if answers and method_key:
            visit.method_count = min(len({_name(a.method) for a in answered if a.method}), 32767)
        else:
            visit.method_count = None
        likert = [a for a in answered if a.likert is not None]
        visit.red_flag_count = (
            min(sum(1 for a in likert if a.likert <= (2 if a.scale == 5 else 1)), 32767) if likert else None
        )
        found = [r.attachments for r in rows if r.attachments is not None]
        visit.attachments_count = min(sum(found), 2_000_000_000) if found else None

    # ------------------------------------------------------------------ the run details
    def _finish_details(self) -> None:
        visits = self.result.visits
        counts = self.counts
        questions = counts["questions"]
        # the answers that join a visit written (not one whose build failed), by what they apply to
        built = {v.key for v in visits}
        joined = Counter(a.applies_to for a in self.result.answers if a.visit_key in built)
        linked = sum(joined.values())
        self.result.details = {
            "visits": len(visits),
            "findings": self.result.findings,
            "without_date": sum(1 for v in visits if v.visit_date is None),
            "dated_by_end": sum(1 for v in visits if v.start_date is None and v.end_date is not None),
            "reference_conflicts": sum(1 for keys in self.by_reference.values() if len(keys) > 1),
            "rows_without_reference": sum(1 for v in visits if v.key.startswith("f-")),
            "location": {
                k: counts["location"].get(k, 0)
                for k in ("site", "location", "ancestor", "none", "precise", "written", "by_pcode")
            },
            "governorate": {
                k: counts["governorate"].get(k, 0) for k in ("linked", "via_site_only", "unlinked")
            },
            "sections_from": {
                k: counts["sections_from"].get(k, 0)
                for k in ("activity", "pd", "action_point", "partner", "none")
            },
            "offices_from": {
                k: counts["offices_from"].get(k, 0) for k in ("activity", "pd", "action_point", "none")
            },
            "action_points": {
                k: counts["action_points"].get(k, 0)
                for k in ("fm_total", "related_id", "reference", "reference_number", "unlinked")
            },
            "questions": {
                "parsed": self.result.questions,
                "linked": linked,
                "unlinked": len(self.result.answers) - linked,
                "no_question": questions.get("no_question", 0),
                "visits_with_questions": sum(1 for v in visits if v.questions_asked is not None),
                "applies_to": {k: joined.get(k, 0) for k in ("entity", "partner", "visit")},
            },
        }


def _add(values: list[str], new: Iterable[str]) -> None:
    """Add the texts not in ``values`` yet (every answer record of a visit repeats its sections), up
    to ``MAX_ITEMS``."""
    for text in new:
        if len(values) >= MAX_ITEMS:
            return
        if text not in values:
            values.append(text)


def _key_order(key: str) -> tuple[int, int, str]:
    """Visits by activity id, then by reference, then the rows on their own."""
    if key.isdecimal():
        return 0, int(key), ""
    return (1, 0, key) if key.startswith("r-") else (2, 0, key)


def _pd_offices(pca: Mapping[str, Any]) -> list[str]:
    """A programme document's field offices: its office set, else its offices, else its office names."""
    if pca.get("offices_set"):
        return list(pca["offices_set"])
    if pca.get("offices"):
        return list(pca["offices"])
    return [name.strip() for name in (pca.get("offices_names") or "").split(",") if name.strip()]


def _running(pca: Mapping[str, Any], day: datetime.date) -> bool:
    """A programme document running on ``day`` (an open start or end counts as running)."""
    return (pca["start"] is None or pca["start"] <= day) and (pca["end"] is None or day <= pca["end"])
