"""Every eTools Datamart endpoint: what it holds, how it is limited to the country office, how it
links to NeuroDB's programme documents and partners, and whether NeuroDB reads it.

Three kinds of datasets:

* ``TYPED``: records kept in their own table (``datamart.models``), which the pages use;
* ``DOCUMENTS``: records kept whole in ``DatamartDocument``, for the AI assistant (and admin);
* ``EXCLUDED``: endpoints NeuroDB does not read, with the reason.

The AI assistant's eTools tools (``neurodb.assistant.tools``) query any of the first two by name.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Any


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
    # A record without a ``source_id`` key takes its ``id`` as its source id: only where every reader of the
    # source id checks the match (the programme document and indicator it names), as for PRP's indicator
    # reports, whose ids are not confirmed to be the partner reports' ``indicator_report``.
    id_is_source: bool = False
    # Keys a page, figure or check reads besides those above: each run counts how many records carry them
    # (``details.keys_seen``, business-area datasets), so a key the Datamart does not send shows at once.
    reads: tuple[str, ...] = ()


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
        "not, costs.",
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
        id_is_source=True,
        reads=(
            "progress_value",
            "financial_contribution_to_date",
            "report_type",
            "report_number",
            "report_status",
            "pr_status",
            "pr_end_date",
            "pr_due_date",
            "due_date",
            "pr_submission_date",
            "submission_date",
        ),
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

# ---- diagnostic datasets: the Datamart's own monitoring, read by no page, figure, check or other sync (only
# Ask NeuroDB's generic eTools tools and the read-only lists). One that fails is shown where each dataset is
# shown (Data health's datasets table, the run list and its run page), never raised as an alarm (Needs
# attention and NeuroDB Watch: neurodb.web.health; the daily review's sync failures: review.checks).
# workspaces stays out: its endpoint is also the live business-area lookup of the PRP datasets.
DIAGNOSTIC = frozenset({"datamart_etl_status", "etools_usage"})


@dataclass(frozen=True)
class AreaFilters:
    """The business area filters a dataset is read with once the Datamart proves it applies one
    (``datamart_sync.prove_area_filter``): the key its records name their business area code by, and the
    filter keys tried, in turn, on one page each."""

    named_by: str
    keys: tuple[str, ...]


# ---- the datasets whose business area filter is proven every night before it is used: the Datamart ignores
# country_name on prp/datareport (every country's rows are read) and business_area_code on PRP's progress
# reports. prp_indicator_reports keeps its proven business_area, prp_programme_documents today's request.
AREA_FILTERS: dict[str, AreaFilters] = {
    "partner_reports": AreaFilters("business_area", ("business_area", "business_area_code")),
    "prp_progress_reports": AreaFilters("business_area_code", ("business_area_code", "business_area")),
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
    "sources/prp/indicatordisaggregation, indicatordisaggregationvalue": (
        "PRP tables of every country, no country filter: only the labels of the ids the partner reports "
        "name are read, one by one, by the partner_reports sync (datamart.DisaggregationLabel)"
    ),
    "sources/prp/* (the other source tables)": "raw PRP tables of every country; no country filter",
    "prp/indicator-report": "superseded by prp/indicator-report-v2 (read)",
    "prp/datareport": "read into its own table (partner_reports)",
    "datamart/audit/financial-findings": "audits only; audit/financial-findings-all (read) covers every type",
    "datamart/audit/engagement-details": "summary of audit/engagements (read) and its detail datasets",
    "datamart/partners_hact_active": "the partners (read) filtered to active HACT partners",
}


PERSONAL_KEYS = ("email", "phone", "mobile")

# ---- field monitoring (FM): what Ask NeuroDB may read of these datasets (``query.public``)
# Their records name the visit lead and team and hold long free texts (narratives, answers,
# summaries). Ask's eTools tools drop the keys that hold a person, withhold texts longer than
# FM_TEXT_MAX and redact the rest; Monitoring insights reads them in full for staff.
FM_PRIVATE = frozenset({"field_monitoring", "fm_questions", "fm_options", "fm_programme_activities"})
PERSON_TOKENS = frozenset(
    {"team", "member", "members", "monitor", "monitors", "staff", "user", "users", "person", "persons"}
    | {"focal", "focalpoint", "focalpoints", "assigned", "assignee", "assignees", "author", "authors"}
    | {"traveler", "travelers", "traveller", "travellers", "reviewer", "reviewers", "owner", "owners"}
    | {"email", "phone", "mobile", "teamlead", "principal", "respondent", "respondents"}
    # the roles a record names a person by ("supervisor_name", "authorized_officers", "prc_officers",
    # "overall_approver", "unicef_signatory", "unicef_representative")
    | {"supervisor", "supervisors", "officer", "officers", "signatory", "signatories", "signer", "signers"}
    | {"approver", "approvers", "manager", "managers", "coordinator", "coordinators", "assessor"}
    | {"assessors", "requester", "requesters", "submitter", "submitters", "verifier", "verifiers"}
    | {"creator", "creators", "contact", "contacts", "chief", "chiefs", "representative"}
    | {"representatives", "director", "directors", "username", "surname", "firstname", "lastname"}
    | {"fullname"}
)
# "lead" and "head" name a person ("visit_lead", "section_leads", "head_of_unit"), except before a thing:
# "lead_office", "lead_section" (a partner's lead office and section), "head_office"
LED_WORDS = frozenset({"lead", "leads", "head", "heads"})
NOT_LED = frozenset(
    {"office", "offices", "section", "sections", "organization", "organisation", "agency", "partner"}
    | {"ministry", "unit", "cluster", "country", "sector", "donor", "government", "entity", "quarters"}
)
# "by" names a person when nothing but a name part follows it ("reviewed_by", "reviewed_by_name",
# "createdByName", "led_by"); "visits_by_status" is a count by status, and a party is not a person
# ("provided_by", "funded_by": UNICEF or the partner)
BY_TAIL = frozenset({"name", "names", "email", "user", "username", "id", "full", "first", "last", "display"})
BY_PARTY = frozenset(
    {"provided", "funded", "supported", "implemented", "covered", "paid", "financed", "required"}
    | {"grouped", "sorted", "disaggregated", "broken", "split"}
)
# A key holds a number, a date or a kind, not a person, when it holds an amount or a measure
# ("persons_reached", "team_members_count", "staff_cost", "approved_cost_traveler", "number_of_persons",
# "unicef_cash", "outstanding_amt"; not when a person who handles it follows: "budget_owner_name",
# "funds_manager"), names a date ("signed_by_unicef_date", "date_of_comments_by_ip", "submitted_at") or
# ends with a kind ("user_type", "beneficiary_category"), unless it names a way to reach someone
# ("phone_number")
MEASURE_WORDS = frozenset(
    {"count", "counts", "number", "num", "total", "totals", "cost", "costs", "amount", "amounts", "size"}
    | {"budget", "percentage", "percent", "pct", "rate", "ratio", "share", "reached", "target", "targets"}
    | {"cash", "contribution", "contributions", "amt", "expenditure", "expenditures", "value", "values"}
    | {"funds", "disbursed", "reserved", "liquidation", "liquidations", "paid", "price", "prices", "ct"}
    | {"achieved", "spent", "balance", "outstanding", "sum"}
)
# Those for whom an amount is: "approved_cost_traveler" is a cost
FOR_WHOM = frozenset({"traveler", "travelers", "traveller", "travellers", "person", "persons", "staff"})
DATE_WORDS = frozenset({"date", "dates", "datetime"})  # anywhere in the key
LAST_DATE_WORDS = frozenset({"time", "at", "on", "year", "month", "day", "deadline"})  # its last word
KIND_WORDS = frozenset({"type", "types", "category", "categories", "kind", "kinds", "status", "level"})
CONTACT_WORDS = frozenset(
    {"email", "emails", "phone", "phones", "mobile", "fax", "tel", "username", "contact", "whatsapp"}
    | {"viber", "telephone", "telephones", "landline", "hotline", "hotlines", "cell", "cellphone"}
)
# Texts people wrote, not their names: a key that holds one is never sent, and its words are never
# taken for a name ("comments", "remarks", "report_note")
WRITTEN_BY_PEOPLE = frozenset(
    {"note", "notes", "comment", "comments", "remark", "remarks", "description", "descriptions"}
)
# Split names: "unicef_signatory_first_name", "assessor_last_name"
NAME_PARTS = frozenset({"first", "last", "middle", "full", "given", "family"})
FM_TEXT_MAX = 80  # longer strings in FM_PRIVATE datasets are withheld

# ---- safeguarding: the answers of PSEA assessments. Their comments can describe a case of sexual
# exploitation or abuse, which must never reach the AI: Ask NeuroDB reads only these keys of a record
# (the standard, its rating, the partner, the date); every other value is left out, and the records are
# never searched by text nor queried on another key (``query.public``, ``query._refuse_personal``).
SAFEGUARDING = frozenset({"psea_answers"})
SAFEGUARDING_KEYS = frozenset(
    {
        "id",
        "indicator_subject",
        "rating",
        "assessment_partner_name",
        "assessment_vendor_number",
        "assessment_date",
    }
)
FM_TEXT_WITHHELD = "(text withheld: read it in Monitoring insights)"
# camelCase and acronyms: "createdByName", "UNICEFFocalPoints" (unicef_focal_points), "TPMVisit"
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NOT_LETTERS = re.compile(r"[^a-z]+")


def _key_words(part: str) -> list[str]:
    return [word for word in _NOT_LETTERS.split(part) if word]


def _measure(words: list[str]) -> bool:
    """Words that hold an amount or a measure (``MEASURE_WORDS``), unless a person who handles it
    follows the last measure word ("budget_owner_name", "funds_manager", "cost_approved_by"); a person
    it is for does not count ("approved_cost_traveler", "cost_per_person")."""
    at = [i for i, word in enumerate(words) if word in MEASURE_WORDS]
    if not at:
        return False
    tail = words[at[-1] + 1 :]
    if "per" in tail:
        tail = tail[: tail.index("per")]
    if tail[-1:] and tail[-1] in FOR_WHOM:
        tail = tail[:-1]
    return not any(
        word in PERSON_TOKENS or word in LED_WORDS or word in NAME_PARTS or word in ("by", "name", "names")
        for word in tail
    )


def _not_a_person(words: list[str]) -> bool:
    """A key that holds a number, a date, a kind or a party (see ``MEASURE_WORDS``), not a person."""
    found = set(words)
    if found & CONTACT_WORDS or not words:
        return False
    if found & DATE_WORDS or words[-1] in LAST_DATE_WORDS | KIND_WORDS or words[:2] == ["number", "of"]:
        return True
    if _measure(words):
        return True
    return any(word == "by" and i and words[i - 1] in BY_PARTY for i, word in enumerate(words))


def key_words(key: Any) -> list[str]:
    """The words of a key's last part ("a.b": "b"), split on anything that is not a letter, camelCase and
    acronyms too: "UNICEFFocalPoints" gives unicef, focal, points."""
    return _key_words(_CAMEL.sub("_", str(key or "").rsplit(".", 1)[-1]).lower())


def holds_amount(key: Any) -> bool:
    """A key that holds an amount or a measure (:func:`_measure`) and does not name a way to reach
    someone: a number written there is never taken for a phone number."""
    words = key_words(key)
    return _measure(words) and not set(words) & CONTACT_WORDS


def written_by_people(key: Any) -> bool:
    """A key that holds a text people wrote (``WRITTEN_BY_PEOPLE``: "comments", "site_visit_notes",
    "report_note"), not a name: its words are never learnt as names."""
    words = key_words(key)
    return bool(words) and words[-1] in WRITTEN_BY_PEOPLE


def _part_holds_person(part: str, suffixes: tuple[str, ...]) -> bool:
    words = _key_words(part)
    if not words:
        return False
    if set(words) & CONTACT_WORDS:
        return True
    if _not_a_person(words):
        return False
    if part.endswith(suffixes):
        return True
    if any(a in NAME_PARTS and b in ("name", "names") for a, b in zip(words, words[1:], strict=False)):
        return True
    for i, word in enumerate(words):
        if word == "by" and all(w in BY_TAIL for w in words[i + 1 :]):
            return True
        if word in LED_WORDS and (i + 1 == len(words) or words[i + 1] not in NOT_LED):
            return True
        if word in PERSON_TOKENS:
            return True
    return False


@functools.lru_cache(maxsize=4096)
def person_key(key: str) -> bool:
    """Whether a record key holds a person: one of its words (split on anything that is not a letter,
    "a.b" and camelCase too) is in PERSON_TOKENS, "lead" or "head" not before a thing (``NOT_LED``),
    "by" followed by nothing but a name part, a part of a name ("signatory_first_name"), or it ends
    like the fields NeuroDB Watch never sends (``_by``, ``_lead``, ``_email``, ``_owner``...). Whole
    words only: "visit_lead" and "team_members" hold a person, "monitoring_activity" does not. A key that
    holds a number, a date, a kind or a party (:func:`_not_a_person`) does not, unless it names a contact
    ("phone_number")."""
    from neurodb.watch.redact import DROPPED_SUFFIXES  # lazy: watch reads the datamart, not the reverse

    text = _CAMEL.sub("_", str(key or "")).lower()
    return any(_part_holds_person(part, DROPPED_SUFFIXES) for part in text.split("."))


@functools.lru_cache(maxsize=4096)
def holds_person(key: str) -> bool:
    """The one rule every reader uses (Ask NeuroDB's eTools tools, Monitoring insights): :func:`person_key`,
    or a part that is a field NeuroDB Watch never sends because it names or was written by a person
    (``redact._person_field``: "first_name", "contacts", "comments"...), unless that part holds a number,
    a date, a kind or a party."""
    from neurodb.watch import redact  # lazy: watch reads the datamart, not the reverse

    text = str(key or "")
    if person_key(text):
        return True
    for part in text.split("."):
        words = _key_words(_CAMEL.sub("_", part).lower())
        if part and redact._person_field(part) and not _not_a_person(words):
            return True
    return False


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
