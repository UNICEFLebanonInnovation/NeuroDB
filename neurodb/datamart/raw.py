"""Raw eTools data (Admin → Data and sync): every eTools source NeuroDB reads, record by record, as it came,
for the people who check a page's figures against it (the Validators group, superusers). The pages are
``datamart.raw_admin``; this module holds what they read.

* which page reads which source (:data:`PAGE_SOURCES`, :data:`USED_BY`);
* the fields of each source a person reviewed (:data:`REVIEWED_KEYS`, the fields NeuroDB reads, and
  ``RawFieldReview``): any other field shows only numbers, yes/no and dates (``raw_people``);
* the fields each source sends, counted from the newest :data:`FIELDS_COUNTED` records NeuroDB holds;
* how each source is listed and filtered (:data:`VIEWS`, :class:`Filters`);
* its CSV: streamed inside one snapshot of the data, at most :data:`CSV_LIMIT` records, at most
  ``settings.RAW_CSV_MAX_RUNNING`` large downloads at once on the whole site (advisory locks).

People are never shown or written: every record goes through ``raw_people.public_record``.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import Q, QuerySet
from django.db.models.fields.json import KT
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.partnerships import statuses as pd_statuses

from . import catalogue, raw_people, reporting
from . import query as dmq
from .models import DatamartDocument, RawFieldReview

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
# Index table 3: the eTools REST API entities, read into Raw eTools data by a later release
REST_ENTITIES: dict[str, tuple[str, str]] = {
    "rest_partners": ("Partners", "partners/partners/"),
    "rest_partner_details": ("Partner details", "partners/partners/<id>/"),
    "rest_agreements": ("Agreements", "partners/agreements/"),
    "rest_interventions": ("Programme documents", "partners/interventions/"),
    "rest_intervention_details": ("Programme document details", "partners/interventions/<id>/"),
    "rest_travels": ("Trips", "t2f/travels/"),
    "rest_travel_details": ("Trip details", "t2f/travels/<id>/"),
    "rest_engagements": ("Engagements", "audit/engagements/"),
    "rest_engagement_details": ("Engagement details", "audit/engagements/<id>/"),
    "rest_action_points": ("Action points", "action-points/action-points/"),
}

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


def path_of(name: str) -> str:
    """The Datamart path a source is read from."""
    from neurodb.integrations.etools import datamart_sync

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
    return dmq.dataset(name).base


def model_of(name: str):
    return dmq.dataset(name).model


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
    return connection.ops.quote_name(DatamartDocument._meta.db_table), "AND dataset = %s", [name]


def field_counts(name: str, names: frozenset[str] | None = None) -> tuple[list[FieldCount], int]:
    """The top-level fields of the newest :data:`FIELDS_COUNTED` records NeuroDB holds of a source, in one
    query:
    how many carry each, hold a value in it, and hold a text in it; and how many fields are left out because
    their names are not identifiers or name a person (``raw_people``). The answers of PSEA assessments list
    only their safeguarding fields. Most carried first."""
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
        found = cursor.fetchall()
    return _described(name, found, names)


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
            ("Status", "d:status"),
            ("Date", "date"),
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
# A record kept whole whose title can name a person (a trip's purpose, a file's name) is never searched by it
TITLE_NOT_SEARCHED = frozenset({"staff_trips", "attachments"})
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


def view(name: str) -> RawView:
    """How a source is listed and filtered: its own :data:`VIEWS` entry, else one built from its table."""
    if name in VIEWS:
        return VIEWS[name]
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
    """The column a source's year and dates filter reads: its main date (``date`` for a record kept whole)."""
    if not typed(name):
        return "date" if catalogue.DOCUMENTS[name].date else ""
    return view(name).date or catalogue.TYPED[name][2]


def status_field(name: str) -> str:
    """The field the status filter reads: a typed record's status column, or the reviewed status field of a
    record kept whole ("" when it has none)."""
    found = view(name).status_key
    if typed(name) or not found:
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
UPDATED = {"last": "the last sync", "earlier": "an earlier sync"}
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
            updated=_text(params, "updated") if params.get("updated") in UPDATED else "",
            report_type=_text(params, "report_type", 30).upper(),
            pd_link=_text(params, "pd_link") if params.get("pd_link") in PD_LINKS else "",
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
        }
        return {k: v for k, v in values.items() if v}

    def apply(self, name: str, rows: QuerySet, run: SyncRun | None) -> QuerySet:
        """``rows`` narrowed by the filter, as the page states it (a refused search finds nothing)."""
        if self.refused:
            return rows.none()
        spec = view(name)
        if self.pd:
            rows = rows.filter(_pd_q(name, spec, self.pd))
        if self.partner:
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
        if self.updated and run is not None and run.finished_at is not None:
            window = Q(synced_at__gte=run.started_at, synced_at__lte=run.finished_at)
            rows = (
                rows.filter(window) if self.updated == "last" else rows.filter(synced_at__lt=run.started_at)
            )
        if name == "partner_reports":
            if self.report_type:
                rows = rows.filter(report_type=self.report_type)
            if self.pd_link:
                rows = rows.filter(PD_LINKS[self.pd_link])
        return rows


def _pd_q(name: str, spec: RawView, value: str) -> Q:
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
    if typed(name):
        for column in spec.search:
            found |= Q(**{f"{column}__icontains": value})
        return found or Q(pk__in=[])
    found = Q(record_key__icontains=value)
    if name not in TITLE_NOT_SEARCHED:
        found |= Q(title__icontains=value)
    return found


def _record_q(name: str, value: str) -> Q:
    number = int(value) if value.isdigit() and len(value) < 19 else None
    if typed(name):
        return Q(datamart_id=number) | Q(source_id=number) if number is not None else Q(pk__in=[])
    return Q(record_key=value) | (Q(source_id=number) if number is not None else Q())


def filters_shown(name: str) -> dict[str, bool]:
    """Which filters a source's form shows: those it has what they need for."""
    spec = view(name)
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


def pd_words(row: Any) -> str:
    """A record's programme document in NeuroDB: its number, else why none."""
    pd = getattr(row, "intervention", None)
    if pd is not None:
        return pd.number or f"PD {pd.pk}"
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


REPORT_COLUMNS = ("report_type", "report_number")  # the list's "Report" column


def cells(
    name: str,
    row: Any,
    columns: list[tuple[str, str]],
    names: frozenset[str],
    reviewed: frozenset[str],
    run: SyncRun | None,
) -> list[Any]:
    """A record's cells for the list, cleaned with only the fields shown (``raw_people``)."""
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
            out.append(pd_words(row))
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
            out.append("yes" if updated_by_last(row, run) else "no")
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
PARTNER_REPORT_COLUMNS = ("pd_link", "progress_report_key", "indicator_report_key")
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
    withheld)" for one a person wrote."""
    if raw_people.names_people(key):
        return f"{key} (text withheld)" if raw_people.written(key) else f"{key} (people)"
    return key


def csv_columns(name: str, keys: list[str]) -> list[str]:
    head = list(NEURODB_COLUMNS) + (list(PARTNER_REPORT_COLUMNS) if name == "partner_reports" else [])
    if name in catalogue.SAFEGUARDING:
        return head + [k for k in keys if k in catalogue.SAFEGUARDING_KEYS] + [raw_people.WITHHELD_KEYS]
    return head + [header_name(k) for k in keys] + list(TAIL_COLUMNS)


def csv_rows(
    name: str, rows: QuerySet, keys: list[str], run: SyncRun | None, names: frozenset[str]
) -> Iterator[dict[str, Any]]:
    """The download's rows, read :data:`CSV_CHUNK` records at a time inside one snapshot, each through the
    people
    rule. A query that takes longer than :data:`STREAM_TIMEOUT_MS` ends the file where it is."""
    reviewed = reviewed_keys(name)
    as_of = local_moment(run.finished_at if run else None)
    wanted = set(keys)
    try:
        with snapshot(STREAM_TIMEOUT_MS):
            for row in related(name, rows).order_by("-pk").iterator(chunk_size=CSV_CHUNK):
                shown, withheld = raw_people.public_record(row.data, name, names, reviewed=reviewed)
                pd = getattr(row, "intervention", None)
                partner = partner_of(row)
                out: dict[str, Any] = {
                    "figures_as_of": as_of,
                    "record_id": record_id(name, row),
                    "synced_at": local_moment(row.synced_at),
                    "updated_by_last_sync": "yes" if updated_by_last(row, run) else "no",
                    "pd_number_in_neurodb": pd.number if pd is not None else pd_words(row),
                    "pd_status_in_neurodb": pd.status if pd is not None else "",
                    "partner_in_neurodb": partner.name if partner is not None else "",
                    "vendor_number": partner.vendor_number if partner is not None else "",
                }
                if name == "partner_reports":
                    own = dmq._own_names(catalogue.scrub(row.data if isinstance(row.data, dict) else {}))
                    out.update(pd_link=pd_link(row), **report_keys(row, names, own))
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
                yield out
    except DatabaseError:
        logger.warning("raw eTools data: the download of %s stopped (took too long)", name, exc_info=True)


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
                found[name] = list(_counts(base(name), run))
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


def _counts(rows: QuerySet, run: SyncRun | None) -> tuple[int, int, int]:
    from django.db.models import Count

    rows = rows.order_by()
    if run is None or run.finished_at is None:
        return rows.count(), 0, 0
    found = rows.aggregate(
        held=Count("pk"),
        by_sync=Count("pk", filter=Q(synced_at__gte=run.started_at, synced_at__lte=run.finished_at)),
        since=Count("pk", filter=Q(synced_at__gt=run.finished_at)),
    )
    return found["held"], found["by_sync"], found["since"]
