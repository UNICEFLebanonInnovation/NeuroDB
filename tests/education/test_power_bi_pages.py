"""The pages compared with Power BI's views (SPEC_PBI §4.3-4.7): the line under the key figures (counted from the view,
from Compiler's rule, not compared yet, or of an older count), the percentage charts without "Not specified", Dirasa's
school figures over the schools that have registrations, the card versions an administrator chooses (they show at
once, a version a payload cannot count falls back to the default), Dirasa's children card with both counts and the
country programme reading the same choice, youth's "All years" and new blocks, Data health's *Compared with Power BI*
(counts only) and the admin pages for Administrators only. The payloads are built here as Compiler builds them
(tests/education/payloads.py); nothing is real."""

import datetime
import json

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import SECTION_EDITOR
from neurodb.cpd import services as cpd_services
from neurodb.education import power_bi
from neurodb.education.admin import ensure_cards
from neurodb.education.models import EducationFigures, PowerBiCard, PowerBiCheck, PowerBiView
from neurodb.reports import services as report_services
from neurodb.youth.models import YouthFigures, figures_model
from tests.youth.test_power_bi_sync import format_two

from .payloads import (
    SCHOOLS,
    dirasa_payload,
    format_three,
    makani_payload,
    power_bi_block,
    view_definition,
)
from .test_dashboards import BRIDGING, REGISTRATIONS, STAFF, TEACHERS

pytestmark = pytest.mark.django_db

COUNTED = datetime.datetime(
    2026, 9, 29, 6, 4, 37, tzinfo=datetime.UTC
)  # the payloads' counted_at (09:04 Beirut)


def store(payload, *, counted_at=COUNTED, compare=True):
    """The payload kept as the sync keeps it: the figures, and the comparison with Power BI's view."""
    record = EducationFigures.objects.create(
        programme=payload["programme"],
        year=payload["year"],
        counted_at=counted_at,
        fetched_at=timezone.now(),
        payload=payload,
    )
    if compare:
        power_bi.store(payload["programme"], payload["year"], payload)
    return record


def page(client, name, **params):
    response = client.get(reverse(f"education:{name}"), params)
    assert response.status_code == 200
    return response


def choose(programme, card, variant, **extra):
    return PowerBiCard.objects.update_or_create(
        programme=programme, card=card, defaults={"variant": variant, **extra}
    )[0]


def makani3(registrations=REGISTRATIONS, **block):
    payload = makani_payload(registrations, STAFF)
    return format_three(payload, power_bi_block("mscc", "2026", **block) if block else None)


# ------------------------------------------------------------------------------------------------ the page line
def test_the_line_says_the_figures_are_the_rows_of_power_bis_view(client_viewer):
    store(makani3())
    response = page(client_viewer, "makani")
    note = response.context["power_bi"]
    assert (note["state"], note["view"], note["period"], note["deleted_left_out"]) == (
        "view",
        "vw_mscc_data",
        "2026",
        1,
    )
    html = response.content.decode()
    assert (
        "Counted from Power BI's view <code>vw_mscc_data</code>: the same registrations Power BI reads for 2026 "
        "(deleted ones left out). Compiler counted at 09:04 on 29 Sep; Power BI shows the time of its own refresh."
    ) in html
    assert "1 deleted registration in the view is left out." in html and "Details on Data health." in html
    # format 3's registrations of the year's partners without a round yet
    assert "2 registrations wait for a cycle and are in neither dashboard." in html
    # every tab whose figures come from the view says so
    for tab in ("education", "health", "maps"):
        assert "Counted from Power BI's view" in page(client_viewer, "makani", tab=tab).content.decode()


def test_the_line_says_why_compilers_own_rule_was_used(client_viewer):
    store(makani3(mode="rule", reason="no SELECT permission on vw_mscc_data"))
    html = page(client_viewer, "makani").content.decode()
    assert (
        "Power BI's view could not be used (no SELECT permission on vw_mscc_data), so Compiler counted with its own "
        "rule; the figures may differ from Power BI."
    ) in html
    assert "Counted from Power BI's view" not in html


def test_a_payload_of_an_older_compiler_is_not_compared_yet(client_viewer):
    store(makani_payload(REGISTRATIONS, STAFF), compare=False)  # format 2: no power_bi block
    response = page(client_viewer, "makani")
    assert response.context["power_bi"] == {"state": "none"}
    assert "Not yet compared with Power BI&#x27;s view." in response.content.decode() or (
        "Not yet compared with Power BI's view." in response.content.decode()
    )
    assert response.context["data"]["pending_round"] == 0


def test_a_comparison_of_an_older_count_says_so(client_viewer):
    """A payload without the block leaves the stored comparison: the page says it is of an earlier count."""
    record = store(makani3())
    record.counted_at = COUNTED + datetime.timedelta(days=1)
    record.save(update_fields=["counted_at"])
    html = page(client_viewer, "makani").content.decode()
    assert "Not compared with Power BI&#x27;s view in this count" in html or (
        "Not compared with Power BI's view in this count" in html
    )
    assert "Counted from Power BI's view" not in html


def test_dirasa_outreach_is_not_from_the_view(client_viewer):
    store(format_three(dirasa_payload(BRIDGING, TEACHERS)))
    assert page(client_viewer, "dirasa").context["power_bi"]["state"] == "view"
    outreach = page(client_viewer, "dirasa", tab="outreach")
    assert outreach.context["power_bi"] is None and "Power BI's view" not in outreach.content.decode()


# --------------------------------------------------------------------------------- charts without "Not specified"
def test_makani_percentage_charts_leave_not_specified_out(client_viewer):
    blank = {
        "child": 9,
        "center": 100,
        "nationality": None,
        "package": None,
        "id_type": None,
    }  # sex not given
    store(makani3(REGISTRATIONS + [blank]))
    data = page(client_viewer, "makani").context["data"]
    assert data["kpis"]["children"] == 6  # counted, but in no bar
    assert data["charts"]["gender"] == [["Female", 3], ["Male", 2]]
    assert data["charts"]["nationality"] == [["Syrian", 3], ["Lebanese", 1], ["Palestinian from Lebanon", 1]]
    assert data["charts"]["package"] == [["Core-Package", 3], ["Walk-in", 2]]
    assert sum(n for _label, n in data["charts"]["gender"]) == 5  # the bars add up to 100 % of the known
    charts = page(client_viewer, "makani", tab="education").context["data"]["charts"]
    assert "Not specified" not in [label for label, _n in charts["id_type"]]


def test_dirasa_charts_leave_not_specified_out(client_viewer):
    blank = {"child": 16, "school": 300, "partner": 7, "governorate": None, "nationality": None}
    store(format_three(dirasa_payload(BRIDGING + [blank], TEACHERS)))
    charts = page(client_viewer, "dirasa").context["data"]["charts"]
    assert charts["nationality"] == [["Syrian", 2], ["Lebanese", 1], ["Non-Lebanese", 2]]
    assert charts["gender"] == [["Female", 3], ["Male", 2]]
    assert charts["governorate"] == [["Akkar", 3], ["Beirut", 1], ["El Nabatieh", 1]]


# ------------------------------------------------------------------------- Dirasa: schools with registrations
TEACHERS_ONLY = {  # a school listed for its teachers alone: no registration in the round
    "id": 303, "name": "Teachers only school", "number": "1004", "type": "Public School", "governorate": 22,
    "district": None, "emergency": "No", "latitude": 33.9, "longitude": 35.5, "listed": [7],
    "counts": {"children": 100, "children_male": 50, "children_female": 50, "children_lebanese": 60,
               "children_non_lebanese": 40, "cwd": 5, "dirasa_children": 0, "dirasa_cwd": 0},
}  # fmt: skip


def test_dirasa_school_figures_count_the_schools_with_registrations(client_viewer):
    teachers = TEACHERS + [{"school": 303, "sex": "Male", "trainings": [3]}]
    schools = SCHOOLS + [TEACHERS_ONLY]  # partner 7 lists the teachers-only school too
    store(format_three(dirasa_payload(BRIDGING, teachers, schools=schools), schools=schools))
    data = page(client_viewer, "dirasa").context["data"]
    kpis, charts = data["kpis"], data["charts"]
    assert kpis["schools"] == 3
    assert (
        kpis["in_school"] == 650 and kpis["in_school_cwd"] == 12
    )  # not the teachers-only school's 100 and 5
    assert kpis["teachers"] == 5  # the teachers card keeps today's definition (SPEC_PBI §5.2): every teacher
    assert charts["school_types"] == [["Private Free School", 1], ["Private School", 1]]  # no "Public School"
    assert charts["in_school_nationality"] == [["Lebanese children", 450], ["Non-Lebanese children", 200]]
    assert charts["trainings"] == [["SEL", 2], ["Inclusion", 1], ["PSEA", 1]]  # the PSEA of school 302 only
    check = power_bi.check("bridging", "Bridging 2025-2026")
    assert check.variants["teachers"]["round_at_schools_with_children"] == 4
    assert check.variants["in_school"] == {"schools_round": 650, "schools_all_listed": 750}
    names = [r["name"] for r in page(client_viewer, "dirasa", tab="map").context["data"]["school_rows"]]
    assert "Teachers only school" not in names and len(names) == 3


# ----------------------------------------------------------------------------------------------- card versions
def test_a_version_chosen_in_the_admin_shows_at_once(client_viewer):
    store(makani3())
    assert page(client_viewer, "makani").context["data"]["kpis"]["cwd"] == 2  # a disability other than "No"
    choose("mscc", "cwd", "not_no_or_blank")  # the cached tab is not read again: the choice is in its key
    response = page(client_viewer, "makani")
    assert response.context["data"]["kpis"]["cwd"] == 3  # child 3's blank disability counted
    assert (
        "Version chosen to match Power BI: Anything but “No”, blanks included." in response.content.decode()
    )
    choose("mscc", "working", "not_no")
    assert page(client_viewer, "makani").context["data"]["kpis"]["working"] == 3  # "Yes" twice and one blank


def test_a_version_the_payload_cannot_count_falls_back_to_the_default(client_viewer):
    store(makani_payload(REGISTRATIONS, STAFF), compare=False)  # format 2: no view_rows, no screened_muac
    choose("mscc", "children", "view_rows")
    choose("mscc", "screened", "muac_only")
    response = page(client_viewer, "makani")
    card = response.context["data"]["cards"]["children"]
    assert (card["value"], card["variant"], card["chosen"], card["fallback"]) == (
        5,
        "registrations",
        "view_rows",
        True,
    )
    assert "cannot be counted here: Registrations shown." in response.content.decode()
    health = page(client_viewer, "makani", tab="health").context["data"]
    assert health["kpis"]["screened"] == 3 and health["cards"]["screened"]["fallback"]
    choose("mscc", "children", "no_such_version")  # a version no longer defined: the default, quietly
    assert power_bi.chosen("mscc", "children") == "registrations"


def test_dirasa_children_shows_unique_children_and_registrations(client_viewer):
    twice = {"child": 11, "school": 301, "partner": 7, "governorate": 20, "sex": "Female", "nationality": 1}
    store(format_three(dirasa_payload(BRIDGING + [twice], TEACHERS)))
    response = page(client_viewer, "dirasa")
    assert response.context["data"]["kpis"]["children"] == 5  # unique children: Power BI's card (E19)
    html = response.content.decode()
    assert "6 registrations" in html
    # the school's governorate is not a grouping of the unique count: registrations, with a hint
    by_place = page(client_viewer, "dirasa", governorate="20")
    card = by_place.context["data"]["cards"]["children"]
    assert (card["value"], card["variant"], card["fallback"]) == (2, "registrations", True)
    # nobody chose a version: the hint says the default cannot answer, never "the version chosen"
    assert card["hint"] == "Unique children cannot be counted under these filters: Registrations shown."
    assert "The version chosen" not in by_place.content.decode()
    ensure_cards()  # the admin list's rows, each with its default: still nobody's choice
    assert "The version chosen" not in page(client_viewer, "dirasa", governorate="20").content.decode()
    choose("bridging", "children", "registrations")
    response = page(client_viewer, "dirasa")
    assert response.context["data"]["kpis"]["children"] == 6
    assert "5 children (each once)" in response.content.decode()


def test_the_country_programme_reads_the_same_childrens_version():
    twice = {"child": 11, "school": 301, "partner": 7, "governorate": 20, "sex": "Female"}
    record = store(format_three(dirasa_payload(BRIDGING + [twice], TEACHERS)))
    assert cpd_services.education_total(record.payload, "bridging") == 5
    assert cpd_services._stored_total(record) == 5
    choose("bridging", "children", "registrations")
    assert cpd_services.education_total(record.payload, "bridging") == 6
    assert (
        cpd_services._stored_total(record) == 6
    )  # kept per worker under the choice: a new choice, a new total


# ------------------------------------------------------------------------------------------------------- youth
@pytest.fixture
def youth(db):
    for year in ("2026", "2025", YouthFigures.ALL):
        payload = format_two(year)
        figures_model(year).objects.create(year=year, fetched_at=timezone.now(), payload=payload)
        power_bi.store("youth", year, payload)


def test_youth_offers_all_years_and_shows_the_registered_young_people(client_viewer, youth):
    response = client_viewer.get(reverse("youth:dashboard"))
    html = response.content.decode()
    assert '<option value="all" >All years</option>' in html or 'value="all"' in html
    data = response.context["data"]
    assert (data["total"], data["second"]) == (3, {"variant": "registered_people", "value": 4})
    assert "4 registered, enrolled or not." in html
    assert data["home_governorates"] == {"rows": [("Akkar", 2), ("Beirut", 2)]}
    assert "Counted from Power BI's view <code>vw_youth_data_all</code>" in html
    every = client_viewer.get(reverse("youth:dashboard"), {"year": "all"})
    assert every.context["filters"].year == "all" and every.context["data"]["targets_hidden"]
    assert "Targets are set per year: they are not shown for all years together." in every.content.decode()
    assert "every year together" in every.content.decode()


def test_a_count_time_without_a_time_zone_is_read_in_the_local_one(client_viewer):
    """Compiler sends its count times with a time zone; one without (or an impossible date) never takes the
    youth page down when it is compared with the stored check's."""
    naive = power_bi.counted_at({"generated_at": "2026-10-16T02:31:10"})
    assert naive is not None and naive.tzinfo is not None
    assert power_bi.counted_at({"counted_at": "2026-06-31T02:31:10+00:00"}) is None  # no 31 June
    payload = format_two("2026") | {"generated_at": "2026-10-16T02:31:10"}
    YouthFigures.objects.create(year="2026", fetched_at=timezone.now(), payload=payload)
    power_bi.store("youth", "2026", payload)
    response = client_viewer.get(reverse("youth:dashboard"))
    assert response.status_code == 200 and response.context["data"]["power_bi"]["state"] == "view"
    assert not response.context["data"]["power_bi"]["stale"]


def test_youth_headline_reads_the_version_chosen(client_viewer, youth):
    choose("youth", "reached", "registered_people")
    data = client_viewer.get(reverse("youth:dashboard")).context["data"]
    assert (data["total"], data["second"]["variant"], data["second"]["value"]) == (4, "enrolled_people", 3)
    by_master = client_viewer.get(reverse("youth:dashboard"), {"master": "10"}).context["data"]
    assert by_master["total"] == 3 and by_master["cards"]["reached"]["fallback"]  # the block has no master
    assert by_master["home_governorates"] == {"rows": None}


# ------------------------------------------------------------------------------------------------- Data health
def _health(client):
    from tests.web.parts import get_part, text

    return text(get_part(client, reverse("reports:data_health"), "power-bi"))


def test_data_health_compares_each_programme_in_counts_only(client, admin_user):
    store(makani3())
    store(makani_payload(REGISTRATIONS, STAFF, year="2025"), compare=False)
    choose("mscc", "cwd", "known_not_no", powerbi_value=3, powerbi_period="2026")
    choose("mscc", "children", "registrations", powerbi_value=999)
    client.force_login(admin_user)
    html = _health(client)
    assert 'id="power-bi-mscc"' in html and "Not yet compared" in html  # Dirasa and youth: nothing stored
    assert "the view" in html and "vw_mscc_data" in html
    assert "−1 deleted" in html  # the view's deleted registration left out
    assert "+1 TLS" in html  # what NeuroDB counted before this change and no longer does
    assert "equals Power BI" in html and "No version equals Power BI" in html
    # counts only: no definition, no masked text, no SQL
    assert view_definition("mscc") not in html and "masked" not in html and "SELECT" not in html
    rows = power_bi.health()
    card = next(c for c in rows[0]["checks"][0]["cards"] if c["card"] == "cwd")
    assert [v["variant"] for v in card["versions"] if v["equal"]] == ["not_no_or_blank"]
    assert [v["variant"] for v in card["versions"] if v["in_use"]] == ["known_not_no"]


def test_data_health_in_rule_mode_never_says_a_registration_is_not_in_the_view(client, admin_user):
    """In rule mode Compiler never read the view's rows (CONTRACT_PBI §3.4: the totals that need them are
    null): whether a registration is in the view is unknown, not "no"."""
    store(makani3(mode="rule", reason="no SELECT permission on vw_mscc_data"))
    client.force_login(admin_user)
    html = _health(client)
    row = power_bi.health()[0]["checks"][0]
    assert (row["mode"], row["view_read"], row["view_ids"], row["not_in_view"], row["left_out"]) == (
        "rule",
        False,
        None,
        [],
        [],
    )
    assert {r["in_view"] for r in row["reasons"]} == {None}
    assert "not in the view" not in html and "not compared: the view was not read" in html
    assert "Compiler&#x27;s rule" in html or "Compiler's rule" in html
    assert "+1 TLS" in html  # what NeuroDB counted before this change is still explained
    # once the view is read (view mode), the column says yes or no again
    store(makani3() | {"year": "2025"})
    row = next(c for c in power_bi.health()[0]["checks"] if c["period"] == "2025")
    assert row["view_read"] and {r["in_view"] for r in row["reasons"]} == {True, False}
    assert row["not_compared"] == ""


def test_data_health_in_view_mode_with_a_failed_comparison_never_says_the_view_was_not_read(
    client, admin_user
):
    """Compiler counted the view's rows (view mode) but its reconciliation failed, e.g. a timeout: the block's
    reconciliation is ``{}`` and the error is listed (CONTRACT_PBI §3.4, §3.7). The counts are the view's, so
    Data health says the comparison could not be made, never "the view was not read"."""
    payload = makani3()
    payload["power_bi"]["reconciliation"] = {}
    payload["power_bi"]["errors"] = [
        "reconciliation: QueryCanceled: canceling statement due to statement timeout"
    ]
    store(payload)
    client.force_login(admin_user)
    row = power_bi.health()[0]["checks"][0]
    assert (row["mode"], row["view_read"], row["not_compared"], row["not_in_view"], row["left_out"]) == (
        "view",
        False,
        "failed",
        [],
        [],
    )
    html = _health(client)
    assert "the view was not read" not in html and "not in the view" not in html
    assert "not compared: the comparison could not be made (see the errors)" in html
    assert "1 part not compared" in html and "reconciliation: QueryCanceled" in html
    # in rule mode with a failed comparison, the view was indeed not read: said so
    store(makani3(mode="rule", reason="view missing") | {"year": "2025"})
    PowerBiCheck.objects.filter(programme="mscc", period="2025").update(reconciliation={})
    row = next(c for c in power_bi.health()[0]["checks"] if c["period"] == "2025")
    assert (row["mode"], row["not_compared"]) == ("rule", "view")


def test_data_health_shows_blank_keys_and_a_related_view_not_described(client, admin_user):
    """CONTRACT_PBI §3.3, §3.6 [changed, fixer R1]: rows of the view without a key are counted apart
    (``null_keys``), and a related view Compiler could not describe is "could not be read", not "missing"."""
    payload = makani3()
    payload["power_bi"]["key_check"]["null_keys"] = 2
    payload["power_bi"]["related"]["vw_center_data"] = {"exists": None, "error": "QueryCanceled"}
    store(payload)
    client.force_login(admin_user)
    row = power_bi.health()[0]["checks"][0]
    assert row["key_facts"]["null_keys"] == 2
    related = {r["name"]: r for r in row["related"]}
    assert related["vw_center_data"]["exists"] and related["vw_center_data"]["error"] == "QueryCanceled"
    html = _health(client)
    assert "2 rows of the view have no key (no registration): left out." in html
    assert "could not be read" in html


def test_data_health_says_when_a_related_view_was_cut_or_counted_for_the_period_only(client, admin_user):
    """CONTRACT_PBI §3.6 [changed, fixer R3]: Compiler keeps 50 groups of a related view at most. A cut list is
    not the view's total, so the version counted from it is unavailable, and Data health says the list was cut
    (it showed the 50 kept rows as if they were all). Dirasa's teachers are counted for the period's rounds
    only (``rounds``), whatever the rounds of the other CLM programmes, so their version is available."""
    payload = makani3()
    payload["power_bi"]["related"]["vw_center_program_staff"]["truncated"] = True
    store(payload)
    dirasa = format_three(
        dirasa_payload(BRIDGING, TEACHERS), power_bi_block("bridging", "Bridging 2025-2026")
    )
    round_id = dirasa["rounds"][0]["id"]
    dirasa["power_bi"]["related"] = {
        "vw_teacher_data": {"exists": True, "by": ["round_id", "is_active_current_round"],
                            "measures": ["rows", "ids"], "rows": [[round_id, "yes", 4, 4], [round_id, "no", 1, 1]],
                            "truncated": False, "rounds": [round_id]},
    }  # fmt: skip
    store(dirasa)
    assert power_bi.variant_totals("mscc", payload)["staff"]["view_rows"] is None
    assert power_bi.variant_totals("bridging", dirasa)["teachers"]["view_rows"] == 5
    client.force_login(admin_user)
    rows = {p["programme"]: p["checks"][0] for p in power_bi.health() if p["checks"]}
    staff = next(r for r in rows["mscc"]["related"] if r["name"] == "vw_center_program_staff")
    teachers = next(r for r in rows["bridging"]["related"] if r["name"] == "vw_teacher_data")
    assert (staff["truncated"], staff["period_rounds"]) == (True, False)
    assert (teachers["truncated"], teachers["period_rounds"]) == (False, True)
    html = _health(client)
    assert html.count("cut at 50 groups: not the view's total, so its version is not counted") == 1
    assert html.count("this period's rounds only") == 1


def test_data_health_says_when_the_comparison_is_of_an_older_count(client, admin_user):
    """A later payload without a comparison (Compiler gone back to an earlier version) keeps the comparison,
    which is then of an older count: Data health and the summary say so, as the page does."""
    store(makani3())
    later = makani_payload(REGISTRATIONS, STAFF, counted_at="2026-10-20T02:31:10+00:00")
    EducationFigures.objects.filter(programme="mscc", year="2026").update(payload=later)
    power_bi.store("mscc", "2026", later)  # as the sync does for every payload
    client.force_login(admin_user)
    row = power_bi.health()[0]["checks"][0]
    assert row["stale"] and row["mode"] == "view"
    assert "not compared in the latest count" in _health(client)
    summary = {r["programme"]: r for r in report_services.data_health()["power_bi"]}
    assert summary["mscc"]["stale"] is True
    store(makani3() | {"year": "2025"})  # compared in the count kept: not stale
    assert not next(c for c in power_bi.health()[0]["checks"] if c["period"] == "2025")["stale"]


def test_data_health_shows_every_comparison_of_the_youth_key_check(client, admin_user, youth):
    """The youth key check compares the year, the partner and the deleted flag (CONTRACT_PBI §3.3: the partner
    and the deleted flag prove the key, since enrolment and registration ids overlap as numbers): Data health
    shows all three, as for Makani and Dirasa their round, partner, centre or school."""
    client.force_login(admin_user)
    html = _health(client)
    rows = {(p["programme"], c["period"]): c for p in power_bi.health() for c in p["checks"]}
    labels = [k["label"] for k in rows[("youth", "2026")]["key_check"]]
    assert labels == [
        "are registrations",
        "have the registration's own year",
        "have the registration's own partner",
        "agree with the registration on whether it is deleted",
    ]
    assert "of the view's rows agree with the registration on whether it is deleted" in html
    assert "own year" in html


def test_data_health_links_to_the_admin_for_administrators_only(client, client_viewer, admin_user):
    store(makani3())
    viewer_page = client_viewer.get(reverse("reports:data_health")).content.decode()
    assert "Compared with Power BI" in viewer_page and "Choose card definitions" not in viewer_page
    client.force_login(admin_user)
    admin_page = client.get(reverse("reports:data_health")).content.decode()
    assert reverse("admin:education_powerbicard_changelist") in admin_page
    assert reverse("admin:education_powerbiview_changelist") in admin_page


def test_data_health_summary_for_the_assistant_and_the_api():
    store(makani3())
    summary = {row["programme"]: row for row in report_services.data_health()["power_bi"]}
    assert summary["mscc"]["mode"] == "view" and summary["mscc"]["difference"] == -1
    assert summary["youth"] == {"programme": "youth", "label": "Youth programmes", "compared": False}
    as_json = report_services.health_as_dict(report_services.data_health())
    assert json.loads(json.dumps(as_json))["power_bi"][0]["checked_at"] == "2026-10-16T02:31:10+00:00"


# ------------------------------------------------------------------------------------------------------- admin
@pytest.fixture
def editor(db, roles):
    user = User.objects.create_user(username="editor", password="editor-pass-123456", is_staff=True)
    user.groups.add(Group.objects.get(name=SECTION_EDITOR))
    return user


def test_the_view_definitions_and_card_versions_are_for_administrators_only(client, editor, admin_user):
    store(makani3())
    view = PowerBiView.objects.get(name="vw_mscc_data")
    client.force_login(editor)
    for url in (
        reverse("admin:education_powerbiview_changelist"),
        reverse("admin:education_powerbiview_change", args=[view.pk]),
        reverse("admin:education_powerbicard_changelist"),
    ):
        assert client.get(url).status_code in (302, 403), url
    index = client.get(reverse("admin:index")).content.decode()
    assert "Power BI cards" not in index and "Power BI views" not in index
    client.force_login(admin_user)
    html = client.get(reverse("admin:education_powerbiview_change", args=[view.pk])).content.decode()
    assert (
        "&lt;masked text, 11 chars&gt;" in html and "(&lt;12 numbers&gt;)" in html
    )  # the stored, masked text
    assert view.definition == view_definition("mscc")


def test_an_administrator_types_power_bis_number_and_picks_a_version(client, admin_user):
    store(makani3())
    client.force_login(admin_user)
    listing = client.get(reverse("admin:education_powerbicard_changelist"))
    assert listing.status_code == 200
    expected = {(p, c) for p, cards in power_bi.VARIANTS.items() for c in cards}
    assert (
        set(PowerBiCard.objects.values_list("programme", "card")) == expected
    )  # every card, with its default
    card = PowerBiCard.objects.get(programme="mscc", card="cwd")
    assert card.variant == "known_not_no" and card.noted_at is None
    url = reverse("admin:education_powerbicard_change", args=[card.pk])
    form = client.get(url)
    assert "Reported by" not in form.content.decode() and "Anything but" in form.content.decode()
    response = client.post(
        url, {"variant": "not_no_or_blank", "powerbi_value": "3", "powerbi_period": "2026"}
    )
    assert response.status_code == 302
    card.refresh_from_db()
    assert (card.variant, card.powerbi_value, card.noted_by) == ("not_no_or_blank", 3, admin_user)
    assert card.noted_at is not None
    bad = client.post(
        url, {"variant": "unique", "powerbi_value": "", "powerbi_period": ""}
    )  # Dirasa's version
    assert bad.status_code == 200 and PowerBiCard.objects.get(pk=card.pk).variant == "not_no_or_blank"


def test_power_bis_number_is_compared_with_the_period_it_was_read_for(client, admin_user):
    """The period is chosen among those Data health compares (a Dirasa round by its name: typing "2026", as
    Power BI's page title says, would match nothing); a number with no period is compared with the latest
    period only, never with every one."""
    later = "Bridging 2026-2027"
    store(format_three(dirasa_payload(BRIDGING, TEACHERS)))  # Bridging 2025-2026
    store(format_three(dirasa_payload(BRIDGING, TEACHERS, round_name=later)))
    unique = str(power_bi.check("bridging", later).variants["children"]["unique"])
    client.force_login(admin_user)
    client.get(reverse("admin:education_powerbicard_changelist"))  # every card, with its default
    card = PowerBiCard.objects.get(programme="bridging", card="children")
    url = reverse("admin:education_powerbicard_change", args=[card.pk])
    form = client.get(url).content.decode()
    assert later in form and "Bridging 2025-2026" in form
    typed = client.post(url, {"variant": "unique", "powerbi_value": unique, "powerbi_period": "2026"})
    assert typed.status_code == 200 and "2026 is not one of the available choices" in typed.content.decode()
    assert PowerBiCard.objects.get(pk=card.pk).powerbi_value is None
    assert (
        client.post(url, {"variant": "unique", "powerbi_value": unique, "powerbi_period": later}).status_code
        == 302
    )
    rows = {c["period"]: c for c in power_bi.health()[1]["checks"]}
    children = {p: next(x for x in c["cards"] if x["card"] == "children") for p, c in rows.items()}
    assert children[later]["powerbi"] == int(unique) and children["Bridging 2025-2026"]["powerbi"] is None
    assert "unique" in [v["variant"] for v in children[later]["versions"] if v["equal"]]
    # left empty: the latest period, saved with the number
    assert (
        client.post(url, {"variant": "unique", "powerbi_value": unique, "powerbi_period": ""}).status_code
        == 302
    )
    assert PowerBiCard.objects.get(pk=card.pk).powerbi_period == later
    # a number kept without a period (typed before the list existed) is compared with the latest one only
    PowerBiCard.objects.filter(pk=card.pk).update(powerbi_period="")
    rows = {c["period"]: c for c in power_bi.health()[1]["checks"]}
    shown = {p: next(x for x in c["cards"] if x["card"] == "children")["powerbi"] for p, c in rows.items()}
    assert shown == {later: int(unique), "Bridging 2025-2026": None}
    # and one typed with a period Data health does not compare says so, rather than "the next run counts it"
    PowerBiCard.objects.filter(pk=card.pk).update(powerbi_period="2026")
    listing = client.get(reverse("admin:education_powerbicard_changelist")).content.decode()
    assert "no such period: choose one" in listing
    page_ = client.get(url).content.decode()
    assert "not a period Data health compares" in page_ and "the next run of the Compiler job" not in page_


def test_the_latest_dirasa_round_is_the_one_the_page_opens_on_whatever_its_name(client, admin_user):
    """A Dirasa round's name is free text in Compiler (CLMRound, shared by every CLM programme), so its text
    order need not be its date order ("Round 9" sorts after "Round 10"). The page opens on the round that
    starts last; Data health, the card admin, a number typed without a period and the summary take the same
    round as the latest, and Data health's first rows are the latest rounds, not the last names."""
    from neurodb.education import services
    from neurodb.education.admin import compared_periods

    rounds = [("Round 7 2023-2024", "2023-09-01"), ("Round 8 2024-2025", "2024-09-01"),
              ("Round 9 2025-2026", "2025-09-01"), ("Round 10 2026-2027", "2026-09-01")]  # fmt: skip
    for name, start in rounds:
        store(format_three(dirasa_payload(BRIDGING, TEACHERS, round_name=name, start_date=start)))
    latest_first = [name for name, _start in reversed(rounds)]
    assert services.periods("bridging") == latest_first  # the page's order
    assert power_bi.latest_period("bridging") == "Round 10 2026-2027"
    assert [c["period"] for c in power_bi.health()[1]["checks"]] == latest_first[: power_bi.HEALTH_PERIODS]
    assert compared_periods("bridging") == latest_first
    summary = {r["programme"]: r for r in report_services.data_health()["power_bi"]}
    assert summary["bridging"]["period"] == "Round 10 2026-2027"
    # Power BI's number typed with the period left empty goes to the round the page opens on
    unique = power_bi.check("bridging", "Round 10 2026-2027").variants["children"]["unique"]
    client.force_login(admin_user)
    changelist = reverse("admin:education_powerbicard_changelist")
    client.get(changelist)  # every card, with its default
    card = PowerBiCard.objects.get(programme="bridging", card="children")
    url = reverse("admin:education_powerbicard_change", args=[card.pk])
    # the card admin's versions, no number typed yet: those of the round the page opens on
    html = client.get(url).content.decode()
    assert [name for name, _start in rounds if f"In {name}:" in html] == ["Round 10 2026-2027"]
    answer = client.post(url, {"variant": "unique", "powerbi_value": str(unique), "powerbi_period": ""})
    assert answer.status_code == 302
    assert PowerBiCard.objects.get(pk=card.pk).powerbi_period == "Round 10 2026-2027"
    # and one kept without a period is compared with that round only: on Data health, in the card admin's
    # versions and in its column of the versions equal to Power BI's number
    PowerBiCard.objects.filter(pk=card.pk).update(powerbi_period="")
    rows = {c["period"]: c for c in power_bi.health()[1]["checks"]}
    shown = {p: next(x for x in c["cards"] if x["card"] == "children")["powerbi"] for p, c in rows.items()}
    assert shown == {"Round 10 2026-2027": unique, "Round 9 2025-2026": None, "Round 8 2024-2025": None}
    html = client.get(url).content.decode()
    assert [name for name, _start in rounds if f"In {name}:" in html] == ["Round 10 2026-2027"]
    html = client.get(changelist, {"programme": "bridging"}).content.decode()
    assert [name for name, _start in rounds if f"({name})" in html] == ["Round 10 2026-2027"]


def test_the_figures_lists_say_how_compiler_counted(client, admin_user):
    store(makani3())
    store(makani3(mode="rule", reason="view missing") | {"year": "2025"}, compare=False)
    client.force_login(admin_user)
    html = client.get(reverse("admin:education_educationfigures_changelist")).content.decode()
    assert "Counted from" in html and "Difference with the view" in html
    assert "the view" in html and "Compiler&#x27;s rule" in html and "-1" in html


def test_ensure_cards_adds_only_the_missing_ones():
    choose("mscc", "cwd", "not_no_or_blank")
    ensure_cards()
    ensure_cards()
    assert PowerBiCard.objects.get(programme="mscc", card="cwd").variant == "not_no_or_blank"
    assert PowerBiCard.objects.count() == sum(len(cards) for cards in power_bi.VARIANTS.values())
    assert not PowerBiCheck.objects.exists()


# --------------------------------------------------------------------------------------------- the assistant
def test_the_assistant_says_how_the_figures_were_counted():
    from neurodb.assistant.hub_tools import education_figures

    store(makani3())
    answer = education_figures("makani")
    assert answer["compared_with_power_bi"].startswith("Counted from Power BI's view vw_mscc_data")
    assert "definition" not in json.dumps(answer, default=str).lower()
