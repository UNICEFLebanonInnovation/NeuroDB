import datetime as dt

import pytest

from neurodb.integrations.activityinfo import rows
from neurodb.integrations.activityinfo.rows import RowError, parse_row
from tests.integrations.extracts import HEADER_2024, HEADER_2025, make_extract, make_row

pytestmark = pytest.mark.django_db

TODAY = dt.date(2025, 6, 15)


def test_iter_extract_uses_unit_separator_and_header():
    data = make_extract([make_row(), make_row(Value="0")])
    parsed = list(rows.iter_extract(data))
    assert len(parsed) == 2
    assert parsed[0]["Quantity Field ID"] == "c1652hlm3h4gniwr"
    assert parsed[1]["Value"] == "0"
    assert list(parsed[0]) == HEADER_2024


def test_parse_row_maps_v2_fields(database):
    values = parse_row(make_row(), database, today=TODAY)
    assert values is not None
    assert values["dbase"] == database
    assert values["database_ai_id"] == "202516"
    assert values["month"] == "6"
    assert values["month_name"] == "2024-10"
    assert values["ai_folder"] == "16- Child Protection"
    assert values["report_id"] == "ckuhdt9m3h4504o8"
    assert values["indicator_id"] == "c1652hlm3h4gniwr"
    assert values["indicator_awp_code"] == "7.2.4.e"
    assert values["indicator_value"] == 18.0
    assert values["partner_label"] == "Partner_ete"  # dash -> underscore, accents stripped
    assert values["partner_id"] == "Partner_ete"
    assert values["partner_description"] == "Partner Full Name"
    assert values["location_adminlevel_governorate_code"] == "7"
    assert values["location_adminlevel_governorate"] == "Beyrouth"
    assert values["location_adminlevel_caza_code"] == "LBN11"
    assert values["location_adminlevel_cadastral_area_code"] == "10110"
    assert values["project_label"] == "LEBA/PCA2024668/SPD20241420-1"
    assert values["project"] == "Non specific project"
    assert values["project_plan"] == "LRP"
    assert values["parent_form"] == "PARTNERS Reporting Cadastral Level"
    assert values["emergency"] == "Yes"
    assert values["support_covid"] is False
    assert values["last_edited_time"].date() == dt.date(2024, 11, 18)
    assert values["funded_by"] == "UNICEF"


def test_zero_value_rows_are_skipped_not_failed(database):
    assert parse_row(make_row(Value="0"), database, today=TODAY) is None
    assert parse_row(make_row(Value="abc"), database, today=TODAY) is None


def test_non_unicef_funding_skipped_only_for_unicef_databases(database, nutrition_database):
    row = make_row(**{"funded_by.funded_by": "LHF-OCHA"})
    assert parse_row(row, database, today=TODAY)["funded_by"] == "LHF-OCHA"
    assert parse_row(row, nutrition_database, today=TODAY) is None


def test_unicef_partner_forces_funded_by(database):
    row = make_row(**{"partner.name": "UNICEF", "funded_by.funded_by": "Other"})
    assert parse_row(row, database, today=TODAY)["funded_by"] == "UNICEF"


def test_governorate_na_becomes_national(database):
    row = make_row(**{"governorate.code": "NA", "governorate.name": "NA"})
    values = parse_row(row, database, today=TODAY)
    assert values["location_adminlevel_governorate_code"] == "10"
    assert values["location_adminlevel_governorate"] == "National"


def test_emergency_uses_first_present_tag_column(database):
    assert parse_row(make_row(emergency_tag="Regular reporting"), database, today=TODAY)["emergency"] == "No"
    assert parse_row(make_row(emergency_tag="Yes"), database, today=TODAY)["emergency"] == "Yes"
    row = make_row()
    del row["emergency_tag"]
    row["tag"] = "Escalation of hostilities (Flash Appeal)"
    assert parse_row(row, database, today=TODAY)["emergency"] == "Yes"


def test_month_of_reporting_wins_over_empty_month(database):
    row = make_row(month="", month_of_reporting="2025-01")
    assert parse_row(row, database, today=TODAY)["month_name"] == "2025-01"


@pytest.mark.parametrize("month", ["2925-01", "2023-12", "", "NA"])
def test_rows_outside_reporting_year_are_rejected(database, month):
    with pytest.raises(RowError):
        parse_row(make_row(month=month), database, today=TODAY)


def test_adjacent_years_are_accepted(database):
    assert parse_row(make_row(month="2024-12"), database, today=TODAY) is not None
    assert parse_row(make_row(month="2026-01"), database, today=TODAY) is not None


def test_nutrition_database_reads_indicator_columns(nutrition_database):
    row = make_row(
        month="",
        month_of_reporting="2025-01",
        indicator_id="1.1.A",
        indicator_name="# of caregivers of children 0-23 months received skilled IYCF support",
        **{"Quantity Field": "Total Individuals"},
    )
    values = parse_row(row, nutrition_database, today=TODAY)
    assert values["indicator_awp_code"] == "1.1.A"
    assert values["indicator_name"].startswith("# of caregivers")


def test_too_long_values_raise_row_error(database):
    with pytest.raises(RowError):
        parse_row(make_row(**{"partner.partner_full_name": "x" * 300}), database, today=TODAY)


def test_project_fields_are_truncated_to_245(database):
    values = parse_row(make_row(**{"projects.project_name": "y" * 400}), database, today=TODAY)
    assert len(values["project_description"]) == 245


def test_missing_required_column_raises(database):
    row = make_row()
    del row["Quantity Field"]
    with pytest.raises(RowError):
        parse_row(row, database, today=TODAY)


def test_extraction_month_january_reports_december(database):
    assert rows.extraction_month(database, dt.date(2026, 1, 10)) == 12
    assert rows.extraction_month(database, dt.date(2025, 3, 10)) == 3


def test_awp_code():
    assert rows.awp_code("7.2.4.e_SYR_Female: # of") == "7.2.4.e"
    assert rows.awp_code("1.1 # of children") == "1.1"
    assert rows.awp_code(None) == "None"


def test_office_databases_use_reporting_office(database):
    database.have_offices = True
    row = make_row(reporting_office="Beirut FO", month="2025-02", Value="0")
    values = parse_row(row, database, today=TODAY)
    assert values["location_adminlevel_governorate"] == "Beirut FO"
    assert values["partner_label"] == "UNICEF"
    assert values["funded_by"] == "UNICEF"
    assert values["indicator_value"] == 0.0
    assert values["indicator_awp_code"] == ""
    assert "emergency" not in values
    assert parse_row(make_row(reporting_office="X", month="2024-02"), database, today=TODAY) is None
    with pytest.raises(RowError):
        parse_row(make_row(reporting_office="X", month=""), database, today=TODAY)


def test_2025_header_round_trip(nutrition_database):
    data = make_extract(
        [make_row(month="", month_of_reporting="2025-03", indicator_id="2.1", indicator_name="n")],
        header=HEADER_2025,
    )
    (row,) = rows.iter_extract(data)
    assert parse_row(row, nutrition_database, today=TODAY)["month_name"] == "2025-03"
