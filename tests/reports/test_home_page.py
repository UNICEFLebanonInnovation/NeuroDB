"""Home (SPEC_HOME §3.1, §9): who sees what, the empty states, ActivityInfo only where it counts, the scope every
link carries, and privacy.

Home is read as a browser shows it once every part is in (the test client assembles it, ``tests/conftest.py``).
The world is ``overview_fixture``'s (one Child Protection programme document, Amel, 500 children in 2026) with or
without the ActivityInfo ``hierarchy`` (2026 records of January and February: archived on the suite's day unless a
test sets ActivityInfo to "In use").
"""

from __future__ import annotations

import datetime
import json
import re
import zoneinfo

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import MANAGEMENT, SECTION_EDITOR
from neurodb.core.models import ScheduledJob
from neurodb.cpd.models import CountryProgramme
from neurodb.graph.models import Change
from neurodb.indicators.models import NeuroReport
from neurodb.knowledge.models import Document, DocumentFinding, DocumentReviewSettings, ReviewBatch, Topic
from neurodb.reports import home as home_service
from neurodb.reports import overview as overview_service
from neurodb.reports import views
from neurodb.review import services as review_services
from neurodb.review.models import DailyReview, FindingAssignment, ReviewFinding
from neurodb.wellbeing.models import Flag
from tests.reports.overview_fixture import TODAY, make_overview_data
from tests.web.parts import get_part, text

pytestmark = pytest.mark.django_db

HOME = "/"
BEIRUT = zoneinfo.ZoneInfo("Asia/Beirut")
OWNER = "Owner Placeholder"  # an assignment's owner: never on Home
WHO = "Who Placeholder"  # who the AI said should decide: never on Home


@pytest.fixture
def etools_world(reporting_year, settings):
    """eTools only: the reporting year exists but has no ActivityInfo database or record."""
    settings.AI_ASSISTANT_ENABLED = False
    return make_overview_data()


@pytest.fixture
def world(hierarchy, etools_world):
    return etools_world


@pytest.fixture
def reviewed(world):
    review_services.run(date=TODAY, today=TODAY)
    return world


def _user(username, *, role=None, section=None, staff=False, management=False, email=None):
    user = User.objects.create_user(
        username=username, email=email or f"{username}@example.org", password="x-pass-1234567", is_staff=staff
    )
    if role:
        user.groups.add(Group.objects.get(name=role))
    if management:
        user.groups.add(Group.objects.get(name=MANAGEMENT))
    if section is not None:
        user.section = section
        user.save()
    return user


def _home(client, user, query: str = ""):
    client.force_login(user)
    response = client.get(HOME + query)
    assert response.status_code == 200
    return response


def _main(response) -> str:
    """What the page shows in <main>, its own words (the sidebar and the header are the shell's)."""
    html = text(response)
    return html[html.index("<main") : html.index("</main>")]


def _words(html: str) -> str:
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    words = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    return re.sub(r" ([.,:;)])", r"\1", words)  # "page ." of "<a>page</a>." reads "page."


# ------------------------------------------------------------------------------------------------ who sees what
def test_a_viewer_with_a_section_lands_on_it(client, roles, reviewed, section):
    viewer = _user("edu-viewer", role="Viewer", section=section)
    page = _home(client, viewer)
    words = _words(_main(page))
    assert "Your section, Child Protection, by default · every section" in words
    assert "where your section stands in 2026" in words and "Where your section stands in 2026" in words
    assert "Needs a decision" not in words  # Administrators and Management only
    assert "Daily review ·" in words and "Open the daily review" in words
    assert "Your tasks" not in words  # Section editors and Administrators only
    assert page.context["data"]["scope"]["sections"] == ["Child Protection"]


def test_a_viewer_without_a_section_sees_the_country(client, roles, reviewed):
    viewer = _user("country-viewer", role="Viewer")
    words = _words(_main(_home(client, viewer)))
    assert "Thursday 15 October 2026 · Every section" in words and "· only" not in words
    assert "where the country stands in 2026" in words


def test_a_management_member_sees_the_country_and_decisions(client, roles, reviewed, section):
    member = _user("deputy", role="Viewer", section=section, management=True)
    page = _home(client, member)
    words = _words(_main(page))
    # the country by default, their section a click away
    assert "Every section · only Child Protection" in words
    assert "Needs a decision" in words and "All decisions: Management brief" in words
    assert page.context["data"]["scope"]["sections"] == []


def test_an_administrator_sees_decisions_tasks_and_setup_lines(client, admin_user, reviewed):
    page = _home(client, admin_user)
    words = _words(_main(page))
    assert "Needs a decision" in words
    # R2 sweep-3: the Country programme page has no upload; the cycle is added in the administration
    assert (
        "No country programme cycle yet: an administrator adds it in the administration; the Country programme "
        "page says how." in words
    )
    assert "uploads" not in words
    assert "Compiler is not connected" in words
    # a Viewer with staff status has nothing to set up: no muted line
    viewer = _user("staff-viewer", role="Viewer", staff=True)
    other = _words(_main(_home(client, viewer)))
    assert "No country programme cycle yet" not in other and "Compiler is not connected" not in other


def test_a_section_editor_sees_the_note_on_largest_indicators(client, roles, reviewed, section):
    editor = _user("cp-editor", role=SECTION_EDITOR, section=section)
    words = _words(_main(_home(client, editor)))
    assert "1 programme document is still counted by its largest indicator" in words
    viewer = _user("cp-viewer", role="Viewer", section=section)
    assert "still counted by its largest indicator" not in _words(_main(_home(client, viewer)))


# ------------------------------------------------------------------------------------------------- empty states
def test_watch_switched_off_draws_no_card_and_says_so(client, admin_user, reviewed, settings):
    settings.WATCH_ENABLED = False
    page = _home(client, admin_user)
    html = text(page)
    assert 'id="part-for-you"' not in html
    assert "NeuroDB is not checking at the moment: an administrator switched these checks off." in html
    from neurodb.watch import page as watch_page

    assert watch_page.card(admin_user) == {"shown": False, "reason": "off"}


def test_an_account_without_a_role_is_told_why_for_you_is_empty(client, roles, reviewed):
    nobody = _user("no-role")  # active, no donor account, no role group
    words = _words(_main(_home(client, nobody)))
    assert (
        "For you is for people with a role. Your account has none yet, so it opens with the Viewer's pages "
        "only. Ask an administrator to give you a role."
    ) in words
    from neurodb.watch import page as watch_page

    assert watch_page.card(nobody) == {"shown": False, "reason": "no_role"}


def test_a_viewer_without_a_section_is_told_what_for_you_shows(client, roles, reviewed):
    _late_check(days=0)  # checked this morning: no "has not checked" banner (it changes the card's title)
    viewer = _user("no-section", role="Viewer")
    words = _words(_main(_home(client, viewer)))
    assert "For you: nothing needs you today" in words
    assert "Your account has no section, so For you shows only whole-country points." in words
    admin = _user("an-admin", role="Administrator")
    assert "Your account has no section" not in _words(_main(_home(client, admin)))  # the country is theirs


@pytest.mark.parametrize("enabled", [True, False])
def test_a_review_that_never_ran_says_when_it_runs(client, admin_user, world, enabled):
    job = ScheduledJob.objects.get(command="daily_review")
    job.schedule, job.enabled, job.next_run_at = "15 5 * * *", enabled, None
    job.save()
    words = _words(_main(_home(client, admin_user)))
    assert "The daily review has not run yet." in words
    if enabled:
        assert "It runs every morning at 05:15." in words
    else:
        assert "It is switched off in Scheduled jobs." in words
    viewer = _user("plain-viewer", role="Viewer")
    plain = _words(_main(_home(client, viewer)))
    assert "The daily review has not run yet." in plain and "It runs every" not in plain  # staff only


def test_an_old_review_says_its_date(client, admin_user, reviewed):
    words = _words(_main(_home(client, admin_user)))
    assert "Latest daily review: 1 July 2026." in words and "Daily review · Wed 1 Jul" in words


def test_january_says_not_yet_and_october_never_does(client, admin_user, world, site_today):
    """Up to April of this year a scope with no children yet reads "Not yet" with a link to last year; in October a
    section without eTools children shows its 0 (never "Not yet")."""
    october = _main(_home(client, admin_user, "?section=Education"))
    assert '<div class="kpi__value">Not yet</div>' not in october
    site_today.set(datetime.date(2027, 1, 5))
    page = _home(client, admin_user)
    words = _words(_main(page))
    assert "Where the country stands in 2027" in words
    assert '<div class="kpi__value">Not yet</div>' in _main(page)
    assert "No partner report for 2027 yet: the first ones come after the first quarter." in words
    assert f'href="{reverse("reports:country_overview")}?year=2026"' in text(page)  # See 2026 →


# ------------------------------------------------------------------------------------- the first screen's caps
def _many_findings(review: DailyReview, checks: int = 5, each: int = 2):
    for c in range(checks):
        for n in range(each):
            ReviewFinding.objects.create(
                review=review,
                key=f"extra_{c}:{n}",
                check_id=f"extra_{c}",
                severity=ReviewFinding.Severity.WARNING if c % 2 else ReviewFinding.Severity.CRITICAL,
                section="Child Protection",
                title=f"Extra finding {c}.{n}",
                url="/programmes/",
                rank=100 + c * 10 + n,
            )


def test_the_first_screen_shows_at_most_two_decisions_and_two_review_rows(client, admin_user, reviewed):
    """Three each at first; both gave way to two so the key figures stay on the first screen at 1440 x 900 when
    For you has three points (SPEC_HOME §3.1's give-way order, measured on the x20 world)."""
    review = DailyReview.objects.get()
    _many_findings(review)
    response = _home(client, admin_user)
    tasks = text(response.parts["tasks"])
    decisions = tasks[tasks.index("Needs a decision") : tasks.index("Daily review ·")]
    digest = tasks[tasks.index("Daily review ·") :]
    assert decisions.count("<li>") == home_service.DECISIONS_SHOWN == 2
    assert digest.count("<li>") == home_service.REVIEW_ROWS == 2
    assert digest.count("<span>New:") + digest.count("<span>Still open:") <= 2
    assert len(re.findall(r'class="home-changed"', digest)) == 1
    changed = re.search(r'<p class="home-changed">(.*?)</p>', digest, re.S).group(1)
    assert changed.count("<span>") <= home_service.CHANGED_SHOWN
    # a finding listed under "Needs a decision" is not listed again in the digest
    shown = [row["key"] for row in home_service.decisions([], 2026)["rows"]]
    digest_keys = [r["key"] for r in review_services.digest([], exclude_keys=shown)["rows"]]
    assert not set(shown) & set(digest_keys)
    # groups: one row per check, "(and n more like it)" for each row standing for more than one finding
    rows = review_services.digest([], limit=home_service.REVIEW_ROWS, exclude_keys=shown)["rows"]
    assert len({row["check"] for row in rows}) == len(rows)
    for row in rows:
        assert (f"(and {row['more']} more like it)" in digest) == bool(row["more"])
    grouped = review_services.digest([], limit=50, exclude_keys=shown)["rows"]
    assert any(row["check"] == "extra_0" and row["more"] == 1 for row in grouped)  # two findings, one row


# ------------------------------------------------------------------------------------------- since {weekday}
def _change(name: str, when: datetime.datetime, sections: list[int] | None = None) -> Change:
    return Change.objects.create(
        detected_at=when,
        kind="programme_document",
        key=name,
        name=name,
        op=Change.Op.ADDED,
        sections=sections or [],
        notable=True,
    )


def test_since_friday_on_a_monday_includes_friday_evening(client, admin_user, world, site_today):
    site_today.set(datetime.date(2026, 10, 19))  # a Monday
    _change("Friday evening change", datetime.datetime(2026, 10, 16, 20, 30, tzinfo=BEIRUT))
    _change("Thursday change", datetime.datetime(2026, 10, 15, 21, 0, tzinfo=BEIRUT))
    since = text(_home(client, admin_user).parts["since"])
    assert "Since Friday" in since
    assert "Friday evening change" in since and "Thursday change" not in since


def test_since_yesterday_on_a_thursday(client, admin_user, world):
    _change("Wednesday change", datetime.datetime(2026, 10, 14, 10, 0, tzinfo=BEIRUT))
    since = text(_home(client, admin_user).parts["since"])
    assert "Since yesterday" in since and "Wednesday change" in since


def test_a_section_name_neurodb_does_not_know_never_shows_the_country_under_its_name(
    client, admin_user, world
):
    """eTools writes "WASH", NeuroDB's section is "Water, sanitation and hygiene" (code WSH): no NeuroDB section
    matches, so the block says "every section" rather than list the country's changes under "WASH"."""
    Section.objects.create(name="Water, sanitation and hygiene", code="WSH")
    education = Section.objects.create(name="Education", code="EDU")
    _change("An education change", datetime.datetime(2026, 10, 14, 10, 0, tzinfo=BEIRUT), [education.pk])
    since = text(_home(client, admin_user, "?section=WASH").parts["since"])
    hint = re.search(r'<span class="panel__hint">(.*?)</span>', since, re.S).group(1)
    assert hint.strip() == "every section"
    assert home_service.section_ids(["WASH"]) == (None, False)
    assert home_service.section_ids(["Education"]) == ([education.pk], True)
    assert home_service.section_ids([]) == (None, True)
    # the Country overview's What's new card: nothing rather than the country's changes under "WASH"
    assert views._whats_new(["WASH"]) == []


# ---------------------------------------------------------------------------------------- programmes and sources
@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (2022, 2025, "Country programme 2022–2025: ended in 2025"),
        (2027, 2029, "Country programme 2027–2029: starts in 2027"),
    ],
)
def test_a_country_programme_cycle_out_of_today(client, admin_user, world, start, end, expected):
    CountryProgramme.objects.create(name="Cycle", start_year=start, end_year=end, current=True)
    words = _words(text(_home(client, admin_user).parts["programmes"]))
    assert expected in words
    if end < 2026:  # R2 sweep-3: the cycle is added in the administration, not on the Country programme page
        assert (
            "An administrator adds the next cycle in the administration (Country programme cycles)." in words
        )
    assert "uploads" not in words


def test_activityinfo_in_use_and_archived_lines(client, admin_user, world, activityinfo_in_use):
    words = _words(text(_home(client, admin_user).parts["programmes"]))
    assert "ActivityInfo 2026: 2 results tracked against targets" in words
    assert f"{reverse('reports:databases')}?year=2026" in text(_home(client, admin_user).parts["programmes"])


def test_activityinfo_archived_line(client, admin_user, world, activityinfo_archived):
    words = _words(text(_home(client, admin_user).parts["programmes"]))
    assert "ActivityInfo archive 2026: every database and report, kept as it was" in words


# ----------------------------------------------------------------------------------------- the scope of links
def test_every_link_to_the_pages_carries_homes_scope(client, roles, reviewed, section):
    member = _user("deputy2", role="Viewer", section=section, management=True)
    html = text(_home(client, member))
    overview = reverse("reports:country_overview")
    for url in (overview, reverse("reports:brief"), reverse("reports:daily_review")):
        assert f'href="{url}?year=2026"' in html, url  # every section, written out
    viewer = _user("cp-viewer2", role="Viewer", section=section)
    html = text(_home(client, viewer))
    for url in (overview, reverse("reports:brief"), reverse("reports:daily_review")):
        assert f'href="{url}?section=Child+Protection"' in html, url


def test_a_management_member_sees_the_same_figures_on_home_and_the_overview(client, roles, reviewed, section):
    """Home's link writes the scope out: the Country overview it opens never applies a first-load default of its
    own, so the children reached are the same on both pages (here every section, though the member has one)."""
    member = _user("deputy3", role="Viewer", section=section, management=True)
    home = _home(client, member)
    home_children = home.context["data"]["impact"]["children_reached"]
    overview = client.get(reverse("reports:country_overview") + "?year=2026")
    assert overview.context["data"]["impact"]["children_reached"] == home_children
    assert overview.context["selected"]["section"] == []
    # the build's key is the same: one stored build serves both pages
    scope = overview_service.Scope(year=2026, reporting_year=None, sections=[], governorate="")
    assert overview_service._cache_key(scope, "stamp").startswith("overview:v15:2026:")


# ------------------------------------------------------------------------------------- ActivityInfo where it counts
def test_no_activityinfo_on_home_or_the_overview_without_records_and_not_in_use(
    client, admin_user, etools_world
):
    home = _home(client, admin_user)
    assert "ActivityInfo" not in _main(home), "Home names ActivityInfo in a year without its records"
    overview = client.get(reverse("reports:country_overview"))
    assert "ActivityInfo" not in _main(overview)
    figures = get_part(client, reverse("reports:country_overview"), "figures")
    payload = json.loads(
        re.search(r'id="overview-figures-data"[^>]*>(.*?)</script>', text(figures), re.S).group(1)
    )
    assert "ActivityInfo" not in json.dumps(payload)
    assert set(payload["impact"]["monthly"]["series"]) == {"eTools"}


def test_no_activityinfo_zero_in_a_year_without_hpm_children_masters(client, admin_user, world):
    """2026 has ActivityInfo records, but no HPM report counts children: no "ActivityInfo 0" anywhere."""
    NeuroReport.objects.all().delete()
    for page in (_home(client, admin_user), client.get(reverse("reports:country_overview"))):
        main = _words(_main(page))
        assert "ActivityInfo 0" not in main and "ActivityInfo (" not in main
    data = client.get(reverse("reports:country_overview")).context["data"]
    assert data["impact"]["activityinfo_used"] is False and data["impact"]["children_activityinfo"] == 0


def test_the_activityinfo_series_ends_at_its_last_month(client, admin_user, world):
    client.force_login(admin_user)
    figures = get_part(client, reverse("reports:country_overview"), "figures")
    payload = json.loads(
        re.search(r'id="overview-figures-data"[^>]*>(.*?)</script>', text(figures), re.S).group(1)
    )
    monthly = payload["impact"]["monthly"]
    assert monthly["labels"][-1] == "Oct"  # this year: the months to come are left off
    ai = monthly["series"]["ActivityInfo"]
    assert ai[:2] == [150.0, 350.0] and ai[2:] == [None] * 8  # records of January and February, then nothing
    assert any(v is not None for v in monthly["series"]["eTools"][2:])
    kpis = _words(text(get_part(client, reverse("reports:country_overview"), "kpis")))
    assert "ActivityInfo 500 (Jan–Feb): the larger of the two" in kpis


# --------------------------------------------------------------------------------------------------- privacy
def _private_world(review: DailyReview, owner_user: User):
    finding = review.findings.filter(severity__in=("critical", "warning")).first()
    FindingAssignment.objects.create(
        key=finding.key, title=finding.title, owner=OWNER, status=FindingAssignment.Status.ASSIGNED,
        due_date=datetime.date(2026, 11, 2), updated_by=owner_user.username,
    )  # fmt: skip
    review.decisions = [
        {"decision": "Extend the programme document", "why": "It ends soon.", "who": WHO, "urgency": "today",
         "finding_keys": [finding.key]},
    ]  # fmt: skip
    review.decided_by = "a-model"
    review.save()


@pytest.mark.parametrize("state", ["in_use", "archived"])
def test_home_never_shows_owners_who_or_other_peoples_emails(client, admin_user, reviewed, state, request):
    request.getfixturevalue(f"activityinfo_{state}")
    owner = _user("owner-person", role="Viewer", email="owner.person@example.org")
    _private_world(DailyReview.objects.get(), owner)
    html = text(_home(client, admin_user))
    assert "Extend the programme document" in html  # the decision is there ...
    assert "due 2026-11-02" in html or "Nov" in html or "2026-11-02" in html
    for secret in (OWNER, WHO, "owner.person@example.org", "owner-person", "admin_findingassignment"):
        assert secret not in html, secret  # ... never who owns it, who should decide, or the Assign link


@pytest.mark.parametrize("state", ["in_use", "archived"])
def test_wellbeing_flags_reach_only_administrators_and_education_editors(
    client, roles, world, state, request
):
    request.getfixturevalue(f"activityinfo_{state}")
    for n in range(3):
        Flag.objects.create(
            bma_id=n + 1, registration=1000 + n, kind="K1", kind_label="Absent", status=Flag.OPEN,
            reason="Absent three weeks", opened_on=datetime.date(2026, 10, 1),
        )  # fmt: skip
    education = Section.objects.create(name="Education", code="EDU")
    protection = Section.objects.get_or_create(name="Child Protection", defaults={"code": "CP"})[0]
    line = "3 Makani wellbeing flags to follow up"
    admin = _user("w-admin", role="Administrator")
    assert line in _words(_main(_home(client, admin)))
    assert line in _words(_main(_home(client, _user("w-edu", role=SECTION_EDITOR, section=education))))
    assert line not in _words(_main(_home(client, _user("w-cp", role=SECTION_EDITOR, section=protection))))
    assert line not in _words(_main(_home(client, _user("w-viewer", role="Viewer", section=education))))


@pytest.mark.parametrize("state", ["in_use", "archived"])
def test_document_review_counts_only_the_editors_section(client, roles, world, state, request):
    request.getfixturevalue(f"activityinfo_{state}")
    settings_row = DocumentReviewSettings.load()
    settings_row.enabled = True
    settings_row.save()
    batch = ReviewBatch.objects.create(name="Annual reports")
    protection = Section.objects.get_or_create(name="Child Protection", defaults={"code": "CP"})[0]
    education = Section.objects.create(name="Education", code="EDU")
    for title, section, findings in (("CP report", protection, 2), ("EDU report", education, 3)):
        document = Document.objects.create(
            title=title, text="x", status=Document.Status.READY, review_batch=batch,
            review_status=Document.ReviewStatus.DONE, section=section,
        )  # fmt: skip
        for n in range(findings):
            DocumentFinding.objects.create(
                document=document, topic=Topic.other(), text=f"{title} finding {n}", category="challenge"
            )
    editor = _user("d-cp", role=SECTION_EDITOR, section=protection)
    assert "2 document review findings not reviewed yet" in _words(_main(_home(client, editor)))
    admin = _user("d-admin", role="Administrator")
    assert "5 document review findings not reviewed yet" in _words(_main(_home(client, admin)))
    no_section = _user("d-none", role=SECTION_EDITOR)
    assert "document review findings" not in _words(_main(_home(client, no_section)))
    viewer = _user("d-viewer", role="Viewer", section=protection)
    assert "document review findings" not in _words(_main(_home(client, viewer)))


def test_hpm_reports_to_comment_on_only_while_activityinfo_is_in_use(client, roles, world, section, request):
    editor = _user("h-editor", role=SECTION_EDITOR, section=section)
    assert "HPM report" not in _words(_main(_home(client, editor)))  # archived: no line
    request.getfixturevalue("activityinfo_in_use")
    assert "1 HPM report of 2026 to comment on" in _words(_main(_home(client, editor)))


# ------------------------------------------------------------------------------------- round 1 review (R1)
def _contrast(foreground: str, background: str = "#ffffff") -> float:
    """WCAG 2 contrast ratio of two #rrggbb colours."""

    def luminance(colour: str) -> float:
        channels = [int(colour.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _home_css() -> str:
    from pathlib import Path

    from django.conf import settings

    return (Path(settings.BASE_DIR) / "neurodb/web/static/css/home.css").read_text()


def _late_check(days: int = 3):
    from django.utils import timezone

    from neurodb.core.models import SyncRun

    finished = timezone.now() - datetime.timedelta(days=days)
    return SyncRun.objects.create(
        job=SyncRun.Job.WATCH, target="daily", status=SyncRun.Status.SUCCEEDED,
        started_at=finished - datetime.timedelta(minutes=1), finished_at=finished,
    )  # fmt: skip


def test_a_late_check_is_a_readable_warning_and_never_says_nothing_needs_you(client, roles, reviewed):
    """R1 browser-1: the morning check last ran on Monday. The card's banner is a warning box (the For you page's,
    readable in the light theme), never the bare yellow text; its title does not say "nothing needs you today"
    while nothing has been checked."""
    _late_check()
    viewer = _user("late-viewer", role="Viewer")
    card = text(_home(client, viewer).parts["for-you"])
    assert "NeuroDB has not checked since Monday." in card
    assert "text-warning" not in card and 'class="alert alert-warning' in card
    assert "nothing needs you today" not in card
    assert re.search(r'id="watch-overview-title">.*?For you\s*</h3>', card, re.S)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_warning_count_of_the_daily_review_is_readable_in_both_themes(theme):
    """R1 browser-1 / a11y A11Y-3 (Home's part): "141 Warning" was #b76e00 on white (4.00:1). It is drawn in
    app.css's warning text token, which each theme defines (#9a5b00 light, #f0b04a dark)."""
    from tests.web.test_status_contrast import THEMES, _colour, _ratio

    assert re.search(
        r"^\.home-counts__item--warning \{ color: var\(--nd-warning-text\); \}", _home_css(), re.M
    )
    tokens = THEMES[theme]
    colour = _colour("var(--nd-warning-text)", tokens)
    for background in ("--nd-surface", "--nd-bg"):
        assert _ratio(colour, _colour(f"var({background})", tokens)) >= 4.5, (theme, background)


def test_the_review_rows_say_their_severity_in_words(client, admin_user, reviewed):
    """R1 a11y A11Y-2: a row's severity is a worded pill, as the decisions', never a dot told apart by colour."""
    tasks = text(_home(client, admin_user).parts["tasks"])
    digest = tasks[tasks.index("Daily review ·") :]
    rows = re.findall(r"<li>(.*?)</li>", digest, re.S)
    assert rows
    for row in rows:
        assert re.search(
            r'<span class="pill pill--sm pill--(danger|warning|info)">(Critical|Warning|To note)</span>', row
        ), row
        assert "review-finding__sev" not in row


def test_the_for_you_points_say_their_severity_in_words():
    from django.template.loader import render_to_string

    top = [{"title": "Report due Friday", "reason": "", "severity": "critical", "severity_label": "Critical",
            "anchor": "watch-1"}]  # fmt: skip
    html = render_to_string("watch/partials/_card.html", {"shown": True, "count": 1, "top": top})
    assert re.search(r'<span class="pill pill--sm pill--danger">Critical</span>', html)
    assert 'role="img"' not in html


@pytest.mark.parametrize("fmm", [True, False])
def test_the_also_line_links_carry_homes_sections(client, admin_user, reviewed, settings, fmm):
    """R1 browser-2, changed for R2 browser-3 (the section's visits linked to Monitoring insights, which counts a
    section's visits another way, and "these partners" named no one): with a section chosen, the action points link
    carries it (the page then lists the same open and overdue points); the field monitoring visits say what they
    count, the visits to partners with a programme document in the sections, and are not a link; nor is the
    third-party pair, whose page has no section filter. With every section, each figure links to its page."""
    settings.FMM_ENABLED = fmm
    standing = text(_home(client, admin_user, "?section=Child+Protection").parts["standing"])
    also = standing[standing.index("Also in 2026") : standing.index("</p>", standing.index("Also in 2026"))]
    points = reverse("reports:action_points")
    assert f'href="{points}?section=Child+Protection&amp;overdue=1&amp;priority=1"' in also
    words = _words(also)
    assert re.search(
        r"\d+ field monitoring visits? to partners with a programme document in Child Protection", words
    ), words
    assert "these partners" not in words
    monitoring = reverse("reports:monitoring")
    assert f'href="{monitoring}?' not in also  # the field monitoring page shows the whole country
    assert (
        f'href="{reverse("fmm:dashboard")}' not in also
    )  # Monitoring insights counts a section's visits otherwise
    two = text(_home(client, admin_user, "?section=Child+Protection&section=Education").parts["standing"])
    assert "to partners with a programme document in Child Protection, Education" in _words(two)
    country = text(_home(client, admin_user, "?section=").parts["standing"])
    also = country[country.index("Also in 2026") :]
    assert f'href="{points}?overdue=1&amp;priority=1"' in also
    assert f'href="{monitoring}?year=2026"' in also and "partners" not in _words(also.split("</p>")[0])
    if fmm:
        assert f'href="{reverse("fmm:dashboard")}?section=&amp;year=2026"' in also


@pytest.fixture
def activityinfo_waiting(reporting_year, database, settings, activityinfo_in_use):
    """ActivityInfo set "In use" while the year ticked current (2026) has a database and no record yet."""
    settings.AI_ASSISTANT_ENABLED = False
    return make_overview_data()


def test_activityinfo_in_use_without_this_years_records_is_never_called_an_archive(
    client, admin_user, activityinfo_waiting
):
    """R1 spec_state-2: set "In use", 2026 has databases but no record yet: Home draws an in-use line (the menu
    says "Monthly results (ActivityInfo)" too), never "ActivityInfo archive"."""
    programmes = text(_home(client, admin_user).parts["programmes"])
    words = _words(programmes)
    assert "ActivityInfo archive" not in words
    assert "ActivityInfo 2026: in use, waiting for its first records" in words
    assert f'href="{reverse("reports:databases")}?year=2026"' in programmes


def test_activityinfo_in_use_with_last_years_records_ticked_current_names_that_year(
    client, admin_user, settings, activityinfo_in_use
):
    """R2 fixes-2: set "In use" while the reporting year ticked current is 2025, with a year of records, on 15
    October 2026 (the import serves no year: 2026 has none). Home names 2025 as any ActivityInfo year, never "in
    use, waiting for its first records" (the menu lists 2025 and its database)."""
    from neurodb.facts.models import ActivityReportNew
    from neurodb.indicators.models import Database, MasterIndicator, ReportingYear
    from neurodb.indicators.services import activityinfo

    settings.AI_ASSISTANT_ENABLED = False
    make_overview_data()
    ry25 = ReportingYear.objects.create(name="2025", year="2025", current=True)
    db25 = Database.objects.create(
        ai_id=202518, db_id="x2025", name="CP 2025", label="CP", username="", password="", reporting_year=ry25,
        is_funded_by_unicef=False,
    )  # fmt: skip
    MasterIndicator.objects.create(database=db25, name="Children", awp_code="1", aggregation_method="SUM")
    for month in range(1, 13):
        ActivityReportNew.objects.create(
            dbase=db25, database_ai_id="202518", indicator_id="i_a", indicator_value=5,
            month_name=f"2025-{month:02d}-01", partner_label="P",
        )  # fmt: skip
    activityinfo.invalidate()
    state = activityinfo.state()
    assert state.in_use and state.serving is None and state.current_name == "2025" and 2025 in state.years
    programmes = text(_home(client, admin_user).parts["programmes"])
    words = _words(programmes)
    assert "waiting for its first records" not in words and "ActivityInfo archive" not in words
    assert "ActivityInfo 2025: 1 result tracked against targets" in words
    assert f'href="{reverse("reports:databases")}?year=2025"' in programmes


def test_activityinfo_in_use_with_records_and_no_result_has_no_zero(
    client, admin_user, world, activityinfo_in_use
):
    """R1 time_roles-F5: no "0 results tracked against targets"."""
    from neurodb.indicators.models import MasterIndicator

    MasterIndicator.objects.update(is_active=False)
    words = _words(text(_home(client, admin_user).parts["programmes"]))
    assert "0 results" not in words
    assert "ActivityInfo 2026: records imported" in words


def test_one_result_tracked_is_singular(client, admin_user, world, activityinfo_in_use):
    """R1 time_roles-F7."""
    from neurodb.indicators.models import MasterIndicator

    MasterIndicator.objects.filter(name="Share of girls").update(is_active=False)
    words = _words(text(_home(client, admin_user).parts["programmes"]))
    assert "ActivityInfo 2026: 1 result tracked against targets" in words


def test_a_server_without_etools_data_says_so_instead_of_zero_tiles(client, admin_user, db):
    """R1 time_roles-F3: a fresh install (the eTools Datamart sync never ran): one line, never "0 partners",
    "$0" or "0 of 0 third-party visits"."""
    standing = _words(text(_home(client, admin_user).parts["standing"]))
    assert "No eTools data yet: the eTools Datamart sync has not run on this server." in standing
    assert "All the figures: Country overview" in standing
    for zero in ("0 partners", "$0", "0 of 0", "Children reached", "Also in 2026"):
        assert zero not in standing, zero


def test_no_third_party_visit_planned_draws_no_zero_pair(client, admin_user, db):
    """R1 time_roles-F3 (second half): the sync ran, no visit is planned: no "0 of 0 third-party visits done"."""
    from django.utils import timezone

    from neurodb.core.models import SyncRun

    now = timezone.now()
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART, status=SyncRun.Status.SUCCEEDED, started_at=now, finished_at=now
    )
    standing = _words(text(_home(client, admin_user).parts["standing"]))
    assert "Children reached" in standing and "No eTools data yet" not in standing  # true zeros: tiles
    assert "third-party visits" not in standing


@pytest.mark.parametrize(
    ("day", "phrase"), [(datetime.date(2026, 10, 15), "yesterday"), (datetime.date(2026, 10, 19), "Friday")]
)
def test_since_says_one_phrase_in_its_heading_and_its_empty_line(
    client, admin_user, world, site_today, day, phrase
):
    """R1 browser-4: "Since yesterday" over "Nothing notable changed since Wednesday." named one window twice."""
    site_today.set(day)
    since = _words(text(_home(client, admin_user).parts["since"]))
    assert f"Since {phrase}" in since and f"Nothing notable changed since {phrase}." in since


def test_before_any_morning_check_an_administrator_is_sent_to_for_you(client, admin_user, reviewed):
    """R1 spec_pages-5: "Press Check now" is a button of the For you page, not of Home."""
    card = text(_home(client, admin_user).parts["for-you"])
    assert "has not finished a morning check yet" in card
    assert "Press Check now" not in card
    assert "Open For you and press Check now" in card
    banner = card[card.index("alert-warning") :]
    assert f'href="{reverse("watch:for_you")}"' in banner[: banner.index("</div>")]
    page = client.get(reverse("watch:for_you"))
    assert "Press Check now to run the first one" in text(page)  # the page itself keeps its words


def test_decisions_show_dates_in_words(client, admin_user, world, monkeypatch):
    """R1 spec_pages-6: "due 2026-11-02" and "(daily review of 2026-07-01)" were ISO dates."""
    from neurodb.reports import brief

    item = {"key": "k1", "severity": "critical", "title": "Extend 3 programme documents", "url": "/programmes/",
            "section": "Health", "status_label": "Raised", "due_date": "2026-11-02"}  # fmt: skip
    found = {"mode": "rules", "items": [item], "review_date": "2026-07-01", "decided_by": ""}
    monkeypatch.setattr(brief, "decisions", lambda sections, year, review=None: found)
    tasks = text(_home(client, admin_user).parts["tasks"])
    assert "due Mon 2 Nov" in _words(tasks)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", _words(tasks))
    found["items"] = []
    tasks = _words(text(_home(client, admin_user).parts["tasks"]))
    assert "Nothing needs a decision (daily review of 1 July 2026)." in tasks


def test_the_reviews_changes_are_a_sentence_without_zeros(reviewed):
    """R1 a11y N-3: "New: 240 · Still open: 30" were bare labels; Home says it in a sentence, without the parts
    that are 0 (the daily review's own page keeps its labels)."""
    review = DailyReview.objects.get()
    review.findings.update(state=ReviewFinding.State.NEW)
    changed = review_services.digest([])["changed"]
    n = review.findings.count()
    assert changed == [f"Since the review before: {n} new finding{'' if n == 1 else 's'}."]
    page = review_services.for_page("today", [])["changed"]
    assert any(line.startswith("New: ") for line in page)


def test_counts_on_home_have_thousands_separators():
    """R1 browser-8: "1071 open action points" beside "4,469,997"."""
    from django.template.loader import render_to_string

    assurance = {"field_monitoring_visits": 1500, "tpm_visits_completed": 1200, "tpm_visits_planned": 2400,
                 "open_action_points": 1071, "overdue_high_priority": 252}  # fmt: skip
    data = {"delivery": {"assurance": assurance}, "impact": {"main_indicators": {"largest": 1300}}}
    html = _words(
        render_to_string(
            "reports/partials/_home_also.html",
            {"data": data, "year": 2026, "is_editor": True, "programmes_query": "", "fmm_enabled": True,
             "selected": {"section": []}, "points_query": "overdue=1&priority=1", "fmm_query": "section=&year=2026"},
        )
    )  # fmt: skip
    assert "1,071 open action points (252 high priority overdue)" in html
    assert "1,500 field monitoring visits" in html and "1,200 of 2,400 third-party visits done" in html
    assert "1,300 programme documents are still counted" in html
    since = _words(
        render_to_string(
            "reports/parts/home_since.html",
            {"since_label": "Since yesterday", "since_yesterday": True, "changes": [], "matched": True,
             "selected": {"section": []},
             "your_tasks": [{"kind": "documents", "count": 1234, "url": "/"}, {"kind": "wellbeing", "count": 2500,
                            "url": "/"}, {"kind": "hpm", "count": 1001, "year": 2026, "url": "/"}]},
        )
    )  # fmt: skip
    assert "1,234 document review findings" in since and "2,500 Makani wellbeing flags" in since
    assert "1,001 HPM reports of 2026" in since


def test_long_decision_titles_take_two_lines_rather_than_end_alike(client, admin_user, reviewed):
    """R1 browser-3: two decisions cut to the same "… LEB/PCA202600…" at 1440: their titles may take two lines.
    The review's rows (one per check, so different titles) keep one line: the key figures stay on the first screen
    at 1440 x 900 (measured on x20, the Management member's Home)."""
    rules = dict(re.findall(r"\.(home-list--\w+) \.home-list__title \{([^}]*)\}", _home_css()))
    assert "-webkit-line-clamp: 2" in rules["home-list--decisions"]
    assert "white-space: normal" in rules["home-list--decisions"]
    tasks = text(_home(client, admin_user).parts["tasks"])
    assert '<ul class="home-list home-list--rows home-list--decisions">' in tasks
