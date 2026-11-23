"""The daily review's three newer checks and their trial: field monitoring rated off track while the partner
reports the programme document on track, audit findings with no follow-up, the HACT assurance all year; and
how a check in trial marks its findings Trial (on the Daily review page and on Home) and sends none to NeuroDB
Watch until an administrator switches it on (Watch then imports them, tests/watch/test_concerns.py).

Every name here is made up: partners "Example Partner <n>", references and visits numbered for the test."""

from __future__ import annotations

import datetime
import itertools
import json
from decimal import Decimal

import pytest
from django.db import models
from django.urls import reverse
from django.utils import timezone

from neurodb.datamart import fm
from neurodb.datamart import models as dm
from neurodb.datamart import services as datamart
from neurodb.partnerships.models import PCA, PartnerOrganization
from neurodb.review import checks, services
from neurodb.review.models import CheckSetting, ReviewFinding
from neurodb.watch.detectors import DAILY, Context, money
from tests.web.parts import HX

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 7, 1)  # half way through the programme documents of 2026
NEW_CHECKS = {"monitoring_disagrees", "audit_no_follow_up", "hact_assurance"}
NARRATIVE = "Narrative written by the monitor, never copied"
VISIT_LEAD = "Visit Lead Plantedfour"
AUDITOR = "Example Audit Firm Plantedfive"
ASSIGNEE = "Assignee Plantedsix"
_ids = itertools.count(9001)


def days_ago(n: int, today: datetime.date = TODAY) -> datetime.date:
    return today - datetime.timedelta(days=n)


def partner(n: int = 1) -> PartnerOrganization:
    return PartnerOrganization.objects.create(
        etl_id=f"x{n}",
        name=f"Example Partner {n}",
        partner_type="Civil Society Organization",
        vendor_number=f"VX{n}",
    )


def programme(org, number: str, reached: int | None, *, status="active", target=1000, **fields) -> PCA:
    """An active PD of 2026 with one indicator of ``target``, reported at ``reached`` by the end of June (500 of
    1,000 is on track on 1 July, 300 is off track, 1,000 is over target); none reported when None."""
    values = {
        "etl_id": str(next(_ids)),
        "partner": org,
        "partner_name": org.name,
        "number": number,
        "title": f"Example services {number[-4:]}",
        "status": status,
        "start": datetime.date(2026, 1, 1),
        "end": datetime.date(2026, 12, 31),
        "section_names": ["Education"],
    }
    values.update(fields)
    pd = PCA.objects.create(**values)
    if reached is None:
        return pd
    source_id = next(_ids)
    title = f"# of children in example classes {number[-4:]}"
    dm.PDIndicator.objects.create(
        datamart_id=next(_ids),
        source_id=source_id,
        intervention=pd,
        pd_reference_number=number,
        section_name="Education",
        lower_result_name="1.1 Example output",
        display_type="number",
        unit="number",
        baseline_numerator=Decimal(0),
        title=title,
        target_numerator=Decimal(target),
        tag_age_group="Children",
    )
    end = datetime.date(2026, 6, 30)
    dm.ReportedIndicator.objects.create(
        datamart_id=next(_ids),
        partner=org,
        intervention=pd,
        partner_name=org.name,
        pd_reference_number=number,
        progress_report=f"PR-{source_id}",
        report_number="QPR1",
        report_type="QPR",
        report_status="Accepted",
        period_start=datetime.date(2026, 1, 1),
        period_end=end,
        due_date=end + datetime.timedelta(days=15),
        submission_date=end - datetime.timedelta(days=1),
        indicator=title,
        target=str(target),
        achievement_in_period=str(reached),
        total_cumulative_progress=str(reached),
        total_cumulative_progress_in_location=str(reached),
        calculation_across_locations="sum",
        etools_indicator_id=str(source_id),
    )
    return pd


def monitored(
    pd, activity: int, end: datetime.date, rating="Off Track", *, entity=None, linked=True, how="exact"
):
    """One field monitoring finding row of visit ``activity`` about ``pd`` (linked as the sync links it)."""
    return dm.MonitoringFinding.objects.create(
        datamart_id=next(_ids),
        partner=pd.partner,
        vendor_number=pd.partner.vendor_number,
        entity=entity or pd.number,
        entity_type="PD",
        monitoring_activity=f"MA-{activity}",
        monitoring_activity_id=activity,
        overall_finding_rating=rating,
        narrative_finding=NARRATIVE,
        visit_lead=VISIT_LEAD,
        start_date=end - datetime.timedelta(days=1),
        end_date=end,
        intervention=pd if linked else None,
        pd_match=how if linked else "",
    )


def drafts_of(check, today: datetime.date = TODAY) -> dict[str, checks.Draft]:
    return {d.key: d for d in check(checks.Context(today=today))}


# ---------------------------------------------------------------- field monitoring against reported progress
@pytest.fixture
def fm_world(db):
    org = partner(1)
    on = programme(org, "LEB/PCA2026901/PD2026901", 500)  # on track
    monitored(on, 901, datetime.date(2026, 6, 20))
    monitored(on, 901, datetime.date(2026, 6, 20))  # a second entity row of the same visit
    monitored(on, 902, datetime.date(2026, 5, 10))
    off = programme(org, "LEB/PCA2026902/PD2026902", 300)  # the reports say off track too: no tension
    monitored(off, 903, datetime.date(2026, 6, 1))
    old = programme(org, "LEB/PCA2026904/PD2026904", 500)  # rated off track more than 90 days ago
    monitored(old, 904, days_ago(91))
    fine = programme(org, "LEB/PCA2026905/PD2026905", 1000)  # over target, monitoring on track
    monitored(fine, 905, datetime.date(2026, 6, 15), rating="On Track")
    loose = programme(org, "LEB/PCA2026906/PD2026906", 500)  # rated off track, never linked to a PD
    monitored(loose, 906, datetime.date(2026, 6, 15), linked=False)
    silent = programme(org, "LEB/PCA2026907/PD2026907", None)  # nothing reported: no reported progress
    monitored(silent, 907, datetime.date(2026, 6, 15))
    return {"partner": org, "on": on, "off": off}


def test_field_monitoring_off_track_while_the_reports_are_on_track(fm_world):
    on = fm_world["on"]
    found = drafts_of(checks.check_monitoring_disagrees)
    assert list(found) == [f"monitoring_disagrees:{on.number}"]
    draft = found[f"monitoring_disagrees:{on.number}"]
    assert (draft.check, draft.severity, draft.section) == ("monitoring_disagrees", "warning", "Education")
    assert draft.url == reverse("reports:programme_detail", args=[on.pk])
    assert draft.title == (
        f"Field monitoring rated {on.number} (Example Partner 1) off track while its reports are on track"
    )
    assert draft.detail == (
        "2 field monitoring visits in the last 90 days rated it off track, the latest on 20 Jun 2026. The "
        "partner's reports put every indicator with a target and a report on track or ahead of schedule "
        "(1 indicator)."
    )
    assert draft.evidence["records"] == [
        "MA-901, 2026-06-20: Off Track (programme document found by the same reference)",
        "MA-902, 2026-05-10: Off Track (programme document found by the same reference)",
        "# of children in example classes 6901: Reached 500 of target 1,000 (On track)",
    ]
    assert draft.evidence["numbers"] == {
        "visits_off_track": 2,
        "last_visit": "2026-06-20",
        "indicators_on_track": 1,
    }
    text = json.dumps(draft.evidence) + draft.title + draft.detail
    assert NARRATIVE not in text and VISIT_LEAD not in text  # no narrative, no person


def test_the_counts_agree_with_the_programme_document_page(fm_world, site_today):
    """The indicators on track are those of the PD page's status counts (the same monitoring rows), none off
    track there; the visits, those the PD page counts for the year (every field monitoring row of this PD is off
    track and recent)."""
    on = fm_world["on"]
    numbers = drafts_of(checks.check_monitoring_disagrees)[f"monitoring_disagrees:{on.number}"].evidence[
        "numbers"
    ]
    site_today.set(TODAY)  # the PD page reads its figures for the day it opens
    page = datamart.programme_datamart(on, today=TODAY)
    counts = page["monitoring"]["status_counts"]
    assert numbers["indicators_on_track"] == counts["on_track"] + counts["over_target"]
    assert counts["off_track"] == 0
    assert numbers["visits_off_track"] == next(v["fm"] for v in page["visits"] if v["year"] == 2026)


def test_a_programme_document_link_that_is_ambiguous_is_left_out(fm_world):
    """The finding names the PCA/PD pair of two programme documents whose dates both cover the visit: which one
    it is about is a guess, so it is left out; once only one covers the visit, it counts."""
    org = fm_world["partner"]
    linked = programme(org, "LEB/PCA2026903/PD2026903", 500)
    copy = programme(org, "LEB/PCA2026903/PD2026903-1", None, status="draft")
    monitored(linked, 908, datetime.date(2026, 6, 25), entity="PCA2026903/PD2026903", how="token")
    assert f"monitoring_disagrees:{linked.number}" not in drafts_of(checks.check_monitoring_disagrees)
    copy.end = datetime.date(2025, 12, 31)
    copy.start = datetime.date(2025, 1, 1)
    copy.save()
    found = drafts_of(checks.check_monitoring_disagrees)[f"monitoring_disagrees:{linked.number}"]
    assert found.evidence["records"][0] == (
        "MA-908, 2026-06-25: Off Track (programme document found by the same PCA and PD numbers)"
    )


def test_a_stale_link_is_left_out(fm_world):
    """A finding stored with a PD its reference no longer names (the sync links it again) is not counted."""
    org = fm_world["partner"]
    other = programme(org, "LEB/PCA2026909/PD2026909", 500)
    monitored(other, 909, datetime.date(2026, 6, 25), entity=fm_world["off"].number)
    assert f"monitoring_disagrees:{other.number}" not in drafts_of(checks.check_monitoring_disagrees)


def test_the_resolver_tells_a_clear_link_from_a_guess():
    d = datetime.date
    first = (1, "LEB/PCA2026903/PD2026903", "Example", 7, d(2026, 1, 1), d(2026, 12, 31))
    copy = (2, "LEB/PCA2026903/PD2026903-1", "Example", 7, d(2026, 3, 1), d(2027, 6, 30))
    found = fm.PDResolver([first, copy])
    assert found.unambiguous("LEB/PCA2026903/PD2026903", "pd", 7, d(2026, 5, 1)) == (1, "exact")
    assert found.unambiguous("PCA2026903/PD2026903", "pd", 7, d(2026, 2, 1)) == (1, "token")  # only 1 covers
    assert found.unambiguous("PCA2026903/PD2026903", "pd", 7, d(2026, 5, 1)) == (None, "")  # both cover it
    assert found.unambiguous("PCA2026903/PD2026903", "pd", 7, None) == (None, "")
    assert found.resolve("PCA2026903/PD2026903", "pd", 7, d(2026, 5, 1)) == (
        2,
        "token",
    )  # resolve still picks


# ---------------------------------------------------------------- audit findings with no follow-up
def engagement(org, reference: str, kind="audit", report_days=90, status="final", **fields):
    values = {
        "datamart_id": next(_ids),
        "source_id": next(_ids),
        "partner": org,
        "partner_name": org.name,
        "vendor_number": org.vendor_number,
        "reference_number": reference,
        "engagement_type": kind,
        "status": status,
        "auditor": AUDITOR,
        "start_date": days_ago((report_days or 0) + 60),
        "date_of_final_report": days_ago(report_days) if report_days is not None else None,
        "high_priority_findings": 0,
        "key_control_weaknesses": 0,
        "financial_findings_count": 0,
    }
    values.update(fields)
    return dm.AuditEngagement.objects.create(**values)


def action_point(**fields):
    values = {
        "datamart_id": next(_ids),
        "source_id": next(_ids),
        "reference_number": f"AP/{next(_ids)}",
        "description": "Follow up the finding",
        "status": "open",
        "related_module": "audit",
        "assigned_to_name": ASSIGNEE,
    }
    values.update(fields)
    return dm.ActionPoint.objects.create(**values)


@pytest.fixture
def audit_world(db):
    org = partner(2)
    found = {
        "high": engagement(org, "EXA/AUD/2026/001", high_priority_findings=2, financial_findings_count=3),
        "weak": engagement(org, "EXA/SC/2026/002", kind="sc", report_days=100, key_control_weaknesses=1),
        "listed": engagement(org, "EXA/AUD/2026/003", report_days=75, financial_findings_count=None),
        "fk": engagement(org, "EXA/SA/2026/004", kind="sa", report_days=70, high_priority_findings=1),
        "by_reference": engagement(
            org, "EXA/SC/2026/005", kind="sc", report_days=80, high_priority_findings=1
        ),
        "in_data": engagement(org, "EXA/AUD/2026/006", report_days=80, high_priority_findings=1),
        "recent": engagement(org, "EXA/AUD/2026/007", report_days=30, high_priority_findings=1),
        "assessment": engagement(org, "EXA/MA/2026/008", kind="ma", high_priority_findings=1),
        "cancelled": engagement(org, "EXA/AUD/2026/009", status="cancelled", high_priority_findings=1),
        "clean": engagement(org, "EXA/AUD/2026/010"),
        "no_report": engagement(org, "EXA/AUD/2026/011", report_days=None, high_priority_findings=1),
        "old": engagement(org, "EXA/AUD/2023/012", report_days=800, high_priority_findings=1),
    }
    for n in (1, 2):  # the financial findings listed, the Datamart's count missing
        dm.AuditFinding.objects.create(datamart_id=next(_ids), engagement=found["listed"], finding_number=n)
    action_point(engagement=found["fk"], partner=org)
    action_point(module_reference_number="EXA/SC/2026/005", partner=org)
    listed = action_point(partner=org)
    found["in_data"].data = {"action_points": [{"id": listed.source_id}]}
    found["in_data"].save()
    return found


def test_audit_findings_with_no_action_point_are_raised(audit_world):
    found = drafts_of(checks.check_audit_no_follow_up)
    high, weak, listed = audit_world["high"], audit_world["weak"], audit_world["listed"]
    assert set(found) == {f"audit_no_follow_up:{e.reference_number}" for e in (high, weak, listed)}
    first = found[f"audit_no_follow_up:{high.reference_number}"]
    assert (first.check, first.severity, first.section) == ("audit_no_follow_up", "warning", "")
    assert first.url == reverse("reports:engagement_detail", args=[high.pk])
    assert (
        first.title == "Audit EXA/AUD/2026/001 (Example Partner 2): 2 high-priority findings, no action point"
    )
    assert first.detail == (
        "The final report was issued on 02 Apr 2026, 90 days ago, and no action point follows it up in eTools. "
        "It lists 2 high-priority findings, 0 key control weaknesses and 3 financial findings."
    )
    assert first.evidence["records"] == [
        "EXA/AUD/2026/001: Audit of Example Partner 2, final report 2026-04-02"
    ]
    assert first.evidence["numbers"] == {
        "high_priority_findings": 2,
        "key_control_weaknesses": 0,
        "financial_findings": 3,
        "report_date": "2026-04-02",
        "days_since_report": 90,
    }
    assert found[f"audit_no_follow_up:{weak.reference_number}"].severity == "info"
    assert found[f"audit_no_follow_up:{weak.reference_number}"].title == (
        "Spot check EXA/SC/2026/002 (Example Partner 2): 1 key control weakness, no action point"
    )
    assert found[f"audit_no_follow_up:{listed.reference_number}"].severity == "info"
    for draft in found.values():
        text = json.dumps(draft.evidence) + draft.title + draft.detail
        assert AUDITOR not in text and ASSIGNEE not in text  # no auditor, no assignee


def test_the_follow_up_and_the_counts_are_the_engagement_pages(audit_world):
    """An engagement is raised exactly when its page lists no action point (linked by the sync, listed in its
    data or raised under its reference), with the page's own finding counts."""
    found = drafts_of(checks.check_audit_no_follow_up)
    for name in ("high", "weak", "listed", "fk", "by_reference", "in_data"):
        e = audit_world[name]
        page = datamart.engagement_detail(e)
        draft = found.get(f"audit_no_follow_up:{e.reference_number}")
        assert (draft is None) == bool(page["action_points"]), name
        if draft is not None:
            numbers = draft.evidence["numbers"]
            assert numbers["financial_findings"] == page["findings_count"], name
            assert numbers["high_priority_findings"] == (e.high_priority_findings or 0), name
            assert numbers["key_control_weaknesses"] == (e.key_control_weaknesses or 0), name


# ---------------------------------------------------------------- HACT assurance all year
def hact(org, year=2026, **fields):
    values = {
        "datamart_id": next(_ids),
        "partner": org,
        "partner_name": org.name if org else "",
        "vendor_number": org.vendor_number if org else "",
        "year": year,
        "pv_required": 3,
        "pv_completed": 1,
        "sc_required": 1,
        "sc_completed": 1,
        "audits_required": 0,
        "audits_completed": 0,
    }
    values.update(fields)
    return dm.PartnerHACTYear.objects.create(**values)


@pytest.fixture
def hact_world(db):
    behind, done = partner(3), partner(4)
    hact(behind, last_modify_date=datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC))
    hact(behind, year=2025, pv_completed=0)  # last year's
    hact(done, pv_completed=3)
    return {"behind": behind, "done": done}


def test_hact_assurance_still_to_do_is_noted_before_october(hact_world):
    behind = hact_world["behind"]
    found = drafts_of(checks.check_hact_assurance)
    assert list(found) == [f"hact_assurance:2026:{behind.pk}"]
    draft = found[f"hact_assurance:2026:{behind.pk}"]
    assert (draft.check, draft.severity, draft.section) == ("hact_assurance", "info", "")
    assert draft.title == "HACT assurance for Example Partner 3: 2 programmatic visits still to do this year"
    assert draft.detail == (
        "eTools shows 1 programmatic visit done of 3 required and 1 spot check done of 1 required in 2026. "
        "Still to do: 2 programmatic visits, with 6 months of the year left (July to December)."
    )
    assert draft.url == reverse("reports:partner_profile", args=[behind.pk])
    plan = reverse("reports:monitoring") + f"?tab=plan&partner={behind.pk}&year=2026"
    assert draft.evidence["links"] == [{"label": "HACT visit plan", "url": plan}]
    assert draft.evidence["records"] == [
        "Programmatic visits in 2026: 1 of 3 completed",
        "Spot checks in 2026: 1 of 1 completed",
    ]
    assert draft.evidence["numbers"] == {
        "pv_required": 3,
        "pv_completed": 1,
        "sc_required": 1,
        "sc_completed": 1,
        "audits_required": 0,
        "audits_completed": 0,
        "months_left": 6,
    }


def test_hact_assurance_stays_to_note_from_october_as_watch_follows_it(hact_world):
    behind = hact_world["behind"]
    for day in (datetime.date(2026, 10, 1), datetime.date(2026, 11, 20), datetime.date(2026, 12, 20)):
        draft = drafts_of(checks.check_hact_assurance, day)[f"hact_assurance:2026:{behind.pk}"]
        assert draft.severity == "info", day
        assert draft.detail.endswith(" NeuroDB Watch has its own HACT check from 1 October.")
        assert "follows it too" not in draft.detail
    assert "with 1 month of the year left (December). NeuroDB Watch" in draft.detail


def test_hact_assurance_reads_watchs_rows_and_rule(hact_world):
    """The same partners and numbers as Watch's HACT check on the same day: the latest synced row of a partner's
    year, and what it still requires (``money.gaps``)."""
    behind = hact_world["behind"]
    hact(behind, pv_completed=2, last_modify_date=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC))  # newer
    day = datetime.date(2026, 10, 5)
    review = drafts_of(checks.check_hact_assurance, day)
    watch = {c.key: c for c in money.hact_assurance_gaps(Context.make(DAILY, today=day))}
    assert {key.replace("hact_assurance:", "hact:", 1) for key in review} == set(watch)
    for key, draft in review.items():
        candidate = watch[key.replace("hact_assurance:", "hact:", 1)]
        numbers = {k: v for k, v in draft.evidence["numbers"].items() if k != "months_left"}
        assert (
            numbers
            == {k: candidate.evidence["numbers"][k] for k in numbers}
            == {
                "pv_required": 3,
                "pv_completed": 2,
                "sc_required": 1,
                "sc_completed": 1,
                "audits_required": 0,
                "audits_completed": 0,
            }
        )
        assert "1 programmatic visit" in draft.title and "1 programmatic visit to go" in candidate.title


def test_a_hact_row_with_no_partner_links_to_the_assurance_page(db):
    hact(None, vendor_number="VX99", partner_name="Example Partner 99")
    draft = drafts_of(checks.check_hact_assurance)["hact_assurance:2026:vendor:VX99"]
    assert draft.url == reverse("reports:assurance") + "?hact_year=2026"
    assert draft.evidence["links"] == [
        {"label": "HACT visit plan", "url": reverse("reports:monitoring") + "?tab=plan&year=2026"}
    ]


# ---------------------------------------------------------------- the trial
def test_the_new_checks_start_in_trial_and_the_earlier_ones_are_on(db):
    assert NEW_CHECKS <= set(checks.CHECK_IDS) and len(checks.CHECKS) == 17
    assert set(checks.LABELS) == set(checks.CHECK_IDS)
    assert checks.in_trial() == NEW_CHECKS
    for check_id in checks.CHECK_IDS:
        expected = CheckSetting.Mode.TRIAL if check_id in NEW_CHECKS else CheckSetting.Mode.ON
        assert checks.default_mode(check_id) == expected
    assert checks.default_mode("a_check_added_later") == CheckSetting.Mode.TRIAL
    CheckSetting.objects.create(check_id="hact_assurance", mode=CheckSetting.Mode.ON)
    CheckSetting.objects.create(check_id="not_reported", mode=CheckSetting.Mode.TRIAL)
    assert checks.in_trial() == {"monitoring_disagrees", "audit_no_follow_up", "not_reported"}


@pytest.fixture
def world(fm_world, audit_world, hact_world, settings):
    settings.AI_ASSISTANT_ENABLED = False
    return fm_world


def test_findings_of_a_check_in_trial_are_marked_and_an_administrator_switches_it_on(
    world, client, admin_user
):
    review = services.run(date=TODAY, today=TODAY)
    found = list(review.findings.exclude(state=ReviewFinding.State.RESOLVED))
    assert {f.check_id for f in found} >= NEW_CHECKS
    assert all(f.trial == (f.check_id in NEW_CHECKS) for f in found)

    client.force_login(admin_user)
    changelist = reverse("admin:review_checksetting_changelist")
    html = client.get(changelist).content.decode()
    assert CheckSetting.objects.count() == len(checks.CHECKS)  # every check listed, in its default mode
    assert "Trial: not sent to NeuroDB Watch" in html and "On: also sent to NeuroDB Watch" in html
    assert "page only" not in html
    assert "Audit findings with no action point" in html
    open_audits = review.findings.filter(check_id="audit_no_follow_up").exclude(
        state=ReviewFinding.State.RESOLVED
    )
    assert open_audits.count() == 3
    assert f'check_id__exact=audit_no_follow_up&amp;review__id__exact={review.pk}">3</a>' in html
    pk = CheckSetting.objects.get(check_id="audit_no_follow_up").pk
    response = client.post(changelist, {"action": "switch_on", "_selected_action": [pk]})
    assert response.status_code == 302
    setting = CheckSetting.objects.get(pk=pk)
    assert (setting.mode, setting.on_since, setting.updated_by) == ("on", timezone.localdate(), "admin")

    review = services.run(date=TODAY + datetime.timedelta(days=1), today=TODAY)  # from the next review on
    by_check = {f.check_id: f.trial for f in review.findings.exclude(state=ReviewFinding.State.RESOLVED)}
    assert by_check["audit_no_follow_up"] is False and by_check["monitoring_disagrees"] is True

    client.post(changelist, {"action": "back_to_trial", "_selected_action": [pk]})
    setting.refresh_from_db()
    assert (setting.mode, setting.trial_since) == ("trial", timezone.localdate())


def test_viewers_cannot_switch_a_check_on(db, client, viewer):
    client.force_login(viewer)
    response = client.get(reverse("admin:review_checksetting_changelist"))
    assert response.status_code in (302, 403)


def test_the_daily_review_page_marks_the_findings_of_a_check_in_trial(world, reporting_year, client_viewer):
    services.run(date=TODAY, today=TODAY)
    html = client_viewer.get("/daily-review/?section=&_part=review", **HX).content.decode()
    more = client_viewer.get("/daily-review/?section=&_part=review-more", **HX).content.decode()
    both = html + more
    assert (
        both.count('<span class="review-tag review-tag--trial"')
        == ReviewFinding.objects.filter(trial=True).exclude(state=ReviewFinding.State.RESOLVED).count()
    )
    assert ">Trial</span>" in both
    assert (
        "A new check in trial: its findings are not sent to NeuroDB Watch until an administrator switches it on."
        in both
    )
    assert "here only" not in both
    assert 'class="review-finding__links"' in both and ">HACT visit plan" in both
    assert "seventeen fixed checks read the programme data" in html
    assert "A new check starts in trial" in html


# ---------------------------------------------------------------- review fixes
def test_hact_assurance_done_beyond_what_is_required_reads_plainly(db):
    hact(partner(5), pv_required=1, pv_completed=2, sc_required=1, sc_completed=0)
    (draft,) = drafts_of(checks.check_hact_assurance).values()
    assert (
        "2 programmatic visits done of 1 required and 0 spot checks done of 1 required in 2026"
        in draft.detail
    )
    assert "2 of 1" not in draft.detail


def test_a_new_check_s_trial_since_is_the_day_a_review_first_ran_it(world, client, admin_user):
    assert not CheckSetting.objects.exists()
    services.run(date=TODAY, today=TODAY)
    settings = {c.check_id: c for c in CheckSetting.objects.all()}
    assert set(settings) == set(checks.CHECK_IDS)
    for check_id in NEW_CHECKS:
        assert (settings[check_id].mode, settings[check_id].trial_since) == ("trial", timezone.localdate())
        assert settings[check_id].on_since is None
    established = settings["reports_overdue"]
    assert (established.mode, established.on_since, established.trial_since) == ("on", None, None)
    # the administration's list stamps a row it makes too
    CheckSetting.objects.filter(check_id="hact_assurance").delete()
    client.force_login(admin_user)
    client.get(reverse("admin:review_checksetting_changelist"))
    assert CheckSetting.objects.get(check_id="hact_assurance").trial_since == timezone.localdate()


def test_the_check_s_page_in_the_administration_names_it_once_in_words(db, client, admin_user):
    checks.ensure_settings()
    setting = CheckSetting.objects.get(check_id="hact_assurance")
    assert str(setting) == "HACT assurance still to do this year"
    client.force_login(admin_user)
    html = client.get(reverse("admin:review_checksetting_change", args=[setting.pk])).content.decode()
    title = html[html.index("<title>") : html.index("</title>")]
    assert "HACT assurance still to do this year" in title and "hact_assurance" not in title
    main = html[html.index("<form") :]
    assert main.count("hact_assurance") == 1  # the id once, under its name
    from neurodb.review.admin import CheckSettingAdmin

    assert "check_id" not in CheckSettingAdmin.fields


def test_the_check_ids_in_the_list_are_readable_in_dark_mode(db, client, admin_user):
    client.force_login(admin_user)
    html = client.get(reverse("admin:review_checksetting_changelist")).content.decode()
    assert '<small class="text-font-subtle-light dark:text-font-subtle-dark">hact_assurance</small>' in html


def test_home_marks_a_trial_finding_as_on_the_daily_review_page(world, reporting_year, client_viewer):
    review = services.run(date=TODAY, today=TODAY)
    review.findings.filter(trial=False).update(rank=models.F("rank") + 1000)  # the trial findings first
    found = services.digest([], limit=50)
    assert {r["check"] for r in found["rows"] if r["trial"]} == NEW_CHECKS
    assert all(r["trial"] == (r["check"] in NEW_CHECKS) for r in found["rows"])
    # Home's counts are the Daily review page's: the trial findings stay counted
    shown = review.findings.exclude(state=ReviewFinding.State.RESOLVED).exclude(severity="good")
    assert found["open"] == shown.count()
    html = client_viewer.get("/home/?_part=tasks", **HX).content.decode()
    from neurodb.reports import home

    assert html.count('<span class="review-tag review-tag--trial"') == home.REVIEW_ROWS  # each row Home lists
    assert "not sent to NeuroDB Watch" in html


def test_the_trial_is_described_as_it_works():
    """A finding of a check in trial reaches Home and the summaries too: only NeuroDB Watch leaves it out."""
    from pathlib import Path

    from neurodb.review import admin as review_admin

    assert str(CheckSetting.Mode.TRIAL.label) == "Trial: not sent to NeuroDB Watch"
    assert ReviewFinding._meta.get_field("trial").help_text == (
        "found by a check in trial: never sent to NeuroDB Watch"
    )
    assert str(review_admin.CheckSettingAdmin.back_to_trial.short_description) == (
        "Put back in trial: not sent to NeuroDB Watch"
    )
    root = Path(__file__).resolve().parents[2]
    readme = " ".join((root / "README.md").read_text().split())
    assert "its findings are not sent to NeuroDB Watch until an administrator switches it on" in readme
    for path in ("README.md", "neurodb/review/models.py", "neurodb/review/admin.py",
                 "neurodb/watch/detectors/concerns.py", "neurodb/watch/investigate.py",
                 "neurodb/review/migrations/0004_check_settings.py",
                 "neurodb/reports/templates/reports/partials/_daily_review_finding.html"):  # fmt: skip
        text = " ".join((root / path).read_text().split())
        assert "page only" not in text and "here only" not in text and "on its page only" not in text, path
