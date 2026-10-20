"""Builders for the eTools master indicator tests: programme documents, their PD indicators (one Datamart row per
planned location, tagged from the title as the sync tags them) and partner reports, and master indicators with
their years and links. The figures are read through the real partner monitoring rows (Reached computed from the
reports), as the Master indicators tab reads them. Generic names only: Partner A, PD/2026/001."""

from __future__ import annotations

import datetime
import itertools
from decimal import Decimal

from neurodb.datamart import masters, monitoring
from neurodb.datamart import models as dm
from neurodb.datamart.reached import norm
from neurodb.datamart.tags import tags_of
from neurodb.partnerships.models import PCA, PartnerOrganization

TODAY = datetime.date(2026, 10, 15)  # the suite's day (tests/conftest.py)
YEAR = 2026
EDUCATION, CP = "Education", "Child Protection"
_ids = itertools.count(1)


def day(month: int, date: int, year: int = YEAR) -> datetime.date:
    return datetime.date(year, month, date)


def programme(
    letter: str,
    number: int,
    *,
    start=datetime.date(2026, 1, 1),
    end=datetime.date(2026, 12, 31),
    cp_outputs=None,
    status="active",
) -> PCA:
    """Programme document PD/2026/00<number> of Partner <letter>."""
    partner, _created = PartnerOrganization.objects.get_or_create(
        etl_id=f"partner-{letter}",
        defaults={"name": f"Partner {letter}", "partner_type": "CSO", "vendor_number": f"V-{letter}"},
    )
    return PCA.objects.create(
        etl_id=f"pd-{number}",
        partner=partner,
        partner_name=partner.name,
        number=f"PD/2026/{number:03d}",
        title=f"Programme document {number}",
        status=status,
        start=start,
        end=end,
        cp_outputs=cp_outputs,
    )


def indicator(
    pd: PCA,
    source_id: int | None,
    title: str,
    *,
    section: str = EDUCATION,
    display_type: str = "number",
    target=(None, None),
    places=("Akkar",),
    cp_output: str = "",
    **extra,
) -> None:
    """A PD indicator, one Datamart row per planned location, its tags read from its title."""
    found = tags_of(title).as_dict()
    unit = {"percentage": "percentage", "text": "days"}.get(display_type, "number")
    for place in places:
        dm.PDIndicator.objects.create(
            datamart_id=next(_ids),
            source_id=source_id,
            intervention=pd,
            pd_reference_number=pd.number,
            title=title,
            section_name=section,
            lower_result_name="1.1 Services",
            cp_output=cp_output,
            display_type=display_type,
            unit=unit,
            target_numerator=None if target[0] is None else Decimal(str(target[0])),
            target_denominator=None if target[1] is None else Decimal(str(target[1])),
            location_name=place,
            **{f"tag_{key}": value for key, value in found.items()},
            **extra,
        )


def report(pd, source_id, title, number, end, values=None, *, kind="QPR", status="Accepted", **extra) -> None:
    """One report of one indicator, a row per location: ``values`` maps a location to PRP's "()" value (a
    number, or its ``{"v", "d", "c"}``); ``places`` with ``extra`` alone writes rows without it."""
    places = extra.pop("places", None)
    rows = values.items() if values is not None else ((place, None) for place in places)
    for place, value in rows:
        if value is None:
            disaggregation = {}
        else:
            disaggregation = {"()": value if isinstance(value, dict) else {"v": value, "d": 1, "c": value}}
        dm.ReportedIndicator.objects.create(
            datamart_id=next(_ids),
            partner=pd.partner,
            intervention=pd,
            pd_reference_number=pd.number,
            progress_report=f"{pd.number}-{number}",
            indicator_report=f"{pd.number}-{number}-{source_id}",
            report_number=number,
            report_type=kind,
            report_status=status,
            period_start=end - datetime.timedelta(days=89 if kind == "QPR" else 29),
            period_end=end,
            due_date=end + datetime.timedelta(days=15),
            submission_date=end + datetime.timedelta(days=10)
            if status in ("Accepted", "Submitted")
            else None,
            indicator=title,
            etools_indicator_id=str(source_id or ""),
            location=place,
            disaggregation=disaggregation,
            **extra,
        )


def pct(v: float, d: float) -> dict:
    """A percentage or ratio reported with its numerator and denominator."""
    return {"v": v, "d": d, "c": v / d if d else 0}


def master(
    name: str, method: str = "sum", *, section: str = EDUCATION, target=None, code="", year=YEAR, **extra
):
    """A master indicator, with its settings for ``year`` when a target or a code is given."""
    found = dm.EtoolsMaster.objects.create(section=section, name=name, method=method, **extra)
    if target is not None or code:
        dm.EtoolsMasterYear.objects.create(master=found, year=year, target=target, code=code)
    return found


def link(
    to: dm.EtoolsMaster,
    pd: PCA,
    source_id: int | None,
    *,
    title: str = "",
    role: str = "total",
    status: str = "confirmed",
    unit_class: str = "number",
) -> dm.EtoolsMasterLink:
    """A link as the curation saves it (no clean(): the figures must hold whatever was saved)."""
    ident = f"s{source_id}" if source_id else masters.ident_code((pd.id, f"t{norm(title)}"))
    return dm.EtoolsMasterLink.objects.create(
        master=to,
        pd=pd,
        ident=ident,
        title=title,
        pd_number=pd.number,
        unit_class=unit_class,
        role=role,
        status=status,
    )


def rows(year: int | None = YEAR, **filters) -> list[monitoring.Row]:
    """The year's PD indicator rows as the Master indicators tab reads them (every PD running in the year)."""
    return monitoring.as_rows(
        monitoring.indicators(monitoring.Filters(year=year, scope="year", **filters), TODAY)
    )


def figure(of: dm.EtoolsMaster, found=None, year: int = YEAR, **kwargs) -> masters.Figure:
    """One master's figures in ``year``, its links and years read as a page reads them (prefetched)."""
    loaded = dm.EtoolsMaster.objects.prefetch_related("links", "years").get(pk=of.pk)
    loaded.method, loaded.pd_parts_add_up = of.method, of.pd_parts_add_up  # unsaved changes count too
    return masters.figures([loaded], rows(year) if found is None else found, year, TODAY, **kwargs)[0]


def subs(fig: masters.Figure) -> dict[str, masters.Sub]:
    """A figure's subs by their indicator's identity ("s101")."""
    return {sub.link.ident: sub for sub in fig.subs}
