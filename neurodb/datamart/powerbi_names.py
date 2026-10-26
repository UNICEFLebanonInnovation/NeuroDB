"""The PRP Power BI report's own names, in one place: its pages in their order, the visuals and cards NeuroDB
compares with each of its pages, and each page's slicers and other filters (search boxes, date pickers,
drop-downs), as the report itself writes them (the user's print of 26 Oct 2026: SPEC_pbi_findings'
PBI_REPORT_REFERENCE). Correct a name here, and only here: the Power BI comparison sheet
(``comparison``), Compare with Power BI's recognition of the exports (``powerbi_exports``) and every sentence
that names a page, a visual, a card or a slicer (``powerbi_words``, its *How to export* too) read it here.

Nothing here is data: no partner, programme document or person.
"""

from __future__ import annotations

POWERBI_REPORT = "UNICEF Program Performance"
# the pages, as the report's page list names them (PD/SPD Indicator's own title says "PD/SPD Indicators")
POWERBI_PAGE_ORDER = (
    "Program Performance",
    "Partner Performance",
    "Reporting Progress",
    "Data Quality",
    "PD/SPD Indicator",
    "PD/SPD Report",
    "PD/SPD Report/Location",
    "PD/SPD Report/Disaggregation",
    "Partner Fund Monitoring",
    "Funded Programs",
)
(
    PROGRAM_PERFORMANCE,
    PARTNER_PERFORMANCE,
    REPORTING_PROGRESS,
    DATA_QUALITY,
    PD_INDICATORS,
    PD_REPORT,
    PD_LOCATION,
    PD_DISAGGREGATION,
    FUND_MONITORING,
    FUNDED_PROGRAMS,
) = POWERBI_PAGE_ORDER
# each card of the report a NeuroDB figure or an export of Compare with Power BI is compared with, by its key:
# (page, the card's name). A card's export holds one column, the card's name
POWERBI_CARDS: dict[str, tuple[str, str]] = {
    "partners_count": (PARTNER_PERFORMANCE, "# of Partners"),
    "partners_reported": (PARTNER_PERFORMANCE, "# of Partners with at least 1 report"),
    "partners_not_reported": (PARTNER_PERFORMANCE, "# of Partners not reported"),
    "accepted_report": (REPORTING_PROGRESS, "Accepted Report"),
    "submitted_report": (REPORTING_PROGRESS, "Submitted Report"),
    "due_report": (REPORTING_PROGRESS, "Due Report"),
    "overdue_report": (REPORTING_PROGRESS, "Overdue Report"),
    "pd_count": (DATA_QUALITY, "PD Count"),
    "active_pds": (DATA_QUALITY, "Active PDs"),
    "ended_pds": (DATA_QUALITY, "Ended PDs"),
    "dq_total": (DATA_QUALITY, "No. of Indicators (Total)"),
    "dq_with_reached": (DATA_QUALITY, "No. of Indicators with Reached"),
    "dq_tiles": (DATA_QUALITY, "No. of Indicators without Reached"),
    "dq_with_target": (DATA_QUALITY, "No. of Indicators with Target"),
    "dq_without_target": (DATA_QUALITY, "No. of Indicators without Target"),
}
# each visual (a table or a chart) a block of the sheet or an export of Compare with Power BI is compared
# with, by its key: (page, visual); a page's one table is "Table". With the cards, every visual NeuroDB
# compares
POWERBI_VISUALS: dict[str, tuple[str, str]] = {
    "pp_indicators": (PROGRAM_PERFORMANCE, "Table"),
    "pp_sections": (PROGRAM_PERFORMANCE, "Section buttons"),
    "partner_cards": (
        PARTNER_PERFORMANCE,
        "# of Partners with (Accepted, Submitted, Sent back, Due, and Overdue)",
    ),
    "follow_up": (PARTNER_PERFORMANCE, "Partners require follow up (Overdue, Sent back, Submitted & Due)"),
    "by_section": (REPORTING_PROGRESS, "Reporting Progress by Sector"),
    "by_month": (REPORTING_PROGRESS, "# of Reports by Type over Reporting Date"),
    "missing_targets": (DATA_QUALITY, "Indicators with Missing Targets"),
    "dq_list": (DATA_QUALITY, "Indicators without Reached or with Zero Reached"),
    "pd_indicators": (PD_INDICATORS, "Table"),
    "report_list": (PD_REPORT, "Table"),
    "locations": (PD_LOCATION, "Table"),
    "disaggregations": (PD_DISAGGREGATION, "Table"),
    "fund_monitoring": (FUND_MONITORING, "Table"),
    "funded_programmes": (FUNDED_PROGRAMS, "Table"),
    **POWERBI_CARDS,
}
# each page's slicers, in the report's order
POWERBI_PAGE_SLICERS: dict[str, tuple[str, ...]] = {
    PROGRAM_PERFORMANCE: (
        "Country name",
        "PD status",
        "Emergency indicator",
        "Partner",
        "Document type",
        "Reporting Year",
        "Office",
    ),
    PARTNER_PERFORMANCE: (
        "Country name",
        "PD status",
        "Section",
        "Partner",
        "Partner type",
        "Report status",
        "Report type",
    ),
    REPORTING_PROGRESS: (
        "Country name",
        "PD status",
        "Section",
        "Reporting year",
        "Report #",
        "Report status",
        "Partner",
    ),
    DATA_QUALITY: (
        "Country name",
        "PD status",
        "Section",
        "Reporting year",
        "Partner",
        "Partner type",
        "Report type",
    ),
    PD_INDICATORS: (
        "Country name",
        "PD status",
        "Emergency indicator",
        "Office",
        "Document type",
        "Section",
        "Partner",
    ),
    PD_REPORT: (
        "Country name",
        "PD status",
        "Reporting year",
        "Report type",
        "Report status",
        "Report #",
        "Section",
    ),
    PD_LOCATION: (
        "Country name",
        "PD status",
        "Reporting year",
        "Report type",
        "Report status",
        "Emergency indicator",
        "Section",
    ),
    PD_DISAGGREGATION: (
        "Country name",
        "PD status",
        "Reporting year",
        "Report type",
        "Report status",
        "Emergency indicator",
        "Section",
    ),
    FUND_MONITORING: ("Country name", "PD status", "Donor", "Partner", "Partner Type", "FR #", "Grant #"),
    FUNDED_PROGRAMS: ("Country name", "PD status", "Donor", "Partner", "Partner type", "FR #", "Grant #"),
}
# each page's other filters: its section buttons, search boxes, date pickers and drop-downs
POWERBI_PAGE_BOXES: dict[str, tuple[str, ...]] = {
    PROGRAM_PERFORMANCE: ("Section buttons", "CP Output", "Indicator Title"),
    PD_INDICATORS: ("PD Reference #", "Indicator Title"),
    PD_REPORT: ("Reporting Start Date", "Reporting End Date", "PD Reference #"),
    PD_LOCATION: ("Reporting Start Date", "Reporting End Date", "PD Reference #"),
    PD_DISAGGREGATION: (
        "Report Start Date",
        "Reporting End Date",
        "PD Reference #",
        "Indicator",
        "Location",
        "Location Pcode",
        "Disaggregation",
    ),
    FUND_MONITORING: ("Partner", "PD Reference #"),
    FUNDED_PROGRAMS: ("PD Reference #",),
}
COUNTRY_SLICER = "Country name"
PD_STATUS_SLICER = "PD status"
# the slicers of Power BI's fields an export's "Applied filters" line names, by the slicer's name as matched
# (its case aside: Program Performance writes "Reporting Year", the other pages "Reporting year")
FIELD_SLICERS: dict[str, str] = {
    "country_name": "country name",
    "status": "pd status",
    "reporting_year": "reporting year",
    "section_name": "section",
}


def visual(key: str) -> str:
    """A visual's or a card's name (:data:`POWERBI_VISUALS`)."""
    return POWERBI_VISUALS[key][1]


def slicer(field_or_name: str, pages: tuple[str, ...] = POWERBI_PAGE_ORDER) -> str:
    """A slicer as these pages write it (a Power BI field, :data:`FIELD_SLICERS`, or a slicer's name in any
    case): its spellings on the pages that have it, joined when they differ ("Reporting Year / Reporting
    year"); "" when none of the pages has it."""
    wanted = FIELD_SLICERS.get(field_or_name, field_or_name).casefold()
    found = [s for page in pages for s in POWERBI_PAGE_SLICERS.get(page, ()) if s.casefold() == wanted]
    return " / ".join(dict.fromkeys(found))


def pages_with(name: str, pages: tuple[str, ...] = POWERBI_PAGE_ORDER) -> tuple[str, ...]:
    """The pages that have a slicer of this name (its case aside)."""
    wanted = name.casefold()
    return tuple(p for p in pages if any(s.casefold() == wanted for s in POWERBI_PAGE_SLICERS.get(p, ())))


def to_clear(pages: tuple[str, ...], kept: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Every slicer and other filter of these pages but the country, the PD status and ``kept`` (slicers set
    to NeuroDB's filter), each once (its case aside: its spellings joined when the pages differ), in the
    pages' order."""
    keep = {s.casefold() for s in (COUNTRY_SLICER, PD_STATUS_SLICER, *kept)}
    found: dict[str, list[str]] = {}
    for page in pages:
        for name in (*POWERBI_PAGE_SLICERS.get(page, ()), *POWERBI_PAGE_BOXES.get(page, ())):
            if name.casefold() not in keep:
                spellings = found.setdefault(name.casefold(), [])
                if name not in spellings:
                    spellings.append(name)
    return tuple(" / ".join(spellings) for spellings in found.values())
