"""eTools Datamart -> NeuroDB, one ``SyncRun`` (job ``etools_datamart``) per dataset.

Two kinds of targets:

* The existing eTools tables the whole site reads (``etools.PartnerOrganization``, ``etools.PCA``,
  ``etools.Agreement``) are upserted by their eTools id (``etl_id`` = the Datamart ``source_id``),
  so the programme, partner and donor pages, the filters and the assistant keep working unchanged
  on Datamart data. Rows are never deleted from them.
* The datasets those tables have no place for (funds reservations, grants, PD indicators,
  assessments, audits and spot checks, action points, TPM visits, field monitoring findings, HACT
  totals) go to the ``datamart`` app, keyed by the Datamart id and linked to the programme
  document and partner. A dataset read to the end replaces its table: rows the Datamart no longer
  returns are deleted.

Each record is written in its own savepoint (``runs.process_items``): one bad record is logged and
counted, the rest are written and the run ends PARTIAL. Every request is limited to one country
(``settings.ETOOLS_DATAMART_COUNTRY``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import connection, models, transaction
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import catalogue
from neurodb.datamart import models as dm
from neurodb.geo.models import Location
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.etools.fields import assign, coerce, parse_iso_date
from neurodb.integrations.http import IntegrationError
from neurodb.integrations.runs import fail, finish_by_counts, new_run, process_items
from neurodb.partnerships.models import PCA, Agreement, PartnerOrganization

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------------- value helpers
def names(value: Any, *keys: str) -> list[str]:
    """Names from a Datamart ``*_data`` value: a list of dicts or strings, a dict, or a
    comma-separated string. ``keys`` are tried in order on dicts (default ``name``)."""
    keys = keys or ("name",)
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, dict):
        value = [value]
    found: list[str] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            text = next((str(item[k]) for k in keys if item.get(k) not in (None, "")), "")
        else:
            text = str(item) if item not in (None, "") else ""
        if text and text not in found:
            found.append(text)
    return found


def person_names(value: Any) -> list[str]:
    """Focal points: ``{"first_name", "last_name"}`` or ``name`` or ``email`` dicts."""
    people = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            full = " ".join(str(item.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
            full = full or str(item.get("name") or item.get("email") or "")
            if full:
                people.append(full)
        elif item:
            people.append(str(item))
    return people


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _count(value: Any) -> int | None:
    """``"3"``, ``"3.0"``, ``3`` -> 3; empty or not a number -> None (HACT history sends strings)."""
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except ArithmeticError:
        return Decimal(0)


# ----------------------------------------------------------------------------------- linking
class Links:
    """Resolves eTools ids, vendor numbers and reference numbers to local rows (cached per run)."""

    def __init__(self) -> None:
        self._partners_by_etl: dict[str, int] | None = None
        self._partners_by_vendor: dict[str, int] | None = None
        self._pcas_by_etl: dict[str, int] | None = None
        self._pcas_by_number: dict[str, int] | None = None
        self._engagements: dict[str, int] | None = None
        self.scope: Any = None  # the CountryScope of the run, once a catalogue dataset needs it
        self.missing: dict[str, int] = defaultdict(int)

    def refresh(self) -> None:
        self._partners_by_etl = self._partners_by_vendor = None
        self._pcas_by_etl = self._pcas_by_number = None
        self._engagements = None

    def _load_partners(self) -> None:
        self._partners_by_etl, self._partners_by_vendor = {}, {}
        for pk, etl_id, vendor in PartnerOrganization.objects.values_list("pk", "etl_id", "vendor_number"):
            self._partners_by_etl[str(etl_id)] = pk
            if vendor:
                self._partners_by_vendor.setdefault(vendor.strip(), pk)

    def _load_pcas(self) -> None:
        self._pcas_by_etl, self._pcas_by_number = {}, {}
        for pk, etl_id, number in PCA.objects.values_list("pk", "etl_id", "number"):
            self._pcas_by_etl[str(etl_id)] = pk
            if number:
                self._pcas_by_number.setdefault(number.strip(), pk)

    def partner(self, source_id: Any = None, vendor_number: Any = None) -> int | None:
        if self._partners_by_etl is None:
            self._load_partners()
        pk = None
        if source_id not in (None, ""):
            pk = self._partners_by_etl.get(str(source_id))
        if pk is None and vendor_number:
            pk = self._partners_by_vendor.get(str(vendor_number).strip())
        if pk is None and (source_id or vendor_number):
            self.missing["partner"] += 1
        return pk

    def intervention(self, source_id: Any = None, number: Any = None) -> int | None:
        if self._pcas_by_etl is None:
            self._load_pcas()
        pk = None
        if source_id not in (None, ""):
            pk = self._pcas_by_etl.get(str(source_id))
        if pk is None and number:
            pk = self._pcas_by_number.get(str(number).strip())
        if pk is None and (source_id or number):
            self.missing["programme_document"] += 1
        return pk

    def engagement(self, reference_number: Any) -> int | None:
        """An assurance engagement by its reference number (the detail datasets have no other key)."""
        if self._engagements is None:
            self._engagements = {
                ref.strip(): pk
                for pk, ref in dm.AuditEngagement.objects.exclude(reference_number="").values_list(
                    "pk", "reference_number"
                )
            }
        pk = self._engagements.get(str(reference_number or "").strip())
        if pk is None and reference_number:
            self.missing["engagement"] += 1
        return pk

    def details(self) -> dict[str, Any]:
        return {"not_linked": dict(self.missing)} if self.missing else {}


# ------------------------------------------------------------------------ eTools tables (v2)
PARTNER_FIELDS = (
    "name",
    "short_name",
    "partner_type",
    "cso_type",
    "vendor_number",
    "rating",
    "address",
    "email",
    "phone_number",
    "alternate_id",
    "alternate_name",
    "description",
    "blocked",
    "deleted_flag",
    "hidden",
    "shared_with",
    "hact_values",
    "hact_min_requirements",
    "type_of_assessment",
    "last_assessment_date",
    "core_values_assessment_date",
    "total_ct_cp",
    "total_ct_cy",
    "net_ct_cy",
    "reported_cy",
    "total_ct_ytd",
)


# The v2 tables the Datamart sync inserts into. Their id sequences can lag behind the data after a
# database restore (pg_dump without sequence values, a copy between servers): every INSERT then
# fails with a duplicate key while UPDATEs of existing rows succeed. Raising the sequence to max(id)
# is harmless when it is already ahead.
LEGACY_INSERT_TABLES = (PartnerOrganization, Agreement, PCA, PCA.locations.through)


def align_sequences() -> dict[str, Any]:
    """Move each legacy table's id sequence past max(id) when it is behind; returns what was done."""
    if connection.vendor != "postgresql":
        return {}
    done: dict[str, Any] = {}
    for model in LEGACY_INSERT_TABLES:
        table = model._meta.db_table
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [table])
                sequence = (cursor.fetchone() or [None])[0]
                if not sequence:
                    continue
                cursor.execute(  # the table name comes from the model, quoted by the backend
                    f"SELECT COALESCE(MAX(id), 0) FROM {connection.ops.quote_name(table)}"  # noqa: S608
                )
                max_id = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT last_value FROM pg_sequences WHERE schemaname || '.' || sequencename = %s "
                    "OR sequencename = %s",
                    [sequence, sequence.split(".")[-1]],
                )
                row = cursor.fetchone()
                last = row[0] if row else None
                if max_id and (last is None or last < max_id):
                    cursor.execute("SELECT setval(%s, %s)", [sequence, max_id])
                    done[table] = {"was": last, "now": max_id}
        except Exception as exc:  # no privilege on the sequence: the INSERTs will say so per record
            logger.warning("etools datamart: could not check the id sequence of %s: %s", table, exc)
            done[table] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    if done:
        logger.warning("etools datamart: id sequences aligned: %s", done)
    return done


def upsert_partner(item: dict[str, Any], links: Links) -> None:
    source_id = item.get("source_id")
    if source_id in (None, ""):
        raise ValueError("partner without source_id")
    etl_id = str(source_id)
    partner = PartnerOrganization.objects.filter(etl_id=etl_id).first() or PartnerOrganization(etl_id=etl_id)
    assign(partner, item, PARTNER_FIELDS)
    partner.save()


INTERVENTION_FIELDS = {
    "number": "number",
    "document_type": "document_type",
    "partner_name": "partner_name",
    "status": "status",
    "title": "title",
    "country_programme": "country_programme",
    "start": "start_date",
    "end": "end_date",
    "end_date": "end_date",
    "initiation_date": "submission_date",
    "submission_date": "submission_date_prc",
    "review_date": "review_date_prc",
    "signed_by_unicef_date": "signed_by_unicef_date",
    "signed_by_partner_date": "signed_by_partner_date",
    "fr_number": "fr_number",
    "planned_visits": "planned_programmatic_visits",
}


def _set_array(instance: models.Model, name: str, values: list[str]) -> None:
    setattr(instance, name, coerce(instance._meta.get_field(name), values))


def _agreement(item: dict[str, Any], partner_id: int | None) -> Agreement | None:
    agreement_id = item.get("agreement_id")
    if agreement_id in (None, ""):
        return None
    agreement = Agreement.objects.filter(etl_id=str(agreement_id)).first()
    if agreement is None:
        agreement = Agreement(etl_id=str(agreement_id), agreement_type="")
    number = item.get("agreement_reference_number")
    if number:
        agreement.agreement_number = coerce(Agreement._meta.get_field("agreement_number"), number)
    if not agreement.agreement_type:
        # "LEB/PCA2023123" -> "PCA"; the agreements dataset sets the real type afterwards.
        guess = str(number or "").split("/")[-1][:4].rstrip("0123456789")
        agreement.agreement_type = (
            guess if guess in dict(Agreement._meta.get_field("agreement_type").choices) else ""
        )
    agreement.partner_id = partner_id or agreement.partner_id
    agreement.partner_name = coerce(Agreement._meta.get_field("partner_name"), item.get("partner_name"))
    agreement.save()
    return agreement


def upsert_intervention(item: dict[str, Any], links: Links) -> None:
    source_id = item.get("source_id") or item.get("intervention_id")
    if source_id in (None, ""):
        raise ValueError("intervention without source_id")
    pca = PCA.objects.filter(etl_id=str(source_id)).first() or PCA(etl_id=str(source_id), title="")
    if not item.get("number") and item.get("reference_number"):
        item = {**item, "number": item["reference_number"]}
    assign(pca, item, INTERVENTION_FIELDS)
    partner_id = links.partner(item.get("partner_source_id"), item.get("partner_vendor_number"))
    pca.partner_id = partner_id or pca.partner_id
    try:
        with transaction.atomic():  # an agreement that cannot be written must not lose the PD
            agreement = _agreement(item, pca.partner_id)
    except Exception as exc:
        logger.warning("etools datamart: agreement of PD %s not written: %s", source_id, exc)
        links.missing["agreement"] += 1
        agreement = None
    if agreement is not None:
        pca.agreement = agreement
    sections = names(item.get("sections_data")) or names(item.get("sections"))
    offices = names(item.get("offices_data"))
    _set_array(pca, "section_names", sections)
    _set_array(pca, "offices_set", offices)
    pca.offices_names = coerce(PCA._meta.get_field("offices_names"), ", ".join(offices))
    _set_array(pca, "unicef_focal_points", person_names(item.get("unicef_focal_points_data")))
    _set_array(pca, "cp_outputs", names(item.get("cp_outputs_data")))
    if "donors" in item:
        _set_array(pca, "donors", names(item.get("donors")))
    if "grants" in item:
        _set_array(pca, "grants", names(item.get("grants")))
    locations = item.get("locations_data")
    p_codes = names(locations, "pcode", "p_code")
    _set_array(pca, "location_p_codes", p_codes)
    _set_array(pca, "location_names", names(locations))
    pca.save()
    if p_codes:
        try:
            with transaction.atomic():  # the location links are not worth losing the PD over
                pca.locations.set(Location.objects.filter(p_code__in=p_codes))
        except Exception as exc:
            logger.warning("etools datamart: locations of PD %s not linked: %s", source_id, exc)
            links.missing["locations"] += 1


def update_budget(item: dict[str, Any], links: Links) -> None:
    pk = links.intervention(item.get("source_id") or item.get("intervention_id"), item.get("number"))
    if pk is None:
        return
    pca = PCA.objects.get(pk=pk)
    cash, supply = _money(item.get("budget_unicef_cash")), _money(item.get("budget_unicef_supply"))
    values = {
        "total_budget": item.get("budget_total"),
        "unicef_cash": item.get("budget_unicef_cash"),
        "cso_contribution": item.get("budget_cso_contribution"),
        "budget_currency": item.get("budget_currency"),
        "total_unicef_budget": str(cash + supply),
    }
    assign(pca, values, list(values))
    pca.save()


def update_agreement(item: dict[str, Any], links: Links) -> None:
    """The agreements dataset has no eTools id: enrich the agreements the interventions created."""
    number = item.get("reference_number")
    agreement = Agreement.objects.filter(agreement_number=number).first() if number else None
    if agreement is None:
        links.missing["agreement"] += 1
        return
    kind = item.get("agreement_type")
    if kind in dict(Agreement._meta.get_field("agreement_type").choices):
        agreement.agreement_type = kind
    assign(
        agreement, item, ("start", "end", "signed_by_unicef_date", "signed_by_partner_date", "partner_name")
    )
    agreement.save()


def _donors_set(pca_id: int) -> list[dict[str, Any]]:
    """PCA.donors_set rebuilt from the FR lines: one entry per donor and grant."""
    totals: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for donor, code, grant, amount in dm.FundsReservation.objects.filter(intervention_id=pca_id).values_list(
        "donor", "donor_code", "grant_number", "overall_amount"
    ):
        totals[(donor or "Unknown", code or "", grant or "")] += amount or Decimal(0)
    return [
        {"donor": donor, "donor_code": code, "grant_number": grant, "value": float(value)}
        for (donor, code, grant), value in sorted(totals.items())
    ]


# ------------------------------------------------------------------------ datamart app tables
@dataclass
class Dataset:
    """How one Datamart dataset maps onto a ``datamart`` model."""

    model: type[dm.DatamartRecord]
    fields: dict[str, str] = field(default_factory=dict)  # model field -> record key
    link: Callable[[models.Model, dict[str, Any], Links], None] | None = None
    after: Callable[[dm.DatamartRecord, dict[str, Any], Links], None] | None = None
    # Extra query parameters (e.g. a date window) and, with them, the rows a complete read replaces.
    params: Callable[[], dict[str, Any]] | None = None
    scope: Callable[[models.QuerySet], models.QuerySet] | None = None


def _link_intervention(key: str = "source_intervention_id", number_key: str = "pd_reference_number"):
    def link(row, item, links):
        row.intervention_id = links.intervention(item.get(key), item.get(number_key))

    return link


def _link_indicator(row, item, links):
    """By the PD reference number first: ``result_link_intervention`` is not documented as the
    eTools id, so it is only the fallback."""
    number = item.get("pd_reference_number")
    pk = links.intervention(None, number) if number else None
    if pk is None:
        pk = links.intervention(item.get("result_link_intervention"))
    row.intervention_id = pk


def _link_partner(source_key: str | None, vendor_key: str):
    def link(row, item, links):
        row.partner_id = links.partner(item.get(source_key) if source_key else None, item.get(vendor_key))

    return link


def _link_engagement(row, item, links):
    partner = item.get("partner") if isinstance(item.get("partner"), dict) else {}
    vendor = partner.get("vendor_number") or item.get("partner_code")
    row.partner_id = links.partner(partner.get("source_id"), vendor)
    row.vendor_number = coerce(row._meta.get_field("vendor_number"), vendor)
    row.amount_tested = coerce(
        row._meta.get_field("amount_tested"),
        item.get("spotcheck_total_amount_tested") or item.get("audited_expenditure"),
    )


def _engagement_pds(row, item, links):
    pds = item.get("active_pd_data") or []
    ids = []
    for pd in pds if isinstance(pds, list) else []:
        if isinstance(pd, dict):
            pk = links.intervention(pd.get("source_id"), pd.get("number") or pd.get("reference_number"))
        else:
            pk = links.intervention(None, pd)
        if pk:
            ids.append(pk)
    row.interventions.set(ids)


def _link_action_point(row, item, links):
    row.partner_id = links.partner(item.get("partner_source_id"), item.get("vendor_number"))
    row.intervention_id = links.intervention(
        item.get("intervention_source_id"), item.get("intervention_number")
    )


def _link_monitoring(row, item, links):
    row.partner_id = links.partner(None, item.get("vendor_number"))
    location = item.get("location")
    row.location_name = coerce(
        row._meta.get_field("location_name"),
        (location.get("name") if isinstance(location, dict) else location) or "",
    )


def _link_finding(row, item, links):
    row.partner_id = links.partner(None, item.get("partner_vendor_number"))
    row.engagement_id = links.engagement(item.get("reference_number"))


def _reporting_window() -> dict[str, Any]:
    """Partner reports of the last ``ETOOLS_DATAMART_REPORTING_YEARS`` years (the table is large)."""
    return {"reporting_period_start_date__gte": _reporting_since().isoformat()}


def _reporting_since() -> dt.date:
    return dt.date(timezone.localdate().year - settings.ETOOLS_DATAMART_REPORTING_YEARS + 1, 1, 1)


def _in_reporting_window(queryset: models.QuerySet) -> models.QuerySet:
    return queryset.filter(models.Q(period_start__gte=_reporting_since()) | models.Q(period_start=None))


def _link_report(row, item, links):
    row.partner_id = links.partner(None, item.get("partner_vendor_number"))
    row.intervention_id = links.intervention(
        item.get("etools_intervention_id"), item.get("intervention_reference_number")
    )
    row.due_date = coerce(
        row._meta.get_field("due_date"), item.get("due_date") or item.get("reporting_period_due_date")
    )


def _link_tpm_activity(row, item, links):
    row.partner_id = links.partner(None, item.get("partner_vendor_number"))
    row.intervention_id = links.intervention(None, item.get("pd_ssfa_reference_number"))
    row.locations = coerce(row._meta.get_field("locations"), ", ".join(names(item.get("locations_data"))))


def _link_staff_visit(row, item, links):
    row.partner_id = links.partner(item.get("source_partner_id"))
    row.intervention_id = links.intervention(
        item.get("source_partnership_id"), item.get("partnership_number")
    )


def _link_planned_visits(row, item, links):
    row.intervention_id = links.intervention(None, item.get("pd_reference_number"))
    row.partner_id = links.partner(None, item.get("partner_vendor_number"))


HACT_COUNTS = {
    "pv_required": "pv_mr",
    "pv_planned": "pv_planned_year",
    "pv_completed": "pv_completed_year",
    "sc_required": "sc_mr",
    "sc_planned": "sc_planned_year",
    "sc_completed": "sc_completed_year",
    "audits_required": "audits_mr",
    "audits_completed": "audits_completed",
    "outstanding_findings": "audits_outstanding_findings",
}


def _link_hact_year(row, item, links):
    row.partner_id = links.partner(item.get("partner_source_id"), item.get("vendor_number"))
    for name, key in HACT_COUNTS.items():
        setattr(row, name, _count(item.get(key)))


DATASETS: dict[str, tuple[str, Dataset]] = {
    "funds_reservations": (
        "funds-reservation",
        Dataset(
            dm.FundsReservation,
            {
                "pd_reference_number": "pd_reference_number",
                "fr_number": "fr_number",
                "line_item": "line_item",
                "line_item_text": "line_item_text",
                "fr_type": "fr_type",
                "vendor_code": "vendor_code",
                "donor": "donor",
                "donor_code": "donor_code",
                "grant_number": "grant_number",
                "fund": "fund",
                "wbs": "wbs",
                "currency": "currency",
                "overall_amount": "overall_amount",
                "overall_amount_dc": "overall_amount_dc",
                "total_amt": "total_amt",
                "intervention_amt": "intervention_amt",
                "actual_amt": "actual_amt",
                "outstanding_amt": "outstanding_amt",
                "document_date": "document_date",
                "start_date": "start_date",
                "end_date": "end_date",
                "due_date": "due_date",
                "completed_flag": "completed_flag",
            },
            link=_link_intervention(),
        ),
    ),
    "grants": (
        "funds/grants",
        Dataset(
            dm.Grant, {"name": "name", "donor": "donor", "expiry": "expiry", "description": "description"}
        ),
    ),
    "pd_indicators": (
        "pd-indicators",
        Dataset(
            dm.PDIndicator,
            {
                k: k
                for k in (
                    "pd_reference_number",
                    "title",
                    "unit",
                    "display_type",
                    "baseline_numerator",
                    "baseline_denominator",
                    "target_numerator",
                    "target_denominator",
                    "section_name",
                    "lower_result_name",
                    "cluster_name",
                    "location_name",
                    "location_pcode",
                    "disaggregation_name",
                    "is_active",
                    "is_high_frequency",
                )
            },
            link=_link_indicator,
        ),
    ),
    "assessments": (
        "partners/assessment",
        Dataset(
            dm.PartnerAssessment,
            {
                k: k
                for k in (
                    "partner_name",
                    "vendor_number",
                    "type",
                    "rating",
                    "requested_date",
                    "planned_date",
                    "completed_date",
                    "current",
                    "active",
                )
            },
            link=_link_partner(None, "vendor_number"),
        ),
    ),
    "psea_assessments": (
        "psea/assessments",
        Dataset(
            dm.PSEAAssessment,
            {
                k: k
                for k in (
                    "partner_name",
                    "vendor_number",
                    "reference_number",
                    "overall_rating",
                    "assessment_date",
                    "status",
                )
            },
            link=_link_partner(None, "vendor_number"),
        ),
    ),
    "engagements": (
        "audit/engagements",
        Dataset(
            dm.AuditEngagement,
            {
                k: k
                for k in (
                    "partner_name",
                    "reference_number",
                    "engagement_type",
                    "status",
                    "auditor",
                    "start_date",
                    "end_date",
                    "date_of_field_visit",
                    "date_of_final_report",
                    "year_of_audit",
                    "total_value",
                    "financial_findings",
                    "audit_opinion",
                    "rating",
                )
            },
            link=_link_engagement,
            after=_engagement_pds,
        ),
    ),
    "action_points": (
        "actionpoints",
        Dataset(
            dm.ActionPoint,
            {
                "reference_number": "reference_number",
                "description": "description",
                "status": "status",
                "high_priority": "high_priority",
                "due_date": "due_date",
                "date_of_completion": "date_of_completion",
                "assigned_to_name": "assigned_to_name",
                "office": "office",
                "section": "section_type",
                "category": "category_description",
                "related_module": "related_module",
                "module_reference_number": "module_reference_number",
                "partner_name": "partner_name",
                "intervention_number": "intervention_number",
                "location_name": "location_name",
            },
            link=_link_action_point,
        ),
    ),
    "tpm_visits": (
        "tpm-visits",
        Dataset(
            dm.TPMVisit,
            {
                "partner_name": "partner_name",
                "vendor_number": "vendor_number",
                "reference_number": "visit_reference_number",
                "status": "visit_status",
                "tpm_name": "tpm_name",
                "start_date": "visit_start_date",
                "end_date": "visit_end_date",
                "date_of_unicef_approved": "date_of_unicef_approved",
                "author_name": "author_name",
            },
            link=_link_partner("source_partner_id", "vendor_number"),
        ),
    ),
    "field_monitoring": (
        "fm-ontrack",
        Dataset(
            dm.MonitoringFinding,
            {
                "vendor_number": "vendor_number",
                "entity": "entity",
                "entity_type": "entity_type",
                "monitoring_activity": "monitoring_activity",
                "reference_number": "reference_number",
                "status": "status",
                "overall_finding_rating": "overall_finding_rating",
                "narrative_finding": "narrative_finding",
                "start_date": "monitoring_activity_start_date",
                "end_date": "monitoring_activity_end_date",
                "site": "site",
                "is_programmatic_visit": "is_programmatic_visit",
                "is_remote_monitoring": "is_remote_monitoring",
                "visit_lead": "visit_lead",
            },
            link=_link_monitoring,
        ),
    ),
    "hact": (
        "hact/aggregate",
        Dataset(
            dm.HACTAggregate,
            {
                k: k
                for k in (
                    "year",
                    "microassessments_total",
                    "programmaticvisits_total",
                    "followup_spotcheck",
                    "completed_spotcheck",
                    "completed_hact_audits",
                    "completed_special_audits",
                )
            },
        ),
    ),
    "funds_reservation_headers": (
        "funds/fundsreservationheader",
        Dataset(
            dm.FundsReservationHeader,
            {
                k: k
                for k in (
                    "pd_reference_number",
                    "fr_number",
                    "fr_type",
                    "vendor_code",
                    "document_text",
                    "currency",
                    "total_amt",
                    "intervention_amt",
                    "actual_amt",
                    "outstanding_amt",
                    "document_date",
                    "start_date",
                    "end_date",
                    "completed_flag",
                )
            },
            link=_link_intervention(),
        ),
    ),
    "audit_findings": (
        "audit/financial-findings-all",  # every engagement type; audit/financial-findings is audits only
        Dataset(
            dm.AuditFinding,
            {
                "reference_number": "reference_number",
                "engagement_type": "engagement_type",
                "engagement_status": "engagement_status",
                "partner_name": "partner_name",
                "vendor_number": "partner_vendor_number",
                "finding_number": "finding_number",
                "title": "title",
                "amount": "amount",
                "local_amount": "local_amount",
                "description": "description",
                "recommendation": "recommendation",
                "ip_comments": "ip_comments",
                "created": "created",
            },
            link=_link_finding,
        ),
    ),
    "partner_reports": (
        "prp/datareport",
        Dataset(
            dm.ReportedIndicator,
            {
                "partner_name": "partner_name",
                "vendor_number": "partner_vendor_number",
                "pd_reference_number": "intervention_reference_number",
                "progress_report": "progress_report",
                "report_number": "report_number",
                "report_type": "report_type",
                "report_status": "report_status",
                "report_accepted_status": "report_accepted_status",
                "is_report_final": "is_report_final",
                "period_start": "reporting_period_start_date",
                "period_end": "reporting_period_end_date",
                "submission_date": "report_submission_date",
                "acceptance_date": "report_acceptance_date",
                "submitted_by": "submitted_by",
                "narrative": "narrative",
                "section": "section",
                "pd_output": "pd_output_title",
                "pd_output_progress_status": "pd_output_progress_status",
                "indicator": "performance_indicator",
                "baseline": "baseline",
                "target": "target",
                "location": "current_location",
                "p_code": "p_code",
                "achievement_in_period": "achievement_in_reporting_period",
                "total_cumulative_progress": "total_cumulative_progress",
                "total_cumulative_progress_in_location": "total_cumulative_progress_in_location",
            },
            link=_link_report,
            params=_reporting_window,
            scope=_in_reporting_window,
        ),
    ),
    "tpm_activities": (
        "tpm-activities",
        Dataset(
            dm.TPMActivity,
            {
                "visit_reference_number": "visit_reference_number",
                "task_reference_number": "task_reference_number",
                "visit_status": "visit_status",
                "status": "status",
                "tpm_name": "tpm_name",
                "partner_name": "partner_name",
                "vendor_number": "partner_vendor_number",
                "pd_reference_number": "pd_ssfa_reference_number",
                "section": "section",
                "date": "date",
                "is_programmatic_visit": "is_pv",
            },
            link=_link_tpm_activity,
        ),
    ),
    "staff_visits": (
        "travel-activities",
        Dataset(
            dm.ProgrammaticVisit,
            {
                k: k
                for k in (
                    "travel_reference_number",
                    "travel_type",
                    "date",
                    "partner_name",
                    "partnership_number",
                    "primary_traveler",
                    "location_name",
                    "location_pcode",
                )
            },
            link=_link_staff_visit,
        ),
    ),
    "planned_visits": (
        "interventions-planned-visits",
        Dataset(
            dm.PlannedVisits,
            {
                "pd_reference_number": "pd_reference_number",
                "year": "year",
                "q1": "programmatic_q1",
                "q2": "programmatic_q2",
                "q3": "programmatic_q3",
                "q4": "programmatic_q4",
            },
            link=_link_planned_visits,
        ),
    ),
    "hact_history": (
        "hact/history",
        Dataset(
            dm.PartnerHACTYear,
            {
                "partner_name": "partner_name",
                "vendor_number": "vendor_number",
                "year": "year",
                "risk_rating": "risk_rating",
                "assessment_type": "assessment_type",
                "cash_transfers": "ct_jan_dec",
                "liquidations": "liqu_1oct_30sep",
                "expiring_threshold": "expiring_threshold",
                "approaching_threshold": "approach_threshold",
            },
            link=_link_hact_year,
        ),
    ),
    "pd_activities": (
        "interventions-activities",
        Dataset(
            dm.PDActivity,
            {
                "pd_reference_number": "pd_number",
                "result": "ll_name",
                "result_code": "ll_code",
                "code": "activity_code",
                "name": "activity",
                "unicef_cash": "activity_unicef_cash",
                "cso_cash": "activity_cso_cash",
            },
            link=_link_intervention(key="", number_key="pd_number"),
        ),
    ),
}


def _stored(item: dict[str, Any]) -> dict[str, Any]:
    """The record for the ``data`` column; NUL characters are not storable in Postgres JSON."""

    def clean(value):
        if isinstance(value, str):
            return value.replace("\x00", "")
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return clean(item)


def upsert_record(spec: Dataset, item: dict[str, Any], links: Links, seen: set[int]) -> None:
    datamart_id = _int(item.get("id"))
    if datamart_id is None:
        raise ValueError("record without id")
    seen.add(datamart_id)  # before writing: a record that fails to update keeps its previous row
    row = spec.model.objects.filter(datamart_id=datamart_id).first() or spec.model(datamart_id=datamart_id)
    row.source_id = _int(item.get("source_id"))
    assign(row, item, {"last_modify_date": "last_modify_date"})
    assign(row, item, spec.fields)
    if spec.link:
        spec.link(row, item, links)
    row.data = _stored(item)
    row.save()
    if spec.after:
        spec.after(row, item, links)


# ------------------------------------------------------------------------------------ runners
def _run(run: SyncRun, body: Callable[[], dict[str, Any]]) -> SyncRun:
    try:
        details = body()
    except Exception as exc:
        fail(run, exc)
        raise
    return finish_by_counts(run, **details)


def _label(item: dict[str, Any]) -> str:
    return str(item.get("source_id") or item.get("id") or "?")


# ------------------------------------------------------------------------ whole records (documents)
scrub = catalogue.scrub  # contact details are never stored in documents


def record_key(item: dict[str, Any]) -> str:
    """The Datamart id, or a hash of the record for the datasets without one."""
    if item.get("id") not in (None, ""):
        return str(item["id"])
    return hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()


class DocumentWriter:
    """Upserts the records of one catalogue dataset into ``DatamartDocument`` and, once the dataset
    was read to the end, removes the records the Datamart no longer returns."""

    def __init__(self, name: str, links: Links) -> None:
        self.name, self.spec, self.links = name, catalogue.DOCUMENTS[name], links
        self.seen: set[str] = set()

    def write(self, item: dict[str, Any]) -> None:
        spec, key = self.spec, record_key(item)
        self.seen.add(key)
        partner_key, vendor_key = spec.partner
        pd_key, number_key = spec.intervention
        partner_id = (
            self.links.partner(
                item.get(partner_key) if partner_key else None, item.get(vendor_key) if vendor_key else None
            )
            if partner_key or vendor_key
            else None
        )
        intervention_id = (
            self.links.intervention(
                item.get(pd_key) if pd_key else None, item.get(number_key) if number_key else None
            )
            if pd_key or number_key
            else None
        )
        if partner_id is None and intervention_id is not None:
            partner_id = PCA.objects.filter(pk=intervention_id).values_list("partner_id", flat=True).first()
        title = " · ".join(str(item[k]) for k in spec.title if item.get(k) not in (None, ""))
        dm.DatamartDocument.objects.update_or_create(
            dataset=self.name,
            record_key=key,
            defaults={
                "source_id": _int(item.get("source_id")),
                "partner_id": partner_id,
                "intervention_id": intervention_id,
                "title": title[:500],
                "date": parse_iso_date(item.get(spec.date)) if spec.date else None,
                "data": scrub(item),
            },
        )

    def finish(self) -> dict[str, Any]:
        if not self.seen:  # an empty answer never empties the dataset
            return {"documents": 0}
        stale = dm.DatamartDocument.objects.filter(dataset=self.name).exclude(record_key__in=self.seen)
        removed, _ = stale.delete()
        return {"documents": len(self.seen), "documents_removed": removed}


def _write_documents(name: str, items: list[dict[str, Any]], links: Links) -> dict[str, Any]:
    """Raw copies of a dataset whose records were just applied elsewhere (one failure never stops them)."""
    writer, failed = DocumentWriter(name, links), 0
    for item in items:
        try:
            with transaction.atomic():
                writer.write(item)
        except Exception as exc:
            failed += 1
            logger.warning("etools datamart %s: document %s failed: %s", name, _label(item), exc)
    details = writer.finish()
    return {**details, "documents_failed": failed} if failed else details


def sync_legacy(
    name: str, dataset: str, handler: Callable[[dict[str, Any], Links], None]
) -> Callable[..., SyncRun]:
    def sync(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
        def body() -> dict[str, Any]:
            links.missing.clear()
            aligned = align_sequences()
            items = list(client.list(dataset))
            process_items(run, items, lambda item: handler(item, links), _label)
            links.refresh()  # new partners and programme documents are linkable from here on
            details = links.details()
            links.missing.clear()
            if aligned:
                details["sequences_aligned"] = aligned
            return {**details, **_write_documents(name, items, links)}

        return _run(run, body)

    sync.__name__ = f"sync_{name}"
    return sync


def sync_dataset(name: str) -> Callable[..., SyncRun]:
    dataset, spec = DATASETS[name]

    def sync(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
        def body() -> dict[str, Any]:
            links.missing.clear()
            seen: set[int] = set()
            linked_before = _funded_pcas() if spec.model is dm.FundsReservation else set()
            params = spec.params() if spec.params else None
            process_items(
                run, client.list(dataset, params), lambda item: upsert_record(spec, item, links, seen), _label
            )
            if seen:
                replaced = spec.scope(spec.model.objects.all()) if spec.scope else spec.model.objects.all()
                _, deleted = replaced.exclude(datamart_id__in=seen).delete()
                details = {"removed": deleted.get(spec.model._meta.label, 0), **links.details()}
            else:  # an empty answer (a wrong country name?) never empties the table
                details = {"removed": 0, "empty_response": True, **links.details()}
            if spec.model is dm.FundsReservation:
                details["donor_sets"] = _refresh_donor_sets(linked_before)
            return details

        return _run(run, body)

    sync.__name__ = f"sync_{name}"
    return sync


# Per-type engagement datasets: they add detail to the engagements read from audit/engagements and have
# no id of their own that the engagements carry, so they are matched by reference number.
ENRICHMENTS: dict[str, tuple[str, dict[str, str]]] = {
    "audit_results": (
        "audit/results",
        {
            "risk_rating": "risk_rating",
            "audit_opinion": "audit_opinion",
            "audited_expenditure": "audited_expenditure",
            "amount_refunded": "amount_refunded",
            "pending_unsupported_amount": "pending_unsupported_amount",
            "financial_findings_count": "count_financial_findings",
            "high_priority_findings": "count_high_risk_findings",
            "key_control_weaknesses": "count_key_control_weaknesses",
        },
    ),
    "audits": (
        "audit/audit",
        {
            "audited_expenditure": "audited_expenditure",
            "audit_opinion": "audit_opinion",
            "amount_refunded": "amount_refunded",
            "pending_unsupported_amount": "pending_unsupported_amount",
            "financial_findings_count": "financial_findings_count",
            "key_control_weaknesses": "key_internal_control_count",
        },
    ),
    "spot_checks": (
        "audit/spot-check-findings",
        {
            "amount_tested": "spotcheck_total_amount_tested",
            "amount_refunded": "amount_refunded",
            "pending_unsupported_amount": "pending_unsupported_amount",
        },
    ),
    "micro_assessments": ("audit/micro-assessment", {"risk_rating": "overall_risk_rating"}),
    "special_audits": ("audit/special-audit", {}),
}


def enrich_engagement(name: str, fields: dict[str, str], item: dict[str, Any], links: Links) -> None:
    pk = links.engagement(item.get("reference_number"))
    if pk is None:
        return
    engagement = dm.AuditEngagement.objects.get(pk=pk)
    present = {model_field: key for model_field, key in fields.items() if item.get(key) not in (None, "")}
    assign(engagement, item, present)
    high = item.get("high_priority_findings")
    if isinstance(high, list):
        engagement.high_priority_findings = len(high)
    engagement.details = {**(engagement.details or {}), name: _stored(item)}
    engagement.save()


def sync_enrichment(name: str) -> Callable[..., SyncRun]:
    dataset, fields = ENRICHMENTS[name]

    def sync(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
        def body() -> dict[str, Any]:
            links.missing.clear()
            links.refresh()  # the engagements were just synced
            items = list(client.list(dataset))
            process_items(run, items, lambda item: enrich_engagement(name, fields, item, links), _label)
            details = links.details()
            links.missing.clear()
            return {**details, **_write_documents(name, items, links)}

        return _run(run, body)

    sync.__name__ = f"sync_{name}"
    return sync


def _funded_pcas() -> set[int]:
    return set(
        dm.FundsReservation.objects.exclude(intervention=None)
        .values_list("intervention_id", flat=True)
        .distinct()
    )


def _refresh_donor_sets(linked_before: set[int]) -> int:
    """Rebuild ``PCA.donors_set`` (the donor page's amounts) from the FR lines of each PD, including
    the PDs whose last FR line has just gone (their set becomes empty)."""
    updated = 0
    for pca_id in _funded_pcas() | linked_before:
        updated += PCA.objects.filter(pk=pca_id).update(donors_set=_donors_set(pca_id))
    return updated


# Dependency order: partners before programme documents, programme documents before everything
# that links to them.
# ------------------------------------------------------------------ catalogue datasets (documents)
class CountryScope:
    """The country filter of the non-``country_name`` datasets, resolved once per run."""

    def __init__(self, client: DatamartClient) -> None:
        self.client = client
        self._business_area: str | None = None

    @property
    def country(self) -> str:
        return (settings.ETOOLS_DATAMART_COUNTRY or "").strip()

    def business_area(self) -> str:
        """The country's business area code: the setting, else the Datamart workspace of that name."""
        if self._business_area is None:
            code = settings.ETOOLS_DATAMART_BUSINESS_AREA
            if not code:
                for workspace in self.client.list("workspaces", country=False):
                    if str(workspace.get("name", "")).strip().lower() == self.country.lower():
                        code = str(workspace.get("business_area_code") or "")
                        break
            if not code:
                raise IntegrationError(
                    f"no business area code for {self.country!r}; set ETOOLS_DATAMART_BUSINESS_AREA"
                )
            self._business_area = code
        return self._business_area

    def request(self, spec: catalogue.Source) -> tuple[dict[str, Any], bool]:
        """(query parameters, whether to add the country_name filter) for a dataset."""
        if spec.scope == "business_area":
            return {spec.filter_key: self.business_area()}, False
        if spec.scope == "lookup":
            return {}, False
        return {}, True

    def keeps(self, spec: catalogue.Source, item: dict[str, Any]) -> bool:
        """Whether a record belongs to the country: a filter the API ignored must never let another
        country's records in."""
        if spec.scope == "business_area":
            value = item.get(spec.filter_key)
            return value in (None, "") or str(value) == self.business_area()
        if spec.scope == "lookup":
            return not spec.keep or str(item.get(spec.keep, "")).strip().lower() == self.country.lower()
        value = item.get("country_name")
        return value in (None, "") or str(value).strip().lower() == self.country.lower()


def sync_documents(name: str) -> Callable[..., SyncRun]:
    spec = catalogue.DOCUMENTS[name]

    def sync(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
        def body() -> dict[str, Any]:
            links.missing.clear()
            scope = links.scope or CountryScope(client)
            links.scope = scope
            params, by_country = scope.request(spec)
            writer, other = DocumentWriter(name, links), 0

            def items():
                nonlocal other
                for item in client.list(spec.path, params, country=by_country):
                    if scope.keeps(spec, item):
                        yield item
                    else:
                        other += 1

            process_items(run, items(), writer.write, _label)
            details = {**links.details(), **writer.finish()}
            return {**details, "other_country_skipped": other} if other else details

        return _run(run, body)

    sync.__name__ = f"sync_{name}"
    return sync


ENTITY_SYNCS: dict[str, Callable[..., SyncRun]] = {
    "partners": sync_legacy("partners", "partners", upsert_partner),
    "interventions": sync_legacy("interventions", "interventions", upsert_intervention),
    "intervention_budgets": sync_legacy("intervention_budgets", "interventions-budget", update_budget),
    "agreements": sync_legacy("agreements", "partners/agreements", update_agreement),
    **{name: sync_dataset(name) for name in DATASETS if name != "audit_findings"},
    **{name: sync_enrichment(name) for name in ENRICHMENTS},
    "audit_findings": sync_dataset("audit_findings"),  # after the engagements they belong to
    **{
        name: sync_documents(name) for name, spec in catalogue.DOCUMENTS.items() if spec.scope != "written_by"
    },
}


def sync_all(
    *,
    only: Iterable[str] | None = None,
    triggered_by: str = "schedule",
    client: DatamartClient | None = None,
) -> list[SyncRun]:
    """Run the dataset syncs in dependency order, each with its own ``SyncRun``; a failed dataset
    is recorded FAILED and the next one still runs."""
    selected = set(only) if only else set(ENTITY_SYNCS)
    unknown = selected - set(ENTITY_SYNCS)
    if unknown:
        raise ValueError(f"unknown eTools Datamart datasets: {', '.join(sorted(unknown))}")
    client = client or DatamartClient()
    links = Links()
    runs: list[SyncRun] = []
    for name, sync in ENTITY_SYNCS.items():
        if name not in selected:
            continue
        run = new_run(SyncRun.Job.ETOOLS_DATAMART, target=name, triggered_by=triggered_by)
        try:
            sync(run, client=client, links=links)
        except Exception:
            logger.error("etools datamart %s aborted; continuing with the next dataset", name)
        runs.append(run)
    return runs
