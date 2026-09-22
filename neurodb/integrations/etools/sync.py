"""eTools -> legacy ``etools_*`` tables, one function and one ``SyncRun`` per entity.

Ports the field mappings of ``etools/tasks.py`` (v2) onto ``neurodb.partnerships`` models with
the v2 upsert keys: ``etl_id`` for ``PartnerOrganization``/``Agreement``/``PCA`` and the eTools
``id`` for ``Engagement``/``Travel``/``TravelActivity``/``ActionPoint``. API keys that have no
column in the legacy schema (``postal_code``, ``city``, ``country``, ``street_address``,
``basis_for_risk_rating`` on partners; ``country_programme``/``status`` on agreements;
``metadata`` on interventions; ``status_date`` on engagements; ``face_form_*`` and
``related_agreement`` on engagement details) were silently ignored by v2 and are dropped here.

Fixed v2 defects (see the review): one missing partner aborted the run -> missing references are
logged, counted in ``details["missing_refs"]`` and left NULL; per-item exceptions printed and
swallowed -> logged with the item id, counted in ``rows_failed``, the run ends PARTIAL;
travels paginated from page 45 -> from page 1; ``PCA.donors_set`` kept only the last funding
reservation's line items -> accumulated across all FRs; ``TravelActivity.date`` taken from the
trip start -> from ``activity["date"]``; ``start_date=None`` stored as ``''`` -> NULL; FK ids
that do not exist locally (section, office, category) -> NULL instead of an integrity error.

Each item is written in its own savepoint (``runs.process_items``) rather than one transaction
per entity, so a late failure keeps the items already synced - the v2 loops were autocommit too.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterable
from typing import Any

from django.db import models
from django.utils import timezone

from neurodb.accounts.models import Office, Section
from neurodb.core.models import SyncRun
from neurodb.geo.models import Location
from neurodb.integrations.etools.client import EToolsClient
from neurodb.integrations.etools.fields import assign, coerce, parse_iso_date
from neurodb.integrations.runs import fail, finish_by_counts, new_run, process_items
from neurodb.partnerships.models import (
    PCA,
    ActionPoint,
    Agreement,
    Category,
    Engagement,
    PartnerOrganization,
    Travel,
    TravelActivity,
)

logger = logging.getLogger(__name__)

ENGAGEMENT_FINAL = "final"
ENGAGEMENT_DETAIL_PATHS = {
    "audit": "/api/audit/audits/{id}/",
    "ma": "/api/audit/micro-assessments/{id}/",
    "sc": "/api/audit/spot-checks/{id}/",
    "sa": "/api/audit/special-audits/{id}/",
}

PARTNER_FIELDS = (
    "rating",
    "last_assessment_date",
    "short_name",
    "reported_cy",
    "total_ct_ytd",
    "vendor_number",
    "hidden",
    "cso_type",
    "net_ct_cy",
    "phone_number",
    "shared_with",
    "partner_type",
    "address",
    "total_ct_cy",
    "name",
    "total_ct_cp",
    "email",
    "deleted_flag",
)
PARTNER_DETAIL_FIELDS = PARTNER_FIELDS + (
    "staff_members",
    "assessments",
    "planned_engagement",
    "hact_values",
    "hact_min_requirements",
    "planned_visits",
    "core_values_assessments",
    "flags",
    "type_of_assessment",
    "core_values_assessment_date",
)
AGREEMENT_FIELDS = (
    "agreement_number",
    "partner_name",
    "agreement_type",
    "end",
    "start",
    "signed_by_unicef_date",
    "signed_by_partner_date",
)
INTERVENTION_FIELDS = (
    "number",
    "document_type",
    "partner_name",
    "status",
    "title",
    "start",
    "end",
    "frs_total_frs_amt",
    "unicef_cash",
    "cso_contribution",
    "country_programme",
    "frs_earliest_start_date",
    "frs_latest_end_date",
    "sections",
    "section_names",
    "cp_outputs",
    "unicef_focal_points",
    "frs_total_intervention_amt",
    "frs_total_outstanding_amt",
    "offices",
    "actual_amount",
    "offices_names",
    "total_unicef_budget",
    "total_budget",
    "flagged_sections",
    "budget_currency",
    "fr_currencies_are_consistent",
    "all_currencies_are_consistent",
    "fr_currency",
    "multi_curr_flag",
    "location_p_codes",
    "donors",
    "donor_codes",
    "grants",
)
INTERVENTION_DETAIL_FIELDS = ("number", "document_type", "status", "title", "start", "end", "frs_details")
ENGAGEMENT_FIELDS = {"unique_id": "reference_number", "agreement": "agreement"}
ENGAGEMENT_LIST_FIELDS = ("engagement_type", "total_value", "status")
ENGAGEMENT_DETAIL_FIELDS = (
    "cancel_comment",
    "agreement",
    "po_item",
    "exchange_rate",
    "total_amount_tested",
    "total_amount_of_ineligible_expenditure",
    "internal_controls",
    "amount_refunded",
    "additional_supporting_documentation_provided",
    "justification_provided_and_accepted",
    "write_off_required",
    "explanation_for_additional_information",
    "audited_expenditure",
    "financial_findings",
    "audit_opinion",
    "pending_unsupported_amount",
    "findings",
    "partner_contacted_at",
    "start_date",
    "end_date",
    "authorized_officers",
    "staff_members",
    "date_of_cancel",
    "date_of_final_report",
    "date_of_report_submit",
    "date_of_comments_by_ip",
    "date_of_comments_by_unicef",
    "date_of_draft_report_to_ip",
    "date_of_draft_report_to_unicef",
    "date_of_field_visit",
    "joint_audit",
    "shared_ip_with",
)
TRAVEL_FIELDS = {
    "reference_number": "reference_number",
    "traveler_name": "traveler",
    "purpose": "purpose",
    "supervisor_name": "supervisor_name",
    "start_date": "start_date",
    "end_date": "end_date",
}
TRAVEL_DETAIL_FIELDS = {
    "international_travel": "international_travel",
    "ta_required": "ta_required",
    "itinerary_set": "itinerary",
    "activities_set": "activities",
    "attachments_set": "attachments",
    "attachments_sets": "attachments",
    "mode_of_travel": "mode_of_travel",
    "estimated_travel_cost": "estimated_travel_cost",
    "completed_at": "completed_at",
    "canceled_at": "canceled_at",
    "rejection_note": "rejection_note",
    "cancellation_note": "cancellation_note",
    "certification_note": "certification_note",
    "report_note": "report",
    "additional_note": "additional_note",
    "misc_expenses": "misc_expenses",
    "first_submission_date": "first_submission_date",
}
ACTION_POINT_FIELDS = (
    "reference_number",
    "description",
    "due_date",
    "high_priority",
    "status",
    "status_date",
)


# --------------------------------------------------------------------------------- reference lookups
class References:
    """Resolves eTools ids to local rows, remembering what was missing for the run details."""

    def __init__(self) -> None:
        self.missing: dict[str, int] = {}
        self._id_cache: dict[str, set[int]] = {}

    def by_etl_id(self, model: type[models.Model], etl_id: Any) -> models.Model | None:
        if etl_id in (None, ""):
            return None
        instance = model.objects.filter(etl_id=str(etl_id)).first()
        if instance is None:
            self._note(model, etl_id)
        return instance

    def existing_id(self, model: type[models.Model], pk: Any) -> int | None:
        """``pk`` when a row with that id exists locally (sections, offices, categories), else None."""
        if pk in (None, ""):
            return None
        label = model._meta.label
        if label not in self._id_cache:
            self._id_cache[label] = set(model.objects.values_list("pk", flat=True))
        pk = int(pk)
        if pk not in self._id_cache[label]:
            self._note(model, pk)
            return None
        return pk

    def _note(self, model: type[models.Model], key: Any) -> None:
        self.missing[model._meta.model_name] = self.missing.get(model._meta.model_name, 0) + 1
        logger.warning("etools: %s %s not found locally", model._meta.model_name, key)

    def details(self) -> dict[str, Any]:
        return {"missing_refs": self.missing} if self.missing else {}


def _label(item: dict[str, Any]) -> str:
    return str(item.get("id", "?"))


def _nested_name(item: dict[str, Any], key: str) -> str:
    value = item.get(key) or {}
    return str(value.get("name", "")) if isinstance(value, dict) else ""


def _run_entity(run: SyncRun, body: Callable[[], dict[str, Any]]) -> SyncRun:
    """Execute ``body`` and finish ``run`` by its counters; FAILED (and re-raised) on a crash."""
    try:
        details = body()
    except Exception as exc:
        fail(run, exc)
        raise
    return finish_by_counts(run, **details)


# ------------------------------------------------------------------------------------- partners
def sync_partners(run: SyncRun, *, client: EToolsClient | None = None) -> SyncRun:
    """``/api/v2/partners/`` -> ``PartnerOrganization`` (v2 ``sync_partner_data``)."""
    client = client or EToolsClient()

    def handle(item: dict[str, Any]) -> None:
        etl_id = str(item["id"])
        partner = PartnerOrganization.objects.filter(etl_id=etl_id).first() or PartnerOrganization(
            etl_id=etl_id
        )
        # name and partner_type go through the same coercion as every other field (NOT NULL, max_length).
        assign(partner, item, ("name", "partner_type", *PARTNER_FIELDS))
        partner.save()

    def body() -> dict[str, Any]:
        process_items(run, client.list("/api/v2/partners/", page_size=None), handle, _label)
        return {}

    return _run_entity(run, body)


def sync_partner_details(run: SyncRun, *, client: EToolsClient | None = None) -> SyncRun:
    """``/api/v2/partners/{id}/`` for every local partner (v2 ``sync_individual_partner_data``)."""
    client = client or EToolsClient()

    def handle(partner: PartnerOrganization) -> None:
        item = client.get(f"/api/v2/partners/{partner.etl_id}/")
        assign(partner, item, PARTNER_DETAIL_FIELDS)
        partner.save()

    def body() -> dict[str, Any]:
        process_items(run, PartnerOrganization.objects.all().iterator(), handle, lambda p: p.etl_id)
        return {}

    return _run_entity(run, body)


# ----------------------------------------------------------------------------------- agreements
def sync_agreements(run: SyncRun, *, client: EToolsClient | None = None) -> SyncRun:
    """``/api/v2/agreements/`` -> ``Agreement`` (v2 ``sync_agreement_data``)."""
    client = client or EToolsClient()
    refs = References()

    def handle(item: dict[str, Any]) -> None:
        agreement, _ = Agreement.objects.get_or_create(
            etl_id=str(item["id"]), defaults={"agreement_type": ""}
        )
        agreement.partner = refs.by_etl_id(PartnerOrganization, item.get("partner"))
        assign(agreement, item, AGREEMENT_FIELDS)
        agreement.save()

    def body() -> dict[str, Any]:
        process_items(run, client.list("/api/v2/agreements/", page_size=None), handle, _label)
        return refs.details()

    return _run_entity(run, body)


# -------------------------------------------------------------------------------- interventions
def _add_locations_by_p_code(intervention: PCA, p_codes: Iterable[str]) -> None:
    for p_code in p_codes or []:
        location = Location.objects.filter(p_code=p_code).first()
        if location is not None:
            intervention.locations.add(location)


def sync_interventions(run: SyncRun, *, client: EToolsClient | None = None) -> SyncRun:
    """``/api/v2/interventions/`` -> ``PCA`` (v2 ``sync_intervention_data``)."""
    client = client or EToolsClient()

    def handle(item: dict[str, Any]) -> None:
        intervention, _ = PCA.objects.get_or_create(etl_id=str(item["id"]), defaults={"title": ""})
        assign(intervention, item, INTERVENTION_FIELDS)
        assign(intervention, item, {"end_date": "end", "offices_set": "offices_names"})
        intervention.save()
        _add_locations_by_p_code(intervention, item.get("location_p_codes") or [])

    def body() -> dict[str, Any]:
        process_items(run, client.list("/api/v2/interventions/", page_size=None), handle, _label)
        return {}

    return _run_entity(run, body)


def donor_line_items(detail: dict[str, Any]) -> list[Any]:
    """Line items of *all* funding reservations (v2 kept only the last FR's)."""
    frs = (detail.get("frs_details") or {}).get("frs") or []
    return [line for fr in frs for line in (fr.get("line_item_details") or [])]


def sync_intervention_details(run: SyncRun, *, client: EToolsClient | None = None) -> SyncRun:
    """``/api/v2/interventions/{id}/`` for PCAs with donors (v2 ``sync_intervention_individual_data``)."""
    client = client or EToolsClient()
    refs = References()

    def handle(intervention: PCA) -> None:
        item = client.get(f"/api/v2/interventions/{intervention.etl_id}/")
        intervention.partner = refs.by_etl_id(PartnerOrganization, item.get("partner_id"))
        intervention.agreement = refs.by_etl_id(Agreement, item.get("agreement"))
        assign(intervention, item, INTERVENTION_DETAIL_FIELDS)
        assign(intervention, item, {"end_date": "end"})
        intervention.donors_set = donor_line_items(item)
        intervention.save()

    def body() -> dict[str, Any]:
        queryset = PCA.objects.filter(donors__len__gt=0).iterator()
        process_items(run, queryset, handle, lambda p: p.etl_id)
        return refs.details()

    return _run_entity(run, body)


# -------------------------------------------------------------------------------------- travels
def _travel_activity(travel: Travel, activity: dict[str, Any], refs: References) -> None:
    """v2 ``sync_trip_individual_data`` inner loop; ``date`` comes from the activity (fix)."""
    act, _ = TravelActivity.objects.get_or_create(id=int(activity["id"]))
    act.travel_type = str(activity.get("travel_type") or "").title()
    act.date = parse_iso_date(activity.get("date")) or travel.start_date
    act.is_primary_traveler = bool(activity.get("is_primary_traveler"))
    act.travel = travel
    act.partner = refs.by_etl_id(PartnerOrganization, activity.get("partner"))
    act.partnership = refs.by_etl_id(PCA, activity.get("partnership"))
    act.save()
    act.travels.add(travel)
    for location_id in activity.get("locations") or []:
        location = Location.objects.filter(id=location_id).first()
        if location is not None:
            act.locations.add(location)


def _travel_detail(travel: Travel, item: dict[str, Any], refs: References) -> None:
    assign(travel, item, TRAVEL_DETAIL_FIELDS)
    attachments = item.get("attachments") or []
    travel.have_hact = sum(
        1 for a in attachments if "HACT" in str(a.get("name", "")) and ".docx" in str(a.get("name", ""))
    )
    for activity in item.get("activities") or []:
        if not (activity.get("partner") or activity.get("partnership")):
            continue
        travel.travel_type = str(activity.get("travel_type") or "").title()
        _travel_activity(travel, activity, refs)
    travel.save()


def sync_travels(run: SyncRun, *, client: EToolsClient | None = None, days: int = 365) -> SyncRun:
    """``/api/t2f/travels/`` (all pages, from page 1) then details of trips started in ``days``.

    v2 ``sync_trip_data`` + ``sync_trip_individual_data`` (details limited to one year by the
    ``sync_etools_data`` command).
    """
    client = client or EToolsClient()
    refs = References()

    def handle_list(item: dict[str, Any]) -> None:
        travel, _ = Travel.objects.get_or_create(id=int(item["id"]))
        assign(travel, item, TRAVEL_FIELDS)
        travel.status = str(item.get("status") or "").lower()
        travel.section_id = refs.existing_id(Section, item.get("section"))
        travel.office_id = refs.existing_id(Office, item.get("office"))
        travel.save()

    def handle_detail(travel: Travel) -> None:
        _travel_detail(travel, client.get(f"/api/t2f/travels/{travel.id}/"), refs)

    def body() -> dict[str, Any]:
        process_items(run, client.list("/api/t2f/travels/"), handle_list, _label)
        listed = run.rows_in
        since = timezone.now().date() - dt.timedelta(days=days)
        recent = Travel.objects.filter(start_date__gte=since).iterator()
        process_items(run, recent, handle_detail, lambda t: str(t.id))
        return {"travels_listed": listed, "details_fetched": run.rows_in - listed, **refs.details()}

    return _run_entity(run, body)


# ---------------------------------------------------------------------------------- engagements
def _engagement_detail(engagement: Engagement, client: EToolsClient, refs: References) -> None:
    """v2 ``sync_audit_individual_data``; unknown engagement types had no valid endpoint in v2."""
    template = ENGAGEMENT_DETAIL_PATHS.get(engagement.engagement_type)
    if template is None:
        logger.warning("etools: engagement %s has unknown type %r", engagement.id, engagement.engagement_type)
        return
    data = client.get(template.format(id=engagement.id))
    assign(engagement, data, ENGAGEMENT_DETAIL_FIELDS)
    if "findings" in data:
        engagement.findings_sets = data["findings"]
    engagement.save()
    for pd in data.get("active_pd") or []:
        intervention = refs.by_etl_id(PCA, pd.get("id") if isinstance(pd, dict) else pd)
        if intervention is not None:
            engagement.active_pd.add(intervention)


def sync_engagements(run: SyncRun, *, client: EToolsClient | None = None) -> SyncRun:
    """``/api/audit/engagements/`` + per-type detail -> ``Engagement`` (v2 ``sync_audit_data``)."""
    client = client or EToolsClient()
    refs = References()

    def handle(item: dict[str, Any]) -> None:
        engagement, _ = Engagement.objects.get_or_create(id=int(item["id"]), defaults={"description": ""})
        assign(engagement, item, ENGAGEMENT_FIELDS)
        assign(engagement, item, ENGAGEMENT_LIST_FIELDS)
        partner = item.get("partner") or {}
        engagement.partner = refs.by_etl_id(
            PartnerOrganization, partner.get("id") if isinstance(partner, dict) else partner
        )
        engagement.save()
        _engagement_detail(engagement, client, refs)

    def body() -> dict[str, Any]:
        process_items(run, client.list("/api/audit/engagements/"), handle, _label)
        return refs.details()

    return _run_entity(run, body)


# -------------------------------------------------------------------------------- action points
def sync_action_points(run: SyncRun, *, client: EToolsClient | None = None) -> SyncRun:
    """``/api/action-points/action-points/?engagement=<id>`` for final engagements (v2)."""
    client = client or EToolsClient()
    refs = References()

    def handle(pair: tuple[Engagement, dict[str, Any]]) -> None:
        engagement, item = pair
        point, _ = ActionPoint.objects.get_or_create(id=int(item["id"]), defaults={"description": ""})
        assign(point, item, ACTION_POINT_FIELDS)
        point.related_module = f"{item.get('related_module', '')}_{engagement.engagement_type}"
        category = item.get("category") or {}
        point.category_id = refs.existing_id(Category, category.get("id"))
        point.category_name = coerce(
            ActionPoint._meta.get_field("category_name"), category.get("description")
        )
        point.author_name = _nested_name(item, "author")
        point.assigned_by_name = _nested_name(item, "assigned_by")
        point.assigned_to_name = _nested_name(item, "assigned_to")
        point.section_id = refs.existing_id(Section, (item.get("section") or {}).get("id"))
        point.office_id = refs.existing_id(Office, (item.get("office") or {}).get("id"))
        point.engagement = engagement
        point.partner = refs.by_etl_id(PartnerOrganization, (item.get("partner") or {}).get("id"))
        point.save()

    def items() -> Iterable[tuple[Engagement, dict[str, Any]]]:
        for engagement in Engagement.objects.filter(status=ENGAGEMENT_FINAL).iterator():
            for item in client.list("/api/action-points/action-points/", {"engagement": engagement.id}):
                yield engagement, item

    def body() -> dict[str, Any]:
        process_items(run, items(), handle, lambda pair: _label(pair[1]))
        return refs.details()

    return _run_entity(run, body)


# ---------------------------------------------------------------------------------- orchestrator
ENTITY_SYNCS: dict[str, Callable[..., SyncRun]] = {
    "partners": sync_partners,
    "partner_details": sync_partner_details,
    "agreements": sync_agreements,
    "interventions": sync_interventions,
    "intervention_details": sync_intervention_details,
    "travels": sync_travels,
    "engagements": sync_engagements,
    "action_points": sync_action_points,
}


def sync_all(
    *,
    only: Iterable[str] | None = None,
    triggered_by: str = "schedule",
    client: EToolsClient | None = None,
) -> list[SyncRun]:
    """Run the entity syncs in dependency order (the v2 ``sync_etools_data`` command order).

    Each entity gets its own ``SyncRun``; a crashed entity is recorded FAILED and the next one
    still runs. Returns the runs in execution order.
    """
    client = client or EToolsClient()
    selected = set(only) if only else set(ENTITY_SYNCS)
    unknown = selected - set(ENTITY_SYNCS)
    if unknown:
        raise ValueError(f"unknown eTools entities: {', '.join(sorted(unknown))}")
    runs: list[SyncRun] = []
    for name, sync in ENTITY_SYNCS.items():
        if name not in selected:
            continue
        run = new_run(SyncRun.Job.ETOOLS, target=name, triggered_by=triggered_by)
        try:
            sync(run, client=client)
        except Exception:
            logger.error("etools %s aborted; continuing with the next entity", name)
        runs.append(run)
    return runs
