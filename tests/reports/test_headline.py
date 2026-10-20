"""The headline "children / beneficiaries reached": one main indicator per programme document, never the
sum of its indicators (``neurodb.datamart.headline``), on every page that shows it.

On ``overview_fixture`` (TODAY 1 July 2026): PD LEB/PD1 has the children indicator 100 (Akkar 300 in
Q1, Beirut 200 in Q2: 500 in 2026, target 1,000) and a teachers indicator. ``_second`` adds the case of
the demo database: a second children indicator of the same PD, so a child may be counted by both.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import SECTION_EDITOR
from neurodb.datamart import headline, monitoring
from neurodb.datamart import models as dm
from neurodb.datamart.models import IndicatorFlag
from neurodb.datamart.tags import tags_of
from neurodb.geo.models import Location
from neurodb.partnerships.models import PCA
from neurodb.reports import brief, overview
from tests.reports.overview_fixture import CHILDREN_TITLE, TODAY, make_overview_data

pytestmark = pytest.mark.django_db

ENROLLED = "# of children enrolled in non-formal education"
MATERIALS = "# of students receiving learning materials"


@pytest.fixture
def data(hierarchy):
    return make_overview_data()


def _indicator(
    data,
    source_id: int,
    title: str,
    reports: list[tuple[datetime.date, str, float]],
    *,
    target: float = 2000,
    section: str = "Child Protection",
    display_type: str = "number",
    period_method: str = "sum",
    status: str = "Accepted",
    pd: PCA | None = None,
):
    """A PD indicator with its accepted quarterly reports: (period end, place name, value)."""
    pd = pd or data["pd"]
    places = {p.name: p for p in Location.objects.filter(name__in={r[1] for r in reports} or {"Halba"})}
    first = places[reports[0][1]] if reports else Location.objects.get(name="Halba")
    dm.PDIndicator.objects.create(
        datamart_id=source_id, source_id=source_id, intervention=pd, pd_reference_number=pd.number,
        section_name=section, title=title, display_type=display_type,
        unit="percentage" if display_type == "percentage" else "number", baseline_numerator=Decimal(0),
        target_numerator=Decimal(target), target_denominator=Decimal(100) if display_type == "percentage" else None,
        location=first, location_name=first.name, location_pcode=first.p_code,
        tag_age_group=tags_of(title).age_group,
    )  # fmt: skip
    for n, (end, place_name, value) in enumerate(reports):
        place = places[place_name]
        dm.ReportedIndicator.objects.create(
            datamart_id=source_id * 10 + n, partner=pd.partner, intervention=pd, partner_name=pd.partner_name,
            pd_reference_number=pd.number, progress_report=f"PR-{source_id}-{n}", report_number=f"QPR{n + 1}",
            report_type="QPR", report_status=status, period_start=end - datetime.timedelta(days=89),
            period_end=end, due_date=end + datetime.timedelta(days=15),
            submission_date=end + datetime.timedelta(days=10), indicator=title, location=place.name,
            p_code=place.p_code, location_ref=place, achievement_in_period=str(value),
            disaggregation={"()": {"v": value, "d": 1, "c": value}}, calculation_across_locations="sum",
            calculation_across_periods=period_method, etools_indicator_id=str(source_id),
        )  # fmt: skip


def _second(data, value: float = 700):
    """The demo database's case: a second children indicator of LEB/PD1, reported in Beirut in Q2."""
    _indicator(data, 300, ENROLLED, [(datetime.date(2026, 6, 30), "Achrafieh", value)])


def build(reporting_year=None, **scope):
    return overview.build(
        overview.Scope(year=2026, reporting_year=reporting_year, today=TODAY, **scope), cache=False
    )


def _rows(pd_id: int):
    return monitoring.indicators(monitoring.Filters(pd_ids=[pd_id], scope="all"), TODAY)


# ------------------------------------------------------------------------------------ the rule
def test_two_children_indicators_of_one_pd_are_never_added(data):
    _second(data)
    impact = build()["impact"]
    # 700 (the larger), never 500 + 700 = 1,200
    assert impact["children_etools"] == 700
    assert impact["main_indicators"] == {"flagged": 0, "largest": 1}
    assert impact["monthly"]["etools"][:6] == [0, 0, 0, 0, 0, 700]
    assert impact["monthly"]["etools_to_date"][11] == 700
    assert sum(t["value"] for t in impact["children_by_tag"]) == 700
    assert [g["etools"] for g in impact["by_governorate"] if g["name"] == "Beirut"] == [700]
    assert all(g["etools"] == 0 for g in impact["by_governorate"] if g["name"] == "Akkar")


def test_the_unique_beneficiaries_indicator_wins_over_the_largest(data, admin_user):
    _second(data)
    headline.set_main(data["pd"], "100", "psychosocial support", admin_user)
    impact = build()["impact"]
    assert impact["children_etools"] == 500 and impact["main_indicators"] == {"flagged": 1, "largest": 0}
    by_gov = {g["name"]: g["etools"] for g in impact["by_governorate"]}
    assert (by_gov["Akkar"], by_gov["Beirut"]) == (300, 200)


def test_the_choice_is_one_per_pd_and_can_be_undone(data, admin_user):
    _second(data)
    pd = data["pd"]
    headline.set_main(pd, "100", "psychosocial support", admin_user)
    headline.set_main(pd, "300", ENROLLED, admin_user)
    flags = IndicatorFlag.objects.filter(intervention=pd, unique_beneficiaries=True)
    assert list(flags.values_list("key", flat=True)) == ["300"]
    # the flag set only for the role says nothing about children: the title rule still decides
    assert IndicatorFlag.objects.get(key="300").counts_children is None
    assert not IndicatorFlag.objects.filter(key="100").exists()  # it held nothing else: removed
    assert headline.clear_main(pd, admin_user) == 1
    assert headline.flagged_keys() == {}
    assert build()["impact"]["main_indicators"] == {"flagged": 0, "largest": 1}


def test_a_children_flag_is_kept_when_the_role_moves(data, admin_user):
    _second(data)
    IndicatorFlag.objects.create(source="etools", key="100", counts_children=True, note="confirmed by CP")
    headline.set_main(data["pd"], "100", "psychosocial support", admin_user)
    headline.set_main(data["pd"], "300", ENROLLED, admin_user)
    kept = IndicatorFlag.objects.get(key="100")
    assert (kept.counts_children, kept.unique_beneficiaries, kept.note) == (True, False, "confirmed by CP")


def test_the_database_refuses_two_unique_beneficiaries_indicators_for_one_pd(data):
    from django.db import IntegrityError, transaction

    IndicatorFlag.objects.create(
        source="etools", key="100", unique_beneficiaries=True, intervention=data["pd"]
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        IndicatorFlag.objects.create(
            source="etools", key="300", unique_beneficiaries=True, intervention=data["pd"]
        )


def test_a_percentage_is_never_a_main_indicator(data, admin_user):
    _indicator(data, 400, "% of children attending school", [(datetime.date(2026, 6, 30), "Halba", 90)],
               target=100, display_type="percentage")  # fmt: skip
    IndicatorFlag.objects.create(
        source="etools", key="400", unique_beneficiaries=True, intervention=data["pd"]
    )
    rows = _rows(data["pd"].id)
    mains = headline.choose(rows, is_children=headline.children_rule(), year=2026)
    assert mains[data["pd"].id].row.key == "100" and mains[data["pd"].id].rule == headline.LARGEST


def test_a_flagged_indicator_need_not_name_children(data, admin_user):
    """The teachers indicator is no children indicator by its title, but a section may make it the PD's
    unique beneficiaries indicator: it is then the PD's figure."""
    headline.set_main(data["pd"], "200", "teachers", admin_user)
    impact = build()["impact"]
    assert impact["children_etools"] == 0  # never reported
    assert impact["main_indicators"] == {"flagged": 0, "largest": 0}


def test_behind_target_is_one_indicator_never_a_sum(data):
    _second(data)
    rows = _rows(data["pd"].id)
    rule = headline.children_rule()
    mains = headline.choose(rows, is_children=rule, year=2026)
    found = headline.behind(rows, mains[data["pd"].id], rule)
    # the main indicator (700 of 2,000): 1,300 behind, never 1,300 + 500
    assert found[0] == 1300 and found[1].key == "300"
    others = [r for r in rows if r.key != "300"]
    assert headline.behind(others, mains[data["pd"].id], rule)[0] == 500  # the other children indicator


def test_a_max_indicator_is_not_added_month_by_month(data):
    """An indicator taking the latest of its periods (enrolment): January to December is its latest
    value, never Q1 + Q2."""
    dm.ReportedIndicator.objects.filter(etools_indicator_id="100").update(calculation_across_periods="latest")
    impact = build()["impact"]
    assert impact["children_etools"] == 200
    assert impact["monthly"]["etools_to_date"][2] == 300 and impact["monthly"]["etools_to_date"][5] == 200


# ------------------------------------------------------------------------------- sections, partners
def test_a_pd_counts_once_in_the_section_of_its_main_indicator(data, admin_user):
    _indicator(data, 300, ENROLLED, [(datetime.date(2026, 6, 30), "Achrafieh", 700)], section="Education")
    everywhere = build()["impact"]["children_etools"]
    protection = build(sections=["Child Protection"])["impact"]["children_etools"]
    education = build(sections=["Education"])["impact"]["children_etools"]
    assert (everywhere, protection, education) == (700, 0, 700)  # the sections add up to the country
    headline.set_main(data["pd"], "100", "psychosocial support", admin_user)
    assert build(sections=["Child Protection"])["impact"]["children_etools"] == 500
    assert build(sections=["Education"])["impact"]["children_etools"] == 0


def test_partner_section_and_country_totals_agree(data, reporting_year):
    _second(data)
    other = PCA.objects.create(
        etl_id="12", partner=data["partner"], partner_name=data["partner"].name, number="LEB/PD2",
        title="Learning", status="active", start=datetime.date(2026, 1, 1), end=datetime.date(2026, 12, 31),
    )  # fmt: skip
    _indicator(data, 500, "# of girls reached with learning support", [(datetime.date(2026, 3, 31), "Halba", 250)],
               pd=other)  # fmt: skip
    result = build(reporting_year)
    impact, delivery = result["impact"], result["delivery"]
    assert impact["children_etools"] == 700 + 250
    assert sum(p["children"] for p in delivery["by_partner"]) == impact["children_etools"]
    assert sum(s["children"] for s in impact["main_by_section"]) == impact["children_etools"]
    assert impact["main_indicators"] == {"flagged": 0, "largest": 2}


def test_achievement_by_section_is_a_median_of_indicators(data):
    _second(data)  # 700 of 2,000 = 35 %; the other: 500 of 1,000 = 50 %
    _indicator(
        data,
        600,
        "# of children receiving case management",
        [(datetime.date(2026, 6, 30), "Halba", 900)],
        target=600,
    )  # fmt: skip  (150 %, capped at 100)
    (section,) = build()["impact"]["by_section"]
    assert (section["indicators"], section["with_reached"], section["percent"]) == (3, 3, 50.0)
    assert section["target"] == 100.0 and section["unit"] == "%"
    assert "reached" not in section  # no sum of different indicators' values


# ---------------------------------------------------------------------------- the other pages
def test_the_brief_says_which_rule_its_children_follow(data, reporting_year):
    _second(data)
    result = brief.build(
        brief.Scope(year=2026, reporting_year=reporting_year, sections=[], today=TODAY), cache=False
    )
    tiles = {t["key"]: t for t in result["headline"]["tiles"]}
    assert tiles["children"]["label"] == "Children reached"
    assert "sum of each PD's main indicator" in tiles["children"]["hint"]
    assert tiles["achievement"]["label"] == "Achievement of PD targets (median)"
    (bullet,) = result["pace"]["bullets"]
    assert (bullet["achieved"], bullet["target"], bullet["programme_documents"]) == (700, 2000, 1)
    assert result["pace"]["projection"]["target"] == 2000
    assert "at least" not in result["text"] and "Children reached: 700" in result["text"]


def test_the_donor_page_reads_one_indicator_per_pd(data, reporting_year):
    from neurodb.donors import services
    from neurodb.donors.models import DonorAccount

    _second(data)
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    account = DonorAccount.objects.create(
        user=user, name="European Union", donors=["EU"], must_change_password=False
    )
    (pd,) = services.contribution(account, 2026, TODAY)["pds"]
    assert pd["children_whole"] == 700 and pd["indicator"] == ENROLLED
    assert (pd["indicator_cumulative"], pd["indicator_target"]) == (700, 2000)
    assert pd["indicator_rule"] == "largest single indicator"


def test_the_daily_review_counts_children_behind_on_one_indicator(data):
    from neurodb.review import checks

    _second(data)
    ctx = checks.Context(TODAY)
    inds = ctx.by_pd[data["pd"].id]
    assert ctx.children_behind(inds) == 1300  # 2,000 - 700 on the main indicator, never 1,300 + 500
    assert ctx.behind(inds)[1].title == ENROLLED


# ------------------------------------------------------------------------------------ the PD page
def _editor(section_name: str = "Child Protection") -> User:
    from neurodb.watch.models import SectionMatch

    section = Section.objects.get_or_create(name=section_name, defaults={"code": section_name[:3]})[0]
    SectionMatch.objects.get_or_create(
        etools_name=section_name, defaults={"section": section, "how": "exact", "confirmed": True}
    )
    user = User.objects.create_user(
        username=f"editor-{section.pk}", password="editor-pass-123456", section=section
    )
    user.groups.add(Group.objects.get(name=SECTION_EDITOR))
    return user


def test_the_pd_page_shows_the_main_indicator_and_who_may_choose_it(client, data, viewer, admin_user, roles):
    _second(data)
    url = reverse("reports:programme_detail", args=[data["pd"].pk])
    client.force_login(viewer)
    page = client.get(url).content.decode()
    assert "Largest single indicator" in page and "Make it the unique beneficiaries indicator" not in page
    client.force_login(admin_user)
    page = client.get(url).content.decode()
    assert page.count("Make it the unique beneficiaries indicator") == 3  # the three plain numbers


def test_a_section_editor_of_the_pd_sets_it_and_others_may_not(client, data, viewer, roles):
    _second(data)
    post = reverse("reports:pd_main_indicator", args=[data["pd"].pk])
    client.force_login(viewer)
    assert client.post(post, {"key": "100"}).status_code == 403
    client.force_login(_editor("Education"))
    assert client.post(post, {"key": "100"}).status_code == 403
    client.force_login(_editor("Child Protection"))
    response = client.post(post, {"key": "100"})
    assert response.status_code == 302 and response["Location"].endswith(
        reverse("reports:programme_detail", args=[data["pd"].pk])
    )
    assert headline.flagged_keys() == {data["pd"].pk: "100"}
    page = client.get(response["Location"]).content.decode()
    assert "Unique beneficiaries" in page and "No longer the unique beneficiaries indicator" in page
    assert build()["impact"]["children_etools"] == 500
    client.post(post, {"clear": "1"})
    assert headline.flagged_keys() == {}


def test_a_percentage_or_another_pds_indicator_is_refused(client, data, admin_user):
    _indicator(data, 400, "% of children attending school", [(datetime.date(2026, 6, 30), "Halba", 90)],
               target=100, display_type="percentage")  # fmt: skip
    client.force_login(admin_user)
    post = reverse("reports:pd_main_indicator", args=[data["pd"].pk])
    page = client.post(post, {"key": "400"}, follow=True).content.decode()
    assert "cannot count beneficiaries" in page
    page = client.post(post, {"key": "999"}, follow=True).content.decode()
    assert "Choose one of this programme document" in page
    assert not IndicatorFlag.objects.filter(unique_beneficiaries=True).exists()
    assert client.get(post).status_code == 405


def test_a_new_choice_shows_at_once_despite_the_caches(client, data, admin_user, settings):
    settings.DEBUG = False
    _second(data)
    scope = overview.Scope(year=2026, reporting_year=None, today=TODAY)
    assert overview.build(scope)["impact"]["children_etools"] == 700
    client.force_login(admin_user)
    client.post(reverse("reports:pd_main_indicator", args=[data["pd"].pk]), {"key": "100"})
    assert overview.build(scope)["impact"]["children_etools"] == 500
    client.post(reverse("reports:pd_main_indicator", args=[data["pd"].pk]), {"clear": "1"})
    assert overview.build(scope)["impact"]["children_etools"] == 700


def test_a_donor_account_cannot_choose(client, data):
    from neurodb.donors.models import DonorAccount

    user = User.objects.create_user(username="eu", password="donor-pass-123456")
    DonorAccount.objects.create(user=user, name="EU", donors=["EU"], must_change_password=False)
    client.force_login(user)
    response = client.post(reverse("reports:pd_main_indicator", args=[data["pd"].pk]), {"key": "100"})
    assert response.status_code in (302, 403) and not IndicatorFlag.objects.exists()


def test_the_admin_requires_the_programme_document(client, admin_user, data):
    from neurodb.datamart.admin import IndicatorFlagForm

    form = IndicatorFlagForm(
        data={"source": "etools", "key": "100", "unique_beneficiaries": "on", "counts_children": ""}
    )
    assert not form.is_valid() and "intervention" in form.errors
    form = IndicatorFlagForm(
        data={"source": "etools", "key": "100", "unique_beneficiaries": "on", "counts_children": "",
              "intervention": data["pd"].pk}
    )  # fmt: skip
    assert form.is_valid(), form.errors
    flag = form.save()
    assert flag.counts_children is None and flag.unique_beneficiaries
    form = IndicatorFlagForm(data={"source": "activityinfo", "key": "7", "unique_beneficiaries": "on",
                                   "counts_children": "false", "intervention": data["pd"].pk})  # fmt: skip
    assert not form.is_valid() and "unique_beneficiaries" in form.errors


# ------------------------------------------------------------------- one choice on every page (check)
def _over_two_years(data):
    """LEB/PD1 runs 2025-2026; the enrolment indicator reached 900 children in 2025 and none yet in 2026,
    the psychosocial one 500 in 2026: the larger to date (900) is not the larger this year (500)."""
    PCA.objects.filter(pk=data["pd"].pk).update(start=datetime.date(2025, 1, 1))
    data["pd"].refresh_from_db()
    _indicator(data, 300, ENROLLED, [(datetime.date(2025, 12, 31), "Achrafieh", 900)])


def test_the_main_indicator_of_a_year_is_the_same_on_every_page(data, reporting_year):
    from neurodb.donors import services
    from neurodb.donors.models import DonorAccount
    from neurodb.review import checks

    _over_two_years(data)
    pd = data["pd"]
    # the overview of each year counts the PD's largest children indicator of that year
    assert build(reporting_year)["impact"]["children_etools"] == 500
    assert (
        overview.build(overview.Scope(year=2025, reporting_year=None, today=TODAY), cache=False)["impact"][
            "children_etools"
        ]
        == 900
    )
    # the PD page and the daily review rank by the current year too: the same indicator as the overview
    main = headline.mains_of(_rows(pd.id), year=2026)[pd.id]
    assert (main.row.key, main.rule) == ("100", headline.LARGEST)
    assert headline.mains_of(_rows(pd.id), year=2025)[pd.id].row.key == "300"
    ctx = checks.Context(TODAY)
    assert ctx.mains[pd.id].row.key == "100"
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    account = DonorAccount.objects.create(user=user, name="EU", donors=["EU"], must_change_password=False)
    (row,) = services.contribution(account, 2026, TODAY)["pds"]
    assert row["indicator"] == CHILDREN_TITLE == main.row.title and row["children_whole"] == 500


def test_the_pd_page_names_the_main_indicator_of_the_current_year(client, data, admin_user):
    from neurodb.datamart import services

    _over_two_years(data)
    block = services.programme_datamart(data["pd"], today=TODAY)["monitoring"]
    assert block["main"].row.key == "100" and block["main_year"] == 2026  # not 300, the larger to date
    client.force_login(admin_user)
    page = client.get(reverse("reports:programme_detail", args=[data["pd"].pk])).content.decode()
    year = datetime.date.today().year
    assert f"children reached in {year} (its largest children indicator that year)" in page


def test_an_impossible_value_is_never_the_main_indicator_even_when_flagged(data, admin_user):
    """A section made a "people reached" indicator the PD's unique beneficiaries indicator, but its value
    (5 million against a target of 2,000) is a reporting error: it is set aside on every page, listed on
    the overview, and the PD's largest children indicator stands in."""
    _indicator(data, 700, "# of people reached with awareness sessions",
               [(datetime.date(2026, 6, 30), "Halba", 5_000_000)])  # fmt: skip
    headline.set_main(data["pd"], "700", "awareness", admin_user)
    impact = build()["impact"]
    assert impact["children_etools"] == 500  # never 5,000,000
    assert impact["main_indicators"] == {"flagged": 0, "largest": 1}
    assert [a["title"] for a in impact["implausible"]] == ["# of people reached with awareness sessions"]
    main = headline.mains_of(_rows(data["pd"].id), year=2026)[data["pd"].id]
    assert (main.row.key, main.rule) == ("100", headline.LARGEST)


def test_the_donor_page_puts_a_pd_in_the_section_of_its_main_indicator(data, reporting_year):
    """Two of the PD's three indicators are Child Protection's, but its main indicator (700 children) is
    Education's: on the overview the PD's money and children count in Education, so on the donor page
    too (its cost per child is set against Education's)."""
    from neurodb.donors import services
    from neurodb.donors.models import DonorAccount

    _indicator(data, 300, ENROLLED, [(datetime.date(2026, 6, 30), "Achrafieh", 700)], section="Education")
    sections = {s["section"]: s for s in build(reporting_year)["money"]["by_section"]}
    assert sections["Education"]["children"] == 700 and "Child Protection" not in sections
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    account = DonorAccount.objects.create(user=user, name="EU", donors=["EU"], must_change_password=False)
    (row,) = services.contribution(account, 2026, TODAY)["pds"]
    assert row["section"] == "Education"


def test_the_admin_checks_the_indicator_belongs_to_the_pd_and_counts_people(data):
    from neurodb.datamart.admin import IndicatorFlagForm

    _indicator(data, 400, "% of children attending school", [(datetime.date(2026, 6, 30), "Halba", 90)],
               target=100, display_type="percentage")  # fmt: skip
    base = {
        "source": "etools",
        "unique_beneficiaries": "on",
        "counts_children": "",
        "intervention": data["pd"].pk,
    }
    form = IndicatorFlagForm(data={**base, "key": "999"})  # no indicator of this PD
    assert not form.is_valid() and "key" in form.errors
    form = IndicatorFlagForm(data={**base, "key": "400"})  # a percentage
    assert not form.is_valid() and "unique_beneficiaries" in form.errors
    form = IndicatorFlagForm(data={**base, "key": "100"})
    assert form.is_valid(), form.errors
    flag = form.save()
    assert flag.label == CHILDREN_TITLE  # the indicator's title, for the list
    _second(data)
    form = IndicatorFlagForm(data={**base, "key": "300"})  # a second one for the same PD
    assert not form.is_valid()  # refused in the form, not by a server error


def test_the_headline_says_how_much_awaits_acceptance(data, reporting_year):
    """The enrolment indicator (the PD's main one) reached 300 in an accepted Q1 report and 400 more in a
    Q2 report still awaiting acceptance: children reached 700, of which 400 a send-back would take off. The
    Q2 report (the PD's, of every indicator: one report, one status) awaits acceptance as a whole."""
    _indicator(data, 300, ENROLLED, [(datetime.date(2026, 3, 31), "Achrafieh", 300),
                                     (datetime.date(2026, 6, 30), "Achrafieh", 400)])  # fmt: skip
    dm.ReportedIndicator.objects.filter(period_end__month=6).update(report_status="Submitted")
    impact = build()["impact"]
    assert (impact["children_etools"], impact["children_awaiting"]) == (700, 400)
    assert build(governorate="Beirut")["impact"]["children_awaiting"] is None  # not split by place
    result = brief.build(
        brief.Scope(year=2026, reporting_year=reporting_year, sections=[], today=TODAY), cache=False
    )
    tiles = {t["key"]: t for t in result["headline"]["tiles"]}
    assert "400 from reports awaiting acceptance" in tiles["children"]["hint"]
    # a "latest" indicator: the year's figure is the latest report (400); without it, 300: 100 awaits
    dm.ReportedIndicator.objects.filter(etools_indicator_id="300").update(calculation_across_periods="latest")
    impact = build()["impact"]
    # 100's 500 is larger now, its Q2 200 awaiting acceptance with the rest of the report
    assert (impact["children_etools"], impact["children_awaiting"]) == (500, 200)
    (row,) = [r for r in _rows(data["pd"].id) if r.key == "300"]
    assert row.result.awaiting_in(2026) == 100 and row.result.awaiting_in(2025) is None


def test_a_chosen_indicator_that_does_not_count_is_shown_and_can_be_undone(client, data, viewer, admin_user):
    """The unique beneficiaries indicator is chosen but its value cannot be true: the PD page says it is not
    counted (the largest single indicator stands in) and offers to undo the choice, instead of hiding it
    behind a "Make it..." button that would change nothing."""
    _indicator(data, 700, "# of people reached with awareness sessions",
               [(datetime.date(2026, 6, 30), "Halba", 5_000_000)])  # fmt: skip
    headline.set_main(data["pd"], "700", "awareness", admin_user)
    url = reverse("reports:programme_detail", args=[data["pd"].pk])
    client.force_login(viewer)
    page = client.get(url).content.decode()
    assert "Unique beneficiaries, not counted" in page and "too large to be true" in page
    assert "No longer the unique beneficiaries indicator" not in page  # a Viewer may not change it
    client.force_login(admin_user)
    page = client.get(url).content.decode()
    assert page.count("No longer the unique beneficiaries indicator") == 1
    assert page.count("Make it the unique beneficiaries indicator") == 2  # the two other plain numbers
    client.post(reverse("reports:pd_main_indicator", args=[data["pd"].pk]), {"clear": "1"})
    assert headline.flagged_keys() == {}
    page = client.get(url).content.decode()
    assert "not counted" not in page and page.count("Make it the unique beneficiaries indicator") == 3


def test_a_chosen_indicator_gone_from_etools_is_shown_and_can_be_removed(client, data, admin_user):
    _second(data)
    headline.set_main(data["pd"], "300", "enrolment", admin_user)
    dm.PDIndicator.objects.filter(source_id=300).delete()  # removed from the PD in eTools
    dm.ReportedIndicator.objects.filter(etools_indicator_id="300").delete()
    from neurodb.datamart import services

    block = services.programme_datamart(data["pd"], today=TODAY)
    assert block["monitoring"]["flag_unused"] == "missing"
    assert (block["monitoring"]["main"].row.key, block["monitoring"]["main"].rule) == (
        "100",
        headline.LARGEST,
    )
    client.force_login(admin_user)
    url = reverse("reports:programme_detail", args=[data["pd"].pk])
    page = client.get(url).content.decode()
    assert "no longer among its indicators in eTools" in page and "Remove that choice" in page
    client.post(reverse("reports:pd_main_indicator", args=[data["pd"].pk]), {"clear": "1"})
    assert headline.flagged_keys() == {}
    assert "no longer among its indicators" not in client.get(url).content.decode()


def test_a_pd_without_children_this_year_keeps_the_section_most_of_its_indicators_are_in(
    data, reporting_year
):
    """No children reached this year (its reports were sent back): the overview splits the PD's money by
    its indicators' sections (two Child Protection, one Education), so the donor page puts it in Child
    Protection, even though its main indicator (the largest target) is Education's."""
    from neurodb.donors import services
    from neurodb.donors.models import DonorAccount

    _indicator(data, 300, ENROLLED, [], section="Education", target=5000)
    dm.ReportedIndicator.objects.filter(etools_indicator_id="100").update(report_status="Sent back")
    result = build(reporting_year)
    assert result["impact"]["children_etools"] == 0
    reserved = {s["section"]: s["reserved"] for s in result["money"]["by_section"]}
    assert reserved["Child Protection"] > reserved["Education"]
    user = User.objects.create_user(username="eu-donor", password="donor-pass-123456")
    account = DonorAccount.objects.create(user=user, name="EU", donors=["EU"], must_change_password=False)
    (row,) = services.contribution(account, 2026, TODAY)["pds"]
    assert row["section"] == "Child Protection" and row["indicator"] == ENROLLED
