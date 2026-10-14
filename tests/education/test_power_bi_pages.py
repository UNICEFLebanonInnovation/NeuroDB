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
from neurodb.youth.models import YouthFigures
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
    by_place = page(client_viewer, "dirasa", governorate="20").context["data"]["cards"]["children"]
    assert (by_place["value"], by_place["variant"], by_place["fallback"]) == (2, "registrations", True)
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
        YouthFigures.objects.create(year=year, fetched_at=timezone.now(), payload=payload)
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
