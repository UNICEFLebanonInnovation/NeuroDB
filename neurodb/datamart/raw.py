"""Raw eTools data (Admin → Data and sync): every eTools source NeuroDB reads, record by record, as it came,
for the people who check a page's figures against it (the Validators group, superusers). The pages are
``datamart.raw_admin``; this module holds what they read. The sources are the eTools Datamart's datasets
(their own tables, or ``DatamartDocument``) and the eTools REST API's entities (``RestRecord``, written by
``integrations.etools.rest_raw`` and the eTools REST sync: :data:`REST_ENTITIES`).

* which page reads which source (:data:`PAGE_SOURCES`, :data:`USED_BY`);
* the fields of each source a person reviewed (:data:`REVIEWED_KEYS`, the fields NeuroDB reads, and
  ``RawFieldReview``): any other field shows only numbers, yes/no and dates (``raw_people``);
* the fields each source sends, counted by its last sync on every record it kept (:class:`KeyCount`), or
  from the newest :data:`FIELDS_COUNTED` records NeuroDB holds before a sync counted them;
* how each source is listed and filtered (:data:`VIEWS`, :class:`Filters`);
* its CSV: streamed inside one snapshot of the data, at most :data:`CSV_LIMIT` records, at most
  ``settings.RAW_CSV_MAX_RUNNING`` large downloads at once on the whole site (advisory locks);
* the partner report rows eTools no longer sends, kept and counted (``datamart_sync.keep_unsent``): their
  counts (:func:`no_longer_sent`) and the reports a superuser reviews before removing them
  (:func:`unsent_reports`);
* how the last sync linked the funds reservation lines and headers to their programme document, by the
  number they carry first (``datamart_sync.Links.by_number_first``, :func:`fr_link_words`), the lines whose
  number names no programme document NeuroDB holds, and why a line links to none (:class:`FundsNumbers`);
* whether the data proves the link of PRP's own progress reports to their programme documents, and whether the
  pages could show their reports still owed (``datamart_sync.recorded_pd_link``, :func:`prp_link_words`).

People are never shown or written: every record goes through ``raw_people.public_record``.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import itertools
import json
import logging
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import Q, QuerySet
from django.db.models.fields.json import KT
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.partnerships import statuses as pd_statuses
from neurodb.partnerships.models import PCA, PartnerOrganization

from . import catalogue, raw_people, reporting
from . import query as dmq
from .fm import PDResolver, norm_reference
from .models import (
    DatamartDocument,
    FundsReservation,
    FundsReservationHeader,
    RawFieldReview,
    ReportedIndicator,
    RestRecord,
)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------------- sources
# Plain names, as the pages say them
NAMES: dict[str, str] = {
    "locations": "Locations",
    "location_sites": "Monitoring sites",
    "partners": "Partners",
    "interventions": "Programme documents (as the Datamart sends them)",
    "intervention_budgets": "Programme document budgets",
    "agreements": "Partner agreements",
    "funds_reservations": "Funds reservation lines",
    "grants": "Grants",
    "pd_indicators": "PD indicators",
    "assessments": "HACT assessments",
    "psea_assessments": "PSEA assessments",
    "engagements": "Assurance engagements",
    "tpm_visits": "Third-party monitoring visits",
    "field_monitoring": "Field monitoring findings",
    "hact": "Country HACT totals",
    "funds_reservation_headers": "Funds reservations",
    "partner_reports": "Partner reports (PRP data reports)",
    "tpm_activities": "Third-party monitoring activities",
    "staff_visits": "Staff trip activities",
    "action_points": "Action points",
    "planned_visits": "Programmatic visits planned",
    "hact_history": "Partner HACT per year",
    "pd_activities": "PD workplan activities",
    "audit_results": "Audit results",
    "audits": "Audits",
    "spot_checks": "Spot checks",
    "micro_assessments": "Micro-assessments",
    "special_audits": "Special audits",
    "audit_findings": "Financial findings",
    "intervention_locations": "Programme documents by location",
    "intervention_country_programmes": "Programme documents' country programmes",
    "intervention_epd": "ePD narratives",
    "intervention_management_budget": "Programme management budgets",
    "intervention_reviews": "PRC reviews",
    "pmp_indicators": "Partnership management figures",
    "cp_indicators": "Indicators with baseline and target",
    "attachments": "Attachments",
    "planned_engagements": "Assurance planned",
    "psea_answers": "PSEA answers",
    "fam_indicators": "Financial assurance status",
    "fm_questions": "Field monitoring answers",
    "fm_options": "Field monitoring answer options",
    "fm_programme_activities": "Field monitoring programme activities",
    "staff_trips": "Staff trips",
    "prp_indicator_reports": "PRP indicator reports",
    "prp_programme_documents": "PRP programme documents",
    "prp_progress_reports": "PRP progress reports",
    "offices": "Field offices",
    "sections": "Sections",
    "etools_usage": "eTools use",
    "workspaces": "Workspaces",
    "datamart_etl_status": "Datamart refresh status",
}

# Index table 1: what Progress reports and Partner monitoring read, in this order
PRP_SOURCES = (
    "partner_reports",
    "pd_indicators",
    "prp_indicator_reports",
    "prp_progress_reports",
    "prp_programme_documents",
    "interventions",
    "partners",
    "intervention_budgets",
    "locations",
)
# Index table 3: the eTools REST API entities (integrations.etools.rest_raw.ENTITIES: the same, in that
# order), with the address each is read from
REST_ENTITIES: dict[str, tuple[str, str]] = {
    "rest_partners": ("Partners", "/api/v2/partners/"),
    "rest_partner_details": ("Partner details", "/api/v2/partners/<id>/"),
    "rest_agreements": ("Agreements", "/api/v2/agreements/"),
    "rest_interventions": ("Programme documents", "/api/v2/interventions/"),
    "rest_intervention_details": ("Programme document details", "/api/v2/interventions/<id>/"),
    "rest_travels": ("Trips", "/api/t2f/travels/"),
    "rest_travel_details": ("Trip details", "/api/t2f/travels/<id>/"),
    "rest_engagements": ("Engagements", "/api/audit/engagements/"),
    "rest_engagement_details": ("Engagement details", "/api/audit/<type>/<id>/"),
    "rest_action_points": ("Action points", "/api/action-points/action-points/"),
}
NAMES.update({name: f"{label} (eTools REST API)" for name, (label, _path) in REST_ENTITIES.items()})

PAGES: dict[str, str] = {
    "reports:overview": "Home",
    "reports:partner_reporting": "Progress reports",
    "reports:pd_monitoring": "Partner monitoring",
    "reports:monitoring": "Field monitoring",
    "assistant:ask": "Ask NeuroDB",
}
EVERY_DATASET = "every catalogue dataset"
PAGE_SOURCES: dict[str, tuple[str, ...]] = {  # page (navigation key) -> the sources it reads, in this order
    # Home: its partner reporting blocks, then the More on Home fold (tasks, the year's key figures)
    "reports:overview": (
        "partner_reports",
        "pd_indicators",
        "interventions",
        "partners",
        "locations",
        "field_monitoring",
        "tpm_visits",
        "tpm_activities",
        "action_points",
        "funds_reservation_headers",
    ),
    "reports:partner_reporting": ("partner_reports", "pd_indicators", "interventions", "partners"),
    # the grid, Program Performance, Data quality and the map (its places), Reached with PRP's report totals
    "reports:pd_monitoring": (
        "pd_indicators",
        "partner_reports",
        "prp_indicator_reports",
        "interventions",
        "partners",
        "locations",
    ),
    # every dataset the Field monitoring view reads (its two tabs: findings and visits, the HACT visit plan)
    "reports:monitoring": (
        "field_monitoring",
        "tpm_visits",
        "tpm_activities",
        "staff_visits",
        "planned_visits",
        "hact_history",
        "interventions",
        "partners",
    ),
    "assistant:ask": (EVERY_DATASET,),
}


def _used_by() -> dict[str, tuple[str, ...]]:
    found: dict[str, list[str]] = {}
    for page, sources in PAGE_SOURCES.items():
        names = catalogue.dataset_names() if sources == (EVERY_DATASET,) else sources
        for name in names:
            found.setdefault(name, []).append(page)
    return {name: tuple(pages) for name, pages in found.items()}


USED_BY: dict[str, tuple[str, ...]] = _used_by()


def sources() -> tuple[str, ...]:
    """Every source of Raw eTools data: the eTools Datamart datasets NeuroDB syncs, in the sync's order."""
    from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS

    return tuple(ENTITY_SYNCS)


def other_sources() -> tuple[str, ...]:
    """Index table 2: every other Datamart dataset, in the sync's order."""
    return tuple(name for name in sources() if name not in PRP_SOURCES)


def rest(name: str) -> bool:
    """An entity of the eTools REST API (kept in ``RestRecord``), not a Datamart dataset."""
    return name in REST_ENTITIES


def known(name: str) -> bool:
    """A source of Raw eTools data: a Datamart dataset NeuroDB syncs, or an eTools REST entity."""
    return name in sources() or rest(name)


def path_of(name: str) -> str:
    """The Datamart path, or the eTools REST address, a source is read from."""
    from neurodb.integrations.etools import datamart_sync

    if rest(name):
        return REST_ENTITIES[name][1]
    if name in datamart_sync.DATASETS:
        return datamart_sync.DATASETS[name][0]
    if name in datamart_sync.ENRICHMENTS:
        return datamart_sync.ENRICHMENTS[name][0]
    return catalogue.DOCUMENTS[name].path if name in catalogue.DOCUMENTS else ""


def typed(name: str) -> bool:
    """A source kept in a table of its own (``datamart.models``), not whole in ``DatamartDocument``."""
    return name in catalogue.TYPED


def base(name: str) -> QuerySet:
    """The records NeuroDB holds of a source."""
    if rest(name):
        return RestRecord.objects.filter(entity=name)
    return dmq.dataset(name).base


def model_of(name: str):
    return RestRecord if rest(name) else dmq.dataset(name).model


def has(name: str, column: str) -> bool:
    model = model_of(name)
    return any(getattr(f, "name", "") == column for f in model._meta.get_fields())


# ----------------------------------------------------------------------------------------- what NeuroDB reads
# The fields the syncs read of the sources written by another sync (``datamart_sync``'s handlers), and where
LEGACY_READS: dict[str, dict[str, str]] = {
    "intervention_budgets": {
        "budget_total": "the programme document's budget",
        "budget_unicef_cash": "the programme document's budget",
        "budget_unicef_supply": "the programme document's budget",
        "budget_cso_contribution": "the programme document's budget",
        "budget_currency": "the programme document's budget",
        "number": "the programme document's number",
        "source_id": "the programme document's eTools id",
        "intervention_id": "the programme document's eTools id",
    },
    "agreements": {
        k: "the agreement"
        for k in (
            "reference_number",
            "agreement_type",
            "start",
            "end",
            "signed_by_unicef_date",
            "signed_by_partner_date",
            "partner_name",
        )
    },
    "locations": {
        k: "the locations table"
        for k in ("id", "name", "p_code", "admin_level", "admin_level_name", "parent", "point", "is_active")
    },
    "location_sites": {
        k: "the monitoring sites table"
        for k in ("id", "name", "p_code", "parent", "point", "latitude", "longitude", "is_active")
    },
}


def read_into(name: str) -> dict[str, str]:
    """Each field of a source NeuroDB reads (the fields maps of ``datamart_sync`` and ``catalogue``), and what
    it is read into, in words."""
    from neurodb.integrations.etools import datamart_sync

    found: dict[str, str] = {"id": "the record id", "source_id": "the eTools id"}
    if rest(name):
        found.update(rest_reads(name))
        return found
    if name in datamart_sync.DATASETS:
        spec = datamart_sync.DATASETS[name][1]
        found["last_modify_date"] = "last_modify_date"
        for column, key in spec.fields.items():
            found.setdefault(key, column)
    if name in datamart_sync.ENRICHMENTS:
        for column, key in datamart_sync.ENRICHMENTS[name][1].items():
            found.setdefault(key, f"the engagement's {column}")
    if name == "partners":
        found.update({k: k for k in datamart_sync.PARTNER_FIELDS})
    if name == "interventions":
        for column, key in datamart_sync.INTERVENTION_FIELDS.items():
            found.setdefault(key, column)
        for key, column in (
            ("reference_number", "number"),
            ("partner_source_id", "partner"),
            ("partner_vendor_number", "partner"),
            ("sections_data", "section_names"),
            ("sections", "section_names"),
            ("offices_data", "offices_set"),
            ("unicef_focal_points_data", "unicef_focal_points"),
            ("cp_outputs_data", "cp_outputs"),
            ("donors", "donors"),
            ("grants", "grants"),
            ("locations_data", "location_p_codes"),
            ("agreement_type", "agreement"),
        ):
            found.setdefault(key, column)
    found.update({k: v for k, v in LEGACY_READS.get(name, {}).items() if k not in found})
    spec = catalogue.DOCUMENTS.get(name)
    if spec is not None:
        for key, into in (
            (spec.partner[0], "partner"),
            (spec.partner[1], "partner"),
            (spec.intervention[0], "programme document"),
            (spec.intervention[1], "programme document"),
            (spec.date, "date"),
            (spec.keep, "the country's records"),
            *((k, "title") for k in spec.title),
            *((k, "a page, figure or check") for k in spec.reads),
        ):
            if key:
                found.setdefault(key, into)
    return found


def rest_reads(name: str) -> dict[str, str]:
    """The fields the eTools REST sync reads of an entity (``integrations.etools.sync``'s fields), and what it
    reads them into."""
    from neurodb.integrations.etools import sync

    into = {
        "rest_partners": ("the partner", ("name", "partner_type", *sync.PARTNER_FIELDS)),
        "rest_partner_details": ("the partner", sync.PARTNER_DETAIL_FIELDS),
        "rest_agreements": ("the agreement", ("partner", *sync.AGREEMENT_FIELDS)),
        "rest_interventions": (
            "the programme document",
            (*sync.INTERVENTION_FIELDS, "end", "offices_names", "unicef_focal_points", "location_p_codes"),
        ),
        "rest_intervention_details": (
            "the programme document",
            ("partner_id", "agreement", *sync.INTERVENTION_DETAIL_FIELDS),
        ),
        "rest_travels": ("the trip", (*sync.TRAVEL_FIELDS.values(), "status", "section", "office")),
        "rest_travel_details": ("the trip", (*sync.TRAVEL_DETAIL_FIELDS, "attachments", "activities")),
        "rest_engagements": (
            "the engagement",
            (*sync.ENGAGEMENT_FIELDS.values(), *sync.ENGAGEMENT_LIST_FIELDS, "partner"),
        ),
        "rest_engagement_details": (
            "the engagement",
            (*sync.ENGAGEMENT_DETAIL_FIELDS, "findings", "active_pd"),
        ),
        "rest_action_points": (
            "the action point",
            (*sync.ACTION_POINT_FIELDS, "related_module", "category", "section", "office", "partner"),
        ),
    }[name]
    return {key: into[0] for key in into[1]}


# The fields reviewed in the code, per source: the fields of the repository's test payloads for that source
# whose words cannot hold a name (the last word an id, a date or a time, a kind or a status; an amount or a
# measure, but in field monitoring, whose values are what a monitor wrote), and the country fields every
# source may name (COUNTRY_KEYS). With the fields NeuroDB reads (read_into) and a superuser's reviews
# (RawFieldReview), they are shown, through the people rule; every other field shows only numbers, yes/no and
# dates. Pinned by a test: a change is deliberate.
COUNTRY_KEYS = frozenset({"country_name", "business_area", "business_area_code"})
REVIEWED_KEYS: dict[str, frozenset[str]] = {
    "action_points": frozenset(
        {
            "budget",
            "cost",
            "intervention_source_id",
            "local_amount",
            "overall_status",
            "partner_source_id",
        }
    ),
    "cp_indicators": frozenset({"target_numerator"}),
    "engagements": frozenset({"spotcheck_total_amount_tested"}),
    "fm_options": frozenset({"question_id"}),
    "fm_programme_activities": frozenset({"intervention_number", "monitoring_activity_id", "vendor_number"}),
    "fm_questions": frozenset({"monitoring_activity_id", "question_id"}),
    "funds_reservations": frozenset({"source_intervention_id"}),
    "hact_history": frozenset({"audits_outstanding_findings", "partner_source_id", "pv_completed_year"}),
    "intervention_epd": frozenset({"hq_support_cost"}),
    "interventions": frozenset(
        {
            "agreement_id",
            "agreement_reference_number",
            "funds_reservation",
            "intervention_id",
        }
    ),
    "partner_reports": frozenset(
        {
            "etools_cp_output_id",
            "etools_intervention_id",
            "indicator_type",
            "reporting_period_due_date",
        }
    ),
    "pd_indicators": frozenset({"cluster_indicator_id"}),
    "planned_visits": frozenset({"partner_vendor_number"}),
    "prp_indicator_reports": frozenset({"financial_contribution_currency", "status"}),
    "prp_programme_documents": frozenset({"budget", "end_date", "legacy_id", "prp_id"}),
    "prp_progress_reports": frozenset({"due_date", "programme_document_id", "status", "submission_date"}),
    "staff_trips": frozenset({"approved_cost", "approved_cost_traveler", "end_date"}),
    "staff_visits": frozenset({"source_partner_id", "source_partnership_id"}),
    "tpm_visits": frozenset({"source_partner_id"}),
}


def reviewed_by_hand(name: str) -> frozenset[str]:
    """The fields of a source a superuser reviewed (``RawFieldReview``, read once per request)."""
    from neurodb.core import compute

    def read() -> list[tuple[str, str]]:
        return list(RawFieldReview.objects.order_by().values_list("source", "key"))

    return frozenset(key for source, key in compute.per_request("raw:field-reviews", read) if source == name)


def reviewed_keys(name: str) -> frozenset[str]:
    """The reviewed fields of a source: :data:`REVIEWED_KEYS`, the fields NeuroDB reads, and those a superuser
    reviewed."""
    if name in catalogue.SAFEGUARDING:  # the fields Ask reads of a PSEA answer, and no other
        return catalogue.SAFEGUARDING_KEYS
    found = REVIEWED_KEYS.get(name, frozenset()) | COUNTRY_KEYS | set(read_into(name))
    return frozenset(found | reviewed_by_hand(name))


# --------------------------------------------------------------------------------------- the fields counted
FIELDS_COUNTED = 2000  # the newest records the fields of a source are counted from
KEYS_KEPT = 400  # the fields a sync's count keeps (details.keys_kept), the most carried first
MORE_FIELDS = "(more fields)"  # the fields past the 400th: their counts summed
OTHER_FIELDS = "(other fields)"  # records carrying a field whose name is never stored; with a value in one
_EMPTY = (None, "", [], {})


class KeyCount:
    """The top-level fields of the records a sync keeps, counted as it writes them: for each field, the
    records carrying it and those where it holds a value (anything but empty, ``[]`` or ``{}``), into the
    run's ``details.keys_kept`` (:meth:`details`). Counted on the record as stored (``catalogue.scrub``: no
    contact field). A field whose name is not an identifier (a space or an ``@``, longer than 64 characters,
    shaped like an e-mail address, a link or a phone: ``raw_people.identifier``) is never stored by name: it
    adds to :data:`OTHER_FIELDS`, as does any field of a PSEA answer outside the safeguarding fields. The
    run's details are shown to Administrators as they are, hence the names are sorted out before they are
    kept."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.carrying: Counter[str] = Counter()
        self.with_value: Counter[str] = Counter()
        self.other = [0, 0]  # records carrying a field never stored by name, and with a value in one
        self._fm = source in catalogue.FM_PRIVATE
        self._safeguarding = source in catalogue.SAFEGUARDING
        self._named: dict[Any, bool] = {}  # a field name: may it be stored (looked at once per run)

    def _stored(self, key: Any) -> bool:
        found = self._named.get(key)
        if found is None:
            found = self._named[key] = (
                isinstance(key, str)
                and (not self._safeguarding or key in catalogue.SAFEGUARDING_KEYS)
                and raw_people.identifier(key, self._fm)
            )
        return found

    def add(self, data: Any) -> None:
        """Count one record kept (as stored)."""
        if not isinstance(data, dict):
            return
        named = [k for k in data if self._stored(k)]
        self.carrying.update(named)
        self.with_value.update(k for k in named if data[k] not in _EMPTY)
        if len(named) < len(data):
            self.other[0] += 1
            self.other[1] += any(data[k] not in _EMPTY for k in data if not self._stored(k))

    def details(self) -> dict[str, list[int]]:
        """``{"<field>": [records carrying it, records where it holds a value]}``, the most carried first, at
        most :data:`KEYS_KEPT` fields; the others summed under :data:`MORE_FIELDS`; :data:`OTHER_FIELDS`."""
        ranked = sorted(self.carrying.items(), key=lambda kv: (-kv[1], kv[0]))
        found = {key: [n, self.with_value[key]] for key, n in ranked[:KEYS_KEPT]}
        rest = ranked[KEYS_KEPT:]
        if rest:
            found[MORE_FIELDS] = [sum(n for _, n in rest), sum(self.with_value[k] for k, _ in rest)]
        if self.other[0]:
            found[OTHER_FIELDS] = list(self.other)
        return found


def run_details(run: SyncRun | None, name: str = "") -> dict[str, Any]:
    """The details of ``run`` that are about the source ``name``: a run of the eTools REST sync keeps those of
    each raw copy it wrote under ``raw_copies`` (one run reads a list and its details)."""
    details = run.details if run is not None and isinstance(run.details, dict) else {}
    if run is not None and run.job == SyncRun.Job.ETOOLS:
        found = (details.get("raw_copies") or {}).get(name)
        return found if isinstance(found, dict) else {}
    return details


def keys_kept(run: SyncRun | None, name: str = "") -> dict[str, list[int]]:
    """The fields a run counted as it wrote (:class:`KeyCount`), or {} for a run before they were counted."""
    found = run_details(run, name).get("keys_kept")
    return found if isinstance(found, dict) else {}


_NUMBER = dmq.NUMBER
_DATE = dmq.DATE_VALUE
_TIME = r"^\d{2}:\d{2}(:\d{2}(\.\d+)?)?$"


@dataclass(frozen=True)
class FieldCount:
    key: str
    carrying: int  # records carrying the field
    with_value: int  # records where it holds a value
    texts: int  # records where it holds a text (not a number, a date, yes/no or empty)
    use: str = ""  # how NeuroDB uses it, in words
    kind: str = ""  # "read", "people", "text", "cleaned", "plain", "new"


USES = {
    "people": "names people: shown as a count",
    "text": "written by a person: withheld",
    "cleaned": "a text: shown without names, e-mails, links and phones",
    "plain": "shown as received",
    "new": "new field: not shown until reviewed",
}


def _table_and_where(name: str) -> tuple[str, str, list[Any]]:
    if typed(name):
        return connection.ops.quote_name(model_of(name)._meta.db_table), "", []
    if rest(name):
        return connection.ops.quote_name(RestRecord._meta.db_table), "AND entity = %s", [name]
    return connection.ops.quote_name(DatamartDocument._meta.db_table), "AND dataset = %s", [name]


def field_counts(
    name: str, names: frozenset[str] | None = None, run: SyncRun | None = None
) -> tuple[list[FieldCount], int]:
    """The top-level fields of a source: how many records carry each, hold a value in it, and hold a text in
    it; and how many fields are left out because their names are not identifiers or name a person
    (``raw_people``). The answers of PSEA assessments list only their safeguarding fields. Most carried first.

    Counted from the newest :data:`FIELDS_COUNTED` records NeuroDB holds, in one query; when ``run`` counted
    the fields of every record it kept (:func:`keys_kept`), its counts are the ones given, and the query only
    says which fields hold a text (a field none of those records carries is taken to hold one)."""
    found = _sampled(name)
    kept = {k: v for k, v in keys_kept(run, name).items() if k not in (MORE_FIELDS, OTHER_FIELDS)}
    if kept:
        texts = {key: n for key, _carrying, _value, n in found}
        found = [
            (key, int(counts[0]), int(counts[1]), texts.get(key, int(counts[1])))
            for key, counts in kept.items()
            if isinstance(counts, list) and len(counts) == 2
        ]
    return _described(name, found, names)


def _sampled(name: str) -> list[tuple[str, int, int, int]]:
    table, where, params = _table_and_where(name)
    query = (
        "SELECT k, count(*), "  # noqa: S608 - the model's table
        "count(*) FILTER (WHERE NOT (t.data -> k IN "
        "('null'::jsonb, '\"\"'::jsonb, '[]'::jsonb, '{}'::jsonb))), "
        "count(*) FILTER (WHERE (jsonb_typeof(t.data -> k) IN ('object', 'array') "
        "AND jsonb_path_exists(t.data -> k, 'strict $.** ? (@.type() == \"string\")')) "
        "OR (jsonb_typeof(t.data -> k) = 'string' AND btrim(t.data ->> k) <> '' "
        "AND t.data ->> k !~ %s AND t.data ->> k !~ %s AND t.data ->> k !~ %s)) "
        f"FROM (SELECT data FROM {table} WHERE jsonb_typeof(data) = 'object' {where} "
        "ORDER BY id DESC LIMIT %s) t, jsonb_object_keys(t.data) k GROUP BY k"
    )
    with connection.cursor() as cursor:
        cursor.execute(query, [_DATE, _NUMBER, _TIME, *params, FIELDS_COUNTED])
        return cursor.fetchall()


def _described(
    name: str, found: list[tuple[str, int, int, int]], names: frozenset[str] | None
) -> tuple[list[FieldCount], int]:
    from neurodb.watch import people

    names = people.known_names() if names is None else names
    rules = dmq.Rules(names, name)
    reviewed = reviewed_keys(name)
    reads = read_into(name)
    out, left_out = [], 0
    for key, carrying, with_value, texts in found:
        if dmq._contact_key(key):  # never stored (catalogue.scrub), never counted
            continue
        if name in catalogue.SAFEGUARDING and key not in catalogue.SAFEGUARDING_KEYS:
            left_out += 1
            continue
        if not raw_people.identifier(key, rules.fm) or dmq._key_withheld(key, rules, frozenset()):
            left_out += 1
            continue
        if raw_people.names_people(key):
            kind = "text" if raw_people.written(key) else "people"
        elif key in reads:
            kind = "read"
        elif not texts:
            kind = "plain"
        elif key in reviewed:
            kind = "cleaned"
        else:
            kind = "new"
        use = f"read into {reads[key]}" if kind == "read" else USES[kind]
        out.append(FieldCount(key, carrying, with_value, texts, use, kind))
    out.sort(key=lambda f: (-f.carrying, f.key))
    return out, left_out


def unsent(name: str, counted: list[FieldCount]) -> list[str]:
    """The fields NeuroDB reads that no record counted sent."""
    seen = {f.key for f in counted}
    return sorted(k for k in read_into(name) if k not in seen and k not in ("id", "source_id"))


# ---------------------------------------------------------------------------------------------- how listed
@dataclass(frozen=True)
class RawView:
    """How a source is listed and filtered on its page. ``columns``: (label, what) where what is ``id``,
    ``pd`` (the programme document in NeuroDB), ``partner`` (the partner in NeuroDB), ``vendor``, ``title``,
    ``date``, ``report`` (type and number), ``updated`` (updated by the last sync), ``t:<column>`` (NeuroDB's
    column, through the pinned lists of ``raw_people``) or ``d:<field>`` (the record's field, through the
    people rule); empty: the generic columns and the most carried reviewed fields. ``pd``, ``partner``: the
    columns those filters compare; ``section``: the section column; ``status_key``: the status column (or
    field of a record kept whole); ``date``: the date column; ``search``: the columns a search reads (never a
    person, a text or the record itself); ``title``: the column shown as a typed record's title."""

    columns: tuple[tuple[str, str], ...] = ()
    pd: tuple[str, ...] = ()
    partner: tuple[str, ...] = ()
    section: str = ""
    status_key: str = ""
    date: str = ""
    search: tuple[str, ...] = ()
    title: str = ""
    payload_fields: int = 5  # the most carried reviewed fields listed after the generic columns


VIEWS: dict[str, RawView] = {
    "partner_reports": RawView(
        columns=(
            ("Record id", "id"),
            ("PD reference", "t:pd_reference_number"),
            ("PD in NeuroDB", "pd"),
            ("Partner in NeuroDB", "partner"),
            ("Vendor number", "t:vendor_number"),
            ("Report", "report"),
            ("Status as received", "t:report_status"),
            ("Period start", "t:period_start"),
            ("Period end", "t:period_end"),
            ("Due", "t:due_date"),
            ("Submitted", "t:submission_date"),
            ("Indicator", "t:indicator"),
            ("Location", "t:location"),
            ("Value in the period", "t:achievement_in_period"),
            ("Updated by the last sync", "updated"),
        ),
        pd=("pd_reference_number",),
        partner=("partner_name", "vendor_number"),
        section="section",
        status_key="report_status",
        date="period_end",
        search=(
            "pd_reference_number",
            "vendor_number",
            "indicator",
            "progress_report",
            "indicator_report",
            "p_code",
        ),
    ),
    "pd_indicators": RawView(
        columns=(
            ("Record id", "id"),
            ("PD reference", "t:pd_reference_number"),
            ("PD in NeuroDB", "pd"),
            ("Partner in NeuroDB", "partner"),
            ("Indicator", "t:title"),
            ("PD output", "t:lower_result_name"),
            ("Section", "t:section_name"),
            ("Location", "t:location_name"),
            ("Target", "t:target_numerator"),
            ("Active", "t:is_active"),
            ("Emergency", "t:is_high_frequency"),
        ),
        pd=("pd_reference_number",),
        section="section_name",
        search=("pd_reference_number", "lower_result_name", "location_name", "location_pcode"),
    ),
    "prp_indicator_reports": RawView(
        columns=(
            ("Record id", "id"),
            ("PD reference", "d:pd_reference_number"),
            ("Partner in NeuroDB", "partner"),
            ("Vendor number", "d:partner"),
            ("Indicator", "d:performance_indicator"),
            ("Report", "report"),
            ("Status", "d:report_status"),
            ("Period end", "d:time_period_end"),
            ("Progress value", "d:progress_value"),
        ),
        status_key="report_status",
    ),
    "prp_progress_reports": RawView(
        columns=(
            ("Record id", "id"),
            ("Reference", "d:report_number"),
            ("Title", "title"),
            ("PD in NeuroDB", "pd"),
            ("Partner in NeuroDB", "partner"),
            ("Status", "d:status"),
            ("Period end", "d:end_date"),
            ("Due", "d:due_date"),
            ("Submitted", "d:submission_date"),
        ),
        status_key="status",
        payload_fields=3,
    ),
    "prp_programme_documents": RawView(
        columns=(
            ("Record id", "id"),
            ("Reference", "d:reference_number"),
            ("Title", "title"),
            ("Status", "d:status"),
            ("Date", "date"),
        ),
        status_key="status",
        payload_fields=3,
    ),
    "interventions": RawView(
        columns=(
            ("Record id", "id"),
            ("Reference", "d:number"),
            ("Title", "title"),
            ("Status", "d:status"),
            ("Date", "date"),
        ),
        status_key="status",
        payload_fields=3,
    ),
}
# A typed record's title: a reference or a name the page lists it by (never a person or a text people wrote)
TITLES: dict[str, str] = {
    "funds_reservations": "fr_number",
    "funds_reservation_headers": "fr_number",
    "grants": "name",
    "assessments": "type",
    "psea_assessments": "reference_number",
    "engagements": "reference_number",
    "tpm_visits": "reference_number",
    "field_monitoring": "reference_number",
    "hact": "year",
    "tpm_activities": "task_reference_number",
    "staff_visits": "travel_reference_number",
    "action_points": "reference_number",
    "planned_visits": "pd_reference_number",
    "hact_history": "year",
    "pd_activities": "code",
    "audit_findings": "reference_number",
}
# A record kept whole whose title can name a person (a trip's purpose, a file's name) is never searched by it;
# nor a trip of the eTools REST API (its title is its reference number, searched as such)
TITLE_NOT_SEARCHED = frozenset({"staff_trips", "attachments", "rest_travels", "rest_travel_details"})
_PD_COLUMNS = ("pd_reference_number", "intervention_number", "partnership_number")
_PARTNER_COLUMNS = ("partner_name", "vendor_number")
_SEARCHED = (  # the typed columns a search reads where a source has them: references and numbers only
    "reference_number",
    "pd_reference_number",
    "vendor_number",
    "fr_number",
    "grant_number",
    "task_reference_number",
    "visit_reference_number",
    "travel_reference_number",
    "partnership_number",
    "module_reference_number",
    "intervention_number",
)


# The eTools REST entities: their reference is a programme document's or an agreement's number, a partner's
# vendor number, a trip's, an engagement's or an action point's reference number; the field naming a partner
# by its eTools id ("partner__id": the id of the partner object a record holds)
REST_PD = frozenset({"rest_interventions", "rest_intervention_details", "rest_agreements"})
REST_PARTNERS = frozenset({"rest_partners", "rest_partner_details"})
REST_PARTNER_KEYS = {
    "rest_agreements": "partner",
    "rest_intervention_details": "partner_id",
    "rest_engagements": "partner__id",
    "rest_action_points": "partner__id",
}
REST_VIEW = RawView(
    columns=(
        ("Record id", "id"),
        ("Reference", "t:reference"),
        ("Title", "title"),
        ("Updated by the last sync", "updated"),
    ),
    search=("reference", "record_key"),  # and the title, but a trip's (_search_q)
    title="title",
)


def view(name: str) -> RawView:
    """How a source is listed and filtered: its own :data:`VIEWS` entry, else one built from its table."""
    if name in VIEWS:
        return VIEWS[name]
    if rest(name):
        return REST_VIEW
    if not typed(name):
        return RawView()
    status = next((c for c in ("status", "report_status") if has(name, c)), "")
    return RawView(
        pd=tuple(c for c in _PD_COLUMNS if has(name, c)),
        partner=tuple(c for c in _PARTNER_COLUMNS if has(name, c)),
        section="section" if has(name, "section") else "",
        status_key=status,
        date=catalogue.TYPED[name][2],
        search=tuple(c for c in _SEARCHED if has(name, c)),
        title=TITLES.get(name, ""),
    )


def date_column(name: str) -> str:
    """The column a source's year and dates filter reads: its main date (``date`` for a record kept whole; the
    day an eTools REST record was last updated)."""
    if rest(name):
        return "synced_at__date"
    if not typed(name):
        return "date" if catalogue.DOCUMENTS[name].date else ""
    return view(name).date or catalogue.TYPED[name][2]


def status_field(name: str) -> str:
    """The field the status filter reads: a typed record's status column, or the reviewed status field of a
    record kept whole ("" when it has none, as an eTools REST record)."""
    found = view(name).status_key
    if typed(name) or not found or rest(name):
        return found
    return found if found in reviewed_keys(name) else ""


# ----------------------------------------------------------------------------------------------- filtering
PD_LINKS = {  # Partner reports' "Programme document link": the exact rules the pages use
    "running": Q(intervention__status__in=pd_statuses.RUNNING),
    "other_status": reporting.LEFT_OUT_REASONS["other_status"]
    & ~Q(intervention__status__in=pd_statuses.RUNNING + pd_statuses.NOT_A_PARTNERSHIP),
    "draft_or_cancelled": Q(intervention__status__in=pd_statuses.NOT_A_PARTNERSHIP),
    "not_held": reporting.LEFT_OUT_REASONS["not_held"],
    "no_pd": reporting.LEFT_OUT_REASONS["no_pd"],
}
PD_LINK_WORDS = {
    "running": "running",
    "other_status": "another status",
    "draft_or_cancelled": "draft or cancelled",
    "not_held": "not in NeuroDB",
    "no_pd": "names none",
}
# The funds reservation lines' and headers' "Programme document link": those whose number (their own, else a
# line's FR header's) names no programme document NeuroDB holds, by the rule the sync links them by
# (FundsNumbers.not_held); and a line's PD in NeuroDB when its number fits several (the sync links it to none)
FR_SOURCES = ("funds_reservations", "funds_reservation_headers")
FR_LINK_WORDS = {"number_not_held": "Their number names no PD NeuroDB holds"}
FR_SEVERAL = "several PDs fit"
UPDATED = {"last": "the last sync", "earlier": "an earlier sync"}
# the partner reports' "Updated by" choices for the rows eTools no longer sends (datamart_sync.keep_unsent):
# those whose number eTools sends for another period (to review first), all of them, and those whose status
# PRP's own progress reports give otherwise
UNSENT = {
    "unsent_period": "eTools sends their number for another period",
    "unsent": "eTools no longer sends them",
    # those PRP's own progress reports give another status (datamart_sync.unsent_word_reports, check 11)
    "unsent_word": "PRP's progress reports give them another status",
}


# The PD indicators' "Active" choices (eTools' is_active): an indicator inactive in every row is mostly an
# older version of the PD's indicator (identity.inactive_indicators, inactive_words)
ACTIVE_WORDS = {"yes": "yes", "no": "no"}


def updated_choices(name: str) -> dict[str, str]:
    """The "Updated by" choices of a source's filter; the partner reports' add the rows no longer sent."""
    return {**UPDATED, **UNSENT} if name == "partner_reports" else UPDATED


REFUSED = "eTools records cannot be searched for a person."


def _text(params, key: str, most: int = 100) -> str:
    return " ".join(str(params.get(key) or "").split())[:most]


def _date(params, key: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(_text(params, key, 10))
    except ValueError:
        return None


def _year(params) -> int | None:
    text = _text(params, "year", 4)
    return int(text) if text.isdigit() and 2000 <= int(text) <= 2100 else None


@dataclass
class Filters:
    """The filter of a source's page (a GET form), as the page and its CSV read it."""

    pd: str = ""
    partner: str = ""
    section: str = ""
    status: str = ""
    year: int | None = None
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    q: str = ""
    record: str = ""
    updated: str = ""
    report_type: str = ""
    pd_link: str = ""
    fr_link: str = ""
    active: str = ""  # the PD indicators': "yes" or "no" (is_active)
    refused: str = field(default="", compare=False)

    @classmethod
    def from_params(cls, params) -> Filters:
        found = cls(
            pd=_text(params, "pd"),
            partner=_text(params, "partner"),
            section=_text(params, "section"),
            status=_text(params, "status", 200),
            year=_year(params),
            date_from=_date(params, "from"),
            date_to=_date(params, "to"),
            q=_text(params, "q"),
            record=_text(params, "record", 64),
            updated=_text(params, "updated") if params.get("updated") in {**UPDATED, **UNSENT} else "",
            report_type=_text(params, "report_type", 30).upper(),
            pd_link=_text(params, "pd_link") if params.get("pd_link") in PD_LINKS else "",
            fr_link=_text(params, "fr_link") if params.get("fr_link") in FR_LINK_WORDS else "",
            active=_text(params, "active") if params.get("active") in ACTIVE_WORDS else "",
        )
        from neurodb.watch import people

        if any(text and people.mentions(text) for text in (found.q, found.partner)):
            found.refused = REFUSED
        return found

    def query(self) -> dict[str, str]:
        """The filter as GET parameters (for the CSV's address and the pages' links)."""
        values = {
            "pd": self.pd,
            "partner": self.partner,
            "section": self.section,
            "status": self.status,
            "year": str(self.year or ""),
            "from": self.date_from.isoformat() if self.date_from else "",
            "to": self.date_to.isoformat() if self.date_to else "",
            "q": self.q,
            "record": self.record,
            "updated": self.updated,
            "report_type": self.report_type,
            "pd_link": self.pd_link,
            "fr_link": self.fr_link,
            "active": self.active,
        }
        return {k: v for k, v in values.items() if v}

    def words(self) -> str:
        """The filter in the words of the page's form ("every record" when nothing is chosen)."""
        found = [
            ("Programme document", self.pd),
            ("Partner", self.partner),
            ("Section", self.section),
            ("Status", self.status),
            ("Report type", self.report_type),
            (
                "Programme document link",
                PD_LINK_WORDS.get(self.pd_link, "") or FR_LINK_WORDS.get(self.fr_link, ""),
            ),
            ("Year", str(self.year or "")),
            ("From", self.date_from.isoformat() if self.date_from else ""),
            ("To", self.date_to.isoformat() if self.date_to else ""),
            ("Search", self.q),
            ("Record id", self.record),
            ("Updated by", {**UPDATED, **UNSENT}.get(self.updated, "")),
            ("Active", ACTIVE_WORDS.get(self.active, "")),
        ]
        return "; ".join(f"{label}: {value}" for label, value in found if value) or "every record"

    def apply(self, name: str, rows: QuerySet, run: SyncRun | None) -> QuerySet:
        """``rows`` narrowed by the filter, as the page states it (a refused search finds nothing)."""
        if self.refused:
            return rows.none()
        spec = view(name)
        if self.pd:
            rows = rows.filter(_pd_q(name, spec, self.pd))
        if self.partner and rest(name):
            rows = _rest_partner(name, rows, self.partner)
        elif self.partner:
            rows = rows.filter(_partner_q(name, spec, self.partner))
        if self.section:
            rows = rows.filter(_section_q(name, spec, self.section))
        status = status_field(name)
        if self.status and status:
            rows = rows.filter(**{status if typed(name) else f"data__{status}": self.status})
        date = date_column(name)
        if date and self.year:
            rows = rows.filter(**{f"{date}__year": self.year})
        if date and self.date_from:
            rows = rows.filter(**{f"{date}__gte": self.date_from})
        if date and self.date_to:
            rows = rows.filter(**{f"{date}__lte": self.date_to})
        if self.q:
            rows = rows.filter(_search_q(name, spec, self.q))
        if self.record:
            rows = rows.filter(_record_q(name, self.record))
        if self.updated in UNSENT and name == "partner_reports":  # never offered elsewhere
            if self.updated == "unsent":
                rows = rows.exclude(not_sent_since=None)
            elif self.updated == "unsent_word":
                from neurodb.integrations.etools.datamart_sync import unsent_word_reports  # lazy: the sync

                rows = (
                    rows.exclude(not_sent_since=None)
                    .annotate(_report=reporting.REPORT_KEY)
                    .filter(_report__in=sorted(unsent_word_reports()))
                )
            else:
                rows = reporting.sent_for_another_period(rows)
        elif self.updated in UPDATED and run is not None and run.finished_at is not None:
            window = Q(synced_at__gte=run.started_at, synced_at__lte=run.finished_at)
            rows = (
                rows.filter(window) if self.updated == "last" else rows.filter(synced_at__lt=run.started_at)
            )
        if name == "partner_reports":
            if self.report_type:
                rows = rows.filter(report_type=self.report_type)
            if self.pd_link:
                rows = rows.filter(PD_LINKS[self.pd_link])
        if name in FR_SOURCES and self.fr_link == "number_not_held":
            rows = rows.filter(FundsNumbers().not_held(rows))
        if name == "pd_indicators" and self.active:
            rows = rows.filter(is_active=self.active == "yes")
        return rows


class FundsNumbers:
    """The reference number a funds reservation line or header is linked by, read as the sync reads it
    (``datamart_sync.Links.by_number_first``): its own, else, for a line, its FR header's
    (``datamart_sync.fr_header_numbers``); and the programme documents NeuroDB holds that it may name
    (``fm.PDResolver.candidates``: the exact number, another amendment's, the PD part alone). One for a page,
    a download or a filter: the programme documents and the headers are each read once, when first needed."""

    def __init__(self) -> None:
        self._resolver: PDResolver | None = None
        self._headers: dict[str, str] | None = None

    def headers(self) -> dict[str, str]:
        if self._headers is None:
            from neurodb.integrations.etools.datamart_sync import fr_header_numbers  # lazy: the Datamart sync

            self._headers = fr_header_numbers()
        return self._headers

    def number_of(self, row: Any) -> str:
        """The number ``row`` is linked by, normalised; '' when it has none (it links by its eTools id)."""
        own = norm_reference(row.pd_reference_number)
        if own or not isinstance(row, FundsReservation):
            return own
        return self.headers().get((row.fr_number or "").strip(), "")

    def fit(self, number: Any) -> list[int]:
        """The programme documents NeuroDB holds that ``number`` may name."""
        if self._resolver is None:
            self._resolver = PDResolver()
        return self._resolver.candidates(number)[0]

    def not_held(self, rows: QuerySet) -> Q:
        """Of ``rows`` (funds reservation lines or headers), those whose number names no programme document
        NeuroDB holds (what the sync counts "number_not_held"): their own number, or, for a line without one,
        its FR header's. A row with no number at all links by its eTools id: never one of them."""
        numbered = rows.exclude(pd_reference_number="").order_by()
        numbers = numbered.values_list("pd_reference_number", flat=True).distinct()
        found = Q(pd_reference_number__in=[n for n in numbers if norm_reference(n) and not self.fit(n)])
        if rows.model is FundsReservation:
            frs = [fr for fr, number in self.headers().items() if not self.fit(number)]
            found |= Q(pd_reference_number="", fr_number__in=frs)
        return found


def _rest_partner(name: str, rows: QuerySet, value: str) -> QuerySet:
    """An eTools REST entity's records of a partner: by its vendor number or eTools id (a partner's record),
    else by the eTools id of the partner a record names; never by a name or a title."""
    if name in REST_PARTNERS:
        return rows.filter(Q(reference=value) | Q(source_id=value))
    key = REST_PARTNER_KEYS.get(name)
    if not key:
        return rows.none()
    return rows.annotate(_partner=KT(f"data__{key}")).filter(_partner=value)


def _pd_q(name: str, spec: RawView, value: str) -> Q:
    if rest(name):
        return Q(reference=value) if name in REST_PD else Q(pk__in=[])
    if typed(name):
        found = Q()
        for column in spec.pd:
            found |= Q(**{column: value})
        if has(name, "intervention"):
            found |= Q(intervention__number=value)
        return found or Q(pk__in=[])
    found = Q(intervention__number=value)
    key = catalogue.DOCUMENTS[name].intervention[1]
    return found | Q(**{f"data__{key}": value}) if key else found


def _partner_q(name: str, spec: RawView, value: str) -> Q:
    found = Q()
    if has(name, "partner"):
        found |= Q(partner__name__icontains=value) | Q(partner__vendor_number=value)
    elif has(name, "intervention"):
        found |= Q(intervention__partner__name__icontains=value) | Q(
            intervention__partner__vendor_number=value
        )
    if typed(name):
        for column in spec.partner:
            found |= Q(**{f"{column}__icontains" if column.endswith("name") else column: value})
    else:
        key = catalogue.DOCUMENTS[name].partner[1]
        if key:
            found |= Q(**{f"data__{key}": value})
    return found or Q(pk__in=[])


def _section_q(name: str, spec: RawView, value: str) -> Q:
    if name == "partner_reports":
        return reporting.section_q([value])
    if typed(name) and spec.section:
        return Q(**{spec.section: value})
    if has(name, "intervention"):
        return Q(intervention__section_names__contains=[value])
    return Q(pk__in=[])


def _search_q(name: str, spec: RawView, value: str) -> Q:
    found = Q()
    if rest(name):
        found = Q(reference__icontains=value) | Q(record_key__icontains=value)
        return found if name in TITLE_NOT_SEARCHED else found | Q(title__icontains=value)
    if typed(name):
        for column in spec.search:
            found |= Q(**{f"{column}__icontains": value})
        return found or Q(pk__in=[])
    found = Q(record_key__icontains=value)
    if name not in TITLE_NOT_SEARCHED:
        found |= Q(title__icontains=value)
    return found


def _record_q(name: str, value: str) -> Q:
    if rest(name):
        return Q(record_key=value) | Q(source_id=value)
    number = int(value) if value.isdigit() and len(value) < 19 else None
    if typed(name):
        return Q(datamart_id=number) | Q(source_id=number) if number is not None else Q(pk__in=[])
    return Q(record_key=value) | (Q(source_id=number) if number is not None else Q())


def filters_shown(name: str) -> dict[str, bool]:
    """Which filters a source's form shows: those it has what they need for."""
    spec = view(name)
    if rest(name):
        return {
            "pd": name in REST_PD,
            "partner": name in REST_PARTNERS or name in REST_PARTNER_KEYS,
            "section": False,
            "status": False,
            "dates": True,
            "search": True,
            "report_type": False,
            "pd_link": False,
            "fr_link": False,
            "active": False,
        }
    return {
        "pd": bool(spec.pd)
        or has(name, "intervention")
        or (not typed(name) and any(catalogue.DOCUMENTS[name].intervention)),
        "partner": has(name, "partner")
        or has(name, "intervention")
        or bool(spec.partner)
        or (not typed(name) and bool(catalogue.DOCUMENTS[name].partner[1])),
        "section": name == "partner_reports" or bool(spec.section) or has(name, "intervention"),
        "status": bool(status_field(name)),
        "dates": bool(date_column(name)),
        "search": bool(spec.search) or not typed(name),
        "report_type": name == "partner_reports",
        "pd_link": name == "partner_reports",
        "fr_link": name in FR_SOURCES,
        "active": name == "pd_indicators",
    }


CHOICES_TTL = 10 * 60


def choices(name: str, run: SyncRun | None) -> dict[str, list[str]]:
    """The status and section values a source's filters offer, kept :data:`CHOICES_TTL` seconds per source and
    run (one read of the shared cache). A value the people rule would change (a name, an e-mail address or a
    phone where a status or a section is) is never offered."""
    from neurodb.core import compute
    from neurodb.partnerships.models import PCA
    from neurodb.watch import people

    def read() -> dict[str, list[str]]:
        known = people.known_names()

        def offered(values: Any, key: str) -> list[str]:
            found = {str(v) for v in values if v not in (None, "")}
            kept = (v for v in found if raw_people.text(v, name, key, known, frozenset()) == v)
            return sorted(kept, key=str.lower)

        rows = base(name).order_by()
        status = status_field(name)
        found: dict[str, list[str]] = {"status": [], "section": [], "report_type": []}
        if status:
            column = (
                rows.values_list(status, flat=True)
                if typed(name)
                else rows.values_list(KT(f"data__{status}"))
            )
            values = [v[0] if isinstance(v, tuple) else v for v in column.distinct()[:200]]
            found["status"] = offered(values, status)
        if name == "pd_indicators":
            found["section"] = offered(rows.values_list("section_name", flat=True).distinct(), "section_name")
        elif filters_shown(name)["section"]:
            names = PCA.objects.order_by().exclude(section_names=None).values_list("section_names", flat=True)
            found["section"] = offered((s for row in names.distinct() for s in row or []), "section")
        if name == "partner_reports":
            found["report_type"] = offered(
                rows.values_list("report_type", flat=True).distinct(), "report_type"
            )
        return found

    key = f"raw:choices:{name}:{run.pk if run else '-'}"
    return compute.once(key, read, CHOICES_TTL, lang_neutral=True)


# ------------------------------------------------------------------------------------------------- the list
PAGE_SIZE = 50


def columns_of(name: str, counted: list[FieldCount]) -> list[tuple[str, str]]:
    """A source's list columns: its view's, else the generic ones, then the reviewed fields most often carried
    (never one that names people or that a person wrote)."""
    spec = view(name)
    out = list(spec.columns) or [
        ("Record id", "id"),
        ("Title", "title"),
        ("Date", "date"),
        ("Partner in NeuroDB", "partner"),
        ("PD in NeuroDB", "pd"),
    ]
    taken = {what[2:] for _, what in out if what.startswith(("d:", "t:"))}
    extra = [
        f.key
        for f in counted
        if f.kind in ("read", "plain", "cleaned")
        and f.key not in taken
        and f.key not in dmq.NOISE | {"source_id"}
    ]
    return out + [(key, f"d:{key}") for key in extra[: spec.payload_fields]]


def related(name: str, rows: QuerySet) -> QuerySet:
    """``rows`` with the partner and programme document they link to, and the programme document's partner
    (:func:`partner_of` reads it when a record names no partner), in the same query."""
    joins = [f for f in ("partner", "intervention") if has(name, f)]
    if "intervention" in joins:
        joins.append("intervention__partner")
    return rows.select_related(*joins) if joins else rows


def pd_words(row: Any, funds: FundsNumbers | None = None) -> str:
    """A record's programme document in NeuroDB: its number, else why none ("" for an eTools REST record that
    is not a programme document's; :func:`link_rest` gives the others theirs). A funds reservation line or
    header is read by the rule the sync links it by (``funds``: one for a page or a download): "several PDs
    fit" when its number fits several (the sync then links none), "not in NeuroDB" when its number, or with no
    number its eTools id, names none NeuroDB holds."""
    if isinstance(row, RestRecord) and row.entity not in REST_PCA:
        return ""
    pd = getattr(row, "intervention", None)
    if pd is not None:
        return pd.number or f"PD {pd.pk}"
    if isinstance(row, RestRecord):
        return "not in NeuroDB" if row.source_id else "names none"
    if isinstance(row, FundsReservation | FundsReservationHeader):
        funds = funds or FundsNumbers()
        if number := funds.number_of(row):
            return FR_SEVERAL if len(funds.fit(number)) > 1 else "not in NeuroDB"
        data = row.data if isinstance(row.data, dict) else {}
        return "not in NeuroDB" if data.get("source_intervention_id") not in (None, "") else "names none"
    if isinstance(row, DatamartDocument) and row.dataset == "prp_progress_reports":
        return "not linked"  # through PRP's programme documents (datamart_sync.link_progress_reports)
    data = getattr(row, "data", None) or {}
    named = (getattr(row, "pd_reference_number", "") or "") or data.get("etools_intervention_id") not in (
        None,
        "",
    )
    return "not in NeuroDB" if named else "names none"


def pd_link(row: Any) -> str:
    """A partner report row's programme document link, in the words of the filter (:data:`PD_LINK_WORDS`)."""
    pd = getattr(row, "intervention", None)
    if pd is None:
        data = row.data if isinstance(row.data, dict) else {}
        named = bool(row.pd_reference_number) or data.get("etools_intervention_id") not in (None, "")
        return PD_LINK_WORDS["not_held" if named else "no_pd"]
    if pd.status in pd_statuses.RUNNING:
        return PD_LINK_WORDS["running"]
    if pd.status in pd_statuses.NOT_A_PARTNERSHIP:
        return PD_LINK_WORDS["draft_or_cancelled"]
    return PD_LINK_WORDS["other_status"]


def partner_of(row: Any) -> Any:
    partner = getattr(row, "partner", None)
    if partner is None and getattr(row, "intervention", None) is not None:
        partner = row.intervention.partner if row.intervention.partner_id else None
    return partner


# What NeuroDB holds under an eTools REST record's eTools ids: a programme document's record is the programme
# document of its id (its partner, the programme document's); a partner's record, the partner of its id; an
# agreement's, an engagement's or an action point's, the partner whose id it names (REST_PARTNER_KEYS)
REST_PCA = frozenset({"rest_interventions", "rest_intervention_details"})


def rest_linked(name: str) -> tuple[bool, bool]:
    """Whether the records of an eTools REST entity link to a programme document, and to a partner."""
    return name in REST_PCA, name in REST_PCA or name in REST_PARTNERS or name in REST_PARTNER_KEYS


def _named_id(data: Any, key: str) -> str:
    """The eTools id a record names under ``key`` ("partner__id": the id of its partner object)."""
    from neurodb.integrations.etools.datamart_sync import id_value  # lazy: the Datamart sync

    for part in key.split("__"):
        data = data.get(part) if isinstance(data, dict) else None
    return id_value(data)


def link_rest(name: str, rows: list[Any]) -> list[Any]:
    """``rows`` (records of the eTools REST entity ``name``), each given the programme document and the
    partner NeuroDB holds under the eTools ids it carries, as ``intervention`` and ``partner``: what
    :func:`pd_words`, :func:`partner_of` and a CSV's row read on the records of a Datamart dataset. One query
    for them all."""
    if name in REST_PCA:
        ids = {row.source_id for row in rows if row.source_id}
        held = PCA.objects.select_related("partner").filter(etl_id__in=ids) if ids else []
        found = {pd.etl_id: pd for pd in held}
        for row in rows:
            row.intervention = found.get(row.source_id) if row.source_id else None
        return rows
    if name in REST_PARTNERS:
        keys = [row.source_id for row in rows]
    elif name in REST_PARTNER_KEYS:
        keys = [_named_id(row.data, REST_PARTNER_KEYS[name]) for row in rows]
    else:
        return rows
    ids = {key for key in keys if key}
    found = {p.etl_id: p for p in PartnerOrganization.objects.filter(etl_id__in=ids)} if ids else {}
    for row, key in zip(rows, keys, strict=True):
        row.partner = found.get(key) if key else None
    return rows


def report_keys(row: Any, names: frozenset[str], own: frozenset[str]) -> dict[str, str]:
    """A partner report row's progress report and indicator report keys (``reached``: every page groups its
    rows by them), each cleaned as a text of the record would be: the same key, unless a name, an e-mail
    address or a phone was copied into it."""
    from . import reached as rc

    values = {f: getattr(row, f) for f in rc.ROW_FIELDS if hasattr(row, f)}
    found = {
        "progress_report_key": rc.report_key(values),
        "indicator_report_key": rc.indicator_report_key(values),
    }
    return {k: raw_people.text(v, "partner_reports", k, names, own) for k, v in found.items()}


def record_id(name: str, row: Any) -> str:
    return str(row.datamart_id) if typed(name) else row.record_key


def updated_by_last(row: Any, run: SyncRun | None) -> bool:
    if run is None or run.finished_at is None or row.synced_at is None:
        return False
    return run.started_at <= row.synced_at <= run.finished_at


def local_day(moment: datetime.datetime | None) -> str:
    """A moment's day in Beirut, in words: 24 Oct 2026."""
    if moment is None:
        return ""
    day = timezone.localtime(moment).date()
    return f"{day.day} {day:%b %Y}"


def between(first: datetime.datetime | None, last: datetime.datetime | None) -> str:
    """Two moments as days in words: "between 20 Sep and 24 Oct 2026", "on 24 Oct 2026"."""
    a, b = local_day(first), local_day(last)
    if a == b:
        return f"on {b}"
    if a.endswith(b[-4:]):
        a = a[:-5]
    return f"between {a} and {b}"


# a partner report row eTools no longer sends, on its record page after "Updated by: an earlier sync (…);"
NOT_SENT_SINCE = "eTools no longer sends it since {since}: kept and counted"


def updated_words(row: Any, run: SyncRun | None) -> str:
    """The list's "Updated by the last sync": yes or no; for a partner report row eTools no longer sends,
    since when, and when it was last sent (its ``synced_at``: marking a row never moves it)."""
    since = getattr(row, "not_sent_since", None)
    if since is not None:
        return f"No: no longer sent since {local_day(since)} (last sent {local_day(row.synced_at)})"
    return "yes" if updated_by_last(row, run) else "no"


REPORT_COLUMNS = ("report_type", "report_number")  # the list's "Report" column


def cells(
    name: str,
    row: Any,
    columns: list[tuple[str, str]],
    names: frozenset[str],
    reviewed: frozenset[str],
    run: SyncRun | None,
    funds: FundsNumbers | None = None,
) -> list[Any]:
    """A record's cells for the list, cleaned with only the fields shown (``raw_people``); ``funds``: the
    funds reservations' numbers, one for the page (:func:`pd_words`)."""
    fields = [what[2:] for _, what in columns if what.startswith("d:")]
    if not typed(name) and any(what == "report" for _, what in columns):
        fields += list(REPORT_COLUMNS)
    shown, withheld = raw_people.public_record(row.data, name, names, fields=fields, reviewed=reviewed)
    own = dmq._own_names(catalogue.scrub(row.data if isinstance(row.data, dict) else {}))
    out: list[Any] = []
    for _, what in columns:
        if what == "id":
            out.append(record_id(name, row))
        elif what == "pd":
            out.append(pd_words(row, funds))
        elif what == "partner":
            partner = partner_of(row)
            out.append(partner.name if partner is not None else "—")
        elif what == "title":
            column = view(name).title if typed(name) else "title"
            value = getattr(row, column, "") if column else ""
            out.append(
                raw_people.column(name, "title", value, names, own) if isinstance(value, str) else value
            )
        elif what == "date":
            out.append(getattr(row, date_column(name), None) if date_column(name) else None)
        elif what == "report":
            if typed(name):
                found = (raw_people.column(name, f, getattr(row, f), names, own) for f in REPORT_COLUMNS)
                out.append(" ".join(x for x in found if x))
            else:
                out.append(" ".join(str(shown.get(k) or "") for k in REPORT_COLUMNS).strip())
        elif what == "updated":
            out.append(updated_words(row, run))
        elif what.startswith("t:"):
            out.append(raw_people.column(name, what[2:], getattr(row, what[2:], None), names, own))
        elif what.startswith("d:"):
            key = what[2:]
            if key in shown:
                out.append(_shown(shown[key]))
            elif key in withheld:
                out.append(withheld[key].words)
            else:
                out.append("")
    return out


def _shown(value: Any) -> Any:
    """A value as a cell: nested objects and lists as compact JSON."""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return value


# ----------------------------------------------------------------------------------------------- downloads
LONG_TEXTS = frozenset(
    {"partner_reports", "field_monitoring", "action_points", "audit_findings", "staff_trips"}
)
CSV_LIMIT = {"long_texts": 25_000, "other": 100_000}
COUNT_TIMEOUT_MS = 15_000  # the filter's count on a page or before a download
STREAM_TIMEOUT_MS = 120_000  # each query of a download
CSV_CHUNK = 2000  # records read at a time by a download
HEADER_RECORDS = 1000  # the records of the filter whose fields make a download's header
RAW_CSV_LOCK = 7_150_000  # the first of the download slots' advisory locks (settings.RAW_CSV_MAX_RUNNING)
CHOOSE = "a year, a section, a partner or a programme document"
TOO_SLOW = (
    "This filter takes too long on {n} records: add a date range or a programme document NeuroDB holds."
)
BUSY = "Another large download is being prepared: try again in a minute."
# the last row of a download a query cut (in its first column, figures_as_of): the file is not complete
STOPPED = "The download stopped here: the database took too long. Download again or narrow the filter."


def csv_limit(name: str) -> int:
    """The records a download of a source may hold: fewer for the sources with long texts, which take longer
    to clean (``raw_people``)."""
    return CSV_LIMIT["long_texts" if name in LONG_TEXTS else "other"]


def too_many(name: str, n: int) -> str:
    from neurodb.reports import exports

    return exports.too_many_words(n, csv_limit(name), "records", CHOOSE)


@contextlib.contextmanager
def snapshot(timeout_ms: int, read_only: bool = True) -> Iterator[None]:
    """One state of the data for what runs inside: a transaction ``REPEATABLE READ READ ONLY`` (when it is the
    outermost one) whose statements stop after ``timeout_ms``."""
    outer = not connection.in_atomic_block
    with transaction.atomic():
        with connection.cursor() as cursor:
            if outer and read_only and connection.vendor == "postgresql":
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if connection.vendor == "postgresql":
                if not outer:  # inside another transaction: put it back after
                    cursor.execute("SHOW statement_timeout")
                    before = cursor.fetchone()[0]
                cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        try:
            yield
        finally:
            if not outer and connection.vendor == "postgresql":
                with contextlib.suppress(DatabaseError), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('statement_timeout', %s, true)", [before])


def counted(name: str, rows: QuerySet) -> int | None:
    """The records of a filter, counted under :data:`COUNT_TIMEOUT_MS`; None when that takes too long."""
    try:
        with snapshot(COUNT_TIMEOUT_MS):
            return rows.order_by().count()
    except DatabaseError:
        logger.warning("raw eTools data: the count of %s took too long", name)
        return None


class Slot:
    """A download slot: an advisory lock of this connection, freed once, when the download ends or its
    response is closed (a browser that stops reading, a response never read)."""

    def __init__(self, index: int) -> None:
        self.index, self.held = index, True

    def free(self) -> None:
        if not self.held:
            return
        self.held = False
        if connection.vendor != "postgresql":
            return
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [RAW_CSV_LOCK + self.index])
        except DatabaseError:  # a broken connection: the lock went with its session
            logger.warning("raw eTools data: could not free a download slot", exc_info=True)


def take_slot() -> Slot | None:
    """A download slot, or None when every one is taken (``settings.RAW_CSV_MAX_RUNNING`` on the whole
    site)."""
    if connection.vendor != "postgresql":
        return Slot(0)
    with connection.cursor() as cursor:
        for i in range(max(int(settings.RAW_CSV_MAX_RUNNING), 1)):
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [RAW_CSV_LOCK + i])
            if cursor.fetchone()[0]:
                return Slot(i)
    return None


def local_moment(moment: datetime.datetime | None) -> str:
    return timezone.localtime(moment).strftime("%Y-%m-%d %H:%M") if moment else ""


NEURODB_COLUMNS = (
    "figures_as_of",
    "record_id",
    "synced_at",
    "updated_by_last_sync",
    "pd_number_in_neurodb",
    "pd_status_in_neurodb",
    "partner_in_neurodb",
    "vendor_number",
)
PARTNER_REPORT_COLUMNS = ("pd_link", "progress_report_key", "indicator_report_key", "no_longer_sent_since")
TAIL_COLUMNS = ("people_inside", "fields_not_shown", "other_fields")


def header_keys(name: str, rows: QuerySet, counted_fields: list[FieldCount]) -> list[str]:
    """The record fields a download writes, in the fields table's order: those counted (§ fields this source
    sends) and those of the first :data:`HEADER_RECORDS` records of the filter (one query), the field names
    that are not identifiers or that name a person left out."""
    from neurodb.watch import people

    keys = [f.key for f in counted_fields]
    table = connection.ops.quote_name(rows.model._meta.db_table)
    first = rows.order_by("-pk").values("pk")[:HEADER_RECORDS]
    sql, params = first.query.sql_with_params()
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT DISTINCT jsonb_object_keys(data) FROM {table} "  # noqa: S608 - the model's table
            f"WHERE id IN ({sql}) AND jsonb_typeof(data) = 'object'",
            params,
        )
        more = sorted(row[0] for row in cursor.fetchall())
    rules = dmq.Rules(people.known_names(), name)
    for key in more:
        if key in keys or dmq._contact_key(key):
            continue
        if name in catalogue.SAFEGUARDING and key not in catalogue.SAFEGUARDING_KEYS:
            continue
        if raw_people.identifier(key, rules.fm) and not dmq._key_withheld(key, rules, frozenset()):
            keys.append(key)
    return keys


def header_name(key: str) -> str:
    """A record field as a download's column: "<field> (people)" for a field that names people, "<field> (text
    withheld)" for one a person wrote, "<field> (as received)" for one named as a column of NeuroDB's own
    (a record's ``vendor_number`` beside the vendor number of the partner NeuroDB links it to)."""
    if raw_people.names_people(key):
        return f"{key} (text withheld)" if raw_people.written(key) else f"{key} (people)"
    if key in NEURODB_COLUMNS or key in PARTNER_REPORT_COLUMNS or key in TAIL_COLUMNS:
        return f"{key} (as received)"
    return key


def csv_columns(name: str, keys: list[str]) -> list[str]:
    head = list(NEURODB_COLUMNS) + (list(PARTNER_REPORT_COLUMNS) if name == "partner_reports" else [])
    if name in catalogue.SAFEGUARDING:
        return head + [k for k in keys if k in catalogue.SAFEGUARDING_KEYS] + [raw_people.WITHHELD_KEYS]
    return head + [header_name(k) for k in keys] + list(TAIL_COLUMNS)


def csv_record(
    name: str,
    row: Any,
    wanted: set[str],
    run: SyncRun | None,
    names: frozenset[str],
    reviewed: frozenset[str],
    as_of: str,
    funds: FundsNumbers | None = None,
) -> dict[str, Any]:
    """One record as a download's row, through the people rule: NeuroDB's columns, then the fields of
    ``wanted`` (the header's), then the tail. The figure trail's CSVs write their records with it too.
    ``funds``: the funds reservations' numbers, one for the download (:func:`pd_words`)."""
    shown, withheld = raw_people.public_record(row.data, name, names, reviewed=reviewed)
    pd = getattr(row, "intervention", None)
    partner = partner_of(row)
    out: dict[str, Any] = {
        "figures_as_of": as_of,
        "record_id": record_id(name, row),
        "synced_at": local_moment(row.synced_at),
        "updated_by_last_sync": "yes" if updated_by_last(row, run) else "no",
        "pd_number_in_neurodb": pd.number if pd is not None else pd_words(row, funds),
        "pd_status_in_neurodb": pd.status if pd is not None else "",
        "partner_in_neurodb": partner.name if partner is not None else "",
        "vendor_number": partner.vendor_number if partner is not None else "",
    }
    if name == "partner_reports":
        own = dmq._own_names(catalogue.scrub(row.data if isinstance(row.data, dict) else {}))
        since = row.not_sent_since
        out.update(
            pd_link=pd_link(row),
            **report_keys(row, names, own),
            no_longer_sent_since=timezone.localtime(since).date().isoformat() if since else "",
        )
    other: dict[str, Any] = {}
    for key, value in shown.items():
        if key in wanted:
            out[header_name(key)] = _shown(value)
        else:
            other[key] = value
    for key, found in withheld.items():
        if key in wanted:
            out[header_name(key)] = found.cell
        elif key == raw_people.WITHHELD_KEYS:
            out[key] = found.count
        elif not found.inside and key != raw_people.FIELDS_NAMING:
            other[key] = found.cell
    if name in catalogue.SAFEGUARDING:
        out.setdefault(raw_people.WITHHELD_KEYS, 0)
    else:
        out["people_inside"] = raw_people.people_inside(withheld)
        named = withheld.get(raw_people.FIELDS_NAMING)
        out["fields_not_shown"] = named.count if named else 0
        out["other_fields"] = _shown(other) if other else ""
    return out


def csv_rows(
    name: str, rows: QuerySet, keys: list[str], run: SyncRun | None, names: frozenset[str]
) -> Iterator[dict[str, Any]]:
    """The download's rows, read :data:`CSV_CHUNK` records at a time inside one snapshot, each through the
    people rule. A query that takes longer than :data:`STREAM_TIMEOUT_MS` ends the file where it is, with one
    last row saying so (:data:`STOPPED`)."""
    reviewed = reviewed_keys(name)
    as_of = local_moment(run.finished_at if run else None)
    wanted = set(keys)
    funds = FundsNumbers() if name in FR_SOURCES else None
    try:
        with snapshot(STREAM_TIMEOUT_MS):
            found = related(name, rows).order_by("-pk").iterator(chunk_size=CSV_CHUNK)
            if rest(name):  # what NeuroDB holds under their eTools ids: one query a chunk
                chunks = itertools.batched(found, CSV_CHUNK)
                found = (row for chunk in chunks for row in link_rest(name, list(chunk)))
            for row in found:
                yield csv_record(name, row, wanted, run, names, reviewed, as_of, funds)
    except DatabaseError:
        logger.warning("raw eTools data: the download of %s stopped (took too long)", name, exc_info=True)
        yield {"figures_as_of": STOPPED}  # after the snapshot: the cut is in the file, never a complete one


def filename(name: str, run: SyncRun | None) -> str:
    """``raw_<source>_<YYYY-MM-DD>_<HHMM>.csv``, the moment being the end of the source's last sync."""
    from neurodb.reports import exports

    return exports.export_filename(f"raw_{name}", "csv", moment=run.finished_at if run else None)


# ------------------------------------------------------------------------------------- the index's counts
def held_counts(runs: dict[str, tuple[SyncRun | None, SyncRun | None]]) -> dict[str, list[int]]:
    """For every source: [in NeuroDB, updated by its last sync, updated since], counted by one grouped query
    per table (the records kept whole together), stored for every worker an hour, keyed on the latest runs of
    the jobs that write these tables (as Data health's row counts)."""
    from neurodb.core import compute
    from neurodb.reports.services import DATASET_ROWS_TTL, _dataset_rows_key

    def count() -> dict[str, list[int]]:
        found: dict[str, list[int]] = {}
        documents = [name for name in runs if not typed(name)]
        for name in runs:
            if typed(name):
                run = runs[name][0]
                found[name] = list(_counts(base(name), run, name))
        if documents:
            table = connection.ops.quote_name(DatamartDocument._meta.db_table)
            windows = [
                (name, run.started_at if run else None, run.finished_at if run else None)
                for name, (run, _) in runs.items()
                if not typed(name)
            ]
            values = ", ".join(["(%s, %s::timestamptz, %s::timestamptz)"] * len(windows))
            params = [x for window in windows for x in window]
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT d.dataset, count(*), "  # noqa: S608 - the model's table
                    "count(*) FILTER (WHERE d.synced_at >= w.s AND d.synced_at <= w.e), "
                    "count(*) FILTER (WHERE d.synced_at > w.e) "
                    f"FROM {table} d JOIN (VALUES {values}) AS w(dataset, s, e) ON d.dataset = w.dataset "
                    "GROUP BY d.dataset",
                    params,
                )
                for dataset, held, by_sync, since in cursor.fetchall():
                    found[dataset] = [held, by_sync, since]
            for name in documents:
                found.setdefault(name, [0, 0, 0])
        return found

    stamp = ";".join(f"{name}={run.pk if run else '-'}" for name, (run, _) in sorted(runs.items()))
    key = f"raw:held:{_dataset_rows_key()}:{compute.fingerprint(stamp)}"
    return compute.once(key, count, DATASET_ROWS_TTL, lang_neutral=True)


def rewritten_by(name: str) -> tuple[str, ...]:
    """The datasets whose runs save the records of ``name`` again right after its own, in the same sync: the
    engagement detail datasets (``datamart_sync.ENRICHMENTS``: audits, spot checks, micro-assessments...) save
    every engagement they match, so the rows ``engagements`` wrote then read as written by them."""
    from neurodb.integrations.etools import datamart_sync

    return tuple(datamart_sync.ENRICHMENTS) if name == "engagements" else ()


def written_until(name: str, run: SyncRun) -> datetime.datetime | None:
    """The end of what ``run`` of ``name`` wrote: its own end, or, for a dataset whose records the runs of
    others save again right after it (:func:`rewritten_by`), the end of the first finished run of each of
    those that started after it, before the next run of ``name`` (as ``trail_monitoring.pd_writers`` counts
    the programme documents their budgets save again). One query."""
    after = rewritten_by(name)
    if run.finished_at is None or not after:
        return run.finished_at
    ends, end = set(after), run.finished_at
    later = (
        SyncRun.objects.filter(job=run.job, target__in=(name, *after), started_at__gte=run.started_at)
        .exclude(pk=run.pk)
        .order_by("started_at", "pk")
        .values_list("target", "finished_at")[:100]
    )
    for target, finished in later:
        if target == name:  # the next run of the dataset: what follows is its own
            break
        if target in ends and finished is not None:
            ends.discard(target)
            end = max(end, finished)
    return end


def _counts(rows: QuerySet, run: SyncRun | None, name: str = "") -> tuple[int, int, int]:
    """[in NeuroDB, written by ``run`` (from its start to :func:`written_until`), written since]."""
    from django.db.models import Count

    rows = rows.order_by()
    if run is None or run.finished_at is None:
        return rows.count(), 0, 0
    until = written_until(name, run) if name else run.finished_at
    found = rows.aggregate(
        held=Count("pk"),
        by_sync=Count("pk", filter=Q(synced_at__gte=run.started_at, synced_at__lte=until)),
        since=Count("pk", filter=Q(synced_at__gt=until)),
    )
    return found["held"], found["by_sync"], found["since"]


def rest_counts(runs: dict[str, tuple[SyncRun | None, SyncRun | None]]) -> dict[str, list[int]]:
    """For every eTools REST entity of ``runs``: [in NeuroDB, updated by its last sync, updated since], in one
    grouped query (the copies are small: no cache)."""
    windows = [
        (name, run.started_at if run else None, run.finished_at if run else None)
        for name, (run, _) in runs.items()
    ]
    found = {name: [0, 0, 0] for name in runs}
    if not windows:
        return found
    table = connection.ops.quote_name(RestRecord._meta.db_table)
    values = ", ".join(["(%s, %s::timestamptz, %s::timestamptz)"] * len(windows))
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT d.entity, count(*), "  # noqa: S608 - the model's table
            "count(*) FILTER (WHERE d.synced_at >= w.s AND d.synced_at <= w.e), "
            "count(*) FILTER (WHERE d.synced_at > w.e) "
            f"FROM {table} d JOIN (VALUES {values}) AS w(entity, s, e) ON d.entity = w.entity "
            "GROUP BY d.entity",
            [x for window in windows for x in window],
        )
        for entity, held, by_sync, since in cursor.fetchall():
            found[entity] = [held, by_sync, since]
    return found


# ------------------------------------------------------------------------- partner reports no longer sent
REVIEW_REPORTS = 50  # the reports the review list shows, then "and N more"


def no_longer_sent(rows: QuerySet | None = None) -> dict[str, Any]:
    """The partner report rows eTools no longer sends (marked by a whole read, kept and counted) of ``rows``
    (every row by default): ``rows``, of how many progress reports (``reporting.REPORT_KEY``), when they were
    last sent (``first``, ``last``: their ``synced_at``), the ``newest`` mark, and of those reports the ones
    eTools sends again for another period (``other_period``, ``reporting.sent_for_another_period``). Counts
    only: one query, and a second when some are marked."""
    from django.db.models import Count, Max, Min

    marked = (rows if rows is not None else ReportedIndicator.objects.all()).exclude(not_sent_since=None)
    found = marked.order_by().aggregate(
        rows=Count("pk"),
        reports=Count(reporting.REPORT_KEY, distinct=True),
        first=Min("synced_at"),
        last=Max("synced_at"),
        newest=Max("not_sent_since"),
    )
    found["other_period"] = 0
    if found["rows"]:
        found["other_period"] = (
            reporting.sent_for_another_period(marked)
            .order_by()
            .aggregate(n=Count(reporting.REPORT_KEY, distinct=True))["n"]
        )
    return found


def rows_key(pks: Iterable[int]) -> str:
    """The rows a reviewer saw, as the button posts them and the removal checks them
    (``backfill.remove_unsent``): a sha256 of their sorted pks, so the same count of other rows never passes
    for them."""
    return hashlib.sha256(",".join(str(pk) for pk in sorted(pks)).encode()).hexdigest()


def rows_of_reports(rows: int, reports: int) -> str:
    """ "312 rows of 14 progress reports", "1 row of 1 progress report"."""
    return (
        f"{rows:,} {'row' if rows == 1 else 'rows'} of {reports:,} "
        f"{'progress report' if reports == 1 else 'progress reports'}"
    )


def unsent_words(rows: int, reports: int, first: Any, last: Any, other: int) -> str:
    """The source page's sentence on the partner report rows eTools no longer sends ("" when none)."""
    if not rows:
        return ""
    said = (
        f"{rows_of_reports(rows, reports)} eTools no longer sends {'is' if rows == 1 else 'are'} kept and "
        f"counted, marked no longer sent (last sent {between(first, last)})."
    )
    if other:
        said += (
            f" eTools sends the number of {other:,} of these reports again for another period: review those "
            "first."
        )
    return said


# The vendor check of the funds reservation lines and headers, per link kind (datamart_sync.FR_VENDOR_KINDS)
FR_VENDOR_WORDS = {
    "exact": "by exact number",
    "amended": "by another amendment's",
    "pd_part": "by the PD part",
    "header": "by the header's number",
    "by_id": "by eTools id",
}


def fr_link_words(found: dict[str, int], name: str) -> str:
    """The source page's sentence on how the last sync linked the funds reservation lines (or headers) to
    their programme document (``details.pd_link``: ``datamart_sync.Links.by_number_first``); "" for a sync
    before the rule, which counted none."""
    if not found:
        return ""

    def n(key: str) -> str:
        return f"{found.get(key, 0):,}"

    lines = name == "funds_reservations"
    noun = "lines" if lines else "funds reservations"
    ways = [
        f"exact {n('exact')}",
        f"another amendment's number {n('amended')}",
        f"the PD part alone {n('pd_part')}",
    ]
    if lines:
        ways.append(f"the FR's header number {n('header')}")
    said = (
        f"Linked to their programme document by reference number: {n('by_number')} ({'; '.join(ways)}). "
        f"Of these, {n('twice')} carry a number several PDs share and an eTools id that names none of them: "
        "each went to the PD whose dates cover it, else to the one that ends last. By eTools id only (no "
        f"number): {n('by_id')}."
    )
    gone = f"a number of a PD NeuroDB does not hold {n('number_not_held')}"
    if found.get("number_not_held"):
        gone += f" (the eTools id names a PD NeuroDB holds on {n('number_not_held_id_held')} of them)"
    gone += f"; a number several PDs fit (another amendment's or the PD part alone) {n('unclear')}"
    if found.get("id_not_held"):
        gone += f"; no number, and an eTools id of a PD NeuroDB does not hold {n('id_not_held')}"
    said += (
        f" Not linked: {gone}. The eTools id names another PD NeuroDB holds on {n('id_other')} of the {noun} "
        f"linked by number (the old rule put them there); moved to another PD by this sync: {n('moved')}."
    )
    kinds = [kind for kind in FR_VENDOR_WORDS if found.get(f"vendor_compared_{kind}")]
    vendor = [
        f"{n(f'vendor_same_{kind}')} of {n(f'vendor_compared_{kind}')} "
        f"{f'{noun} linked ' if i == 0 else ''}{FR_VENDOR_WORDS[kind]}"
        for i, kind in enumerate(kinds)
    ]
    if vendor:
        said += f" The partner's vendor number matches on {', '.join(vendor)}."
    return said


def _n(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def inactive_words(found: dict[str, int] | None) -> str:
    """The PD indicators page's sentence on the indicators eTools marks inactive in every row, as the last
    proof counted them (``identity.inactive_indicators``); "" before a sync counted them, or when there are
    none."""
    if not found or not found.get("indicators"):
        return ""
    without = found.get("without_rows", 0)
    return (
        f"eTools marks {_n(found['indicators'], 'indicator', 'indicators')} of "
        f"{_n(found.get('pds', 0), 'programme document', 'programme documents')} inactive in every row; "
        f"{without:,} of them {'has' if without == 1 else 'have'} no partner report row and "
        f"{'is' if without == 1 else 'are'} still listed on Programmes → Indicators and Partner monitoring."
    )


def budget_link_words(found: Any) -> str:
    """The programme document budgets page's sentence on the budget records of the last sync
    (``details.budget_link``, ``datamart_sync.Links.budget``): those whose number names another programme
    document than their eTools id (linked by the id, as always), and the programme documents several records
    wrote (the last one read is kept). "" for a sync before they were counted."""
    if not isinstance(found, dict) or "records" not in found:
        return ""
    other, several = found.get("number_other", 0), found.get("several", 0)
    names = (
        "names by its number another programme document than its eTools id (NeuroDB links it by the id)"
        if other == 1
        else "name by their number another programme document than their eTools id (NeuroDB links them by "
        "the id)"
    )
    records = _n(found["records"], "budget record", "budget records")
    written = _n(several, "programme document was", "programme documents were")
    return (
        f"Of the {records} of the last sync, {other:,} {names}; {written} written by several budget records "
        "(the last one read is kept)."
    )


# why the data does not prove the link of PRP's progress reports to their programme documents
# (datamart_sync.LINK_WHY, as check 11 says it), in the words of Raw eTools data
PRP_LINK_WHY = {
    "no_link": "nothing in PRP's programme documents matches what the progress reports name",
    "repeated": "what could match them repeats across PRP's programme documents",
    "dates_disagree": "the matches tie reports to programme documents of other dates",
    "not_clear": "two ways of matching them disagree",
    "few": "the best way of matching them ties too few reports",
}


def prp_link_words(verdict: dict[str, Any]) -> str:
    """The PRP progress reports page's sentence on their link to the programme documents, as the last sync
    recorded it (``datamart_sync.recorded_pd_link``): whether the data proves it, how the reports the partner
    reports also have agree, the reports still owed, and whether the pages can show them. Counts only."""
    from neurodb.integrations.etools.datamart_sync import shown_words  # lazy: the Datamart sync

    said = []
    if "passes" in verdict:
        if verdict.get("proven"):
            said.append("Tied to their programme document: yes, the data proves it.")
        else:
            why = PRP_LINK_WHY.get(str(verdict.get("link_why") or ""), "the data does not prove it")
            said.append(f"Tied to their programme document: no, {why} (see check 11).")
        agrees = verdict.get("agrees") or {}
        said.append(
            f"Of the {agrees.get('compared', 0):,} reports also in the partner reports, "
            f"{agrees.get('same', 0):,} end on the same day ({agrees.get('same_day', 0):,} of "
            f"{agrees.get('dated', 0):,} on the same submission day)."
        )
        owed = f"Still owed for a period that has ended: {verdict.get('owed', 0):,}"
        if verdict.get("stale"):
            owed += f" ({verdict['stale']:,} more no longer returned by PRP's reads, not counted)"
        said.append(f"{owed}.")
    said.append(f"Can be shown on the pages: {shown_words(verdict)}.")
    return " ".join(said)


def unsent_reports(rows: QuerySet, names: frozenset[str]) -> tuple[list[dict[str, Any]], int]:
    """The progress reports of the rows of ``rows`` eTools no longer sends, for review before a removal: one
    line each, the last sent first (its programme document's number, the report, its period end, when it was
    last sent, its rows), at most :data:`REVIEW_REPORTS`, and how many more. No person: the texts go through
    the people rule."""
    from django.db.models import Count, Max

    grouped = (
        rows.exclude(not_sent_since=None)
        .annotate(_key=reporting.REPORT_KEY)
        .values("_key")
        .annotate(
            pd=Max("intervention__number"),
            ref=Max("pd_reference_number"),
            kind=Max("report_type"),
            number=Max("report_number"),
            end=Max("period_end"),
            last=Max("synced_at"),
            n=Count("pk"),
        )
        .order_by("-last", "_key")
    )
    total = grouped.count()
    lines = []
    for found in grouped[:REVIEW_REPORTS]:
        report = " ".join(x for x in (found["kind"], found["number"]) if x)
        lines.append(
            {
                "pd": raw_people.text(
                    found["pd"] or found["ref"] or "—",
                    "partner_reports",
                    "pd_reference_number",
                    names,
                    frozenset(),
                ),
                "report": raw_people.text(
                    report or "—", "partner_reports", "report_number", names, frozenset()
                ),
                "period_end": found["end"],
                "last_sent": found["last"],
                "rows": found["n"],
            }
        )
    return lines, max(total - REVIEW_REPORTS, 0)
