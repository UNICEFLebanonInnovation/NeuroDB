"""One rule for "report submitted": a progress report counts as submitted when it has a submission
date or its status says so (``monitoring.SUBMITTED``, matched whole and in any case). The daily
review, the monitoring summary, partner reporting and the brief all read it, so a report is
overdue in all four places or in none."""

import datetime

import pytest

from neurodb.datamart import models as dm
from neurodb.datamart import monitoring, services
from neurodb.reports import brief
from neurodb.review import checks
from tests.reports.overview_fixture import TODAY, make_overview_data

pytestmark = pytest.mark.django_db

SENT = datetime.date(2026, 5, 2)


@pytest.mark.parametrize(
    ("status", "date", "submitted"),
    [
        ("Accepted", None, True),
        ("SUBMITTED", None, True),
        ("Sent back", None, True),
        (" sent back ", None, True),
        ("Sen", None, True),
        ("acc", None, True),
        ("Due", None, False),
        ("", None, False),
        ("Overdue", None, False),
        ("Not submitted", None, False),  # the whole status is matched, never a part of it
        ("Accepted later", None, False),
        ("Due", SENT, True),  # a submission date settles it
        ("", SENT, True),
    ],
)
def test_the_rule_reads_the_same_in_python_and_in_the_database(status, date, submitted):
    row = dm.ReportedIndicator.objects.create(datamart_id=1, report_status=status, submission_date=date)
    assert monitoring.is_submitted(row) is submitted
    assert monitoring.is_submitted({"report_status": status, "submission_date": date}) is submitted
    assert dm.ReportedIndicator.objects.filter(monitoring.pending_q()).exists() is not submitted


def test_a_missing_status_is_not_submitted():
    assert not monitoring.is_submitted({"report_status": None, "submission_date": None})
    assert not monitoring.is_submitted({})


class _FrozenDatetime:
    """``datetime`` as partner reporting sees it, with today fixed to the fixture's day."""

    timedelta = datetime.timedelta

    class date(datetime.date):
        @classmethod
        def today(cls):
            return TODAY


@pytest.fixture
def data(db, settings, monkeypatch):
    """The overview fixture, with its second quarterly report (PR-2, two location rows) due ten
    days ago and no submission date; the partner marked it accepted."""
    settings.AI_ASSISTANT_ENABLED = False
    monkeypatch.setattr(services, "datetime", _FrozenDatetime)
    made = make_overview_data()
    copy = dm.ReportedIndicator.objects.get(progress_report="PR-2")
    copy.pk, copy.datamart_id, copy.location = None, 99, "Halba"
    copy.save()
    dm.ReportedIndicator.objects.filter(progress_report="PR-2").update(
        submission_date=None, due_date=TODAY - datetime.timedelta(days=10)
    )
    return made


def overdue_everywhere(reporting_year) -> dict[str, int]:
    """How many progress reports each of the four places calls overdue on ``TODAY``."""
    review = checks.check_reports_overdue(checks.Context(today=TODAY))
    filters = monitoring.Filters(year=2026)
    summary = monitoring.summary(monitoring.indicators(filters, today=TODAY), filters, today=TODAY)
    only_overdue = services.partner_reporting({"overdue": "1"})
    built = brief.build(brief.Scope(year=2026, reporting_year=reporting_year, today=TODAY), cache=False)
    return {
        "review": sum(d.evidence["numbers"]["reports"] for d in review),
        "monitoring summary": summary["overdue_reports"],
        "partner reporting filter": len(only_overdue["reports"]),
        "partner reporting count": services.partner_reporting({})["summary"]["overdue"],
        "brief": built["confidence"]["timeliness"]["counts"]["missing"],
    }


def _scorecard(reporting_year) -> dict:
    built = brief.build(brief.Scope(year=2026, reporting_year=reporting_year, today=TODAY), cache=False)
    (row,) = built["partners"]["scorecard"]
    return row


def test_a_report_accepted_without_its_date_is_overdue_nowhere(data, reporting_year):
    assert overdue_everywhere(reporting_year) == {
        "review": 0,
        "monitoring summary": 0,
        "partner reporting filter": 0,
        "partner reporting count": 0,
        "brief": 0,
    }
    summary = services.partner_reporting({})["summary"]
    assert (summary["reports"], summary["submitted"]) == (2, 2)  # submitted and overdue never overlap
    assert _scorecard(reporting_year)["reports_on_time_percent"] == 100.0


def test_a_report_with_neither_date_nor_status_is_overdue_everywhere_once(data, reporting_year):
    dm.ReportedIndicator.objects.filter(progress_report="PR-2").update(report_status="Due")
    # two location rows of one report: counted once in every place
    assert overdue_everywhere(reporting_year) == {
        "review": 1,
        "monitoring summary": 1,
        "partner reporting filter": 1,
        "partner reporting count": 1,
        "brief": 1,
    }
    (finding,) = checks.check_reports_overdue(checks.Context(today=TODAY))
    assert finding.key == "reports_overdue:LEB/PD1" and finding.evidence["numbers"]["days_overdue"] == 10
    assert services.partner_reporting({})["summary"]["submitted"] == 1
    assert _scorecard(reporting_year)["reports_on_time_percent"] == 50.0


def test_an_empty_status_without_a_date_is_overdue_too(data, reporting_year):
    dm.ReportedIndicator.objects.filter(progress_report="PR-2").update(report_status="")
    assert set(overdue_everywhere(reporting_year).values()) == {1}
