"""The figure trail of Progress reports and of Home's Progress reports block (datamart.trail, SPEC_validation §3,
stage 3): under every filter, each figure the page draws is the page's own (its view's context and its
``data-figure``), rebuilt from the rows it is the same, and the Kept rows CSV gives it again by ``counts_once`` and
by a distinct count of its unit; the chain of steps adds up; the PD status step is the page's left-out note; the
hooks change no SQL and no figure; a trail runs as many queries whatever the number of figures."""

# ruff: noqa: F401, F811 (the fixtures "data", "more" and "frozen" come from the partner monitoring tests)
from __future__ import annotations

import csv
import datetime
import io
import itertools
import re
from html.parser import HTMLParser

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.core import compute
from neurodb.core.models import SyncRun
from neurodb.datamart import models as dm
from neurodb.datamart import reached as rc
from neurodb.datamart import reporting, services, trail, trail_rows
from neurodb.partnerships.models import PCA
from tests.reports.test_pd_monitoring import TODAY, data, report_rows
from tests.reports.test_powerbi_views import frozen, more
from tests.web.parts import get_part, text
from tests.web.test_plain_words import NEVER

pytestmark = pytest.mark.django_db
PR = "progress-reports"
PARTNER = "{partner}"  # the partner's id, filled in by the test
FILTERS = [
    {},
    {"pd_status": ""},
    {"section": "Education"},
    {"pd_status": "", "section": "Education"},
    {"year": "2026"},
    {"pd_status": "", "status": "accepted"},
    {"pd_status": "", "status": ["overdue", "not_yet_due"]},
    {"pd_status": "closed"},
    {"partner": PARTNER},
    {"report_type": "HR"},
    {"pd_status": "", "emergency": "yes"},
    {"pd_status": "", "q": "girls"},
    {"pd_status": "", "overdue": "1"},
    {"pd_status": "", "start_from": "2026-01-01", "end_to": "2026-06-30"},
    {"pd": "LEB/PD1"},
    {"pd_status": "", "status": ["submitted", "overdue"]},
    {"pd_status": "", "q": "no report holds these words"},  # keeps nothing: the page draws no section table
]
CONDITIONS = {  # a figure's conditions, read on the Kept rows CSV's columns
    "status": lambda row, v: row["report_status_neurodb"] == rc.STATUS_LABELS[v],
    "section": lambda row, v: row["section_counted"] == v,
    "month": lambda row, v: row["period_end_month"] == v,
    "late": lambda row, v: row["submitted_late"] == ("yes" if v else "no"),
    "partner": lambda row, v: row["partner_unit"] == (f"partner:{v}" if isinstance(v, int) else f"name:{v}"),
    "has_partner": lambda row, v: (row["partner_unit"] != "") == v,
    "reported": lambda row, v: row["partner_has_reported"] == ("yes" if v else "no"),
    "pd": lambda row, v: row["pd_unit"] == v,
    "report": lambda row, v: row["progress_report_unit"] == v,
    "follow_up": lambda row, v: (
        (row["report_status_neurodb"] in {rc.STATUS_LABELS[k] for k in reporting.FOLLOW_UP}) == v
    ),
}
ONCE_COLUMNS = {  # the Kept rows CSV's counts_once_* columns and the figure each sums to
    "pr.indicator_reports": "counts_once_indicator_report",
    "pr.progress_reports": "counts_once_progress_report",
    "pr.partners": "counts_once_partner",
}
UNIT_COLUMNS = {
    trail.IR: "indicator_report_unit",
    trail.PR: "progress_report_unit",
    trail.PARTNER: "partner_unit",
    trail.PD: "pd_unit",
}


@pytest.fixture
def world(more, frozen):
    """The partner monitoring world (``more``), plus: a cancelled programme document's report; a report naming
    no partner (no id, no name); a report of a programme document NeuroDB does not hold, and one naming none;
    rows kept from an earlier sync; a progress report spread over two sections (one indicator report's rows
    too); an emergency indicator; and an overdue report one of whose rows was submitted."""
    pd, partner, d = more["pd"], more["partner"], datetime.date
    cancelled = PCA.objects.create(
        etl_id="13",
        partner=partner,
        partner_name=partner.name,
        number="LEB/PD3",
        title="Cancelled",
        status="cancelled",
        start=d(2026, 1, 1),
        end=d(2026, 12, 31),
    )
    report_rows(cancelled, partner, "# of kits", "PR-20", "QPR1", "QPR", d(2026, 3, 31), {"Akkar": 5}, 5)
    tents = report_rows(pd, partner, "# of tents", "PR-21", "HR5", "HR", d(2026, 5, 31), {"Akkar": 3}, 3)
    dm.ReportedIndicator.objects.filter(pk__in=[r.pk for r in tents]).update(partner=None, partner_name="")
    for n, (ref, title) in enumerate(
        (("LEB/GONE", "# of chairs"), ("", "# reached by the cluster")), start=1
    ):
        dm.ReportedIndicator.objects.create(
            datamart_id=900 + n,
            partner=partner,
            partner_name=partner.name,
            pd_reference_number=ref,
            progress_report=f"PR-2{1 + n}",
            report_number="QPR1",
            report_type="QPR",
            report_status="Accepted",
            period_start=d(2026, 1, 1),
            period_end=d(2026, 3, 31),
            due_date=d(2026, 4, 15),
            submission_date=d(2026, 4, 10),
            indicator=title,
            location="Akkar",
        )
    girls = dm.ReportedIndicator.objects.filter(indicator=more["girls"])
    girls.update(high_frequency=True, section="Child Protection")
    girls.filter(progress_report="PR-2", location="Akkar").update(section="Education")
    report_rows(pd, partner, more["boys"], "PR-2", "QPR2", "QPR", d(2026, 6, 30), {"Akkar": 40}, 60)
    owed = report_rows(
        pd,
        partner,
        "# of caregivers",
        "PR-24",
        "QPR9",
        "QPR",
        d(2026, 3, 31),
        {"Akkar": 1, "Bekaa": 2},
        3,
        status="",
    )
    dm.ReportedIndicator.objects.filter(pk=owed[1].pk).update(submission_date=d(2026, 4, 20))
    now = timezone.now()
    run = SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="partner_reports",
        status=SyncRun.Status.SUCCEEDED,
        started_at=now - datetime.timedelta(hours=2),
        finished_at=now - datetime.timedelta(hours=1),
        rows_in=10,
        details={"datamart_count": 41, "read": 41, "other_country_skipped": 28},
    )
    held = dm.ReportedIndicator.objects.order_by("pk")
    held.update(synced_at=run.started_at + datetime.timedelta(minutes=5))
    earlier = list(held.values_list("pk", flat=True)[:3])
    dm.ReportedIndicator.objects.filter(pk__in=earlier).update(
        synced_at=run.started_at - datetime.timedelta(days=1)
    )
    return {**more, "cancelled": cancelled, "run": run}


@pytest.fixture
def validator(client, roles):
    user = User.objects.create_user(username="checker", password="pass-123456-x")
    user.groups.set(Group.objects.filter(name__in=(VIEWER, VALIDATORS)))
    client.force_login(user)
    return client


def _params(params: dict, world) -> dict:
    return {k: str(world["partner"].pk) if v == PARTNER else v for k, v in params.items()}


class _Figures(HTMLParser):
    """Every ``data-figure`` of a page: {key: the first number of the element's text}."""

    VOID = {"br", "img", "input", "meta", "link", "hr", "wbr", "source", "col"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[list] = []
        self.found: dict[str, list[int | None]] = {}

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append([tag, dict(attrs).get("data-figure"), []])

    def handle_endtag(self, tag):
        while self.stack:
            name, key, parts = self.stack.pop()
            if key is not None:
                number = re.search(r"-?\d[\d,]*", "".join(parts))
                self.found.setdefault(key, []).append(
                    int(number.group(0).replace(",", "")) if number else None
                )
            if name == tag:
                break

    def handle_data(self, data):
        for item in self.stack:
            item[2].append(data)


def drawn(html: str) -> dict[str, int | None]:
    parser = _Figures()
    parser.feed(html)
    out = {}
    for key, values in parser.found.items():
        assert len(set(values)) == 1, (key, values)  # a figure drawn twice reads the same
        out[key] = values[0]
    return out


def _csv(client, page: str, params: dict, what: str) -> list[dict[str, str]]:
    response = client.get(reverse("reports:check_rows", args=[page]), {**params, "what": what})
    assert response.status_code == 200, (what, response.content[:300])
    return list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))


def _expected(reports, partners) -> dict[str, int]:
    """Each figure as the view's context gives it: the Reporting progress tab and the Partners tab."""
    data = reports.context["data"]
    out = {
        "pr.indicator_reports": data["counts"]["indicator_reports"],
        "pr.progress_reports": data["counts"]["progress_reports"],
        "pr.late": data["summary"]["late"],
        "pr.reports_listed": reports.context["page_obj"].paginator.count,
        "pr.section_total.total": data["by_section"]["total"],
    }
    for s in data["counts"]["statuses"]:
        out[f"pr.status.{s['key']}"] = s["indicator_reports"]
        out[f"pr.status_reports.{s['key']}"] = s["progress_reports"]
    for name in ("by_pd_status", "other_status", "not_held", "no_pd"):
        out[f"pr.left_out.{name}"] = data["left_out"][name]
    for row in data["by_section"]["rows"]:
        for key, n in zip(reporting.STATUS_KEYS, row["counts"], strict=True):
            out[f"pr.section.{row['section']}.{key}"] = n
        out[f"pr.section.{row['section']}.total"] = row["total"]
    for key, n in zip(reporting.STATUS_KEYS, data["by_section"]["totals"], strict=True):
        out[f"pr.section_total.{key}"] = n
    labels = {label: key for key, label in rc.STATUS_LABELS.items()}
    for i, month in enumerate(data["by_month"]["labels"]):
        for label, values in data["by_month"]["series"].items():
            out[f"pr.month.{trail.month_key(month)}.{labels[label]}"] = values[i]
    for r in data["reports"]:
        out[f"pr.report.{r['key']}.indicators"] = r["indicators"]
    perf = partners.context["data"]["partners"]
    out.update(
        {
            "pr.partners": perf["partners"],
            "pr.partners_reported": perf["reported"],
            "pr.partners_not_reported": perf["not_reported"],
            "pr.follow_up": partners.context["page_obj"].paginator.count,
        }
    )
    for s in perf["by_status"]:
        out[f"pr.partners_status.{s['key']}"] = s["partners"]
    for row in perf["not_reported_list"]:
        for label, n in row["statuses"].items():
            out[f"pr.partner.{row['key']}.{labels[label]}"] = n
    return out


def evaluate(rows: list[dict[str, str]], figure: trail.Figure) -> int:
    """A figure counted again on the Kept rows CSV: its rows (the report statuses chosen, its conditions), then
    a distinct count of its unit's column."""
    found = [r for r in rows if r["report_status_chosen"] == "yes"]
    for name, value in figure.select:
        if name != "set":
            found = [r for r in found if CONDITIONS[name](r, value)]
    units = {r[UNIT_COLUMNS[figure.unit]] for r in found} - {""}
    return len(units)


def _drawn_keys(found: trail.Trail, tab: str, shown_reports: list[str]) -> set[str]:
    """The figures a tab draws: those of its blocks, and a table's cells of its rows shown (the reports' and
    the reports to follow up's Indicators)."""
    tabs = {f.key: b.tab for b in found.blocks for f in b.figures}
    keys = set()
    for f in found.figures:
        if f.listed:
            if f.key.removeprefix("pr.report.").removesuffix(".indicators") in shown_reports:
                keys.add(f.key)
        elif tabs[f.key] == tab and f.drawn:
            keys.add(f.key)
    return keys


# --------------------------------------------------------------------------------------------- every figure
@pytest.mark.parametrize("params", FILTERS)
def test_each_figure_is_the_page_s_rebuilt_from_the_rows_and_in_the_kept_rows(validator, world, params):
    params = _params(params, world)
    url = reverse("reports:partner_reporting")
    reports = validator.get(url, params)
    partners = validator.get(url, {**params, "tab": "partners"})
    expected = _expected(reports, partners)
    found = trail.progress_trail(PR, params, TODAY)
    keys = {f.key for f in found.figures}
    assert keys == set(expected), keys ^ set(expected)
    for f in found.figures:
        assert f.page == expected[f.key], f.key  # (a) the page's own figure
        assert f.rebuilt == f.page, f.key  # (c) rebuilt from the rows
    assert not found.differences
    # (b) the figures each tab draws: every one is the page's, and the tab draws every figure of its blocks
    for response, tab, listed in (
        (reports, "reports", [r["key"] for r in reports.context["page_obj"]]),
        (partners, "partners", [r["key"] for r in partners.context["page_obj"]]),
    ):
        shown = drawn(text(response))
        assert set(shown) == _drawn_keys(found, tab, listed), (
            tab,
            set(shown) ^ _drawn_keys(found, tab, listed),
        )
        for key, value in shown.items():
            assert value == expected[key], key
    # (d) the Kept rows CSV gives each figure again, by a distinct count of its unit
    kept = _csv(validator, PR, params, "kept")
    assert len(kept) == next(s for s in found.steps if s.key == "K").kept.rows
    for f in found.figures:
        if dict(f.select).get("set") == "chosen":
            assert evaluate(kept, f) == f.page, f.key
    # ... and by a sum of counts_once_* over the rows of the report statuses chosen (the help's pivot table)
    chosen = [r for r in kept if r["report_status_chosen"] == "yes"]
    for key, column in ONCE_COLUMNS.items():
        assert sum(int(r[column]) for r in chosen) == found.figure(key).page, key
    # ... and the figure's own CSV, by counts_once and by a distinct count of unit_key
    for f in found.figures:
        if f.key.startswith(("pr.status.", "pr.left_out.", "pr.partners", "pr.late", "pr.indicator_reports")):
            rows = _csv(validator, PR, params, f.what)
            assert sum(int(r["counts_once"]) for r in rows) == f.page, f.key
            assert len({r["unit_key"] for r in rows} - {""}) == f.page, f.key
    # (e) the chain adds up, in rows and in units
    filters = [s for s in found.steps if s.kind == "filter"]
    before = next(s for s in found.steps if s.key == "D4").kept
    for step in filters:
        assert step.kept.rows == before.rows - step.out.rows, step.key
        assert step.kept.indicator_reports == before.indicator_reports - step.out.indicator_reports, step.key
        assert step.kept.progress_reports == before.progress_reports - step.out.progress_reports, step.key
        before = step.kept
    assert next(s for s in found.steps if s.key == "K").kept == before
    # (f) the PD status step leaves out what the page's note says
    pd_status = next(s for s in filters if s.key == "pd_status")
    left = reports.context["data"]["left_out"]
    assert pd_status.out.indicator_reports == left["by_pd_status"] + left["no_pd"]
    lines = " ".join(line.words for line in pd_status.lines)
    assert f"Names no programme document: {trail.words(left['no_pd'], trail.IR)}" in lines
    if "pd_status" not in params or params["pd_status"]:
        assert f"not in NeuroDB: {trail.words(left['not_held'], trail.IR)}" in lines
        assert f"chosen): {trail.words(left['other_status'], trail.IR)}" in lines
    # the page draws the trail page, whose every figure is the same
    page = validator.get(reverse("reports:check_figures", args=[PR]), params)
    assert page.status_code == 200
    assert "rebuilt from the rows: the same." in text(page) and 'id="trail-differs"' not in text(page)


def test_the_world_leaves_rows_out_at_every_step_the_spec_names(validator, world):
    """Each left-out step of the world is there: a draft or cancelled programme document, one NeuroDB does not
    hold, a report naming none, a report naming no partner, rows from an earlier sync."""
    found = trail.progress_trail(PR, {}, TODAY)
    steps = {s.key: s for s in found.steps}
    assert steps["not_a_partnership"].out.indicator_reports == 1  # the cancelled PD's report
    assert steps["pd_status"].out.rows > 0
    assert found.left_out == {"other_status": 1, "not_held": 1, "no_pd": 1, "by_pd_status": 2}
    assert "3 kept from an earlier sync" in steps["D4"].lines[0].words
    assert "eTools said it had 41 partner report rows" in steps["D1"].lines[0].words
    assert "of every country: the Datamart does not filter this list by country" in steps["D1"].lines[0].words
    # 41 received = 28 of another country + 13 updated by the sync: nothing in red
    assert "NeuroDB received 41." == steps["D2"].lines[0].words
    assert not any(line.alert for step in found.steps for line in step.lines)
    partners = next(b for b in found.blocks if b.key == "partners")
    assert "neither a partner id nor a partner name: 1 progress report" in partners.notes[0].words
    rows = _csv(validator, PR, {}, "no_partner")
    assert {r["indicator_report_key"].split("|")[2] for r in rows} == {"# of tents"}


def test_rows_starting_before_the_date_neurodb_holds_reports_from_download_from_their_step(
    validator, world, monkeypatch
):
    start = datetime.date(2026, 2, 1)
    monkeypatch.setattr(rc, "history_from", lambda today=None: rc.HistoryFrom(start, rc.READ_FROM, start))
    found = trail.progress_trail(PR, {}, TODAY)
    step = next(s for s in found.steps if s.key == "D4b")
    before = dm.ReportedIndicator.objects.filter(period_start__lt=start)
    assert before.exists() and f"rows starting before it: {before.count():,}." in step.lines[0].words
    assert step.what == step.lines[0].what == "step:history"
    rows = _csv(validator, PR, {}, "step:history")
    assert sorted(r["record_id"] for r in rows) == sorted(
        str(i) for i in before.values_list("datamart_id", flat=True)
    )


def test_more_rows_than_a_download_holds_open_raw_etools_data_instead(validator, world, monkeypatch):
    from neurodb.datamart import raw

    monkeypatch.setattr(raw, "CSV_LIMIT", {"long_texts": 5, "other": 5})
    found = trail.progress_trail(PR, {}, TODAY)
    d4 = next(s for s in found.steps if s.key == "D4")
    assert d4.what == d4.lines[0].what == ""  # no CSV the download would refuse
    (line,) = [x for x in d4.lines if x.raw]
    assert line.raw == trail.SOURCE and "are more than the 5 a download holds" in line.words
    url = reverse("reports:check_figures", args=[PR])
    raw_url = reverse("admin:datamart_raw_source", args=[trail.SOURCE])
    page = text(validator.get(url))
    assert "what=step%3Astart" not in page and "what=step:start" not in page
    response = validator.get(reverse("reports:check_rows", args=[PR]), {"what": "step:start"})
    assert response.status_code == 400 and "more than 5" in response.content.decode()  # its address says why
    assert raw_url not in page and "Raw data: ask for staff status" in page  # no staff status
    User.objects.filter(username="checker").update(is_staff=True)
    assert f'<a href="{raw_url}" data-trail-raw>Raw eTools data</a>' in text(validator.get(url))


def test_a_filter_that_lets_rows_back_in_is_shown_in_red(world, monkeypatch):
    """The guard of the chain (SPEC_validation §3.8): a filter that keeps rows the step before it left out
    breaks the chain, and its step says so."""
    original = reporting.ReportFilters.queryset

    def queryset(self, today, *, steps=None, **kwargs):
        rows = original(self, today, steps=steps, **kwargs)
        if steps is not None:  # the section filter lets every row back in
            index = next(i for i, (name, _) in enumerate(steps) if name == "section")
            steps[index] = ("section", steps[0][1])
        return rows

    monkeypatch.setattr(reporting.ReportFilters, "queryset", queryset)
    found = trail.progress_trail(PR, {"report_type": "HR", "section": "Education"}, TODAY)
    steps = {s.key: s for s in found.steps}
    assert any(line.alert and trail.DIFFERS in line.words for line in steps["section"].lines)
    assert not any(line.alert for line in steps["report_type"].lines)


def test_the_trail_says_when_an_etools_sync_is_running(validator, world, monkeypatch):
    from neurodb.datamart import keys
    from neurodb.integrations import background

    url = reverse("reports:check_figures", args=[PR])
    assert 'id="trail-sync-running"' not in text(validator.get(url))
    for job in keys.ETOOLS_JOBS:
        monkeypatch.setattr(background, "is_running", lambda name, job=job: name == job)
        page = text(validator.get(url))
        assert 'id="trail-sync-running"' in page and "the figures can move until it ends" in page, job


@pytest.mark.parametrize(
    ("params", "step"),
    [
        ({"pd_status": "", "section": "Education"}, "section"),
        ({"pd_status": "", "emergency": "yes"}, "emergency"),
        ({"pd_status": "", "overdue": "1"}, "overdue"),
    ],
)
def test_a_step_s_csv_counts_once_the_units_it_leaves_out_entirely(validator, world, params, step):
    found = trail.progress_trail(PR, params, TODAY)
    left = next(s for s in found.steps if s.key == step)
    rows = _csv(validator, PR, params, f"step:{step}")
    assert len(rows) == left.out.rows > 0
    assert sum(int(r["counts_once"]) for r in rows) == left.out.indicator_reports
    assert sum(int(r["counts_once_progress_report"]) for r in rows) == left.out.progress_reports
    partly = {r["progress_report_unit"] for r in rows if r["progress_report_partly_kept"] == "yes"}
    assert len(partly) == left.partly[1] > 0  # some of their rows kept
    assert len({r["indicator_report_unit"] for r in rows if r["unit_partly_kept"] == "yes"}) == left.partly[0]
    for r in rows:
        if r["unit_partly_kept"] == "yes":
            assert r["counts_once"] == "0"
    if step in ("section", "overdue"):
        assert left.partly[0] > 0  # an indicator report with rows on both sides


def test_counts_once_partner_counts_each_partner_once_among_the_statuses_chosen(validator, world):
    """A partner with reports of a status chosen and of one not chosen has its 1 on a row of the status chosen:
    under every one or two report statuses, counts_once_partner summed over the rows chosen is the page's
    Partners, and over the others the partners of the reports not chosen."""
    for n in (1, 2):
        for statuses in itertools.combinations(reporting.STATUS_KEYS, n):
            params = {"pd_status": "", "status": list(statuses)}
            found = trail.progress_trail(PR, params, TODAY)
            kept = _csv(validator, PR, params, "kept")
            for chosen in ("yes", "no"):
                rows = [r for r in kept if r["report_status_chosen"] == chosen]
                partners = {r["partner_unit"] for r in rows} - {""}
                assert sum(int(r["counts_once_partner"]) for r in rows) == len(partners), (statuses, chosen)
                if chosen == "yes":
                    assert len(partners) == found.figure("pr.partners").page, statuses


def test_the_rows_that_set_the_status_name_the_record_whose_word_the_report_took(validator, world):
    rows = dm.ReportedIndicator.objects.filter(progress_report="PR-1").order_by("datamart_id")
    older, newer = rows.first(), rows.last()
    stamp = timezone.now()
    rows.update(last_modify_date=stamp - datetime.timedelta(days=2))
    dm.ReportedIndicator.objects.filter(pk=older.pk).update(report_status="Sent back")
    dm.ReportedIndicator.objects.filter(pk=newer.pk).update(last_modify_date=stamp)
    found = trail.progress_trail(PR, {}, TODAY)
    status = _csv(validator, PR, {}, "status")
    mine = [
        r for r in status if r["progress_report_key"] == rc.report_key(rows.values(*rc.ROW_FIELDS).first())
    ]
    assert {r["status_word_used"] for r in mine} == {"Accepted"}
    assert {r["status_word_from_record_id"] for r in mine} == {str(newer.datamart_id)}
    assert {r["status_word_of_this_row"] for r in mine} == {"Accepted", "Sent back"}
    assert found.rows.reports[mine[0]["progress_report_key"]].word == "Accepted"


def test_the_rows_that_set_the_status_name_the_newest_row_not_the_highest_id(validator, world):
    """The row that gives a report its word is the one changed last among the rows carrying that word, whatever
    the Datamart ids of the others: here the lowest id, while the highest id carries the word too, changed
    before the row of another word."""
    rows = list(dm.ReportedIndicator.objects.filter(progress_report="PR-1").order_by("datamart_id"))
    if len(rows) < 3:
        extra = rows[0]
        extra.pk, extra.datamart_id, extra.location = None, rows[-1].datamart_id + 1, "Zahle"
        extra.save()
        rows = list(dm.ReportedIndicator.objects.filter(progress_report="PR-1").order_by("datamart_id"))
    first, middle, last = rows[0], rows[1], rows[-1]
    stamp = timezone.now()
    each = dm.ReportedIndicator.objects.filter
    each(pk__in=[r.pk for r in rows]).update(
        report_status="Accepted", last_modify_date=stamp - datetime.timedelta(days=5)
    )
    each(pk=first.pk).update(last_modify_date=stamp)  # the newest change: Accepted
    each(pk=middle.pk).update(report_status="Sent back", last_modify_date=stamp - datetime.timedelta(days=1))
    each(pk=last.pk).update(last_modify_date=stamp - datetime.timedelta(days=3))  # Accepted, before Sent back
    status = _csv(validator, PR, {}, "status")
    key = rc.report_key(each(pk=first.pk).values(*rc.ROW_FIELDS).first())
    mine = [r for r in status if r["progress_report_key"] == key]
    assert {r["status_word_used"] for r in mine} == {"Accepted"}
    assert {r["status_word_from_record_id"] for r in mine} == {str(first.datamart_id)}
    newest = next(r for r in mine if r["record_id"] == str(first.datamart_id))
    assert newest["status_word_of_this_row"] == "Accepted"


# ------------------------------------------------------------------------------------------------ Home's block
def test_home_s_block_is_home_s_tiles_rows_and_partners_line(validator, world, monkeypatch):
    from neurodb.reports import home

    monkeypatch.setattr(home, "HOME_PDS", 1)
    part = get_part(validator, "/?section=", "reporting")
    shown = drawn(text(part))
    context = part.context
    found = trail.progress_trail("home", {"section": [""], "block": "reporting"}, TODAY)
    figures = {f.key: f for f in found.figures}
    assert not found.differences
    assert set(shown) == {f.key for f in found.figures if f.drawn}
    for key, value in shown.items():
        assert value == figures[key].page, key
    counts = context["reporting"]["counts"]
    assert figures["pr.indicator_reports"].page == counts["indicator_reports"]
    assert figures["home.pds"].page == context["reporting"]["pds"]["count"]
    (row,) = context["pd_rows"]
    for key in reporting.FOLLOW_UP:
        assert figures[f"home.pd.{row['pd_reference_number']}.{key}"].page == row["counts"][key]
    # the check line under the block's hint opens this trail
    link = re.search(r'<a href="([^"]+)" data-check-trail>Check these figures</a>', text(part))
    assert link and "block=reporting" in link.group(1) and link.group(1).startswith("/check/home/")
    page = validator.get(link.group(1).replace("&amp;", "&"))
    assert page.status_code == 200 and "rebuilt from the rows: the same." in text(page)
    # Home's block has no note on what the PD status filter leaves out: the trail gives it
    assert any("Home shows no note" in note for note in found.notes)
    assert found.left_out == reporting.left_out(reporting.ReportFilters.from_params({}), TODAY)


def test_home_s_block_with_no_progress_report_has_no_partners_line(validator, world):
    """A section with no progress report: Home draws its tiles (all 0) and no partners line, and the trail lists
    the partners line's figures as not drawn."""
    part = get_part(validator, "/?section=Nutrition", "reporting")
    shown = drawn(text(part))
    found = trail.progress_trail("home", {"section": ["Nutrition"], "block": "reporting"}, TODAY)
    assert found.figure("pr.progress_reports").page == 0 and not found.differences
    assert set(shown) == {f.key for f in found.figures if f.drawn}
    for key in ("pr.partners", "pr.partners_reported", "pr.partners_not_reported"):
        assert key not in shown and found.figure(key).note == "not drawn: no progress report", key


def test_a_home_row_s_csv_stays_on_its_programme_document_when_the_rows_move(validator, world):
    d = datetime.date
    other = PCA.objects.create(
        etl_id="15",
        partner=world["partner"],
        number="LEB/PD5",
        title="Five",
        status="active",
        start=d(2026, 1, 1),
        end=d(2026, 12, 31),
    )
    report_rows(
        other,
        world["partner"],
        "# of mats",
        "PR-50",
        "QPR1",
        "QPR",
        d(2026, 3, 31),
        {"Akkar": 1},
        1,
        status="Submitted",
    )
    params = {"section": "", "block": "reporting"}
    before = trail.progress_trail("home", params, TODAY)
    assert [
        f.key for f in before.figures if f.key.startswith("home.pd.") and f.key.endswith(".submitted")
    ] == [
        "home.pd.LEB/PD1.submitted",
        "home.pd.LEB/PD5.submitted",
    ]
    rows = _csv(validator, "home", params, "figure:home.pd.LEB/PD1.submitted")
    # an overdue report of LEB/PD5 puts its row first on Home: LEB/PD1's row moves, its CSV does not
    report_rows(
        other,
        world["partner"],
        "# of nets",
        "PR-51",
        "QPR8",
        "QPR",
        d(2026, 2, 28),
        {"Akkar": 1},
        1,
        status="",
    )
    compute.clear()  # a sync would give a new moment of the eTools figures, and new keys
    after_trail = trail.progress_trail("home", params, TODAY)
    assert [
        f.key for f in after_trail.figures if f.key.startswith("home.pd.") and f.key.endswith(".submitted")
    ] == [
        "home.pd.LEB/PD5.submitted",
        "home.pd.LEB/PD1.submitted",
    ]
    after = _csv(validator, "home", params, "figure:home.pd.LEB/PD1.submitted")
    assert {r["pd_unit"] for r in rows} == {r["pd_unit"] for r in after} == {f"pd:{world['pd'].pk}"}
    assert sum(int(r["counts_once"]) for r in after) == after_trail.figure("home.pd.LEB/PD1.submitted").page


def test_home_needs_its_block(validator, world):
    assert validator.get(reverse("reports:check_figures", args=["home"])).status_code == 404
    assert (
        validator.get(reverse("reports:check_figures", args=["home"]), {"block": "indicators"}).status_code
        == 404
    )
    assert validator.get(reverse("reports:check_figures", args=["partner-monitoring"])).status_code == 404


# ------------------------------------------------------------------------------------- By location, disaggregation
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"pd_status": ""},
        {"pd_status": "", "status": "accepted"},
        {"pd_status": "", "section": "Education"},
    ],
)
def test_the_tables_lines_are_the_tab_s(validator, world, params):
    for tab, key in (("locations", "pr.location_lines"), ("disaggregations", "pr.disaggregation_locations")):
        part = get_part(
            validator, reverse("reports:partner_reporting") + "?" + _encode({**params, "tab": tab}), "table"
        )
        shown = drawn(text(part))
        found = trail.table_trail(tab, params, TODAY)
        (figure,) = found.block.figures
        assert figure.key == key and figure.page == figure.rebuilt == part.context["page_obj"].paginator.count
        assert shown == {key: figure.page}
        rows = _csv(validator, PR, params, "lines" if tab == "locations" else "blocks")
        assert len(rows) == figure.page
        assert all(r["chosen_record_id"] in r["record_ids"].split() for r in rows)
        if params.get("status"):
            assert any("Report status not chosen" in line.words for line in found.steps)


def test_each_tab_filter_of_by_disaggregation_shows_its_step(validator, world):
    params = {
        "pd_status": "",
        "indicator": "girls",
        "location": "Akkar",
        "p_code": "x",
        "disaggregation": "y",
    }
    found = trail.table_trail("disaggregations", params, TODAY)
    words = " ".join(line.words for line in found.steps)
    assert 'Indicator does not contain every word of "girls"' in words
    assert 'P-code does not contain "x"' in words
    found = trail.table_trail("disaggregations", {"pd_status": "", "location": "Bekaa"}, TODAY)
    assert 'Location does not contain "Bekaa"' in " ".join(line.words for line in found.steps)
    found = trail.table_trail("disaggregations", {"pd_status": "", "disaggregation": "nothing"}, TODAY)
    assert 'No value matches "nothing"' in " ".join(line.words for line in found.steps)
    (figure,) = found.block.figures
    assert figure.page == figure.rebuilt == 0


def test_over_the_limit_the_table_is_not_drawn_and_the_trail_says_so(validator, world, monkeypatch):
    monkeypatch.setattr(reporting, "LOCATION_LIMIT", 2)
    found = trail.table_trail("locations", {"pd_status": ""}, TODAY)
    held = next(f for f in found.block.figures if f.key == "pr.location_rows_held")
    lines = next(f for f in found.block.figures if f.key == "pr.location_lines")
    assert held.page == held.rebuilt == dm.ReportedIndicator.objects.count() - 2  # the draft and none-PD rows
    assert lines.page is lines.rebuilt is None and not lines.drawn
    assert any("over it, the table is not drawn" in line.words for line in found.steps)
    part = get_part(validator, reverse("reports:partner_reporting") + "?pd_status=&tab=locations", "table")
    assert drawn(text(part)) == {"pr.location_rows_held": held.page}


# ------------------------------------------------------------------------------------------------ the hooks
@pytest.mark.parametrize("params", FILTERS)
def test_the_hooks_change_no_sql_and_no_figure(world, params):
    params = _params(params, world)
    filters = reporting.ReportFilters.from_params(params)
    steps: list = []
    assert str(filters.queryset(TODAY).query) == str(filters.queryset(TODAY, steps=steps).query)
    assert steps[0][0] == "start" and steps[-1][0] == "pd_status"
    assert str(filters.queryset(TODAY, by_pd_status=False).query) == str(
        filters.queryset(TODAY, by_pd_status=False, steps=[]).query
    )
    rows = filters.queryset(TODAY)
    plain = reporting.progress_reports(rows, TODAY)
    recorded = reporting.progress_reports(rows, TODAY, record=trail.Recorder())
    assert plain == recorded
    statuses = {r["key"]: r["status"] for r in plain}
    wanted = set(filters.statuses) or None
    lines = reporting.location_rows(rows, statuses, TODAY, wanted)
    found = reporting.location_rows(rows, statuses, TODAY, wanted, record=trail.Recorder())
    extra = ("indicator_report_key", "place", "record_ids", "chosen_record_id")
    assert lines["rows"] == [{k: v for k, v in r.items() if k not in extra} for r in found["rows"]]
    blocks = reporting.disaggregation_rows(rows, statuses, TODAY, wanted, params)
    again = reporting.disaggregation_rows(rows, statuses, TODAY, wanted, params, record=trail.Recorder())
    assert [b["total"] for b in blocks["blocks"]] == [b["total"] for b in again["blocks"]]
    assert reporting.units(rows).indicator_reports == sum(r["indicators"] for r in plain)


def test_the_page_never_passes_a_hook(validator, world, monkeypatch):
    """The pages call the hooked functions without ``steps`` or ``record``: a page's figures are never
    recorded."""
    import inspect

    seen = []
    original = reporting.ReportFilters.queryset

    def queryset(self, today, **kwargs):
        seen.append(("queryset", kwargs.get("steps")))
        return original(self, today, **kwargs)

    monkeypatch.setattr(reporting.ReportFilters, "queryset", queryset)
    hooked = ("progress_reports", "_status_words", "_location_cells", "location_rows", "disaggregation_rows")
    for name in hooked:
        function = getattr(reporting, name)

        def wrapper(*args, _function=function, _name=name, **kwargs):
            seen.append((_name, inspect.signature(_function).bind(*args, **kwargs).arguments.get("record")))
            return _function(*args, **kwargs)

        monkeypatch.setattr(reporting, name, wrapper)
    url = reverse("reports:partner_reporting")
    for tab in ("reports", "partners"):
        assert validator.get(url, {"tab": tab}).status_code == 200
    for tab in ("locations", "disaggregations"):
        get_part(validator, f"{url}?tab={tab}", "table")
    assert validator.get("/?section=").status_code == 200
    get_part(validator, "/?section=", "reporting")
    assert {name for name, _ in seen} == {"queryset", *hooked}
    assert all(hook is None for _, hook in seen)


def test_a_trail_runs_as_many_queries_whatever_the_number_of_figures(world, settings):
    settings.DEBUG = True  # computed at each call: nothing stored between the two counts
    with CaptureQueriesContext(connection) as few:
        small = trail.progress_trail(PR, {"pd_status": ""}, TODAY)
    pd, partner = world["pd"], world["partner"]
    for n in range(40):  # 40 more progress reports, each with its figures
        end = datetime.date(2025, 1, 1) + datetime.timedelta(days=30 * n)
        report_rows(pd, partner, f"# of extra {n}", f"PR-X{n}", f"HR{100 + n}", "HR", end, {"Akkar": 1}, 1)
    with CaptureQueriesContext(connection) as many:
        large = trail.progress_trail(PR, {"pd_status": ""}, TODAY)
    assert len(large.figures) > len(small.figures) + 40
    assert len(many) == len(few)


def test_a_trail_is_kept_two_minutes_for_every_worker(world):
    from neurodb.core import compute

    trail.progress_trail(PR, {}, TODAY)
    with CaptureQueriesContext(connection) as again:
        trail.progress_trail(PR, {}, TODAY)
    assert len(again) <= 2  # the moment of the eTools figures, read for the key; nothing computed
    compute.reset_memo()


# ------------------------------------------------------------------------------------------------- the words
def test_the_trail_s_sentences_say_none_of_the_words_of_the_code(world):
    found = trail.progress_trail(PR, {"pd_status": "", "section": "Education", "status": "accepted"}, TODAY)
    table = trail.table_trail("disaggregations", {"pd_status": "", "location": "x"}, TODAY)
    sentences = [*found.notes, *found.filter_words, *(table_line.words for table_line in table.steps)]
    for step in found.steps:
        sentences += [step.title, step.kept_words, step.out_words, *(line.words for line in step.lines)]
    for block in (*found.blocks, table.block):
        sentences.append(block.title)
        sentences += [line.words for line in block.notes]
        sentences += [x for f in block.figures for x in (f.label, f.how, f.note)]
    for sentence in sentences:
        assert not NEVER.findall(sentence), sentence


def _encode(params: dict) -> str:
    from django.http import QueryDict

    query = QueryDict(mutable=True)
    for key, value in params.items():
        query.setlist(key, value if isinstance(value, list) else [value])
    return query.urlencode()
