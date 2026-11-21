"""ActivityInfo out of every page but its archive (the owner's rule, November 2026).

NeuroDB no longer uses ActivityInfo; its archive stays as it is. With ActivityInfo data present (databases of two
years with master indicators counting children, Neuro and HPM reports, a partner's ActivityInfo results, records
against a programme document, the import's runs, year-end forecasts), in use or archived, no regular page names
ActivityInfo, shows a figure taken from it, or links into its archive: every page of the full menu and its tabs,
Home for a section editor, the partner profile, a programme document, the search for a word ActivityInfo data
matches, Data health, the public landing page and every help guide page. Only the archive is left as it is: the
menu's archive block, the search's archive entries (labelled as the archive), the archive's own pages and the one
help section that describes them. Ask NeuroDB is told the archive is past results, never to be brought up unasked.
"""

from __future__ import annotations

import datetime
import re
from urllib.parse import urlsplit

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import SECTION_EDITOR
from neurodb.core.models import ActivityInfoSetting, SyncRun
from neurodb.facts.models import ActivityReportNew
from neurodb.indicators.models import Database, NeuroReport
from neurodb.indicators.services import activityinfo, navigation
from neurodb.insights.models import IndicatorForecast
from neurodb.partnerships.models import PCA, PartnerLink
from tests.graph.conftest import no_ai  # noqa: F401 - the knowledge hub's build, without the AI
from tests.reports.brief_world import World, months

pytestmark = pytest.mark.django_db

# "ActivityInfo", "Activity Info", "activityinfo" (an address or an anchor) and the owner's other spellings
NAMED = re.compile(r"activity[\s_-]?info|\bAI databases?\b", re.I)
# an address of the archive: its databases and their pages, the Neuro and HPM reports, the year-end forecasts
# and a partner's ActivityInfo page
ARCHIVE_LINK = re.compile(
    r'(?:href|hx-get|data-href|action)="(?:https?://[^/"]+)?'
    r'(/databases/[^"]*|/reports/\d+/[^"]*|/insights/forecasts/[^"]*|/partners/\d+/activityinfo/[^"]*)"'
)
# the search's archive groups and its Pages entries for the archive, labelled as the archive
SEARCH_ARCHIVE_GROUP = re.compile(
    r'<div class="search-group__title">ActivityInfo archive: [^<]*<span class="search-group__count">[^<]*</span>'
    r'</div>(?:\s*<a class="search-item[^"]*"[^>]*>(?:(?!</a>).)*</a>)*',
    re.S,
)
SEARCH_ARCHIVE_PAGE = re.compile(
    r'<a class="search-item"[^>]*>(?:(?!</a>).)*<span class="search-item__hint">ActivityInfo archive</span></a>',
    re.S,
)
# the one help section that describes the archive and its pages
HELP_ARCHIVE = ("overview", "past-monthly-results")
ASK_ARCHIVE_LINE = (
    "ActivityInfo is an archive of past monthly results: do not bring it up or use its data unless the person "
    "asks about the archive or those past results."
)


# ------------------------------------------------------------------------------------------ the world
def _mode(mode: str) -> None:
    ActivityInfoSetting.objects.update_or_create(pk=1, defaults={"mode": mode})
    activityinfo.invalidate()


def _run(job: str, status: str, **details) -> SyncRun:
    now = timezone.now()
    return SyncRun.objects.create(
        job=job, status=status, started_at=now - datetime.timedelta(hours=1), finished_at=now, details=details
    )


@pytest.fixture(params=["in_use", "archived"])
def ai_world(request, db, roles):
    """eTools and ActivityInfo side by side, ActivityInfo larger: one programme document of Child Protection
    reporting children monthly in 2025 and 2026, ActivityInfo databases of 2025 and 2026 (2026 ticked current,
    records up to September) whose HPM master counts five times as many children, a Neuro report, records
    named after the programme document, the partner's ActivityInfo name linked to it, the import's runs (one
    failed), a year-end forecast likely short, and a superuser and a section editor of Child Protection."""
    world = World()
    section = world.section("Child Protection", "CP")
    pd = world.pd("Child Protection", {**months(2025, 12, 80), **months(2026, 9, 100)})
    PCA.objects.filter(pk=pd.pk).update(
        donors=["European Union"],
        grants=["SC220001"],
        donors_set=[{"donor": "European Union", "grant": "SC220001", "value": 50000}],
    )
    world.ai(2025, "Child Protection", {m: 400 for m in range(1, 13)})
    database = world.ai(2026, "Child Protection", {m: 500 for m in range(1, 10)}, current=True)
    year = database.reporting_year
    NeuroReport.objects.create(name="Neuro Child Protection 2026", is_hpm=False, ryear=year)
    master = database.masterindicator_set.get()
    for month in (8, 9):  # records against the programme document's number, in another place
        ActivityReportNew.objects.create(
            dbase=database, database_ai_id=str(database.ai_id), indicator_id="i2", indicator_name="x",
            indicator_value=7, month_name=f"2026-{month:02d}-01", month=f"2026-{month:02d}",
            partner_label="P", project_label=pd.number, location_adminlevel_governorate="Akkar",
            location_adminlevel_governorate_code="AKK", location_name="Halba camp", funded_by="UNICEF",
        )  # fmt: skip
    PartnerLink.objects.create(
        label="P", partner=pd.partner, method="manual", records=23, first_month="2025-01", last_month="2026-09",
        database_ids=sorted(Database.objects.values_list("pk", flat=True)),
    )  # fmt: skip
    _run(SyncRun.Job.ACTIVITYINFO_DATA, SyncRun.Status.SUCCEEDED)
    _run(SyncRun.Job.ACTIVITYINFO_STRUCTURE, SyncRun.Status.FAILED)
    _run(SyncRun.Job.PARTNER_LINKS, SyncRun.Status.FAILED)
    _run(SyncRun.Job.FORECAST, SyncRun.Status.SUCCEEDED, backtest={"shown": True}, year=2026)
    IndicatorForecast.objects.create(
        master=master, database=database, section_id=section.pk, year=2026, as_of_month=9, value_to_date=4500,
        target=1000000, forecast=6000, low=5500, high=6500, linear=6000, status=IndicatorForecast.Status.SHORT,
        basis="its own past years", computed_at=timezone.now(),
    )  # fmt: skip
    _mode(request.param)
    boss = User.objects.create_superuser(
        username="boss", email="boss@example.org", password="pass-123456-abc"
    )
    editor = User.objects.create_user(
        username="editor", email="editor@example.org", password="pass-123456-abc"
    )
    editor.groups.add(Group.objects.get(name=SECTION_EDITOR))
    User.objects.filter(pk=editor.pk).update(section=section)
    navigation.invalidate()
    world.pd_record = pd
    world.database = database
    world.master = master
    world.mode = request.param
    world.boss = boss
    world.editor = User.objects.get(pk=editor.pk)
    return world


# ------------------------------------------------------------------------------------------ reading a page
def _balanced_end(html: str, start: int, tag: str) -> int:
    """The end of the element opened at ``start`` (nested ones of the same tag counted)."""
    depth, at = 0, start
    opening, closing = re.compile(rf"<{tag}\b"), f"</{tag}>"
    while True:
        next_open = opening.search(html, at + 1)
        next_close = html.find(closing, at + 1)
        if next_close < 0:
            return len(html)
        if next_open is not None and next_open.start() < next_close:
            depth += 1
            at = next_open.start()
            continue
        if depth == 0:
            return next_close + len(closing)
        depth -= 1
        at = next_close


def without_archive(html: str) -> str:
    """The page without what the archive keeps: the menu's archive block, the search's archive entries and
    the help section that describes the archive (on the help pages that show it)."""
    start = html.find('<details class="sidebar__archive"')
    if start >= 0:
        html = html[:start] + html[_balanced_end(html, start, "details") :]
    html = SEARCH_ARCHIVE_GROUP.sub("", html)
    html = SEARCH_ARCHIVE_PAGE.sub("", html)
    anchor = html.find(f'id="{HELP_ARCHIVE[1]}"')
    if anchor >= 0:
        start = html.rfind("<h2", 0, anchor)
        end = html.find("<h2", anchor)
        html = html[:start] + (html[end:] if end >= 0 else "")
    return html


def problems(html: str, where: str) -> list[str]:
    """Where the page names ActivityInfo or links into its archive, with the words around it."""
    kept = without_archive(html)
    found = []
    for match in NAMED.finditer(kept):
        around = " ".join(kept[max(0, match.start() - 120) : match.end() + 80].split())
        found.append(f"{where}: names it: …{around}…")
    for match in ARCHIVE_LINK.finditer(kept):
        around = " ".join(kept[max(0, match.start() - 120) : match.end() + 40].split())
        found.append(f"{where}: links into the archive ({match.group(1)}): …{around}…")
    return found


def _tab_links(html: str, path: str) -> list[str]:
    """The addresses of a page's tab bars (``segmented`` groups and navs) on the same page."""
    links = []
    for bar in re.finditer(r'<(div|nav) class="segmented[^"]*"[^>]*>', html):
        block = html[bar.start() : _balanced_end(html, bar.start(), bar.group(1))]
        for href in re.findall(r'<a\b[^>]*\bhref="([^"#]+)"', block):
            href = href.replace("&amp;", "&")
            if href.startswith("?"):
                href = path + href
            if urlsplit(href).path == path:
                links.append(href)
    return links


def _menu_pages() -> list[str]:
    return [reverse(item.route) for group in navigation.MENU for item in group.items] + [
        reverse(navigation.HELP.route)
    ]


def _walk(client, urls: list[str], seen: set[str]) -> list[str]:
    found = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        response = client.get(url)
        assert response.status_code == 200, f"{url}: {response.status_code}"
        html = response.content.decode()
        found += problems(html, url)
        if "?" not in url:  # each page's tabs, once
            found += _walk(client, _tab_links(html, url), seen)
    return found


# ------------------------------------------------------------------------------------------ the pages
@pytest.mark.parts_may_fail
def test_no_page_of_the_full_menu_names_activityinfo_or_links_into_its_archive(client, ai_world, settings):
    settings.WATCH_ENABLED = True
    client.force_login(ai_world.boss)
    found = _walk(client, _menu_pages(), set())
    assert not found, "\n".join(found)


def test_home_for_a_section_editor_names_no_activityinfo(client, ai_world):
    client.force_login(ai_world.editor)
    html = client.get(reverse("reports:overview")).content.decode()
    assert not problems(html, "Home"), "\n".join(problems(html, "Home"))
    # its tasks hold no HPM reports to comment on (an archive page)
    assert "HPM report" not in html


def test_the_partner_profile_programme_document_and_search_name_no_activityinfo(client, ai_world):
    client.force_login(ai_world.boss)
    pd = ai_world.pd_record
    pages = {
        "partner profile": reverse("reports:partner_profile", args=[pd.partner_id]),
        "programme document": reverse("reports:programme_detail", args=[pd.pk]),
        "programme document (quick view)": reverse("reports:programme_detail", args=[pd.pk]),
        "partners": reverse("reports:partners"),
        "donors": reverse("reports:donors"),
        "search": reverse("reports:search") + "?q=Child",
        "data health": reverse("reports:data_health"),
    }
    found = []
    for name, url in pages.items():
        response = client.get(url, HTTP_HX_REQUEST="true") if "quick view" in name else client.get(url)
        assert response.status_code == 200, f"{name}: {response.status_code}"
        found += problems(response.content.decode(), name)
    assert not found, "\n".join(found)


def test_the_search_keeps_the_archive_entries_labelled_as_the_archive(client, ai_world):
    client.force_login(ai_world.boss)
    html = client.get(reverse("reports:search") + "?q=Child").content.decode()
    titles = [t.strip() for t in re.findall(r'<div class="search-group__title">([^<]*)<', html)]
    assert "ActivityInfo archive: Databases" in titles and "ActivityInfo archive: Master indicators" in titles
    # every group of the archive says so, in use or not; none names ActivityInfo otherwise
    assert "Databases" not in titles and "Master indicators" not in titles
    assert not [t for t in titles if "ActivityInfo" in t and not t.startswith("ActivityInfo archive: ")]
    assert reverse("reports:database_dashboard", args=[ai_world.database.pk]) in html
    pages = client.get(reverse("reports:search") + "?q=databases").content.decode()
    assert '<span class="search-item__hint">ActivityInfo archive</span>' in pages


@pytest.mark.parametrize("who", ["visitor", "staff"])
def test_the_public_landing_page_names_no_activityinfo(client, ai_world, who):
    if who == "staff":
        client.force_login(ai_world.boss)
    html = client.get(reverse("landing")).content.decode()
    assert not problems(html, "landing"), "\n".join(problems(html, "landing"))


def test_the_archive_is_kept_as_it_is(client, ai_world):
    client.force_login(ai_world.boss)
    database, master, pd = ai_world.database, ai_world.master, ai_world.pd_record
    hpm = NeuroReport.objects.get(is_hpm=True, ryear=database.reporting_year)
    neuro = NeuroReport.objects.get(is_hpm=False)
    pages = [
        reverse("reports:databases") + f"?year={database.reporting_year.name}",
        reverse("reports:database_dashboard", args=[database.pk]),
        reverse("reports:database_analytical", args=[database.pk]),
        reverse("reports:database_snapshot", args=[database.pk]),
        reverse("reports:database_map", args=[database.pk]),
        reverse("reports:indicator_detail", args=[database.pk, master.pk]),
        reverse("reports:report_hpm", args=[hpm.pk]),
        reverse("reports:report_dashboard", args=[neuro.pk]),
        reverse("reports:report_analytical", args=[neuro.pk]),
        reverse("insights:forecasts"),
        reverse("reports:partner_activityinfo", args=[pd.partner_id, database.pk]),
    ]
    for url in pages:
        response = client.get(url)
        assert response.status_code == 200, url
    assert "ActivityInfo databases" in client.get(pages[0]).content.decode()
    partner_page = client.get(pages[-1]).content.decode()
    assert "ActivityInfo" in partner_page and "Records reported under the ActivityInfo name" in partner_page
    # the menu's archive block, on every page, with its label
    home = client.get(reverse("reports:overview")).content.decode()
    block = home[home.find('<details class="sidebar__archive"') :]
    label = "Monthly results (ActivityInfo)" if ai_world.mode == "in_use" else "ActivityInfo archive"
    assert f"<span>{label}</span>" in block and "ActivityInfo databases" in block


# ------------------------------------------------------------------------------------------ the help guide
def test_the_help_guide_names_activityinfo_only_where_it_describes_the_archive(client, ai_world):
    from neurodb.help import guide

    client.force_login(ai_world.boss)
    found = []
    for page in guide.ORDER:
        html = client.get(reverse("help:page", args=[page])).content.decode()
        if page == HELP_ARCHIVE[0]:
            assert f'id="{HELP_ARCHIVE[1]}"' in html, "the archive's own help section is gone"
            assert "ActivityInfo archive" in html  # it describes the archive by its menu name
        found += problems(html, f"help/{page}")  # the archive's section left out (without_archive)
    assert not found, "\n".join(found)


def test_the_help_guide_markdown_names_activityinfo_only_in_the_archive_section():
    from neurodb.help import guide

    outside = [
        f"{section.page}#{section.slug}: {section.heading}"
        for page in guide.pages().values()
        for section in page.sections
        if (section.page, section.slug) != HELP_ARCHIVE
        and NAMED.search(section.heading + "\n" + section.text)
    ]
    assert not outside, "\n".join(outside)


# the archive's pages by their own names: its Neuro and HPM reports, its year-end forecasts
ARCHIVE_PAGES = re.compile(r"\bHPM\b|\bNeuro\b|year-end forecasts?", re.I)


def test_the_help_guide_names_the_archives_pages_only_in_the_archive_section():
    """Outside the archive's own section, a help page says "the archive" rather than naming its pages
    (Ask NeuroDB's privacy line among them)."""
    from neurodb.help import guide

    outside = []
    for page in guide.pages().values():
        for section in page.sections:
            if (section.page, section.slug) == HELP_ARCHIVE:
                continue
            text = section.heading + "\n" + section.text
            outside += [
                f"{section.page}#{section.slug}: …{text[max(0, m.start() - 60) : m.end() + 30]}…"
                for m in ARCHIVE_PAGES.finditer(text)
            ]
    assert not outside, "\n".join(outside)


def test_the_whats_new_help_lists_no_kind_of_the_archive():
    """What's new lists no change of the archive (graph.news.ARCHIVE_KINDS: its databases, master indicators
    and Neuro and HPM reports): its help section names none of them among the things it lists."""
    from neurodb.graph import news
    from neurodb.help import guide

    section = guide.section("watch", "whats-new")
    assert section is not None
    words = {"database", "master indicator", "Neuro", "HPM"}
    assert len(news.ARCHIVE_KINDS) == 3  # the words above name every one of them
    named = sorted(w for w in words if re.search(rf"\b{w}s?\b", section.text, re.I))
    assert not named, named


# ------------------------------------------------------------------------------------------ Ask NeuroDB
def test_ask_neurodb_is_told_activityinfo_is_an_archive_not_to_bring_up(ai_world):
    from neurodb.assistant import agent

    assert ASK_ARCHIVE_LINE in agent.SYSTEM_PROMPT
    # its example link opens a regular page, never an archive page
    assert "/databases/" not in agent.SYSTEM_PROMPT
    # the year line never says ActivityInfo is in use, nor that children reached counts it
    line = agent.year_line()
    assert "in use" not in line and "counts them" not in line and "larger of the two" not in line


# ------------------------------------------------------------------------------------------ the figures
AI_CHILDREN = "4,514"  # ActivityInfo's children of 2026 (500 a month to September, 14 more): never on a page


def test_no_figure_can_take_activityinfo_children_any_more(ai_world):
    from dataclasses import fields

    from neurodb.donors import services as donors
    from neurodb.reports import brief, overview

    assert "reporting_year" not in {f.name for f in fields(overview.Scope)}
    assert not {"reporting_year", "previous_reporting_year"} & {f.name for f in fields(brief.Scope)}
    assert "ai_data" not in donors.SOURCE_JOBS and "ai_data" not in overview.FRESHNESS_JOBS
    impact = overview.build(overview.Scope(year=2026), cache=False)["impact"]
    assert impact["children_reached"] == impact["children_etools"] == 900
    assert not [key for key in impact if "activityinfo" in key]
    assert "activityinfo" not in impact["monthly"]
    assert all("activityinfo" not in g for g in impact["by_governorate"])
    assert not NAMED.search(impact["source"])


def test_the_pages_show_etools_children_alone(client, ai_world):
    client.force_login(ai_world.boss)
    for url in (reverse("reports:country_overview"), reverse("reports:overview"), reverse("reports:brief")):
        html = client.get(url).content.decode()
        assert ">900<" in html and AI_CHILDREN not in html, url


def test_the_management_brief_compares_etools_alone(ai_world):
    from neurodb.reports import brief

    built = brief.build(brief.Scope(year=2026), cache=False)
    tile = next(t for t in built["headline"]["tiles"] if t["key"] == "children")
    assert tile["value"] == 900
    assert not NAMED.search(str(built)), NAMED.search(str(built))


def test_the_landing_page_figures_come_from_etools_alone(ai_world):
    from neurodb.web import services as web

    figures = web.public_highlights()
    gone = {
        "databases",
        "indicators",
        "records",
        "governorates",
        "sections",
        "partners",
        "years",
        "last_update",
    }
    assert not gone & set(figures)
    assert not NAMED.search(str(figures))


# ------------------------------------------------------------------------------------------ what's new
def test_whats_new_leaves_the_archive_out(client, ai_world, no_ai):  # noqa: F811 - the fixture above
    from neurodb.assistant import hub_tools
    from tests.graph.conftest import build_hub

    build_hub()
    ai_world.ai(2026, "Child Protection", {9: 50})  # a new ActivityInfo database: news only for the archive
    build_hub()
    client.force_login(ai_world.boss)
    found = []
    for url in (
        reverse("graph:whats_new") + "?days=7&section=&all=1",
        reverse("graph:whats_new") + "?days=7&section=&kind=database",
        reverse("reports:overview"),
        reverse("reports:country_overview"),
    ):
        found += problems(client.get(url).content.decode(), url)
    assert not found, "\n".join(found)
    # Ask NeuroDB's lookup still tells the archive's changes, when asked
    told = hub_tools.whats_new(include_minor=True)
    assert NAMED.search(str(told))


# ------------------------------------------------------------------------------------------ the daily review
def test_the_daily_review_raises_nothing_about_activityinfo(client, ai_world):
    from neurodb.review import checks
    from neurodb.review import services as review

    PartnerLink.objects.create(label="Not linked yet", partner=None, method="", records=4)
    drafts, _errors = checks.run_all(checks.Context(today=datetime.date(2026, 10, 15), previous=None))
    said = [
        f"{d.key}: {d.title}"
        for d in drafts
        if NAMED.search(f"{d.title} {d.detail} {d.evidence}")
        or d.key.split(":", 1)[-1] in ("ai_structure", "ai_data", "partner_links", "forecast")
    ]
    assert not said, said
    review.run(date=datetime.date(2026, 10, 15), narrate=False)
    client.force_login(ai_world.boss)
    for url in (reverse("reports:daily_review"), reverse("reports:overview")):
        assert not problems(client.get(url).content.decode(), url)


# ------------------------------------------------------------------------------------------ For you
def test_for_you_raises_nothing_about_activityinfo(client, ai_world, site_today, settings):
    from neurodb.watch import detectors
    from neurodb.watch.detectors import calendar, concerns, system

    settings.WATCH_ENABLED = True
    for day in (datetime.date(2026, 10, 15), datetime.date(2026, 12, 20)):  # the forecasts; the rollover
        site_today.set(day)
        _run(SyncRun.Job.FORECAST, SyncRun.Status.SUCCEEDED, backtest={"shown": True}, year=2026)
        activityinfo.invalidate()
        results = detectors.collect(detectors.Context.make())
        said = [
            f"{c.key}: {c.title}"
            for result in results
            for c in result.candidates
            if NAMED.search(f"{c.title} {c.detail} {c.evidence}")
            or ARCHIVE_LINK.search(f'href="{c.url}"')
            or c.key.startswith(("forecast:", "system:job_failed:ai_", "system:job_failed:partner_links"))
        ]
        assert not said, said
    # what For you still followed closes without naming ActivityInfo
    ctx = detectors.Context.make()
    item = type("Item", (), {"evidence": {}})
    closes = {
        **concerns.forecasts_resolved(ctx, [type("I", (item,), {"key": "forecast:2026:1"})()]),
        **calendar.rollovers_resolved(ctx, [type("I", (item,), {"key": "rollover:2027"})()]),
        **system.resolved(ctx, [type("I", (item,), {"key": "system:job_failed:ai_data"})()]),
        **system.resolved(ctx, [type("I", (item,), {"key": "system:no_current_year"})()]),
    }
    assert len(closes) == 4 and not [c.reason for c in closes.values() if NAMED.search(c.reason)]
    client.force_login(ai_world.boss)
    page = client.get(reverse("watch:for_you")).content.decode()
    assert not problems(page, "For you") and "year-end forecast" not in page


# ------------------------------------------------------------------------------------------ the country programme
def test_the_country_programme_counts_no_activityinfo_source(client, ai_world):
    from neurodb.cpd import services as cpd
    from neurodb.cpd.models import CountryProgramme, Indicator, Outcome, Output
    from neurodb.cpd.models import Link as CPDLink

    cycle = CountryProgramme.objects.create(
        name="Lebanon CP 2026-2028", start_year=2026, end_year=2028, current=True, etools_name="LEB CP 2026"
    )
    outcome = Outcome.objects.create(programme=cycle, code="1", title="Children are protected", sections="")
    output = Output.objects.create(outcome=outcome, code="1.1", title="Case management")
    indicator = Indicator.objects.create(output=output, code="1.1.1", title="Children in care", target=30000)
    CPDLink.objects.create(
        indicator=indicator, kind="activityinfo", year=2026, master=ai_world.master,
        label="Child Protection 2026 — Children reached (total)", confirmed=True,
    )  # fmt: skip
    detail = cpd.indicator_detail(indicator, datetime.date(2026, 10, 15))
    assert detail.years.get(2026) is None or detail.years[2026]["value"] is None
    client.force_login(ai_world.boss)
    for url in (reverse("cpd:dashboard"), reverse("cpd:indicator", args=[indicator.pk])):
        assert not problems(client.get(url).content.decode(), url), url


# ------------------------------------------------------------------------------------------ Data health
def test_data_health_readiness_shows_no_activityinfo_measure(client, ai_world):
    """A check stored before the change (with its ActivityInfo sources and the verdicts that rest on them)
    shows without them; a check run now measures none."""
    from neurodb.insights.management.commands import ml_readiness

    _run(
        SyncRun.Job.ML_READINESS,
        SyncRun.Status.SUCCEEDED,
        readiness={
            "measured_at": "2026-10-12T05:00:00+00:00",
            "sections": [
                {"key": "activityinfo", "label": "ActivityInfo monthly reports", "error": "", "metrics": [
                    {"key": "reports", "label": "Activity reports", "value": 20, "display": "20", "note": ""}
                ]},
                {"key": "indicators", "label": "Master indicators across years", "error": "", "metrics": []},
                {"key": "etools", "label": "eTools programme documents and partner reporting", "error": "",
                 "metrics": [
                    {"key": "pds", "label": "Programme documents (signed, active or ended)", "value": 1,
                     "display": "1", "note": ""},
                    {"key": "partner_labels_linked", "label": "ActivityInfo partner names linked to an eTools "
                     "partner", "value": 1.0, "display": "100%", "note": ""},
                ]},
            ],
            "verdicts": [
                {"key": "falling_behind", "question": "Which indicators or activities may be falling behind?",
                 "method": "Year-end forecast", "status": "not_yet", "status_label": "Not yet", "checks": [
                    {"ok": False, "text": "Years with monthly reports: 1 (needs 2)"}]},
                {"key": "coverage", "question": "Where are the gaps?", "method": "Counts by place",
                 "status": "not_yet", "status_label": "Not yet", "checks": [
                    {"ok": False, "text": "ActivityInfo records linked to eTools partners: 0% (needs 70%)"},
                    {"ok": True, "text": "PDs with locations: 100% (needs 50%)"}]},
            ],
        },
    )  # fmt: skip
    client.force_login(ai_world.boss)
    html = client.get(reverse("reports:data_health")).content.decode()
    assert not problems(html, "data health"), "\n".join(problems(html, "data health"))
    assert "Programme documents (signed, active or ended)" in html and "falling behind" not in html
    assert "Years with monthly reports" not in html and "PDs with locations" in html
    result = ml_readiness.run("test").details["readiness"]
    assert {s["key"] for s in result["sections"]}.isdisjoint({"activityinfo", "indicators"})
    assert {v["key"] for v in result["verdicts"]}.isdisjoint({"falling_behind", "patterns"})
    assert not NAMED.search(str(result))
