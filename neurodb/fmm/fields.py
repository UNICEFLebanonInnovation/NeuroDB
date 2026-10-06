"""Which key each logical field of the eTools field monitoring records is read from.

No sample of the field monitoring question, option or programme activity records existed when this
was written, so every logical field (the answer, the activity id, the question text...) has a list of
candidate keys (:data:`CANDIDATES`), most likely first. Each refresh reads every record of the six
datasets (:func:`probe`), counts each key (top level, and one level down as "parent.child") with its
value types and a few redacted examples (``KeyProbe``), and measures for every candidate the share of
records holding a usable value of the field's kind (its coverage). :func:`resolve` then chooses:

1. a key an administrator pinned (``override_key``) when the data shows it; one it does not show is
   flagged ("Set key not in the data") and the next steps choose instead;
2. otherwise the first candidate, in list order, whose coverage reaches ``FMM_KEY_MIN_COVERAGE``
   (0.5), flagged "ambiguous" when a later candidate fills at least 30 points more;
3. otherwise the candidate with the highest coverage above 0;
4. otherwise none: the field is "not found", and what needs it says "not available".

The choice is stored in ``FieldMapping`` and read back with :func:`key_for`. The admin shows all of it
as "Fields found". Values are read with :mod:`neurodb.fmm.parse`.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from neurodb.datamart import catalogue

from . import parse, privacy
from .models import FieldMapping, KeyProbe

CANDIDATES: dict[str, dict[str, tuple[str, ...]]] = {
    # extra keys in MonitoringFinding.data (after catalogue.scrub). eTools' FMM export names come first
    # (the FMS user manual, §13.2: hact_q1_answer, field_offices, sections_names, monitoring_modality...)
    "field_monitoring": {
        "sections": (
            "sections_names",  # "Education; WASH"
            "sections",
            "section",
            "section_names",
            "monitoring_activity_sections",
            "sections.name",
        ),
        "offices": (
            "field_offices",  # "Zahle; Tripoli"
            "field_office",
            "offices",
            "office",
            "office_name",
            "monitoring_activity_offices",
            "field_office.name",
        ),
        "team": (  # person
            "team_members",
            "team",
            "team_members_names",
            "members",
            "monitors",
            "visit_team",
            "person_responsible",
        ),
        "pd_reference": (
            "intervention_number",
            "pd_reference_number",
            "pd_number",
            "intervention",
            "intervention.number",
        ),
        "cp_output": ("cp_output", "cp_output_name", "output", "result", "cp_output.name"),
        "status": ("status", "monitoring_activity_status", "activity_status"),
        # the HACT answers written on the finding row: they take precedence over the checklist answers
        # (fm-questions) of the same question for that row
        "q1_answer": ("hact_q1_answer", "q1_answer", "hact_q1"),
        "q2_answer": ("hact_q2_answer", "q2_answer", "hact_q2"),
        "q3_answer": ("hact_q3_answer", "q3_answer", "hact_q3"),
        "modality": ("monitoring_modality", "modality", "monitoring_type"),
        "programme_areas": ("programme_areas", "programme_area"),
        "location_name": ("location_name", "location.name"),
        "location_pcode": ("location_pcode", "location.p_code", "location.pcode"),
        "latitude": ("location_lat", "latitude", "location.latitude", "location.lat"),
        "longitude": ("location_lon", "longitude", "location.longitude", "location.lon", "location.lng"),
        "location_type": ("location_type", "location.admin_level_name", "location.type"),
        "visit_goals": ("visit_goals", "goals"),
        "objective": ("objective", "objectives", "visit_objective"),
        "supplies": ("dim_supplies", "supplies"),
        "psea": ("dim_psea", "psea"),
    },
    "fm_questions": {
        "activity_id": ("monitoring_activity_id", "activity_id", "monitoring_activity.id", "activity.id"),
        "activity_ref": (
            "monitoring_activity",
            "monitoring_activity_reference_number",
            "activity_reference_number",
            "monitoring_activity.reference_number",
            "reference_number",
        ),
        "question_id": ("question_id", "question.id", "question"),  # 'question' only when an id
        "question_text": (
            "question_text",
            "question.text",
            "question.title",
            "question_title",
            "text",
            "title",
            "question",
            "label",
        ),
        "answer": (
            "answer",
            "value",
            "answer_value",
            "response",
            "finding",
            "answer_text",
            "answer.value",
            "option",
        ),
        "answer_label": ("answer_label", "option_label", "value_label", "option.label", "answer.label"),
        "summary": ("summary", "specific_details", "details", "summary_answer", "narrative", "comment"),
        "is_hact": ("is_hact", "is_hact_question", "hact", "question.is_hact"),
        "order": ("order", "question_order", "question.order", "sequence", "number"),
        "entity": (
            "entity",
            "entity_name",
            "related_to",
            "level_name",
            "partner",
            "intervention",
            "cp_output",
        ),
        "entity_type": ("entity_type", "level", "related_to_type"),
        "method": ("method", "methods", "method_type", "data_collection_method"),
        "sections": ("sections", "section"),
        "offices": ("field_office", "offices", "office"),
    },
    "fm_options": {
        "question_id": ("question_id", "question.id", "question"),
        "value": ("value", "option_value", "code", "id"),
        "label": ("label", "option_label", "text", "name"),
    },
    "fm_programme_activities": {
        "activity_id": ("monitoring_activity_id", "activity_id", "monitoring_activity.id"),
        "activity_ref": (
            "monitoring_activity",
            "monitoring_activity_reference_number",
            "activity_reference_number",
        ),
        "programme_activity": ("programme_activity", "activity", "name", "title", "activity_name"),
        "cp_output": ("cp_output", "cp_output_name", "cp_output.name", "output", "result"),
        "pd_reference": (
            "intervention_number",
            "pd_reference_number",
            "intervention",
            "pd",
            "intervention.number",
        ),
        "section": ("section", "sections", "section_name"),
    },
    "offices": {"name": ("name",)},
    "sections": {"name": ("name",)},
}
PERSON_FIELDS = {("field_monitoring", "team")}
KINDS = {  # others: "text"
    "activity_id": "id",
    "question_id": "id",
    "is_hact": "bool",
    "order": "int",
    "latitude": "number",
    "longitude": "number",
}
DATASETS = tuple(CANDIDATES)
DATASET_LABELS = {
    "field_monitoring": "Field monitoring findings (fm-ontrack)",
    "fm_questions": "Checklist answers (fm-questions)",
    "fm_options": "Answer options (fm-options)",
    "fm_programme_activities": "Programme activities of each visit (fm-programme-activities)",
    "offices": "Field offices (office)",
    "sections": "Sections (reports/sections)",
}
# What each field feeds, in plain words (Fields found)
NEEDED_BY: dict[tuple[str, str], str] = {
    ("field_monitoring", "sections"): "The sections of a visit, before its programme document's",
    ("field_monitoring", "offices"): "The field office of a visit, before its programme document's",
    ("field_monitoring", "team"): "The Team column and the visit page (names only, never sent anywhere)",
    (
        "field_monitoring",
        "pd_reference",
    ): "The programme document of a visit when the entity does not name it",
    ("field_monitoring", "cp_output"): "The CP outputs of a visit",
    ("field_monitoring", "status"): "The visit status when the record's own status column is empty",
    ("field_monitoring", "q1_answer"): "HACT Q1 of the finding row, before its checklist answers (R3)",
    ("field_monitoring", "q2_answer"): "Q2 of the finding row, before its checklist answers (R1)",
    ("field_monitoring", "q3_answer"): "Q3 of the finding row, before its checklist answers (R5)",
    ("field_monitoring", "modality"): "The monitoring modality of a visit (UNICEF staff, TPM...): a filter",
    ("field_monitoring", "programme_areas"): "The programme areas of a visit (visit page)",
    ("field_monitoring", "location_name"): "The place of a visit when its location is not linked",
    ("field_monitoring", "location_pcode"): "The place of a visit, matched to the gazetteer by P-code",
    ("field_monitoring", "latitude"): "The point of a visit on the map (with the longitude)",
    ("field_monitoring", "longitude"): "The point of a visit on the map (with the latitude)",
    ("field_monitoring", "location_type"): "Whether a visit's point is at the lowest admin level (map)",
    ("field_monitoring", "visit_goals"): "The goals of a visit (visit page)",
    ("field_monitoring", "objective"): "The objective of a visit (visit page)",
    ("field_monitoring", "supplies"): "The supplies answer of a finding row (visit page)",
    ("field_monitoring", "psea"): "The PSEA answer of a finding row (visit page)",
    ("fm_questions", "activity_id"): "Which visit an answer belongs to (R2, R3, R5, HACT Q1, PSEA)",
    ("fm_questions", "activity_ref"): "Which visit an answer belongs to, when no activity id is given",
    ("fm_questions", "question_id"): "Telling the questions apart (R2) and their answer options",
    ("fm_questions", "question_text"): "Finding Q1, Q2, Q3 and the PSEA question (R1, R3, R5, PSEA)",
    ("fm_questions", "answer"): "Whether a question was answered and how (R2, R3, R5, HACT Q1, PSEA)",
    ("fm_questions", "answer_label"): "The words of an answer given as an option code",
    ("fm_questions", "summary"): "The summary of an answer (R5)",
    ("fm_questions", "is_hact"): "Telling HACT questions apart (R3)",
    ("fm_questions", "order"): "The order of the questions on the visit page",
    ("fm_questions", "entity"): "Which entity, partner or visit an answer is about (R3, HACT Q1)",
    ("fm_questions", "entity_type"): "Which entity, partner or visit an answer is about",
    ("fm_questions", "method"): "How an answer was collected (visit page)",
    ("fm_questions", "sections"): "The sections of a visit, from its answers",
    ("fm_questions", "offices"): "The field office of a visit, from its answers",
    ("fm_options", "question_id"): "Which question an answer option belongs to",
    ("fm_options", "value"): "The code of an answer option",
    ("fm_options", "label"): "The words of an answer option (HACT Q1, R3, PSEA)",
    ("fm_programme_activities", "activity_id"): "Which visit a programme activity belongs to",
    ("fm_programme_activities", "activity_ref"): "Which visit, when no activity id is given",
    ("fm_programme_activities", "programme_activity"): "The programme activities of a visit",
    ("fm_programme_activities", "cp_output"): "The CP outputs of a visit",
    ("fm_programme_activities", "pd_reference"): "The programme document of a visit",
    ("fm_programme_activities", "section"): "The sections of a visit",
    ("offices", "name"): "Field office names",
    ("sections", "name"): "Section names",
}
ANSWER_FIELDS = ("answer_label", "answer", "summary")  # the answer shown first, as parse.read_answer reads it
AMBIGUOUS_MARGIN = 0.3  # a later candidate this much fuller makes the choice "ambiguous"
MAX_EXAMPLES = 3
MAX_EXAMPLE_TRIES = 200  # values looked at for a key's examples, at most
EXAMPLE_RAW_CHARS = 400  # kept from a value until it is cleaned (well beyond the 60 shown)
MAX_KEYS = 500  # distinct keys counted per dataset (a field's own keys always are); a record keyed by
# ids would add one per id
MAX_DROPPED = 10_000  # keys left uncounted that are remembered, to count them
MAX_KEY_CHARS = 120  # KeyProbe.key
MAX_PERSON_VALUES = 20_000  # values of person keys kept to clean the examples with
CACHE_SECONDS = 60  # key_for() reads the mappings again after this

_cache: tuple[float, dict[tuple[str, str], str | None], dict[tuple[str, str], bool]] | None = None


def kind_of(field: str) -> str:
    return KINDS.get(field, "text")


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


# ------------------------------------------------------------------------------------------ probing
class Probe:
    """What the records of one dataset hold: each key (top level, and one level down as "parent.child")
    with the records holding it, the types of its values and up to three examples; per candidate key
    (and per key an administrator pinned), the records with a usable value of each kind a field needs;
    and, for the checklist answers, how many were answered under each possible choice of keys."""

    def __init__(self, dataset: str, overrides: Mapping[str, str] | None = None) -> None:
        self.dataset = dataset
        self.total = 0
        self.failed = 0
        self.records: Counter[str] = Counter()
        self.types: dict[str, Counter[str]] = defaultdict(Counter)
        self.examples: dict[str, list[Any]] = defaultdict(list)
        self.usable: Counter[tuple[str, str]] = Counter()
        self.answers: Counter[frozenset[tuple[str, str]]] = Counter()
        self.person_values: set[str] = set()
        self._dropped: set[str] = set()  # keys not counted past MAX_KEYS (MAX_DROPPED remembered)
        # the keys each field may be read from: its candidates, and the key an administrator pinned
        # (``overrides``: field -> key), measured like a candidate so that its coverage and answers
        # are known
        fields: dict[str, tuple[str, ...]] = dict(CANDIDATES.get(dataset, {}))
        for field, key in (overrides or {}).items():
            if field in fields and key and key not in fields[field]:
                fields[field] = (*fields[field], key)
        self._wanted = {key for keys in fields.values() for key in keys}  # counted even past MAX_KEYS
        self._pairs: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for field, keys in fields.items():
            for key in keys:
                self._pairs[key.split(".", 1)[0]].add((key, kind_of(field)))
                self._pairs[key].add((key, kind_of(field)))
        self._answer_keys = (
            sorted({key for field in ANSWER_FIELDS for key in fields.get(field, ())})
            if dataset == "fm_questions"
            else []
        )
        # the answer keys by the top-level key they start with, so that a record is looked at under
        # the keys it holds only
        self._answer_tops: dict[str, list[str]] = defaultdict(list)
        for key in self._answer_keys:
            self._answer_tops[key].append(key)
            if "." in key:
                self._answer_tops[key.split(".", 1)[0]].append(key)
        self._person_keys = {key for ds, field in PERSON_FIELDS if ds == dataset for key in fields[field]}
        self._person: dict[str, bool] = {}  # privacy.person_like per key, read once
        self._naming: dict[str, bool] = {}  # privacy.names_person per key, read once
        self._tries: Counter[str] = Counter()  # values looked at for a key's examples

    # ---- reading one record
    def add(self, record: Any) -> None:
        self.total += 1
        if not isinstance(record, dict):  # a JSON record is a dict; anything else holds no key
            return
        pairs: set[tuple[str, str]] = set()
        answer_keys: set[str] = set()
        for key, value in record.items():
            key = str(key)
            self._count(key, value)
            if isinstance(value, dict):
                for child, child_value in value.items():
                    self._count(f"{key}.{child}", child_value)
            elif isinstance(value, list):  # the keys of a list's objects, counted once per record
                children: dict[str, Any] = {}
                for element in value[: parse.MAX_LIST]:
                    if isinstance(element, dict):
                        for child, child_value in element.items():
                            children.setdefault(str(child), child_value)
                for child, child_value in children.items():
                    self._count(f"{key}.{child}", child_value)
            if key in self._pairs:
                pairs |= self._pairs[key]
            if key in self._answer_tops:
                answer_keys.update(self._answer_tops[key])
        for key, kind in pairs:
            if parse.value(record, key, kind) is not None:
                self.usable[(key, kind)] += 1
        if self._answer_keys:
            self.answers[
                frozenset(
                    (key, state)
                    for key in answer_keys
                    if (state := parse.text_state(parse.walk(record, key)))
                )
            ] += 1
        if len(self.person_values) < MAX_PERSON_VALUES:
            self._people(record, depth=0)

    @property
    def keys_dropped(self) -> int:
        """Distinct keys left uncounted because the dataset already had ``MAX_KEYS`` keys."""
        return len(self._dropped)

    def _count(self, key: str, value: Any) -> None:
        key = key[:MAX_KEY_CHARS]
        if key not in self.records and len(self.records) >= MAX_KEYS and key not in self._wanted:
            if len(self._dropped) < MAX_DROPPED:
                self._dropped.add(key)
            return
        self.records[key] += 1
        self.types[key][type_name(value)] += 1
        examples = self.examples[key]
        if len(examples) >= MAX_EXAMPLES or self._tries[key] >= MAX_EXAMPLE_TRIES:
            return
        if self._holds_person(key):
            if not examples:
                examples.append(None)  # shown as "(withheld)"
            return
        if value is None or value == "" or value == [] or value == {}:
            return
        self._tries[key] += 1  # a key whose values repeat ("Lebanon") stops being looked at
        text = privacy.example_text(value)[:EXAMPLE_RAW_CHARS]
        if text and text not in examples:
            examples.append(text)

    def _holds_person(self, key: str) -> bool:
        if key not in self._person:
            self._person[key] = privacy.person_like(key) or key in self._person_keys
        return self._person[key]

    def _names_person(self, key: str) -> bool:
        if key not in self._naming:
            self._naming[key] = privacy.names_person(key) or key in self._person_keys
        return self._naming[key]

    def _people(self, value: Any, depth: int, under_person: bool = False) -> None:
        """Keep the texts written under keys that name a person (not a note or a comment), so that the
        examples of other keys are cleaned of these names too (a team member named in a narrative)."""
        if depth > 4 or len(self.person_values) >= MAX_PERSON_VALUES:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                person = under_person or self._names_person(str(key))
                if person or isinstance(child, dict | list):
                    self._people(child, depth + 1, person)
        elif isinstance(value, list):
            for child in value[: parse.MAX_LIST]:
                self._people(child, depth + 1, under_person)
        elif under_person and isinstance(value, str) and value.strip():
            self.person_values.add(value.strip()[:EXAMPLE_RAW_CHARS])

    # ---- results
    def as_mapping(self) -> dict[str, Any]:
        """The figures :func:`coverage` and :func:`resolve` read (every candidate measured, 0 included)."""
        measured = {pair for pairs in self._pairs.values() for pair in pairs}
        return {
            "total": self.total,
            "keys": dict(self.records),
            "usable": {pair: self.usable.get(pair, 0) for pair in measured},
        }

    def answer_counts(self, keys: Mapping[str, str | None]) -> dict[str, Any]:
        """For the checklist answers: how many records were answered, under the keys chosen for the
        answer, its label and its summary (``keys``). The answer shown is its label, else the answer,
        else the summary, as ``parse.read_answer`` reads it; a blank or placeholder one is not answered.
        Nothing can be told when neither the answer nor its label was found."""
        out: dict[str, Any] = {
            "records": self.total,
            "answers_found": bool(keys.get("answer") or keys.get("answer_label")),
        }
        if not out["answers_found"]:
            return {
                **out,
                "answered": None,
                "unanswered": None,
                "answered_share": None,
                "unanswered_seen": False,
            }
        answered = unanswered = 0  # over the records read (a record that is no object holds no answer)
        for states, n in self.answers.items():
            found = dict(states)
            state = next((found[k] for f in ANSWER_FIELDS if (k := keys.get(f)) and found.get(k)), "")
            if state == "text":
                answered += n
            else:
                unanswered += n
        read = answered + unanswered
        share = round(answered / read, 4) if read else None
        return {
            **out,
            "answered": answered,
            "unanswered": unanswered,
            "answered_share": share,
            "unanswered_seen": unanswered > 0,
        }


def records(dataset: str) -> Iterator[tuple[int, Any]]:
    """(pk, record) of every record of ``dataset``, streamed, without contact details
    (``catalogue.scrub``: the findings table keeps its records as received)."""
    from neurodb.datamart.models import DatamartDocument, MonitoringFinding

    if dataset == "field_monitoring":
        rows = MonitoringFinding.objects.order_by("pk").values_list("pk", "data")
    else:
        rows = DatamartDocument.objects.filter(dataset=dataset).order_by("pk").values_list("pk", "data")
    for pk, data in rows.iterator(chunk_size=2000):
        yield pk, catalogue.scrub(data if data is not None else {})


def overrides(dataset: str) -> dict[str, str]:
    """The keys administrators pinned for the fields of ``dataset``: {field: key}."""
    pinned = FieldMapping.objects.filter(dataset=dataset).exclude(override_key="")
    return {
        m.field: m.override_key.strip()
        for m in pinned
        if m.override_key.strip() and m.field in CANDIDATES.get(dataset, {})
    }


def probe(dataset: str, on_error: Callable[[str, BaseException], None] | None = None) -> Probe:
    """Read every record of ``dataset`` (:class:`Probe`). A record that cannot be read is counted in
    ``Probe.failed`` and given to ``on_error``; the others are read."""
    result = Probe(dataset, overrides(dataset))
    for pk, record in records(dataset):
        try:
            result.add(record)
        except Exception as exc:
            result.failed += 1
            if on_error is not None:
                on_error(f"{dataset} {pk}", exc)
    return result


def write_probe(result: Probe, names: frozenset[str], now=None) -> int:
    """Replace the dataset's ``KeyProbe`` rows with this probe's (examples cleaned with ``names``)."""
    now = now or timezone.now()
    rows = [
        KeyProbe(
            dataset=result.dataset,
            key=key,
            records=n,
            total=result.total,
            types=dict(result.types[key].most_common()),
            examples=[  # None: the key holds a person (the probe kept no value of it)
                privacy.WITHHELD if raw is None else privacy.example(result.dataset, key, raw, names)
                for raw in result.examples.get(key, [])
            ],
            refreshed_at=now,
        )
        for key, n in sorted(result.records.items())
    ]
    KeyProbe.objects.filter(dataset=result.dataset).delete()
    KeyProbe.objects.bulk_create(rows, batch_size=2000)
    return len(rows)


# ------------------------------------------------------------------------------------------ choosing
def coverage(dataset: str, key: str, probe: Mapping, field: str = "") -> float:
    """The share of the dataset's records holding a usable value of ``field``'s kind under ``key``
    (when the probe did not measure that, the share of records holding the key at all)."""
    total = probe.get("total") or 0
    if not total:
        return 0.0
    usable = probe.get("usable") or {}
    pair = (key, kind_of(field))
    count = usable[pair] if pair in usable else (probe.get("keys") or {}).get(key, 0)
    return min(1.0, count / total)


def resolve(dataset: str, field: str, probe: Mapping, current: FieldMapping | None) -> FieldMapping:
    """``current`` (or a new row) with the key chosen for ``field`` (see the module's description).
    ``override_key`` is never changed here."""
    mapping = current or FieldMapping(dataset=dataset, field=field)
    keys = probe.get("keys") or {}
    present = [
        (key, coverage(dataset, key, probe, field)) for key in CANDIDATES[dataset][field] if keys.get(key)
    ]
    mapping.candidates = [{"key": key, "coverage": round(share, 4)} for key, share in present]
    override = (mapping.override_key or "").strip()
    if override and keys.get(override):
        mapping.chosen_key, mapping.state = override, FieldMapping.State.OVERRIDE
        mapping.coverage = round(coverage(dataset, override, probe, field), 4)
        return mapping
    chosen, share, state = _choose(present)
    mapping.chosen_key, mapping.coverage = chosen, round(share, 4)
    mapping.state = FieldMapping.State.OVERRIDE_MISSING if override else state
    return mapping


def _choose(present: list[tuple[str, float]]) -> tuple[str, float, str]:
    minimum = settings.FMM_KEY_MIN_COVERAGE
    for i, (key, share) in enumerate(present):
        if share >= minimum:
            fuller = any(later - share >= AMBIGUOUS_MARGIN - 1e-9 for _, later in present[i + 1 :])
            return key, share, FieldMapping.State.AMBIGUOUS if fuller else FieldMapping.State.FOUND
    best = max(present, key=lambda pair: pair[1], default=None)
    if best and best[1] > 0:
        return best[0], best[1], FieldMapping.State.FOUND
    return "", 0.0, FieldMapping.State.MISSING


def resolve_all(probes: Mapping[str, Probe]) -> list[FieldMapping]:
    """Choose and store the key of every field of the probed datasets (rows of fields no longer listed
    are removed). Administrators' overrides are kept as they are."""
    current = {(m.dataset, m.field): m for m in FieldMapping.objects.all()}
    wanted = {(ds, field) for ds in probes for field in CANDIDATES[ds]}
    out, new, changed = [], [], []
    for dataset, result in probes.items():
        figures = result.as_mapping()
        for field in CANDIDATES[dataset]:
            row = current.get((dataset, field))
            mapping = resolve(dataset, field, figures, row)
            (changed if row is not None else new).append(mapping)
            out.append(mapping)
    FieldMapping.objects.bulk_create(new)
    FieldMapping.objects.bulk_update(changed, ["chosen_key", "coverage", "candidates", "state"])
    gone = [m.pk for key, m in current.items() if key[0] in probes and key not in wanted]
    FieldMapping.objects.filter(pk__in=gone).delete()
    transaction.on_commit(forget)
    forget()
    return out


# ------------------------------------------------------------------------------------------ reading
def _mappings() -> tuple[dict[tuple[str, str], str | None], dict[tuple[str, str], bool]]:
    global _cache
    now = time.monotonic()
    if _cache is None or now - _cache[0] > CACHE_SECONDS:
        keys: dict[tuple[str, str], str | None] = {}
        found: dict[tuple[str, str], bool] = {}
        for mapping in FieldMapping.objects.all():
            pair = (mapping.dataset, mapping.field)
            key = mapping.chosen_key or None
            keys[pair] = None if mapping.state == FieldMapping.State.MISSING else key
            found[pair] = bool(keys[pair]) and mapping.state != FieldMapping.State.MISSING
        _cache = (now, keys, found)
    return _cache[1], _cache[2]


def key_for(dataset: str, field: str) -> str | None:
    """The key ``field`` of ``dataset`` is read from, as the last refresh chose it (an administrator's
    override first); ``None`` when it was not found (or never looked for). Read once a minute."""
    return _mappings()[0].get((dataset, field))


def available(dataset: str, field: str) -> bool:
    """The field was found (or pinned): what needs it can be worked out."""
    return _mappings()[1].get((dataset, field), False)


def forget() -> None:
    """Read the mappings again at the next :func:`key_for` (after a refresh or an override)."""
    global _cache
    _cache = None
