"""Dirasa counted per year (Compiler's payload format 4, SPEC_DY §4-5, CONTRACT_PBI §9): the page selects a year
("Dirasa Programme Dashboard 2026"), lists the rounds of the year under its title and offers a Round slicer; a
child registered in two rounds of the year counts once in the children card and twice in the registrations. The
older per-round rows stay stored and hidden as soon as a per-year row is the latest fetched (and come back after a
rollback to format 3). Data health, the card admin, the summary and Home, Ask, the country programme and the sync
use the years. The payloads are built here as Compiler builds them (tests/education/payloads.py); nothing is
real."""

import datetime
import html as html_lib
import re

import pytest
from django.core.cache import cache
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from neurodb.assistant.tools import ToolInputError
from neurodb.core.models import SyncRun
from neurodb.cpd import services as cpd_services
from neurodb.cpd import sources as cpd_sources
from neurodb.cpd.models import Link
from neurodb.education import power_bi, services
from neurodb.education.admin import compared_periods
from neurodb.education.figures import Figures
from neurodb.education.models import EducationFigures, PowerBiCard, PowerBiCheck
from neurodb.education.sync import sync, targets
from neurodb.insights import readiness
from neurodb.reports import home
from tests.cpd.test_cpd import cycle, framework  # noqa: F401  (fixtures)

from .payloads import ROUNDS_2026, dirasa_payload, dirasa_year_payload, format_three, power_bi_block
from .test_dashboards import BRIDGING, OUTREACH, TEACHERS
from .test_power_bi_sync import Compiler

pytestmark = pytest.mark.django_db

R1, R2 = 31, 32  # ROUNDS_2026: Bridging Round 1 2026 (from 12 Jan), Bridging Round 2 2026 (from 1 Sep)
# Round 1: BRIDGING's five children (11-15); Round 2: child 11 again and child 16 (new)
YEAR_REGISTRATIONS = [dict(r, round=R1) for r in BRIDGING] + [
    {"child": 11, "school": 300, "partner": 7, "governorate": 20, "sex": "Female", "nationality": 1,
     "disability": 1, "level": "level_two", "round": R2},
    {"child": 16, "school": 301, "partner": 7, "governorate": 22, "sex": "Male", "nationality": 5,
     "disability": 1, "round": R2},
]  # fmt: skip
YEAR_TEACHERS = [dict(t, round=R1) for t in TEACHERS] + [
    {"school": 300, "sex": "Female", "trainings": [3], "round": R2}
]
OLD_ROUND = "Bridging 2025-2026"  # dirasa_payload's round (id 9, from 1 Oct 2025)
ROUNDS_2025 = [
    {"id": 9, "name": OLD_ROUND, "start_date": "2025-10-01", "end_date": "2026-06-30", "current": False,
     "current_year": False, "year": 2025, "year_from": "start_date"},
]  # fmt: skip
# fetch times, fixed (the suite's clock is pinned)
EARLY = datetime.datetime(2026, 10, 1, 2, 30, tzinfo=datetime.UTC)
LATER = datetime.datetime(2026, 10, 2, 2, 30, tzinfo=datetime.UTC)
ROLLBACK = datetime.datetime(2026, 10, 3, 2, 30, tzinfo=datetime.UTC)
COUNTED = datetime.datetime(2026, 9, 29, 6, 7, 25, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()  # the page and the sources' caches: each test counts on its own
    yield
    cache.clear()


def year_payload(year="2026", **kwargs):
    if year == "2025":
        kwargs.setdefault("rounds", ROUNDS_2025)
        regs = [dict(r, round=9) for r in BRIDGING[:3]]
        return dirasa_year_payload(
            regs, [dict(t, round=9) for t in TEACHERS], OUTREACH, year="2025", **kwargs
        )
    return dirasa_year_payload(YEAR_REGISTRATIONS, YEAR_TEACHERS, OUTREACH, year=year, **kwargs)


def store(payload, *, fetched_at=None, compare=True):
    """The payload kept as the sync keeps it (with its fetch time), and its comparison with Power BI's view."""
    record = EducationFigures.objects.create(
        programme=payload["programme"],
        year=payload["year"],
        counted_at=COUNTED,
        fetched_at=fetched_at or timezone.now(),
        payload=payload,
    )
    if compare:
        power_bi.store(payload["programme"], payload["year"], payload)
    return record


def old_round(**kwargs):
    return format_three(dirasa_payload(BRIDGING, TEACHERS, OUTREACH, **kwargs))


def page(client, **params):
    response = client.get(reverse("education:dirasa"), params)
    assert response.status_code == 200
    return response


def rounds_text(response) -> str:
    found = re.search(
        r"<p class=\"page-subtitle small\" data-rounds>(.*?)</p>", response.content.decode(), re.S
    )
    return " ".join(html_lib.unescape(found.group(1)).split()) if found else ""


def slicers(response) -> dict:
    return {s["slicer"].name: s for s in response.context["slicers"]}


# ---------------------------------------------------------------------------------------------- the year page
def test_the_page_shows_a_year_its_rounds_and_each_child_once(client_viewer):
    store(year_payload())
    response = page(client_viewer)
    assert (response.context["periods"], response.context["period"]) == (["2026"], "2026")
    assert (response.context["period_param"], str(response.context["period_label"])) == ("year", "Year")
    html = response.content.decode()
    assert "Dirasa Programme Dashboard 2026" in html
    assert 'for="edu-period">Year</label>' in html and 'id="edu-period" name="year"' in html
    assert '<input type="hidden" name="year" value="2026">' in html  # the slicer bar keeps the year
    assert rounds_text(response) == (
        "Rounds: Bridging Round 1 2026 (from 12 Jan 2026, 5 registrations), "
        "Bridging Round 2 2026 (from 1 Sep 2026, 2 registrations)."
    )
    card = response.context["data"]["cards"]["children"]
    # child 11 is in both rounds: once in the children card, twice in the registrations
    assert (card["value"], card["variant"], card["unique"], card["registrations"]) == (6, "unique", 6, 7)
    assert "7 registrations" in html
    assert response.context["data"]["kpis"]["teachers"] == 5  # 4 in Round 1, 1 in Round 2


def test_the_rounds_line_says_where_each_rounds_year_comes_from():
    from neurodb.education import dirasa

    rounds = [
        dict(ROUNDS_2026[0]),
        {"id": 33, "name": "Round 3 2026", "start_date": None, "end_date": None, "current": False,
         "current_year": True, "year": 2026, "year_from": "name"},
        {"id": 34, "name": "Round 4 2026", "start_date": "2026-11-02", "end_date": None, "current": False,
         "current_year": True, "year": 2026, "year_from": "start_date"},
    ]  # fmt: skip
    regs = [dict(r, round=R1) for r in BRIDGING[:1]] + [dict(r, round=33) for r in BRIDGING[1:2]]
    payload = dirasa_year_payload(regs, rounds=rounds)
    line = dirasa.rounds_line(Figures(payload))
    assert line["current"] is False
    assert [(r["name"], r["start"], r["from_name"], r["registrations"]) for r in line["rounds"]] == [
        ("Bridging Round 1 2026", datetime.date(2026, 1, 12), False, 1),
        ("Round 3 2026", None, True, 1),
        ("Round 4 2026", datetime.date(2026, 11, 2), False, 0),
    ]
    text = " ".join(render_to_string("education/_rounds_line.html", {"line": line}).split())
    assert html_lib.unescape(text) == (
        '<p class="page-subtitle small" data-rounds>Rounds: Bridging Round 1 2026 (from 12 Jan 2026, 1 registration), '
        "Round 3 2026 (year taken from its name, 1 registration), Round 4 2026 (from 2 Nov 2026, no registration "
        "yet).</p>"
    )
    current = dirasa_year_payload(regs, rounds=rounds, period_rule="current_rounds")
    text = render_to_string("education/_rounds_line.html", {"line": dirasa.rounds_line(Figures(current))})
    assert "Rounds marked current in Compiler: Bridging Round 1 2026" in " ".join(text.split())
    # a payload counted per round has no such line
    assert dirasa.rounds_line(Figures(old_round())) is None


# -------------------------------------------------------------------------------------------- the round slicer
def test_the_round_slicer_shows_one_round_of_the_year(client_viewer):
    payload = year_payload()
    store(payload)
    response = page(client_viewer)
    assert list(slicers(response))[0] == "round"
    assert slicers(response)["round"]["options"] == [
        (str(R1), "Bridging Round 1 2026"),
        (str(R2), "Bridging Round 2 2026"),
    ]
    data = page(client_viewer, round=str(R2)).context["data"]
    card = data["cards"]["children"]
    assert (card["value"], card["variant"], card["registrations"]) == (2, "unique", 2)  # children 11 and 16
    assert (data["kpis"]["schools"], data["kpis"]["teachers"]) == (2, 1)
    assert data["charts"]["trainings"] == [["PSEA", 1]]
    assert data["charts"]["gender"] == [["Female", 1], ["Male", 1]]
    first = page(client_viewer, round=str(R1)).context["data"]
    assert (first["cards"]["children"]["value"], first["kpis"]["teachers"], first["kpis"]["schools"]) == (
        5,
        4,
        3,
    )
    # both teachers versions count the round's teachers
    figures = Figures(payload)
    for variant in ("round", "round_at_schools_with_children"):
        assert power_bi.variant_value("bridging", "teachers", variant, figures, {"round": R2}) == 1
    # the unique count answers the round with the partner or the gender (Compiler's groupings) ...
    for params, expected in (({"partner": "7"}, 2), ({"gender": "Male"}, 1)):
        card = page(client_viewer, round=str(R2), **params).context["data"]["cards"]["children"]
        assert (card["value"], card["variant"]) == (expected, "unique"), params
    # ... not with the school: the card shows the registrations and says so
    card = page(client_viewer, round=str(R2), school="300").context["data"]["cards"]["children"]
    assert (card["value"], card["variant"], card["fallback"]) == (1, "registrations", True)
    assert card["hint"] == "Unique children cannot be counted under these filters: Registrations shown."
    # the schools map shows the round's schools
    mapped = page(client_viewer, tab="map", round=str(R2)).context["data"]
    assert [r["name"] for r in mapped["school_rows"]] == ["Akkar private school", "Beirut free school"]
    # not on Outreach: Compiler's Kobo outreach has no round
    outreach = page(client_viewer, tab="outreach", round=str(R2))
    assert "round" not in slicers(outreach) and [str(n) for n in outreach.context["not_applied"]] == ["Round"]
    # a round of another year is no option of this one: ignored
    assert page(client_viewer, round="9").context["filters"] == {}


def test_a_payload_counted_per_round_has_no_round_slicer_on_any_tab(client_viewer):
    store(old_round())
    store(old_round(round_name="Bridging 2024-2025", start_date="2024-10-01"))
    for tab in ("overview", "outreach", "barriers", "map"):
        response = page(client_viewer, tab=tab, round=OLD_ROUND)
        assert response.context["period"] == OLD_ROUND and response.context["period_param"] == "round", tab
        assert "round" not in slicers(response) and response.context["rounds_line"] is None, tab
        assert ("round", OLD_ROUND) not in response.context["kept"], tab
    html = page(client_viewer, tab="outreach", round=OLD_ROUND).content.decode()
    # the period's own hidden input only: no second "round" sent along (data-keep) when another round is chosen
    assert '<input type="hidden" name="round" value="Bridging 2025-2026">' in html
    assert not re.search(r'name="round"[^>]*data-keep', html)
    assert (
        "Dirasa Programme Dashboard Bridging 2025-2026" in html and 'for="edu-period">Round</label>' in html
    )
    # choosing another round there opens it
    assert (
        page(client_viewer, tab="outreach", round="Bridging 2024-2025").context["period"]
        == "Bridging 2024-2025"
    )


# -------------------------------------------------------------------------------- years replace the old rounds
def test_years_hide_the_old_rounds_and_a_rollback_brings_them_back(client_viewer):
    old = store(old_round(), fetched_at=EARLY)
    store(year_payload("2025"), fetched_at=LATER)
    store(year_payload(), fetched_at=LATER)
    response = page(client_viewer)
    assert response.context["periods"] == ["2026", "2025"] and response.context["period"] == "2026"
    stored = power_bi.stored_periods(["bridging"])
    assert (stored.kinds["bridging"], stored.hidden["bridging"]) == ("year", {OLD_ROUND})
    # an old link naming a round opens the year holding it, with that round chosen
    linked = page(client_viewer, round=OLD_ROUND)
    assert linked.context["period"] == "2025" and slicers(linked)["round"]["selected"] == "9"
    assert linked.context["data"]["cards"]["children"]["value"] == 3
    # an old name no stored year holds opens the newest year
    unknown = page(client_viewer, round="Bridging 2019-2020")
    assert unknown.context["period"] == "2026" and slicers(unknown)["round"]["selected"] == ""
    # the admin list: the old round is not on the page, a year opens with ?year=
    assert services.period_kind("bridging") == "year"
    # a rollback of Compiler to format 3: its rounds are fetched again, the latest, and listed again
    EducationFigures.objects.filter(pk=old.pk).update(fetched_at=ROLLBACK)
    rolled = page(client_viewer)
    assert rolled.context["periods"] == [OLD_ROUND] and rolled.context["period_param"] == "round"
    assert power_bi.hidden_periods(["bridging"])["bridging"] == {"2026", "2025"}
    # a year and a round fetched at the same time: the year wins
    EducationFigures.objects.filter(programme="bridging").update(fetched_at=LATER)
    assert services.periods("bridging") == ["2026", "2025"]


def test_the_figures_admin_says_which_rows_the_page_no_longer_shows(client, admin_user):
    store(old_round(), fetched_at=EARLY)
    store(year_payload(), fetched_at=LATER)
    client.force_login(admin_user)
    html = client.get(reverse("admin:education_educationfigures_changelist")).content.decode()
    assert "Not on the page: replaced by the figures Compiler sends now" in html
    assert f'{reverse("education:dirasa")}?year=2026"' in html


# --------------------------------------------------------------------------------------------------- Data health
def _health(client):
    from tests.web.parts import get_part, text

    return text(get_part(client, reverse("reports:data_health"), "power-bi"))


def test_data_health_compares_dirasa_years(client, admin_user, django_assert_max_num_queries):
    store(old_round(), fetched_at=EARLY)  # its comparison is kept, never shown
    view_rounds = [
        *ROUNDS_2026,
        {"id": 33, "name": "Round 3 2026", "start_date": None, "end_date": None, "current": False,
         "current_year": True, "year": 2026, "year_from": "name", "view_rows": 2},
        {"id": 34, "name": "Summer catch-up", "start_date": None, "end_date": None, "current": False,
         "current_year": False, "year": None, "year_from": None, "view_rows": 1},
    ]  # fmt: skip
    block = power_bi_block("bridging", "2026", year_rounds=view_rounds)
    store(year_payload(block=block), fetched_at=LATER)
    store(year_payload("2025", block=power_bi_block("bridging", "2025", year_rounds=ROUNDS_2025, current=False)),
          fetched_at=LATER)  # fmt: skip
    assert PowerBiCheck.objects.filter(programme="bridging").count() == 3
    with django_assert_max_num_queries(4):
        rows = power_bi.health()
    dirasa = next(p for p in rows if p["programme"] == "bridging")["checks"]
    assert [c["period"] for c in dirasa] == ["2026", "2025"]  # no old round
    assert [c["year_rule"] for c in dirasa] == ["start_year", "start_year"]
    client.force_login(admin_user)
    html = _health(client)
    assert OLD_ROUND not in html
    assert (
        "Rounds of 2026: those whose Bridging start date is in 2026; a round without one, the year in its name."
        in html
    )
    assert "Rounds of 2025: those whose Bridging start date is in 2025" in html
    # the view's rows per round: each round's year, from its name, or in no year
    assert "2026 (from its name)" in " ".join(html.split()) and "in no year" in html
    # the teachers' versions, in Data health's words
    assert "Teachers of the period&#x27;s rounds, at the schools chosen" in html or (
        "Teachers of the period's rounds, at the schools chosen" in html
    )
    # Compiler set to count the rounds marked current
    store_current = year_payload(block=power_bi_block("bridging", "2026", year_rounds=ROUNDS_2026,
                                                      period_rule="current_rounds"))  # fmt: skip
    power_bi.store("bridging", "2026", store_current)
    html = _health(client)
    assert "Rounds of 2026: those marked current in Compiler (setting current_rounds)." in html
    # a format-3 Dirasa row says nothing of a year
    assert power_bi._row(PowerBiCheck.objects.get(period=OLD_ROUND), None, {})["year_rule"] == ""


def test_the_card_admin_offers_years_and_says_an_old_round_must_be_typed_again(client, admin_user):
    store(old_round(), fetched_at=EARLY)
    store(year_payload("2025"), fetched_at=LATER)
    store(year_payload(), fetched_at=LATER)
    assert compared_periods("bridging") == ["2026", "2025"]
    assert power_bi.latest_period("bridging") == "2026"
    client.force_login(admin_user)
    changelist = reverse("admin:education_powerbicard_changelist")
    client.get(changelist)  # every card, with its default
    card = PowerBiCard.objects.get(programme="bridging", card="children")
    PowerBiCard.objects.filter(pk=card.pk).update(powerbi_value=5, powerbi_period=OLD_ROUND)
    url = reverse("admin:education_powerbicard_change", args=[card.pk])
    form = html_lib.unescape(client.get(url).content.decode())
    assert f"{OLD_ROUND} (not a period Data health compares)" in form
    assert '<option value="2026">2026</option>' in form and '<option value="2025">2025</option>' in form
    assert (
        f"Data health compares no period “{OLD_ROUND}” for this programme: choose one of the periods in the list "
        "(Dirasa: a year such as 2026)."
    ) in form
    assert "Dirasa: a year such as 2026; a round by its name in counts made before Compiler counted" in form
    assert "no such period: choose one" in client.get(changelist, {"programme": "bridging"}).content.decode()
    # typed again for the year: compared on Data health
    unique = power_bi.check("bridging", "2026").variants["children"]["unique"]
    assert (
        client.post(
            url, {"variant": "unique", "powerbi_value": str(unique), "powerbi_period": "2026"}
        ).status_code
        == 302
    )
    checks = {c["period"]: c for c in power_bi.health()[1]["checks"]}
    children = next(x for x in checks["2026"]["cards"] if x["card"] == "children")
    assert children["powerbi"] == unique == 6 and "unique" in [
        v["variant"] for v in children["versions"] if v["equal"]
    ]


# -------------------------------------------------------------------------------------- summary, Home, Ask
def test_the_summary_and_home_name_the_year(admin_user):
    store(old_round(), fetched_at=EARLY)
    store(year_payload("2025"), fetched_at=LATER)
    store(year_payload(), fetched_at=LATER)
    summary = {r["programme"]: r for r in power_bi.summary()}
    assert summary["bridging"]["period"] == "2026"
    rows = home._compiler_rows({"compiler": True}, False)
    dirasa = next(r for r in rows if r["label"] == "Dirasa (Bridging)")
    assert (dirasa["period"], dirasa["counted"]) == ("2026", 4)
    html = render_to_string("reports/parts/home_programmes.html", {"rows": rows, "selected": {}})
    assert "Dirasa (Bridging): 4 registrations, 2026" in html
    # readiness counts the periods the pages list, not the hidden rounds
    section = readiness.Section("context", "Context")
    readiness.context(section, datetime.date(2026, 10, 15))
    assert next(m.value for m in section.metrics if m.key == "education_periods") == 2


def test_ask_reads_the_year_and_one_round_of_it():
    from neurodb.assistant.hub_tools import HUB_TOOLS, education_figures

    store(old_round(), fetched_at=EARLY)
    store(year_payload("2025"), fetched_at=LATER)
    store(year_payload(), fetched_at=LATER)
    answer = education_figures("dirasa")
    assert (answer["period"], answer["periods"], answer["round"]) == ("2026", ["2026", "2025"], None)
    assert answer["rounds"] == ["Bridging Round 1 2026", "Bridging Round 2 2026"]
    assert answer["figures"]["kpis"]["children"] == 6
    second = education_figures("dirasa", round="Bridging Round 2 2026")
    assert (second["round"], second["figures"]["kpis"]["children"]) == ("Bridging Round 2 2026", 2)
    assert education_figures("dirasa", round=str(R1))["round"] == "Bridging Round 1 2026"  # by its id too
    # an old round's name as the period: the year holding it, that round chosen
    old = education_figures("dirasa", period=OLD_ROUND)
    assert (old["period"], old["round"]) == ("2025", OLD_ROUND)
    with pytest.raises(ToolInputError, match="its rounds are Bridging Round 1 2026, Bridging Round 2 2026"):
        education_figures("dirasa", round="Round 7")
    schema = HUB_TOOLS["education_figures"][2]["properties"]
    assert schema["round"]["description"] == "Dirasa only: one round of the year, by its name."
    assert "e.g. 2026" in schema["period"]["description"]


def test_ask_on_older_counts_takes_the_round_as_the_period():
    from neurodb.assistant.hub_tools import education_figures

    store(old_round())
    store(old_round(round_name="Bridging 2024-2025", start_date="2024-10-01"))
    answer = education_figures("dirasa", round="Bridging 2024-2025")
    assert (answer["period"], answer["rounds"], answer["round"]) == ("Bridging 2024-2025", None, None)
    # with a period given, the round is ignored
    assert education_figures("dirasa", period=OLD_ROUND, round="Bridging 2024-2025")["period"] == OLD_ROUND


# -------------------------------------------------------------------------------------------- country programme
def test_the_country_programme_offers_dirasa_years(framework):  # noqa: F811
    old = old_round()
    store(old, fetched_at=EARLY)
    payload = year_payload()
    store(payload, fetched_at=LATER)
    groups = dict(cpd_sources.choices(framework.reached))
    options = dict(groups["Compiler education (children enrolled)"])
    assert options["edu:bridging:2026"] == "Dirasa 2026 — children enrolled"
    assert f"edu:bridging:{OLD_ROUND}" not in options  # hidden: not offered
    # a link made to the old round keeps its value; the year's total is its unique children
    link = Link.objects.create(indicator=framework.reached, kind="education", year=2026,
                               education_programme="bridging", source_period=OLD_ROUND, label="Dirasa")  # fmt: skip
    assert cpd_services._education_values([link]) == {link.pk: 5.0}
    assert cpd_services.education_total(payload) == 6


# ---------------------------------------------------------------------------------------------------------- sync
def test_the_sync_asks_for_dirasa_years_and_keeps_them(settings):
    settings.COMPILER_EDUCATION_YEARS = 3
    years = [{"year": y, "counted": False, "format": None, "stale": True} for y in ("2026", "2025", "2024")]
    the_index = [{"programme": "bridging", "label": "Bridging (Dirasa)", "current_year": "2026", "years": years,
                  "format_version": 4}]  # fmt: skip
    assert targets(the_index) == [["bridging", "2026"], ["bridging", "2025"], ["bridging", "2024"]]
    compiler = Compiler(
        {("bridging", "2026"): year_payload(), ("bridging", "2025"): year_payload("2025")}, the_index
    )
    run = sync(triggered_by="test", client=compiler)
    assert run.status == SyncRun.Status.SUCCEEDED, run.error
    assert compiler.read == [("bridging", "2026"), ("bridging", "2025"), ("bridging", "2024")]
    assert run.details["pending"] == ["bridging 2024"]
    kept = {f.year: f.payload["format"] for f in EducationFigures.objects.filter(programme="bridging")}
    assert kept == {"2026": 4, "2025": 4}
    check = power_bi.check("bridging", "2026")
    assert check.base["period_rule"] == "start_year" and check.variants["children"]["unique"] == 6
    assert [p["name"] for p in check.periods] == ["Bridging Round 1 2026", "Bridging Round 2 2026"]
    assert services.periods("bridging") == ["2026", "2025"]


def test_the_teachers_profile_counts_the_years_rounds_only():
    payload = year_payload()
    payload["power_bi"]["related"] = {
        "vw_teacher_data": {"exists": True, "by": ["round_id", "is_active_current_round"], "measures": ["rows", "ids"],
                            "rows": [[R1, "yes", 4, 4], [R2, "yes", 1, 1], [9, "yes", 7, 7]], "truncated": False,
                            "rounds": [R1, R2]},
    }  # fmt: skip
    assert power_bi.variant_totals("bridging", payload)["teachers"] == {
        "round": 5,
        "round_at_schools_with_children": 5,
        "view_rows": 5,
    }


# ------------------------------------------------------------------------------------- fixes from review round 2
def _caveats(response) -> list[str]:
    found = re.findall(r'<p class="caveat" role="note"[^>]*>(.*?)</p>', response.content.decode(), re.S)
    return [" ".join(html_lib.unescape(c).split()) for c in found]


def test_the_rounds_line_names_registrations_of_other_rounds_or_none(client_viewer):
    """In view mode Compiler counts the view's rows of the year, whose registration may be of a round of another
    year (``other_year``) or of none (``no_round``, CONTRACT_PBI §9.3): the enrolment cube keeps the
    registration's own round, so the line adds them after the year's rounds and its counts add up to the
    registrations shown."""
    other = {"child": 17, "school": 300, "partner": 7, "governorate": 20, "sex": "Male", "nationality": 1,
             "disability": 1, "round": 9}  # fmt: skip
    regs = [*YEAR_REGISTRATIONS, other, dict(other, child=18, round=None)]
    store(dirasa_year_payload(regs, YEAR_TEACHERS, OUTREACH))
    response = page(client_viewer)
    assert response.context["data"]["cards"]["children"]["registrations"] == 9
    assert rounds_text(response) == (
        "Rounds: Bridging Round 1 2026 (from 12 Jan 2026, 5 registrations), "
        "Bridging Round 2 2026 (from 1 Sep 2026, 2 registrations), other rounds or no round (2 registrations)."
    )
    cache.clear()
    EducationFigures.objects.all().delete()
    store(dirasa_year_payload([*YEAR_REGISTRATIONS, other], YEAR_TEACHERS, OUTREACH))
    assert rounds_text(page(client_viewer)).endswith(", other rounds or no round (1 registration).")


def test_the_rounds_line_gives_no_count_when_compiler_could_not_count_the_registrations(client_viewer):
    payload = year_payload()
    payload["cubes"]["enrolment"] = {"error": "QueryCanceled: canceling statement due to statement timeout"}
    store(payload)
    response = page(client_viewer)
    assert "Compiler could not count some parts this time" in response.content.decode()
    # names and start dates only: never "no registration yet"
    assert rounds_text(response) == (
        "Rounds: Bridging Round 1 2026 (from 12 Jan 2026), Bridging Round 2 2026 (from 1 Sep 2026)."
    )
    # no round can be chosen: the page and Ask say why, never "no registrations or teachers"
    assert _caveats(page(client_viewer, round=str(R2))) == [
        "Compiler could not count the registrations this time, so no round of 2026 can be shown on its own."
    ]
    from neurodb.assistant.hub_tools import education_figures

    with pytest.raises(
        ToolInputError, match="Compiler could not count Dirasa 2026's registrations this time"
    ):
        education_figures("dirasa", round="Bridging Round 2 2026")
    from neurodb.education import dirasa

    rounds = [dict(ROUNDS_2026[0], id=33, name="Round 3 2026", start_date=None, year_from="name"),
              dict(ROUNDS_2026[0], id=34, name="Summer catch-up", start_date=None, year=None, year_from=None)]  # fmt: skip
    payload = dirasa_year_payload(YEAR_REGISTRATIONS, rounds=rounds)
    payload["cubes"]["enrolment"] = {"error": "QueryCanceled"}
    line = dirasa.rounds_line(Figures(payload))
    assert [r["registrations"] for r in line["rounds"]] == [None, None]
    text = " ".join(render_to_string("education/_rounds_line.html", {"line": line}).split())
    assert html_lib.unescape(text) == (
        '<p class="page-subtitle small" data-rounds>Rounds: Round 3 2026 (year taken from its name), '
        "Summer catch-up (no Bridging start date).</p>"
    )


ROUND_4 = {"id": 34, "name": "Round 4 2026", "start_date": "2026-11-02", "end_date": None, "current": False,
           "current_year": True, "year": 2026, "year_from": "start_date"}  # fmt: skip


def test_ask_says_a_round_of_the_year_without_figures_has_none():
    from neurodb.assistant.hub_tools import education_figures

    store(year_payload(rounds=[*ROUNDS_2026, ROUND_4]))
    with pytest.raises(ToolInputError, match="Round 4 2026 has no registrations or teachers in Dirasa 2026"):
        education_figures("dirasa", round="Round 4 2026")
    with pytest.raises(ToolInputError, match="Round 4 2026 has no registrations or teachers in Dirasa 2026"):
        education_figures("dirasa", period="Round 4 2026")  # a round's name as the period: the same
    # a round with figures is still answered; on Outreach (Compiler's Kobo outreach) no round applies
    assert education_figures("dirasa", round="Bridging Round 2 2026")["round"] == "Bridging Round 2 2026"
    outreach = education_figures("dirasa", tab="outreach", round="Round 4 2026")
    assert (outreach["tab"], outreach["round"]) == ("outreach", None)


def test_the_page_says_when_the_round_asked_for_cannot_be_shown(client_viewer):
    store(year_payload("2025"), fetched_at=EARLY)
    store(year_payload(rounds=[*ROUNDS_2026, ROUND_4]), fetched_at=LATER)
    # a round of the year without registrations or teachers (an old link naming it)
    response = page(client_viewer, round="Round 4 2026")
    assert response.context["period"] == "2026" and response.context["filters"] == {}
    assert _caveats(response) == [
        "Round 4 2026 has no registrations or teachers in 2026: the figures are those of every round of the year."
    ]
    # the same round chosen by its id
    assert _caveats(page(client_viewer, round="34")) == _caveats(response)
    # an old link naming a round no stored year lists: the newest year, and the page says so
    response = page(client_viewer, round="Bridging 2019-2020")
    assert response.context["period"] == "2026"
    assert _caveats(response) == [
        "No year shown here lists the round “Bridging 2019-2020”: the figures are those of every round of 2026."
    ]
    # a round of another year kept from the year chosen before
    response = page(client_viewer, year="2026", round="9")
    assert _caveats(response) == [
        "The round chosen is not a round of 2026: the figures are those of every round of the year."
    ]
    # a round shown, or no round asked for: nothing to say
    assert _caveats(page(client_viewer, round=str(R2))) == [] and _caveats(page(client_viewer)) == []
    assert _caveats(page(client_viewer, year="2025", round="9")) == []
    # Outreach has no round: only the Round filter's own "not applied" line
    assert _caveats(page(client_viewer, tab="outreach", round="34")) == []


def test_makani_opens_on_its_newest_year_whatever_its_rounds_start_dates(client_viewer):
    from .payloads import makani_payload
    from .test_dashboards import REGISTRATIONS, STAFF

    newest = makani_payload(REGISTRATIONS, STAFF, year="2026")
    newest["rounds"] = [{"id": 2, "name": "Round 1 2026", "start_date": None, "current": True}]  # nullable
    store(newest, compare=False)
    store(makani_payload(REGISTRATIONS[:1], STAFF, year="2025"), compare=False)  # its round from 15 Jan 2025
    assert power_bi.page_periods(["mscc"]) == {"mscc": ["2026", "2025"]}
    assert page_of(client_viewer, "education:makani").context["period"] == "2026"
    assert power_bi.latest_first("mscc", ["2025", "2026"]) == ["2026", "2025"]


def page_of(client, name, **params):
    response = client.get(reverse(name), params)
    assert response.status_code == 200
    return response


def test_dirasa_years_are_ordered_by_number_not_by_their_first_rounds_start():
    # Compiler set to count the rounds marked current: 2026 holds a round that started in Sep 2025, before the
    # first round of 2025 (Oct 2025)
    started_before = {"id": 40, "name": "Bridging 2025-2026 B", "start_date": "2025-09-01", "end_date": None,
                      "current": False, "current_year": True, "year": 2025, "year_from": "start_date"}  # fmt: skip
    store(year_payload(rounds=[started_before, *ROUNDS_2026], period_rule="current_rounds"))
    store(year_payload("2025"))
    assert power_bi.page_periods(["bridging"]) == {"bridging": ["2026", "2025"]}


def test_a_round_with_teachers_only_is_a_round_to_choose(client_viewer):
    round_3 = {"id": 33, "name": "Round 3 2026", "start_date": "2026-10-05", "end_date": None, "current": False,
               "current_year": True, "year": 2026, "year_from": "start_date"}  # fmt: skip
    teachers = [*YEAR_TEACHERS, {"school": 301, "sex": "Male", "trainings": [1], "round": 33}]
    store(dirasa_year_payload(YEAR_REGISTRATIONS, teachers, OUTREACH, rounds=[*ROUNDS_2026, round_3]))
    response = page(client_viewer)
    assert slicers(response)["round"]["options"] == [
        (str(R1), "Bridging Round 1 2026"),
        (str(R2), "Bridging Round 2 2026"),
        ("33", "Round 3 2026"),
    ]
    data = page(client_viewer, round="33").context["data"]
    assert (data["kpis"]["teachers"], data["cards"]["children"]["registrations"]) == (1, 0)
    assert rounds_text(response).endswith("Round 3 2026 (from 5 Oct 2026, no registration yet).")


def test_the_rounds_line_says_when_a_round_has_no_bridging_start_date_nor_a_year_in_its_name():
    from neurodb.education import dirasa

    # Compiler set to count the rounds marked current: a round flagged current, without a start date, whose name
    # holds no year
    rounds = [
        {"id": 35, "name": "Summer catch-up", "start_date": None, "end_date": None, "current": False,
         "current_year": True, "year": None, "year_from": None},
        {"id": 36, "name": "Autumn catch-up", "start_date": None, "end_date": None, "current": False,
         "current_year": True, "year": None, "year_from": None},
    ]  # fmt: skip
    regs = [dict(r, round=35) for r in BRIDGING[:2]]
    line = dirasa.rounds_line(Figures(dirasa_year_payload(regs, rounds=rounds, period_rule="current_rounds")))
    text = " ".join(render_to_string("education/_rounds_line.html", {"line": line}).split())
    assert html_lib.unescape(text) == (
        '<p class="page-subtitle small" data-rounds>Rounds marked current in Compiler: Summer catch-up (no Bridging '
        "start date, 2 registrations), Autumn catch-up (no Bridging start date, no registration yet).</p>"
    )


def test_the_country_programme_offers_only_the_periods_the_page_lists(framework):  # noqa: F811
    def offered():
        groups = dict(cpd_sources.choices(framework.reached))
        return dict(groups.get("Compiler education (children enrolled)", []))

    # an old round whose name begins with a year of the programme: hidden once Dirasa is counted by year
    early = "2026-2027 Bridging"
    store(old_round(round_name=early, start_date="2026-10-01"), fetched_at=EARLY)
    store(year_payload(), fetched_at=LATER)
    options = offered()
    assert "edu:bridging:2026" in options and f"edu:bridging:{early}" not in options
    # a rollback of Compiler to format 3: the round is fetched again and offered, the years are hidden
    EducationFigures.objects.filter(year=early).update(fetched_at=ROLLBACK)
    options = offered()
    assert f"edu:bridging:{early}" in options and "edu:bridging:2026" not in options


def test_data_health_names_the_rounds_of_a_related_view(client, admin_user):
    payload = year_payload()
    payload["power_bi"]["related"] = {
        "vw_teacher_data": {"exists": True, "by": ["round_id"], "measures": ["rows", "ids"],
                            "rows": [[R2, 1, 1], [R1, 4, 4], [77, 2, 2]], "truncated": False, "rounds": [R1, R2]},
    }  # fmt: skip
    store(payload)
    client.force_login(admin_user)
    html = " ".join(html_lib.unescape(_health(client)).split())
    assert (
        "vw_teacher_data</span>: 32 (Bridging Round 2 2026) / 1 / 1 · 31 (Bridging Round 1 2026) / 4 / 4 · 77 / 2 / 2 "
        '<span class="text-muted">(round_id / rows, ids; this period\'s rounds only)' in html
    )
