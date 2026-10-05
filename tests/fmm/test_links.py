"""Monitoring insights linked from and to the rest of NeuroDB (§E, stage 8a): the partner and PD panels,
the overview and assurance links, the action points page, the knowledge hub and What's new, the Watch
check ``fm_follow_up``, the section source and the visit page's block "what the partner reported, and
other visits"."""

from __future__ import annotations

import datetime
import importlib
import re

import pytest
from django.urls import reverse
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.cpd.models import CountryProgramme, Outcome, Output
from neurodb.datamart import fm
from neurodb.datamart import models as dm
from neurodb.datamart import services as datamart_services
from neurodb.fmm import metrics, refresh, services
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import Visit
from neurodb.fmm.scope import Scope
from neurodb.graph.models import Change, Edge, Entity
from neurodb.knowledge.models import Document
from neurodb.knowledge.models import Link as KnowledgeLink
from neurodb.watch import detectors, memory, sections
from neurodb.watch.detectors import Context, fieldmonitoring
from neurodb.watch.models import DetectorSetting, WatchItem
from tests.fmm.conftest import CANARIES, LEAD, MEMBER, MEMBER_EMAIL
from tests.fmm.test_pages import _section_editor, visible
from tests.graph.conftest import build_hub

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
YEAR = 2026
K = Entity.Kind


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    """ "This year" is 2026 on every page and panel, whatever day the tests run."""
    monkeypatch.setattr(scope_module, "_today", lambda today: today or TODAY)
    monkeypatch.setattr("neurodb.reports.views._this_year", lambda: YEAR)


def _text(response) -> str:
    return " ".join(visible(response.content.decode()).split())


def _hrefs(html: str, path: str) -> list[str]:
    return [h.replace("&amp;", "&") for h in re.findall(rf'href="({re.escape(path)}\?[^"]*)"', html)]


# ------------------------------------------------------------------------------------- partner panel
def test_the_partner_panel_equals_monitoring_insights(built, fm_world):
    amel = fm_world.partners["amel"]
    panel = services.partner_summary(amel.pk, YEAR)
    scope = Scope.from_params({"partner": str(amel.pk), "year": str(YEAR), "section": ""})
    kpis = metrics.kpis(scope)
    assert panel["visits"] == kpis["visits"] == 3  # 1722, 1725 (cancelled) and 1727
    assert panel["avg_quality"] == kpis["avg_quality"] and panel["scored"] == kpis["scored"]
    assert panel["off_track"] == scope.visits().filter(rating="off_track").count() == 1
    follow_up = metrics.action_points(scope)
    assert (panel["action_points_open"], panel["action_points_overdue"]) == (
        follow_up["open"],
        follow_up["overdue"],
    )
    assert panel["action_points_open"] == 1  # 8001, raised from visit 1722
    assert panel["last"]["key"] == "1727" and panel["last"]["end_date"] == datetime.date(2026, 8, 1)
    assert panel["url"] == f"/fmm/?partner={amel.pk}&year={YEAR}&section="
    # a partner no visit monitored has no panel
    from neurodb.partnerships.models import PartnerOrganization

    other = PartnerOrganization.objects.create(etl_id="9", name="No visits", short_name="NV")
    assert services.partner_summary(other.pk, YEAR) is None


def test_the_partner_page_shows_the_panel_and_a_section_user_following_it_sees_its_figures(
    built, fm_world, client
):
    amel = fm_world.partners["amel"]
    client.force_login(_section_editor(fm_world.section))
    response = client.get(reverse("reports:partner_profile", args=[amel.pk]))
    html = response.content.decode()
    assert response.status_code == 200
    text = _text(response)
    assert "Monitoring insights" in text and f"3 FM visits in {YEAR}" in text
    links = _hrefs(html, "/fmm/")
    assert links and all("section=" in link for link in links)
    # the user's own section (Education) would show 0 of amel's visits; the link turns it off
    page = _text(client.get(links[0]))
    assert "Monitoring visits 3" in page and "Your section" not in page


def test_the_panels_are_hidden_when_monitoring_insights_is_off(built, fm_world, client_viewer, settings):
    settings.FMM_ENABLED = False
    html = client_viewer.get(reverse("reports:partner_profile", args=[fm_world.partners["amel"].pk]))
    assert "/fmm/" not in html.content.decode()
    pd = fm_world.pds["amended"]
    assert (
        "/fmm/" not in client_viewer.get(reverse("reports:programme_detail", args=[pd.pk])).content.decode()
    )


# ------------------------------------------------------------------------------------------ PD panel
def _pd_extras(fm_world):
    """A partner-reported indicator, a TPM activity, a staff trip (with its traveller) and a knowledge
    base document for the amended programme document."""
    pd, amel = fm_world.pds["amended"], fm_world.partners["amel"]
    dm.PDIndicator.objects.create(
        datamart_id=1,
        source_id=100,
        intervention=pd,
        pd_reference_number=pd.number,
        title="# of children in learning support",
        target_numerator=1000,
        display_type="number",
    )
    dm.ReportedIndicator.objects.create(
        datamart_id=1,
        partner=amel,
        intervention=pd,
        pd_reference_number=pd.number,
        progress_report="PR-1",
        report_number="QPR1",
        report_type="QPR",
        report_status="Accepted",
        period_start=datetime.date(2026, 4, 1),
        period_end=datetime.date(2026, 6, 30),
        indicator="# of children in learning support",
        target="1000",
        location="Zahle",
        achievement_in_period="600",
        total_cumulative_progress="600",
    )
    dm.TPMActivity.objects.create(
        datamart_id=1,
        intervention=pd,
        partner=amel,
        visit_reference_number="TPM-2026-7",
        task_reference_number="TPM-2026-7/1",
        status="completed",
        date=datetime.date(2026, 7, 20),
    )
    dm.ProgrammaticVisit.objects.create(
        datamart_id=1,
        intervention=pd,
        partner=amel,
        travel_reference_number="TRV-2026-31",
        travel_type="Programmatic visit",
        date=datetime.date(2026, 8, 10),
        primary_traveler="Hassan Plantedtraveller",
        location_name="Zahle",
    )
    note = Document.objects.create(
        title="Learning support review", text="Notes.", status=Document.Status.READY
    )
    KnowledgeLink.objects.create(
        document=note, kind="programme_document", object_id=pd.pk, label=pd.number, origin="detected"
    )
    return pd


def test_the_pd_panel_equals_monitoring_insights_and_the_fm_series(built, fm_world):
    pd = fm_world.pds["amended"]
    panel = services.pd_summary(pd.pk, YEAR)
    scope = Scope.from_params({"pd": str(pd.pk), "year": str(YEAR), "section": ""})
    assert panel["visits"] == metrics.kpis(scope)["visits"] == sum(q["visits"] for q in panel["quarters"])
    assert panel["visits"] == scope.visits().count() > 0
    assert [q["planned"] for q in panel["quarters"]] == [1, 1, 0, 0] and panel["planned"] == 2
    assert panel["url"] == f"/fmm/?pd={pd.pk}&year={YEAR}&section="
    assert [v["key"] for v in panel["latest"]] == list(
        Visit.objects.filter(pd_ids__contains=[pd.pk])
        .order_by("-end_date", "-pk")
        .values_list("key", flat=True)[:3]
    )
    # the PD page's visits table gains a field monitoring column: one per visit, by its end date year
    rows = datamart_services.visits_by_year(pd)
    expected = fm.visits_by_year(dm.MonitoringFinding.objects.filter(intervention=pd))
    assert {r["year"]: r["fm"] for r in rows if r["fm"]} == expected
    # a PD that no visit monitored and that plans none has no panel
    from neurodb.partnerships.models import PCA

    quiet = PCA.objects.create(
        etl_id="99", partner=fm_world.partners["amel"], number="LEB/PCA2026900/PD2026901"
    )
    assert services.pd_summary(quiet.pk, YEAR) is None


def test_the_pd_page_shows_the_panel_and_what_the_partner_reported(built, fm_world, client):
    pd = _pd_extras(fm_world)
    client.force_login(_section_editor(fm_world.section))
    response = client.get(reverse("reports:programme_detail", args=[pd.pk]))
    html = response.content.decode()
    text = _text(response)
    assert response.status_code == 200
    assert "Field monitoring visits" in text and "Planned in eTools" in text
    assert "reported for Jun 2026" in text  # the partner's own latest report, next to the field rating
    assert "TPM-2026-7/1" in html and "TRV-2026-31" in html
    assert "Hassan Plantedtraveller" not in html  # never the traveller's name
    links = _hrefs(html, "/fmm/")
    assert links and all("section=" in link for link in links)
    panel = services.pd_summary(pd.pk, YEAR)
    page = _text(client.get(links[0]))
    assert f"Monitoring visits {panel['visits']}" in page


def test_the_visit_page_shows_what_the_partner_reported_and_other_visits(built, fm_world, client_viewer):
    _pd_extras(fm_world)
    html = client_viewer.get(reverse("fmm:visit", args=["1727"])).content.decode()
    text = " ".join(visible(html).split())
    assert "What the partner reported, and other visits to the same programme document" in text
    assert "reported for Jun 2026" in text
    assert "TPM-2026-7/1" in html and "TRV-2026-31" in html and "Learning support review" in html
    assert "Hassan Plantedtraveller" not in html
    # the other FM visits to the PD within 90 days, never the visit itself
    blocks = services.pd_context(
        Visit.objects.get(key="1727").pd_ids, datetime.date(2026, 8, 1), exclude_key="1727"
    )
    keys = {v["key"] for b in blocks for v in b["visits"]}
    assert "1727" not in keys and "1722" in keys  # 12 May is within 90 days of 1 August
    # the knowledge base falls back to the partner's documents when none mentions the PD
    KnowledgeLink.objects.all().delete()
    partner_note = Document.objects.create(title="Amel annual note", text="x", status=Document.Status.READY)
    KnowledgeLink.objects.create(
        document=partner_note,
        kind="partner",
        object_id=fm_world.partners["amel"].pk,
        label="Amel",
        origin="detected",
    )
    again = services.pd_context([fm_world.pds["amended"].pk], datetime.date(2026, 8, 1))
    assert [d.title for d in again[0]["documents"]] == ["Amel annual note"]


# --------------------------------------------------------------------------- overview and assurance
def test_the_overview_links_with_its_year_sections_and_governorate(built, client_viewer, reporting_year):
    from neurodb.reports import overview

    response = client_viewer.get(
        reverse("reports:overview"), {"year": "2026", "section": "Education", "governorate": "Beqaa"}
    )
    html = response.content.decode()
    links = _hrefs(html, "/fmm/")
    assert links == ["/fmm/?year=2026&section=Education&governorate=Beqaa"]
    scope = Scope.from_params(dict(re.findall(r"([a-z]+)=([^&]*)", links[0].split("?")[1])))
    assert scope.sections == ("Education",) and scope.year == 2026
    assert scope.governorate == overview.governorate_key("Bekaa")  # the overview's name, normalised
    bekaa = Scope.from_params({"year": "2026", "section": "", "governorate": "Beqaa"}).visits()
    assert set(bekaa.values_list("key", flat=True)) == {
        "1722",
        "1723",
        "1725",
        "1727",
        "1728",
    }  # 1728 through its site
    # without a section the link still says so, so a user's own section does not come back
    html = client_viewer.get(reverse("reports:overview"), {"year": "2026"}).content.decode()
    assert _hrefs(html, "/fmm/") == ["/fmm/?year=2026&section="]
    # the overview's own figures do not move (its link to the field monitoring page included)
    data = overview.build(overview.Scope(year=2026, reporting_year=reporting_year, today=TODAY), cache=False)
    assert data["delivery"]["assurance"]["field_monitoring_visits"] == 8
    assert 'href="/field-monitoring/?year=2026"' in html


def test_the_assurance_column_counts_completed_programmatic_visits_per_row_partner(
    built, fm_world, client_viewer
):
    amel, mercy = fm_world.partners["amel"], fm_world.partners["mercy"]
    dm.PartnerHACTYear.objects.create(datamart_id=2, partner=mercy, year=2026, pv_required=3, pv_completed=1)
    hact = datamart_services.hact_compliance(2026)
    rows = {row.partner_id: row for row in hact["rows"]}
    counted = fm.programmatic_visits_by_partner(2026)
    # amel: 1722 and 1727 completed and programmatic (1725 cancelled is not); mercy: 1723 and 1726
    assert rows[amel.pk].fm_pv == counted[amel.pk] == 2
    assert rows[mercy.pk].fm_pv == counted[mercy.pk] == 2
    assert rows[amel.pk].fm_url == f"/fmm/?partner={amel.pk}&programmatic=1&year=2026&section="
    html = client_viewer.get(reverse("reports:assurance"), {"hact_year": "2026"}).content.decode()
    assert "FM programmatic visits (NeuroDB)" in html
    assert f'href="/fmm/?partner={amel.pk}&amp;programmatic=1&amp;year=2026&amp;section="' in html


def test_the_action_points_page_shows_the_visit(built, fm_world, client_viewer):
    links = services.visits_for_action_points([ap.pk for ap in vars(fm_world.action_points).values()])
    assert links[fm_world.action_points.by_id.pk] == ("1722", "Visit 1722")
    assert links[fm_world.action_points.by_reference.pk] == ("1723", "Visit 1723")
    assert fm_world.action_points.unlinked.pk not in links
    html = client_viewer.get(reverse("reports:action_points"), {"module": "fm"}).content.decode()
    assert 'hx-get="/fmm/visits/1722/"' in html and ">Visit 1722</a>" in html


def test_the_action_points_page_shows_the_visit_whatever_the_case_of_the_module(
    built, fm_world, client_viewer
):
    """The refresh matches FM action points by ``related_module`` in any case; the page's link too."""
    dm.ActionPoint.objects.filter(pk=fm_world.action_points.by_id.pk).update(related_module="FM")
    html = client_viewer.get(reverse("reports:action_points")).content.decode()
    assert 'hx-get="/fmm/visits/1722/"' in html and ">Visit 1722</a>" in html


def test_the_pd_panel_lists_the_country_programme_outputs_and_the_tpm_status_date(built, fm_world, client):
    """Block 11a on the PD page: the CPD outputs the PD contributes to (``output_matches``, linked to
    the country programme dashboard), and a TPM status dated by the eTools sync, not the visit date."""
    pd = _pd_extras(fm_world)
    output = _cp_output()
    pd.cp_outputs = ["2.2 INCREASED ACCESS TO EDUCATION", "9.9 Something else"]
    pd.save(update_fields=["cp_outputs"])
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="tpm_activities",
        status=SyncRun.Status.SUCCEEDED,
        started_at=datetime.datetime(2026, 10, 4, 3, 0, tzinfo=datetime.UTC),
        finished_at=datetime.datetime(2026, 10, 4, 3, 30, tzinfo=datetime.UTC),
    )
    block = services.pd_summary(pd.pk, YEAR)["context"][0]
    assert block["outputs"] == [
        {
            "code": "2.2",
            "title": output.title,
            "url": f"{reverse('cpd:dashboard')}?cycle={output.outcome.programme_id}",
        }
    ]
    client.force_login(_section_editor(fm_world.section))
    text = _text(client.get(reverse("reports:programme_detail", args=[pd.pk])))
    assert "Country programme outputs Output 2.2" in text
    assert "Completed as of eTools sync 4 Oct 2026" in text and "as of 20 Jul 2026" not in text
    # a PD with no matching CP output lists none
    pd.cp_outputs = ["9.9 Something else"]
    pd.save(update_fields=["cp_outputs"])
    assert services.pd_summary(pd.pk, YEAR)["context"][0]["outputs"] == []


# ------------------------------------------------------------------------------------ knowledge hub
def _cp_output() -> Output:
    cycle = CountryProgramme.objects.create(
        name="Lebanon CP 2026-2028", start_year=2026, end_year=2028, current=True
    )
    outcome = Outcome.objects.create(programme=cycle, code="2", title="Learning")
    return Output.objects.create(outcome=outcome, code="2.2", title="Increased access to education")


def _recent(days: int) -> datetime.date:
    return timezone.localdate() - datetime.timedelta(days=days)


def _visit(key: str, rating: str, ended: datetime.date, **fields) -> Visit:
    return Visit.objects.create(
        key=key, label=f"Visit {key}", end_date=ended, rating=rating, status_group="reported",
        refreshed_at=timezone.now(), **fields,
    )  # fmt: skip


def test_hub_visits_have_their_links_and_no_text_or_people(built, fm_world):
    output = _cp_output()
    Visit.objects.update(end_date=_recent(10))  # every visit within the hub's 24 months
    build_hub()
    visit = Entity.objects.get(kind=K.FM_VISIT, key="1722")
    assert visit.name.startswith("Visit 1722 · AMEL · ")
    assert set(visit.attrs) == {"date", "status_group", "rating", "quality", "urgency_band"}
    assert (
        visit.lookup == {"tool": "fm_visit", "args": {"visit": "1722"}} and visit.url == "/fmm/visits/1722/"
    )
    edges = {
        (e.relation, e.target.kind, e.target.key)
        for e in Edge.objects.filter(source=visit).select_related("target")
    }
    amended, amel = fm_world.pds["amended"], fm_world.partners["amel"]
    assert ("about", K.PROGRAMME, str(amended.pk)) in edges
    assert ("about", K.PARTNER, str(amel.pk)) in edges
    assert ("about", K.CPD_OUTPUT, str(output.pk)) in edges  # "2.2 INCREASED ACCESS TO EDUCATION"
    places = {(kind, key) for relation, kind, key in edges if relation == "takes_place_in"}
    assert {kind for kind, _key in places} == {K.GOVERNORATE, K.DISTRICT}
    education = Entity.objects.get(kind=K.FM_VISIT, key="1726")
    assert ("in_section", K.SECTION, str(fm_world.section.pk)) in {
        (e.relation, e.target.kind, e.target.key) for e in Edge.objects.filter(source=education)
    }
    # structured facts only: no narrative, answer, visit lead or team anywhere in the hub's visits
    for e in Entity.objects.filter(kind=K.FM_VISIT):
        blob = " ".join([e.name, e.aliases, e.description, str(e.attrs), str(e.lookup)])
        for canary in (*CANARIES, LEAD, MEMBER, MEMBER_EMAIL, "Classes held as planned"):
            assert canary not in blob


def test_visits_older_than_the_hub_months_are_left_out(built, settings):
    settings.FMM_HUB_MONTHS = 24
    Visit.objects.update(end_date=_recent(10))
    Visit.objects.filter(key="1722").update(end_date=_recent(800))
    build_hub()
    keys = set(Entity.objects.filter(kind=K.FM_VISIT).values_list("key", flat=True))
    assert "1722" not in keys and "1726" in keys


def test_the_first_build_with_visits_marks_none_notable_then_only_recent_concerns(built, settings):
    settings.FMM_NEWS_DAYS = 30
    Visit.objects.update(end_date=_recent(5))
    settings.FMM_ENABLED = False
    build_hub()  # the hub's first build, without visits
    settings.FMM_ENABLED = True
    build_hub()  # the first build that brings visits: recorded, none of them news
    added = Change.objects.filter(kind=K.FM_VISIT, op=Change.Op.ADDED)
    assert added.count() == Visit.objects.count() and not added.filter(notable=True).exists()
    assert Visit.objects.filter(rating="off_track").exists()  # an off-track recent visit was not told
    _visit("9001", "off_track", _recent(5))
    _visit("9002", "constrained", _recent(10))
    _visit("9003", "off_track", _recent(45))  # ended before FMM_NEWS_DAYS: a late-synced backlog
    _visit("9004", "on_track", _recent(2))
    build_hub()
    notable = dict(
        Change.objects.filter(kind=K.FM_VISIT, op=Change.Op.ADDED, key__startswith="900").values_list(
            "key", "notable"
        )
    )
    assert notable == {"9001": True, "9002": True, "9003": False, "9004": False}


def test_a_rating_change_is_news_and_a_quality_change_is_not(built):
    Visit.objects.update(end_date=_recent(5))
    build_hub()
    build_hub()
    Change.objects.all().delete()
    Visit.objects.filter(key="1726").update(quality_score=12, urgency_band="red", status_group="cancelled")
    build_hub()
    assert not Change.objects.filter(kind=K.FM_VISIT, key="1726").exists()
    Visit.objects.filter(key="1726").update(rating="off_track")
    build_hub()
    change = Change.objects.get(kind=K.FM_VISIT, key="1726", op=Change.Op.CHANGED)
    assert change.notable and change.fields == {"rating": ["on_track", "off_track"]}


def test_the_graph_migration_alters_both_kind_fields():
    migration = importlib.import_module("neurodb.graph.migrations.0003_fm_visit_kind").Migration
    altered = {(op.model_name, op.name) for op in migration.operations}
    assert altered == {("entity", "kind"), ("change", "kind")}
    for op in migration.operations:
        assert ("fm_visit", "Field monitoring visit") in op.field.choices


# --------------------------------------------------------------------------------- Watch and sections
def _pass(day: datetime.date = TODAY) -> memory.Outcome:
    now = datetime.datetime.combine(day, datetime.time(6, 0), tzinfo=datetime.UTC)
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        status=SyncRun.Status.SUCCEEDED,
        started_at=now - datetime.timedelta(hours=2),
        finished_at=now - datetime.timedelta(hours=1),
    )
    return memory.run(Context.make(now=now), [fieldmonitoring.FM_FOLLOW_UP])


def test_fm_follow_up_is_a_trial_check_on_the_datamart_and_the_refresh():
    check = detectors.get("fm_follow_up")
    assert check is fieldmonitoring.FM_FOLLOW_UP and check.default_mode == detectors.TRIAL
    assert set(check.source_jobs) == {SyncRun.Job.ETOOLS_DATAMART, SyncRun.Job.FMM_REFRESH}
    assert all(job in SyncRun.Job.values for job in check.source_jobs)


def test_fm_follow_up_opens_for_a_concern_without_an_action_point_and_closes_when_one_is_linked(
    built, fm_world
):
    _pass()
    assert DetectorSetting.objects.get(detector="fm_follow_up").mode == DetectorSetting.Mode.TRIAL
    items = {i.key: i for i in WatchItem.objects.filter(detector="fm_follow_up")}
    # 1727: on track, but its HACT Q1 (answered once for the visit) is Constrained, ended 1 Aug, no
    # action point. 1723 is constrained too, but has one; 1722 ended more than 120 days ago.
    assert set(items) == {"fm:followup:1727"}
    item = items["fm:followup:1727"]
    assert item.state == WatchItem.State.OPEN and item.severity == WatchItem.Severity.WARNING
    assert item.title == "Visit 1727 (AMEL) rated Constrained on 1 Aug 2026 has no follow-up action point"
    assert (item.entity_kind, item.entity_key, item.url) == ("fm_visit", "1727", "/fmm/visits/1727/")
    assert item.evidence["source_job"] == "fmm_refresh"
    assert item.evidence["records"] == [
        {"label": "Visit 1727", "date": "2026-08-01", "value": "Constrained", "url": "/fmm/visits/1727/"}
    ]
    assert item.evidence["numbers"] == {"days_open": 65}
    for canary in (*CANARIES, LEAD, MEMBER):
        assert canary not in str(item.evidence) + item.title + item.detail
    # an action point raised from the visit closes it
    dm.ActionPoint.objects.create(
        datamart_id=8100, related_module="fm", related_module_id=1727, status="open"
    )
    refresh.run(triggered_by="test", today=TODAY)
    _pass()
    item.refresh_from_db()
    assert item.state == WatchItem.State.CLOSED and "action point" in item.close_reason


def test_fm_follow_up_is_critical_when_off_track_for_a_month_and_closes_when_the_rating_changes(built):
    Visit.objects.filter(key="1727").update(hact_q1="off_track")
    _pass()
    item = WatchItem.objects.get(key="fm:followup:1727")
    assert item.severity == WatchItem.Severity.CRITICAL and "rated Off track on 1 Aug 2026" in item.title
    Visit.objects.filter(key="1727").update(hact_q1="on_track")
    _pass()
    item.refresh_from_db()
    assert item.state == WatchItem.State.CLOSED and item.close_kind == WatchItem.CloseKind.CHANGED


def test_fm_follow_up_finds_nothing_when_monitoring_insights_is_off(built, settings):
    settings.FMM_ENABLED = False
    ctx = Context.make(now=datetime.datetime.combine(TODAY, datetime.time(6, 0), tzinfo=datetime.UTC))
    assert list(fieldmonitoring.visits_without_follow_up(ctx)) == []


def test_the_section_source_reads_the_visits_own_section_names(built):
    Visit.objects.filter(key="1722").update(sections_from="activity", section_names=["Health & Nutrition"])
    assert sections._fm_names in sections.SOURCES and sections._fm_names() == ["Health & Nutrition"]
