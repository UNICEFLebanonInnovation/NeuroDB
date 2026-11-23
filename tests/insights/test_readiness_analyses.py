"""Machine learning readiness, measured further: how well the sources link up, what arrives each year (and the
action point history kept), a verdict for each analysis the programme may want later, what changed since the
last check, and the check's monthly schedule. Counts, dates and statuses only: no person and no free text.

The yearly counts are made by the same rules as the pages that count them (Assurance's year, the field
monitoring page's visits, findings and TPM visits, the action points page's raised and completed months, the
progress reports' own report key); each test here proves the two agree.
"""

from __future__ import annotations

import datetime
import json
import re
from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone

from neurodb.core import scheduler
from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart import fm, identity, masters, reporting
from neurodb.datamart import models as dm
from neurodb.datamart import services as dms
from neurodb.datamart.monitoring import pending_q
from neurodb.fmm.models import ActionPointVerification, DayRuleTally, Visit, VisitEntity
from neurodb.insights import readiness
from neurodb.insights.management.commands.ml_readiness import run
from neurodb.partnerships.models import PCA, PartnerOrganization

day = datetime.date
TODAY = day(2026, 10, 15)  # the site's clock in every test (tests/conftest.py)
PLANTED = "Plantedname"  # a made-up person or text that must never reach the check
LATER = ("unusual", "themes", "change", "follow_up", "before_after", "value_for_money")


def _metrics(result):
    return {f"{s['key']}.{m['key']}": m["value"] for s in result["sections"] for m in s["metrics"]}


def _shown(result):
    return {f"{s['key']}.{m['key']}": m["display"] for s in result["sections"] for m in s["metrics"]}


def _note(result, key):
    return next(
        m["note"] for s in result["sections"] for m in s["metrics"] if f"{s['key']}.{m['key']}" == key
    )


def _aware(year, month, dd):
    return datetime.datetime(year, month, dd, 9, 0, tzinfo=datetime.UTC)


@pytest.fixture
def linked(db):
    partner = PartnerOrganization.objects.create(
        etl_id="9001", name="Example Partner One", vendor_number="EX1"
    )
    running = PCA.objects.create(
        etl_id="9101", number="EX/PCA2026001/PD2026001", title="Example programme one", status="active",
        partner=partner, start=day(2025, 1, 1), end=day(2027, 12, 31),
    )  # fmt: skip
    ended = PCA.objects.create(
        etl_id="9102", number="EX/PCA2024002/PD2024002", title="Example programme two", status="ended",
        partner=partner, start=day(2023, 1, 1), end=day(2024, 12, 31),
    )  # fmt: skip
    return SimpleNamespace(partner=partner, running=running, ended=ended)


# ------------------------------------------------------------------------------------- links between sources
def test_how_the_sources_link_up(linked, monkeypatch):
    partner, pd = linked.partner, linked.running
    for n, (intervention, how, who) in enumerate(
        [(pd, "exact", partner), (pd, "token", partner), (pd, "title", None), (None, "", partner)], start=1
    ):
        dm.MonitoringFinding.objects.create(
            datamart_id=n, intervention=intervention, pd_match=how, partner=who, narrative_finding=PLANTED
        )
    now = timezone.now()
    visit = Visit.objects.create(key="v1", label="Visit 1", refreshed_at=now, pd=pd)
    Visit.objects.create(key="v2", label="Visit 2", refreshed_at=now)
    VisitEntity.objects.create(
        visit=visit, datamart_id=1, kind="pd", pd=pd, pd_match="exact", rating="on_track"
    )
    VisitEntity.objects.create(visit=visit, datamart_id=2, kind="pd", rating="on_track")
    VisitEntity.objects.create(visit=visit, datamart_id=3, kind="partner", partner=partner, rating="on_track")
    spot = dm.AuditEngagement.objects.create(datamart_id=1, engagement_type="sc", auditor=PLANTED)
    spot.interventions.set([pd])
    dm.AuditEngagement.objects.create(datamart_id=2, engagement_type="audit")
    dm.AuditEngagement.objects.create(datamart_id=3, engagement_type="ma")  # about a partner, not a PD
    dm.ActionPoint.objects.create(
        datamart_id=1, intervention=pd, partner=partner, engagement_source_id=77, engagement=spot,
        assigned_to_name=PLANTED,
    )  # fmt: skip
    dm.ActionPoint.objects.create(
        datamart_id=2, partner=partner, related_module="audit"
    )  # its audit not found
    dm.ActionPoint.objects.create(datamart_id=3, related_module="fm")  # not raised from an audit
    dm.FundsReservation.objects.create(datamart_id=1, intervention=pd)
    dm.FundsReservation.objects.create(datamart_id=2)
    # the running PD's indicators (one repeated per location, one without an eTools id) and the ended PD's one
    rows = [
        (101, "Children enrolled"),
        (101, "Children enrolled"),
        (102, "Teachers trained"),
        (None, "Schools  run"),
    ]
    for n, (source_id, title) in enumerate(rows, start=1):
        dm.PDIndicator.objects.create(datamart_id=n, intervention=pd, source_id=source_id, title=title)
    dm.PDIndicator.objects.create(datamart_id=9, intervention=linked.ended, source_id=104, title="Earlier")
    master = dm.EtoolsMaster.objects.create(section="Education", name="Children in learning")
    untitled = masters.ident_code(identity.ident_of(pd.pk, None, "Schools  run")[1])
    for ident, status in (("s101", "confirmed"), (untitled, "confirmed"), ("s102", "suggested")):
        dm.EtoolsMasterLink.objects.create(master=master, pd=pd, ident=ident, status=status)
    dm.EtoolsMasterLink.objects.create(master=master, pd=linked.ended, ident="s104", status="confirmed")

    result = readiness.measure(TODAY)
    assert not any(s["error"] for s in result["sections"])
    m = _metrics(result)
    assert m["links.fm_findings_pd"] == 0.75 and m["links.fm_findings_partner"] == 0.75
    note = _note(result, "links.fm_findings_pd")
    assert "the same reference: 1" in note and "the same PCA and PD numbers: 1" in note
    assert "the same title, among the partner's PDs: 1" in note and "not linked: 1" in note
    assert m["links.fm_records_pd"] == 0.5 and "the same reference: 1" in _note(result, "links.fm_records_pd")
    assert m["links.fm_visits_pd"] == 0.5
    assert m["links.ap_pd"] == 0.333 and m["links.ap_partner"] == 0.667
    assert m["links.ap_engagement"] == 0.5  # of the two raised from an audit or spot check
    assert m["links.audits_pd"] == 0.5  # micro-assessments left out
    assert m["links.funds_pd"] == 0.5
    assert m["links.indicators_master"] == 0.667  # 2 of the running PD's 3 indicators; the suggestion is not
    assert m["links.master_groups"] == 0  # one running PD under the master: not a group to compare in
    monkeypatch.setattr(readiness, "PDS_PER_GROUP", 1)
    assert _metrics(readiness.measure(TODAY))["links.master_groups"] == 1
    assert PLANTED not in json.dumps(result)


# --------------------------------------------------------------------------------- what arrives each year
def test_what_arrives_each_year_agrees_with_the_pages_that_count_it(linked):
    pd = linked.running
    engagement = dm.AuditEngagement.objects.create
    engagement(datamart_id=11, engagement_type="audit", year_of_audit=2024, start_date=day(2025, 3, 1))
    engagement(datamart_id=12, engagement_type="sc", start_date=day(2025, 6, 1))
    engagement(datamart_id=13, engagement_type="sc", start_date=day(2026, 2, 1))
    engagement(datamart_id=14, engagement_type="ma", start_date=day(2026, 5, 1))
    engagement(datamart_id=15, engagement_type="sa", year_of_audit=2025)
    finding = dm.MonitoringFinding.objects.create
    finding(datamart_id=21, monitoring_activity_id=501, start_date=day(2025, 4, 2), visit_lead=PLANTED)
    finding(datamart_id=22, monitoring_activity_id=501, start_date=day(2025, 4, 2))  # the same visit
    finding(datamart_id=23, monitoring_activity_id=502, end_date=day(2025, 9, 9))  # no start date
    finding(datamart_id=24, monitoring_activity_id=503, start_date=day(2026, 1, 5))
    dm.AuditFinding.objects.create(datamart_id=31, created=day(2025, 7, 1), description=PLANTED)
    tpm = dm.TPMVisit.objects.create
    tpm(datamart_id=41, status="unicef_approved", start_date=day(2025, 5, 1), author_name=PLANTED)
    tpm(datamart_id=42, status="tpm_reported", start_date=day(2026, 5, 1))
    tpm(datamart_id=43, status="cancelled", start_date=day(2026, 5, 1))
    tpm(datamart_id=44, status="assigned", start_date=day(2026, 6, 1))  # not done yet
    point = dm.ActionPoint.objects.create
    point(datamart_id=51, status="open", data={"created": "2025-11-03T10:00:00Z"})
    point(
        datamart_id=52,
        status="completed",
        data={"created": "2025-12-30"},
        date_of_completion=_aware(2026, 2, 1),
    )
    point(
        datamart_id=53,
        status="Completed",
        data={"date_created": "2026-01-15"},
        date_of_completion=_aware(2026, 3, 1),
    )
    point(datamart_id=54, status="cancelled", data={})
    for n, state in enumerate(("verified", "verified", "pending"), start=1):
        ActionPointVerification.objects.create(
            datamart_id=51 + (n == 3), state=state, verified_by_name=PLANTED, note=PLANTED
        )

    def report(n, number, kind, start, end, status="", sent=None):
        dm.ReportedIndicator.objects.create(
            datamart_id=n, intervention=pd, pd_reference_number=pd.number, report_number=number, report_type=kind,
            period_start=start, period_end=end, report_status=status, submission_date=sent, narrative=PLANTED,
        )  # fmt: skip

    report(61, "QPR1", "QPR", day(2025, 1, 1), day(2025, 3, 31), "Accepted", day(2025, 4, 10))
    report(62, "QPR1", "QPR", day(2025, 1, 1), day(2025, 3, 31), "Accepted", day(2025, 4, 10))  # same report
    report(63, "QPR2", "QPR", day(2025, 4, 1), day(2025, 6, 30), "Submitted")
    report(64, "HR1", "HR", day(2026, 1, 1), day(2026, 1, 31), "acc")
    report(65, "QPR7", "qpr ", day(2026, 7, 1), day(2026, 9, 30), "Overdue")  # not sent: not arrived

    result = readiness.measure(TODAY)
    assert not any(s["error"] for s in result["sections"])
    m = _metrics(result)
    assert m["arrivals.audits"] == {"2024": 1, "2025": 0, "2026": 0}
    assert m["arrivals.special_audits"] == {"2025": 1, "2026": 0}
    assert m["arrivals.spot_checks"] == {"2025": 1, "2026": 1}
    assert m["arrivals.micro_assessments"] == {"2026": 1}
    assert m["arrivals.fm_visits"] == {"2025": 2, "2026": 1}
    assert m["arrivals.fm_findings"] == {"2025": 3, "2026": 1}
    assert m["arrivals.audit_findings"] == {"2025": 1, "2026": 0}
    assert m["arrivals.tpm_visits"] == {"2025": 1, "2026": 1}
    assert m["arrivals.ap_raised"] == {"2025": 2, "2026": 1}
    assert m["arrivals.ap_completed"] == {"2026": 2}
    assert m["arrivals.pme_verifications"] == {"2026": 3}
    assert m["arrivals.pme_first"] == "2026-10-15" and m["arrivals.pme_decided"] == 1
    assert m["arrivals.reports_qpr"] == {"2025": 2, "2026": 0} and m["arrivals.reports_hr"] == {"2026": 1}
    assert m["arrivals.reports_first"] == "2025-01-01"
    shown = _shown(result)
    assert shown["arrivals.spot_checks"] == "2025: 1 · 2026 so far: 1"
    assert shown["arrivals.pme_first"] == "15 Oct 2026"

    # the same rules as the pages: Assurance (its year and type filters), field monitoring (its visits per year,
    # its findings and completed TPM visits for a year), the action points page (raised and completed months)
    for key, kind in (
        ("audits", "audit"),
        ("special_audits", "sa"),
        ("spot_checks", "sc"),
        ("micro_assessments", "ma"),
    ):
        for year, n in m[f"arrivals.{key}"].items():
            page = dms.assurance(QueryDict(f"year={year}&type={kind}"))
            assert page["engagements"].count() == n, (kind, year)
    assert m["arrivals.fm_visits"] == {
        str(y): n for y, n in fm.visits_by_year(dm.MonitoringFinding.objects.all()).items()
    }
    for year in ("2025", "2026"):
        page = dms.monitoring(QueryDict(f"year={year}"))
        assert page["findings"].count() == m["arrivals.fm_findings"][year]
        assert page["tpm_completed"] == m["arrivals.tpm_visits"][year]
    monthly = dms.action_point_charts(dm.ActionPoint.objects.all(), TODAY)["monthly"]
    for series, key in (("Raised", "ap_raised"), ("Completed", "ap_completed")):
        by_year = {}
        for month, n in zip(monthly["labels"], monthly["series"][series], strict=True):
            by_year[month[:4]] = by_year.get(month[:4], 0) + n
        assert {y: n for y, n in by_year.items() if n} == {y: n for y, n in m[f"arrivals.{key}"].items() if n}
    # the progress reports by the code's own report key, those sent only
    sent = dm.ReportedIndicator.objects.exclude(pending_q())
    for kind, key in (("QPR", "reports_qpr"), ("HR", "reports_hr")):
        for year, n in m[f"arrivals.{key}"].items():
            rows = sent.filter(period_end__year=int(year), report_type__iexact=kind)
            assert reporting.report_count(rows) == n, (kind, year)
    assert PLANTED not in json.dumps(result)


def test_a_typical_running_indicator_s_settled_values(linked):
    """Settled: a value in a report UNICEF accepted. An indicator's values are found by its eTools id, else by
    its title among its PD's indicators. The ended PD's indicator is not a running one."""
    pd, ended = linked.running, linked.ended
    indicator = dm.PDIndicator.objects.create
    indicator(datamart_id=1, intervention=pd, source_id=101, title="Children enrolled")
    indicator(datamart_id=2, intervention=pd, source_id=101, title="Children enrolled")  # another place
    indicator(datamart_id=3, intervention=pd, source_id=102, title="Teachers trained")
    indicator(datamart_id=4, intervention=pd, source_id=None, title="Schools run")
    indicator(datamart_id=5, intervention=ended, source_id=104, title="Earlier")
    rows = iter(range(1, 100))

    def value(pd_, etools_id, title, number, kind, end, status="Accepted", place="A"):
        dm.ReportedIndicator.objects.create(
            datamart_id=next(rows), intervention=pd_, report_number=number, report_type=kind, period_end=end,
            report_status=status, etools_indicator_id=etools_id, indicator=title, location=place,
        )  # fmt: skip

    value(pd, "101", "Children enrolled", "QPR1", "QPR", day(2025, 3, 31))
    value(pd, "101", "Children enrolled", "QPR1", "QPR", day(2025, 3, 31), place="B")  # one value, two places
    value(pd, "101", "Children enrolled", "QPR2", "QPR", day(2025, 6, 30), status="acc")
    value(pd, "101", "Children enrolled", "HR3", "HR", day(2025, 6, 30))  # a second value in that quarter
    value(pd, "101", "Children enrolled", "QPR3", "QPR", day(2025, 9, 30), status="Submitted")  # not settled
    value(pd, "", "teachers  TRAINED", "QPR1", "QPR", day(2025, 3, 31))  # no eTools id: found by its title
    value(pd, "", "Teachers trained", "QPR3", "QPR", day(2025, 9, 30))
    value(pd, "", "Schools run", "QPR3", "QPR", day(2025, 9, 30))
    for number in range(1, 6):
        value(
            ended,
            "104",
            "Earlier",
            f"QPR{number}",
            "QPR",
            day(2024, 3 * number - 2 if number < 5 else 12, 28),
        )
    m = _metrics(readiness.measure(TODAY))
    assert m["arrivals.settled_values"] == 2  # 3, 2 and 1 values
    assert m["arrivals.settled_quarters"] == 2  # 2, 2 and 1 quarters


# ------------------------------------------------------------------------------------ action point history
def test_the_action_point_history_reads_not_kept_yet_until_it_is_kept(db, monkeypatch):
    result = readiness.measure(TODAY)
    assert _metrics(result)["arrivals.ap_history_days"] == 0
    assert _shown(result)["arrivals.ap_history_days"] == "Not kept yet"
    # once a table of one row per day exists (the action point history: datamart ActionPointStatus) and fills
    monkeypatch.setattr(readiness, "history_model", lambda: DayRuleTally)
    result = readiness.measure(TODAY)
    assert _shown(result)["arrivals.ap_history_days"] == "Not kept yet"  # the table is there, still empty
    for dd in (day(2026, 10, 1), day(2026, 10, 2), day(2026, 10, 14)):
        DayRuleTally.objects.create(day=dd, rule="R1", status="pass", records=1, points_n=1)
    result = readiness.measure(TODAY)
    m = _metrics(result)
    assert m["arrivals.ap_history_days"] == 3 and m["arrivals.ap_history_first"] == "2026-10-01"
    assert _shown(result)["arrivals.ap_history_days"] == "3 days, since 1 Oct 2026"


def test_the_history_model_is_looked_up_by_name():
    try:
        expected = django_apps.get_model("datamart", "ActionPointStatus")
    except LookupError:
        expected = None
    assert readiness.history_model() is expected


# -------------------------------------------------------------------------------------- verdicts for later
def _ready_measures():
    return {
        "links.master_groups": readiness.COMPARABLE_GROUPS,
        "links.indicators_master": readiness.MASTER_LINKED,
        "arrivals.settled_values": max(
            readiness.VALUES_PER_INDICATOR, readiness.CHANGE_VALUES, readiness.BEFORE_AFTER_VALUES
        ),
        "arrivals.settled_quarters": readiness.CHANGE_QUARTERS,
        "arrivals.fm_findings": {"2025": readiness.THEME_FM_FINDINGS, "2026": 3},
        "arrivals.audit_findings": {"2025": readiness.THEME_AUDIT_FINDINGS, "2026": 1},
        "arrivals.pme_decided": readiness.FOLLOW_UP_VERIFIED,
        "arrivals.pme_recent": 0,
        "arrivals.ap_history_days": readiness.FOLLOW_UP_HISTORY_DAYS,
        "arrivals.ap_history_first": "2025-10-01",
        "arrivals.ap_completed": {"2025": readiness.FOLLOW_UP_COMPLETED, "2026": 4},
        "arrivals.events_before_after": readiness.BEFORE_AFTER_EVENTS,
    }


def test_every_later_analysis_is_ready_once_its_thresholds_are_met_but_value_for_money():
    found = {v.key: v for v in readiness.verdicts(_ready_measures(), TODAY)}
    assert [k for k in found if found[k].group == "later"] == list(LATER)
    for key in LATER[:-1]:
        assert (found[key].status, found[key].reason, found[key].when) == ("ready", "", ""), key
    money = found["value_for_money"]
    assert money.status == "not_yet" and money.when == ""
    assert "NeuroDB holds no spending per programme document" in money.reason
    assert "Funds reserved and disbursed are not spending" in money.reason


def test_a_later_analysis_not_ready_says_why_and_about_when_only_when_the_pace_tells():
    m = _ready_measures() | {
        "links.master_groups": 0,  # waits on people confirming links: no date
        "arrivals.settled_values": 3,  # three values over three quarters: one a quarter
        "arrivals.settled_quarters": 3,
        "arrivals.fm_findings": {"2025": 10, "2026": readiness.THEME_FM_FINDINGS - 1},  # this year on course
        "arrivals.ap_history_days": 100,
        "arrivals.ap_history_first": "2026-07-07",
        "arrivals.pme_decided": readiness.FOLLOW_UP_VERIFIED - 50,
        "arrivals.pme_recent": 50,  # fifty a year: fifty more in a year
    }
    found = {v.key: v for v in readiness.verdicts(m, TODAY)}
    unusual = found["unusual"]
    assert unusual.status == "not_yet" and "master indicator" in unusual.reason and unusual.when == ""
    change = found["change"]
    assert change.status == "not_yet" and change.reason and change.when == "about July 2027"
    themes = found["themes"]  # audits alone have enough findings: partly
    assert themes.status == "partly" and themes.when == "about January 2027"  # once 2026 is a full year
    follow_up = found["follow_up"]
    assert follow_up.status == "not_yet" and follow_up.when == "about October 2027"  # the later of two dates
    before_after = found["before_after"]  # enough events; two more values each, at one a quarter
    assert before_after.status == "partly" and before_after.when == "about April 2027"
    # a measure of a year the pace cannot reach gives no date
    slow = readiness.verdicts(m | {"arrivals.fm_findings": {"2025": 10, "2026": 12}}, TODAY)
    assert next(v for v in slow if v.key == "themes").when == ""


def test_an_older_check_shows_only_the_verdicts_it_had(db):
    older = {
        "measured_at": "2026-10-12T05:00:00+00:00",
        "sections": [{"key": "etools", "label": "eTools", "error": "", "metrics": [
            {"key": "pd_locations", "label": "PDs with locations", "value": 0.8, "display": "80%", "note": ""}]}],
        "verdicts": [{"key": "coverage", "question": "?", "method": "", "status": "partly", "checks": []},
                     {"key": "review_list", "question": "?", "method": "", "status": "not_yet", "checks": []}],
    }  # fmt: skip
    assert [v["key"] for v in readiness.shown(older)["verdicts"]] == ["coverage", "review_list"]
    new = readiness.measure(TODAY)
    assert [v["key"] for v in readiness.shown(new)["verdicts"]] == ["coverage", "review_list", *LATER]


# ----------------------------------------------------------------------------------- since the last check
def test_since_the_last_check_lists_what_changed():
    def check(points, when, status):
        return {
            "measured_at": when,
            "sections": [{"key": "assurance", "label": "Assurance", "error": "", "metrics": [
                {"key": "action_points", "label": "Action points", "value": points, "display": f"{points:,}",
                 "note": ""},
                {"key": "visits", "label": "Programmatic visits", "value": 4, "display": "4", "note": ""}]}],
            "verdicts": [{"key": "follow_up", "question": "Which follow-ups work?", "status": status,
                          "status_label": readiness.STATUS_LABELS[status], "checks": []}],
        }  # fmt: skip

    before = check(1200, "2026-09-01T03:45:00+00:00", "not_yet")
    after = check(1450, "2026-10-01T03:45:00+00:00", "partly")
    found = readiness.since(before, after)
    assert found["day"] == day(2026, 9, 1)
    assert found["metrics"] == [{"label": "Action points", "before": "1,200", "after": "1,450"}]
    assert found["verdicts"] == [
        {"question": "Which follow-ups work?", "before": "Not yet", "after": "Partly ready"}
    ]
    assert readiness.since(after, after) == {"day": day(2026, 10, 1), "metrics": [], "verdicts": []}
    assert readiness.since(None, after) is None


def _readiness_part(page):
    start = page.index('id="ml-readiness"')
    return page[start : page.index("</section>", start)]


def test_data_health_shows_the_later_analyses_and_what_changed_since_the_last_check(client_viewer, linked):
    run("test")
    page = client_viewer.get(reverse("reports:data_health")).content.decode()
    part = _readiness_part(page)
    assert "Analyses for later" in part and "Checked again every month" in part
    for verdict in readiness.verdicts({}, TODAY):
        assert verdict.question.replace("'", "&#x27;") in part, verdict.question
    assert "How the sources link up" in part and "What arrives each year" in part
    assert "Since the last check" not in part  # one check only
    assert ' style="' not in part and "<script" not in part
    dm.ActionPoint.objects.create(datamart_id=1, status="open", intervention=linked.running)
    run("test")
    part = _readiness_part(client_viewer.get(reverse("reports:data_health")).content.decode())
    words = re.sub(r"<[^>]+>", "", part)
    assert "Since the last check" in words and "Action points: 0 → 1" in words


# ----------------------------------------------------------------------------------------- the schedule
def _monthly():
    """The data migration that makes the check monthly (core 0034), run again on the test database."""
    import_module("neurodb.core.migrations.0034_ml_readiness_monthly").monthly(django_apps, None)


def test_the_weekly_readiness_schedule_becomes_monthly(db):
    job = ScheduledJob.objects.get(key="ml-readiness")
    ScheduledJob.objects.filter(pk=job.pk).update(schedule="45 6 * * 1", next_run_at=timezone.now())
    _monthly()
    job.refresh_from_db()
    assert (job.schedule, job.enabled, job.next_run_at) == ("45 6 1 * *", True, None)
    assert ScheduledJob.objects.filter(command="ml_readiness").count() == 1
    ScheduledJob.objects.exclude(pk=job.pk).update(enabled=False)  # only this one is looked at
    assert scheduler.tick() == []  # planned, not started at once
    job.refresh_from_db()
    assert timezone.localtime(job.next_run_at).date() == day(2026, 11, 1)


def test_a_schedule_an_administrator_chose_is_kept_and_a_missing_one_is_made(db):
    ScheduledJob.objects.filter(command="ml_readiness").update(schedule="0 5 * * 3", enabled=False)
    _monthly()
    assert list(ScheduledJob.objects.filter(command="ml_readiness").values_list("schedule", "enabled")) == [
        ("0 5 * * 3", False)
    ]
    ScheduledJob.objects.filter(command="ml_readiness").delete()
    _monthly()
    job = ScheduledJob.objects.get(command="ml_readiness")
    assert (job.key, job.schedule, job.enabled) == ("ml-readiness", "45 6 1 * *", True)


def test_the_job_records_reasons_and_no_person(linked):
    dm.ActionPoint.objects.create(datamart_id=1, status="open", assigned_to_name=PLANTED, description=PLANTED)
    result = run("test")
    assert result.status == SyncRun.Status.SUCCEEDED
    stored = result.details["readiness"]
    assert {v["key"] for v in stored["verdicts"]} == {"coverage", "review_list", *LATER}
    assert all("reason" in v and "when" in v and "group" in v for v in stored["verdicts"])
    assert PLANTED not in json.dumps(stored) and "ActivityInfo" not in json.dumps(stored)
