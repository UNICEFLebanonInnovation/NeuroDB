"""The cross-page verification of the eTools reporting pages: each test pins one disagreement the
verification found between two pages (or a safety limit), so the same filter gives the same numbers
everywhere and no request can take a web worker down."""

from __future__ import annotations

import csv
import datetime
import io
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from neurodb.assistant import tools
from neurodb.datamart import checks, headline, monitoring, reached, reporting, services
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.reports import overview

pytestmark = pytest.mark.django_db
# the suite's day (tests/conftest.py SITE_NOW), not the real clock: this is read at import, before it travels
TODAY = datetime.date(2026, 10, 15)
D = datetime.date
AK, BK = ("Akkar", "LB11"), ("Bekaa", "LB12")
_ids = iter(range(50_000, 10**9))


def indicator(pd, source_id, title, locations, *, target="1000", display="number", hf=False, cp_output=""):
    for name, pcode in locations:
        dm.PDIndicator.objects.create(
            datamart_id=next(_ids),
            source_id=source_id,
            intervention=pd,
            pd_reference_number=pd.number,
            title=title,
            section_name="Education",
            lower_result_name="1.1 Output",
            target_numerator=Decimal(target),
            target_denominator=Decimal("100") if display == "percentage" else None,
            display_type=display,
            unit="percentage" if display == "percentage" else "number",
            location_name=name,
            location_pcode=pcode,
            is_high_frequency=hf,
            tag_age_group="Children",
            cp_output=cp_output,
        )


def report(pd, etools_id, title, pr, number, start, end, status, values, *, ir=None, kind="QPR", **extra):
    """One indicator report: a row per location, each location's value in the "()" total."""
    for (name, pcode), value in values.items():
        parts = value if isinstance(value, dict) else {"v": value, "d": 1, "c": value}
        dm.ReportedIndicator.objects.create(
            datamart_id=next(_ids),
            intervention=pd,
            partner=pd.partner,
            partner_name=pd.partner.name,
            pd_reference_number=pd.number,
            progress_report=pr,
            indicator_report=ir if ir is not None else f"{pr}-{etools_id}",
            report_number=number,
            report_type=kind,
            report_status=status,
            period_start=start,
            period_end=end,
            due_date=end + datetime.timedelta(days=30),
            submission_date=end + datetime.timedelta(days=10)
            if status in ("Accepted", "Submitted")
            else None,
            indicator=title,
            etools_indicator_id=str(etools_id),
            location=name,
            p_code=pcode,
            section="Education",
            achievement_in_period=str(parts.get("c", "")),
            disaggregation={"()": parts},
            calculation_across_locations="sum",
            calculation_across_periods=extra.pop("across_periods", "sum"),
            **extra,
        )


@pytest.fixture
def pd(db):
    partner = PartnerOrganization.objects.create(
        etl_id="93", name="Gamma Org", partner_type="CSO", vendor_number="2500000003"
    )
    return PCA.objects.create(
        etl_id="903",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PCA-C",
        title="C",
        status="active",
        start=D(2026, 1, 1),
        end=D(2026, 12, 31),
        document_type="PD",
    )


def grid(pd, **extra) -> list[monitoring.Indicator]:
    return monitoring.indicators(monitoring.Filters(pd_ids=[pd.id], scope="all", **extra), TODAY)


# ------------------------------------------------------------------- indicator reports stay apart
def test_two_indicators_sharing_an_etools_id_stay_apart_on_every_page(pd):
    """The rows of two PD indicators carry the same eTools id (a CP output indicator id, not the PD
    indicator's own), each with its own indicator report: the Report/Location tab, the pop-up and the
    tiles keep them apart, as the grid does."""
    indicator(pd, 701, "# of girls enrolled", [AK])
    indicator(pd, 702, "# of boys enrolled", [AK])
    q1 = (D(2026, 1, 1), D(2026, 3, 31))
    report(pd, 9001, "# of girls enrolled", "C-1", "QPR1", *q1, "Accepted", {AK: 30}, ir="IR-1")
    report(pd, 9001, "# of boys enrolled", "C-1", "QPR1", *q1, "Accepted", {AK: 20}, ir="IR-2")
    assert {r.title: r.reached for r in grid(pd)} == {"# of girls enrolled": 30.0, "# of boys enrolled": 20.0}
    data = services.partner_reporting({}, TODAY)
    tab = reporting.location_rows(data["rows"], data["statuses"], TODAY)["rows"]
    assert sorted((r["indicator"], r["value_in_period"]) for r in tab) == [
        ("# of boys enrolled", 20.0),
        ("# of girls enrolled", 30.0),
    ]
    popup = services.report_detail("C-1", TODAY)["indicators"]
    assert sorted((i["indicator"], i["total"]) for i in popup) == [
        ("# of boys enrolled", 20.0),
        ("# of girls enrolled", 30.0),
    ]
    assert data["counts"]["indicator_reports"] == 2


def test_two_indicators_with_one_title_are_never_one_popup_line(pd):
    """Two indicators share a title and their rows carry no eTools id (each its own indicator report):
    the pop-up lists two lines, never their sum."""
    title = "# of children reached"
    indicator(pd, 711, title, [AK])
    indicator(pd, 712, title, [BK])
    q1 = (D(2026, 1, 1), D(2026, 3, 31))
    report(pd, "", title, "C-1", "QPR1", *q1, "Accepted", {AK: 30}, ir="IR-11")
    report(pd, "", title, "C-1", "QPR1", *q1, "Accepted", {BK: 20}, ir="IR-12")
    popup = services.report_detail("C-1", TODAY)["indicators"]
    assert sorted(i["total"] for i in popup) == [20.0, 30.0]
    assert services.partner_reporting({}, TODAY)["counts"]["indicator_reports"] == len(popup) == 2


def test_the_popup_target_of_a_percentage_is_the_grid_s(pd):
    indicator(pd, 761, "% of children passing", [AK], target="80", display="percentage")
    report(pd, 761, "% of children passing", "C-31", "QPR1", D(2026, 1, 1), D(2026, 3, 31), "Accepted",
           {AK: {"v": 40, "d": 50}}, target="{'v': 80, 'd': 100}")  # fmt: skip
    (row,) = grid(pd)
    (line,) = services.report_detail("C-31", TODAY)["indicators"]
    assert line["target"] == row.target == 80.0
    # an indicator the PD no longer lists: the report's own target, read as a percentage too
    dm.PDIndicator.objects.filter(intervention=pd).delete()
    (line,) = services.report_detail("C-31", TODAY)["indicators"]
    assert line["target"] == 0.8  # no PD definition: a plain number, v over d


# ---------------------------------------------------------------------------- one status per report
def test_a_report_whose_rows_disagree_has_one_status_on_every_page(pd):
    """A partial re-sync: the girls rows of a progress report still say Accepted, the boys rows (synced
    later) Submitted. Every page reads the report as Submitted, the word of its newest rows."""
    indicator(pd, 731, "# of girls enrolled", [AK])
    indicator(pd, 732, "# of boys enrolled", [AK])
    q1 = (D(2026, 1, 1), D(2026, 3, 31))
    report(pd, 731, "# of girls enrolled", "C-9", "QPR1", *q1, "Accepted", {AK: 30})
    report(pd, 732, "# of boys enrolled", "C-9", "QPR1", *q1, "Submitted", {AK: 20})
    rows = {r.title: r for r in grid(pd)}
    assert {t: (r.report_status, r.awaiting) for t, r in rows.items()} == {
        "# of girls enrolled": ("Submitted", 30.0),
        "# of boys enrolled": ("Submitted", 20.0),
    }
    data = services.partner_reporting({}, TODAY)
    # the tiles count progress reports: one Submitted report, of two indicator reports
    assert data["counts"]["submitted"] == 1 and data["counts"]["accepted"] == 0
    assert {s["key"]: s["indicator_reports"] for s in data["counts"]["statuses"]}["submitted"] == 2
    assert [r["status"] for r in data["reports"]] == ["submitted"]
    assert services.report_detail("C-9", TODAY)["status"] == "submitted"
    tab = reporting.location_rows(data["rows"], data["statuses"], TODAY)["rows"]
    assert {r["status"] for r in tab} == {"submitted"}
    # a filter that keeps only the rows still saying Accepted: the status is read from the whole report
    girls = services.partner_reporting({"q": "girls"}, TODAY)
    assert [r["status"] for r in girls["reports"]] == ["submitted"] and girls["counts"]["submitted"] == 1
    # the Datamart's own change date comes first: the rows changed last decide
    changed = datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC)
    dm.ReportedIndicator.objects.filter(indicator="# of girls enrolled").update(last_modify_date=changed)
    assert {r.report_status for r in grid(pd)} == {"Accepted"}
    data = services.partner_reporting({}, TODAY)
    assert (data["counts"]["accepted"], data["counts"]["submitted"]) == (1, 0)
    assert services.report_detail("C-9", TODAY)["status"] == "accepted"


def test_newest_status_reads_each_word_s_latest_rows():
    early, late = datetime.datetime(2026, 1, 1), datetime.datetime(2026, 2, 1)
    assert reached.newest_status({"Accepted": (late, None, 1), "Submitted": (early, late, 9)}) == "Accepted"
    assert reached.newest_status({"Accepted": (None, early, 1), "Submitted": (None, late, 2)}) == "Submitted"
    assert reached.newest_status({"": (late, late, 5), " Sent back ": (None, None, 1)}) == "Sent back"
    assert reached.newest_status({}) == ""


# --------------------------------------------------------------------------------- location filter
def test_the_location_filter_matches_a_place_by_its_p_code(pd):
    """The PD plans "Akkar" (LB11); PRP names the same P-code "Akkar Governorate"."""
    indicator(pd, 751, "# of girls enrolled", [AK, BK])
    report(pd, 751, "# of girls enrolled", "C-21", "QPR1", D(2026, 1, 1), D(2026, 3, 31), "Accepted",
           {("Akkar Governorate", "LB11"): 30, BK: 10})  # fmt: skip
    (full,) = grid(pd)
    (only,) = grid(pd, locations=["Akkar"])
    assert full.reached == 40.0 and only.reached == full.by_location["LB11"]["reached"] == 30.0
    assert set(only.by_location) == {"LB11"}


def test_the_map_under_a_location_filter_keeps_the_indicator_s_reached(pd):
    indicator(pd, 501, "# of adolescents reached", [AK, BK], target="3000")
    q = [(D(2025, 7, 1), D(2025, 9, 30)), (D(2025, 10, 1), D(2025, 12, 31))]
    PCA.objects.filter(pk=pd.pk).update(start=D(2025, 7, 1))
    report(pd, 501, "# of adolescents reached", "C-1", "QPR1", *q[0], "Accepted", {AK: 100, BK: 50})
    report(pd, 501, "# of adolescents reached", "C-2", "QPR2", *q[1], "Accepted", {AK: 200, BK: 150})
    places = monitoring.map_points(
        monitoring.Filters(pd_ids=[pd.id], scope="all", locations=["Akkar"]), TODAY
    )
    rows = [(p["p_code"], r) for p in places["points"] + places["unlocated"] for r in p["rows"]]
    assert [p for p, _r in rows] == ["LB11"]  # the chosen place only
    ((_p, row),) = rows
    assert (row["reached_here"], row["reached"]) == (300.0, 500.0)  # its own, and every location's
    assert row["achieved_percent"] == round(500 / 3000 * 100, 1)


# ------------------------------------------------------------------------------- headline indicator
def test_the_pd_page_names_the_overview_s_main_indicator_for_an_average(pd):
    """An average-over-periods children indicator far above its target in this year only: the PD page
    sets it aside as the overview does (it reads the year's figure, not only Reached to date)."""
    year = TODAY.year
    PCA.objects.filter(pk=pd.pk).update(start=D(year - 1, 1, 1), end=D(year, 12, 31))
    pd.refresh_from_db()
    indicator(pd, 721, "# of children in clubs (average)", [AK], target="100")
    indicator(pd, 722, "# of children reached with PSS", [AK], target="1000")
    reports = [
        (D(year - 1, 1, 1), D(year - 1, 3, 31), 10),
        (D(year - 1, 4, 1), D(year - 1, 6, 30), 10),
        (D(year - 1, 7, 1), D(year - 1, 9, 30), 10),
        (D(year, 1, 1), D(year, 3, 31), 3000),
    ]
    for n, (start, end, value) in enumerate(reports):
        report(pd, 721, "# of children in clubs (average)", f"C-{n}", f"QPR{n}", start, end, "Accepted",
               {AK: value}, across_periods="avg")  # fmt: skip
    report(pd, 722, "# of children reached with PSS", "C-3", "QPR3", D(year, 1, 1), D(year, 3, 31), "Accepted",
           {AK: 500})  # fmt: skip
    builder = overview._Builder(overview.Scope(year=year, today=TODAY))
    builder.rows = builder._rows()
    builder._choose_mains()
    page = services.programme_datamart(pd, today=TODAY)
    assert builder.mains[pd.id].row.key == page["monitoring"]["main"].row.key == "722"
    row = next(r for r in page["monitoring"]["rows"] if r.key == "721")
    assert headline.largest_value(row, year) == 3000.0 and headline.largest_value(row) < 3000


# ------------------------------------------------------------------------------------ Ask NeuroDB
def test_ask_programme_details_names_the_six_statuses(pd):
    indicator(pd, 741, "# of girls enrolled", [AK])
    report(pd, 741, "# of girls enrolled", "C-1", "QPR1", D(2026, 1, 1), D(2026, 3, 31), "Accepted", {AK: 5})
    end = TODAY + datetime.timedelta(days=60)
    report(
        pd, 741, "# of girls enrolled", "C-2", "QPR2", end - datetime.timedelta(days=89), end, "Due", {AK: 0}
    )
    out = tools.run("programme_details", {"number": pd.number})
    page = services.programme_datamart(pd)
    assert (
        {r["report"]: r["status"] for r in out["progress_reports"]}
        == {r["report_number"]: r["status_label"] for r in page["reports"]}
        == {"QPR1": "Accepted", "QPR2": "Not yet due"}
    )


# ------------------------------------------------------------------------------------- partner page
def test_the_partner_page_counts_the_reports_its_link_lists(pd, client_viewer):
    partner = pd.partner
    indicator(pd, 741, "# of girls enrolled", [AK])
    report(
        pd, 741, "# of girls enrolled", "C-11", "QPR1", D(2026, 1, 1), D(2026, 3, 31), "Accepted", {AK: 30}
    )
    report(pd, 741, "# of girls enrolled", "C-12", "QPR2", D(2026, 4, 1), D(2026, 6, 30), "Accepted", {AK: 5})
    # a report synced before its partner link was set: the vendor number only
    dm.ReportedIndicator.objects.filter(progress_report="C-12").update(partner=None)
    # a cancelled PD's report: never on a reporting page
    cancelled = PCA.objects.create(etl_id="904", partner=partner, partner_name=partner.name, number="LEB/X",
                                   title="X", status="cancelled")  # fmt: skip
    report(cancelled, 9, "# of x", "X-1", "QPR1", D(2026, 1, 1), D(2026, 3, 31), "Accepted", {AK: 1})
    page = client_viewer.get(reverse("reports:partner_profile", args=[partner.id]))
    counted = page.context["datamart"]["reporting"]["reports"]
    assert counted == 1
    link = f"{reverse('reports:partner_reporting')}?partner={partner.id}&amp;pd_status="
    assert link in page.text and "?q=" not in page.text
    listed = client_viewer.get(
        reverse("reports:partner_reporting"), {"partner": str(partner.id), "pd_status": ""}
    )
    assert listed.context["data"]["summary"]["reports"] == counted


# ------------------------------------------------------------------------------- Progress reports
@pytest.fixture
def reporting_pds(pd):
    ended = PCA.objects.create(etl_id="905", partner=pd.partner, partner_name=pd.partner.name,
                               number="LEB/ENDED", title="E", status="ended")  # fmt: skip
    draft = PCA.objects.create(etl_id="906", partner=pd.partner, partner_name=pd.partner.name,
                               number="LEB/DRAFT", title="D", status="draft")  # fmt: skip
    q1 = (D(2026, 1, 1), D(2026, 3, 31))
    report(pd, 1, "# of girls enrolled", "A-1", "QPR1", *q1, "Accepted", {AK: 1, BK: 1}, high_frequency=True)
    report(pd, 2, "# of boys enrolled", "A-1", "QPR1", *q1, "Accepted", {AK: 1})
    report(ended, 3, "# of x", "E-1", "QPR1", *q1, "Accepted", {AK: 1})
    report(draft, 4, "# of y", "D-1", "QPR1", *q1, "Accepted", {AK: 1})
    return {"ended": ended, "draft": draft}


def test_progress_reports_open_on_the_running_pds(reporting_pds, client_viewer):
    url = reverse("reports:partner_reporting")
    first = client_viewer.get(url)
    assert {r["pd_reference_number"] for r in first.context["data"]["reports"]} == {"LEB/PCA-C"}
    assert first.context["f"].status_choice == ["active", "signed", "suspended"]
    every = client_viewer.get(url, {"pd_status": ""}).context["data"]["reports"]
    assert {r["pd_reference_number"] for r in every} == {"LEB/PCA-C", "LEB/ENDED"}  # never a draft PD
    ended = client_viewer.get(url, {"pd_status": "ended"}).context["data"]["reports"]
    assert {r["pd_reference_number"] for r in ended} == {"LEB/ENDED"}
    assert "draft" not in reporting.filter_options()["pd_statuses"]


def test_progress_reports_emergency_filter(reporting_pds, client_viewer):
    """The Emergency indicator keeps the reports of SPDs, as Power BI's (the user's decision of 26 Oct 2026):
    A-1's high-frequency girls rows are not emergency ones."""
    PCA.objects.filter(pk=reporting_pds["ended"].pk).update(document_type="SPD")
    url = reverse("reports:partner_reporting")
    every_status = {"pd_status": ""}

    def counts(**params):
        found = client_viewer.get(url, {**every_status, **params}).context["data"]["counts"]
        return found["progress_reports"], found["indicator_reports"]

    assert (counts(emergency="yes"), counts(emergency="no"), counts()) == ((1, 1), (1, 2), (2, 3))
    for chosen, pd, word in (("yes", "LEB/ENDED", "Yes"), ("no", "LEB/PCA-C", "No")):
        tab = client_viewer.get(url, {**every_status, "tab": "locations", "emergency": chosen})
        assert {(r["programme_document"], r["emergency"]) for r in tab.context["locations"]["rows"]} == {
            (pd, word)
        }


def test_the_month_chart_places_a_report_in_the_month_its_period_ends():
    reports = [
        {"due_date": D(2026, 10, 30), "period_end": D(2026, 9, 30), "status": reached.DUE, "indicators": 21},
        {
            "due_date": D(2027, 1, 30),
            "period_end": D(2026, 12, 31),
            "status": reached.NOT_YET_DUE,
            "indicators": 3,
        },
    ]
    chart = reporting.by_month(reports)
    assert chart["labels"] == ["Sep 2026", "Dec 2026"]
    # progress reports, as Power BI's chart counts them (the user's decision), not their indicator reports
    assert chart["series"] == {"Due": [1, 0], "Not yet due": [0, 1]}


# ---------------------------------------------------------------------------- Program Performance
def test_program_performance_reads_the_reporting_year_chosen(pd, client_viewer):
    PCA.objects.filter(pk=pd.pk).update(start=D(2025, 1, 1))
    indicator(pd, 801, "# of girls enrolled", [AK], target="100")
    indicator(pd, 802, "# of boys enrolled", [AK], target="100")
    report(pd, 801, "# of girls enrolled", "P-1", "QPR1", D(2025, 1, 1), D(2025, 3, 31), "Accepted", {AK: 90})
    report(pd, 801, "# of girls enrolled", "P-2", "QPR5", D(2026, 1, 1), D(2026, 3, 31), "Accepted", {AK: 10})
    report(pd, 802, "# of boys enrolled", "P-2", "QPR5", D(2026, 1, 1), D(2026, 3, 31), "Accepted", {AK: 40})
    url = reverse("reports:pd_monitoring")
    every = client_viewer.get(url, {"view": "table", "scope": "all"})
    assert [r.key for r in every.context["page_obj"]] == ["801", "802"]  # % Reached to date: 100, 40
    year = client_viewer.get(url, {"view": "table", "scope": "all", "year": "2026"})
    assert year.context["sort"] == "-achieved_in_year"
    assert [(r.key, r.achieved_in_year) for r in year.context["page_obj"]] == [("802", 40.0), ("801", 10.0)]
    assert "% Reached in 2026" in year.text and "Reached to date" in year.text
    text = b"".join(client_viewer.get(url, {"view": "table", "scope": "all", "year": "2026", "export": "csv"})
                    .streaming_content).decode("utf-8-sig")  # fmt: skip
    rows = {r["indicator"]: r for r in csv.DictReader(io.StringIO(text))}
    assert rows["# of boys enrolled"]["achieved_percent_in_year"] == "40.0"
    text = b"".join(
        client_viewer.get(url, {"view": "table", "scope": "all", "export": "csv"}).streaming_content
    ).decode("utf-8-sig")
    assert "achieved_percent_in_year" not in text.splitlines()[0]


def test_the_views_keep_the_year_shown_and_the_menu_offers_every_year(pd, client_viewer, reporting_year):
    url = reverse("reports:pd_monitoring")
    grid_page = client_viewer.get(url, {"scope": "all"})
    year = grid_page.context["filters"].year
    for name in ("table", "quality", "map"):
        assert f"year={year}" in grid_page.context["tab_urls"][name]
    table = client_viewer.get(url, {"view": "table", "scope": "all"})
    assert "year=all" in table.context["tab_urls"]["grid"]
    assert table.context["year_all_active"] and ">All years<" in table.text.replace("\n", "")


def test_cp_output_of_an_indicator_not_reported_yet_names_the_pd_s_outputs(pd):
    PCA.objects.filter(pk=pd.pk).update(cp_outputs=["5.2 Learning", "5.3 Protection"])
    indicator(pd, 811, "# of girls enrolled", [AK])
    (row,) = grid(pd)
    assert row.cp_output == "5.2 Learning, 5.3 Protection"
    (flat,) = reporting.pd_indicators(PCA.objects.filter(pk=pd.pk), {})
    assert flat["cp_output"] == row.cp_output


def test_the_funds_page_names_the_prp_contribution_as_power_bi(client_viewer):
    page = client_viewer.get(reverse("reports:funds"), {"tab": "contributions"})
    assert page.status_code == 200 and "Reported in PRP" not in page.text


# --------------------------------------------------------------------------------------- safety
def test_csv_exports_keep_a_formula_a_text(pd, client_viewer):
    indicator(pd, 821, '=HYPERLINK("http://example.invalid","click")', [AK])
    for url in (
        "/programmes/?tab=indicators&export=csv",
        "/partner-monitoring/?view=table&export=csv&scope=all",
    ):
        text = b"".join(client_viewer.get(url).streaming_content).decode("utf-8-sig")
        cells = [c for row in csv.reader(io.StringIO(text)) for c in row if "HYPERLINK" in c]
        assert cells and all(c.startswith("'=") for c in cells), (url, cells)
    from neurodb.reports.exports import csv_cell

    found = [
        csv_cell(v) for v in ("=1+1", "+1+cmd", "-x", "@a", "\tz", "plain", -5, 3.5, "-12.5", "+3", "1,200")
    ]
    assert found == ["'=1+1", "'+1+cmd", "'-x", "'@a", "'\tz", "plain", -5, 3.5, "-12.5", "+3", "1,200"]


def test_the_checks_page_stops_within_its_time(db, monkeypatch):
    """Every check shares one time budget: once used up, the checks left say they did not run."""
    found = checks.run_all(budget=0)
    assert len(found) == len(checks.CHECKS) == 12 and all(c["verdict"].startswith("Not run") for c in found)

    now = [0.0]
    monkeypatch.setattr(checks.time, "monotonic", lambda: now[0])
    timeouts = []

    def statement(cursor, then: float) -> dict:
        cursor.execute("SHOW statement_timeout")
        timeouts.append(cursor.fetchone()[0])
        now[0] = then  # the statement took that long
        return {"tone": checks.OK, "verdict": "Done.", "figures": []}

    def two_statements(cursor) -> dict:
        statement(cursor, 10.5)
        return statement(cursor, 10.5)  # the deadline passed during the first one

    stub = (
        checks.Check("a", "A", "?", "-", lambda c: statement(c, 4.0)),
        checks.Check("b", "B", "?", "-", two_statements),
        checks.Check("c", "C", "?", "-", lambda c: statement(c, 0.0)),
    )
    monkeypatch.setattr(checks, "CHECKS", stub)
    found = checks.run_all(budget=10)
    assert [c["verdict"].split(":")[0] for c in found] == ["Done.", "Not run", "Not run"]
    # a statement stops at the page's deadline when it is nearer than one statement's 20 seconds
    assert timeouts == ["10s", "6s"]


def test_the_contact_scrub_writes_a_batch_in_one_statement(pd):
    for n in range(3):
        dm.ReportedIndicator.objects.create(
            datamart_id=next(_ids), intervention=pd, data={"submitter_email": "x", "keep": n, "phone": "1"}
        )
    from neurodb.datamart import backfill

    with CaptureQueriesContext(connection) as queries:
        for table, column in backfill._record_columns():
            backfill._scrub_column(table, column)
    updates = [q for q in queries.captured_queries if q["sql"].lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 1
    assert sorted(r["keep"] for r in dm.ReportedIndicator.objects.values_list("data", flat=True)) == [0, 1, 2]
    assert not any(
        "phone" in d or "submitter_email" in d
        for d in dm.ReportedIndicator.objects.values_list("data", flat=True)
    )


def test_the_disaggregation_tab_makes_the_lines_of_the_page_shown_only(pd, client_viewer, monkeypatch):
    from neurodb.reports import views

    monkeypatch.setattr(views, "DISAGGREGATION_BLOCKS", 2)
    indicator(pd, 831, "# of girls enrolled", [AK, BK])
    for n in range(3):
        start = D(2025, 1 + 3 * n, 1)
        report(pd, 831, "# of girls enrolled", f"G-{n}", f"QPR{n}", start, start + datetime.timedelta(days=80),
               "Accepted", {AK: 5, BK: 5})  # fmt: skip
    page = client_viewer.get(
        reverse("reports:partner_reporting"), {"tab": "disaggregations", "pd_status": ""}
    )
    blocks = page.context["disaggregations"]["blocks"]
    assert len(blocks) == 6 and sum("table_lines" in b for b in blocks) == 2
    assert reporting.DISAGGREGATION_LIMIT <= 10_000 and reporting.LOCATION_LIMIT <= 20_000
    monkeypatch.setattr(reporting, "DISAGGREGATION_LIMIT", 5)
    too_many = client_viewer.get(
        reverse("reports:partner_reporting"), {"tab": "disaggregations", "pd_status": ""}
    )
    assert "choose a reporting year" in too_many.text
    export = client_viewer.get(
        reverse("reports:partner_reporting"), {"tab": "disaggregations", "pd_status": "", "export": "csv"}
    )
    assert export.status_code == 400
