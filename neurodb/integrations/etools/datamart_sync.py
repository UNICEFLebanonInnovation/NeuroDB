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

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.db import models

from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.geo.models import Location
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.etools.fields import assign, coerce
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
        self.missing: dict[str, int] = defaultdict(int)

    def refresh(self) -> None:
        self._partners_by_etl = self._partners_by_vendor = None
        self._pcas_by_etl = self._pcas_by_number = None

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
    agreement = _agreement(item, pca.partner_id)
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
        pca.locations.set(Location.objects.filter(p_code__in=p_codes))


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


def sync_legacy(
    name: str, dataset: str, handler: Callable[[dict[str, Any], Links], None]
) -> Callable[..., SyncRun]:
    def sync(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
        def body() -> dict[str, Any]:
            links.missing.clear()
            process_items(run, client.list(dataset), lambda item: handler(item, links), _label)
            links.refresh()  # new partners and programme documents are linkable from the next dataset
            return links.details()

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
            process_items(
                run, client.list(dataset), lambda item: upsert_record(spec, item, links, seen), _label
            )
            if seen:
                _, deleted = spec.model.objects.exclude(datamart_id__in=seen).delete()
                details = {"removed": deleted.get(spec.model._meta.label, 0), **links.details()}
            else:  # an empty answer (a wrong country name?) never empties the table
                details = {"removed": 0, "empty_response": True, **links.details()}
            if spec.model is dm.FundsReservation:
                details["donor_sets"] = _refresh_donor_sets(linked_before)
            return details

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
ENTITY_SYNCS: dict[str, Callable[..., SyncRun]] = {
    "partners": sync_legacy("partners", "partners", upsert_partner),
    "interventions": sync_legacy("interventions", "interventions", upsert_intervention),
    "intervention_budgets": sync_legacy("intervention_budgets", "interventions-budget", update_budget),
    "agreements": sync_legacy("agreements", "partners/agreements", update_agreement),
    **{name: sync_dataset(name) for name in DATASETS},
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
