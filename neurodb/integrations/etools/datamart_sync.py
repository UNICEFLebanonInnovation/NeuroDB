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
(``settings.ETOOLS_DATAMART_COUNTRY``), and a record naming another country is never written
(``details.other_country_skipped``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import connection, models, transaction
from django.db.models.fields.json import KT
from django.utils import timezone

from neurodb.core.models import DatamartSetting, SyncRun
from neurodb.datamart import catalogue, tags
from neurodb.datamart import models as dm
from neurodb.datamart.fm import PDResolver, entity_kind
from neurodb.geo.models import Location, LocationType
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.etools.fields import assign, coerce, parse_iso_date
from neurodb.integrations.etools.locations import parse_point
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
    """Focal points by name: ``{"first_name", "last_name"}`` or ``name`` dicts, or names. Never an e-mail
    address: a focal point known only by one is left out, and so is a name that holds one."""
    people = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            full = " ".join(str(item.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
            full = full or str(item.get("name") or "").strip()
        else:
            full = str(item or "").strip()
        full = " ".join(full.split())
        if full and "@" not in full and full not in people:
            people.append(full)
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
        self._engagements_by_source: dict[str, int] | None = None
        self._locations_by_pcode: dict[str, int] | None = None
        self._location_ids: set[int] | None = None
        self._sites: dict[tuple[int | None, str], int] | None = None
        self._tpm_activities: dict[str, int] | None = None
        self._tpm_visits: dict[str, int] | None = None
        self._fm_pds: PDResolver | None = None
        self.scope: Any = None  # the CountryScope of the run, once a catalogue dataset needs it
        self.missing: dict[str, int] = defaultdict(int)

    def refresh(self) -> None:
        self._partners_by_etl = self._partners_by_vendor = None
        self._pcas_by_etl = self._pcas_by_number = None
        self._engagements = self._engagements_by_source = None
        self._locations_by_pcode = self._location_ids = self._sites = None
        self._tpm_activities = self._tpm_visits = None
        self._fm_pds = None

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

    def _load_locations(self) -> None:
        self._locations_by_pcode, self._location_ids = {}, set()
        for pk, p_code in Location.objects.values_list("pk", "p_code"):
            self._location_ids.add(pk)
            if p_code:
                self._locations_by_pcode.setdefault(p_code.strip().upper(), pk)

    def location(self, source_id: Any = None, p_code: Any = None) -> int | None:
        """A location by its eTools id (the local id) or, failing that, its P-code; never by name."""
        if self._location_ids is None:
            self._load_locations()
        pk = None
        if source_id not in (None, "") and str(source_id).isdigit() and int(source_id) in self._location_ids:
            pk = int(source_id)
        if pk is None and p_code:
            pk = self._locations_by_pcode.get(str(p_code).strip().upper())
        if pk is None and (source_id or p_code):
            self.missing["location"] += 1
        return pk

    def site(self, location_id: int | None, name: Any) -> int | None:
        """A monitoring site by its name inside a location (eTools gives findings the site name only)."""
        if self._sites is None:
            self._sites = {}
            for pk, parent_id, site_name in dm.MonitoringSite.objects.values_list("pk", "parent_id", "name"):
                self._sites.setdefault((parent_id, site_name.strip().lower()), pk)
                self._sites.setdefault((None, site_name.strip().lower()), pk)
        key = str(name or "").strip().lower()
        if not key:
            return None
        pk = self._sites.get((location_id, key)) or self._sites.get((None, key))
        if pk is None:
            self.missing["site"] += 1
        return pk

    def fm_pd(
        self, entity: Any, entity_type: Any, partner_id: int | None, visit_date: dt.date | None
    ) -> tuple[int | None, str]:
        """The programme document a field monitoring finding is about, and how it was found
        (``datamart.fm.PDResolver``: the reference, its PCA/PD pair, its base number or, for a PD
        entity, the PD title among the partner's). A PD entity that matches none is counted missing."""
        if self._fm_pds is None:
            self._fm_pds = PDResolver(
                PCA.objects.values_list("pk", "number", "title", "partner_id", "start", "end")
            )
        kind = entity_kind(entity_type, entity)
        pk, how = self._fm_pds.resolve(str(entity or ""), kind, partner_id, visit_date)
        if pk is None and kind == "pd":
            self.missing["programme_document_fm"] += 1
        return pk, how

    def tpm_activity(self, source_id: Any) -> int | None:
        if self._tpm_activities is None:
            rows = dm.TPMActivity.objects.exclude(source_id=None).values_list("pk", "source_id")
            self._tpm_activities = {str(sid): pk for pk, sid in rows}
        if source_id in (None, ""):
            return None
        pk = self._tpm_activities.get(str(source_id))
        if pk is None:
            self.missing["tpm_activity"] += 1
        return pk

    def tpm_visit(self, reference_number: Any) -> int | None:
        """A TPM visit by its reference number (the activities carry no visit id)."""
        if self._tpm_visits is None:
            rows = dm.TPMVisit.objects.exclude(reference_number="").values_list("pk", "reference_number")
            self._tpm_visits = {ref.strip(): pk for pk, ref in rows}
        key = str(reference_number or "").strip()
        return self._tpm_visits.get(key) if key else None

    def engagement_by_source(self, source_id: Any) -> int | None:
        if self._engagements_by_source is None:
            rows = dm.AuditEngagement.objects.exclude(source_id=None).values_list("pk", "source_id")
            self._engagements_by_source = {str(sid): pk for pk, sid in rows}
        if source_id in (None, ""):
            return None
        pk = self._engagements_by_source.get(str(source_id))
        if pk is None:
            self.missing["engagement"] += 1
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
    partner.name = _partner_name(partner)
    partner.save()


def _partner_name(partner: PartnerOrganization) -> str:
    """A name the v2 table accepts: ``(name, vendor_number)`` is unique there, and eTools carries
    duplicate vendor records (hidden or deleted partners) with the same or an empty name. A blank
    name falls back to the short or alternate name, then to the eTools id; a name another partner
    already holds with the same vendor number is suffixed with the eTools id."""
    name = (partner.name or "").strip() or (partner.short_name or "").strip()
    name = name or (partner.alternate_name or "").strip() or f"Partner {partner.etl_id}"
    clash = PartnerOrganization.objects.filter(name=name, vendor_number=partner.vendor_number).exclude(
        etl_id=partner.etl_id
    )
    if clash.exists():
        name = f"{name[:230]} (eTools {partner.etl_id})"
    return name[:255]


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


def _tag(row, title: Any) -> None:
    for name, value in tags.tags_of(str(title or "")).as_dict().items():
        setattr(row, f"tag_{name}", value)


def _link_indicator(row, item, links):
    """By the PD reference number first: ``result_link_intervention`` is not documented as the
    eTools id, so it is only the fallback."""
    number = item.get("pd_reference_number")
    pk = links.intervention(None, number) if number else None
    if pk is None:
        pk = links.intervention(item.get("result_link_intervention"))
    row.intervention_id = pk
    _tag(row, item.get("title"))
    # the Datamart names the eTools location id three ways on this endpoint
    row.location_id = links.location(
        item.get("location_source_id") or item.get("source_location_id") or item.get("location"),
        item.get("location_pcode"),
    )


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
    row.location_id = links.location(
        item.get("location_source_id") or item.get("location"), item.get("location_pcode")
    )
    row.tpm_activity_id = links.tpm_activity(item.get("tpm_activity_source_id"))
    row.engagement_id = links.engagement_by_source(item.get("engagement_source_id"))
    if row.engagement_id is None and str(item.get("related_module") or "").lower() == "audit":
        row.engagement_id = links.engagement(item.get("module_reference_number"))


def _location_parts(value: Any) -> tuple[Any, str, str]:
    """(eTools id, P-code, name) of a Datamart location value: a dict, an id or a name."""
    if isinstance(value, dict):
        return (
            value.get("source_id") or value.get("id"),
            str(value.get("p_code") or value.get("pcode") or ""),
            str(value.get("name") or ""),
        )
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return value, "", ""
    return None, "", str(value or "")


def _link_monitoring(row, item, links):
    row.partner_id = links.partner(None, item.get("vendor_number"))
    source_id, p_code, name = _location_parts(item.get("location"))
    row.location_name = coerce(row._meta.get_field("location_name"), name)
    row.location_pcode = coerce(row._meta.get_field("location_pcode"), p_code)
    row.location_source_id = int(source_id) if str(source_id or "").isdigit() else None
    row.location_id = links.location(source_id, p_code)
    row.monitoring_site_id = links.site(row.location_id, item.get("site"))
    # row.end_date is already set from monitoring_activity_end_date (assign() runs before link())
    row.intervention_id, row.pd_match = links.fm_pd(
        item.get("entity"), item.get("entity_type"), row.partner_id, row.end_date
    )


def _link_finding(row, item, links):
    row.partner_id = links.partner(None, item.get("partner_vendor_number"))
    row.engagement_id = links.engagement(item.get("reference_number"))


def _reporting_window() -> dict[str, Any]:
    """Partner reports of the last ``ETOOLS_DATAMART_REPORTING_YEARS`` years (the table is large) and
    of every running programme document since its start (``reached.history_start``): Reached adds up
    every counted report of a PD, so its early reports must be read too."""
    return {"reporting_period_start_date__gte": _reporting_since().isoformat()}


def _reporting_since() -> dt.date:
    from neurodb.datamart.reached import history_start

    return history_start(timezone.localdate())


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
    row.high_frequency = str(item.get("high_frequency") or "").strip().lower() in ("true", "yes", "1", "t")
    row.disaggregation = item.get("disaggregation") if isinstance(item.get("disaggregation"), dict) else {}
    row.location_ref_id = links.location(None, item.get("p_code"))
    if "@" in (row.submitted_by or "") or "@" in str(item.get("submitted_by") or ""):
        # who submitted, by name only: never an e-mail address, in its column or in the record kept
        row.submitted_by = ""
        item["submitted_by"] = ""
    for name, key in (
        ("indicator_report", "indicator_report"),
        ("cp_output", "cp_output"),
        ("etools_cp_output_id", "etools_cp_output_id"),
    ):
        value = item.get(key)
        if isinstance(value, dict):  # a nested record: its name or title, else its id
            value = value.get("name") or value.get("title") or value.get("id")
        setattr(row, name, coerce(row._meta.get_field(name), "" if value is None else str(value)))
    _tag(row, item.get("performance_indicator"))


def _link_tpm_activity(row, item, links):
    row.partner_id = links.partner(None, item.get("partner_vendor_number"))
    row.intervention_id = links.intervention(None, item.get("pd_ssfa_reference_number"))
    row.locations = coerce(row._meta.get_field("locations"), ", ".join(names(item.get("locations_data"))))
    row.location_pcodes = names(item.get("locations_data"), "p_code", "pcode")
    row.visit_id = links.tpm_visit(item.get("visit_reference_number"))


def _tpm_activity_locations(row, item, links):
    ids = []
    places = item.get("locations_data")
    for place in places if isinstance(places, list) else []:
        source_id, p_code, _ = _location_parts(place)
        pk = links.location(source_id, p_code)
        if pk:
            ids.append(pk)
    row.location_links.set(ids)


def _link_staff_visit(row, item, links):
    row.partner_id = links.partner(item.get("source_partner_id"))
    row.intervention_id = links.intervention(
        item.get("source_partnership_id"), item.get("partnership_number")
    )
    row.location_id = links.location(item.get("location"), item.get("location_pcode"))


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
                    "location_source_id",
                    "location_level",
                    "location_levelname",
                    "numerator_label",
                    "denominator_label",
                    "means_of_verification",
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
                "monitoring_activity_id": "monitoring_activity_id",
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
                "previous_location_progress": "previous_location_progress",
                "admin_level": "admin_level",
                "calculation_across_locations": "calculation_method_across_location",
                "calculation_across_periods": "calculation_method_across_reporting_periods",
                "etools_indicator_id": "etools_cp_output_indicators_id",
                "etools_pd_result_id": "etools_pd_result_id",
                "narrative_assessment": "pd_output_narrative_assessment",
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
            after=_tpm_activity_locations,
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
                "location_pcode": "location_pcode",
                "location_source_id": "location_source_id",
                "location_level": "location_level",
                "location_levelname": "location_levelname",
                "related_module_id": "related_module_id",
                "tpm_activity_source_id": "tpm_activity_source_id",
                "engagement_source_id": "engagement_source_id",
                "travel_activity_source_id": "travel_activity_source_id",
            },
            link=_link_action_point,
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
    """The record for the ``data`` column, as the records kept whole are stored (``catalogue.scrub``):
    without the keys naming an e-mail address, a phone or a mobile, and without NUL characters (not
    storable in Postgres JSON)."""
    return catalogue.scrub(item)


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


def country_name(value: Any) -> str:
    """A country name as compared: case and spaces aside."""
    return " ".join(str(value or "").split()).lower()


def other_country(item: dict[str, Any], country: str) -> bool:
    """Whether a record names a country other than ``country``. A record that names none is kept: the
    request's ``country_name`` filter chose it; and with no country set, nothing is another's."""
    wanted, named = country_name(country), country_name(item.get("country_name"))
    return bool(wanted and named and named != wanted)


def remove_other_country(model: type[dm.DatamartRecord], country: str, keep: set[int]) -> int:
    """Delete the rows of a ``datamart`` table whose record names another country than ``country``,
    whatever their date (an earlier run wrote them before the guard, or before the reading window moved),
    never one of ``keep`` (the records this read wrote). The names are compared by :func:`other_country`
    itself, over the few names the table holds. Run after a read that returned this country's records, so
    a wrong country name never empties a table."""
    if not country_name(country):
        return 0
    names = model.objects.order_by().annotate(_c=KT("data__country_name")).values_list("_c", flat=True)
    foreign = [n for n in names.distinct() if n and other_country({"country_name": n}, country)]
    if not foreign:
        return 0
    rows = (
        model.objects.annotate(_c=KT("data__country_name"))
        .filter(_c__in=foreign)
        .exclude(datamart_id__in=keep)
    )
    _, deleted = model.objects.filter(pk__in=rows.values("pk")).delete()
    return deleted.get(model._meta.label, 0)


class OfCountry:
    """The records of a ``country_name``-filtered request that belong to the client's country: a filter
    the API ignored must never let another country's records in (``CountryScope.keeps`` does the same
    for the records kept whole). ``skipped`` counts the records left out; they are never written.
    Rows an earlier run wrote are removed from the ``datamart`` tables (:func:`remove_other_country`) and
    from the records kept whole; the partner, programme document, location and site tables keep them.
    ``read`` counts the distinct records read, kept or skipped (by :func:`record_key`), and ``repeated``
    those the Datamart returned again (:func:`read_whole`)."""

    def __init__(self, client: DatamartClient, items: Iterable[dict[str, Any]]) -> None:
        self.client = client  # its ``last_count`` once the items are read: what the Datamart announced
        self.country = getattr(client, "country", settings.ETOOLS_DATAMART_COUNTRY)
        self.items, self.skipped = items, 0
        self.keys: set[str] = set()
        self.repeated = 0

    def __iter__(self):
        for item in self.items:
            key = record_key(item)
            if key in self.keys:
                self.repeated += 1
            self.keys.add(key)
            if other_country(item, self.country):
                self.skipped += 1
            else:
                yield item

    @property
    def read(self) -> int:
        return len(self.keys)

    def details(self) -> dict[str, int]:
        return {"other_country_skipped": self.skipped} if self.skipped else {}


# ------------------------------------------------------------------------ whole records (documents)
scrub = catalogue.scrub  # contact details are never stored in documents


def record_key(item: dict[str, Any]) -> str:
    """The Datamart id, or a hash of the record for the datasets without one."""
    if item.get("id") not in (None, ""):
        return str(item["id"])
    return hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()


class DocumentWriter:
    """Upserts the records of one catalogue dataset into ``DatamartDocument`` and, once the dataset
    was read to the end, removes the records the Datamart no longer returns. A dataset whose records name
    no partner or programme document of their own, linked after its read (:data:`AFTER_DOCUMENTS`), keeps
    the link a record holds: a read that fails part-way never unlinks the records it rewrote."""

    def __init__(self, name: str, links: Links) -> None:
        self.name, self.spec, self.links = name, catalogue.DOCUMENTS[name], links
        self.seen: set[str] = set()
        self.linked_after = name in AFTER_DOCUMENTS and not any((*self.spec.partner, *self.spec.intervention))

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
        source_id = item.get("id") if spec.id_is_source and "source_id" not in item else item.get("source_id")
        defaults = {
            "source_id": _int(source_id),
            "title": title[:500],
            "date": parse_iso_date(item.get(spec.date)) if spec.date else None,
            "data": scrub(item),
        }
        if not self.linked_after:  # else the link step after the read sets them
            defaults.update(partner_id=partner_id, intervention_id=intervention_id)
        dm.DatamartDocument.objects.update_or_create(dataset=self.name, record_key=key, defaults=defaults)

    def finish(self, *, whole: bool = True) -> dict[str, Any]:
        """Remove the records the Datamart no longer returns, after a ``whole`` read that returned some: an
        empty answer, or fewer records than the Datamart announced, removes nothing."""
        if not self.seen:  # an empty answer never empties the dataset
            return {"documents": 0}
        if not whole:
            return {"documents": len(self.seen), "documents_removed": 0}
        stale = dm.DatamartDocument.objects.filter(dataset=self.name).exclude(record_key__in=self.seen)
        removed, _ = stale.delete()
        return {"documents": len(self.seen), "documents_removed": removed}


def _write_documents(
    name: str, items: list[dict[str, Any]], links: Links, found: OfCountry | None = None
) -> dict[str, Any]:
    """Raw copies of a dataset whose records were just applied elsewhere (one failure never stops them).
    ``found``, the read they come from: when it got fewer records than the Datamart announced, no copy it
    did not return is removed (:func:`read_whole`) and the run ends PARTIAL."""
    writer, failed = DocumentWriter(name, links), 0
    for item in items:
        try:
            with transaction.atomic():
                writer.write(item)
        except Exception as exc:
            failed += 1
            logger.warning("etools datamart %s: document %s failed: %s", name, _label(item), exc)
    read = found.read if found else len(writer.seen)
    whole = found is None or read_whole(getattr(found.client, "last_count", None), read, found.repeated)
    details = writer.finish(whole=whole)
    if writer.seen and not whole:
        details.update(_removal_skipped(read, found.repeated))
    return {**details, "documents_failed": failed} if failed else details


# ------------------------------------------------------------------- locations (the gazetteer)
def _location_type(admin_level: Any, name: Any, cache: dict[int, int]) -> int | None:
    """The ``LocationType`` of an admin level, named as eTools names it.

    The v2 table already holds types by name (``Governorate``, ``Cadaster``, ``School`` ...), most
    without an admin level, and the name is unique: an existing row with eTools' name is reused
    (and given the level when it has none) before a new one is created."""
    if admin_level in (None, "") or not str(admin_level).lstrip("-").isdigit():
        return None
    level = int(admin_level)
    if level not in cache:
        label = str(name or f"Admin level {level}").strip()[:254]
        location_type = LocationType.objects.filter(admin_level=level).first()
        if location_type is None:
            location_type = LocationType.objects.filter(name__iexact=label).first()
            if location_type is not None and location_type.admin_level is None:
                location_type.admin_level = level
                location_type.save(update_fields=["admin_level", "modified"])
        if location_type is None:
            location_type = LocationType.objects.create(name=label, admin_level=level)
        cache[level] = location_type.pk
    return cache[level]


def upsert_location(item: dict[str, Any], links: Links, types: dict[int, int]) -> int | None:
    """One ``datamart/locations`` record into the locations table, keyed by the eTools id."""
    source_id = item.get("source_id") or item.get("id")
    if source_id in (None, ""):
        return None
    pk = int(source_id)
    location, _ = Location.objects.get_or_create(
        id=pk, defaults={"name": item.get("name") or "", "lft": 1, "rght": 2, "level": 0, "tree_id": pk}
    )
    location.name = coerce(Location._meta.get_field("name"), item.get("name") or "")
    location.p_code = coerce(Location._meta.get_field("p_code"), item.get("p_code") or "")
    location.type_id = _location_type(item.get("admin_level"), item.get("admin_level_name"), types)
    location.latitude = coerce(Location._meta.get_field("latitude"), item.get("latitude"))
    location.longitude = coerce(Location._meta.get_field("longitude"), item.get("longitude"))
    location.is_active = item.get("is_active") is not False
    location.save()
    return pk


def sync_locations(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
    """The eTools locations (P-code, admin level, parent, coordinates): the table every other
    eTools record links to by id or P-code. Parents are linked once every row exists."""

    def body() -> dict[str, Any]:
        links.missing.clear()
        found = OfCountry(client, client.list("locations"))
        items = list(found)
        types: dict[int, int] = {}
        by_datamart_id: dict[str, int] = {}
        parents: dict[int, Any] = {}

        def handle(item: dict[str, Any]) -> None:
            pk = upsert_location(item, links, types)
            if pk is not None:
                by_datamart_id[str(item.get("id"))] = pk
                parents[pk] = item.get("parent")

        process_items(run, items, handle, _label)
        linked = 0
        existing = set(Location.objects.values_list("pk", flat=True))
        for pk, parent in parents.items():
            parent_pk = _location_parts(parent)[0]
            parent_pk = by_datamart_id.get(str(parent_pk), parent_pk)
            if parent_pk in (None, "") or not str(parent_pk).isdigit() or int(parent_pk) not in existing:
                continue
            Location.objects.filter(pk=pk).update(parent_id=int(parent_pk))
            linked += 1
        links.refresh()
        details = {
            "parents_linked": linked,
            "location_types": len(types),
            "relinked": relink_locations(),
            **links.details(),
            **found.details(),
        }
        links.missing.clear()
        return {**details, **_write_documents("locations", items, links, found)}

    return _run(run, body)


# (table, location column, eTools id column, P-code column): rows synced before the gazetteer
# existed, or before a location appeared in it, get their link on the next locations run.
_RELINK = (
    ("datamart_pdindicator", "location_id", "location_source_id", "location_pcode"),
    ("datamart_reportedindicator", "location_ref_id", None, "p_code"),
    ("datamart_actionpoint", "location_id", "location_source_id", "location_pcode"),
    ("datamart_monitoringfinding", "location_id", "location_source_id", "location_pcode"),
    ("datamart_programmaticvisit", "location_id", None, "location_pcode"),
    ("datamart_monitoringsite", "parent_id", None, "parent_pcode"),
)


def relink_locations() -> dict[str, int]:
    """Point every unlinked record at its location by eTools id, then by P-code (never by name)."""
    counts: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table, column, id_column, pcode_column in _RELINK:
            n = 0
            if id_column:
                # identifiers come from the tuple above, never from input
                by_id = (
                    f"UPDATE {table} t SET {column} = l.id FROM locations_location l "  # noqa: S608
                    f"WHERE t.{column} IS NULL AND t.{id_column} = l.id"
                )
                cursor.execute(by_id)
                n += cursor.rowcount
            by_pcode = (
                f"UPDATE {table} t SET {column} = l.id FROM locations_location l "  # noqa: S608
                f"WHERE t.{column} IS NULL AND t.{pcode_column} <> '' "
                f"AND upper(l.p_code) = upper(t.{pcode_column})"
            )
            cursor.execute(by_pcode)
            n += cursor.rowcount
            if n:
                counts[table.removeprefix("datamart_")] = n
        # programme documents: the planned locations from their P-code arrays
        cursor.execute(
            "INSERT INTO etools_pca_locations (pca_id, location_id) "
            "SELECT p.id, l.id FROM etools_pca p, unnest(p.location_p_codes) AS code "
            "JOIN locations_location l ON upper(l.p_code) = upper(code) "
            "WHERE NOT EXISTS (SELECT 1 FROM etools_pca_locations x "
            "WHERE x.pca_id = p.id AND x.location_id = l.id)"
        )
        if cursor.rowcount:
            counts["programme_documents"] = cursor.rowcount
    return counts


def upsert_site(item: dict[str, Any], links: Links) -> None:
    """One ``datamart/location-sites`` record: a named point inside an admin location."""
    parent_source_id, parent_pcode, _ = _location_parts(item.get("parent"))
    longitude, latitude = parse_point(item.get("point"))
    site, _ = dm.MonitoringSite.objects.get_or_create(
        datamart_id=int(item["id"]), defaults={"name": item.get("name") or ""}
    )
    values = {
        "source_id": item.get("source_id"),
        "name": item.get("name") or "",
        "p_code": item.get("p_code") or "",
        "latitude": latitude,
        "longitude": longitude,
        "is_active": item.get("is_active") is not False,
        "parent_pcode": parent_pcode,
        "last_modify_date": item.get("last_modify_date"),
    }
    assign(site, values, list(values))
    site.parent_id = links.location(parent_source_id, parent_pcode)
    site.save()


def sync_sites(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
    def body() -> dict[str, Any]:
        links.missing.clear()
        found = OfCountry(client, client.list("location-sites"))
        items = list(found)
        process_items(run, items, lambda item: upsert_site(item, links), _label)
        links.refresh()
        details = {**links.details(), **found.details()}
        links.missing.clear()
        return {**details, **_write_documents("location_sites", items, links, found)}

    return _run(run, body)


def sync_legacy(
    name: str, dataset: str, handler: Callable[[dict[str, Any], Links], None]
) -> Callable[..., SyncRun]:
    def sync(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
        def body() -> dict[str, Any]:
            links.missing.clear()
            aligned = align_sequences()
            found = OfCountry(client, client.list(dataset))
            items = list(found)
            process_items(run, items, lambda item: handler(item, links), _label)
            links.refresh()  # new partners and programme documents are linkable from here on
            details = {**links.details(), **found.details()}
            links.missing.clear()
            if aligned:
                details["sequences_aligned"] = aligned
            return {**details, **_write_documents(name, items, links, found)}

        return _run(run, body)

    sync.__name__ = f"sync_{name}"
    return sync


def read_whole(announced: int | None, read: int, repeated: int = 0) -> bool:
    """Whether a read got every record the Datamart announced (the ``count`` of its first page), each once.
    ``read`` counts distinct records (a record returned twice counts once), ``repeated`` the records
    returned again. With fewer distinct records than announced (a page cut short), or any record returned
    twice (rows moving on the Datamart while its pages were read: a record returned again stands for one
    never returned, whatever the count), the rows it did not return are kept, not removed. A list without a
    count, with no record returned twice, is taken as whole."""
    return not repeated and (announced is None or read >= announced)


def _removal_skipped(read: int, repeated: int = 0) -> dict[str, Any]:
    """The details of a read that got fewer distinct records than the Datamart announced, or some twice
    (``duplicates_read``): nothing it did not return is removed, and the run ends PARTIAL
    (``finish_by_counts``), so Data health says so; the next whole read removes them."""
    found = {"removal_skipped": True, "read": read, "partial": True}
    return {**found, "duplicates_read": repeated} if repeated else found


def sync_dataset(name: str) -> Callable[..., SyncRun]:
    dataset, spec = DATASETS[name]

    def sync(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
        def body() -> dict[str, Any]:
            links.missing.clear()
            seen: set[int] = set()
            linked_before = _funded_pcas() if spec.model is dm.FundsReservation else set()
            params = spec.params() if spec.params else None
            # the rows this read replaces, counted before it writes (a large "removed" reads against it)
            held_before = (spec.scope(spec.model.objects.all()) if spec.scope else spec.model.objects).count()
            found = OfCountry(client, client.list(dataset, params))
            process_items(run, found, lambda item: upsert_record(spec, item, links, seen), _label)
            # the distinct records read (another country's and those without an id among them)
            announced, read = getattr(client, "last_count", None), found.read
            if seen:
                # another country's rows first (every one, whatever its date), then the rest of the scope
                # the Datamart no longer returns
                other = remove_other_country(spec.model, found.country, seen)
                if read_whole(announced, read, found.repeated):
                    replaced = spec.scope(spec.model.objects.all()) if spec.scope else spec.model.objects
                    _, deleted = replaced.exclude(datamart_id__in=seen).delete()
                    details = {"removed": deleted.get(spec.model._meta.label, 0), **links.details()}
                else:  # fewer records than announced, or some twice: the rows it did not return are kept
                    details = {"removed": 0, **_removal_skipped(read, found.repeated), **links.details()}
                if other:
                    details["other_country_removed"] = other
            else:  # an empty answer (a wrong country name?) never empties the table
                details = {"removed": 0, "empty_response": True, **links.details()}
            details.update(found.details(), held_before=held_before)
            if announced is not None:
                details["datamart_count"] = announced
            if spec.model is dm.FundsReservation:
                details["donor_sets"] = _refresh_donor_sets(linked_before)
            if spec.model is dm.ReportedIndicator:
                details["cp_outputs"] = derive_cp_outputs()
                details["read_from"] = (params or {}).get("reporting_period_start_date__gte", "")
                try:  # the labels never fail the reports: a failure is said, and the run is partial
                    details["disaggregation_labels"] = sync_disaggregation_labels(client)
                except Exception as exc:
                    logger.warning("etools datamart: disaggregation labels not read: %s", exc)
                    details["disaggregation_labels"] = {"error": str(exc)[:300]}
                    details["partial"] = True
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
            found = OfCountry(client, client.list(dataset))
            items = list(found)
            process_items(run, items, lambda item: enrich_engagement(name, fields, item, links), _label)
            details = {**links.details(), **found.details()}
            links.missing.clear()
            return {**details, **_write_documents(name, items, links, found)}

        return _run(run, body)

    sync.__name__ = f"sync_{name}"
    return sync


# The CP output of each PD indicator, from its latest partner report row that names one: by the
# eTools indicator id, else by the title (pd-indicators does not carry it).
_CP_OUTPUT_BY_ID = """
UPDATE datamart_pdindicator p SET cp_output = r.cp_output
FROM (
    SELECT DISTINCT ON (intervention_id, etools_indicator_id) intervention_id, etools_indicator_id, cp_output
    FROM datamart_reportedindicator
    WHERE cp_output <> '' AND etools_indicator_id <> '' AND intervention_id IS NOT NULL
    ORDER BY intervention_id, etools_indicator_id, period_end DESC NULLS LAST, id DESC
) r
WHERE p.intervention_id = r.intervention_id AND p.source_id::text = r.etools_indicator_id
  AND p.cp_output IS DISTINCT FROM r.cp_output
"""
_CP_OUTPUT_BY_TITLE = """
UPDATE datamart_pdindicator p SET cp_output = r.cp_output
FROM (
    SELECT DISTINCT ON (intervention_id, lower(trim(indicator))) intervention_id,
           lower(trim(indicator)) AS title, cp_output
    FROM datamart_reportedindicator
    WHERE cp_output <> '' AND intervention_id IS NOT NULL
    ORDER BY intervention_id, lower(trim(indicator)), period_end DESC NULLS LAST, id DESC
) r
WHERE p.cp_output = '' AND p.intervention_id = r.intervention_id AND lower(trim(p.title)) = r.title
"""


def derive_cp_outputs() -> int:
    """Give each PD indicator the CP output its partner reports name; returns the rows changed."""
    changed = 0
    with connection.cursor() as cursor:
        for sql in (_CP_OUTPUT_BY_ID, _CP_OUTPUT_BY_TITLE):
            cursor.execute(sql)
            changed += max(cursor.rowcount, 0)
    return changed


# ------------------------------------------------------------------ PRP disaggregation labels
# The partner reports key their disaggregation values by PRP ids ("(12,)"); the labels are in PRP's own
# tables, which have no country filter: they are read one id at a time, for the ids the reports name.
DISAGGREGATION_VALUES = "sources/prp/indicatordisaggregationvalue"
DISAGGREGATIONS = "sources/prp/indicatordisaggregation"
LABEL_REQUESTS_PER_RUN = 2000  # the rest are read by the next sync
LABEL_SECONDS_PER_RUN = 600  # nor longer than this: a slow source never holds the nightly sync up
LABEL_REFRESH_DAYS = 30  # a label read longer ago is read again (PRP may rename it)
LABEL_FAILURES_IN_A_ROW = 10  # then the step stops (the source cannot be read)


def disaggregation_ids_seen() -> set[int]:
    """The disaggregation-value ids the partner reports key their values by."""
    from neurodb.datamart.disaggregation import ids_in

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT k FROM datamart_reportedindicator, jsonb_object_keys(disaggregation) k "
            "WHERE jsonb_typeof(disaggregation) = 'object'"
        )
        return ids_in(row[0] for row in cursor.fetchall())


class _LabelReader:
    """Reads labels by id within a budget of requests and of time; a record PRP does not have is
    marked, never retried before :data:`LABEL_REFRESH_DAYS`; too many failures in a row stop the
    step."""

    def __init__(self, client: DatamartClient) -> None:
        self.client = client
        self.counts = {"read": 0, "not_found": 0, "failed": 0, "left": 0}
        self.budget = LABEL_REQUESTS_PER_RUN
        self.until = time.monotonic() + LABEL_SECONDS_PER_RUN
        self.failures = 0

    def read(self, kind: str, dataset: str, pk: int, fields: Callable[[dict[str, Any]], dict[str, Any]]):
        if self.budget <= 0 or time.monotonic() > self.until:
            self.counts["left"] += 1
            return
        self.budget -= 1
        try:
            record = self.client.get(dataset, pk)
        except IntegrationError as exc:
            if exc.status == 404:
                self.counts["not_found"] += 1
                self.failures = 0
                dm.DisaggregationLabel.objects.update_or_create(
                    kind=kind, prp_id=pk, defaults={"not_found": True}
                )
                return
            self.counts["failed"] += 1
            self.failures += 1
            if self.failures >= LABEL_FAILURES_IN_A_ROW:
                raise IntegrationError(
                    f"stopped after {self.failures} failures in a row ({exc})", status=exc.status
                ) from exc
            return
        self.failures = 0
        self.counts["read"] += 1
        dm.DisaggregationLabel.objects.update_or_create(
            kind=kind, prp_id=pk, defaults={**fields(record), "not_found": False}
        )


def _label_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\x00", "").split())[:255]


def _value_label(record: dict[str, Any]) -> dict[str, Any]:
    """A disaggregation value's label and its disaggregation, given by id (``disaggregation`` or
    ``disaggregation_id``) or as a nested record (whose name is then kept as well)."""
    ref = record.get("disaggregation", record.get("disaggregation_id"))
    nested = ref if isinstance(ref, dict) else None
    type_id = _int(nested.get("id") if nested else ref)
    if nested and type_id is not None and _label_text(nested.get("name")):
        dm.DisaggregationLabel.objects.update_or_create(
            kind=dm.DisaggregationLabel.Kind.TYPE,
            prp_id=type_id,
            defaults={"label": _label_text(nested.get("name")), "not_found": False},
        )
    return {
        "label": _label_text(record.get("value")),
        "disaggregation_id": type_id,
        "active": record.get("active") is not False,
    }


def sync_disaggregation_labels(client: DatamartClient) -> dict[str, Any]:
    """Read the labels of the disaggregation values the partner reports name, and of their
    disaggregations (``DisaggregationLabel``): the ids not held yet, or read more than
    :data:`LABEL_REFRESH_DAYS` ago, at most :data:`LABEL_REQUESTS_PER_RUN` requests per run."""
    stale = timezone.now() - dt.timedelta(days=LABEL_REFRESH_DAYS)
    labels = dm.DisaggregationLabel.objects
    seen = disaggregation_ids_seen()
    reader = _LabelReader(client)

    def fresh(kind: str) -> set[int]:
        return set(labels.filter(kind=kind, synced_at__gte=stale).values_list("prp_id", flat=True))

    for pk in sorted(seen - fresh(dm.DisaggregationLabel.Kind.VALUE)):
        reader.read(
            dm.DisaggregationLabel.Kind.VALUE,
            DISAGGREGATION_VALUES,
            pk,
            _value_label,
        )
    types: set[int] = set()
    ids = sorted(seen)
    for start in range(0, len(ids), 5000):
        types.update(
            labels.filter(kind=dm.DisaggregationLabel.Kind.VALUE, prp_id__in=ids[start : start + 5000])
            .exclude(disaggregation_id=None)
            .values_list("disaggregation_id", flat=True)
        )
    for pk in sorted(types - fresh(dm.DisaggregationLabel.Kind.TYPE)):
        reader.read(
            dm.DisaggregationLabel.Kind.TYPE,
            DISAGGREGATIONS,
            pk,
            lambda r: {"label": _label_text(r.get("name")), "active": r.get("active") is not False},
        )
    return {"values_seen": len(seen), "types_seen": len(types), **reader.counts}


def _funded_pcas() -> set[int]:
    return set(
        dm.FundsReservation.objects.exclude(intervention=None)
        .order_by()
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
# Where a run's business area code came from (``details.business_area_from``)
FROM_ENVIRONMENT, FROM_ADMIN, FROM_WORKSPACES = "environment", "admin", "workspaces"
CODES_SEEN = 10  # the business area codes a run lists, the most frequent first (counts only)
NO_CODE = "(none)"  # how a record that names no business area is counted among them


def area_code(value: Any) -> str:
    """A business area code as compared: ``"2490"``, ``2490``, ``" 2490 "``, ``2490.0``, ``"2490.0"`` and
    ``{"code": "2490"}`` all read ``"2490"``; a missing or empty value reads ``""``."""
    if isinstance(value, dict):
        value = next((value[k] for k in ("code", "business_area_code") if value.get(k) not in (None, "")), "")
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text


def business_area_setting() -> tuple[str, str]:
    """The business area code set for the PRP datasets, and where: ``ETOOLS_DATAMART_BUSINESS_AREA`` (the
    hosting team's) first, then the admin's (Admin → Data and sync → eTools Datamart: PRP business area code).
    ``("", "")`` when
    neither is set: the code is then looked up in the Datamart's workspaces."""
    code = area_code(settings.ETOOLS_DATAMART_BUSINESS_AREA)
    if code:
        return code, FROM_ENVIRONMENT
    stored = DatamartSetting.objects.filter(pk=1).values_list("prp_business_area", flat=True).first()
    code = area_code(stored)
    return (code, FROM_ADMIN) if code else ("", "")


class CountryScope:
    """The country filter of the non-``country_name`` datasets, resolved once per run."""

    def __init__(self, client: DatamartClient) -> None:
        self.client = client
        self._business_area: str | None = None
        self.business_area_from = ""  # FROM_ENVIRONMENT, FROM_ADMIN or FROM_WORKSPACES, once resolved

    @property
    def country(self) -> str:
        return (settings.ETOOLS_DATAMART_COUNTRY or "").strip()

    def business_area(self) -> str:
        """The country's business area code: the environment's, else the admin's
        (:func:`business_area_setting`), else that of the first Datamart workspace of the country's name that
        has one (names compared with case and spaces aside)."""
        if self._business_area is None:
            code, source = business_area_setting()
            if not code:
                wanted = country_name(self.country)
                for workspace in self.client.list("workspaces", country=False):
                    found = area_code(workspace.get("business_area_code"))
                    if found and country_name(workspace.get("name")) == wanted:
                        code, source = found, FROM_WORKSPACES
                        break
            if not code:
                raise IntegrationError(
                    "enter the business area code in Admin → Data and sync → eTools Datamart: PRP business "
                    f"area code (none is set for {self.country!r}, and no Datamart workspace of that name "
                    "has one), or ask the hosting team to set ETOOLS_DATAMART_BUSINESS_AREA"
                )
            self._business_area, self.business_area_from = code, source
        return self._business_area

    def request(self, spec: catalogue.Source) -> tuple[dict[str, Any], bool]:
        """(query parameters, whether to add the country_name filter) for a dataset."""
        if spec.scope == "business_area":
            return {spec.filter_key: self.business_area()}, False
        if spec.scope == "lookup":
            return {}, False
        return {}, True

    def keeps(self, spec: catalogue.Source, item: dict[str, Any]) -> bool | None:
        """Whether a record belongs to the country: a filter the API ignored must never let another
        country's records in. A business-area record is compared by its code (:func:`area_code`), else by
        its country name when it carries one; one that names neither gives None: it is kept (the request's
        filter chose it, as :func:`other_country` keeps a record that names no country) and counted apart."""
        if spec.scope == "business_area":
            code = area_code(item.get(spec.filter_key))
            if code:
                return code == self.business_area()
            if country_name(item.get("country_name")):
                return not other_country(item, self.country)
            return None
        if spec.scope == "lookup":
            return not spec.keep or str(item.get(spec.keep, "")).strip().lower() == self.country.lower()
        value = item.get("country_name")
        return value in (None, "") or str(value).strip().lower() == self.country.lower()


class Census:
    """What a read of a business-area dataset saw, as counts only (never a record's values, but for the
    business area codes): the records read, the codes they name (the most frequent), how many carry each
    key the sync and its readers use (``keys_seen``: a key the Datamart does not send reads 0), and the
    years of the records kept (``dated``: check 11 reads the records of the year and the years beside it)."""

    def __init__(self, spec: catalogue.Source, today: dt.date | None = None) -> None:
        self.spec, self.year = spec, (today or timezone.localdate()).year
        self.read = 0
        self.codes: Counter[str] = Counter()
        keys = ("id", "source_id", "country_name", spec.filter_key, spec.date, *spec.partner)
        keys += (*spec.intervention, *spec.title, *spec.reads)
        self.watched = tuple(dict.fromkeys(k for k in keys if k))
        self.keys: Counter[str] = Counter()
        self.years: Counter[str] = Counter()

    def saw(self, item: dict[str, Any]) -> None:
        self.read += 1
        self.codes[area_code(item.get(self.spec.filter_key))[:20] or NO_CODE] += 1
        self.keys.update(k for k in self.watched if item.get(k) not in (None, "", [], {}))

    def kept(self, item: dict[str, Any]) -> None:
        try:
            day = parse_iso_date(item.get(self.spec.date)) if self.spec.date else None
        except ValueError:  # an impossible date: the write fails and says so
            day = None
        year = getattr(day, "year", None)
        near = year is not None and abs(year - self.year) <= 1
        self.years[str(year) if near else ("none" if year is None else "other")] += 1

    def details(self) -> dict[str, Any]:
        return {
            "read": self.read,
            "business_areas_seen": dict(self.codes.most_common(CODES_SEEN)),
            "keys_seen": {k: self.keys[k] for k in self.watched},
            "dated": dict(sorted(self.years.items())),
        }


# the keys a PRP progress report may name its PRP programme document by (``sources/prp/unicefprogressreport``
# is a raw PRP table: no recorded record confirms which one it sends), the first found first
PROGRESS_REPORT_PD_KEYS = ("programme_document_id", "programme_document")


def _named_pd(data: Any) -> str:
    """The PRP programme document a progress report names (its id as text: an id, ``{"id": ..}``), or ""."""
    data = data if isinstance(data, dict) else {}
    for key in PROGRESS_REPORT_PD_KEYS:
        value = data.get(key)
        if isinstance(value, dict):
            value = value.get("id")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if value not in (None, "") and not isinstance(value, bool | list):
            return str(value).strip()
    return ""


def link_progress_reports() -> dict[str, int]:
    """PRP's progress reports (``prp_progress_reports``) linked to their programme document and its partner,
    through PRP's own programme documents (``prp_programme_documents``, linked to the eTools PD by its
    external id or reference number): a progress report names its PRP programme document by id, the key that
    record is held under. Run after each of the two datasets (the catalogue syncs the programme documents
    first), so a link follows a change in either. Counts only, for the run's details: ``linked``;
    ``pd_not_named`` (the record names none, under the keys tried); ``pd_not_held`` (a PRP programme
    document NeuroDB does not hold); ``pd_not_linked`` (held, not linked to a PD NeuroDB holds)."""
    held = dict(
        dm.DatamartDocument.objects.filter(dataset="prp_programme_documents").values_list(
            "record_key", "intervention_id"
        )
    )
    partners = dict(
        PCA.objects.filter(pk__in={pk for pk in held.values() if pk}).values_list("pk", "partner_id")
    )
    counts: Counter[str] = Counter()
    changed = []
    reports = dm.DatamartDocument.objects.filter(dataset="prp_progress_reports").only(
        "pk", "data", "intervention_id", "partner_id"
    )
    for doc in reports.iterator(chunk_size=2000):
        named = _named_pd(doc.data)
        pd_id = held.get(named) if named else None
        counts[
            "linked"
            if pd_id
            else "pd_not_named"
            if not named
            else "pd_not_held"
            if named not in held
            else "pd_not_linked"
        ] += 1
        partner_id = partners.get(pd_id) if pd_id else None
        if (doc.intervention_id, doc.partner_id) != (pd_id, partner_id):
            doc.intervention_id, doc.partner_id = pd_id, partner_id
            changed.append(doc)
    dm.DatamartDocument.objects.bulk_update(changed, ["intervention_id", "partner_id"], batch_size=2000)
    return dict(counts)


# the link steps run after a dataset's records are written (:func:`sync_documents`)
AFTER_DOCUMENTS: dict[str, Callable[[], dict[str, int]]] = {
    "prp_programme_documents": link_progress_reports,
    "prp_progress_reports": link_progress_reports,
}
# a dataset whose records may name no business area: such a record is kept only when the PRP programme
# document it names (:func:`_named_pd`) is held in this dataset, read by business area; the others are
# counted as another country's (``other_country_skipped``, and ``unnamed_skipped`` apart)
KEPT_BY_PROGRAMME_DOCUMENT = {"prp_progress_reports": "prp_programme_documents"}


def sync_documents(name: str) -> Callable[..., SyncRun]:
    spec = catalogue.DOCUMENTS[name]

    def sync(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
        def body() -> dict[str, Any]:
            links.missing.clear()
            scope = links.scope or CountryScope(client)
            links.scope = scope
            params, by_country = scope.request(spec)
            writer, other, unnamed = DocumentWriter(name, links), 0, 0
            census = Census(spec) if spec.scope == "business_area" else None
            keys: set[str] = set()  # the distinct records read, kept or skipped (record_key)
            repeated = 0  # the records the Datamart returned again (read_whole)
            # a record naming no business area is kept only when the PRP programme document it names is held
            # (those are read by business area): the others are counted as another country's
            by_pd = KEPT_BY_PROGRAMME_DOCUMENT.get(name)
            held = (
                set(dm.DatamartDocument.objects.filter(dataset=by_pd).values_list("record_key", flat=True))
                if by_pd
                else set()
            )
            through_pd = unnamed_skipped = 0

            def items():
                nonlocal other, unnamed, repeated, through_pd, unnamed_skipped
                for item in client.list(spec.path, params, country=by_country):
                    # a record returned again is written again (its last copy wins) and counted once
                    key = record_key(item)
                    first = key not in keys
                    keys.add(key)
                    if not first:
                        repeated += 1
                    elif census:
                        census.saw(item)
                    kept = scope.keeps(spec, item)
                    if kept is None and by_pd:
                        kept = _named_pd(item) in held
                        through_pd += first and kept
                        unnamed_skipped += first and not kept
                        if not kept:
                            other += first
                            continue
                    elif kept is False:
                        other += first
                        continue
                    else:
                        unnamed += first and kept is None
                    if census and first:
                        census.kept(item)
                    yield item

            process_items(run, items(), writer.write, _label)
            announced, read = getattr(client, "last_count", None), len(keys)
            whole = read_whole(announced, read, repeated)
            details = {**links.details(), **writer.finish(whole=whole)}
            if other:
                details["other_country_skipped"] = other
            if announced is not None:
                details["datamart_count"] = announced
            if writer.seen and not whole:
                details.update(_removal_skipped(read, repeated))
            if census:  # what was asked, from where, and what came back: counts and codes only
                details.update(
                    business_area=scope.business_area(),
                    business_area_from=scope.business_area_from,
                    **census.details(),
                )
                if unnamed:
                    details["country_unnamed"] = unnamed
                    if not set(census.codes) - {NO_CODE}:  # no record named a code: the filter is unconfirmed
                        details["partial"] = True
                if through_pd:
                    details["kept_by_programme_document"] = through_pd
                if unnamed_skipped:
                    details["unnamed_skipped"] = unnamed_skipped
                if not writer.seen:  # nothing kept: Data health warns, naming the code
                    details["empty_response"] = True
            if name in AFTER_DOCUMENTS:  # PRP's progress reports linked to their programme document
                details["progress_reports_pd_link"] = AFTER_DOCUMENTS[name]()
            return details

        return _run(run, body)

    sync.__name__ = f"sync_{name}"
    return sync


ENTITY_SYNCS: dict[str, Callable[..., SyncRun]] = {
    "locations": sync_locations,  # first: programme documents, indicators, visits... link to them
    "location_sites": sync_sites,
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
    from neurodb.datamart import query  # lazy: query reads the datamart this sync writes
    from neurodb.watch import people

    try:  # the people the records name, read again from every record just synced
        query.remember_people()
        people.forget()
    except Exception:
        logger.exception("etools datamart: the people the records name could not be read")
    return runs
