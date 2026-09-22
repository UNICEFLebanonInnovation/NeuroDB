"""Builders for ActivityInfo LONG/TEXT extracts shaped like pivoting/AIReports/202416_ai_data.txt."""

from __future__ import annotations

SEP = "\x1f"

# Real header of the 2024 partner-reporting extract (202416_ai_data.txt).
HEADER_2024 = [
    "Database", "DatabaseId", "Folder", "Form", "FormId", "ParentForm", "ParentFormId", "RecordId",
    "Record last edited time", "cadastral_area.cas_code", "cadastral_area.code", "cadastral_area.name",
    "caza.code", "caza.name", "comment", "date_of_reporting", "emergency_reporting", "emergency_tag",
    "funded_by.full_name", "funded_by.funded_by", "governorate.code", "governorate.name", "location_details",
    "month", "national_level_reporting", "partner.name", "partner.partner_full_name", "partner.type",
    "projects.end_date", "projects.please_select_related_project", "projects.project_code",
    "projects.project_name", "projects.select_plan", "projects.start_date", "targeted_population",
    "Quantity Field ID", "Quantity Field Code", "Quantity Field", "Value",
]  # fmt: skip

# Columns added in the 2025 extract (202518_ai_data.txt): month is empty, month_of_reporting is set.
HEADER_2025 = HEADER_2024[:-4] + [
    "indicator_id", "indicator_name", "month_of_reporting", "tag",
    "Quantity Field ID", "Quantity Field Code", "Quantity Field", "Value",
]  # fmt: skip

BASE_ROW = {
    "Database": "0. 2025 Sectors Reporting ",
    "DatabaseId": "ck2yrizmo2",
    "Folder": "16- Child Protection",
    "Form": "Monthly Reporting",
    "FormId": "ckuhdt9m3h4504o8",
    "ParentForm": "PARTNERS Reporting Cadastral Level",
    "ParentFormId": "crwpyfxm3h443lk2",
    "RecordId": "cyamqm3m3n4w7ds1e",
    "Record last edited time": "2024-11-18T14:48:10Z",
    "cadastral_area.cas_code": "10110",
    "cadastral_area.code": "10012",
    "cadastral_area.name": "Aain el-Mraisse foncière",
    "caza.code": "LBN11",
    "caza.name": "Beirut",
    "emergency_tag": "Cross-Border Emergency/Escalation/Conflict",
    "funded_by.funded_by": "UNICEF",
    "governorate.code": "7",
    "governorate.name": "Beyrouth",
    "month": "2024-10",
    "partner.name": "Partner-été",
    "partner.partner_full_name": "Partner Full Name",
    "partner.type": "NNGO",
    "projects.please_select_related_project": "Non specific project",
    "projects.project_code": "LEBA/PCA2024668/SPD20241420-1",
    "projects.project_name": "Communication with communities",
    "projects.select_plan": "LRP",
    "Quantity Field ID": "c1652hlm3h4gniwr",
    "Quantity Field": "7.2.4.e_SYR_Female: # of individuals reached with rapid response items (NFIs)",
    "Value": "18",
}


def make_row(**overrides: str) -> dict[str, str]:
    row = dict(BASE_ROW)
    row.update(overrides)
    return row


def make_extract(rows: list[dict[str, str]], header: list[str] = HEADER_2024) -> bytes:
    lines = [SEP.join(header)]
    for row in rows:
        lines.append(SEP.join(row.get(column, "") for column in header))
    return ("\n".join(lines) + "\n").encode("utf-8")
