"""Open action points and a dated timeline on the programme document page and the partner page (part
"follow-up", ``neurodb.datamart.timeline``).

Each page loads the block as a part after the page itself. It holds three counts of open action points (open,
overdue, open and high priority), each linked to the Action points page filtered to exactly those points and
counted by that page's own query, and one dated list, newest first, of what happened to the programme document
or the partner, from the tables NeuroDB already holds. Counts, dates, references, statuses and ratings only: no
person, no firm, no text a partner or a monitor wrote.

The world (made-up names, today 15 Oct 2026): one partner with a running programme document and one that ended in
2023, progress reports (accepted, sent back, overdue), field monitoring visits, third-party monitoring, assurance
engagements, assessments, a HACT year, action points and their status history. Another partner holds action
points that must never be counted, one of them naming the programme document in its text.
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse
from django.utils import timezone

from neurodb.datamart import models as dm
from neurodb.datamart import timeline
from neurodb.fmm.models import Visit
from neurodb.partnerships.models import PCA, PartnerOrganization
from tests.web.parts import (
    HX,
    assemble,
    eager,
    executable,
    get_part,
    normalise,
    placeholders,
    raw_get,
    text,
)

pytestmark = pytest.mark.django_db

D = datetime.date
TODAY = D(2026, 10, 15)  # the suite's day (tests/conftest.py)
QUICK = {"HTTP_X_NEURODB_QUICK": "1"}
# what must never reach the block: made-up people, firms and free text
ASSIGNEE = "Quinn Fakename-Assignee"
DESCRIPTION = "Made-up follow-up wording that stays on the action point page"
NARRATIVE = "Made-up visit narrative that stays on the visit page"
LEAD = "Rowan Fakename-Lead"
AUTHOR = "Sky Fakename-Author"
AUDITOR = "Example Audit Firm Invalid"
TPM_FIRM = "Example Monitors Invalid"
FINDING = "Made-up finding wording that stays on the engagement page"
NEVER = (ASSIGNEE, DESCRIPTION, NARRATIVE, LEAD, AUTHOR, AUDITOR, TPM_FIRM, FINDING, "@example.org")

ENTRY = re.compile(
    r'<li class="timeline__item" data-kind="(?P<kind>[a-z_]+)">\s*<time class="timeline__date" '
    r'datetime="(?P<day>\d{4}-\d{2}-\d{2})">(?P<body>.*?)</li>',
    re.S,
)
HREF = re.compile(r'href="([^"]*)"')
TAGS = re.compile(r"<[^>]+>")
_ids = iter(range(70_000, 10**9))


def _words(html: str) -> str:
    return " ".join(unescape(TAGS.sub(" ", html)).split())


def _report(pd, pr, number, end, word, *, submitted=None, accepted=None):
    dm.ReportedIndicator.objects.create(
        datamart_id=next(_ids),
        intervention=pd,
        partner=pd.partner,
        partner_name=pd.partner.name,
        pd_reference_number=pd.number,
        progress_report=pr,
        indicator_report=f"{pr}-1",
        report_number=number,
        report_type="QPR",
        report_status=word,
        period_start=end - datetime.timedelta(days=89),
        period_end=end,
        due_date=end + datetime.timedelta(days=30),
        submission_date=submitted,
        acceptance_date=accepted,
        submitted_by=AUTHOR,
        narrative=NARRATIVE,
        narrative_assessment=NARRATIVE,
        indicator="# of children reached",
        etools_indicator_id="1",
        location="Akkar",
        p_code="LB11",
        section="Education",
        disaggregation={"()": {"v": 1, "d": 1, "c": 1}},
        calculation_across_locations="sum",
        calculation_across_periods="sum",
    )


def _point(n, partner, pd, status, *, due=None, created=None, high=False, done=None, description=DESCRIPTION):
    return dm.ActionPoint.objects.create(
        datamart_id=9100 + n,
        source_id=9100 + n,
        partner=partner,
        intervention=pd,
        intervention_number=pd.number if pd else "",
        reference_number=f"LEB/2026/AP/{n}",
        description=description,
        status=status,
        high_priority=high,
        due_date=due,
        date_of_completion=done,
        assigned_to_name=ASSIGNEE,
        related_module="audit",
        data={"created": created} if created else {},
    )


def _history(source_id, day, pd, partner, status, *, high=False, due=None, in_datamart=True):
    dm.ActionPointStatus.objects.create(
        source_id=source_id, day=day, status=status, high_priority=high, due_date=due,
        in_datamart=in_datamart, intervention=pd, partner=partner, related_module="audit",
    )  # fmt: skip


@pytest.fixture
def world(reporting_year, settings):
    settings.FMM_ENABLED = True
    partner = PartnerOrganization.objects.create(
        etl_id="9001",
        name="Sample Relief Org",
        partner_type="Civil Society Organization",
        vendor_number="V9001",
    )
    pd = PCA.objects.create(
        etl_id="90011", partner=partner, partner_name=partner.name, number="LEB/PCA2025901/PD2025901",
        title="Sample services", status="active", document_type="PD", start=D(2025, 3, 1), end=D(2027, 2, 28),
        signed_by_partner_date=D(2025, 2, 10), signed_by_unicef_date=D(2025, 2, 15),
        unicef_focal_points=[LEAD],
    )  # fmt: skip
    old = PCA.objects.create(
        etl_id="90012", partner=partner, partner_name=partner.name, number="LEB/PCA2022902/PD2022902",
        title="Sample earlier services", status="closed", document_type="PD", start=D(2022, 1, 1),
        end=D(2023, 12, 31),
    )  # fmt: skip
    # progress reports: accepted (submitted, then accepted), sent back, overdue; one of the old PD
    _report(pd, "PR-1", "QPR1", D(2026, 3, 31), "Accepted", submitted=D(2026, 4, 10), accepted=D(2026, 4, 20))
    _report(pd, "PR-2", "QPR2", D(2026, 6, 30), "Sent back", submitted=D(2026, 7, 12))
    _report(pd, "PR-3", "QPR3", D(2026, 8, 31), "Overdue")
    _report(
        old, "PR-9", "QPR1", D(2023, 3, 31), "Accepted", submitted=D(2023, 4, 10), accepted=D(2023, 4, 15)
    )
    # field monitoring: one visit of the PD over two entities (its worst rating is the visit's), one of the partner
    for n, rating in ((1, "On Track"), (2, "Off Track")):
        dm.MonitoringFinding.objects.create(
            datamart_id=next(_ids), partner=partner, intervention=pd, monitoring_activity="FM/2026/0042",
            monitoring_activity_id=4242, entity=f"Entity {n}", overall_finding_rating=rating,
            start_date=D(2026, 9, 1), end_date=D(2026, 9, 3), narrative_finding=NARRATIVE, visit_lead=LEAD,
        )  # fmt: skip
    dm.MonitoringFinding.objects.create(
        datamart_id=next(_ids), partner=partner, monitoring_activity="FM/2026/0043", monitoring_activity_id=4243,
        entity=partner.name, overall_finding_rating="Constrained", start_date=D(2026, 2, 1),
        end_date=D(2026, 2, 2), narrative_finding=NARRATIVE, visit_lead=LEAD,
    )  # fmt: skip
    # third-party monitoring: the PD's activity and the partner's visit
    visit = dm.TPMVisit.objects.create(
        datamart_id=next(_ids), partner=partner, reference_number="TPM/2026/0007", status="unicef_approved",
        start_date=D(2026, 5, 18), author_name=AUTHOR, tpm_name=TPM_FIRM,
    )  # fmt: skip
    dm.TPMActivity.objects.create(
        datamart_id=next(_ids), partner=partner, intervention=pd, visit=visit, task_reference_number="TPM/2026/0007-1",
        visit_reference_number="TPM/2026/0007", status="completed", date=D(2026, 5, 20), tpm_name=TPM_FIRM,
        pd_reference_number=pd.number,
    )  # fmt: skip
    # assurance: a spot check of the PD, an audit and a micro-assessment of the partner (the last before 24 months)
    check = dm.AuditEngagement.objects.create(
        datamart_id=next(_ids), partner=partner, reference_number="LEB/2026/SC/001", engagement_type="sc",
        status="final", auditor=AUDITOR, date_of_final_report=D(2026, 6, 15), financial_findings_count=2,
        high_priority_findings=1,
    )  # fmt: skip
    check.interventions.add(pd)
    audit = dm.AuditEngagement.objects.create(
        datamart_id=next(_ids), partner=partner, reference_number="LEB/2025/AUD/002", engagement_type="audit",
        status="report_submitted", auditor=AUDITOR, date_of_field_visit=D(2025, 12, 5),
    )  # fmt: skip
    dm.AuditFinding.objects.create(
        datamart_id=next(_ids), partner=partner, engagement=audit, reference_number="LEB/2025/AUD/002",
        title="Unsupported spending", description=FINDING, recommendation=FINDING, amount=Decimal("90"),
    )  # fmt: skip
    dm.AuditEngagement.objects.create(
        datamart_id=next(_ids), partner=partner, reference_number="LEB/2024/MA/003", engagement_type="ma",
        status="final", auditor=AUDITOR, date_of_final_report=D(2024, 1, 20), risk_rating="Low",
    )  # fmt: skip
    dm.PartnerAssessment.objects.create(
        datamart_id=next(_ids),
        partner=partner,
        type="Micro Assessment",
        rating="Low",
        completed_date=D(2025, 11, 30),
    )
    dm.PSEAAssessment.objects.create(
        datamart_id=next(_ids), partner=partner, reference_number="PSEA/2026/01", status="final",
        assessment_date=D(2026, 3, 3), overall_rating=8,
    )  # fmt: skip
    for year in (2025, 2026):  # 2026 is not over: no entry for it
        dm.PartnerHACTYear.objects.create(
            datamart_id=next(_ids), partner=partner, year=year, risk_rating="Low", pv_required=2, pv_completed=1,
            sc_required=1, sc_completed=1, audits_required=1, audits_completed=0,
        )  # fmt: skip
    # action points: the PD's (open and overdue and high priority; in progress; completed), the partner's
    done = timezone.make_aware(datetime.datetime(2026, 8, 20, 10, 0))
    _point(1, partner, pd, "open", due=D(2026, 9, 1), created="2026-06-16T10:00:00Z", high=True)
    _point(2, partner, pd, "in_progress", due=D(2026, 12, 1), created="2026-09-05T08:00:00Z")
    _point(
        3, partner, pd, "completed", due=D(2026, 9, 1), created="2026-07-01T08:00:00Z", high=True, done=done
    )
    _point(4, partner, None, "open", due=D(2026, 10, 1), created="2026-09-20T08:00:00Z")
    # another partner's: never counted, even the one whose text names the PD
    other = PartnerOrganization.objects.create(
        etl_id="9002",
        name="Other Sample Org",
        partner_type="Civil Society Organization",
        vendor_number="V9002",
    )
    _point(5, other, None, "open", due=D(2026, 9, 1), created="2026-06-01T08:00:00Z", high=True)
    _point(6, other, None, "open", due=D(2026, 9, 1), description=f"Same as {pd.number} and V9001")
    # the status history since 1 Aug 2026: AP 1 made high priority, AP 3 completed, AP 4's due date moved
    _history(9101, D(2026, 8, 1), pd, partner, "open")
    _history(9101, D(2026, 8, 5), pd, partner, "open", high=True)
    _history(9103, D(2026, 8, 1), pd, partner, "open", high=True)
    _history(9103, D(2026, 8, 20), pd, partner, "completed", high=True)
    _history(9104, D(2026, 9, 20), None, partner, "open", due=D(2026, 9, 25))
    _history(9104, D(2026, 10, 12), None, partner, "open", due=D(2026, 10, 1))
    return {"partner": partner, "pd": pd, "old": old, "other": other}


def _pd_url(world) -> str:
    return reverse("reports:programme_detail", args=[world["pd"].pk])


def _partner_url(world) -> str:
    return reverse("reports:partner_profile", args=[world["partner"].pk])


def _entries(html: str) -> list[dict[str, str]]:
    return [
        {"kind": m["kind"], "day": m["day"], "words": _words(m["body"]), "links": HREF.findall(m["body"])}
        for m in ENTRY.finditer(html)
    ]


def _part(client, url):
    return get_part(client, url, "follow-up")


# ------------------------------------------------------------------------------------------------- the part
@pytest.mark.shell
@pytest.mark.parametrize("page", ["pd", "partner"])
def test_the_block_is_a_part_that_loads_after_the_page(client_viewer, world, page):
    url = _pd_url(world) if page == "pd" else _partner_url(world)
    html = text(raw_get(client_viewer, url))
    found = placeholders(html)
    assert found["follow-up"] == f"{url}?_part=follow-up"  # the page's own address: no filter to carry
    assert "Timeline" not in _words(html.split('data-part="follow-up"', 1)[0].rsplit("<div", 1)[0])
    assert "timeline__item" not in html  # the list comes with the part, never with the page


def test_the_pd_timeline_lists_each_kind_newest_first(client_viewer, world):
    html = text(_part(client_viewer, _pd_url(world)))
    entries = _entries(html)
    days = [e["day"] for e in entries]
    assert days == sorted(days, reverse=True), days  # newest first
    kinds = {e["kind"] for e in entries}
    assert kinds == {"pd", "report", "fm", "tpm", "assurance", "action_point", "history"}
    by_day = {(e["day"], e["kind"]): e["words"] for e in entries}
    # the programme document: its end still to come, its start and signatures; its status now
    assert "Ends" in by_day[("2027-02-28", "pd")] and "planned" in by_day[("2027-02-28", "pd")]
    assert "Started" in by_day[("2025-03-01", "pd")]
    assert "Signed by UNICEF" in by_day[("2025-02-15", "pd")]
    assert "Signed by the partner" in by_day[("2025-02-10", "pd")]
    assert "Status in eTools now: Active" in _words(html)
    # progress reports: submitted then accepted, sent back, overdue, each with its reference
    assert (
        "submitted" in by_day[("2026-04-10", "report")].lower() and "QPR1" in by_day[("2026-04-10", "report")]
    )
    assert "accepted" in by_day[("2026-04-20", "report")].lower()
    assert (
        "sent back" in by_day[("2026-07-12", "report")].lower() and "QPR2" in by_day[("2026-07-12", "report")]
    )
    assert (
        "not submitted" in by_day[("2026-09-30", "report")].lower()
        and "QPR3" in by_day[("2026-09-30", "report")]
    )
    # a field monitoring visit, once for its two entities, with its worst rating
    assert [e for e in entries if e["kind"] == "fm"] == [e for e in entries if e["day"] == "2026-09-01"]
    assert "Off track" in by_day[("2026-09-01", "fm")] and "FM/2026/0042" in by_day[("2026-09-01", "fm")]
    assert "TPM/2026/0007-1" in by_day[("2026-05-20", "tpm")]
    # the spot check: its type, final report and finding counts
    check = by_day[("2026-06-15", "assurance")]
    assert "Spot check" in check and "2 financial findings" in check and "1 high priority" in check
    # action points raised and completed, and the changes recorded since the history started
    assert (
        "raised" in by_day[("2026-06-16", "action_point")].lower()
        and "LEB/2026/AP/1" in by_day[("2026-06-16", "action_point")]
    )
    assert "completed" in by_day[("2026-08-20", "action_point")].lower()
    assert "Marked high priority" in by_day[("2026-08-05", "history")]
    assert "from open to completed" in by_day[("2026-08-20", "history")].lower()
    assert "from 1 Aug 2026" in _words(html)
    # the partner's own events are not the PD's
    words = _words(html)
    for elsewhere in ("FM/2026/0043", "LEB/2025/AUD/002", "PSEA/2026/01", "LEB/2026/AP/4", "HACT year"):
        assert elsewhere not in words, elsewhere
    assert "amendments" in words.lower()  # said, never cut silently: NeuroDB holds no dated amendment


def test_each_entry_links_to_where_its_detail_lives(client_viewer, world):
    pd = world["pd"]
    entries = _entries(text(_part(client_viewer, _pd_url(world))))
    assert all(len(e["links"]) == 1 for e in entries), [e for e in entries if len(e["links"]) != 1]
    links = {(e["day"], e["kind"]): unescape(e["links"][0]) for e in entries}
    assert links[("2025-03-01", "pd")] == _pd_url(world)
    report = urlsplit(links[("2026-04-10", "report")])
    assert report.path == reverse("reports:progress_report") and parse_qs(report.query)["report"]
    check = dm.AuditEngagement.objects.get(reference_number="LEB/2026/SC/001")
    assert links[("2026-06-15", "assurance")] == reverse("reports:engagement_detail", args=[check.pk])
    point = dm.ActionPoint.objects.get(reference_number="LEB/2026/AP/1")
    assert links[("2026-06-16", "action_point")] == reverse("reports:action_point", args=[point.pk])
    assert links[("2026-08-05", "history")] == reverse("reports:action_point", args=[point.pk])
    # the field monitoring visit: the Field monitoring page's search until Monitoring insights holds the visit
    assert links[("2026-09-01", "fm")] == reverse("reports:monitoring") + "?q=FM%2F2026%2F0042"
    assert links[("2026-05-20", "tpm")] == reverse("reports:monitoring") + "?q=" + pd.number.replace(
        "/", "%2F"
    )
    # every link opens a page that exists for the person reading
    for address in set(links.values()):
        assert client_viewer.get(address).status_code == 200, address
    Visit.objects.create(
        key="4242", label="Visit 4242", start_date=D(2026, 9, 1), end_date=D(2026, 9, 3), visit_date=D(2026, 9, 1),
        status="completed", status_group="reported", rating="off_track", refreshed_at=timezone.now(),
    )  # fmt: skip
    entries = _entries(text(_part(client_viewer, _pd_url(world))))
    fm = next(e for e in entries if e["kind"] == "fm")
    assert fm["links"] == [reverse("fmm:visit", args=["4242"])]


def test_the_partner_timeline_covers_its_pds_and_names_the_pd_on_each_pd_event(client_viewer, world):
    pd = world["pd"]
    html = text(_part(client_viewer, _partner_url(world)))
    entries = _entries(html)
    days = [e["day"] for e in entries]
    assert days == sorted(days, reverse=True)
    by_day = {(e["day"], e["kind"]): e["words"] for e in entries}
    # the PD's events, each naming the PD by its reference
    for key in (
        ("2025-03-01", "pd"),
        ("2026-04-10", "report"),
        ("2026-09-01", "fm"),
        ("2026-06-16", "action_point"),
        ("2026-08-20", "history"),
    ):
        assert pd.number in by_day[key], key
    # partner-level events: assurance, assessments, HACT, third-party and field monitoring of the partner itself
    assert (
        "Audit" in by_day[("2025-12-05", "assurance")]
        and "1 financial finding" in by_day[("2025-12-05", "assurance")]
    )
    assert "Micro Assessment" in by_day[("2025-11-30", "assessment")]
    assert "PSEA" in by_day[("2026-03-03", "assessment")]
    hact = by_day[("2025-12-31", "hact")]
    assert "HACT year 2025" in hact and "1 programmatic visit done of 2 required" in hact
    assert not any(e["kind"] == "hact" and e["day"].startswith("2026") for e in entries)  # not over yet
    assert "TPM/2026/0007" in by_day[("2026-05-18", "tpm")]
    assert "Constrained" in by_day[("2026-02-01", "fm")]
    assert "LEB/2026/AP/4" in by_day[("2026-09-20", "action_point")]
    assert "Due date changed to 1 Oct 2026" in by_day[("2026-10-12", "history")]
    for key in (("2025-12-05", "assurance"), ("2026-03-03", "assessment"), ("2025-12-31", "hact")):
        assert pd.number not in by_day[key], key
    # another partner's points never come
    assert "LEB/2026/AP/5" not in _words(html) and "LEB/2026/AP/6" not in _words(html)


EXPECTED_POINTS = {
    "pd": {"open": 2, "overdue": 1, "high_priority": 1},
    "partner": {"open": 3, "overdue": 2, "high_priority": 1},
}


@pytest.mark.parametrize("page", ["pd", "partner"])
def test_each_open_points_count_equals_the_rows_its_link_lists(client_viewer, world, page):
    url = _pd_url(world) if page == "pd" else _partner_url(world)
    part = _part(client_viewer, url)
    html = text(part)
    points = part.context["follow_up"]["points"]
    assert {p["key"]: p["count"] for p in points} == EXPECTED_POINTS[page]
    for point in points:
        address = unescape(point["url"])
        assert f'href="{point["url"]}"' in html.replace("&amp;", "&") or f'href="{address}"' in unescape(html)
        listed = client_viewer.get(address)
        assert listed.status_code == 200
        assert listed.context["page_obj"].paginator.count == point["count"], point
        assert len(listed.context["rows"]) == point["count"], point  # fewer than a page of 50 here
        if page == "pd":  # the PD's own points: never one of another partner whose text names the PD
            assert {r.intervention_id for r in listed.context["rows"]} <= {world["pd"].pk}
        else:
            assert {r.partner_id for r in listed.context["rows"]} <= {world["partner"].pk}
    # the same function as the page: the counts of the module equal the page's figures for that filter
    scope = {"pd": world["pd"]} if page == "pd" else {"partner": world["partner"]}
    assert timeline.open_points(**scope) == points


def test_the_action_points_page_filters_on_a_pd_and_says_so(client_viewer, world):
    pd = world["pd"]
    response = client_viewer.get(reverse("reports:action_points"), {"pd": pd.pk, "status": "open"})
    rows = response.context["rows"]
    assert {r.reference_number for r in rows} == {"LEB/2026/AP/1", "LEB/2026/AP/2"}
    html = text(response)
    assert pd.number in _words(html)  # the filter shows as a chip, which removes it
    assert f'name="pd" value="{pd.pk}"' in html  # kept when the filter bar is applied
    # a search for the PD's number lists more than its own points: why the link filters on the PD itself
    searched = client_viewer.get(reverse("reports:action_points"), {"q": pd.number, "status": "open"})
    assert searched.context["page_obj"].paginator.count == 3


@pytest.mark.parametrize("page", ["pd", "partner"])
def test_no_person_firm_or_free_text_reaches_the_block(client_viewer, world, page):
    url = _pd_url(world) if page == "pd" else _partner_url(world)
    html = text(_part(client_viewer, url))
    for never in NEVER:
        assert never not in html, never
    assert executable(html) == [] and 'style="' not in html and "<script" not in html  # CSP


def test_the_window_and_the_cap_are_stated(client_viewer, world, monkeypatch):
    url = _partner_url(world)
    html = text(_part(client_viewer, url))
    words = _words(html)
    days = [e["day"] for e in _entries(html)]
    assert min(days) >= "2024-10-15"  # the last 24 months by default
    # the old PD's start, end and report, and the 2024 micro-assessment: said, with the way to see them
    assert "5 older events are not listed" in words
    show_older = f"{url}?timeline=all#follow-up"
    assert f'href="{show_older}"' in html
    everything = text(_part(client_viewer, f"{url}?timeline=all"))
    old = {(e["day"], e["kind"]) for e in _entries(everything)}
    assert {("2022-01-01", "pd"), ("2023-12-31", "pd"), ("2024-01-20", "assurance")} <= old
    assert "Every event, newest first" in _words(everything) and f'href="{url}#follow-up"' in everything
    # a page of entries at most: the rest is said, never cut silently
    monkeypatch.setattr(timeline, "LIMIT", 3)
    capped = text(_part(client_viewer, url))
    assert len(_entries(capped)) == 3
    total = len(days)
    past = total - 1  # the PD's end is still to come: listed first, counted apart
    words = _words(capped)
    assert f"The 2 latest of the {past} events of the last 24 months are listed" in words
    assert "1 planned date is listed first." in words
    assert f"{total - 3} more are not listed. Show every event" in words
    assert capped.count(f'href="{show_older}"') == 2  # Show every event, and Show older for the older ones


def test_the_history_says_from_when_it_runs(client_viewer, world):
    assert "from 1 Aug 2026" in _words(text(_part(client_viewer, _pd_url(world))))
    dm.ActionPointStatus.objects.all().delete()
    words = _words(text(_part(client_viewer, _pd_url(world))))
    assert "starts from the day NeuroDB first records it" in words
    assert not any(e["kind"] == "history" for e in _entries(text(_part(client_viewer, _pd_url(world)))))


@pytest.mark.parametrize("page", ["pd", "partner"])
def test_the_part_answers_inside_the_quick_view(client_viewer, world, page):
    url = _pd_url(world) if page == "pd" else _partner_url(world)
    shell = raw_get(client_viewer, url, **QUICK)
    assert shell["X-NeuroDB-Quick-View"] == "1"
    address = placeholders(text(shell))["follow-up"]
    assert address == f"{url}?_part=follow-up"
    part = client_viewer.get(address, **HX, **QUICK)
    assert part.status_code == 200 and part["X-NeuroDB-Part"] == "follow-up"
    assert "data-quick-view" not in text(part) and "timeline__item" in text(part)
    assembled = client_viewer.get(url, **QUICK)
    assert assembled["X-NeuroDB-Quick-View"] == "1"
    assert not placeholders(text(assembled)) and "timeline__item" in text(assembled)


def test_the_pd_pop_up_keeps_its_part(client_viewer, world):
    """The HACT visit plan opens a programme document in a pop-up (an HTMX request): the pop-up holds the part too,
    asked for at the programme document's own address."""
    response = client_viewer.get(_pd_url(world), **HX)
    html = text(response)
    assert response.status_code == 200 and 'class="modal-header"' in html
    assert placeholders(html)["follow-up"] == f"{_pd_url(world)}?_part=follow-up"


@pytest.mark.parametrize("page", ["pd", "partner"])
def test_the_assembled_page_is_the_eager_page(client_viewer, world, page):
    url = _pd_url(world) if page == "pd" else _partner_url(world)
    assembled = assemble(client_viewer, url)
    assert "follow-up" in assembled.parts
    drawn = eager(client_viewer, url)
    assert placeholders(text(drawn)) == {}
    assert normalise(assembled.html) == normalise(text(drawn))


def test_the_timeline_styles_use_the_theme_tokens_and_stack_on_a_phone():
    from pathlib import Path

    from django.conf import settings

    css = (Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "css" / "app.css").read_text()
    rules = re.findall(r"^\.timeline[^{]*\{[^}]*\}", css, re.M)
    assert rules, "no timeline style"
    assert not any(re.search(r"#[0-9a-fA-F]{3,8}\b", rule) for rule in rules)  # colours from the tokens only
    phone = re.search(r"@media \(max-width: 575\.98px\) \{[^@]*\.timeline__item \{[^}]*\}", css)
    assert phone and "grid-template-columns: 1fr" in phone.group(0)


PART_QUERIES = 40  # what the busiest of the two read on this world, with room to spare


@pytest.mark.parametrize("page", ["pd", "partner"])
def test_more_events_never_mean_more_queries(client_viewer, world, page):
    """Each kind of event is read in one go: a partner with more programme documents, engagements, visits and
    action points costs the part no more queries."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    url = _pd_url(world) if page == "pd" else _partner_url(world)

    def queries() -> int:
        with CaptureQueriesContext(connection) as seen:
            response = client_viewer.get(f"{url}?_part=follow-up", **HX)
        assert response.status_code == 200 and response["X-NeuroDB-Part"] == "follow-up"
        return len(seen.captured_queries)

    before = queries()
    assert before <= PART_QUERIES
    partner, pd = world["partner"], world["pd"]
    for n in range(3):
        extra = PCA.objects.create(
            etl_id=f"9003{n}", partner=partner, partner_name=partner.name, number=f"LEB/PCA2026{n}/PD2026{n}",
            title="Sample", status="active", document_type="PD", start=D(2026, 1, 1 + n), end=D(2026, 12, 1),
        )  # fmt: skip
        _report(extra, f"PR-X{n}", "QPR1", D(2026, 3, 31), "Accepted", submitted=D(2026, 4, 5 + n))
        engagement = dm.AuditEngagement.objects.create(
            datamart_id=next(_ids), partner=partner, reference_number=f"LEB/2026/SC/1{n}", engagement_type="sc",
            status="final", date_of_final_report=D(2026, 2, 1 + n),
        )  # fmt: skip
        engagement.interventions.add(pd)
        dm.MonitoringFinding.objects.create(
            datamart_id=next(_ids), partner=partner, intervention=pd, monitoring_activity=f"FM/2026/01{n}",
            monitoring_activity_id=5000 + n, overall_finding_rating="On Track", start_date=D(2026, 3, 1 + n),
        )  # fmt: skip
        _point(20 + n, partner, pd, "open", due=D(2026, 12, 1), created=f"2026-05-0{n + 1}T08:00:00Z")
        _history(9120 + n, D(2026, 8, 1), pd, partner, "open")
        _history(9120 + n, D(2026, 9, 1), pd, partner, "completed")
    assert queries() == before


# ------------------------------------------------------------------------------------------- review fixes
def test_a_pd_s_third_party_monitoring_entries_are_its_activities(client_viewer, world):
    """A programme document lists its TPM activities, as its page's own list does: two tasks of one visit are two
    activities, never two visits."""
    pd = world["pd"]
    visit = dm.TPMVisit.objects.get(reference_number="TPM/2026/0007")
    dm.TPMActivity.objects.create(
        datamart_id=next(_ids), partner=pd.partner, intervention=pd, visit=visit, task_reference_number="TPM/2026/0007-2",
        visit_reference_number="TPM/2026/0007", status="completed", date=D(2026, 5, 20), tpm_name=TPM_FIRM,
        pd_reference_number=pd.number,
    )  # fmt: skip
    entries = [e for e in _entries(text(_part(client_viewer, _pd_url(world)))) if e["kind"] == "tpm"]
    assert len(entries) == 2
    assert all("Activity" in e["words"] and "Visit" not in e["words"] for e in entries)
    # the partner's list keeps its visits
    partner = [e for e in _entries(text(_part(client_viewer, _partner_url(world)))) if e["kind"] == "tpm"]
    assert partner and all("Visit" in e["words"] for e in partner)


def test_a_list_whose_events_are_all_older_says_so_without_contradiction(client_viewer, world):
    old = world["old"]
    url = reverse("reports:programme_detail", args=[old.pk])
    words = _words(text(_part(client_viewer, url)))
    assert "Nothing dated" not in words and "0 events" not in words
    assert "No event since 15 Oct 2024." in words
    assert "older events are not listed" in words and "Show older" in words
    # a programme document with no dated event at all
    empty = PCA.objects.create(
        etl_id="90019", partner=world["partner"], partner_name=world["partner"].name, number="LEB/PCA2026909/PD2026909",
        title="Sample undated", status="draft", document_type="PD",
    )  # fmt: skip
    words = _words(text(_part(client_viewer, reverse("reports:programme_detail", args=[empty.pk]))))
    assert "Nothing dated to list yet." in words and "No event since" not in words


@pytest.mark.parametrize("kind", ["tpm", "fm"])
def test_a_visit_still_to_come_is_marked_planned_and_not_counted_as_past(client_viewer, world, kind):
    partner, pd = world["partner"], world["pd"]
    if kind == "tpm":
        dm.TPMVisit.objects.create(
            datamart_id=next(_ids), partner=partner, reference_number="TPM/2026/0901", status="assigned",
            start_date=D(2026, 12, 20),
        )  # fmt: skip
    else:
        dm.MonitoringFinding.objects.create(
            datamart_id=next(_ids), partner=partner, intervention=pd, monitoring_activity="FM/2026/0901",
            monitoring_activity_id=4901, start_date=D(2026, 12, 20), end_date=D(2026, 12, 21),
        )  # fmt: skip
    html = text(_part(client_viewer, _partner_url(world)))
    entries = _entries(html)
    future = next(e for e in entries if e["day"] == "2026-12-20")
    assert future["kind"] == kind and "Planned" in future["words"]
    assert all("Planned" not in e["words"] for e in entries if e["day"] <= TODAY.isoformat())
    assert "planned" in next(e for e in entries if e["day"] == "2027-02-28")["words"]  # the PD's end
    past = sum(1 for e in entries if e["day"] <= TODAY.isoformat())
    words = _words(html)
    assert f"{past} events since 15 Oct 2024, newest first." in words
    assert "2 planned dates are listed first." in words


def test_entries_cut_past_the_limit_are_one_link_away(client_viewer, world, monkeypatch):
    url = _pd_url(world)
    monkeypatch.setattr(timeline, "LIMIT", 3)
    html = text(_part(client_viewer, url))
    words = _words(html)
    assert "older event" not in words  # nothing older: only the limit cuts
    assert "open the lists below" not in words
    assert re.search(r"\d+ more are not listed\. Show every event", words)
    assert f'href="{url}?timeline=all#follow-up"' in html
    monkeypatch.setattr(timeline, "ALL_LIMIT", 3)
    words = _words(text(_part(client_viewer, f"{url}?timeline=all")))
    assert "The list stops at 3 entries." in words
    assert "The progress reports and the action points below list every record of their kind." in words


def test_a_status_beside_an_entry_is_said_to_be_today_s(client_viewer, world):
    html = text(_part(client_viewer, _pd_url(world)))
    assert "A status beside an entry is its status in eTools now, not on that date." in _words(html)
    raised = {
        e["day"]: e
        for e in ENTRY.finditer(html)
        if e["kind"] == "action_point" and "Raised" in _words(e["body"])
    }
    assert "pill" not in raised["2026-07-01"]["body"]  # completed: its Completed entry says so
    assert "pill" in raised["2026-06-16"]["body"]  # open: its status now


def test_a_finished_hact_year_reads_plainly(client_viewer, world):
    dm.PartnerHACTYear.objects.filter(year=2025).update(pv_completed=3, sc_completed=1)
    hact = next(e for e in _entries(text(_part(client_viewer, _partner_url(world)))) if e["kind"] == "hact")
    assert "3 programmatic visits done of 2 required" in hact["words"]
    assert (
        "1 spot check done of 1 required" in hact["words"] and "0 audits done of 1 required" in hact["words"]
    )


def test_the_guides_say_what_the_timeline_lists():
    from pathlib import Path

    from neurodb.help import guide

    help_text = " ".join((guide.GUIDE_DIR / "partner-progress.md").read_text().split())
    assert "lists them all" not in help_text
    assert (
        "*Show older* lists up to 500 of them, newest first; past that it says how many are left out."
        in help_text
    )
    assert "micro-assessments with their risk rating" in help_text
    assert "third-party monitoring activities" in help_text
    overview = " ".join((guide.GUIDE_DIR / "overview.md").read_text().split())
    assert "each programme document's page (its open action points and timeline only)" in overview
    operations = " ".join(
        (Path(__file__).resolve().parents[2] / "docs" / "OPERATIONS.md").read_text().split()
    )
    assert "links to the full lists" not in operations and "lists every event, at most 500" not in operations
