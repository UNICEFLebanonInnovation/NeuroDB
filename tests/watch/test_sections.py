"""eTools section names matched to NeuroDB sections for NeuroDB Watch: what is confirmed by itself, what
waits for an administrator, what goes to the administrators only, and the Needs attention lines."""

import datetime
from io import StringIO

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.urls import reverse

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import ADMIN, MANAGEMENT, SECTION_EDITOR, VIEWER, role_of
from neurodb.datamart import models as dm
from neurodb.donors.models import DonorAccount
from neurodb.partnerships.models import PCA
from neurodb.reports.models import SectionPlan
from neurodb.review.models import DailyReview, FindingAssignment, ReviewFinding
from neurodb.watch import sections
from neurodb.watch.models import SectionMatch

pytestmark = pytest.mark.django_db

How = SectionMatch.How


@pytest.fixture
def education(db):
    return Section.objects.create(name="Education", code="EDU")


@pytest.fixture
def wash(db):
    return Section.objects.create(name="WASH", code="WS")


@pytest.fixture
def health(db):
    return Section.objects.create(name="Health and Nutrition", code="HN")


@pytest.fixture
def named_everywhere(db):
    """One eTools name in each place NeuroDB keeps them."""
    PCA.objects.create(etl_id="1", number="LEB/PCA2026001/PD2026001", section_names=["Education", "WASH"])
    PCA.objects.create(etl_id="2", number="LEB/PCA2026001/PD2026002", section_names=None)
    dm.ActionPoint.objects.create(datamart_id=1, section="WASH / Water, Sanitation and Hygiene")
    dm.ReportedIndicator.objects.create(datamart_id=1, section="Social Policy")
    review = DailyReview.objects.create(date=datetime.date(2026, 10, 4), status="succeeded")
    ReviewFinding.objects.create(
        review=review, key="k", check_id="pd_ending", severity="warning", section="Health", title="t"
    )
    FindingAssignment.objects.create(key="k", section="HN")
    SectionPlan.objects.create(year=2026, section="Child Protection")


def _rows():
    return {row.etools_name: row for row in SectionMatch.objects.all()}


# ---------------------------------------------------------------------------- the names and the rules
def test_every_place_with_an_etools_name_is_read_once(named_everywhere):
    ReviewFinding.objects.create(
        review=DailyReview.objects.get(),
        key="k2",
        check_id="x",
        severity="info",
        section=" education ",
        title="t",
    )
    assert sections.etools_names() == [
        "Child Protection",
        "Education",
        "Health",
        "HN",
        "Social Policy",
        "WASH",
        "WASH / Water, Sanitation and Hygiene",
    ]


def test_an_exact_name_or_code_is_confirmed_by_itself(education, health):
    assert sections.match("Education") == sections.Match(education.pk, How.EXACT, True)
    assert sections.match("  education ") == sections.Match(education.pk, How.EXACT, True)
    assert sections.match("HN") == sections.Match(health.pk, How.CODE, True)


def test_a_contained_name_is_proposed_and_waits_either_way(wash):
    longer = Section.objects.create(name="Water, Sanitation and Hygiene / WASH")
    assert sections.match("WASH / Water, Sanitation and Hygiene") == sections.Match(
        wash.pk, How.CONTAINS, False
    )
    assert sections.match("Wash") == sections.Match(wash.pk, How.EXACT, True)
    Section.objects.filter(pk=wash.pk).update(name="Water and sanitation", code="WS")
    assert sections.match("WASH") == sections.Match(longer.pk, How.CONTAINS, False)  # the other way round


def test_a_two_letter_code_or_name_does_not_match_by_containment(education, health):
    Section.objects.filter(pk=education.pk).update(code="ED")
    assert sections.match("Education and Adolescents") == sections.Match(education.pk, How.CONTAINS, False)
    Section.objects.filter(pk=education.pk).update(name="Learning", code="ED")
    assert sections.match("Education and Adolescents") == sections.Match(None, How.NONE, False)  # "ed" inside
    assert sections.match("HE") == sections.Match(None, How.NONE, False)  # inside "Health and Nutrition"


def test_the_longest_contained_name_is_proposed(db):
    Section.objects.create(name="Gender")
    protection = Section.objects.create(name="Child Protection")
    assert sections.match("Child Protection and Gender").section_id == protection.pk


def test_a_name_two_sections_share_is_never_confirmed_by_itself(db):
    first = Section.objects.create(name="Education")
    Section.objects.create(name="Education")
    assert sections.match("Education") == sections.Match(first.pk, How.CONTAINS, False)


# ---------------------------------------------------------------------------- the stored map
def test_seeding_stores_every_name_with_its_match(named_everywhere, education, wash, health):
    counts = sections.seed()
    rows = _rows()
    assert counts == {
        "names": 7,
        "added": 7,
        "confirmed": 3,
        "to_confirm": 2,
        "unmatched": 2,
        "rematched": 0,
    }
    assert (rows["Education"].section, rows["Education"].how, rows["Education"].confirmed) == (
        education,
        How.EXACT,
        True,
    )
    assert (rows["HN"].section, rows["HN"].how, rows["HN"].confirmed) == (health, How.CODE, True)
    for name, section in (("WASH / Water, Sanitation and Hygiene", wash), ("Health", health)):
        assert (rows[name].section, rows[name].how, rows[name].confirmed) == (section, How.CONTAINS, False)
    for name in ("Social Policy", "Child Protection"):
        assert (rows[name].section, rows[name].how, rows[name].confirmed) == (None, How.NONE, False)


def test_a_rerun_keeps_a_manual_edit_and_adds_no_duplicate(named_everywhere, education, wash, health):
    sections.seed()
    SectionMatch.objects.filter(etools_name="Social Policy").update(
        section=education, how=How.MANUAL, confirmed=True, updated_by="admin"
    )
    SectionMatch.objects.filter(etools_name="Health").update(section=None, how=How.MANUAL, confirmed=False)
    ReviewFinding.objects.create(
        review=DailyReview.objects.get(),
        key="k2",
        check_id="x",
        severity="info",
        section="SOCIAL  policy",
        title="t",
    )
    for rematch in (False, True):
        counts = sections.seed(rematch=rematch)
        assert counts["added"] == 0 and SectionMatch.objects.count() == 7
    rows = _rows()
    assert (rows["Social Policy"].section, rows["Social Policy"].how) == (education, How.MANUAL)
    assert rows["Social Policy"].confirmed and rows["Social Policy"].updated_by == "admin"
    assert (rows["Health"].section, rows["Health"].how) == (None, How.MANUAL)


def test_a_new_name_is_added_and_older_rows_change_only_on_a_rematch(named_everywhere, education, wash):
    sections.seed()
    protection = Section.objects.create(name="Child Protection", code="CP")
    dm.ActionPoint.objects.create(datamart_id=2, section="CP")
    assert sections.seed()["added"] == 1
    rows = _rows()
    assert (rows["CP"].section, rows["CP"].how, rows["CP"].confirmed) == (protection, How.CODE, True)
    assert rows["Child Protection"].how == How.NONE  # new names only
    counts = sections.seed(rematch=True)
    rows = _rows()
    assert (rows["Child Protection"].section, rows["Child Protection"].how) == (protection, How.EXACT)
    assert rows["Child Protection"].confirmed and counts["rematched"] == 1


def test_a_rematch_leaves_what_an_administrator_confirmed(education, wash):
    SectionMatch.objects.create(etools_name="Wash and more", section=wash, how=How.CONTAINS, confirmed=True)
    SectionMatch.objects.create(
        etools_name="Operations", how=How.NONE, confirmed=True
    )  # "no section": settled
    SectionMatch.objects.create(etools_name="Gone", section=None, how=How.EXACT, confirmed=True)
    Section.objects.create(name="Operations")
    assert sections.seed(rematch=True)["rematched"] == 1  # only the row whose section was deleted
    rows = _rows()
    assert rows["Wash and more"].confirmed and rows["Wash and more"].section == wash
    assert rows["Operations"].section is None and rows["Operations"].confirmed
    assert (rows["Gone"].how, rows["Gone"].confirmed) == (How.NONE, False)


# ---------------------------------------------------------------------------- reading the map
def test_resolve_gives_confirmed_sections_only(named_everywhere, education, wash, health):
    sections.seed()
    assert sections.resolve("Education") == [education.pk]
    assert sections.resolve(" EDUCATION ") == [education.pk]
    assert sections.resolve("hn") == [health.pk]
    assert sections.resolve("WASH / Water, Sanitation and Hygiene") == []  # proposed, not confirmed
    assert sections.resolve("Social Policy") == []  # no section
    assert (
        sections.resolve("Never seen") == [] and sections.resolve("") == [] and sections.resolve(None) == []
    )
    SectionMatch.objects.filter(etools_name="WASH / Water, Sanitation and Hygiene").update(confirmed=True)
    mapping = sections.confirmed_map()
    assert sections.resolve("WASH / Water, Sanitation and Hygiene", mapping) == [wash.pk]


def test_resolve_names_gives_the_sections_and_the_names_left_to_the_administrators(
    named_everywhere, education, wash
):
    sections.seed()
    ids, unmapped = sections.resolve_names(["WASH", "Education", "Unknown", "", None, "unknown", "Education"])
    assert ids == sorted([education.pk, wash.pk])
    assert unmapped == ["Unknown"]
    assert sections.resolve_names([]) == ([], [])


def test_unmatched_sections_lists_sections_with_staff_that_no_name_points_to(education, wash, health):
    SectionMatch.objects.create(etools_name="Education", section=education, how=How.EXACT, confirmed=True)
    SectionMatch.objects.create(etools_name="WASH and more", section=wash, how=How.CONTAINS)  # not confirmed
    for name, section in (("edu", education), ("wash", wash), ("hn", health)):
        User.objects.create_user(username=name, password="x-123456789", section=section)
    empty = Section.objects.create(name="Nobody here")
    assert sections.unmatched_sections() == [health, wash]
    User.objects.filter(username="hn").update(is_active=False)
    donor = User.objects.get(username="wash")
    DonorAccount.objects.create(user=donor, name="EU", donors=["EU"], must_change_password=False)
    assert sections.unmatched_sections() == [] and empty not in sections.unmatched_sections()


# ---------------------------------------------------------------------------- the admin
@pytest.fixture
def client_admin(client, admin_user):
    client.force_login(admin_user)
    return client


def test_needs_attention_counts_the_names_without_a_confirmed_section(
    named_everywhere, education, wash, health
):
    sections.seed()
    (line,) = sections.needs_attention()
    assert line["key"] == "watch_sections_unconfirmed"
    assert line["text"].startswith("4 eTools section names have no confirmed NeuroDB section (")
    assert "and 1 more" in line["text"] and "administrators only" in line["text"]
    assert line["url"] == reverse("admin:watch_sectionmatch_changelist") + "?confirmed__exact=0"
    SectionMatch.objects.exclude(etools_name="Health").update(confirmed=True)
    (line,) = sections.needs_attention()
    assert line["text"].startswith("1 eTools section name has no confirmed NeuroDB section (Health)")


def test_needs_attention_lists_sections_whose_staff_get_nothing_once_the_map_exists(education, health):
    User.objects.create_user(username="hn", password="x-123456789", section=health)
    assert sections.needs_attention() == []  # no map yet: nothing to say
    SectionMatch.objects.create(etools_name="Education", section=education, how=How.EXACT, confirmed=True)
    (line,) = sections.needs_attention()
    assert line["key"] == "watch_sections_unmatched"
    assert line["text"] == (
        "1 NeuroDB section with staff has no confirmed eTools section name (Health and Nutrition): "
        "its staff get nothing from NeuroDB Watch."
    )


def test_needs_attention_is_quiet_while_the_watch_is_off(settings, named_everywhere):
    sections.seed()
    settings.WATCH_ENABLED = False
    assert sections.needs_attention() == []


def test_the_admin_home_shows_the_line(client_admin, named_everywhere, education, wash, health):
    sections.seed()
    html = client_admin.get(reverse("admin:index")).content.decode()
    assert "4 eTools section names have no confirmed NeuroDB section" in html
    assert "?confirmed__exact=0" in html


def test_role_of_and_users_without_a_role_are_unchanged_by_management(client_admin, roles):
    viewer = User.objects.create_user(username="v", password="x-123456789")
    viewer.groups.add(Group.objects.get(name=VIEWER))
    editor = User.objects.create_user(username="e", password="x-123456789")
    editor.groups.add(Group.objects.get(name=SECTION_EDITOR))
    chief = User.objects.create_user(username="c", password="x-123456789")
    chief.groups.add(Group.objects.get(name=ADMIN))
    User.objects.create_user(username="none", password="x-123456789")
    before = {user.username: role_of(user) for user in (viewer, editor, chief)}
    assert "1 active user has no role" in client_admin.get(reverse("admin:index")).content.decode()
    for user in (viewer, editor, chief):
        user.groups.add(Group.objects.get(name=MANAGEMENT))
    assert {user.username: role_of(user) for user in (viewer, editor, chief)} == before
    assert before == {"v": VIEWER, "e": SECTION_EDITOR, "c": ADMIN}
    assert "1 active user has no role" in client_admin.get(reverse("admin:index")).content.decode()


# ---------------------------------------------------------------------------- the command
def test_the_command_matches_by_hand_and_says_what_is_left(named_everywhere, education, wash, health):
    out = StringIO()
    call_command("map_watch_sections", stdout=out)
    text = out.getvalue()
    assert "7 seen, 7 added (3 confirmed, 2 to confirm, 2 without a section), 0 matched again" in text
    assert "to confirm: WASH / Water, Sanitation and Hygiene -> WASH (Name contains the other)" in text
    assert "to confirm: Social Policy -> no section" in text
    out = StringIO()
    call_command("map_watch_sections", "--rematch", stdout=out)
    assert "7 seen, 0 added" in out.getvalue() and SectionMatch.objects.count() == 7


def test_opening_the_list_adds_the_names_before_the_first_run(
    client_admin, named_everywhere, education, wash
):
    """The names are there to confirm before NeuroDB Watch has run once."""
    assert not SectionMatch.objects.exists()
    response = client_admin.get(reverse("admin:watch_sectionmatch_changelist"), follow=True)
    assert response.status_code == 200
    assert set(_rows()) == set(sections.etools_names())
    assert "eTools section names added" in response.content.decode()
    # opened again: nothing added twice, no message
    response = client_admin.get(reverse("admin:watch_sectionmatch_changelist"))
    assert SectionMatch.objects.count() == len(sections.etools_names())
    assert "eTools section names added" not in response.content.decode()


def test_opening_the_list_without_etools_data_says_where_the_names_come_from(client_admin):
    response = client_admin.get(reverse("admin:watch_sectionmatch_changelist"))
    assert response.status_code == 200 and not SectionMatch.objects.exists()
    assert "Run the eTools sync first" in response.content.decode()
