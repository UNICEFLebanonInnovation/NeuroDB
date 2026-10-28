"""eTools Datamart -> NeuroDB, one ``SyncRun`` (job ``etools_datamart``) per dataset.

Two kinds of targets:

* The existing eTools tables the whole site reads (``etools.PartnerOrganization``, ``etools.PCA``,
  ``etools.Agreement``) are upserted by their eTools id (``etl_id`` = the Datamart ``source_id``),
  so the programme, partner and donor pages, the filters and the assistant keep working unchanged
  on Datamart data. Rows are never deleted from them. A programme document's budget is the one of the budget
  record that carries its reference number (:meth:`Links.plan_budgets`), not of the one its eTools id names.
* The datasets those tables have no place for (funds reservations, grants, PD indicators,
  assessments, audits and spot checks, action points, TPM visits, field monitoring findings, HACT
  totals) go to the ``datamart`` app, keyed by the Datamart id and linked to the programme
  document and partner (a funds reservation line or header to the PD whose reference number it carries,
  :meth:`Links.by_number_first`). A dataset read whole replaces its table: rows the Datamart no longer
  returns are deleted. A large dataset is read in windows (``windows.Reading``), and nothing is removed
  unless every window was read whole. The partner reports are the exception (``Dataset.keep_unsent``):
  a whole read keeps the rows it no longer returns, counted on every page, and marks them no longer sent
  (``ReportedIndicator.not_sent_since``); it removes only a row whose replacement tonight's read brought: its
  copy (its PRP id at its place, under any Datamart id), or a row under a new Datamart id
  (:func:`keep_unsent`). A row sent again loses its mark, and only a superuser removes marked rows, after
  review (Raw eTools data, ``raw_admin.unsent_view``).

Each record is written in its own savepoint (``runs.process_items``): one bad record is logged and
counted, the rest are written and the run ends PARTIAL. Every request is limited to one country
(``settings.ETOOLS_DATAMART_COUNTRY``), and a record naming another country is never written
(``details.other_country_skipped``).
"""

from __future__ import annotations

import bisect
import datetime as dt
import functools
import hashlib
import json
import logging
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import connection, models, transaction
from django.db.models.fields.json import KT, KeyTransform
from django.utils import timezone

from neurodb.core.models import DatamartSetting, SyncRun
from neurodb.datamart import catalogue, identity, tags
from neurodb.datamart import models as dm
from neurodb.datamart import reached as rc
from neurodb.datamart.fm import PDResolver, entity_kind, norm_reference
from neurodb.geo.models import Location, LocationType
from neurodb.integrations.etools import windows
from neurodb.integrations.etools.datamart import DatamartClient
from neurodb.integrations.etools.fields import assign, coerce, parse_iso_date
from neurodb.integrations.etools.locations import parse_point
from neurodb.integrations.http import IntegrationError
from neurodb.integrations.runs import fail, finish_by_counts, new_run, process_items
from neurodb.partnerships import statuses as pd_statuses
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
        self._pca_vendor: dict[int, str] | None = None  # each PD's partner's vendor number
        self._pca_numbers: dict[int, str] = {}  # each PD's reference number (:meth:`plan_budgets`)
        self._header_numbers: dict[str, str] | None = None  # FR number -> its header's reference number
        self.scope: Any = None  # the CountryScope of the run, once a catalogue dataset needs it
        self.missing: dict[str, int] = defaultdict(int)
        # how a run linked funds reservation lines or headers to their PD (details.pd_link), each record once
        self.fr_link: Counter[str] = Counter()
        self.fr_counted: set[int] = set()
        # the budget records of a run (details.budget_link, :meth:`plan_budgets`): the PD each names by the
        # rule (by record key), the record each PD's budget is written from, the PDs whose budget is emptied
        self.budget_link: Counter[str] = Counter()
        self.budget_pds: dict[str, int | None] = {}
        self.budget_kept: dict[str, dict[str, Any]] = {}
        self.budget_stale: set[int] = set()

    def clear(self) -> None:
        """What a dataset's run counts, cleared at its start and end: ``sync_all`` shares one ``Links`` across
        the datasets."""
        self.missing.clear()
        self.budget_link.clear()
        self.budget_pds.clear()
        self.budget_kept.clear()
        self.budget_stale.clear()

    def budget_pd(self, item: dict[str, Any]) -> tuple[int | None, tuple[str, ...]]:
        """The programme document a budget record writes the budget of, and what the run counts of it: the PD
        whose reference number it carries, the exact number (case aside, spaces collapsed, as the funds link
        reads it: ``fm.norm_reference``); its eTools id (``source_id``, else ``intervention_id``) only when it
        carries no number ("by_id"). A number no PD carries links nothing, whatever the id
        ("number_not_held"; "number_not_held_amended" when it is another amendment's number of a PD NeuroDB
        holds: its budget may be an earlier version's); so does an id of no PD with no number ("id_not_held"),
        or neither ("names_none"). Several PDs carrying the number: the one an eTools id names, else the one
        whose dates cover the record's start, else the one that ends last ("twice", ``PDResolver.pick``)."""
        if self._pcas_by_etl is None:
            self._load_pcas()
        ids = [self._pcas_by_etl.get(v) for v in _budget_ids(item)]
        text = norm_reference(item.get("number"))
        if not text:
            if ids and ids[0] is not None:
                return ids[0], ("by_id",)
            return None, ("id_not_held",) if ids else ("names_none",)
        found, how = self._resolver().candidates(text)
        if how != "exact":
            amended = ("number_not_held_amended",) if how in ("token", "base") else ()
            return None, ("number_not_held", *amended)
        chosen = [pk for pk in ids if pk in found]
        if chosen or len(found) == 1:
            return (chosen or found)[0], ("by_number",)
        return self._resolver().pick(found, parse_iso_date(item.get("start_date"))), ("by_number", "twice")

    def plan_budgets(self, items: list[dict[str, Any]], *, whole: bool) -> None:
        """Which budget record writes which programme document's budget, decided over the whole read before
        any is written (:func:`update_budget`), and counted once per record in ``budget_link``
        (``details.budget_link``) and, when it names a PD NeuroDB does not hold, in ``missing``.

        Each record names its PD by :meth:`budget_pd`. When several name one PD, the one kept is the one
        linked by number, then one of whose eTools ids also names the PD, then the newest by its dates
        (:data:`BUDGET_MOMENTS`), then the last read ("several": the PDs, "set_aside": the records not
        written). A record the Datamart sent twice is one record, its last copy.

        A PD the old rule (eTools id first) gave another PD's budget keeps none: a record's eTools id names it
        while the record's number names another PD (``reached.pd_base``: another amendment of the same PD is
        the same PD), and no record names it tonight. After a whole read only (the record naming it may not
        have come), its budget fields are emptied (:meth:`clear_stale_budgets`, "cleared"). The interventions
        dataset is not read for a budget (none of the fields NeuroDB reads of it is one), so every PD holds
        the budget of the record that carries its number, or none; a PD no record names, and that no record's
        id named against its number, keeps what it holds (an earlier sync's, of its own record).

        Also counted: "number_other" (a record whose number names another PD than its eTools id, bases
        compared: the count before this rule), and, of the records linked by number, those whose
        ``source_id`` and ``intervention_id`` name a PD NeuroDB holds ("<key>_compared") and that same PD
        ("<key>_same"): the proof, night after night, of how far each id can be trusted."""
        if self._pcas_by_etl is None:
            self._load_pcas()
        last: dict[str, tuple[int, dict[str, Any]]] = {}
        for order, item in enumerate(items):
            last[record_key(item)] = (order, item)
        ranked: dict[int, list[tuple[tuple, str, dict[str, Any]]]] = defaultdict(list)
        named_by_id: set[int] = set()
        count = self.budget_link
        for key, (order, item) in last.items():
            pk, how = self.budget_pd(item)
            self.budget_pds[key] = pk
            count["records"] += 1
            count.update(how)
            if pk is None and how[0] in ("number_not_held", "id_not_held"):
                self.missing["programme_document"] += 1
            ids = {
                name: self._pcas_by_etl.get(str(item[name])) if item.get(name) not in (None, "") else None
                for name in BUDGET_ID_KEYS
            }
            by_id = next((ids[name] for name in BUDGET_ID_KEYS if item.get(name) not in (None, "")), None)
            number, own = rc.pd_base(item.get("number")), rc.pd_base(self._pca_numbers.get(by_id))
            if number and own and number != own:  # where the old rule, id first, wrote another PD's budget
                count["number_other"] += 1
                named_by_id.add(by_id)
            if pk is None:
                continue
            if "by_number" in how:
                for name in BUDGET_ID_KEYS:
                    if ids[name] is not None:
                        count[f"{name}_compared"] += 1
                        count[f"{name}_same"] += ids[name] == pk
            rank = ("by_number" in how, pk in ids.values(), _budget_moments(item), order)
            ranked[pk].append((rank, key, item))
        for records in ranked.values():
            _rank, key, item = max(records, key=lambda found: found[0])
            self.budget_kept[key] = item
            if len(records) > 1:
                count["several"] += 1
                count["set_aside"] += len(records) - 1
        if whole:
            self.budget_stale = named_by_id - set(ranked)

    def budget_written(self, item: dict[str, Any]) -> int | None:
        """The programme document whose budget this record writes (:meth:`plan_budgets`); None when it writes
        none: it names none, or another record of its PD is kept (of a record sent twice, its last copy)."""
        key = record_key(item)
        return self.budget_pds.get(key) if self.budget_kept.get(key) is item else None

    def clear_stale_budgets(self) -> int:
        """Empty the budget fields of the PDs another PD's budget was written on (:meth:`plan_budgets`) that
        still hold one; how many."""
        if not self.budget_stale:
            return 0
        holding = models.Q()
        for name in BUDGET_FIELDS:
            holding |= models.Q(**{f"{name}__gt": ""})
        rows = PCA.objects.filter(holding, pk__in=sorted(self.budget_stale))
        cleared = rows.update(**dict.fromkeys(BUDGET_FIELDS), updated_at=timezone.now())
        self.budget_link["cleared"] += cleared
        return cleared

    def budget_details(self) -> dict[str, int]:
        """The budget records counted (:meth:`plan_budgets`): ``records``, ``by_number``, ``number_other``
        and ``several`` always (a run before the number decided counted the first, third and fourth only), the
        others when any."""
        always = ("records", "by_number", "number_other", "several")
        return {
            **{key: self.budget_link[key] for key in always},
            **{key: n for key, n in self.budget_link.items() if n and key not in always},
        }

    def refresh(self) -> None:
        self._partners_by_etl = self._partners_by_vendor = None
        self._pcas_by_etl = self._pcas_by_number = self._pca_vendor = None
        self._engagements = self._engagements_by_source = None
        self._locations_by_pcode = self._location_ids = self._sites = None
        self._tpm_activities = self._tpm_visits = None
        self._fm_pds = self._header_numbers = None

    def _load_partners(self) -> None:
        self._partners_by_etl, self._partners_by_vendor = {}, {}
        for pk, etl_id, vendor in PartnerOrganization.objects.values_list("pk", "etl_id", "vendor_number"):
            self._partners_by_etl[str(etl_id)] = pk
            if vendor:
                self._partners_by_vendor.setdefault(vendor.strip(), pk)

    def _load_pcas(self) -> None:
        self._pcas_by_etl, self._pcas_by_number, self._pca_vendor = {}, {}, {}
        self._pca_numbers = {}
        for pk, etl_id, number, vendor in PCA.objects.values_list(
            "pk", "etl_id", "number", "partner__vendor_number"
        ):
            self._pcas_by_etl[str(etl_id)] = pk
            self._pca_vendor[pk] = vendor or ""
            self._pca_numbers[pk] = number or ""
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

    def by_number_first(
        self, source_id: Any, number: Any, day: dt.date | None, fr_number: Any = ""
    ) -> tuple[int | None, tuple[str, ...]]:
        """The programme document a funds reservation line or header belongs to, by the reference number it
        carries first, and what the run counts of it (``fr_link``'s keys). A line with no number takes its FR
        header's (``fr_number``). The candidates are those of :meth:`PDResolver.candidates`: the exact number,
        else another amendment's ('token', 'base': "amended"), else the PD part alone. Of several, the one the
        eTools id names; else, by exact number, the one whose dates cover ``day``, else the one that ends last
        ("twice"); else none ("unclear"). A number no PD carries links nothing ("number_not_held"): the eTools
        id is never used alone when a number is given, only with no number at all ("by_id"). It counts
        nothing itself: :func:`_link_funds` counts each record once, missing too when it links none."""
        if self._pcas_by_etl is None:
            self._load_pcas()
        named = self._pcas_by_etl.get(str(source_id)) if source_id not in (None, "") else None
        text, header = norm_reference(number), False
        if not text and str(fr_number or "").strip():
            text, header = self.header_number(fr_number), True
        if not text:
            if named is None and source_id not in (None, ""):
                return None, ("id_not_held",)
            return named, ("by_id",) if named is not None else ()
        found, how = self._resolver().candidates(text)
        counts = ["by_number", "header" if header else FR_HOW[how]] if found else []
        if named in found:
            pk = named
        elif len(found) == 1:
            pk = found[0]
        elif found and how == "exact":
            pk = self._resolver().pick(found, day)
            counts.append("twice")
        else:
            if found:
                return None, ("unclear",)
            return None, ("number_not_held",) + (("number_not_held_id_held",) if named is not None else ())
        if named is not None and named != pk:
            counts.append("id_other")  # the old rule, id first, put it there
        return pk, tuple(counts)

    def header_number(self, fr_number: Any) -> str:
        """The reference number of the FR header NeuroDB holds under ``fr_number`` (normalised), '' when none
        (:func:`fr_header_numbers`). The headers sync just before their lines (:data:`ENTITY_SYNCS`), so a
        nightly sync links a line by tonight's headers; a lines run read again alone, by the headers last
        synced."""
        if self._header_numbers is None:
            self._header_numbers = fr_header_numbers()
        return self._header_numbers.get(str(fr_number or "").strip(), "")

    def vendor_of(self, pca_id: int | None) -> str:
        """The vendor number of a programme document's partner ('' when none)."""
        if self._pca_vendor is None:
            self._load_pcas()
        return self._pca_vendor.get(pca_id, "") if pca_id is not None else ""

    def _resolver(self) -> PDResolver:
        """The programme documents by reference number (``datamart.fm.PDResolver``), built once per run."""
        if self._fm_pds is None:
            self._fm_pds = PDResolver(
                PCA.objects.values_list("pk", "number", "title", "partner_id", "start", "end")
            )
        return self._fm_pds

    def holds_number(self, number: Any) -> bool:
        """Whether a programme document NeuroDB holds has this reference number: a question, never counted
        missing (PRP's programme documents ask it of a record that names no business area)."""
        if self._pcas_by_number is None:
            self._load_pcas()
        key = str(number or "").strip()
        return bool(key) and key in self._pcas_by_number

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
        kind = entity_kind(entity_type, entity)
        pk, how = self._resolver().resolve(str(entity or ""), kind, partner_id, visit_date)
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


# The budget fields of a programme document a budget record writes (:func:`update_budget`), emptied on a PD
# the old rule gave another PD's budget (Links.clear_stale_budgets)
BUDGET_FIELDS = ("total_budget", "unicef_cash", "cso_contribution", "budget_currency", "total_unicef_budget")
# a budget record's eTools ids, in the order the old rule read them (Links.budget_pd)
BUDGET_ID_KEYS = ("source_id", "intervention_id")
# the dates of a budget record that tell the newest of several naming one PD, compared in this order
BUDGET_MOMENTS = ("modified", "updated", "last_modify_date", "created")
_NO_MOMENT = dt.datetime.min.replace(tzinfo=dt.UTC)


def _budget_ids(item: dict[str, Any]) -> list[str]:
    """The eTools ids a budget record carries, as text, in :data:`BUDGET_ID_KEYS` order."""
    return [str(item[name]) for name in BUDGET_ID_KEYS if item.get(name) not in (None, "")]


def _moment(value: Any) -> dt.datetime:
    """A date or a moment as written in a record, comparable with any other (UTC); the earliest when none."""
    from django.utils.dateparse import parse_date, parse_datetime

    text = str(value or "").strip()
    try:
        found = parse_datetime(text)
        if found is None and (day := parse_date(text[:10])) is not None:
            found = dt.datetime.combine(day, dt.time())
    except ValueError:
        found = None
    if found is None:
        return _NO_MOMENT
    return found if found.tzinfo else found.replace(tzinfo=dt.UTC)


def _budget_moments(item: dict[str, Any]) -> tuple[dt.datetime, ...]:
    return tuple(_moment(item.get(name)) for name in BUDGET_MOMENTS)


def update_budget(item: dict[str, Any], links: Links) -> None:
    """A PD's budget fields from one budget record: the record :meth:`Links.plan_budgets` chose for the PD
    whose reference number it carries (its eTools id only when it carries no number); any other record writes
    nothing. The plan counts every record (``details.budget_link``)."""
    pk = links.budget_written(item)
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
    # Extra query parameters (e.g. a date window) and, with them, the rows a complete read replaces; both
    # take ``since`` (computed once per run), so the request and the rows it replaces never differ
    since: Callable[[], dt.date] | None = None
    params: Callable[[Any], dict[str, Any]] | None = None
    scope: Callable[[models.QuerySet, Any], models.QuerySet] | None = None
    # the date key a large read is windowed by (``windows.Reading``); the others are windowed by id
    windows: str = ""
    # a whole read keeps the rows it no longer returns, counted, marked no longer sent (:func:`keep_unsent`)
    keep_unsent: bool = False


def _link_intervention(key: str = "source_intervention_id", number_key: str = "pd_reference_number"):
    def link(row, item, links):
        row.intervention_id = links.intervention(item.get(key), item.get(number_key))

    return link


# How a funds reservation line or header was linked by its number (PDResolver.candidates' steps), as counted
FR_HOW = {"exact": "exact", "token": "amended", "base": "amended", "pd_part": "pd_part"}
# the link kinds the vendor check counts (details.pd_link "vendor_compared_<kind>", "vendor_same_<kind>")
FR_VENDOR_KINDS = ("exact", "amended", "pd_part", "header", "by_id")


def fr_header_numbers() -> dict[str, str]:
    """FR number -> the reference number of the FR header NeuroDB holds under it, normalised (headers without
    one left out): the number a funds reservation line without one of its own is linked by
    (:meth:`Links.header_number`). Raw eTools data reads it alike (``raw.FundsNumbers``)."""
    found: dict[str, str] = {}
    rows = dm.FundsReservationHeader.objects.exclude(pd_reference_number="")
    for fr, number in rows.values_list("fr_number", "pd_reference_number"):
        if fr.strip() and norm_reference(number):
            found.setdefault(fr.strip(), norm_reference(number))
    return found


def _vendor(value: Any) -> str:
    """A vendor number as compared: no spaces, no leading zeros."""
    return "".join(str(value or "").split()).upper().lstrip("0")


def _link_funds(row, item, links):
    """A funds reservation line or header: to the programme document whose reference number it carries, the
    eTools id only without a number or when both agree (:meth:`Links.by_number_first`; a line with no number
    takes its FR header's). Linked by the eTools id alone, it goes to that PD only when the vendor number it
    carries is the vendor number of the PD's partner (spaces and leading zeros aside), when it carries none
    ("by_id_no_vendor", as before), or when NeuroDB holds no vendor number for that PD's partner (none, or no
    partner: "by_id_vendor_unknown", as before, since nothing shows another partner's); another vendor
    number links it to no PD ("by_id_other_partner": the id names another partner's PD). Each record is
    counted once a run in ``links.fr_link`` (``details.pd_link``): how it was linked, "moved" when it leaves
    another PD it was on for this one, and, per link kind, whether the vendor number it carries is the PD's
    partner's (the proof the user reads on Raw eTools data); and, when it links none, once in
    ``links.missing`` (``details.not_linked``), so the two agree on the page."""
    before = row.intervention_id
    line = isinstance(row, dm.FundsReservation)
    pk, counts = links.by_number_first(
        item.get("source_intervention_id"),
        item.get("pd_reference_number"),
        row.start_date,
        row.fr_number if line else "",
    )
    vendor = _vendor(row.vendor_code)
    if "by_id" in counts:
        own = _vendor(links.vendor_of(pk))
        if not vendor:
            counts += ("by_id_no_vendor",)
        elif not own:  # the PD's partner's vendor number is unknown: nothing to compare it with
            counts += ("by_id_vendor_unknown",)
        elif vendor != own:
            pk, counts = None, ("by_id_other_partner",)  # no number, and the id names another partner's PD
    row.intervention_id = pk
    if row.datamart_id in links.fr_counted:  # a record returned twice counts once
        return
    links.fr_counted.add(row.datamart_id)
    links.fr_link.update(counts)
    if pk is None and counts:  # a number or an eTools id of no PD NeuroDB holds, or several PDs fit
        links.missing["programme_document"] += 1
    if pk is not None and before is not None and before != pk:
        links.fr_link["moved"] += 1
    kind = next((k for k in FR_VENDOR_KINDS if k in counts), "")
    if pk is not None and kind and vendor:
        links.fr_link[f"vendor_compared_{kind}"] += 1
        if vendor == _vendor(links.vendor_of(pk)):
            links.fr_link[f"vendor_same_{kind}"] += 1


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


# the partner reports' date key: the request asks from it, and their windows are made of it
REPORT_START = "reporting_period_start_date"


def _reporting_window(since: dt.date) -> dict[str, Any]:
    """Partner reports of the last ``ETOOLS_DATAMART_REPORTING_YEARS`` years (the table is large) and
    of every running programme document since its start (``reached.history_start``): Reached adds up
    every counted report of a PD, so its early reports must be read too."""
    return {f"{REPORT_START}__gte": since.isoformat()}


def _reporting_since() -> dt.date:
    from neurodb.datamart.reached import history_start

    return history_start(timezone.localdate())


def _in_reporting_window(queryset: models.QuerySet, since: dt.date) -> models.QuerySet:
    return queryset.filter(models.Q(period_start__gte=since) | models.Q(period_start=None))


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
            link=_link_funds,
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
            link=_link_funds,
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
            since=_reporting_since,
            params=_reporting_window,
            scope=_in_reporting_window,
            windows=REPORT_START,
            keep_unsent=True,
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


def upsert_record(
    spec: Dataset, item: dict[str, Any], links: Links, seen: set[int], fields: Any = None
) -> None:
    """Write one record of a dataset kept in a table of its own. ``fields``: the run's count of the fields of
    the records it keeps (``raw.KeyCount``), each record once (a record returned again is written again)."""
    datamart_id = _int(item.get("id"))
    if datamart_id is None:
        raise ValueError("record without id")
    first = datamart_id not in seen
    seen.add(datamart_id)  # before writing: a record that fails to update keeps its previous row
    row = spec.model.objects.filter(datamart_id=datamart_id).first() or spec.model(datamart_id=datamart_id)
    row.source_id = _int(item.get("source_id"))
    assign(row, item, {"last_modify_date": "last_modify_date"})
    assign(row, item, spec.fields)
    if spec.link:
        spec.link(row, item, links)
    row.data = _stored(item)
    if spec.keep_unsent:
        row.not_sent_since = None  # sent again, by any read: no longer marked
    row.save()
    if spec.after:
        spec.after(row, item, links)
    if fields is not None and first:
        fields.add(row.data)


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
    """The records of a ``country_name``-filtered read that belong to the client's country: a filter
    the API ignored must never let another country's records in (``CountryScope.keeps`` does the same
    for the records kept whole). ``skipped`` counts the records left out, each once (a copy the Datamart sent
    again is not counted again, as ``read`` counts it once); they are never written.
    Rows an earlier run wrote are removed from the ``datamart`` tables (:func:`remove_other_country`) and
    from the records kept whole; the partner, programme document, location and site tables keep them.
    ``read`` counts the distinct records read, kept or skipped (by :func:`record_key`), ``repeated``
    those the Datamart returned again, and ``whole`` says whether the read got them all
    (``windows.Reading.whole``): only then is anything it did not return removed. Under a proven business
    area filter (``area``, :func:`prove_area_filter`), a record naming another business area or another
    country is a stray (``area.strays``, skipped too) and the read is not whole; one naming no code is
    written and counted (``area.unnamed``)."""

    def __init__(self, reading: windows.Reading, area: AreaFilter | None = None) -> None:
        self.reading, self.area = reading, area
        self.country = getattr(reading.client, "country", settings.ETOOLS_DATAMART_COUNTRY)
        self.skipped = 0

    def __iter__(self):
        area = self.area if self.area is not None and self.area.key else None
        for item in self.reading:
            named = area.sees(item) if area else True
            if named is False or other_country(item, self.country):
                if not self.reading.again:  # a record sent twice is left out once
                    self.skipped += 1
                    if area:
                        area.strays += 1
                continue
            if named is None:
                area.unnamed += 1
            yield item

    @property
    def read(self) -> int:
        return self.reading.read

    @property
    def repeated(self) -> int:
        return self.reading.repeated

    @property
    def whole(self) -> bool:
        return self.reading.whole and not (self.area and self.area.strays)

    def details(self) -> dict[str, int]:
        return {"other_country_skipped": self.skipped} if self.skipped else {}


# ------------------------------------------------------------------------ whole records (documents)
scrub = catalogue.scrub  # contact details are never stored in documents


RECORD_KEY_MAX = 64  # DatamartDocument.record_key
ID_WORDS = frozenset({"id", "pk"})  # the last word of a key that holds an id ("prp_id", "external_id", "pk")
ID_TEXT = 40  # an id written as text is this long at most
IDS_TRACKED = 10  # the id-like keys a read tracks for its record identity, and its run lists, at most


def id_value(value: Any) -> str:
    """An id-like value as text: a whole number (77, "77" and 77.0 read "77") or a short text (at most
    :data:`ID_TEXT` characters), ``{"id": ..}`` read as its id; "" for anything else (empty, a list, a
    decimal, a longer text)."""
    if isinstance(value, dict):
        value = value.get("id")
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text if len(text) <= ID_TEXT else ""
    return ""


@functools.lru_cache(maxsize=4096)
def _id_named(key: str) -> bool:
    """A key named as an id: its last word is in :data:`ID_WORDS` ("prp_id", "pk"), and it holds no person."""
    words = catalogue.key_words(key)
    return bool(words) and words[-1] in ID_WORDS and not catalogue.holds_person(key)


def _id_like(key: Any, value: Any) -> bool:
    """A top-level key that holds an id: named as one (:func:`_id_named`), or whose value is an ``{"id": ..}``
    record; never a key that holds a person."""
    key = str(key)
    if isinstance(value, dict) and "id" in value:
        return not catalogue.holds_person(key)
    return _id_named(key)


def id_keys(records: Iterable[dict[str, Any]]) -> Counter[str]:
    """The id-like keys of ``records`` (:func:`_id_like`), with how many records carry a value under each
    (:func:`id_value`): key names and counts only."""
    found: Counter[str] = Counter()
    for item in records:
        found.update(str(k) for k, v in item.items() if _id_like(k, v) and id_value(v))
    return found


def content_key(item: dict[str, Any]) -> str:
    """A hash of the record: what tells apart the records of a dataset that carry no id."""
    return hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()


def record_key(item: dict[str, Any], identity: str = "") -> str:
    """The Datamart id; else, for a record of a dataset without one, ``"<identity>:<value>"`` when
    ``identity`` (a key the read proved unique to every record, :func:`identity_key`) is given and the record
    carries it, the whole key :data:`RECORD_KEY_MAX` characters at most; else a hash of the record
    (:func:`content_key`)."""
    if item.get("id") not in (None, ""):
        return str(item["id"])
    if identity:
        value = id_value(item.get(identity))
        if value and len(identity) + 1 + len(value) <= RECORD_KEY_MAX:
            return f"{identity}:{value}"
    return content_key(item)


class IdentityTracker:
    """The id-like keys (:func:`id_keys`) that every record a read returned without an "id", kept or skipped,
    carries, each value once: the candidates for the dataset's record identity (:func:`identity_key`). Records
    are told apart by their content only (one returned twice, identical, counts once), never by a key being
    tested: two records of different content sharing a value drop that key for the night, whether they are
    two records or one updated during the read. A key is dropped as soon as a record lacks it or repeats a
    value; at most :data:`IDS_TRACKED` are tracked, from the first record. Only the ``hash()`` of each value
    is kept, never its text: a collision drops a key, which never merges two records."""

    def __init__(self, previous: str = "") -> None:
        self.previous = previous
        self.seen = 0  # the records of different content read without an id
        self._contents: set[str] = set()
        self._values: dict[str, set[int]] = {}

    def saw(self, item: dict[str, Any], content: str) -> None:
        """A record without an id, with its :func:`content_key`."""
        if content in self._contents:
            return
        self._contents.add(content)
        self.seen += 1
        if self.seen == 1:  # the candidates: last night's identity, pk and source_id first, then by name
            found = id_keys([item])
            first = [k for k in (self.previous, "pk", "source_id") if k in found]
            self._values = {k: set() for k in [*first, *sorted(set(found) - set(first))][:IDS_TRACKED]}
        for key in list(self._values):
            value = id_value(item.get(key))
            if not value or len(key) + 1 + len(value) > RECORD_KEY_MAX or hash(value) in self._values[key]:
                del self._values[key]
            else:
                self._values[key].add(hash(value))

    @property
    def keys(self) -> list[str]:
        """The keys every record read without an id carries, each value once."""
        return list(self._values) if self.seen else []


def identity_key(tracker: IdentityTracker, previous: str = "", *, filtered: bool = False) -> str:
    """The record identity of tonight's read (``details.record_identity``): among the keys the tracker
    kept, last night's when it still qualifies, then ``pk``, ``source_id``, then the others by name; "" for
    none (the records keep their hash). A read under a business area filter (:func:`prove_area_filter`)
    chooses none: a key unique only within the country (eTools' ``external_id``) would pass there and fail on
    the weekly read with no filter, every row renamed twice a week; it keeps last night's while it still
    qualifies. With no record read without an id, last night's stays."""
    if not tracker.seen:
        return previous
    left = tracker.keys
    if filtered:
        return previous if previous in left else ""
    return next((k for k in (previous, "pk", "source_id", *sorted(left)) if k and k in left), "")


class DocumentWriter:
    """Upserts the records of one catalogue dataset into ``DatamartDocument`` and, once the dataset
    was read to the end, removes the records the Datamart no longer returns. A dataset whose records name
    no partner or programme document of their own, linked after its read (:data:`AFTER_DOCUMENTS`), keeps
    the link a record holds: a read that fails part-way never unlinks the records it rewrote.

    A record without an id is written under tonight's record identity (``identity``, :func:`record_key`): when
    its key is not held yet, the row held under its hash or under last night's identity (``previous``) is
    renamed in place, so an unchanged record keeps its row and a changed one does not become a new row.
    ``by_number``: the records (by :func:`content_key`) linked to their eTools PD by reference number only
    (:data:`KEPT_BY_REFERENCE`); ``linked``: the keys written linked to a programme document NeuroDB holds."""

    def __init__(self, name: str, links: Links, identity: str = "", previous: str = "") -> None:
        self.name, self.spec, self.links = name, catalogue.DOCUMENTS[name], links
        self.identity, self.previous = identity, previous
        self.seen: set[str] = set()
        self.linked_after = name in AFTER_DOCUMENTS and not any((*self.spec.partner, *self.spec.intervention))
        self.by_number: set[str] = set()
        self.linked: set[str] = set()
        self.renamed: set[str] = set()  # the keys rows were held under before they were renamed tonight
        self._held: set[str] | None = None  # the keys held, read once a record without an id is written
        from neurodb.datamart import raw  # lazy: Raw eTools data reads what this sync writes

        self.fields = raw.KeyCount(name)  # the fields of the records kept, each once (details.keys_kept)

    def _rename(self, key: str, item: dict[str, Any]) -> None:
        """The row held under the record's hash, or under last night's identity, renamed to ``key`` in place
        (same row, its link kept) when no row is held under ``key`` yet. This read never removes a row under
        the old key: a write that fails undoes the renaming, and that row is kept, as any failed write's."""
        held = dm.DatamartDocument.objects.filter(dataset=self.name)
        if self._held is None:
            self._held = set(held.values_list("record_key", flat=True))
        if key not in self._held:
            for old in dict.fromkeys((content_key(item), record_key(item, self.previous))):
                if old != key and old in self._held:
                    held.filter(record_key=old).update(record_key=key)
                    self._held.discard(old)
                    self.renamed.add(old)
                    break
        self._held.add(key)

    def write(self, item: dict[str, Any]) -> None:
        spec, key = self.spec, record_key(item, self.identity)
        if (self.identity or self.previous) and item.get("id") in (None, ""):
            self._rename(key, item)
        first = key not in self.seen
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
        # a record kept by its reference number alone: its eTools id is not proven the country's (they repeat
        # across countries), so it is linked by that reference number only
        by_number = bool(self.by_number) and content_key(item) in self.by_number
        if self.name == "intervention_budgets":  # the PD its number names, else its id (Links.budget_pd)
            intervention_id = self.links.budget_pds.get(key)
        elif pd_key or number_key:
            intervention_id = self.links.intervention(
                item.get(pd_key) if pd_key and not by_number else None,
                item.get(number_key) if number_key else None,
            )
        else:
            intervention_id = None
        if not self.linked_after:
            (self.linked.add if intervention_id is not None else self.linked.discard)(key)
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
        if first:
            self.fields.add(defaults["data"])

    def finish(self, *, whole: bool = True) -> dict[str, Any]:
        """Remove the records the Datamart no longer returns, after a ``whole`` read that returned some: an
        empty answer, or a read not whole, removes nothing; a large removal waits one night
        (:func:`removal_waits`). With the fields of the records kept (``keys_kept``)."""
        if not self.seen:  # an empty answer never empties the dataset
            return {"documents": 0}
        kept = {"documents": len(self.seen), "keys_kept": self.fields.details()}
        if not whole:
            return {**kept, "documents_removed": 0}
        held = dm.DatamartDocument.objects.filter(dataset=self.name)
        stale = held.exclude(record_key__in=self.seen | self.renamed)
        n = stale.count()
        if n and removal_waits(self.name, n, held.count()):
            return {**kept, "documents_removed": 0, "removal_held": n, "partial": True}
        removed, _ = stale.delete() if n else (0, {})
        return {**kept, "documents_removed": removed}


def _write_documents(
    name: str, items: list[dict[str, Any]], links: Links, found: OfCountry | None = None
) -> dict[str, Any]:
    """Raw copies of a dataset whose records were just applied elsewhere (one failure never stops them).
    ``found``, the read they come from: its details are the run's (``windows.Reading.details``: the first
    page's count, a page cut, the stable order asked, records that came twice, the windows), as every other
    read records them, so its next read sees them; when it was not read whole (``windows.Reading.whole``), no
    copy it did not return is removed and the run ends PARTIAL."""
    writer, failed = DocumentWriter(name, links), 0
    for item in items:
        try:
            with transaction.atomic():
                writer.write(item)
        except Exception as exc:
            failed += 1
            logger.warning("etools datamart %s: document %s failed: %s", name, _label(item), exc)
    whole = found is None or found.whole
    details = writer.finish(whole=whole)
    if found is not None:
        details.update(found.reading.details())
    if writer.seen and not whole:
        details.update(_removal_skipped(found.reading))
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
        found = OfCountry(windows.Reading(client, "locations", name="locations"))
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
        found = OfCountry(windows.Reading(client, "location-sites", name="location_sites"))
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
            links.clear()
            aligned = align_sequences()
            found = OfCountry(windows.Reading(client, dataset, name=name))
            items = list(found)
            budgets = name == "intervention_budgets"
            if budgets:  # one record per PD, by the number it carries, chosen before any is written
                links.plan_budgets(items, whole=found.whole)
            process_items(run, items, lambda item: handler(item, links), _label)
            if budgets:
                try:
                    with transaction.atomic():
                        links.clear_stale_budgets()
                except Exception as exc:  # the budgets are written; the next whole read empties these
                    logger.warning("etools datamart: stale budgets not emptied: %s", exc)
                    links.budget_link["clear_failed"] += len(links.budget_stale)
            links.refresh()  # new partners and programme documents are linkable from here on
            details = {**links.details(), **found.details()}
            if budgets:
                details["budget_link"] = links.budget_details()
                if details["budget_link"].get("clear_failed"):
                    details["partial"] = True
            if aligned:
                details["sequences_aligned"] = aligned
            documents = _write_documents(name, items, links, found)  # a budget record under the PD it names
            links.clear()
            return {**details, **documents}

        return _run(run, body)

    sync.__name__ = f"sync_{name}"
    return sync


# A count of 5,000 or more that is a multiple of 1,000 reads as a limit of the Datamart's answer, not what it
# holds. (The partner reports read on 20 Oct 2026 was whole: 777,535 announced and read, every country's rows,
# 9,970 of them Lebanon's: no limit near 10,000.)
CAP_FLOOR, CAP_ROUND = 5_000, 1_000


def capped(announced: int | None) -> bool:
    """Whether a count the Datamart announced looks like a limit of its answer: :data:`CAP_FLOOR` or more
    and a multiple of :data:`CAP_ROUND`."""
    return announced is not None and announced >= CAP_FLOOR and announced % CAP_ROUND == 0


def read_whole(announced: int | None, read: int, repeated: int = 0, proven: int = 0) -> bool:
    """Whether a read got every record the Datamart announced (the ``count`` of its first page). ``read``
    counts distinct records (a record returned twice counts once), ``repeated`` the records returned again,
    ``proven`` those of them whose every copy came identical in one answer (``windows.Copies``: the Datamart
    holds them that many times, as one answer never lists a row twice). With fewer records than announced,
    copies proven included (a page cut short), or any record returned again not proven (rows moving on the
    Datamart while its pages were read: a record returned again may stand for one never returned, whatever
    the count), the rows it did not return are kept, not removed. Each record received then came at most as
    many times as the Datamart holds it, so the records received, copies included, add up to what it
    announced only when none is missing. A round count (:func:`capped`) is a limit of the answer, not what
    the Datamart holds: such a read is never whole, whatever it read. A list without a count, with no record
    returned again but proven, is taken as whole."""
    return repeated <= proven and not capped(announced) and (announced is None or read + proven >= announced)


# The signs, in a dataset's previous finished run, that its read looked cut, in the order they are told
# (:func:`cut_sign`): any one makes the next read of 5,000 records or more be windowed (``windows.Reading``)
CUT_SIGNS = ("cap_suspected", "whole_count_capped", "pages_cut", "windows_cut", "read_short")


def cut_sign(details: dict[str, Any]) -> str:
    """The first sign (:data:`CUT_SIGNS`) that the read a run's ``details`` describe looked cut, "" for none:
    an answer that looked cut (``cap_suspected``: a round count read once, the ends test, no count); windows
    that held more than the Datamart's count (``whole_count_capped``); a page shorter than the first, not the
    last (``pages_cut``); a window read to its end holding fewer than it announced (``windows_cut``); a read
    made once that returned fewer records than announced (``read_short``: only when the run recorded ``read``;
    the records that came twice count with it, as a read whose records came twice and as many others did not
    is pages shifting, not a cut). A read windowed for a reason that shows no cut (a count that changed,
    records twice, a plan stopped short) records none, so the next night reads plain."""
    details = details if isinstance(details, dict) else {}
    for sign in CUT_SIGNS[:2]:
        if details.get(sign):
            return sign
    for sign in CUT_SIGNS[2:4]:
        if (_int(details.get(sign)) or 0) > 0:
            return sign
    read, announced = details.get("read"), details.get("datamart_count")
    once = not details.get("window_bound")
    if once and all(isinstance(n, int) and not isinstance(n, bool) for n in (read, announced)):
        if read + (_int(details.get("duplicates_read")) or 0) < announced:
            return "read_short"
    return ""


def _finished_runs(name: str, limit: int = 20):
    """The latest finished (SUCCEEDED or PARTIAL) runs of ``name``, newest first, as (start, details)."""
    return (
        SyncRun.objects.filter(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target=name,
            status__in=(SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL),
        )
        .order_by("-started_at", "-pk")
        .values_list("started_at", "details")[:limit]
    )


def last_details(name: str) -> dict[str, Any]:
    """The details of the latest finished run of ``name`` that read something (no ``empty_response``); ``{}``
    when none. One indexed query, the filter :func:`held_whole_from` uses."""
    for _, details in _finished_runs(name):
        details = details if isinstance(details, dict) else {}
        if not details.get("empty_response"):
            return details
    return {}


def _removal_skipped(reading: windows.Reading) -> dict[str, Any]:
    """The details of a read not whole (``windows.Reading.whole``): fewer distinct records than the Datamart
    announced, some twice and not proven its own copies (``duplicates_read``), an answer that looks cut
    (``cap_suspected``), or a window not read whole (the reading's details say which): nothing it did not
    return is removed, and the run ends PARTIAL (``finish_by_counts``), so Data health says so; the next
    whole read removes them."""
    found: dict[str, Any] = {"removal_skipped": True, "read": reading.read, "partial": True}
    if reading.repeated:
        found["duplicates_read"] = reading.repeated
    if reading.cap_suspected:
        found["cap_suspected"] = reading.cap_suspected
    return found


# ------------------------------------------------------------------- partner reports no longer sent
# what keep_unsent reads of a partner report row: its Datamart id, its report, its indicator and its place
UNSENT_FIELDS = (
    "datamart_id",
    *rc.IDENTITY_FIELDS,
    "progress_report",
    "indicator_report",
    "etools_indicator_id",
    "indicator",
    "pd_output",
    "p_code",
    "location",
)


def same_place(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two partner report rows at one place: the same P-code; when either has none, the same location name."""
    first, second = (a.get("p_code") or "").strip().upper(), (b.get("p_code") or "").strip().upper()
    if first and second:
        return first == second
    return rc.norm(a.get("location")) == rc.norm(b.get("location"))


def replaced_rows(
    stale: list[dict[str, Any]], seen: set[int], started: dt.datetime, held: set[int]
) -> list[int]:
    """The pks of the ``stale`` rows (a whole read no longer returned them) whose replacement tonight's read
    brought, a row of the same progress report (``reached.report_key``) and indicator
    (``reached.indicator_key``): a row under any Datamart id that carries the stale row's own PRP
    indicator report id at the same place (:func:`same_place`): its copy, also when NeuroDB first wrote it
    on an earlier night (a read that was not whole, or a read that returned both); or a row under a new
    Datamart id (one NeuroDB did not hold before the read, ``held``) at the same place, or under a new PRP
    indicator report id while no row of tonight's read carries the stale row's own (PRP gave the indicator
    report a new id: every place of the old one is superseded). Under another PRP id, a row returned again
    under a Datamart id NeuroDB held never replaces another: two PD indicators of one report can share a
    title and an output and carry no eTools id (one indicator to ``indicator_key``), told apart by PRP's
    id alone. For the same reason a new row under a PRP id another row NeuroDB held carries (a stale one,
    or one returned again) is that other indicator report, never the replacement of this one. A place
    tonight's read did not return, under the same PRP id, is never replaced: a read of the records changed
    since a date can return one place of a report and not another. Tonight's rows are read for the
    programme documents and period ends of the stale rows only."""
    if not stale:
        return []
    pds = {row["intervention_id"] for row in stale if row["intervention_id"] is not None}
    refs = {row["pd_reference_number"] for row in stale if row["intervention_id"] is None}
    ends = {row["period_end"] for row in stale}
    by_end = models.Q(period_end__in=[end for end in ends if end is not None])
    if None in ends:
        by_end |= models.Q(period_end=None)
    tonight = dm.ReportedIndicator.objects.filter(synced_at__gte=started, datamart_id__in=seen).filter(
        models.Q(intervention_id__in=pds) | models.Q(intervention=None, pd_reference_number__in=refs), by_end
    )

    def key(row: dict[str, Any]) -> tuple[str, str]:
        return rc.report_key(row), rc.indicator_key(row)

    def prp_id(row: dict[str, Any]) -> str:
        return str(row["indicator_report"] or "")

    found: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    # by report and indicator, the PRP ids of the rows under a Datamart id NeuroDB held (stale, or sent again)
    taken: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in tonight.order_by().values(*UNSENT_FIELDS):
        found[key(row)].append(row)
        if row["datamart_id"] in held:
            taken[key(row)].add(prp_id(row))
    for row in stale:
        taken[key(row)].add(prp_id(row))
    replaced = []
    for row in stale:
        own, sent, others = prp_id(row), found.get(key(row), []), taken[key(row)]
        new = [
            other
            for other in sent
            if (other["datamart_id"] not in held and (prp_id(other) == own or prp_id(other) not in others))
            # a copy NeuroDB first wrote on an earlier night: its own PRP id at its place
            or (own and prp_id(other) == own and same_place(row, other))
        ]
        if not new:
            continue
        if any(same_place(row, other) for other in new) or (
            own and all(prp_id(other) != own for other in sent)
        ):
            replaced.append(row["pk"])
    return replaced


def keep_unsent(
    scoped: Callable[[], models.QuerySet],
    seen: set[int],
    started: dt.datetime,
    unsent_before: set[int],
    held: set[int],
) -> dict[str, Any]:
    """What a whole read of the partner reports does with the rows of its scope it no longer returned: those
    tonight's read brought the replacement of (:func:`replaced_rows`: a copy under any Datamart id
    carrying their PRP id at their place, or a row under a new Datamart id; ``held``: the Datamart ids
    NeuroDB held before the read) are removed (``removed``, ``replaced``), every other is kept, counted on
    every page, and marked no longer sent from ``started`` (``no_longer_sent_new``; a row marked already
    keeps its first mark, and its ``synced_at`` stays the moment it was last sent). Then the marked rows
    of the scope (``no_longer_sent``), those marked before the read that it returned (``sent_again``), and
    the marked reports whose programme document, type and number a row still sent holds under another
    period (``same_number_other_period``, their rows ``…_rows``). Nothing is held a night
    (:func:`removal_waits`): nothing is lost, and a mark never makes the run partial."""
    from django.db.models import Count

    from neurodb.datamart import reporting

    stale = scoped().exclude(datamart_id__in=seen)
    replaced = replaced_rows(list(stale.order_by().values("pk", *UNSENT_FIELDS)), seen, started, held)
    removed = 0
    if replaced:
        _, deleted = dm.ReportedIndicator.objects.filter(pk__in=replaced).delete()
        removed = deleted.get(dm.ReportedIndicator._meta.label, 0)
    new = stale.filter(not_sent_since=None).update(not_sent_since=started)
    marked = scoped().exclude(not_sent_since=None).order_by()
    other = reporting.sent_for_another_period(marked).aggregate(
        reports=Count(reporting.REPORT_KEY, distinct=True), rows=Count("pk")
    )
    return {
        "removed": removed,
        "replaced": removed,
        "no_longer_sent_new": new,
        "no_longer_sent": marked.count(),
        "sent_again": len(unsent_before & seen),
        "same_number_other_period": other["reports"] or 0,
        "same_number_other_period_rows": other["rows"] or 0,
    }


# A whole read that would remove more than half of the rows held, and at least this many, waits one night: a
# Datamart table reloaded while it was read (its ETL empties and refills tables) can announce and return a
# consistent smaller set, which counts that agree cannot catch
HOLD_FLOOR = 1_000
# the night: a held removal is released only by a run this long after the first run that held it (a sync
# pressed again minutes later, while the table is still reloading, holds it again)
HOLD_HOURS = 20


def removal_waits(name: str, n: int, held: int) -> bool:
    """Whether a whole read of ``name`` removes none of the ``n`` rows (of ``held``) it no longer returned
    tonight: more than half of those held and at least :data:`HOLD_FLOOR`, unless the finished runs of the
    dataset have held their removal, one after the other, since :data:`HOLD_HOURS` ago or longer (the whole
    read a night after the first hold removes them; one made earlier holds them again)."""
    if n < HOLD_FLOOR or n * 2 <= held:
        return False
    first = None  # the start of the first run of the holds in a row, newest first
    for started, details in (
        SyncRun.objects.filter(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target=name,
            status__in=(SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL),
        )
        .order_by("-started_at", "-pk")
        .values_list("started_at", "details")[:50]
    ):
        if not (isinstance(details, dict) and details.get("removal_held")):
            break
        first = started
    return first is None or timezone.now() - first < dt.timedelta(hours=HOLD_HOURS)


def history_cut(details: dict[str, Any]) -> bool:
    """Whether a finished partner reports run proves no history whole from the date it read from: read in date
    windows with no ``whole_from`` (none proven), in id windows one of which was not read whole
    (``removal_skipped``), or read once with an answer that looked cut (a round count, ``cap_suspected``).
    :func:`held_whole_from` and ``reached.reports_read_from`` read a run by this one rule."""
    bound = details.get("window_bound")
    if bound == "id":
        return bool(details.get("removal_skipped"))
    if bound:
        return not details.get("whole_from")
    return capped(details.get("datamart_count")) or bool(details.get("cap_suspected"))


def held_whole_from(name: str) -> dt.date | None:
    """The first period start the previous finished run of ``name`` left held whole (``windows.Reading.
    whole_from`` chains from it): its ``whole_from``, else its ``read_from`` (a run read before windows, or in
    id windows every one whole); none when it proves no history whole (:func:`history_cut`: its history was
    cut). None without such a run."""
    for details in (
        SyncRun.objects.filter(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target=name,
            status__in=(SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL),
        )
        .order_by("-started_at", "-pk")
        .values_list("details", flat=True)[:20]
    ):
        details = details if isinstance(details, dict) else {}
        if details.get("empty_response"):
            continue  # nothing was read: the rows held are an earlier run's
        if history_cut(details):
            return None
        try:
            text = details.get("whole_from") or details.get("read_from")
            return dt.date.fromisoformat(str(text or "")[:10])
        except ValueError:
            return None
    return None


def sync_dataset(name: str) -> Callable[..., SyncRun]:
    dataset, spec = DATASETS[name]

    def sync(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
        def body() -> dict[str, Any]:
            links.missing.clear()
            links.fr_link.clear()
            links.fr_counted.clear()
            if spec.model is dm.FundsReservation:  # the FR headers as they stand now: synced just before
                links._header_numbers = None
            seen: set[int] = set()
            linked_before = _funded_pcas() if spec.model is dm.FundsReservation else set()
            since = spec.since() if spec.since else None  # once: the request and the rows it replaces agree
            params = spec.params(since) if spec.params else None

            def scoped() -> models.QuerySet:
                rows = spec.model.objects.all()
                return spec.scope(rows, since) if spec.scope else rows

            # the rows this read replaces, counted before it writes (a large "removed" reads against it)
            held_before = scoped().count()
            # the rows marked no longer sent before it: those it returns are sent again (details.sent_again)
            unsent_before = (
                set(scoped().exclude(not_sent_since=None).values_list("datamart_id", flat=True))
                if spec.keep_unsent
                else set()
            )
            area = None
            if name in catalogue.AREA_FILTERS:  # by business area once the Datamart proves it applies it
                code = resolve_business_area().code  # set or held: no request
                named_by = catalogue.AREA_FILTERS[name].named_by
                not_named = rows_not_named(scoped(), named_by, code) if code else 0
                area = prove_area_filter(
                    client,
                    name,
                    dataset,
                    params or {},
                    country=True,
                    code=code,
                    held=held_before,
                    held_not_named=not_named,
                )
                params = {**(params or {}), **area.params()}
            reading = windows.Reading(client, dataset, params, date_key=spec.windows, name=name)
            # the Datamart ids held before the read: a row under one of them replaces only the row whose PRP
            # id it carries at its place, its copy (keep_unsent)
            held_ids = (
                set(spec.model.objects.values_list("datamart_id", flat=True)) if spec.keep_unsent else set()
            )
            found = OfCountry(reading, area)
            from neurodb.datamart import raw  # lazy: Raw eTools data reads what this sync writes

            fields = raw.KeyCount(name)  # the fields of the records kept (details.keys_kept)
            process_items(run, found, lambda item: upsert_record(spec, item, links, seen, fields), _label)
            if seen:
                # another country's rows first (every one, whatever its date), then the rest of the scope
                # the Datamart no longer returns
                other = remove_other_country(spec.model, found.country, seen)
                if found.whole and spec.keep_unsent:  # nothing is lost: kept, counted and marked
                    details = {
                        **keep_unsent(scoped, seen, run.started_at, unsent_before, held_ids),
                        **links.details(),
                    }
                elif found.whole:
                    stale = scoped().exclude(datamart_id__in=seen)
                    n = stale.count()
                    if n and removal_waits(name, n, held_before):  # a large removal waits one night
                        details = {"removed": 0, "removal_held": n, "partial": True, **links.details()}
                    else:
                        _, deleted = stale.delete() if n else (0, {})
                        details = {"removed": deleted.get(spec.model._meta.label, 0), **links.details()}
                else:  # fewer records than announced, some twice, a cut answer, a window not whole: they stay
                    details = {"removed": 0, **_removal_skipped(reading), **links.details()}
                    if area and area.strays:  # another business area came through the filter
                        details["area_strays"] = area.strays
                if other:
                    details["other_country_removed"] = other
            else:  # an empty answer (a wrong country name?) never empties the table
                details = {"removed": 0, "empty_response": True, **links.details()}
            if spec.keep_unsent and "no_longer_sent" not in details:
                # a read not whole, or an empty answer, marks nothing: the rows marked before stay kept and
                # counted, and the run says how many (check 10, Data health), as a whole read does
                details["no_longer_sent"] = scoped().exclude(not_sent_since=None).count()
            details.update(found.details(), held_before=held_before)
            if seen:
                details["keys_kept"] = fields.details()
            if area is not None:
                details.update(area.details())
            # the count of the first page (never the last window's), and the windows when read in windows
            details.update(reading.details())
            if spec.windows and reading.windowed and seen:
                start = reading.whole_from(held_whole_from(name))
                if start is not None:
                    details["whole_from"] = start.isoformat()
            if spec.link is _link_funds and seen:  # how the lines or headers were linked to their PD
                details["pd_link"] = dict(links.fr_link)
            if spec.model is dm.FundsReservation:
                details["donor_sets"] = _refresh_donor_sets(linked_before)
            if spec.model is dm.ReportedIndicator:
                details["cp_outputs"] = derive_cp_outputs()
                details["read_from"] = since.isoformat() if since else ""
                if seen and since:
                    # where the rows held start, after the removal (or the removal held one night): the
                    # Datamart may hold no report as early as the date asked (reached.history_from)
                    feed = (
                        scoped().filter(period_start__gte=since).aggregate(first=models.Min("period_start"))
                    )
                    if feed["first"]:
                        details["feed_from"] = feed["first"].isoformat()
                try:  # the labels never fail the reports: a failure is said, and the run is partial
                    details["disaggregation_labels"] = sync_disaggregation_labels(client)
                except Exception as exc:
                    logger.warning("etools datamart: disaggregation labels not read: %s", exc)
                    details["disaggregation_labels"] = {"error": str(exc)[:300]}
                    details["partial"] = True
            if spec.model in (dm.ReportedIndicator, dm.PDIndicator):
                # the proof of the tie-breakers that tell apart indicators sharing a title and an output,
                # read by every page from this run (identity.calibration), and the PD indicators the pages
                # leave out marked (eTools marks them inactive and no partner report row names them:
                # identity.listed); it never fails the run
                try:
                    with transaction.atomic():
                        details["identity"] = identity.calibrate_and_list()
                except Exception as exc:
                    logger.warning("etools datamart: indicator identity not calibrated: %s", exc)
                    details["identity_error"] = str(exc)[:300]
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
            found = OfCountry(windows.Reading(client, dataset, name=name))
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
# Where a run's business area code came from (``details.business_area_from``): set by a person (the admin's,
# the hosting team's) or found by NeuroDB (:data:`FOUND`: the workspace it holds, the Datamart's workspaces,
# the partner report rows)
FROM_ENVIRONMENT, FROM_ADMIN, FROM_WORKSPACES = "environment", "admin", "workspaces"
FROM_WORKSPACE_HELD, FROM_PARTNER_REPORTS = "workspace_held", "partner_reports"
FOUND = (FROM_WORKSPACE_HELD, FROM_WORKSPACES, FROM_PARTNER_REPORTS)
CODES_SEEN = 10  # the business area codes a run lists, the most frequent first (counts only)
NO_CODE = "(none)"  # how a record that names no business area is counted among them
AREA_CODE = re.compile(r"[0-9]{1,20}")  # a business area code is digits (2490), never a country's name
AREA_VALUES = 10  # the distinct business_area values of the partner report rows read, the most frequent first
SET_ASIDE = 40  # the characters kept of a value set aside (it is a setting, never a record's value)
# The start of a PRP dataset's error when no business area code is set or found (_no_code_error), and the end
# of the guard's when a value that is not a code reaches the request: Raw eTools data says them in its words
ENTER_CODE = "enter the business area code in "
NEVER_SENT = " is not a business area code: it is never sent"


def area_code(value: Any) -> str:
    """A business area code as compared: ``"2490"``, ``2490``, ``" 2490 "``, ``2490.0``, ``"2490.0"`` and
    ``{"code": "2490"}`` all read ``"2490"``; a missing or empty value reads ``""``."""
    if isinstance(value, dict):
        value = next((value[k] for k in ("code", "business_area_code") if value.get(k) not in (None, "")), "")
    if value is None or isinstance(value, bool):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text


def is_area_code(value: Any) -> bool:
    """Whether a value is a business area code: digits once read by :func:`area_code` ("2490", 2490.0,
    ``{"code": "2490"}``), never "Lebanon", "LB", "2490; 2340" or "". Only a code is ever sent as the
    filter."""
    return bool(AREA_CODE.fullmatch(area_code(value)))


def not_a_code(value: Any) -> str:
    """How a value that is not a business area code is named: a name (no digit, as "Lebanon"), or not."""
    if any(c.isdigit() for c in str(value)):
        return "not a business area code"
    return "a name, not a business area code"


@dataclass(frozen=True)
class AreaCode:
    """The business area code the PRP datasets are read with and where it came from (``source``, one of the
    ``FROM_`` values; both "" when none is found). ``set_aside``: the values a person set that are not codes,
    by where they came from, as given (:data:`SET_ASIDE` characters); ``rows``: for a code found on the
    partner report rows, the rows carrying it and the rows held."""

    code: str = ""
    source: str = ""
    set_aside: dict[str, str] = field(default_factory=dict)
    rows: tuple[int, int] = (0, 0)

    @property
    def found(self) -> bool:
        """Found by NeuroDB, not set by a person."""
        return self.source in FOUND

    def details(self) -> dict[str, Any]:
        """The run's details of the code: ``business_area``, ``business_area_from``, and when given
        ``business_area_set_aside``, ``business_area_rows`` (``[carrying it, held]``) and
        ``business_area_found``."""
        found: dict[str, Any] = {"business_area": self.code, "business_area_from": self.source}
        if self.set_aside:
            found["business_area_set_aside"] = dict(self.set_aside)
        if self.source == FROM_PARTNER_REPORTS:
            found["business_area_rows"] = list(self.rows)
        if self.found:
            found["business_area_found"] = True
        return found


def business_area_setting() -> AreaCode:
    """The business area code a person set for the PRP datasets, and where: the admin's (Admin → Data and sync
    → eTools Datamart: PRP business area code) first, as an administrator enters it later and on purpose,
    then the hosting team's ``ETOOLS_DATAMART_BUSINESS_AREA``. Each counts only when it is a code
    (:func:`is_area_code`); a value that is not (a country's name) is never sent: it is set aside, with
    where it came from. Reads the database and the settings only."""
    stored = DatamartSetting.objects.filter(pk=1).values_list("prp_business_area", flat=True).first()
    code, came_from, set_aside = "", "", {}
    for source, value in ((FROM_ADMIN, stored), (FROM_ENVIRONMENT, settings.ETOOLS_DATAMART_BUSINESS_AREA)):
        if is_area_code(value):
            if not code:
                code, came_from = area_code(value), source
        elif area_code(value):
            set_aside[source] = " ".join(str(value).split())[:SET_ASIDE]
    return AreaCode(code, came_from, set_aside)


def workspace_code(workspaces: Iterable[Any], country: str) -> str:
    """The business area code of the workspaces of ``country``'s name (names compared with case and spaces
    aside): "" unless every one of them that has a code names the same one, so a stale workspace never
    gives its code."""
    wanted = country_name(country)
    codes = {
        area_code(data.get("business_area_code"))
        for data in workspaces
        if isinstance(data, dict) and country_name(data.get("name")) == wanted
    }
    codes = {code for code in codes if is_area_code(code)}
    return codes.pop() if len(codes) == 1 else ""


def held_business_area(country: str) -> str:
    """The business area code of the country's workspace as the last sync of ``workspaces`` stored it
    (:func:`workspace_code`): nothing is asked of the Datamart."""
    held = dm.DatamartDocument.objects.filter(dataset="workspaces").values_list("data", flat=True)
    return workspace_code(held, country)


def _json_value(text: Any) -> Any:
    """A JSON value read as text (``KT``): an object's text read back as the object, the rest as it is."""
    if isinstance(text, str) and text.lstrip().startswith("{"):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def reported_business_area() -> tuple[str, int, int]:
    """(code, rows carrying it, rows held): the business area code most partner report rows held carry
    (``data.business_area``, kept by the sync), when it is a code and more than half of the rows carry it,
    else ``("", 0, held)``. The rows are the country's (another country's are never written, and removed), so
    their code is the country office's. One grouped query over the :data:`AREA_VALUES` most frequent values,
    merged by code (" 2490.0" and ``{"code": "2490"}`` read 2490)."""
    groups = (
        dm.ReportedIndicator.objects.order_by()
        .annotate(_ba=KT("data__business_area"))
        .values("_ba")
        .annotate(n=models.Count("pk"))
        .order_by("-n")[:AREA_VALUES]
    )
    codes: Counter[str] = Counter()
    for group in groups:
        codes[area_code(_json_value(group["_ba"]))] += group["n"]
    if not codes:  # no row held
        return "", 0, 0
    held = dm.ReportedIndicator.objects.count()
    code, n = codes.most_common(1)[0]
    return (code, n, held) if is_area_code(code) and n * 2 > held else ("", 0, held)


def found_business_area(client: DatamartClient | None = None) -> AreaCode:
    """A business area code NeuroDB finds by itself, and where: the workspace of the country it holds (the
    previous sync of ``workspaces``), else the Datamart's workspaces (only with a ``client``: the sync, never
    a page; a failed request is logged and the next step taken), else the code most partner report rows
    held carry (:func:`reported_business_area`). ``AreaCode()`` when none gives one."""
    country = (settings.ETOOLS_DATAMART_COUNTRY or "").strip()
    code = held_business_area(country)
    if code:
        return AreaCode(code, FROM_WORKSPACE_HELD)
    if client is not None:
        try:
            code = workspace_code(client.list("workspaces", country=False), country)
        except IntegrationError as exc:
            logger.warning("etools datamart: the workspaces could not be read for the business area: %s", exc)
        if code:
            return AreaCode(code, FROM_WORKSPACES)
    code, n, held = reported_business_area()
    return AreaCode(code, FROM_PARTNER_REPORTS, rows=(n, held)) if code else AreaCode()


def resolve_business_area(client: DatamartClient | None = None) -> AreaCode:
    """The business area code the PRP datasets are read with, and where it came from: the code a person set
    (:func:`business_area_setting`: the admin's, then the hosting team's, each only when it is a code), else
    the one NeuroDB finds (:func:`found_business_area`). The values set aside are kept with it."""
    chosen = business_area_setting()
    if chosen.code:
        return chosen
    found = found_business_area(client)
    return AreaCode(found.code, found.source, chosen.set_aside, found.rows)


# how a value set aside is named, by where it came from
SET_BY = {
    FROM_ENVIRONMENT: "the hosting team's ETOOLS_DATAMART_BUSINESS_AREA",
    FROM_ADMIN: "the code entered in the admin",
}


def set_aside_words(set_aside: dict[str, str]) -> str:
    """The values set aside, in words: "the hosting team's ETOOLS_DATAMART_BUSINESS_AREA is “Lebanon”, a
    name, not a business area code" ("" when none was)."""
    return " and ".join(
        f"{SET_BY.get(source, source)} is “{value}”, {not_a_code(value)}"
        for source, value in set_aside.items()
    )


def _no_code_error(choice: AreaCode, country: str) -> str:
    """The error of a PRP dataset's run when no business area code is set or found: where to enter it first
    (the pages that cut an error short, checks 8, 9 and 11 at 140 characters, keep it), then what was set
    aside and where NeuroDB looked."""
    looked = "the workspace it holds, the Datamart's workspaces or the partner report rows"
    where = "Admin → Data and sync → eTools Datamart: PRP business area code"
    if choice.set_aside:
        aside = set_aside_words(choice.set_aside)
        return f"{ENTER_CODE}{where}: {aside} (a code is digits, as 2490), and NeuroDB found none in {looked}"
    return (
        f"{ENTER_CODE}{where} (none is set for {country!r}, and NeuroDB found none in {looked}), or ask the "
        "hosting team to set ETOOLS_DATAMART_BUSINESS_AREA"
    )


class CountryScope:
    """The country filter of the non-``country_name`` datasets, resolved once per run."""

    def __init__(self, client: DatamartClient) -> None:
        self.client = client
        self.choice: AreaCode | None = None  # the business area code and where it came from, once resolved

    @property
    def country(self) -> str:
        return (settings.ETOOLS_DATAMART_COUNTRY or "").strip()

    @property
    def business_area_from(self) -> str:
        return self.choice.source if self.choice else ""

    def business_area(self) -> str:
        """The country's business area code (:func:`resolve_business_area`, with the Datamart's workspaces
        asked when nothing set or held gives one); the run fails, saying where to enter it, when none is
        found."""
        if self.choice is None:
            choice = resolve_business_area(self.client)
            if not choice.code:
                raise IntegrationError(_no_code_error(choice, self.country))
            self.choice = choice
        return self.choice.code

    def request(self, spec: catalogue.Source) -> tuple[dict[str, Any], bool]:
        """(query parameters, whether to add the country_name filter) for a dataset. A business area that is
        not a code is never sent (a guard: :func:`resolve_business_area` gives codes only)."""
        if spec.scope == "business_area":
            code = self.business_area()
            if not is_area_code(code):
                raise IntegrationError(f"{code[:SET_ASIDE]!r}{NEVER_SENT}")
            return {spec.filter_key: code}, False
        if spec.scope == "lookup":
            return {}, False
        return {}, True

    def keeps(self, spec: catalogue.Source, item: dict[str, Any]) -> bool | None:
        """Whether a record belongs to the country: a filter the API ignored must never let another
        country's records in. A business-area record is compared by its code (:func:`area_code`), and is
        dropped when it also names another country (a code NeuroDB found is inferred: another office's records
        carrying it never pass as this country's); a business area named by its name ("Lebanon") is compared
        as a country's name; a record naming no business area, by its country name when it carries one. One
        that names neither gives None: it is kept (the request's filter chose it, as :func:`other_country`
        keeps a record that names no country) and counted apart."""
        if spec.scope == "business_area":
            value = item.get(spec.filter_key)
            if is_area_code(value):
                return area_code(value) == self.business_area() and not other_country(item, self.country)
            if area_code(value):
                named = country_name(value) == country_name(self.country)
                return named and not other_country(item, self.country)
            if country_name(item.get("country_name")):
                return not other_country(item, self.country)
            return None
        if spec.scope == "lookup":
            return not spec.keep or str(item.get(spec.keep, "")).strip().lower() == self.country.lower()
        value = item.get("country_name")
        return value in (None, "") or str(value).strip().lower() == self.country.lower()


# --------------------------------------------------------- the business area filter, proven before it is used
# The datasets of ``catalogue.AREA_FILTERS`` (the partner reports, PRP's progress reports) are read by
# business area only once the Datamart proves, that night, that it applies the filter: it ignores country_name
# on prp/datareport and business_area_code on PRP's progress reports, so a filter is never taken on trust.
UNFILTERED_EVERY = dt.timedelta(days=7)  # a read with no business area filter at least this often
# the seventh night is due whatever minute its run starts (sync_all runs the datasets one after the other, so
# a dataset's run starts minutes earlier or later from night to night): a week less the night (HOLD_HOURS)
UNFILTERED_DUE = UNFILTERED_EVERY - dt.timedelta(hours=HOLD_HOURS)
AREA_PAGE = 50  # the records of the one page each filter key is proven on
# why a read took its business area filter or none (``area_filter.why``): the first that applies, in order
AREA_NO_CODE, AREA_HELD_NOT_NAMED, AREA_WEEKLY = "no_code", "held_not_named", "weekly"
AREA_UNFILTERED_FAILED, AREA_PROVEN, AREA_NONE_PROVEN = "unfiltered_failed", "proven", "none_proven"
AREA_WHY = {
    AREA_NO_CODE: "no business area code is set or found",
    AREA_HELD_NOT_NAMED: "rows held name no business area code or another one",
    AREA_WEEKLY: "once a week every record is read",
    AREA_UNFILTERED_FAILED: "the count with no business area filter could not be asked",
    AREA_PROVEN: "the Datamart applies the filter",
    AREA_NONE_PROVEN: "the Datamart applies none of the filters tried",
}
# what the proof found of one filter key (``area_filter.tried[].verdict``)
AREA_VERDICTS = {
    "proven": "is applied",
    "nothing": "finds nothing",
    "ignored": "is ignored (as many records with it as without)",
    "fewer_than_held": "finds fewer records than NeuroDB holds",
    "other_codes": "lets other business areas through",
    "not_named": "returns records that name no business area",
    "refused": "is refused",
    "failed": "failed",
    "strays_this_week": "let another business area through this week",
}


@dataclass
class AreaFilter:
    """The business area filter of one read of a dataset of ``catalogue.AREA_FILTERS``
    (:func:`prove_area_filter`: ``key`` "" for none, and why), and what the read found under a filter: the
    records of another business area or country it let through (``strays``: never written, and the read is not
    whole) and those naming no code (``unnamed``: written and counted, so the next night reads with no
    filter). Key names, codes and counts only."""

    name: str
    code: str
    key: str = ""
    why: str = ""
    tried: list[dict[str, Any]] = field(default_factory=list)
    unfiltered: int | None = None  # the count of the sync's own query with no business area filter
    held: int = 0  # the rows held in the sync's scope
    held_not_named: int = 0  # those naming no business area code or another one
    strays: int = 0
    unnamed: int = 0

    def params(self) -> dict[str, Any]:
        """The filter added to the sync's own query: ``{key: code}``, or none."""
        return {self.key: self.code} if self.key else {}

    def sees(self, item: dict[str, Any]) -> bool | None:
        """Whether a record names the code (its key ``named_by``): True; another code, or another country's
        name as its business area: False (a stray under a filter); none, or this country's name: None."""
        value = item.get(catalogue.AREA_FILTERS[self.name].named_by)
        if is_area_code(value):
            return area_code(value) == self.code
        if not area_code(value) or country_name(value) == country_name(settings.ETOOLS_DATAMART_COUNTRY):
            return None
        return False

    def details(self) -> dict[str, Any]:
        return {
            "area_filter": {
                "key": self.key,
                "code": self.code,
                "why": self.why,
                "unfiltered": self.unfiltered,
                "held": self.held,
                "held_not_named": self.held_not_named,
                "tried": list(self.tried),
                "strays": self.strays,
                "unnamed": self.unnamed,
            }
        }


def judge_area_key(
    count: int | None,
    records: list[dict[str, Any]],
    *,
    unfiltered: int | None,
    held: int,
    code: str,
    named_by: str,
) -> str:
    """The verdict on one business area filter key (:data:`AREA_VERDICTS`), from the count it announced and
    the records of its page: ``proven`` when it finds some but fewer than with no filter (``unfiltered``; with
    no count to compare with it is taken as ignored), no fewer than the rows held (a filter that finds fewer
    would remove some), and every record on its page names ``code`` under ``named_by``; else ``nothing``,
    ``ignored``, ``fewer_than_held``, ``other_codes`` (its page names another code: the filter is compared
    with another field) or ``not_named``. The sync and the probe judge alike."""
    if not count:
        return "nothing"
    if unfiltered is None or count >= unfiltered:
        return "ignored"
    if count < held:
        return "fewer_than_held"
    named = [area_code(record.get(named_by)) for record in records]
    if any(c and c != code for c in named):
        return "other_codes"
    if not named or not all(named):
        return "not_named"
    return "proven"


def rows_not_named(queryset: models.QuerySet, json_key: str, code: str) -> int:
    """The rows of ``queryset`` whose record (``data``) names no business area code under ``json_key``, or
    another than ``code``: one grouped query, merged by code (" 2490.0" and ``{"code": "2490"}`` read 2490).
    """
    groups = (
        queryset.order_by().annotate(_ba=KT(f"data__{json_key}")).values("_ba").annotate(n=models.Count("pk"))
    )
    return sum(g["n"] for g in groups if area_code(_json_value(g["_ba"])) != code)


def last_unfiltered(name: str) -> dt.datetime | None:
    """The start of the latest finished run of ``name`` that read something with no business area filter (a
    run without ``area_filter``, made before it was recorded, or with no key): a weekly read that failed
    part-way is not one."""
    for started, details in _finished_runs(name):
        details = details if isinstance(details, dict) else {}
        if details.get("empty_response"):
            continue
        area = details.get("area_filter")
        if not isinstance(area, dict) or not area.get("key"):
            return started
    return None


def _strayed_keys(name: str) -> set[str]:
    """The filter keys that let a record of another business area through on a finished run of ``name`` in the
    last :data:`UNFILTERED_EVERY` (the run a week ago not counted, whatever minute it started:
    :data:`UNFILTERED_DUE`): they are not tried again before."""
    since, keys = timezone.now() - UNFILTERED_DUE, set()
    for started, details in _finished_runs(name):
        if started < since:
            break
        area = details.get("area_filter") if isinstance(details, dict) else None
        if isinstance(area, dict) and area.get("key") and (_int(area.get("strays")) or 0) > 0:
            keys.add(str(area["key"]))
    return keys


def prove_area_filter(
    client: DatamartClient,
    name: str,
    path: str,
    base: dict[str, Any],
    *,
    country: bool,
    code: str,
    held: int,
    held_not_named: int,
) -> AreaFilter:
    """The business area filter tonight's read of ``name`` takes (its sync's own query ``base``), the first
    rule that applies deciding: no code (``no_code``); a row held naming no code or another one
    (``held_not_named``: a filter cannot return it, and a whole filtered read would remove it); no read with
    no filter in the last :data:`UNFILTERED_EVERY` (``weekly``: a record of the country naming no code, or a
    code that changed, is seen at least once a week; the seventh night whatever minute its run starts,
    :data:`UNFILTERED_DUE`); the count with no filter failing
    (``unfiltered_failed``); else each key of ``catalogue.AREA_FILTERS`` in turn on one page of
    :data:`AREA_PAGE` records (a key that let another business area through this week skipped; a refused or
    failed request recorded, the next key tried), the first :func:`judge_area_key` proves taken (``proven``),
    or none (``none_proven``): an ignored or refused key is never sent with the read. A failed request never
    fails the run."""
    area = AreaFilter(name, code, held=held, held_not_named=held_not_named)
    spec = catalogue.AREA_FILTERS[name]
    if not code:
        area.why = AREA_NO_CODE
        return area
    if held_not_named:
        area.why = AREA_HELD_NOT_NAMED
        return area
    last = last_unfiltered(name)
    if last is None or timezone.now() - last >= UNFILTERED_DUE:
        area.why = AREA_WEEKLY
        return area
    try:
        area.unfiltered = client.count(path, base, country=country)
    except IntegrationError as exc:
        logger.warning("etools datamart %s: the count with no business area filter failed: %s", name, exc)
    if area.unfiltered is None:
        area.why = AREA_UNFILTERED_FAILED
        return area
    strayed = _strayed_keys(name)
    for key in spec.keys:
        if key in strayed:
            area.tried.append({"key": key, "verdict": "strays_this_week"})
            continue
        try:
            count, records = client.first_page(
                path, {**base, key: code, "page_size": AREA_PAGE}, country=country
            )
        except IntegrationError as exc:
            refused = exc.status is not None and 400 <= exc.status < 500
            area.tried.append(
                {"key": key, "verdict": "refused" if refused else "failed", "status": exc.status}
            )
            continue
        verdict = judge_area_key(
            count, records, unfiltered=area.unfiltered, held=held, code=code, named_by=spec.named_by
        )
        named = [area_code(record.get(spec.named_by)) for record in records]
        area.tried.append(
            {
                "key": key,
                "verdict": verdict,
                "count": count,
                "page": len(records),
                "named": sum(1 for c in named if c == code),
                "other": sum(1 for c in named if c and c != code),
                "none": sum(1 for c in named if not c),
            }
        )
        if verdict == "proven":
            area.key, area.why = key, AREA_PROVEN
            return area
    area.why = AREA_NONE_PROVEN
    return area


def tried_words(tried: dict[str, Any]) -> str:
    """One filter key tried, in words: "business_area_code is refused (HTTP 400)"."""
    status = tried.get("status")
    said = f"{tried.get('key')} {AREA_VERDICTS.get(tried.get('verdict'), tried.get('verdict'))}"
    return said + (f" (HTTP {status})" if status else "")


def area_words(area: Any) -> str:
    """How a read of a dataset of ``catalogue.AREA_FILTERS`` was filtered (its ``details.area_filter``), in
    words for Data health, checks 10 and 11 and the probe: "read by business area 2490 (filter business_area:
    9,990 records with it, 780,905 without)", or "read with no business area filter: " and why ("business_area
    is ignored (as many records with it as without), business_area_code is refused (HTTP 400)"); "" for a run
    made before it was recorded."""
    if not isinstance(area, dict) or not area.get("why"):
        return ""
    key, code, why = str(area.get("key") or ""), str(area.get("code") or ""), area.get("why")
    tried = [t for t in area.get("tried") or [] if isinstance(t, dict)]
    if key:
        used = next((t for t in tried if t.get("key") == key), {})
        counts = []
        if isinstance(used.get("count"), int):
            counts.append(f"{used['count']:,} records with it")
        if isinstance(area.get("unfiltered"), int):
            counts.append(f"{area['unfiltered']:,} without")
        return f"read by business area {code} (filter {key}" + (f": {', '.join(counts)})" if counts else ")")
    if why == AREA_HELD_NOT_NAMED:
        n = _int(area.get("held_not_named")) or 0
        rows = "row held names" if n == 1 else "rows held name"
        reason = f"{n:,} {rows} no business area code or one other than {code}"
    elif why == AREA_WEEKLY:
        country = (settings.ETOOLS_DATAMART_COUNTRY or "").strip() or "the country"
        reason = f"once a week every record is read, so none of {country}'s that names no code is missed"
    elif why == AREA_NONE_PROVEN and tried:
        reason = ", ".join(tried_words(t) for t in tried)
    else:
        reason = AREA_WHY.get(why, str(why))
    return f"read with no business area filter: {reason}"


def _order_by(keys: Any) -> str:
    """The keys a stable order was asked by, in words: "by id", "by id and by prp_id"."""
    keys = [str(k) for k in keys if k] if isinstance(keys, list) else []
    return " and ".join(f"by {k}" for k in keys) or "by id"


def copies_words(details: Any) -> str:
    """The records a read proved the Datamart sends twice (a run's ``copies_proven``: every copy came
    identical in one answer, a page of the read or a request asked again), in words; "" for none."""
    details = details if isinstance(details, dict) else {}
    proven = details.get("copies_proven") if isinstance(details.get("copies_proven"), dict) else {}
    n = _int(proven.get("twice")) or 0
    if not n:
        return ""
    on_page, again = _int(proven.get("same_page")) or 0, _int(proven.get("asked_again")) or 0
    how = (
        "on one page"
        if not again
        else "found together again in one more request"
        if not on_page
        else f"{on_page:,} on one page, {again:,} found together again in one more request"
    )
    return (
        f"The Datamart sent {n:,} {'record' if n == 1 else 'records'} twice (identical), every copy in one "
        f"of its answers ({how}): it holds them twice, as one answer never lists a row twice. NeuroDB keeps "
        "one of each."
    )


def shift_words(details: Any) -> str:
    """What a read whose records came twice found (a run's details), in words for Data health, the run page
    and checks 10 and 11; "" for a read with no record twice, or one made before it was recorded. The records
    proven the Datamart's own copies are said first (:func:`copies_words`). For the others, with no stable
    order asked, the counts cannot tell rows moving between pages (as many others then never came) from rows
    the Datamart holds twice, so both are said; records that still came twice in a stable order are most
    likely held twice (``held_twice``)."""
    details = details if isinstance(details, dict) else {}
    proven = copies_words(details)
    return " ".join(part for part in (proven, _twice_words(details, bool(proven))) if part)


def _twice_words(details: dict[str, Any], proven: bool) -> str:
    """:func:`shift_words` of the records twice not proven; ``proven``: some others were."""
    ordering = details.get("ordering") if isinstance(details.get("ordering"), dict) else {}
    same = " identical" if details.get("twice_identical") else ""
    other = " other" if proven else ""
    held = _int(details.get("held_twice")) or 0
    if held:
        records = "record" if held == 1 else "records"
        key = ordering.get("key") or "id"
        return (
            f"The Datamart returned {held:,}{other}{same} {records} twice in a stable order (by {key}): it "
            "likely holds them twice; tell the eTools Datamart team. Nothing was removed."
        )
    shifted = details.get("pages_shifted")
    twice = _int(shifted.get("twice")) if isinstance(shifted, dict) else None
    if not twice:
        return ""
    missing = _int(shifted.get("missing")) or 0
    announced = details.get("datamart_count")
    of = f" (of the {announced:,} it announced)" if isinstance(announced, int) and announced > 0 else ""
    then = (
        ", and then as many others did not come"
        if missing == twice
        else f", and then {missing:,} others did not come{of}"
        if missing
        else ""
    )
    records = "record" if twice == 1 else "records"
    said = (
        f"{twice:,}{other} {records} came twice{' (identical)' if same else ''} while the Datamart's pages "
        f"were read: rows moved between pages{then}, or the Datamart holds them twice. Nothing was removed"
    )
    why, key = ordering.get("why"), ordering.get("key")
    if key and ordering.get("carried") is False:  # the order's pages shared no record: no order checked
        return (
            f"{said}; the records do not carry {key}, so the order asked by {key} cannot be checked, and the "
            "next read is in the Datamart's own order again."
        )
    if why in ("ignored", "refused", "failed"):
        verb = {"ignored": "is ignored", "refused": "is refused", "failed": "could not be asked"}[why]
        by = _order_by(ordering.get("tried"))
        return f"{said}; the Datamart takes no stable order (ordering {by} {verb})."
    if why == "too_small" or (isinstance(announced, int) and announced <= 2 * windows.ORDER_PAGE):
        return f"{said}."
    return f"{said}; the next read asks a stable order."


class Census:
    """What a read of a business-area dataset saw, as counts only (never a record's values, but for the
    business area codes): the records read, the codes they name (the most frequent), how many carry each
    key the sync and its readers use (``keys_seen``: a key the Datamart does not send reads 0), the id-like
    keys they carry (``id_keys``, :func:`id_keys`, the :data:`IDS_TRACKED` most carried: what a record
    identity or a link to the programme documents can be made of), and the years of the records kept
    (``dated``: check 11 reads the records of the year and the years beside it)."""

    def __init__(self, spec: catalogue.Source, today: dt.date | None = None) -> None:
        self.spec, self.year = spec, (today or timezone.localdate()).year
        self.read = 0
        self.codes: Counter[str] = Counter()
        keys = ("id", "source_id", "country_name", spec.filter_key, spec.date, *spec.partner)
        keys += (*spec.intervention, *spec.title, *spec.reads, *spec.counts)
        self.watched = tuple(dict.fromkeys(k for k in keys if k))
        self.keys: Counter[str] = Counter()
        self.ids: Counter[str] = Counter()
        self.years: Counter[str] = Counter()

    def saw(self, item: dict[str, Any]) -> None:
        self.read += 1
        self.codes[area_code(item.get(self.spec.filter_key))[:20] or NO_CODE] += 1
        self.keys.update(k for k in self.watched if item.get(k) not in (None, "", [], {}))
        self.ids.update(id_keys([item]))

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
            "id_keys": dict(self.ids.most_common(IDS_TRACKED)),
            "dated": dict(sorted(self.years.items())),
        }


# the keys a PRP progress report may name its PRP programme document by (``sources/prp/unicefprogressreport``
# is a raw PRP table: no recorded record confirms which one it sends), the first found first
PROGRESS_REPORT_PD_KEYS = ("programme_document_id", "programme_document")


def _named_pd(data: Any) -> str:
    """The PRP programme document a progress report names under :data:`PROGRESS_REPORT_PD_KEYS`, the first
    found (its id as text, :func:`id_value`: an id, ``{"id": ..}``), or ""."""
    data = data if isinstance(data, dict) else {}
    return next((v for v in (id_value(data.get(k)) for k in PROGRESS_REPORT_PD_KEYS) if v), "")


@functools.lru_cache(maxsize=4096)
def _names_pd(key: str) -> bool:
    """A key naming a programme document: its words hold "programme" (or "program") and "document",
    "programmedocument" or "pd"; never one that holds a person."""
    words = set(catalogue.key_words(key))
    programme = bool({"programme", "program"} & words) and "document" in words
    return (programme or "programmedocument" in words or "pd" in words) and not catalogue.holds_person(key)


def pd_named_keys(records: Iterable[dict[str, Any]]) -> list[str]:
    """The keys progress reports may name their PRP programme document by: those of
    :data:`PROGRESS_REPORT_PD_KEYS` first, then every other top-level key naming a programme document
    (:func:`_names_pd`), by name; only a key some record carries an id-like value under (:func:`id_value`)."""
    carried: set[str] = set()
    for item in records:
        carried.update(str(k) for k, v in item.items() if _names_pd(str(k)) and id_value(v))
    first = [k for k in PROGRESS_REPORT_PD_KEYS if k in carried]
    return first + sorted(carried - set(first))


# ----------------------------------------- PRP's progress reports and their programme document: the key pair
# A progress report names its PRP programme document by an id. Which key of PRP's programme documents holds
# that id is not documented, and they may carry no "id" at all (they are then held under a hash, and the
# lookup by the key a document is held under links none: 21 Oct 2026, 1,590 progress reports, none linked):
# the data proves the pair (:func:`prove_pd_link`).
PROGRAMME_DOCUMENTS, PROGRESS_REPORTS = "prp_programme_documents", "prp_progress_reports"
PD_RECORD_KEY = "record_key"  # the key a PRP programme document is held under: today's lookup
REPORT_NAMED = "named"  # the programme document a progress report names (:func:`_named_pd`): today's lookup
LINK_CLEAR = 2  # the pair used links at least this many times as many reports as any pair linking otherwise
LINK_SHARE = 0.5  # and at least this share of the reports naming a programme document under its key
LINK_SHARE_UNDATED = 0.9  # the share asked when fewer than half of its links have dates on both sides
LINK_DATES = 0.9  # the share of its dated links that agree: the report ends within its document's dates
LINK_BEFORE, LINK_AFTER = dt.timedelta(days=31), dt.timedelta(days=365)  # around the document's start and end
# a proven pair's agreement with the partner reports (:func:`pd_link_proof`): the reports the two feeds share,
# and of them those submitted on both sides, needed to judge it; the share of them with the same period end,
# and of those with the same submission day; the days in a row it must pass before the pages may show them
LINK_AGREE_MIN = 10
LINK_AGREE = 0.9
LINK_DAYS = 2
PAIRS_KEPT = 5  # the pairs a run lists, the best first
# why no key pair is proven (``progress_reports_pd_link.why``), in words for check 11
LINK_WHY = {
    "no_link": "no key of PRP's programme documents holds an id the progress reports name",
    "repeated": (
        "no key of PRP's programme documents whose values are unique holds an id the progress reports name"
    ),
    "dates_disagree": "every key pair that links them links reports ending outside their document's dates",
    "not_clear": "two key pairs link them otherwise, neither twice as often as the other",
    "few": "the best key pair links too few of the reports that name a programme document",
}


def _day_or_none(value: Any) -> dt.date | None:
    try:
        return parse_iso_date(value)
    except ValueError:  # an impossible date: no date
        return None


def report_value(data: Any, key: str) -> str:
    """A progress report's value under ``key``: today's lookup for :data:`REPORT_NAMED` (:func:`_named_pd`),
    else :func:`id_value`."""
    data = data if isinstance(data, dict) else {}
    return _named_pd(data) if key == REPORT_NAMED else id_value(data.get(key))


@dataclass
class PDLink:
    """How PRP's progress reports are linked to PRP's programme documents (:func:`prove_pd_link`): a
    report's value under ``report_key`` (:func:`report_value`) is looked up in ``index``, the programme
    documents held by their value under ``pd_key`` (each to the eTools PD it is linked to, or None).
    ``proven``: the data proves the pair; else it is today's lookup (the document named, by the key it is held
    under), and ``why`` says why. Key names and counts only in :meth:`details`."""

    report_key: str = REPORT_NAMED
    pd_key: str = PD_RECORD_KEY
    index: dict[str, int | None] = field(default_factory=dict)
    proven: bool = False
    why: str = ""
    pairs: list[dict[str, Any]] = field(default_factory=list)  # the best pairs, with the reports they link
    pd_keys: dict[str, int] = field(default_factory=dict)  # the documents' candidate keys, records carrying
    not_unique: list[str] = field(default_factory=list)  # candidate keys whose values repeat: left out
    came_twice: list[str] = field(default_factory=list)  # those of them left out by a document read twice

    def named(self, data: Any) -> str:
        """The value a progress report names its programme document by, under the pair's key."""
        return report_value(data, self.report_key)

    def details(self) -> dict[str, Any]:
        found: dict[str, Any] = {
            "by": [self.report_key, self.pd_key],
            "proven": self.proven,
            "pairs": list(self.pairs),
            "pd_keys": dict(self.pd_keys),
        }
        if self.why:
            found["why"] = self.why
        if self.not_unique:
            found["not_unique"] = list(self.not_unique)
        if self.came_twice:
            found["came_twice"] = list(self.came_twice)
        return found


def prove_pd_link(
    pds: list[tuple[str, Any, int | None]],
    reports: list[Any],
    etools_dates: dict[int, tuple[dt.date | None, dt.date | None]] | None = None,
    *,
    written: list[int] | None = None,
) -> PDLink:
    """The key pair that links PRP's progress reports (``reports``, their records) to PRP's programme
    documents (``pds``: the key each is held under, its record, the eTools PD it is linked to), proven on the
    data.

    The documents' keys tried: the key each is held under (today's lookup, first), every id-like key of their
    records (:func:`id_keys`), and ``reference_number``. A value several documents carry is the one the latest
    read wrote (``written``, the read that wrote each, :func:`written_by`): the others are copies of it an
    earlier read wrote, kept while the reads were not whole (a document that changed then got a row of its
    own). A key whose value two documents one read wrote carry is left out (``not_unique``: two documents
    share it, or one changed during that read came twice); with no ``written``, every document counts as one
    read's. A key left out only by rows that agree on every other key tried, one document that came twice, is
    also in ``came_twice``: the key holding the id may be among them, and the next read's copy counts. The
    reports' keys: the document they name (:data:`REPORT_NAMED`, today's, first), and
    every key naming a programme document (:func:`pd_named_keys`). Pairs that link the same reports to the
    same documents are one link, named by its first report key other than today's. A link is dated when the
    report has an ``end_date`` and its document a start and an end (its PRP record's ``start_date`` and
    ``end_date``, else those of its eTools PD, ``etools_dates``); it agrees when the report ends no earlier
    than :data:`LINK_BEFORE` before the start and no later than :data:`LINK_AFTER` after the end. A pair
    fewer than :data:`LINK_DATES` of whose dated links agree is out (``dates_disagree``: a chance match of
    eTools ids with PRP's links reports to documents of other dates). The pair linking the most reports (ties
    to the earlier one) is used when it links at least :data:`LINK_CLEAR` times as many as any other pair that
    links a report it does not link, or to another document (a pair whose links are all its own agrees with
    it), and at least :data:`LINK_SHARE` of the reports naming a document under its key
    (:data:`LINK_SHARE_UNDATED` when fewer than half of its links are dated). With none proven, today's lookup
    is used: it links what it links; when no pair links a report, ``why`` is ``repeated`` if a document that
    came twice left a key out (``came_twice``), else ``no_link``."""
    etools_dates = etools_dates or {}
    datas = [data if isinstance(data, dict) else {} for _, data, _ in pds]
    reports = [data if isinstance(data, dict) else {} for data in reports]

    def pd_values(key: str) -> list[str]:
        return [
            held if key == PD_RECORD_KEY else id_value(data.get(key))
            for (held, _, _), data in zip(pds, datas, strict=True)
        ]

    link = PDLink()
    reads = written if written is not None else [0] * len(pds)
    indexes: dict[str, dict[str, int]] = {}  # a document key -> its values -> the document's position
    tried = list(dict.fromkeys((PD_RECORD_KEY, *sorted(id_keys(datas)), "reference_number")))
    values_of = {key: pd_values(key) for key in tried}
    for key in tried:
        values = values_of[key]
        carried = [v for v in values if v]
        if not carried:
            continue
        link.pd_keys[key] = len(carried)
        latest: dict[str, int] = {}  # a value -> the document carrying it that the latest read wrote
        for i, v in enumerate(values):
            if v and (v not in latest or reads[i] > reads[latest[v]]):
                latest[v] = i
        # two rows one read wrote carry one value: two documents share it (a partner's id), or one document
        # came twice (its rows agree on every other key tried: came_twice)
        twice = [
            (i, latest[v])
            for i, v in enumerate(values)
            if v and i != latest[v] and reads[i] == reads[latest[v]]
        ]
        if twice:
            link.not_unique.append(key)
            others = [k for k in tried if k not in (PD_RECORD_KEY, key)]
            if all(values_of[k][i] == values_of[k][j] for i, j in twice for k in others):
                link.came_twice.append(key)
            continue
        indexes[key] = latest
    # each pair's links (a report's position -> its document's), the same links of two pairs one link
    groups: dict[frozenset, dict[str, Any]] = {}
    for report_key in (REPORT_NAMED, *pd_named_keys(reports)):
        values = [report_value(data, report_key) for data in reports]
        naming = sum(1 for v in values if v)
        for pd_key, index in indexes.items():
            links = {i: index[v] for i, v in enumerate(values) if v and v in index}
            if not links:
                continue
            found = {"report": report_key, "pd": pd_key, "links": links, "naming": naming}
            same = groups.setdefault(frozenset(links.items()), {**found, "order": len(groups)})
            if same["report"] == REPORT_NAMED and report_key != REPORT_NAMED:
                same.update(report=report_key, pd=pd_key, naming=naming)
    starts_ends = []
    for (_, _, pk), data in zip(pds, datas, strict=True):
        start, end = _day_or_none(data.get("start_date")), _day_or_none(data.get("end_date"))
        etools = etools_dates.get(pk) or (None, None)
        starts_ends.append((start or etools[0], end or etools[1]))
    ends = [_day_or_none(data.get("end_date")) for data in reports]
    for group in groups.values():
        dated = agree = 0
        for report, document in group["links"].items():
            start, end = starts_ends[document]
            if ends[report] and start and end:
                dated += 1
                agree += start - LINK_BEFORE <= ends[report] <= end + LINK_AFTER
        group.update(linked=len(group["links"]), dated=dated, agree=agree)
        group["disagree"] = bool(dated) and agree < LINK_DATES * dated
    ranked = sorted(groups.values(), key=lambda g: (-g["linked"], g["order"]))
    contenders = [g for g in ranked if not g["disagree"]]
    best = contenders[0] if contenders else None
    if best is None:
        link.why = "dates_disagree" if groups else "repeated" if link.came_twice else "no_link"
    else:
        rivals = [g for g in contenders[1:] if any(best["links"].get(r) != d for r, d in g["links"].items())]
        share = LINK_SHARE if best["dated"] * 2 >= best["linked"] else LINK_SHARE_UNDATED
        if any(best["linked"] < LINK_CLEAR * g["linked"] for g in rivals):
            link.why = "not_clear"
        elif best["linked"] < share * best["naming"]:
            link.why = "few"
        else:
            link.report_key, link.pd_key, link.proven = best["report"], best["pd"], True
    link.pairs = [
        {
            "report": g["report"],
            "pd": g["pd"],
            "linked": g["linked"],
            "dated": g["dated"],
            "agree": g["agree"],
            **(
                {"verdict": "proven"}
                if link.proven and g is best
                else {"verdict": "dates_disagree"}
                if g["disagree"]
                else {}
            ),
        }
        for g in ranked[:PAIRS_KEPT]
    ]
    link.index = {v: pds[i][2] for v, i in indexes.get(link.pd_key, {}).items()}
    return link


def written_by(name: str, moments: list[dt.datetime]) -> list[int]:
    """For each moment a row of dataset ``name`` was last written (its ``synced_at``), the read that wrote it,
    as a number: the runs of ``name`` started at or before it (one query, the run under way counted). Rows one
    read wrote share it; a row an earlier read wrote, kept since (a read not whole removes nothing), has a
    lower one."""
    runs = SyncRun.objects.filter(job=SyncRun.Job.ETOOLS_DATAMART, target=name)
    starts = sorted(runs.values_list("started_at", flat=True))
    return [bisect.bisect_right(starts, moment) for moment in moments]


def held_pd_link(reports: list[Any] | None = None) -> PDLink:
    """:func:`prove_pd_link` on what NeuroDB holds of PRP's programme documents and progress reports
    (``reports``, the progress reports' records when they are read already), each document with the read that
    wrote it."""
    held_pds = list(
        dm.DatamartDocument.objects.filter(dataset=PROGRAMME_DOCUMENTS).values_list(
            "record_key", "data", "intervention_id", "synced_at"
        )
    )
    pds = [(key, data, pk) for key, data, pk, _ in held_pds]
    written = written_by(PROGRAMME_DOCUMENTS, [synced for *_, synced in held_pds])
    if reports is None:
        held = dm.DatamartDocument.objects.filter(dataset=PROGRESS_REPORTS)
        reports = list(held.values_list("data", flat=True))
    dates = PCA.objects.filter(pk__in={pk for _, _, pk in pds if pk}).values_list("pk", "start", "end")
    return prove_pd_link(pds, reports, {pk: (start, end) for pk, start, end in dates}, written=written)


def report_identity(data: Any) -> tuple | None:
    """What tells one PRP progress report from another across its versions, as they carry no id: the programme
    document it names, under every key naming one (:func:`_names_pd`), its ``report_type`` and its
    ``report_number``; None when it names no document or no number."""
    data = data if isinstance(data, dict) else {}
    named = tuple(sorted((str(k), id_value(v)) for k, v in data.items() if _names_pd(str(k)) and id_value(v)))
    number = id_value(data.get("report_number"))
    if not named or not number:
        return None
    return named, str(data.get("report_type") or ""), number


def older_versions(rows: Iterable[tuple[int, Any, dt.datetime]]) -> set[int]:
    """The rows (``pk``, record, ``synced_at``) of PRP's progress reports that hold an older version of a
    report a later read wrote again (:func:`report_identity`, :func:`written_by`): the report changed (its
    status, a date) on a night its read was not whole, so it got a row of its own and its old row was kept,
    until a whole read removes it. The rows of one report one read wrote are none of them older."""
    rows = list(rows)
    reads = written_by(PROGRESS_REPORTS, [synced for *_, synced in rows])
    latest: dict[tuple, int] = {}
    found = []
    for (pk, data, _), read in zip(rows, reads, strict=True):
        report = report_identity(data)
        if report is not None:
            found.append((pk, report, read))
            latest[report] = max(latest.get(report, read), read)
    return {pk for pk, report, read in found if read < latest[report]}


def link_progress_reports() -> dict[str, Any]:
    """PRP's progress reports (``prp_progress_reports``) linked to their programme document and its partner,
    through PRP's own programme documents (``prp_programme_documents``, linked to the eTools PD by its
    external id or reference number), by the key pair the data proves (:func:`held_pd_link`; with none proven,
    the document a report names by the key it is held under). Run after each of the two datasets (the
    catalogue syncs the programme documents first), so a link follows a change in either. An older version
    of a report (:func:`older_versions`) is linked to none, so no link counts a report twice. Key names and
    counts only, for the run's details: ``linked``; ``pd_not_named`` (the record names none under the pair's
    key); ``pd_not_held`` (a PRP programme document NeuroDB does not hold under the pair's key);
    ``pd_not_linked`` (held, not linked to a PD NeuroDB holds); ``pd_superseded`` (an older version, left
    unlinked); the pair (:meth:`PDLink.details`: ``by``, ``proven``, ``pairs``, ``pd_keys``,
    ``not_unique``, ``came_twice``, ``why``); and whether the pages may show PRP's reports still owed, with
    the figures that prove it (:func:`pd_link_proof`). Nothing when no progress report is held."""
    reports = list(
        dm.DatamartDocument.objects.filter(dataset=PROGRESS_REPORTS).only(
            "pk", "data", "intervention_id", "partner_id", "synced_at"
        )
    )
    if not reports:
        return {}
    link = held_pd_link([doc.data for doc in reports])
    older = older_versions((doc.pk, doc.data, doc.synced_at) for doc in reports)
    partners = dict(
        PCA.objects.filter(pk__in={pk for pk in link.index.values() if pk}).values_list("pk", "partner_id")
    )
    counts: Counter[str] = Counter()
    changed = []
    for doc in reports:
        named = link.named(doc.data)
        pd_id = link.index.get(named) if named and doc.pk not in older else None
        counts[
            "pd_superseded"
            if doc.pk in older
            else "linked"
            if pd_id
            else "pd_not_named"
            if not named
            else "pd_not_held"
            if named not in link.index
            else "pd_not_linked"
        ] += 1
        partner_id = partners.get(pd_id) if pd_id else None
        if (doc.intervention_id, doc.partner_id) != (pd_id, partner_id):
            doc.intervention_id, doc.partner_id = pd_id, partner_id
            changed.append(doc)
    dm.DatamartDocument.objects.bulk_update(changed, ["intervention_id", "partner_id"], batch_size=2000)
    try:  # the proof never fails the link: without it, the pages may not show them ("check_failed")
        with transaction.atomic():
            proof = pd_link_proof(link.proven, timezone.localdate(), by=[link.report_key, link.pd_key])
    except Exception as exc:
        logger.warning("etools datamart: the proof of the progress reports' link was not read: %s", exc)
        proof = {"proof_error": str(exc)[:300]}
    return {**counts, **link.details(), **proof}


# ------------------------------- PRP's progress reports against the partner reports: may the pages show them?
# PRP's own list of progress reports holds the reports still owed for a period that has ended, which the
# partner reports feed stops sending. The pages may show them only while the data proves the link to their
# programme documents: the key pair is proven, and the reports the two feeds share agree on their period end
# and on the day they were submitted (many PDs share quarter calendars, so a chance link can match a period
# end; a submission day is a specific date), on two days in a row. It closes at once when a read fails it: a
# wrong link would show wrong overdue reports, a late one only delays them.
# a PRP progress report no read returned for this long is no longer counted (PRP may have deleted it)
OWED_STALE_DAYS = 7
OWED = frozenset({rc.DUE, rc.OVERDUE})  # a report still owed, once its period has ended
AGREES = ("compared", "same", "elsewhere", "dated", "same_day")  # progress_reports_pd_link.agrees
PROOF_COUNTS = (
    "owed",
    "owed_before_history",
    "stale",
    "twins",
    "ove_read_due",
    "lacking",
    "unsent_other_word",
)
# the fields of a PRP progress report the proof reads, one by one (never the record whole: it holds narratives
# and the people who wrote and reviewed it)
PROGRESS_FIELDS = (
    "status",
    "report_type",
    "report_number",
    "start_date",
    "end_date",
    "due_date",
    "submission_date",
)
# why the pages may not show them (recorded_pd_link: why), in words for check 11 and Raw eTools data
SHOWN_WHY = {
    "not_proven": "their link to the programme documents is not proven",
    "few": "too few of them are also in the partner reports to check the link",
    "periods": "too few of the reports also in the partner reports end on the same day",
    "few_days": "too few reports were submitted on both sides to check the submission day",
    "days": "too few of the reports submitted on both sides were submitted on the same day",
    "one_day": "the link passed every test on {day} only, and must pass on the next day too",
    "not_read": "the last sync of PRP's progress reports or of their programme documents failed or received "
    "nothing",
    "not_synced": "no sync of PRP's progress reports has checked it yet",
    "not_checked": "not checked yet: the next sync of PRP's progress reports checks it",
    "check_failed": "the check failed on the last sync: tell the NeuroDB team",
    "old": "PRP's progress reports were last read on {day}",
}


def day_words(iso: Any) -> str:
    """A recorded day ("2026-10-16") as the pages write it: "16 Oct 2026" ("" for none)."""
    day = rc.iso_day(iso)
    return f"{day.day} {day:%b %Y}" if day else ""


def shown_words(verdict: dict[str, Any]) -> str:
    """Whether the pages may show PRP's progress reports still owed (:func:`recorded_pd_link`), in words:
    "yes, since 16 Oct 2026", or "no: " and why (:data:`SHOWN_WHY`)."""
    if verdict.get("shown"):
        return f"yes, since {day_words(verdict.get('since'))}"
    why = str(verdict.get("why") or "")
    day = day_words(verdict.get("read_on") if why == "old" else verdict.get("checked_on"))
    return f"no: {SHOWN_WHY.get(why, SHOWN_WHY['not_proven']).format(day=day)}"


def linked_progress_reports(today: dt.date) -> list[dict[str, Any]]:
    """PRP's progress reports linked to a programme document NeuroDB holds (an older version is linked to
    none: :func:`link_progress_reports`), each read field by field (:data:`PROGRESS_FIELDS`, ``KT``): its
    record's ``pk``, ``pd`` and the ``base`` of the PD's number (``reached.pd_base``), its ``kind`` (TYPE) and
    number ``key`` (``reached.report_number_key``), ``start``, ``end``, ``due`` and ``sub`` (submission) days,
    PRP's ``word`` and the ``status`` NeuroDB reads it as (``reached.prp_document_status``), when a read last
    returned it (``synced_at``) and which read that was (``read``, :func:`written_by`)."""
    found = list(
        dm.DatamartDocument.objects.filter(dataset=PROGRESS_REPORTS, intervention__isnull=False)
        .annotate(**{f"_{k}": KT(f"data__{k}") for k in PROGRESS_FIELDS})
        .values(
            "pk",
            "intervention_id",
            "intervention__number",
            "date",
            "synced_at",
            *(f"_{k}" for k in PROGRESS_FIELDS),
        )
    )
    reads = written_by(PROGRESS_REPORTS, [row["synced_at"] for row in found])
    out = []
    for row, read in zip(found, reads, strict=True):
        status, end = rc.prp_document_status(
            status=row["_status"],
            end=row["date"] or row["_end_date"],
            due=row["_due_date"],
            sub=row["_submission_date"],
            today=today,
        )
        kind = str(row["_report_type"] or "").strip().upper()
        out.append(
            {
                "pk": row["pk"],
                "pd": row["intervention_id"],
                "base": rc.pd_base(row["intervention__number"]),
                "kind": kind,
                "key": rc.report_number_key(row["_report_number"], kind),
                "start": rc.iso_day(row["_start_date"]),
                "end": end,
                "due": rc.iso_day(row["_due_date"]),
                "sub": rc.iso_day(row["_submission_date"]),
                "word": " ".join(str(row["_status"] or "").lower().split()),
                "status": status,
                "synced_at": row["synced_at"],
                "read": read,
            }
        )
    return out


def counted_once(reports: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """PRP's progress reports (:func:`linked_progress_reports`), one per programme document, type and number
    (the identity :func:`older_versions` uses): the version the latest read wrote, then the latest period end,
    then the newest record; and how many others it leaves out (``twins``: a shift of the Datamart's pages can
    bring two versions of one report in one read). A report with no number is one of its own."""
    best: dict[Any, tuple[tuple, dict[str, Any]]] = {}
    for report in reports:
        group = (report["pd"], report["kind"], report["key"]) if report["key"] else ("record", report["pk"])
        rank = (report["read"], report["end"] or dt.date.min, report["pk"])
        if group not in best or rank > best[group][0]:
            best[group] = (rank, report)
    kept = [report for _, report in best.values()]
    return kept, len(reports) - len(kept)


def fresh_since() -> dt.datetime:
    """From when a read must have returned a PRP progress report for it to count: :data:`OWED_STALE_DAYS`
    before the latest read of them that finished, or the one under way (now, before any). Their reads are
    never whole (the Datamart's pages shift), so nothing removes a report PRP deleted: its last record would
    read Overdue for ever."""
    started = (
        SyncRun.objects.filter(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target=PROGRESS_REPORTS,
            status__in=(SyncRun.Status.SUCCEEDED, SyncRun.Status.PARTIAL, SyncRun.Status.RUNNING),
        )
        .order_by("-started_at", "-pk")
        .values_list("started_at", flat=True)
        .first()
    )
    return (started or timezone.now()) - dt.timedelta(days=OWED_STALE_DAYS)


@dataclass
class PartnerNumbers:
    """The progress reports the partner report rows hold by number (``reporting.report_numbers``, every row):
    each ``(base, TYPE, number key)`` with its period ends and submission days (``reports``), the bases that
    hold each ``(TYPE, number key, period end)`` (``by_end``), and those a row still sent holds (``sent``: the
    others are held only by rows eTools no longer sends). One query."""

    reports: dict[tuple[str, str, str], tuple[set[dt.date], set[dt.date]]] = field(default_factory=dict)
    by_end: dict[tuple[str, str, dt.date], set[str]] = field(default_factory=lambda: defaultdict(set))
    sent: set[tuple[str, str, str]] = field(default_factory=set)

    @classmethod
    def read(cls) -> PartnerNumbers:
        from neurodb.datamart import reporting  # lazy: the partner reports' pages

        found = cls()
        for bases, kind, key, end, sub, unsent in reporting.report_numbers(
            None, "period_end", "submission_date", "not_sent_since"
        ):
            for base in bases:
                ends, subs = found.reports.setdefault((base, kind, key), (set(), set()))
                if end is not None:
                    ends.add(end)
                    found.by_end[(kind, key, end)].add(base)
                if sub is not None:
                    subs.add(sub)
                if unsent is None:
                    found.sent.add((base, kind, key))
        return found

    def of(self, report: dict[str, Any]) -> tuple[set[dt.date], set[dt.date]] | None:
        """The period ends and submission days of a PRP report's number in the rows, under its type or under a
        row with no type (``reporting.holds``); None when the rows hold none."""
        found = [self.reports[k] for k in self._keys(report) if k in self.reports]
        if not found:
            return None
        return set().union(*(ends for ends, _ in found)), set().union(*(subs for _, subs in found))

    def unsent_only(self, report: dict[str, Any]) -> bool:
        """Whether only rows eTools no longer sends hold the report."""
        keys = [k for k in self._keys(report) if k in self.reports]
        return bool(keys) and not any(k in self.sent for k in keys)

    def elsewhere(self, report: dict[str, Any]) -> bool:
        """Whether another programme document's rows hold the report's type, number and period end."""
        if report["end"] is None:
            return False
        bases = self.by_end.get((report["kind"], report["key"], report["end"]), set())
        bases = bases | self.by_end.get(("", report["key"], report["end"]), set())
        return bool(bases - {report["base"]})

    @staticmethod
    def _keys(report: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
        return (report["base"], report["kind"], report["key"]), (report["base"], "", report["key"])


def unsent_other_word(
    reports: list[dict[str, Any]], numbers: PartnerNumbers, today: dt.date
) -> tuple[int, set[str]]:
    """Of PRP's progress reports (counted once), those the partner report rows hold only as no longer sent
    (``ReportedIndicator.not_sent_since``) whose status there (the rows decide, as every page reads them:
    ``reporting.progress_reports``) differs from PRP's: how many, and the rows' reports
    (``reached.report_key``) for Raw eTools data's "Updated by" choice. The report of the same period end when
    the rows have one, else every period of that number. The rows' word is never changed: they are listed for
    review. Read only over a proven key pair (its callers): the superuser's removal list follows the
    choice."""
    mine = [r for r in reports if r["key"] and r["base"] and numbers.unsent_only(r)]
    if not mine:
        return 0, set()
    from neurodb.datamart import reporting  # lazy: the partner reports' pages

    marked = reporting.progress_reports(dm.ReportedIndicator.objects.exclude(not_sent_since=None), today)
    pd_numbers = dict(
        PCA.objects.filter(pk__in={r["intervention_id"] for r in marked if r["intervention_id"]}).values_list(
            "pk", "number"
        )
    )
    by_number: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in marked:
        kind = (r["report_type"] or "").strip().upper()
        key = rc.report_number_key(r["report_number"], kind)
        for base in {rc.pd_base(r["pd_reference_number"]), rc.pd_base(pd_numbers.get(r["intervention_id"]))}:
            if base and key:
                by_number[(base, kind, key)].append(r)
    differ, keys = 0, set()
    for report in mine:
        found = [r for k in PartnerNumbers._keys(report) for r in by_number.get(k, ())]
        same = [r for r in found if r["period_end"] == report["end"]] or found
        if same and report["status"] not in {r["status"] for r in same}:
            differ += 1
            keys.update(r["key"] for r in same)
    return differ, keys


def unsent_word_reports(today: dt.date | None = None) -> set[str]:
    """The partner reports (``reached.report_key``) eTools no longer sends whose status differs in PRP's
    progress reports (:func:`unsent_other_word`, as check 11 counts them): none while the last sync did not
    prove the key pair that links PRP's progress reports to their programme documents
    (:func:`recorded_pd_link`'s ``proven``), so never over today's lookup."""
    if recorded_pd_link().get("proven") is not True:
        return set()
    today = today or timezone.localdate()
    since = fresh_since()
    reports = [r for r in linked_progress_reports(today) if r["synced_at"] >= since]
    if not reports:
        return set()
    return unsent_other_word(counted_once(reports)[0], PartnerNumbers.read(), today)[1]


def _previous_verdict(today: dt.date) -> dict[str, Any]:
    """The previous day's verdict, which the days in a row go on from: the one the latest finished run of
    either of PRP's two datasets that started before ``today`` recorded, never an older run's. {} (the days
    start again from one) when that run recorded none (it failed before the link step, its proof failed, or
    it was made before the verdict was recorded), when it did not start on the day before, or when the latest
    read of the progress reports by then failed, received nothing or is old (:func:`_read_closes`: the pages
    did not show them). :func:`pd_link_proof` also starts again when its key pair (``by``) is not today's."""
    start = timezone.make_aware(dt.datetime.combine(today, dt.time.min))
    latest = (
        SyncRun.objects.filter(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target__in=(PROGRESS_REPORTS, PROGRAMME_DOCUMENTS),
            started_at__lt=start,
        )
        .exclude(status=SyncRun.Status.RUNNING)
        .order_by("-started_at", "-pk")
        .annotate(_link=KeyTransform("progress_reports_pd_link", "details"))
        .values_list("started_at", "_link")
        .first()
    )
    if latest is None or not isinstance(latest[1], dict) or "passes" not in latest[1]:
        return {}
    if timezone.localtime(latest[0]).date() != today - dt.timedelta(days=1) or _read_closes(start):
        return {}
    return latest[1]


def _read_closes(before: dt.datetime) -> dict[str, str]:
    """Why the latest read of PRP's progress reports that started before ``before`` and finished, failed or
    not, closes the verdict: ``{"why": "not_read"}`` when there is none, it failed or it received nothing
    (``empty_response``), so a later sync of their programme documents never opens it again over the same
    records; ``{"why": "old", "read_on": its day}`` when it ended more than :data:`OWED_STALE_DAYS` days
    before; {} when it does not."""
    read = (
        SyncRun.objects.filter(
            job=SyncRun.Job.ETOOLS_DATAMART,
            target=PROGRESS_REPORTS,
            started_at__lt=before,
        )
        .exclude(status=SyncRun.Status.RUNNING)
        .order_by("-started_at", "-pk")
        .annotate(_empty=KeyTransform("empty_response", "details"))
        .values_list("finished_at", "_empty", "status")
        .first()
    )
    if read is None or read[1] or read[2] == SyncRun.Status.FAILED:
        return {"why": "not_read"}
    if read[0] is not None and read[0] < before - dt.timedelta(days=OWED_STALE_DAYS):
        return {"why": "old", "read_on": timezone.localtime(read[0]).date().isoformat()}
    return {}


def pd_link_proof(proven: bool, today: dt.date, *, by: list[str] | None = None) -> dict[str, Any]:
    """Whether the pages may show PRP's progress reports still owed, and the figures that prove it, over the
    reports linked (:func:`linked_progress_reports`, :func:`counted_once`), names and counts only:

    * ``agrees``: of them, those the partner report rows also hold by type and number (``compared``,
      :class:`PartnerNumbers`), those with the same period end (``same``), the others whose period end another
      PD's rows hold (``elsewhere``), those submitted on both sides (``dated``) and on the same day
      (``same_day``);
    * ``passes``: the key pair is proven and at least :data:`LINK_AGREE_MIN` compared, :data:`LINK_AGREE` of
      them the same, at least :data:`LINK_AGREE_MIN` dated, :data:`LINK_AGREE` of those on the same day;
      ``days`` it passed in a row with the same key pair ``by`` (the previous day's verdict,
      :func:`_previous_verdict`, whose ``by`` is the same: another pair links other reports, so it starts
      again from one day, and a pair renamed costs one day); ``shown`` from
      :data:`LINK_DAYS` days, ``shown_since`` the day it first showed; ``shown_why`` why not (``not_proven``,
      ``few``, ``periods``, ``few_days``, ``days``, ``one_day``) or "";
    * of the reports a read returned within :data:`OWED_STALE_DAYS` (:func:`fresh_since`), those of draft or
      cancelled programme documents left out (``pd_statuses.NOT_A_PARTNERSHIP``: the pages never show them):
      ``owed`` (Due or Overdue, their period ended, not in the rows), ``owed_before_history`` (of them, ending
      before the partner reports' history, ``reached.history_from``), ``twins`` (:func:`counted_once`),
      ``ove_read_due`` (PRP says Overdue, read as Due: no due date passed), ``lacking`` (Accepted or Submitted
      this year, not in the rows), ``unsent_other_word`` (:func:`unsent_other_word`, 0 while the key pair is
      not proven); and ``stale``, those that would be owed but no read returned since (counted once, not in
      the rows, draft or cancelled programme documents left out too).

    It changes no figure itself: the pages show the reports still owed only while the recorded verdict shows
    them (``owed.link_state``, :func:`recorded_pd_link`)."""
    reports = linked_progress_reports(today)
    since = fresh_since()
    numbers = PartnerNumbers.read()
    agrees = dict.fromkeys(AGREES, 0)
    once, _ = counted_once(reports)
    for report in once:
        found = numbers.of(report) if report["key"] and report["base"] else None
        if found is None:
            continue
        ends, subs = found
        agrees["compared"] += 1
        if report["end"] is not None and report["end"] in ends:
            agrees["same"] += 1
        elif numbers.elsewhere(report):
            agrees["elsewhere"] += 1
        if report["sub"] is not None and subs:
            agrees["dated"] += 1
            agrees["same_day"] += report["sub"] in subs
    fresh = [r for r in reports if r["synced_at"] >= since]
    current, twins = counted_once(fresh)

    def ended(report: dict[str, Any]) -> bool:
        return report["end"] is not None and report["end"] <= today

    def held(report: dict[str, Any]) -> bool:
        return bool(report["key"] and report["base"]) and numbers.of(report) is not None

    # the reports of draft or cancelled programme documents: the pages never show them (owed._filters'
    # not_a_partnership step), so neither do the counts of reports still owed
    gone = set(
        PCA.objects.filter(
            pk__in={r["pd"] for r in reports}, status__in=pd_statuses.NOT_A_PARTNERSHIP
        ).values_list("pk", flat=True)
    )

    def owes(report: dict[str, Any]) -> bool:
        return report["status"] in OWED and ended(report) and not held(report) and report["pd"] not in gone

    owed = [r for r in current if owes(r)]
    history = rc.history_from(today).date
    counts = {
        "owed": len(owed),
        "owed_before_history": sum(1 for r in owed if r["end"] < history),
        "stale": sum(1 for r in counted_once([r for r in reports if r["synced_at"] < since])[0] if owes(r)),
        "twins": twins,
        "ove_read_due": sum(
            1
            for r in current
            if r["word"] in ("ove", "overdue") and r["status"] == rc.DUE and r["sub"] is None
        ),
        "lacking": sum(
            1
            for r in current
            if r["status"] in rc.COUNTED
            and r["end"] is not None
            and r["end"].year == today.year
            and not held(r)
        ),
        "unsent_other_word": unsent_other_word(current, numbers, today)[0] if proven else 0,
    }
    a = agrees
    why = (
        "not_proven"
        if not proven
        else "few"
        if a["compared"] < LINK_AGREE_MIN
        else "periods"
        if a["same"] < LINK_AGREE * a["compared"]
        else "few_days"
        if a["dated"] < LINK_AGREE_MIN
        else "days"
        if a["same_day"] < LINK_AGREE * a["dated"]
        else ""
    )
    passes = not why
    previous = _previous_verdict(today)
    # the days go on only from a verdict of the same key pair: another pair links other reports
    same = previous.get("passes") is True and previous.get("by") == by
    days = (_count(previous.get("days")) or 0) + 1 if same else 1
    days = days if passes else 0
    shown = days >= LINK_DAYS
    since_day = ""
    if shown:
        since_day = str(previous.get("shown_since") or "") if same and previous.get("shown") is True else ""
        since_day = since_day or today.isoformat()
    if passes and not shown:
        why = "one_day"
    return {
        "agrees": agrees,
        **counts,
        "passes": passes,
        "days": days,
        "shown": shown,
        "shown_since": since_day,
        "shown_why": why,
    }


def recorded_pd_link() -> dict[str, Any]:
    """Whether the pages may show PRP's progress reports still owed, as the sync recorded it
    (:func:`pd_link_proof`): the verdict of the latest finished run (not running) of PRP's progress reports or
    of their programme documents, whichever started later, so an older verdict is never read over newer links.
    ``{"shown": False, "why": "not_synced"}`` when no such run has finished yet, or when that run did not fail
    and found no progress report held (its link step recorded nothing); ``why`` "not_read" when that run
    recorded none (it failed before the link step), "check_failed" when its proof failed (``proof_error``),
    "not_checked" for a run made before the verdict was recorded. A verdict that shows them still closes when
    the latest read of the progress reports that finished failed or received nothing (``not_read``) or ended
    more than :data:`OWED_STALE_DAYS` days ago (``old``, ``read_on`` its day). Else:
    ``shown``, ``why`` (``pd_link_proof``'s ``shown_why``), ``since``, ``days``, ``passes``, ``proven``,
    ``link_why`` (why no key pair is proven, :data:`LINK_WHY`), ``linked``, ``agrees``, the counts of
    :data:`PROOF_COUNTS` and ``checked_on``, the day of the verdict's run. Check 11 and Raw eTools data read
    it."""
    latest = list(
        SyncRun.objects.filter(
            job=SyncRun.Job.ETOOLS_DATAMART, target__in=(PROGRESS_REPORTS, PROGRAMME_DOCUMENTS)
        )
        .exclude(status=SyncRun.Status.RUNNING)
        .order_by("-started_at", "-pk")
        .annotate(_link=KeyTransform("progress_reports_pd_link", "details"))
        .values_list("started_at", "_link", "status")[:1]
    )
    if not latest:
        return {"shown": False, "why": "not_synced"}
    started, link, status = latest[0]
    checked_on = timezone.localtime(started).date().isoformat()
    if link == {} and status != SyncRun.Status.FAILED:  # no progress report was held: nothing checked yet
        return {"shown": False, "why": "not_synced", "checked_on": checked_on}
    if not isinstance(link, dict) or not link:
        return {"shown": False, "why": "not_read", "checked_on": checked_on}
    if "passes" not in link:
        why = "check_failed" if link.get("proof_error") else "not_checked"
        return {"shown": False, "why": why, "checked_on": checked_on}
    agrees = link.get("agrees") if isinstance(link.get("agrees"), dict) else {}
    found: dict[str, Any] = {
        "shown": link.get("shown") is True,
        "why": str(link.get("shown_why") or ""),
        "since": str(link.get("shown_since") or ""),
        "days": _count(link.get("days")) or 0,
        "passes": link.get("passes") is True,
        "proven": link.get("proven") is True,
        "link_why": str(link.get("why") or ""),
        "linked": _count(link.get("linked")) or 0,
        "agrees": {k: _count(agrees.get(k)) or 0 for k in AGREES},
        **{k: _count(link.get(k)) or 0 for k in PROOF_COUNTS},
        "checked_on": checked_on,
    }
    closes = _read_closes(timezone.now()) if found["shown"] else {}
    if closes:
        found.update(shown=False, **closes)
    return found


# the link steps run after a dataset's records are written (:func:`sync_documents`)
AFTER_DOCUMENTS: dict[str, Callable[[], dict[str, Any]]] = {
    PROGRAMME_DOCUMENTS: link_progress_reports,
    PROGRESS_REPORTS: link_progress_reports,
}
# a dataset whose records may name no business area: such a record is kept only when the PRP programme
# document it names, under the key pair proven on what is held before the read (:func:`held_pd_link`), is held
# in this dataset, read by business area; the others are counted as another country's
# (``other_country_skipped``, and ``unnamed_skipped`` apart)
KEPT_BY_PROGRAMME_DOCUMENT = {PROGRESS_REPORTS: PROGRAMME_DOCUMENTS}
# a dataset whose records may name no business area: such a record is kept only when its reference number
# names a programme document NeuroDB holds (reference numbers carry the country's prefix; eTools ids repeat
# across countries, so an id is never compared), and linked to it by that number alone
# (``kept_by_reference``); the others are counted as another country's (``unnamed_skipped``)
KEPT_BY_REFERENCE = frozenset({PROGRAMME_DOCUMENTS})


def sync_documents(name: str) -> Callable[..., SyncRun]:
    spec = catalogue.DOCUMENTS[name]

    def sync(run: SyncRun, *, client: DatamartClient, links: Links) -> SyncRun:
        def body() -> dict[str, Any]:
            links.missing.clear()
            scope = links.scope or CountryScope(client)
            links.scope = scope
            params, by_country = scope.request(spec)
            area = None
            if name in catalogue.AREA_FILTERS:  # by business area once the Datamart proves it applies it
                code = str(params.pop(spec.filter_key))
                held_docs = dm.DatamartDocument.objects.filter(dataset=name)
                not_named = rows_not_named(held_docs, catalogue.AREA_FILTERS[name].named_by, code)
                area = prove_area_filter(
                    client,
                    name,
                    spec.path,
                    params,
                    country=by_country,
                    code=code,
                    held=held_docs.count(),
                    held_not_named=not_named,
                )
                params = {**params, **area.params()}
            # a dataset whose records carry no id: tonight's record identity is chosen once every record is
            # read (identity_key), so they are written after the read; last night's counts them meanwhile
            previous = str(last_details(name).get("record_identity") or "")
            writer, other, unnamed = DocumentWriter(name, links, previous=previous), 0, 0
            census = Census(spec) if spec.scope == "business_area" else None
            keys: set[str] = set()  # the distinct records read, kept or skipped (record_key, last night's)
            tracker = IdentityTracker(previous)
            waiting: dict[str, dict[str, Any]] = {}  # the records kept without an id, by their content
            reading = windows.Reading(client, spec.path, params, country=by_country, name=name)
            # a record naming no business area is kept only when the PRP programme document it names is held
            # (those are read by business area), or, for PRP's programme documents, when its reference number
            # names a programme document NeuroDB holds: the others are counted as another country's
            link = held_pd_link() if name in KEPT_BY_PROGRAMME_DOCUMENT else None
            by_reference = name in KEPT_BY_REFERENCE
            through_pd = by_number = unnamed_skipped = named_here = 0

            def items():
                nonlocal other, unnamed, through_pd, by_number, unnamed_skipped, named_here
                for item in reading:
                    # a record returned again is written again (its last copy wins) and counted once
                    content = content_key(item) if item.get("id") in (None, "") else ""
                    if content:
                        tracker.saw(item, content)
                    key = record_key(item, previous) if not content or previous else content
                    first = key not in keys
                    keys.add(key)
                    if first and census:
                        census.saw(item)
                    kept = scope.keeps(spec, item)
                    if area is not None and area.key and first:  # under a proven filter
                        if kept is False:  # another business area or country came through: never written
                            area.strays += 1
                        elif area.sees(item) is None:
                            area.unnamed += 1
                    if kept is None and (link or by_reference):
                        if link:
                            named = link.named(item)
                            kept = bool(named) and named in link.index
                            through_pd += first and kept
                        else:
                            kept = links.holds_number(item.get("reference_number"))
                            by_number += first and kept
                            if kept:
                                writer.by_number.add(content or content_key(item))
                        unnamed_skipped += first and not kept
                        if not kept:
                            other += first
                            continue
                    elif kept is False:
                        # another business area's record naming a programme document NeuroDB holds by its
                        # reference number (it carries the country's prefix): the business area key may be off
                        number = item.get("reference_number")
                        named_here += first and by_reference and links.holds_number(number)
                        other += first
                        continue
                    else:
                        unnamed += first and kept is None
                    if census and first:
                        census.kept(item)
                    if content:
                        waiting[content] = item
                        continue
                    yield item

            process_items(run, items(), writer.write, _label)
            # the records without an id, under the key the read proved unique (none chosen under a filter)
            writer.identity = identity_key(tracker, previous, filtered=area is not None and bool(area.key))
            process_items(run, waiting.values(), writer.write, _label)
            whole = reading.whole and not (area and area.strays)
            details = {**links.details(), **writer.finish(whole=whole)}
            if other:
                details["other_country_skipped"] = other
            details.update(reading.details())  # the first page's count, never the last window's
            if writer.seen and not whole:
                details.update(_removal_skipped(reading))
                if area and area.strays:  # another business area came through the filter
                    details["area_strays"] = area.strays
            if area is not None:
                details.update(area.details())
            if tracker.seen or previous:  # the key the records without an id are held under ("": their hash)
                details["record_identity"] = writer.identity
            if census:  # what was asked, from where, and what came back: counts and codes only
                choice = scope.choice or AreaCode()  # resolved by the request above
                details.update(**choice.details(), **census.details())
                if unnamed:
                    details["country_unnamed"] = unnamed
                    if not set(census.codes) - {NO_CODE}:  # no record named a code: the filter is unconfirmed
                        details["partial"] = True
                if through_pd:
                    details["kept_by_programme_document"] = through_pd
                if by_number:
                    details["kept_by_reference"] = by_number
                if unnamed_skipped:
                    details["unnamed_skipped"] = unnamed_skipped
                if by_reference:  # are the records kept the country's? linked to its PDs, and none elsewhere
                    details["linked_to_etools"] = len(writer.linked)
                    details["other_code_named_here"] = named_here
                if not writer.seen:  # nothing kept: Data health warns, naming the code
                    details["empty_response"] = True
                    if choice.found:  # a code NeuroDB found may be wrong: PARTIAL, pointing to the admin's
                        details["partial"] = True
            if name in AFTER_DOCUMENTS:  # PRP's progress reports linked to their programme document
                details["progress_reports_pd_link"] = AFTER_DOCUMENTS[name]()
            return details

        return _run(run, body)

    sync.__name__ = f"sync_{name}"
    return sync


def _nightly_order(names: Iterable[str]) -> list[str]:
    """The datasets in their order, the funds reservation headers moved just before their lines: a line
    without a number of its own links by its header's (:meth:`Links.header_number`), so the lines read
    tonight's headers. The headers read no line."""
    order = [name for name in names if name != "funds_reservation_headers"]
    order.insert(order.index("funds_reservations"), "funds_reservation_headers")
    return order


ENTITY_SYNCS: dict[str, Callable[..., SyncRun]] = {
    "locations": sync_locations,  # first: programme documents, indicators, visits... link to them
    "location_sites": sync_sites,
    "partners": sync_legacy("partners", "partners", upsert_partner),
    "interventions": sync_legacy("interventions", "interventions", upsert_intervention),
    "intervention_budgets": sync_legacy("intervention_budgets", "interventions-budget", update_budget),
    "agreements": sync_legacy("agreements", "partners/agreements", update_agreement),
    **{name: sync_dataset(name) for name in _nightly_order(DATASETS) if name != "audit_findings"},
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
