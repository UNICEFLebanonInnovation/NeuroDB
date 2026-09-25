"""Every eTools Datamart endpoint: what it holds, how it is limited to the country office, how it
links to NeuroDB's programme documents and partners, and whether NeuroDB reads it.

Three kinds of datasets:

* ``TYPED``: records kept in their own table (``datamart.models``), which the pages use;
* ``DOCUMENTS``: records kept whole in ``DatamartDocument``, for the AI assistant (and admin);
* ``EXCLUDED``: endpoints NeuroDB does not read, with the reason.

The AI assistant's eTools tools (``neurodb.assistant.tools``) query any of the first two by name.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    """A dataset kept in ``DatamartDocument``."""

    path: str  # API path after /api/<version>/ (``datamart/`` is implied unless prp/, sources/, system/)
    description: str
    # How the request is limited to the country office:
    #   country        ``country_name=<ETOOLS_DATAMART_COUNTRY>``
    #   business_area  ``<filter_key>=<the country's business area code>`` (PRP views)
    #   lookup         a small table read whole; only the country's rows are kept (``keep`` field)
    #   written_by     written by another sync (the raw copy of partners, programme documents...)
    scope: str = "country"
    filter_key: str = ""
    keep: str = ""  # lookup: the field compared with the country name or business area code
    partner: tuple[str, str] = ("", "")  # (eTools partner id key, vendor number key)
    intervention: tuple[str, str] = ("", "")  # (eTools PD id key, PD reference number key)
    date: str = ""
    title: tuple[str, ...] = ()


DOCUMENTS: dict[str, Source] = {
    # ---- raw copies of the records that feed other tables (full field set for the assistant)
    "partners": Source(
        "partners",
        "Implementing partners: type, CSO type, vendor number, risk rating, HACT values and minimum "
        "requirements, cash transfers (CP, current year), outstanding DCT amounts, PSEA and SEA risk "
        "ratings, lead office and section, flags (blocked, expiring assessment, approaching threshold).",
        scope="written_by",
        partner=("source_id", "vendor_number"),
        date="last_assessment_date",
        title=("name",),
    ),
    "interventions": Source(
        "interventions",
        "Programme documents (PD/SPD/SSFA): status, dates, partner, agreement, sections, offices, "
        "locations, CP outputs, donors, grants, funding sources, supply items, PRC review and signature "
        "dates, amendments, humanitarian flag, partner selection modality, planned visits.",
        scope="written_by",
        partner=("partner_source_id", "partner_vendor_number"),
        intervention=("source_id", "number"),
        date="start_date",
        title=("number", "title"),
    ),
    "intervention_budgets": Source(
        "interventions-budget",
        "Programme document budgets: total, UNICEF cash, UNICEF supply, partner contribution, currency, "
        "FR numbers.",
        scope="written_by",
        partner=("partner_source_id", "partner_vendor_number"),
        intervention=("source_id", "number"),
        date="start_date",
        title=("number", "title"),
    ),
    "agreements": Source(
        "partners/agreements",
        "Partner agreements (PCA, SSFA, MOU): type, status, dates, signatures, special conditions, "
        "amendments.",
        scope="written_by",
        partner=("", "vendor_number"),
        date="start",
        title=("reference_number", "partner_name"),
    ),
    "audit_results": Source(
        "audit/results",
        "Audit results per engagement: risk rating, audited expenditure, financial findings, refunds, "
        "write-offs, pending unsupported amount, audit opinion, counts of high-risk findings and key "
        "control weaknesses.",
        scope="written_by",
        partner=("", "vendor_number"),
        title=("reference_number", "vendor"),
    ),
    "audits": Source(
        "audit/audit",
        "Audits: audited expenditure, financial findings by category, key internal control weaknesses by "
        "category, opinion, amounts refunded, written off and pending.",
        scope="written_by",
        date="start_date",
        title=("reference_number", "partner_name"),
    ),
    "spot_checks": Source(
        "audit/spot-check-findings",
        "Spot checks: amount tested, ineligible expenditure, high- and low-priority findings, financial "
        "findings by category, dates of each step.",
        scope="written_by",
        date="date_of_field_visit",
        title=("reference_number", "partner_name"),
    ),
    "micro_assessments": Source(
        "audit/micro-assessment",
        "Micro-assessments: overall risk rating and ratings per subject area (organisation, systems, "
        "procurement, reporting, assets, people, sub-partners), findings count.",
        scope="written_by",
        date="date_of_field_visit",
        title=("reference_number", "partner_name"),
    ),
    "special_audits": Source(
        "audit/special-audit",
        "Special audits: value, special procedures, dates.",
        scope="written_by",
        date="date_of_field_visit",
        title=("reference_number", "partner_name"),
    ),
    # ---- programme documents in more detail
    "intervention_locations": Source(
        "interventions-locations",
        "Programme documents by location: one row per PD and location (name, P-code, admin level).",
        partner=("partner_source_id", "partner_vendor_number"),
        intervention=("intervention_id", "number"),
        date="start_date",
        title=("number", "location_name"),
    ),
    "intervention_country_programmes": Source(
        "interventions-country-programmes",
        "The country programme each programme document contributes to.",
        partner=("", "partner_vendor_number"),
        intervention=("", "pd_number"),
        title=("pd_number", "country_programme"),
    ),
    "intervention_epd": Source(
        "interventions-epd",
        "ePD narratives of programme documents: context, implementation strategy, gender, equity and "
        "sustainability ratings and narratives, capacity development, other partners, technical "
        "guidance, cash transfer modalities, HQ support cost, review type, signature date.",
        partner=("", "partner_vendor_number"),
        intervention=("", "pd_number"),
        date="pd_signature_date",
        title=("pd_number", "pd_title"),
    ),
    "intervention_management_budget": Source(
        "interventions-management-budget",
        "Programme management budget lines of programme documents (UNICEF and partner cash per item).",
        partner=("", "partner_vendor_number"),
        intervention=("", "pd_number"),
        title=("pd_number", "name"),
    ),
    "intervention_reviews": Source(
        "interventions-review",
        "PRC and partnership reviews of programme documents: approval, relevance, comparative advantage, "
        "gender/equity considered, budget alignment, supply issues, comments and actions.",
        partner=("", "partner_vendor_number"),
        intervention=("", "pd_number"),
        date="review_date",
        title=("pd_number", "review_type"),
    ),
    "pmp_indicators": Source(
        "pmp-indicators",
        "Partnership management figures per PD/SSFA: budget, UNICEF cash and supply, partner contribution, "
        "FR numbers and planned amounts, core values attachment.",
        partner=("partner_id", "vendor_number"),
        intervention=("intervention_id", "pd_ssfa_ref"),
        date="pd_ssfa_start_date",
        title=("pd_ssfa_ref", "partner_name"),
    ),
    "cp_indicators": Source(
        "reports/indicators",
        "Indicators attached to programme documents with baseline and target, cluster indicator and "
        "response plan.",
        intervention=("", "pd_sffa_reference_number"),
        title=("pd_sffa_reference_number", "label"),
    ),
    "attachments": Source(
        "attachment/attachment",
        "Documents attached in eTools and PRP: file name and type, category, linked partner, agreement, "
        "PD and progress report, upload date.",
        partner=("", "vendor_number"),
        intervention=("", "pd_ssfa_number"),
        date="created",
        title=("filename", "file_type"),
    ),
    # ---- partners and assurance
    "planned_engagements": Source(
        "partners/plannedengagement",
        "Assurance planned per partner: spot checks per quarter, scheduled audit, special audit, follow-up.",
        partner=("", "vendor_number"),
        title=("partner_name", "type"),
    ),
    "psea_answers": Source(
        "psea/answers",
        "Answers of PSEA assessments per indicator (subject, rating, comments).",
        partner=("", "assessment_vendor_number"),
        date="assessment_date",
        title=("assessment_partner_name", "indicator_subject"),
    ),
    "fam_indicators": Source(
        "fam-indicators",
        "Financial assurance module status per month: spot checks, audits, special audits and "
        "micro-assessments contacted, reported, finalised and cancelled.",
        date="month",
        title=("month",),
    ),
    # ---- field monitoring
    "fm_questions": Source(
        "fm-questions",
        "Field monitoring answers: each question asked during a monitoring activity, its answer, summary, "
        "overall finding, entity monitored, method, location and site.",
        partner=("", "vendor_number"),
        date="monitoring_activity_end_date",
        title=("monitoring_activity", "title"),
    ),
    "fm_options": Source(
        "fm-options",
        "Answer options of the field monitoring questions.",
        title=("question", "label"),
    ),
    "fm_programme_activities": Source(
        "fm-programme-activities",
        "Programme activities and CP outputs covered by each field monitoring activity.",
        date="monitoring_activity_end_date",
        title=("monitoring_activity", "programme_activity"),
    ),
    # ---- travel
    "staff_trips": Source(
        "travels",
        "UNICEF staff trips: purpose, dates, status, office, section, mode of travel, international or "
        "not, costs, report notes.",
        date="start_date",
        title=("reference_number", "purpose"),
    ),
    # ---- partner reporting (Partner Reporting Portal)
    "prp_indicator_reports": Source(
        "prp/indicator-report-v2",
        "Partner progress on each PD indicator per reporting period (PRP): baseline, target, progress, "
        "cumulative progress, output status, narrative assessment, challenges, way forward, funds "
        "received to date, report status.",
        scope="business_area",
        filter_key="business_area",
        partner=("", "partner"),
        intervention=("", "pd_reference_number"),
        date="time_period_end",
        title=("pd_reference_number", "performance_indicator"),
    ),
    "prp_programme_documents": Source(
        "sources/prp/unicefprogrammedocument",
        "Programme documents as held in PRP: budget, UNICEF cash, partner contribution, funds received to "
        "date and percentage, reporting frequency.",
        scope="business_area",
        filter_key="external_business_area_code",
        intervention=("external_id", "reference_number"),
        date="start_date",
        title=("reference_number", "title"),
    ),
    "prp_progress_reports": Source(
        "sources/prp/unicefprogressreport",
        "Partner progress reports (PRP): narrative, challenges, way forward, review status and feedback, "
        "and the partner's satisfaction with UNICEF (timely cash and supplies, FACE forms, requests, joint "
        "monitoring, follow-up of findings, overall satisfaction and comment).",
        scope="business_area",
        filter_key="business_area_code",
        date="end_date",
        title=("report_type", "report_number"),
    ),
    # ---- reference data
    "locations": Source(
        "locations",
        "Administrative locations: name, P-code, admin level, parent, coordinates "
        "(written to the locations table, the gazetteer every eTools record links to).",
        scope="written_by",
        title=("name", "p_code"),
    ),
    "location_sites": Source(
        "location-sites",
        "Monitoring sites: name, P-code, coordinates, parent location (written to datamart.MonitoringSite).",
        scope="written_by",
        title=("name",),
    ),
    "offices": Source("office", "UNICEF field offices.", title=("name",)),
    "sections": Source("reports/sections", "UNICEF programme sections.", title=("name",)),
    "etools_usage": Source(
        "user-stats",
        "eTools users and logins per month in the country office.",
        date="month",
        title=("month",),
    ),
    "workspaces": Source(
        "workspaces",
        "The country office workspace: business area code, currency, VISION synchronisation date.",
        scope="lookup",
        keep="name",
        title=("name", "business_area_code"),
    ),
    "datamart_etl_status": Source(
        "system/monitor",
        "When each Datamart table was last refreshed from eTools and PRP (last run, success, failure, "
        "changes).",
        scope="lookup",
        date="last_success",
        title=("table_name", "status"),
    ),
}

# Datasets in their own tables (datamart.models): description for the assistant, model name, main date.
TYPED: dict[str, tuple[str, str, str]] = {
    "funds_reservations": (
        "FundsReservation",
        "Funds reservation lines: donor, grant, fund, WBS, amount per line, FR totals, PD.",
        "start_date",
    ),
    "funds_reservation_headers": (
        "FundsReservationHeader",
        "Funds reservations: reserved, disbursed (actual) and outstanding amounts per FR and PD.",
        "start_date",
    ),
    "grants": ("Grant", "Grants: donor, expiry date, description.", "expiry"),
    "pd_indicators": (
        "PDIndicator",
        "PD indicators by location and disaggregation: baseline, target, unit, section, output, cluster.",
        "",
    ),
    "pd_activities": ("PDActivity", "PD workplan activities with UNICEF and partner cash.", ""),
    "planned_visits": ("PlannedVisits", "Programmatic visits planned per PD, year and quarter.", ""),
    "assessments": (
        "PartnerAssessment",
        "HACT assessments of partners: type, rating, dates.",
        "completed_date",
    ),
    "psea_assessments": (
        "PSEAAssessment",
        "PSEA assessments of partners: overall rating, date, status.",
        "assessment_date",
    ),
    "engagements": (
        "AuditEngagement",
        "Assurance engagements (audit, special audit, spot check, micro-assessment): partner, status, "
        "auditor, dates, value, amounts tested, financial findings, opinion, risk rating, findings counts.",
        "start_date",
    ),
    "audit_findings": (
        "AuditFinding",
        "Financial findings of engagements: title, amount, description, recommendation, partner comments.",
        "created",
    ),
    "action_points": (
        "ActionPoint",
        "Action points from audits, TPM, FM and trips: description, status, priority, due date, assignee, "
        "office, section, category, PD and partner.",
        "due_date",
    ),
    "hact": ("HACTAggregate", "Country HACT totals per year.", ""),
    "hact_history": (
        "PartnerHACTYear",
        "Partner HACT per year: cash transfers, liquidations, risk rating, programmatic visits, spot "
        "checks and audits planned, required (minimum requirements) and completed, outstanding findings.",
        "",
    ),
    "tpm_visits": ("TPMVisit", "Third-party monitoring visits: TPM partner, status, dates.", "start_date"),
    "tpm_activities": (
        "TPMActivity",
        "Activities of TPM visits: PD, partner, section, locations, date, status, programmatic visit or not.",
        "date",
    ),
    "field_monitoring": (
        "MonitoringFinding",
        "Field monitoring overall findings (on/off track) per entity and monitoring activity, narrative.",
        "end_date",
    ),
    "staff_visits": (
        "ProgrammaticVisit",
        "Activities of UNICEF staff trips (programmatic visit, spot check, meeting...) with partner, PD, "
        "location.",
        "date",
    ),
    "partner_reports": (
        "ReportedIndicator",
        "PRP data reports: indicator achievement per progress report and location, report status, dates.",
        "period_end",
    ),
}

EXCLUDED: dict[str, str] = {
    "datamart/users": "personal data (UNICEF and partner staff accounts)",
    "datamart/partners/contacts": "personal data (partner staff contact details); no country filter",
    "sources/prp/unicefperson": "personal data",
    "sources/prp/unicefprogrammedocumentpartnerfocalpoint": "personal data (focal point links)",
    "sources/prp/unicefprogrammedocumentuniceffocalpoint": "personal data (focal point links)",
    "sources/prp/unicefprogrammedocumentunicefofficers": "personal data (officer links)",
    "rapidpro/*": "RapidPro messaging (contacts are personal data); no country filter",
    "datamart/reports/outcomes, outputs, activities": "global VISION programme structure; no country filter",
    "sources/prp/* (the other source tables)": "raw PRP tables of every country; no country filter",
    "prp/indicator-report": "superseded by prp/indicator-report-v2 (read)",
    "prp/datareport": "read into its own table (partner_reports)",
    "datamart/audit/financial-findings": "audits only; audit/financial-findings-all (read) covers every type",
    "datamart/audit/engagement-details": "summary of audit/engagements (read) and its detail datasets",
    "datamart/partners_hact_active": "the partners (read) filtered to active HACT partners",
}


PERSONAL_KEYS = ("email", "phone", "mobile")


def scrub(value):
    """A record without contact details (keys naming an e-mail, phone or mobile) or NUL characters."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if not any(p in str(k).lower() for p in PERSONAL_KEYS)}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return value.replace("\x00", "")
    return value


def dataset_names() -> list[str]:
    return sorted({*DOCUMENTS, *TYPED})
