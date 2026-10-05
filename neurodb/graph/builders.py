"""Collecting the hub from every source. Each ``add_*`` reads one source and adds its entities and
the links it knows; a source that fails is reported and the others still build."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from django.urls import reverse

from .models import Entity
from .store import Collector, key_of

logger = logging.getLogger(__name__)
K = Entity.Kind


def _url(name: str, *args) -> str:
    try:
        return reverse(name, args=args)
    except Exception:  # noqa: BLE001 - a page that is not there gives no link
        return ""


def _amount(value) -> float | None:
    """A money figure stored as text in the eTools tables."""
    try:
        return round(float(str(value).replace(",", "")), 2) if value not in (None, "") else None
    except ValueError:
        return None


class Names:
    """Find an entity from a name as another source writes it (partners, places, sections)."""

    def __init__(self) -> None:
        self.by_name: dict[str, dict[str, tuple[str, str]]] = {}

    def add(self, kind: str, ref: tuple[str, str], *names: str) -> None:
        for name in names:
            k = key_of(name)
            if k:
                self.by_name.setdefault(kind, {}).setdefault(k, ref)

    def get(self, kind: str, name: str | None) -> tuple[str, str] | None:
        return self.by_name.get(kind, {}).get(key_of(name)) if name else None


# ------------------------------------------------------------------------------------- base
def add_sections(c: Collector, names: Names) -> None:
    from neurodb.accounts.models import Section

    for s in Section.objects.all():
        ref = c.entity(K.SECTION, s.pk, s.name, aliases=[s.code])
        names.add(K.SECTION, ref, s.name, s.code or "")


def add_places(c: Collector, names: Names) -> None:
    from neurodb.geo.models import DistrictLocation, GovernorateLocation, Location

    govs = {}
    for g in GovernorateLocation.objects.all():
        ref = c.entity(K.GOVERNORATE, key_of(g.name), g.name, aliases=[g.code])
        govs[g.code] = ref
        names.add(K.GOVERNORATE, ref, g.name)
    for d in DistrictLocation.objects.all():
        ref = c.entity(K.DISTRICT, key_of(d.name), d.name, aliases=[d.code])
        names.add(K.DISTRICT, ref, d.name)
        c.edge(ref, "part_of", govs.get(d.gov_code), "ActivityInfo places")
    # the eTools gazetteer: governorates (level 1) and districts (level 2), same names merge
    for loc in Location.objects.filter(type__admin_level__in=(1, 2)).select_related("type", "parent"):
        kind = K.GOVERNORATE if loc.type.admin_level == 1 else K.DISTRICT
        ref = names.get(kind, loc.name) or c.entity(kind, key_of(loc.name), loc.name)
        c.entity(*ref, loc.name, aliases=[loc.p_code])
        names.add(kind, ref, loc.name, loc.p_code)
        if kind == K.DISTRICT and loc.parent_id and loc.parent:
            c.edge(ref, "part_of", names.get(K.GOVERNORATE, loc.parent.name), "eTools locations")


def add_partners(c: Collector, names: Names) -> None:
    from neurodb.partnerships.models import PartnerLink, PartnerOrganization

    labels: dict[int, list[str]] = {}
    for link in PartnerLink.objects.filter(partner__isnull=False):
        labels.setdefault(link.partner_id, []).append(link.label)
    for p in PartnerOrganization.objects.all():
        ref = c.entity(
            K.PARTNER,
            p.pk,
            p.name,
            aliases=[p.short_name, p.alternate_name, p.vendor_number, *labels.get(p.pk, [])],
            url=_url("reports:partner_profile", p.pk),
            attrs={
                k: v
                for k, v in (
                    ("type", p.partner_type),
                    ("cso_type", p.cso_type),
                    ("risk_rating", p.rating),
                    ("deleted_in_etools", p.deleted_flag or None),
                )
                if v
            },
            lookup={"tool": "partner_details", "args": {"partner_id": p.pk}},
        )
        names.add(K.PARTNER, ref, p.name, p.short_name or "", p.alternate_name or "", *labels.get(p.pk, []))


# ---------------------------------------------------------------------------- partnerships
def add_programmes(c: Collector, names: Names) -> None:
    from neurodb.partnerships.models import PCA

    places = {}
    from neurodb.geo.models import Location

    for loc in Location.objects.filter(type__admin_level__in=(1, 2)).select_related("type"):
        places[loc.pk] = (K.GOVERNORATE if loc.type.admin_level == 1 else K.DISTRICT, loc.name)
    parents = dict(Location.objects.values_list("pk", "parent_id"))

    def place_refs(location_ids):
        out = set()
        for pk in location_ids:
            seen = 0
            while pk and seen < 8:  # walk up the tree to the district and the governorate
                if pk in places:
                    kind, name = places[pk]
                    ref = names.get(kind, name)
                    if ref:
                        out.add(ref)
                pk, seen = parents.get(pk), seen + 1
        return out

    pd_locations: dict[int, list[int]] = {}
    for pd_id, loc_id in PCA.locations.through.objects.values_list("pca_id", "location_id"):
        pd_locations.setdefault(pd_id, []).append(loc_id)
    for pd in PCA.objects.all():
        number = (pd.number or "").strip()
        ref = c.entity(
            K.PROGRAMME,
            pd.pk,
            f"{number} {pd.title or ''}".strip() or f"PD {pd.pk}",
            aliases=[number, re.sub(r"-\d+$", "", number), pd.title],
            url=_url("reports:programme_detail", pd.pk),
            attrs={
                k: v
                for k, v in (
                    ("status", pd.status),
                    ("type", pd.document_type),
                    ("start", pd.start.isoformat() if pd.start else None),
                    ("end", pd.end.isoformat() if pd.end else None),
                    ("country_programme", pd.country_programme),
                )
                if v
            },
            lookup={"tool": "programme_details", "args": {"number": number}} if number else {},
            snapshot={
                "budget": _amount(pd.total_budget),
                "disbursed": _amount(pd.actual_amount),
                "outstanding": _amount(pd.frs_total_outstanding_amt),
            },
        )
        if pd.partner_id:
            c.edge(ref, "implemented_by", (K.PARTNER, str(pd.partner_id)), "eTools")
        for section in pd.section_names or []:
            c.edge(ref, "in_section", names.get(K.SECTION, section), "eTools")
        for donor in pd.donors or []:
            if key_of(donor):
                donor_ref = c.entity(
                    K.DONOR, key_of(donor), donor, lookup={"tool": "donor_funding", "args": {"donor": donor}}
                )
                c.edge(ref, "funded_by", donor_ref, "eTools")
        for grant in pd.grants or []:
            if grant:
                c.edge(ref, "has_grant", c.entity(K.GRANT, str(grant).strip(), str(grant).strip()), "eTools")
        for place in place_refs(pd_locations.get(pd.pk, [])):
            c.edge(ref, "takes_place_in", place, "eTools locations")


def add_grants(c: Collector, names: Names) -> None:
    from neurodb.datamart.models import Grant

    for g in Grant.objects.all():
        ref = c.entity(
            K.GRANT,
            (g.name or "").strip(),
            g.name,
            description=g.description,
            attrs={"expiry": g.expiry.isoformat()} if g.expiry else {},
        )
        if key_of(g.donor):
            donor = c.entity(
                K.DONOR,
                key_of(g.donor),
                g.donor,
                lookup={"tool": "donor_funding", "args": {"donor": g.donor}},
            )
            c.edge(donor, "funds_through", ref, "eTools Datamart grants")


# ---------------------------------------------------------------------------- ActivityInfo
def add_activityinfo(c: Collector, names: Names) -> None:
    from django.db.models import Count, Max

    from neurodb.facts.models import ActivityReportNew
    from neurodb.indicators.models import Database, MasterIndicator, NeuroReport, NeuroReportMasterIndicator
    from neurodb.partnerships.models import PartnerLink

    reported = {  # the current year's databases: new monthly reports show as a change
        row["dbase_id"]: {"reports": row["reports"], "latest_month": row["latest"]}
        for row in ActivityReportNew.objects.filter(dbase__reporting_year__current=True)
        .values("dbase_id")
        .annotate(reports=Count("id"), latest=Max("month"))
    }
    for db in Database.objects.select_related("reporting_year", "section"):
        year = db.reporting_year.year if db.reporting_year_id else db.year
        ref = c.entity(
            K.DATABASE,
            db.pk,
            db.label or db.name,
            aliases=[db.name, db.sector_label, db.hpm_label],
            url=_url("reports:database_dashboard", db.pk),
            attrs={"year": year},
            lookup={"tool": "database_results", "args": {"database_id": db.pk}},
            snapshot=reported.get(db.pk),
        )
        if db.section_id:
            c.edge(ref, "in_section", (K.SECTION, str(db.section_id)), "ActivityInfo")
    for m in MasterIndicator.objects.filter(is_active=True).select_related("database__reporting_year"):
        db = m.database
        c.entity(
            K.MASTER_INDICATOR,
            m.pk,
            m.name,
            aliases=[m.awp_code],
            attrs={
                k: v
                for k, v in (
                    ("awp_code", m.awp_code),
                    ("unit", m.unit),
                    ("year", db.reporting_year.year if db.reporting_year_id else db.year),
                )
                if v
            },
            url=_url("reports:database_dashboard", db.pk),
            lookup={"tool": "indicator_breakdown", "args": {"database_id": db.pk, "master_id": m.pk}},
        )
        c.edge((K.MASTER_INDICATOR, str(m.pk)), "part_of", (K.DATABASE, str(db.pk)), "ActivityInfo")
    for link in PartnerLink.objects.filter(partner__isnull=False):
        for db_id in link.database_ids or []:
            c.edge(
                (K.PARTNER, str(link.partner_id)),
                "reports_in",
                (K.DATABASE, str(db_id)),
                "ActivityInfo records",
                weight=link.records or 1,
            )
    for r in NeuroReport.objects.select_related("ryear"):
        c.entity(
            K.REPORT,
            r.pk,
            r.name,
            aliases=[r.report_code],
            attrs={"year": r.ryear.year if r.ryear_id else None, "hpm": r.is_hpm},
            url=_url("reports:report_hpm" if r.is_hpm else "reports:report_dashboard", r.pk),
            lookup={"tool": "neuro_report", "args": {"report_id": r.pk}},
        )
    for report_id, master_id in NeuroReportMasterIndicator.objects.values_list("report_id", "master_id"):
        c.edge((K.REPORT, str(report_id)), "includes", (K.MASTER_INDICATOR, str(master_id)), "Neuro reports")


# -------------------------------------------------------------------------- country programme
def add_country_programme(c: Collector, names: Names) -> None:
    import datetime

    from neurodb.cpd import services
    from neurodb.cpd.models import CountryProgramme, Indicator, Link, Outcome, Output
    from neurodb.cpd.services import interventions, output_matches

    today = datetime.date.today()
    progress = {}  # indicator id -> where it stands today, so a status or value change shows

    for p in CountryProgramme.objects.all():
        c.entity(
            K.CPD_CYCLE,
            p.pk,
            p.name,
            attrs={"years": f"{p.start_year}-{p.end_year}", "current": p.current},
            url=_url("cpd:dashboard") + f"?cycle={p.pk}",
            lookup={"tool": "country_programme", "args": {"cycle": p.pk}},
        )
        indicators = list(
            Indicator.objects.filter(programme=p).prefetch_related("values", "milestones", "links")
        )
        values = services.link_values([lk for i in indicators for lk in i.links.all() if lk.confirmed])
        elapsed = services.elapsed_share(p, today)
        for i in indicators:
            state = services.progress(i, values, today, elapsed)
            progress[i.pk] = {
                "value": round(state.value, 2) if state.value is not None else None,
                "achieved_pct": state.achieved,
                "status": str(state.label),
            }
        pds = interventions(p)
        for output in Output.objects.filter(outcome__programme=p):
            for pd in pds:
                if any(output_matches(output, n) for n in (pd.cp_outputs or [])):
                    c.edge(
                        (K.PROGRAMME, str(pd.pk)),
                        "contributes_to",
                        (K.CPD_OUTPUT, str(output.pk)),
                        "eTools CP outputs",
                    )
    for o in Outcome.objects.select_related("programme"):
        ref = c.entity(
            K.CPD_OUTCOME,
            o.pk,
            f"Outcome {o.code}: {o.title}",
            aliases=[o.code],
            url=_url("cpd:dashboard") + f"?cycle={o.programme_id}#outcome-{o.pk}",
            lookup={"tool": "country_programme", "args": {"cycle": o.programme_id}},
        )
        c.edge(ref, "part_of", (K.CPD_CYCLE, str(o.programme_id)), "country programme")
        for section in (o.sections or "").split(","):
            c.edge(ref, "in_section", names.get(K.SECTION, section.strip()), "country programme")
    for o in Output.objects.select_related("outcome"):
        ref = c.entity(
            K.CPD_OUTPUT,
            o.pk,
            f"Output {o.code}: {o.title}",
            aliases=[o.code, o.etools_output],
            url=_url("cpd:dashboard") + f"?cycle={o.outcome.programme_id}#outcome-{o.outcome_id}",
            lookup={"tool": "country_programme", "args": {"cycle": o.outcome.programme_id}},
        )
        c.edge(ref, "part_of", (K.CPD_OUTCOME, str(o.outcome_id)), "country programme")
    for i in Indicator.objects.all():
        ref = c.entity(
            K.CPD_INDICATOR,
            i.pk,
            f"{i.code} {i.title}".strip(),
            aliases=[i.code],
            attrs={
                k: v
                for k, v in (("baseline", i.baseline), ("target", i.target), ("unit", i.unit))
                if v is not None
            },
            url=_url("cpd:indicator", i.pk),
            lookup={"tool": "cpd_indicator", "args": {"indicator_id": i.pk}},
            snapshot=progress.get(i.pk),
        )
        parent = (K.CPD_OUTPUT, str(i.output_id)) if i.output_id else (K.CPD_OUTCOME, str(i.outcome_id))
        c.edge(ref, "measures", parent, "country programme")
    for link in Link.objects.filter(confirmed=True):
        ref = (K.CPD_INDICATOR, str(link.indicator_id))
        if link.kind == Link.Kind.ETOOLS and link.pd_id:
            c.edge(ref, "linked_to", (K.PROGRAMME, str(link.pd_id)), "country programme links")
        elif link.kind == Link.Kind.ACTIVITYINFO and link.master_id:
            c.edge(ref, "linked_to", (K.MASTER_INDICATOR, str(link.master_id)), "country programme links")
        elif link.kind == Link.Kind.YOUTH:
            c.edge(
                ref,
                "linked_to",
                (K.YOUTH_INDICATOR, f"{link.youth_level}:{link.youth_id}"),
                "country programme links",
            )
        elif link.kind == Link.Kind.EDUCATION:
            c.edge(
                ref, "linked_to", (K.EDUCATION_PROGRAMME, link.education_programme), "country programme links"
            )


# ------------------------------------------------------------------------------------ Compiler
def add_compiler(c: Collector, names: Names) -> None:
    from neurodb.youth.figures import Figures, indicator_label
    from neurodb.youth.models import YouthFigures, YouthIndicatorLink

    record = YouthFigures.objects.order_by("-year").first()
    if record:
        for item in Figures(record.payload).payload.get("indicators", []):
            c.entity(
                K.YOUTH_INDICATOR,
                f"{item['level']}:{item['id']}",
                indicator_label(item),
                attrs={"level": item["level"], "year": record.year},
                url=_url("youth:dashboard"),
                lookup={"tool": "youth_figures", "args": {"year": record.year}},
            )
    for link in YouthIndicatorLink.objects.filter(pd__isnull=False):
        c.edge(
            (K.YOUTH_INDICATOR, f"{link.level}:{link.youth_indicator_id}"),
            "linked_to",
            (K.PROGRAMME, str(link.pd_id)),
            "youth links" if link.source == "confirmed" else "youth links (suggested)",
        )
    for key, name, url in (
        ("mscc", "Makani (MSCC)", "education:makani"),
        ("bridging", "Dirasa (Bridging)", "education:dirasa"),
    ):
        c.entity(
            K.EDUCATION_PROGRAMME,
            key,
            name,
            aliases=["Makani" if key == "mscc" else "Dirasa", key],
            url=_url(url),
            lookup={
                "tool": "education_figures",
                "args": {"programme": "makani" if key == "mscc" else "dirasa"},
            },
        )
    from neurodb.wellbeing.models import CenterSummary

    for s in CenterSummary.objects.order_by("center_id", "-month").distinct("center_id"):
        ref = c.entity(
            K.CENTRE,
            s.center_id,
            s.center_name,
            attrs={"partner": s.partner_name, "governorate": s.governorate},
            url=_url("wellbeing:summaries"),
            lookup={"tool": "makani_wellbeing", "args": {"centre": s.center_name}},
            snapshot={  # centre totals of its latest month
                "month": s.month.isoformat(),
                **{k: v for k, v in (s.figures or {}).items() if isinstance(v, int | float)},
            },
        )
        c.edge(ref, "part_of", (K.EDUCATION_PROGRAMME, "mscc"), "Compiler")
        c.edge(ref, "run_by", names.get(K.PARTNER, s.partner_name), "Compiler (partner name)")
        c.edge(ref, "located_in", names.get(K.GOVERNORATE, s.governorate), "Compiler")


# ----------------------------------------------------------------------------------- documents
def add_documents(c: Collector, names: Names) -> None:
    from neurodb.geo.models import DistrictLocation, GovernorateLocation
    from neurodb.knowledge.models import Document, Link
    from neurodb.library.models import Map, Resource

    gov = dict(GovernorateLocation.objects.values_list("pk", "name"))
    dist = dict(DistrictLocation.objects.values_list("pk", "name"))
    indexed_library = set()
    for d in Document.objects.filter(status=Document.Status.READY):
        ref = c.entity(
            K.DOCUMENT,
            d.pk,
            d.title,
            aliases=[d.source],
            description=d.summary,
            attrs={
                k: v
                for k, v in (
                    ("year", d.year),
                    ("date", d.document_date.isoformat() if d.document_date else None),
                    ("from", d.get_origin_display() if hasattr(d, "get_origin_display") else None),
                )
                if v
            },
            url=d.get_absolute_url(),
            lookup={"tool": "read_knowledge", "args": {"document_id": d.pk}},
        )
        if getattr(d, "origin", "") == "library" and d.origin_id:
            indexed_library.add(d.origin_id)
        if d.section_id:
            c.edge(ref, "in_section", (K.SECTION, str(d.section_id)), "knowledge base")
        for link in d.links.all():
            target = {
                Link.Kind.PARTNER: (K.PARTNER, str(link.object_id)),
                Link.Kind.PROGRAMME: (K.PROGRAMME, str(link.object_id)),
                Link.Kind.SECTION: (K.SECTION, str(link.object_id)),
                Link.Kind.GOVERNORATE: names.get(K.GOVERNORATE, gov.get(link.object_id)),
                Link.Kind.DISTRICT: names.get(K.DISTRICT, dist.get(link.object_id)),
            }.get(link.kind)
            c.edge(ref, "mentions", target, "knowledge base", weight=link.mentions or 1)
    for r in Resource.objects.filter(published=True).defer("resource_file", "resource_image"):
        if r.pk in indexed_library:
            continue
        ref = c.entity(
            K.DOCUMENT,
            f"library:{r.pk}",
            r.title,
            description=r.description,
            attrs={"year": r.publication_year, "from": "library"},
            url=_url("reports:library_item", r.pk),
        )
        c.edge(ref, "in_section", names.get(K.SECTION, r.section), "library")
    for mp in Map.objects.all():
        c.entity(K.MAP, mp.pk, mp.name, description=mp.description, url=_url("reports:map_item", mp.pk))


# -------------------------------------------------------------------------------- daily review
def add_review(c: Collector, names: Names) -> None:
    from neurodb.partnerships.models import PCA
    from neurodb.review.models import DailyReview

    review = DailyReview.objects.filter(status="succeeded").order_by("-date").first()
    if not review:
        return
    numbers = {}
    for pk, number in PCA.objects.exclude(number__isnull=True).values_list("pk", "number"):
        numbers[number.lower()] = pk
        numbers.setdefault(re.sub(r"-\d+$", "", number.lower()), pk)
    for f in review.findings.exclude(state="resolved"):
        ref = c.entity(
            K.FINDING,
            f.key,
            f.title,
            description=f.detail,
            attrs={"severity": f.severity, "date": review.date.isoformat(), "state": f.state},
            url=f.url,
            lookup={"tool": "daily_review", "args": {}},
        )
        c.edge(ref, "in_section", names.get(K.SECTION, f.section), "daily review")
        for token in f.key.split(":")[1:]:
            if token.lower() in numbers:
                c.edge(ref, "about", (K.PROGRAMME, str(numbers[token.lower()])), "daily review")
            elif partner := names.get(K.PARTNER, token):
                c.edge(ref, "about", partner, "daily review")


# ------------------------------------------------------------------------- field monitoring
def _add_field_monitoring(c: Collector, names: Names) -> None:
    """The field monitoring visits of Monitoring insights (``neurodb.fmm.hub``), read through a lazy
    import; nothing when that app is not installed or switched off."""
    from django.apps import apps
    from django.conf import settings

    if not apps.is_installed("neurodb.fmm") or not getattr(settings, "FMM_ENABLED", False):
        return
    from neurodb.fmm import hub

    hub.add_field_monitoring(c, names)


SOURCES: list[tuple[str, Callable[[Collector, Names], None]]] = [
    ("sections", add_sections),
    ("places", add_places),
    ("partners", add_partners),
    ("programme documents", add_programmes),
    ("grants", add_grants),
    ("ActivityInfo", add_activityinfo),
    ("country programme", add_country_programme),
    ("Compiler", add_compiler),
    ("documents", add_documents),
    ("daily review", add_review),
    ("field monitoring", _add_field_monitoring),
]


def collect() -> tuple[Collector, dict[str, str]]:
    c, names, errors = Collector(), Names(), {}
    for label, add in SOURCES:
        c.source = label
        try:
            add(c, names)
        except Exception as exc:
            logger.exception("knowledge hub: %s failed", label)
            errors[label] = f"{type(exc).__name__}: {exc}"[:500]
    return c, errors
