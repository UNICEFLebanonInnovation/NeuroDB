"""NeuroDB Watch: the "For you" page, its count in the sidebar, its Overview card and its buttons."""

import datetime
import re
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import ADMIN, MANAGEMENT, SECTION_EDITOR, VIEWER
from neurodb.core.models import SyncRun
from neurodb.donors.models import DonorAccount
from neurodb.watch import page, routing
from neurodb.watch.detectors import CRITICAL, DEADLINE, INFO, ON, SECTION, TRIAL, WARNING
from neurodb.watch.models import DetectorSetting, SectionMatch, WatchItem, WatchNote, WatchReceipt

pytestmark = pytest.mark.django_db

CHECK = "report_due_soon"
Reaction = WatchReceipt.Reaction
LONG_AGO = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
FOR_YOU = "/for-you/"


def today() -> datetime.date:
    return timezone.localdate()


def day(n: int) -> datetime.date:
    return today() + datetime.timedelta(days=n)


# ---------------------------------------------------------------------------- the world
@pytest.fixture
def education(db):
    section = Section.objects.create(name="Education", code="EDU")
    SectionMatch.objects.create(etools_name="Education", section=section, how="exact", confirmed=True)
    return section


@pytest.fixture
def health(db):
    section = Section.objects.create(name="Health", code="HLT")
    SectionMatch.objects.create(etools_name="Health", section=section, how="exact", confirmed=True)
    return section


def person(username, role=VIEWER, section=None, management=False, **fields) -> User:
    user = User.objects.create_user(
        username=username, email=f"{username}@example.org", password="pass-123456-abc", **fields
    )
    User.objects.filter(pk=user.pk).update(date_joined=LONG_AGO)
    if role:
        user.groups.add(Group.objects.get(name=role))
    if management:
        user.groups.add(Group.objects.get(name=MANAGEMENT))
    if section is not None:
        user.section = section
        user.save(update_fields=["section"])
    return User.objects.get(pk=user.pk)


@pytest.fixture
def team(roles, education, health):
    class Team:
        admin = person("admin", ADMIN, education, is_staff=True)
        boss = person("boss", VIEWER, health, management=True)
        edu = person("edu", VIEWER, education)
        edu2 = person("edu2", VIEWER, education)
        hlt = person("hlt", VIEWER, health)
        editor = person("editor", SECTION_EDITOR, education)
        nobody = person("nobody", VIEWER, None)

    DetectorSetting.objects.update_or_create(detector=CHECK, defaults={"mode": ON, "on_since": day(-30)})
    return Team


_numbers = iter(range(1, 100_000))


def item(sections=("Education",), severity=WARNING, due=None, first_seen=None, detector=CHECK, **fields):
    mapping = dict(SectionMatch.objects.filter(confirmed=True).values_list("etools_name", "section_id"))
    n = next(_numbers)
    first_seen = first_seen or day(-2)
    return WatchItem.objects.create(
        key=fields.pop("key", f"due:test:{n}"),
        detector=detector,
        kind=DEADLINE,
        severity=severity,
        title=fields.pop("title", f"Progress report due: PD {n}"),
        due_date=due,
        etools_sections=list(sections),
        section_ids=sorted({mapping[s] for s in sections if s in mapping}),
        scope=SECTION,
        evidence={
            "source": "eTools progress reports",
            "synced_at": "",
            "records": [{"label": f"Report {n}", "date": "", "value": "", "url": "/programmes/"}],
            "numbers": {"days_left": 3},
        },
        first_seen_on=first_seen,
        last_seen_on=today(),
        changed_on=first_seen,
        story=[{"on": first_seen.isoformat(), "text": "First noticed"}],
        **fields,
    )


def tell(user, item_, level=WatchReceipt.Level.NEEDS_YOU, step="new", on=None, **fields) -> WatchReceipt:
    on = on or today()
    values = {"first_told_on": on, "last_told_on": on, "told_step": step, "level": level, **fields}
    return WatchReceipt.objects.create(user=user, item=item_, **values)


def login(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def htmx(client, url, **kwargs):
    return client.get(url, HTTP_HX_REQUEST="true", **kwargs)


def react(client, receipt, **data):
    return client.post(reverse("watch:react", args=[receipt.pk]), data, HTTP_HX_REQUEST="true")


def succeeded(hours_ago: float, target="daily") -> SyncRun:
    finished = timezone.now() - datetime.timedelta(hours=hours_ago)
    return SyncRun.objects.create(
        job=SyncRun.Job.WATCH,
        target=target,
        status=SyncRun.Status.SUCCEEDED,
        started_at=finished - datetime.timedelta(minutes=1),
        finished_at=finished,
    )


def visible_text(html: str) -> str:
    html = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", html)


# ---------------------------------------------------------------------------- who sees what
def test_an_anonymous_visitor_is_sent_to_sign_in(client, team):
    response = client.get(FOR_YOU)
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_a_viewer_sees_the_points_of_their_section(team):
    mine = item(due=day(3), title="Progress report due 7 Oct: LEB/PCA1/PD1 (Amel), Q3")
    theirs = item(sections=("Health",), title="Health point that is not mine")
    tell(team.edu, mine)
    tell(team.hlt, theirs, comment="health comment")
    response = login(team.edu).get(FOR_YOU)
    html = response.content.decode()
    assert response.status_code == 200
    assert "Progress report due 7 Oct: LEB/PCA1/PD1 (Amel), Q3" in html
    assert "Needs you today" in html and "New" in html
    assert "Health point that is not mine" not in html  # section B's point, and its receipt
    assert "health comment" not in html
    assert "What NeuroDB noticed for Education" in html


def test_a_viewer_of_one_section_never_sees_the_receipts_of_another(team):
    shared = item(sections=("Education", "Health"))
    told_hlt = tell(team.hlt, shared, reaction=Reaction.NOT_USEFUL, comment="only for admins")
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert shared.title in html  # the point is theirs too
    assert reverse("watch:react", args=[told_hlt.pk]) not in html
    assert "only for admins" not in html


def test_a_donor_is_sent_to_the_donor_page_and_the_htmx_parts_are_refused(team, education):
    donor = person("donor", VIEWER, education)
    DonorAccount.objects.create(user=donor, name="EU", donors=["EU"], must_change_password=False)
    receipt = tell(donor, item())
    client = login(donor)
    response = client.get(FOR_YOU)
    assert response.status_code == 302 and response["Location"] == reverse("donors:page")
    assert htmx(client, reverse("watch:badge")).status_code == 403
    assert htmx(client, reverse("watch:card")).status_code == 403
    assert react(client, receipt, reaction="useful").status_code == 403
    receipt.refresh_from_db()
    assert receipt.reaction == ""
    page_html = client.get(reverse("donors:page")).content.decode()
    assert reverse("watch:for_you") not in page_html


def test_the_sidebar_link_and_the_lazy_count_are_on_staff_pages(team):
    html = login(team.edu).get(FOR_YOU).content.decode()
    side = html.split('class="sidebar', 1)[1].split("</nav>", 1)[0]
    assert 'href="/for-you/"' in side and "For you" in side
    assert f'hx-get="{reverse("watch:badge")}"' in side and "#i-bell" in side


def test_someone_without_a_section_reads_everything_open_and_is_told_to_ask_for_one(team):
    on_trial = item(detector="new_check")
    DetectorSetting.objects.create(detector="new_check", mode=TRIAL)
    open_point = item()
    html = login(team.nobody).get(FOR_YOU).content.decode()
    assert "ask an administrator to set your section" in html
    assert open_point.title in html and on_trial.title not in html
    assert "/react/" not in html
    assert page.badge_count(team.nobody) == 0


def test_the_whole_country_view_reads_the_country_note_and_trial_checks(team):
    DetectorSetting.objects.create(detector="new_check", mode=TRIAL)
    trial = item(detector="new_check", title="A point found by a new check")
    WatchNote.objects.create(
        date=today(),
        audience_key=WatchNote.COUNTRY,
        audience_name="Whole country",
        sentences=[{"text": "Education has 1 open point.", "keys": [trial.key]}],
        written_by="template",
        ai_skipped_reason="budget",
    )
    tell(team.boss, trial)
    html = login(team.boss).get(FOR_YOU).content.decode()
    assert "Across the country" in html and "Education has 1 open point." in html
    assert "Trial check" in html and trial.title in html
    assert "AI not used today: daily limit reached" in html
    assert f'href="#watch-{trial.pk}"' in html and f'id="watch-{trial.pk}"' in html
    # Not in the country view: the trial check's points are not shown
    assert trial.title not in login(team.edu).get(FOR_YOU).content.decode()


def test_the_administrators_picker_shows_an_audience_and_never_anyones_receipts(team, health):
    edu_point = item()
    tell(team.edu, edu_point, reaction=Reaction.WRONG, comment="secret remark")
    WatchNote.objects.create(
        date=today(),
        audience_key=WatchNote.section_audience(team.edu.section_id),
        sentences=[{"text": "One report is due.", "keys": [edu_point.key]}],
        written_by="model-x",
    )
    client = login(team.admin)
    html = client.get(FOR_YOU, {"audience": WatchNote.section_audience(team.edu.section_id)}).content.decode()
    assert "You are looking at what Education is shown" in html
    assert edu_point.title in html and "One report is due." in html
    assert "/react/" not in html and "secret remark" not in html
    assert "Show notes for" in html and "Check now" in html
    # Nobody else gets the picker; their own page ignores the choice
    viewer_html = login(team.edu).get(FOR_YOU, {"audience": WatchNote.COUNTRY}).content.decode()
    assert "Show notes for" not in viewer_html and "You are looking at" not in viewer_html


def test_a_preview_stamps_nothing(team):
    receipt = tell(team.admin, item())
    login(team.admin).get(FOR_YOU, {"audience": WatchNote.COUNTRY})
    receipt.refresh_from_db()
    assert receipt.seen_at is None


# ---------------------------------------------------------------------------- the count and the card
def test_the_count_shows_unseen_needs_you_points_of_today_and_opening_the_page_clears_it(team):
    told = [tell(team.edu, item()) for _ in range(3)]
    tell(team.edu, item(), on=day(-1))  # yesterday's
    tell(team.edu, item(severity=INFO), level=WatchReceipt.Level.GOOD_TO_KNOW)  # not "Needs you"
    tell(team.edu, item(), step="known")  # only recorded as known
    client = login(team.edu)
    response = htmx(client, reverse("watch:badge"))
    assert response.status_code == 200 and ">3<" in response.content.decode()
    client.get(FOR_YOU)
    assert all(WatchReceipt.objects.get(pk=r.pk).seen_at is not None for r in told)
    assert 'class="nav-count"' not in htmx(client, reverse("watch:badge")).content.decode()


def test_the_count_is_cached_until_the_persons_receipts_change_whichever_worker_cached_it(team):
    first = tell(team.edu, item())
    client = login(team.edu)
    assert ">1<" in htmx(client, reverse("watch:badge")).content.decode()
    DetectorSetting.objects.filter(detector=CHECK).update(mode=TRIAL)  # nothing of theirs changed: cached
    assert ">1<" in htmx(client, reverse("watch:badge")).content.decode()
    DetectorSetting.objects.filter(detector=CHECK).update(mode=ON)
    tell(team.edu, item())  # told by a pass meanwhile
    assert ">2<" in htmx(client, reverse("watch:badge")).content.decode()
    # seen on For you, served by another web worker (no forget_badge in this one): the count follows
    WatchReceipt.objects.filter(pk=first.pk).update(seen_at=timezone.now())
    assert ">1<" in htmx(client, reverse("watch:badge")).content.decode()


def test_a_snooze_lowers_the_count(team):
    receipts = [tell(team.edu, item(due=day(20))) for _ in range(2)]
    client = login(team.edu)
    assert page.badge_count(team.edu) == 2
    response = react(client, receipts[0], snooze="tomorrow")
    assert response.status_code == 200 and "remind you tomorrow" in response.content.decode()
    receipts[0].refresh_from_db()
    assert receipts[0].snoozed_until == day(1)
    assert page.badge_count(team.edu) == 1
    response = react(client, receipts[1], snooze="before_due")
    receipts[1].refresh_from_db()
    assert receipts[1].snoozed_until == day(17)
    assert page.badge_count(team.edu) == 0
    html = client.get(FOR_YOU).content.decode()
    assert "Set aside (2)" in html and "You asked to hear about it again tomorrow" in html


def test_a_reminder_before_a_date_already_past_is_refused_in_plain_words(team):
    receipt = tell(team.edu, item(due=day(2)))
    response = react(login(team.edu), receipt, snooze="before_due")
    html = response.content.decode()
    # the card again, saying why (htmx shows no 4xx: the person would read only an error code)
    assert response.status_code == 200 and "That reminder date has already passed." in html
    assert "watch-card__said--refused" in html and 'id="watch-live"' in html
    receipt.refresh_from_db()
    assert receipt.snoozed_until is None


def test_the_overview_card_lists_the_first_three(team):
    for n in range(4):
        tell(team.edu, item(severity=CRITICAL if n == 3 else WARNING, title=f"Point number {n}"))
    response = htmx(login(team.edu), reverse("watch:card"))
    html = response.content.decode()
    assert "For you: 4 things need you today" in html
    assert "Point number 3" in html  # critical first
    assert html.count("watch-card__sev--") == 3
    assert "See all" in html


def test_the_overview_card_is_empty_for_someone_told_nothing(roles, education):
    norole = person("norole", None, education)
    response = htmx(login(norole), reverse("watch:card"))
    assert response.status_code == 200 and "For you" not in response.content.decode()


def test_the_overview_reads_no_watch_table_and_loads_the_card_later(team, hierarchy):
    tell(team.edu, item())
    client = login(team.edu)
    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse("reports:overview"))
    assert response.status_code == 200
    assert f'hx-get="{reverse("watch:card")}"' in response.content.decode()
    assert not [q["sql"] for q in queries.captured_queries if '"watch_' in q["sql"]]


# ---------------------------------------------------------------------------- the buttons
def test_a_button_acts_only_on_the_persons_own_receipt(team):
    theirs = tell(team.edu2, item())
    response = react(login(team.edu), theirs, reaction="useful")
    assert response.status_code == 404
    theirs.refresh_from_db()
    assert theirs.reaction == ""


def test_a_button_needs_the_csrf_token(team):
    receipt = tell(team.edu, item())
    client = Client(enforce_csrf_checks=True)
    client.force_login(team.edu)
    response = client.post(reverse("watch:react", args=[receipt.pk]), {"reaction": "useful"})
    assert response.status_code == 403
    receipt.refresh_from_db()
    assert receipt.reaction == ""


def test_useful_is_recorded_and_the_card_comes_back(team):
    receipt = tell(team.edu, item())
    response = react(login(team.edu), receipt, reaction="useful")
    html = response.content.decode()
    assert response.status_code == 200 and "Thanks: marked useful." in html
    assert 'value="useful" aria-pressed="true"' in html
    receipt.refresh_from_db()
    assert receipt.reaction == Reaction.USEFUL and receipt.reacted_at and receipt.seen_at


def test_without_htmx_a_button_goes_back_to_the_page(team):
    receipt = tell(team.edu, item())
    client = login(team.edu)
    response = client.post(reverse("watch:react", args=[receipt.pk]), {"reaction": "not_mine"})
    assert response.status_code == 302 and response["Location"].startswith(FOR_YOU)
    receipt.refresh_from_db()
    assert receipt.reaction == Reaction.NOT_MINE


def test_an_unknown_answer_is_refused(team):
    receipt = tell(team.edu, item())
    response = react(login(team.edu), receipt, reaction="delete_everything")
    assert response.status_code == 200 and "Unknown answer." in response.content.decode()
    receipt.refresh_from_db()
    assert receipt.reaction == ""


def test_a_viewers_somethings_wrong_hides_the_point_only_for_them(team):
    point = item()
    mine, colleague = tell(team.edu, point), tell(team.edu2, point)
    response = react(login(team.edu), mine, reaction="wrong", comment="  Not  late at all  ")
    assert "It is hidden for you" in response.content.decode()
    mine.refresh_from_db()
    point.refresh_from_db()
    assert mine.reaction == Reaction.WRONG and mine.comment == "Not late at all"
    assert point.state == WatchItem.State.OPEN
    edu_html = login(team.edu).get(FOR_YOU).content.decode()
    assert "You said something is wrong" in edu_html  # set aside, with Undo
    assert f'id="watch-{point.pk}"' in edu_html
    colleague_html = login(team.edu2).get(FOR_YOU).content.decode()
    assert point.title in colleague_html and "Set aside" not in colleague_html
    assert colleague.pk  # the colleague's own receipt is untouched
    assert "Not late at all" not in colleague_html


def test_an_editors_somethings_wrong_hides_the_point_for_the_section(team):
    point = item()  # its only section is the editor's: wrong for everyone
    editor_receipt = tell(team.editor, point)
    tell(team.edu, point)
    response = react(login(team.editor), editor_receipt, reaction="wrong")
    assert "hidden for everyone until its data changes" in response.content.decode()
    point.refresh_from_db()
    assert point.state == WatchItem.State.WRONG
    assert point.story[-1]["text"].startswith("Marked wrong")
    assert point.title not in login(team.edu).get(FOR_YOU).content.decode()
    # Undo by the editor brings it back
    react(login(team.editor), editor_receipt, reaction="undo")
    point.refresh_from_db()
    assert point.state == WatchItem.State.OPEN
    assert point.title in login(team.edu).get(FOR_YOU).content.decode()


def test_an_editor_of_another_section_does_not_decide_for_this_one(team, health):
    other_editor = person("hlt-editor", SECTION_EDITOR, health)
    point = item(sections=("Education", "Health"))
    receipt = tell(other_editor, point)
    react(login(other_editor), receipt, reaction="done")
    # Health's editor marked it done: hidden for Health, still shown to Education
    assert point.title in login(team.edu).get(FOR_YOU).content.decode()
    hlt_html = login(team.hlt).get(FOR_YOU).content.decode()
    assert "Marked done for your section" in hlt_html


def test_an_editors_done_hides_the_point_for_their_section(team):
    point = item()
    editor_receipt = tell(team.editor, point)
    edu_receipt = tell(team.edu, point)
    response = react(login(team.editor), editor_receipt, reaction="done")
    assert "hidden for your section too" in response.content.decode()
    point.refresh_from_db()
    assert point.state == WatchItem.State.OPEN  # done is checked against the data later
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert "Marked done for your section" in html
    assert page.badge_count(team.edu) == 0
    edu_receipt.refresh_from_db()
    assert edu_receipt.reaction == ""


def test_undo_takes_an_answer_back(team):
    receipt = tell(team.edu, item(), reaction=Reaction.NOT_MINE, reacted_at=timezone.now())
    response = react(login(team.edu), receipt, reaction="undo")
    assert "Your answer was taken back." in response.content.decode()
    receipt.refresh_from_db()
    assert receipt.reaction == "" and receipt.reacted_at is None


def test_a_point_told_again_after_not_mine_is_shown_again(team):
    point = item(severity=CRITICAL)
    yesterday = timezone.now() - datetime.timedelta(days=1)
    tell(team.edu, point, step="worse", reaction=Reaction.NOT_MINE, reacted_at=yesterday)
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert "Set aside" not in html and "Needs you today" in html
    assert f'id="watch-{point.pk}"' in html


# ---------------------------------------------------------------------------- what the page says
def test_model_text_is_escaped(team):
    point = item(title="Report <b>due</b>")
    point.looked_up = {
        "kept": True,
        "text": "<img src=x onerror=alert(2)> 3 reports",
        "on": today().isoformat(),
    }
    point.save()
    tell(team.edu, point)
    WatchNote.objects.create(
        date=today(),
        audience_key=WatchNote.section_audience(team.edu.section_id),
        sentences=[{"text": "<script>alert(1)</script> is due.", "keys": [point.key]}],
        written_by="model-x",
    )
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img src=x" not in html and "&lt;img src=x onerror=alert(2)&gt;" in html
    assert "Report <b>due</b>" not in html
    assert "AI wrote this from the facts below" in html


def test_the_look_up_is_shown_as_written_by_ai_only_when_kept(team):
    kept = item(severity=CRITICAL)
    kept.looked_up = {
        "kept": True,
        "text": "3 of its 4 reports were late this year.",
        "on": today().isoformat(),
    }
    kept.save()
    dropped = item(severity=CRITICAL)
    dropped.looked_up = {"kept": False, "text": "", "reason": "number", "on": today().isoformat()}
    dropped.save()
    tell(team.edu, kept)
    tell(team.edu, dropped)
    html = login(team.edu).get(FOR_YOU).content.decode()
    # The kept look-up on its card (shown once: in "Needs you today"), not the other
    assert html.count('class="watch-lookup"') == 1
    assert html.count("3 of its 4 reports were late this year.") == 1
    assert "What NeuroDB looked up (AI)" in html


def test_the_page_renders_with_the_ai_off(team):
    WatchNote.objects.create(
        date=today(),
        audience_key=WatchNote.section_audience(team.edu.section_id),
        sentences=[{"text": "Due in the next 7 days: one report.", "keys": []}],
        written_by=WatchNote.TEMPLATE,
        ai_skipped_reason=WatchNote.Skipped.OFF,
    )
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert "Listed by NeuroDB. AI not used today: the AI is switched off" in html
    assert "AI wrote this" not in html


def test_an_older_note_says_it_is_not_todays(team):
    WatchNote.objects.create(
        date=day(-1),
        audience_key=WatchNote.section_audience(team.edu.section_id),
        sentences=[{"text": "The sentence of yesterday.", "keys": []}],
        written_by=WatchNote.TEMPLATE,
    )
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert "The sentence of yesterday." in html and "No note yet today" in html


def test_the_empty_page(team):
    # a morning check earlier today (Beirut time), whatever the hour the test runs at (just after
    # midnight, an hour ago would be yesterday)
    now = timezone.localtime()
    since_midnight = now - now.replace(hour=0, minute=0, second=0, microsecond=0)
    succeeded(min(1, since_midnight.total_seconds() / 3600 / 2))
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert "Nothing needs you today." in html and "NeuroDB checked today at" in html
    assert "Everything open (0)" in html
    assert "has not checked" not in html


@pytest.mark.parametrize(("hours", "warned"), [(2, False), (25, False), (27, True)])
def test_the_banner_appears_after_26_hours_without_a_morning_check(team, hours, warned):
    succeeded(hours)
    succeeded(0.5, target="quick")  # a quick pass is not the morning check
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert ("NeuroDB has not checked since" in html) is warned


def test_the_banner_when_it_never_ran_and_the_link_for_administrators(team):
    assert "has not finished a morning check yet" in login(team.edu).get(FOR_YOU).content.decode()
    html = login(team.admin).get(FOR_YOU).content.decode()
    assert reverse("admin:core_scheduledjob_changelist") in html
    assert "Press Check now to run the first one" in html  # an administrator is told what to press


def test_a_card_says_how_we_know_what_it_remembers_and_what_it_connects_to(team):
    synced = (timezone.now() - datetime.timedelta(days=1)).isoformat()
    point = item(
        due=day(6),
        first_seen=day(-14),
        related=[
            {"kind": "partner", "key": "7", "name": "Amel Association", "url": "/partners/7/"},
            {"kind": "grant", "key": "SC1", "name": "SC1", "url": "", "date": day(57).isoformat()},
            {"kind": "document", "key": "d1", "name": "Minutes", "url": "", "date": day(-2).isoformat()},
            {"kind": "document", "key": "d2", "name": "Letter", "url": "", "date": day(-3).isoformat()},
            {"kind": "watch_item", "key": "due:x", "name": "Other", "url": ""},
            {"kind": "partner", "key": "8", "name": "Bad link", "url": "javascript:alert(1)"},
        ],
    )
    point.evidence["synced_at"] = synced
    point.save()
    tell(team.edu, point, first_told_on=day(-13))
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert "How we know" in html and "eTools progress reports, updated yesterday at" in html
    assert "First noticed" in html and "open 14 days" in html and "told you first" in html
    assert "Connected to" in html and "Amel Association" in html and "grant SC1 expires" in html
    assert "2 documents" in html and "1 other open point" in html
    assert "javascript:" not in html
    assert "Due " in html and "(in 6 days)" in html


def test_a_forecast_says_its_range(team):
    point = item(detector="forecast_short", confidence="likely")
    point.evidence["numbers"] = {"low_pct": 62.4, "high_pct": 78.1}
    point.save()
    DetectorSetting.objects.create(detector="forecast_short", mode=ON)
    tell(team.edu, point)
    assert "Estimate: between 62% and 78% of target" in login(team.edu).get(FOR_YOU).content.decode()


@override_settings(AI_ASSISTANT_ENABLED=True)
def test_look_into_this_opens_ask_neurodb_with_the_question_typed(team):
    point = item(title="Grant SC1 expires 30 Nov with money unspent " + "x" * 250)
    tell(team.edu, point)
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert "Look into this" in html
    link = re.search(r'href="(/ask/\?q=[^"]+)"', html).group(1).replace("&amp;", "&")
    question = parse_qs(urlparse(link).query)["q"][0]
    assert question.startswith("Look into this open point for me: Grant SC1 expires 30 Nov")
    assert len(question) <= 1000


def test_staff_wording_avoids_the_words_of_the_machinery(team):
    point = item(due=day(4))
    tell(team.edu, point)
    WatchNote.objects.create(
        date=today(),
        audience_key=WatchNote.section_audience(team.edu.section_id),
        sentences=[{"text": "One report is due.", "keys": [point.key]}],
        written_by=WatchNote.TEMPLATE,
        ai_skipped_reason=WatchNote.Skipped.BUDGET,
    )
    text = visible_text(login(team.edu).get(FOR_YOU).content.decode()).lower()
    for word in ("detector", "receipt", "agent", "llm", "item"):
        assert not re.search(rf"\b{word}s?\b", text), word


# ---------------------------------------------------------------------------- check now
def test_check_now_is_for_administrators(team):
    with mock.patch("neurodb.core.jobs.start", return_value="started") as start:
        assert login(team.edu).post(reverse("watch:check_now")).status_code == 404
        start.assert_not_called()
        response = login(team.admin).post(reverse("watch:check_now"), follow=True)
    start.assert_called_once_with("watch", triggered_by="admin")
    assert "NeuroDB is checking now" in response.content.decode()


# ---------------------------------------------------------------------------- answers, each section on its own
def test_an_editors_somethings_wrong_on_a_point_of_several_sections_hides_it_for_theirs_only(team):
    point = item(sections=("Education", "Health"))
    editor_receipt = tell(team.editor, point)
    tell(team.edu, point)
    tell(team.hlt, point)
    tell(team.admin, point)
    response = react(login(team.editor), editor_receipt, reaction="wrong", comment="Not ours")
    assert "hidden for your section" in response.content.decode()
    point.refresh_from_db()
    assert point.state == WatchItem.State.OPEN  # Health, the Administrators and Management still see it
    editor_receipt.refresh_from_db()
    assert editor_receipt.wrong_hash == routing.evidence_mark(point)
    edu_html = login(team.edu).get(FOR_YOU).content.decode()
    assert "Marked wrong for your section, until its data changes" in edu_html
    hlt_html = login(team.hlt).get(FOR_YOU).content.decode()
    assert point.title in hlt_html and "Marked wrong for your section" not in hlt_html
    assert page.card(team.hlt)["count"] == 1 and page.card(team.edu)["count"] == 0
    # its evidence changes: shown to Education again
    WatchItem.objects.filter(pk=point.pk).update(evidence_hash="the records changed")
    assert page.card(team.edu)["count"] == 1
    assert "Marked wrong for your section" not in login(team.edu).get(FOR_YOU).content.decode()


def test_an_administrators_somethings_wrong_hides_it_for_everyone(team):
    point = item(sections=("Education", "Health"))
    receipt = tell(team.admin, point)
    response = react(login(team.admin), receipt, reaction="wrong")
    assert "hidden for everyone" in response.content.decode()
    point.refresh_from_db()
    assert point.state == WatchItem.State.WRONG


# ---------------------------------------------------------------------------- the answer buttons
def test_the_comment_has_its_own_form_so_enter_sends_somethings_wrong(team):
    """Enter in a text box submits its form with the form's first button: the comment's form has only
    "Something's wrong" (never "Useful")."""
    receipt = tell(team.edu, item())
    html = login(team.edu).get(FOR_YOU).content.decode()
    forms = re.findall(r"<form class=\"watch-actions__form\".*?</form>", html, re.S)
    assert len(forms) == 2
    useful, wrong = forms
    assert 'value="useful"' in useful and 'name="comment"' not in useful
    assert 'name="comment"' in wrong and 'value="useful"' not in wrong
    assert '<input type="hidden" name="reaction" value="wrong">' in wrong
    # what the browser sends when Enter is pressed in the comment: the form's own fields
    response = react(login(team.edu), receipt, reaction="wrong", comment="wrong partner")
    receipt.refresh_from_db()
    assert receipt.reaction == Reaction.WRONG and receipt.comment == "wrong partner"
    assert "Noted: something is wrong. It is hidden for you" in response.content.decode()


def test_after_a_button_focus_and_the_live_region_get_the_message(team):
    receipt = tell(team.edu, item())
    page_html = login(team.edu).get(FOR_YOU).content.decode()
    assert '<div id="watch-live" class="visually-hidden" role="status" aria-live="polite"></div>' in page_html
    html = react(login(team.edu), receipt, reaction="useful").content.decode()
    assert re.search(r'<p class="watch-card__said" tabindex="-1" autofocus>Thanks: marked useful.</p>', html)
    assert re.search(r'<div id="watch-live"[^>]*hx-swap-oob="true">Thanks: marked useful.</div>', html)


def test_done_says_once_more_in_7_days_without_naming_etools(team):
    receipt = tell(team.edu, item())
    html = react(login(team.edu), receipt, reaction="done").content.decode()
    said = re.search(r'<p class="watch-card__said"[^>]*>(.*?)</p>', html).group(1)
    assert said == "Marked done. If it is still open in 7 days, NeuroDB tells you once more."


# ---------------------------------------------------------------------------- what the page shows
def test_each_point_is_on_the_page_once(team):
    needs = item(due=day(5))
    later = item(due=day(40))
    tell(team.edu, needs)
    html = login(team.edu).get(FOR_YOU).content.decode()
    assert html.count(f'id="watch-{needs.pk}"') == 1 and html.count(needs.title) == 1
    assert "Everything else open (1)" in html and later.title in html


def test_the_reason_chip_holds_only_while_it_is_true(team):
    fresh = tell(team.edu, item(due=day(9)), step="new")
    old_new = tell(team.edu, item(due=day(9)), step="new", on=day(-40))
    yesterday = tell(team.edu, item(due=day(9)), step="worse", on=day(-1))
    milestone = tell(team.edu, item(due=day(9)), step="14", on=day(-5))
    shown = {
        receipt.pk: page.point(receipt.item, receipt, today())["reason"]
        for receipt in (fresh, old_new, yesterday, milestone)
    }
    assert shown == {
        fresh.pk: "New",
        old_new.pk: "",  # told 40 days ago: no longer new
        yesterday.pk: "Got worse · told yesterday",
        milestone.pk: "",  # "Due in 14 days" told 5 days ago is out of date: the due chip says it
    }


def test_how_we_know_shows_words_and_dates_not_field_names(team):
    point = item(due=day(10))
    point.evidence = {
        "source": "Year-end forecasts",
        "records": [{"label": "Children reached", "date": f"{today().year}-10-03", "value": 800, "url": ""}],
        "numbers": {
            "low_pct": 62.5,
            "unspent": 48000,
            "year": 2026,
            "as_of_month": 8,
            "flagged_by_review": True,
        },
    }
    point.story = [{"on": f"{today().year}-10-03", "text": "Got worse: was warning, now critical"}]
    point.save()
    shown = page.point(point, None, today())["evidence"]
    numbers = {n["name"]: n["value"] for n in shown["numbers"]}
    assert numbers == {
        "Lowest likely (% of target)": "62.5",
        "Unspent (US$)": "48,000",
        "Year": "2026",
        "Data up to": "August",
        "Flagged by the daily review": "yes",
    }
    assert shown["records"][0]["date"] == "3 Oct" and shown["story"][0]["on"] == "3 Oct"


def test_staff_without_a_section_are_not_told_nothing_is_due(team):
    item(due=day(3))
    html = login(team.nobody).get(FOR_YOU).content.decode()
    assert "Nothing is due in the next 30 days" not in html and "Coming up" not in html
    assert "ask an administrator to set your section" in html


def test_someone_without_a_role_is_told_to_ask_for_a_role(roles, education):
    norole = person("norole2", None, education)
    html = login(norole).get(FOR_YOU).content.decode()
    assert "ask an administrator to give you a role" in html
    assert "ask an administrator to set your section" not in html


def test_the_count_and_the_card_list_only_points_the_page_shows(team):
    point = item()
    tell(team.edu, point)
    client = login(team.edu)
    assert page.badge_count(team.edu) == 1 and page.card(team.edu)["count"] == 1
    DetectorSetting.objects.filter(detector=CHECK).update(mode=TRIAL)  # an administrator: back to trial
    page.forget_badge(team.edu)
    assert page.badge_count(team.edu) == 0 and page.card(team.edu)["count"] == 0
    assert "Nothing needs you today." in client.get(FOR_YOU).content.decode()


def test_the_overview_card_links_each_point_and_names_its_severity(team):
    point = item(severity=CRITICAL)
    tell(team.edu, point)
    html = htmx(login(team.edu), reverse("watch:card")).content.decode()
    assert f'href="{FOR_YOU}#watch-{point.pk}"' in html
    assert 'role="img" aria-label="Critical"' in html


def test_an_answer_reads_the_point_anew_so_a_pass_writing_it_meanwhile_is_kept(team):
    point = item(sections=("Education", "Health"))
    receipt = WatchReceipt.objects.select_related("item").get(pk=tell(team.editor, point).pk)
    # a pass writes the point after the page loaded it
    worse = {"on": today().isoformat(), "text": "Got worse: was warning, now critical"}
    WatchItem.objects.filter(pk=point.pk).update(story=[*point.story, worse], severity=CRITICAL)
    page.react(receipt, reaction=Reaction.WRONG)
    point.refresh_from_db()
    assert point.severity == CRITICAL and worse["text"] in [line["text"] for line in point.story]
    assert point.story[-1]["text"].startswith("Marked wrong for one of its sections")
