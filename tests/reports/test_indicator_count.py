"""The hint under each progress report tile counts indicators, each once, of active programme documents (the
user's decision of 19 Nov 2026): "the count should be unique per indicator number, indicators for active PDs".

An indicator is its eTools indicator id within its programme document, else its title there (case and spaces
aside): the rule of Partner monitoring (``identity.ident_of``). Within one tile an indicator in several reports
(QPR1 to QPR4, monthly HR reports, every year) counts once; the seven tiles are counted apart, so they no
longer add up. The progress report numbers stay as they were: every PD status the filter reads (the running
programme documents, active, signed and suspended, as Home reads them and writes them in its links; Progress
reports alone opens on the active ones since 21 Nov 2026). With PD statuses chosen on Progress reports: the
indicators of the active ones among them, else of those chosen.

The world, on the suite's day (15 October 2026), each indicator row at two locations:

- LEB/A (active): "# of children reached" (eTools id 101) in QPR1 to QPR4 of 2026, QPR4 of 2025 and the monthly
  HR1 to HR3; "# of caregivers reached" (102) in QPR1; "Teachers trained" (no eTools id) in QPR1, written
  "teachers  Trained" in QPR2;
- LEB/B (active): "# of children reached" (eTools id 201) in QPR1 and QPR2; "Teachers trained" (no eTools id)
  in QPR1;
- LEB/S (signed) and LEB/U (suspended): one report each, of one indicator;
- LEB/E (ended): one report of one indicator.
"""

from __future__ import annotations

import csv
import datetime
import io
import itertools
import re

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from openpyxl import load_workbook

from neurodb.accounts.models import User
from neurodb.accounts.roles import VALIDATORS, VIEWER
from neurodb.assistant import tools
from neurodb.datamart import identity, reporting, services, trail
from neurodb.datamart import models as dm
from neurodb.partnerships.models import PCA, PartnerOrganization
from tests.web.parts import get_part, text

pytestmark = pytest.mark.django_db
D = datetime.date
TODAY = D(2026, 10, 15)  # the suite's day (tests/conftest.py SITE_NOW)
PR = "progress-reports"
CHILDREN, CAREGIVERS, TEACHERS = "# of children reached", "# of caregivers reached", "Teachers trained"
KITS, TENTS = "# of kits", "# of tents"
_ids = itertools.count(80_000)
HINT = re.compile(r'<span class="kpi__hint"([^>]*)>([^<]*)</span>')
ATTRIBUTE = re.compile(r'([a-z-]+)="([^"]*)"')
OLD_NOTE = "the indicator reports in them (one indicator in one progress report) are under it"


def _pd(partner, number, status):
    return PCA.objects.create(
        etl_id=number,
        partner=partner,
        partner_name=partner.name,
        number=number,
        title=f"Programme {number}",
        status=status,
        start=D(2025, 1, 1),
        end=D(2026, 12, 31),
        section_names=["Education"],
    )


def _report(pd, number, start, end, indicators, word="", *, kind="QPR", due=None, submitted=None):
    """One progress report: each of its indicators ``(title, eTools id[, PD output])`` at two locations."""
    for title, etools_id, *output in indicators:
        for location in ("Akkar", "Bekaa"):
            dm.ReportedIndicator.objects.create(
                datamart_id=next(_ids),
                partner=pd.partner,
                intervention=pd,
                partner_name=pd.partner.name,
                pd_reference_number=pd.number,
                progress_report=f"PR-{pd.number}-{number}-{end}",
                indicator_report=f"IR-{pd.number}-{number}-{end}-{etools_id or title}-{''.join(output)}",
                pd_output="".join(output),
                report_number=number,
                report_type=kind,
                report_status=word,
                period_start=start,
                period_end=end,
                due_date=due or end + datetime.timedelta(days=15),
                submission_date=submitted,
                section="Education",
                indicator=title,
                etools_indicator_id=etools_id,
                location=location,
                achievement_in_period="10",
            )


def _pd_indicator(pd, source_id, title, output):
    """One PD indicator in eTools (``PDIndicator``), at two locations, as Partner monitoring reads them."""
    for location in ("Akkar", "Bekaa"):
        dm.PDIndicator.objects.create(
            datamart_id=next(_ids),
            source_id=source_id,
            intervention=pd,
            pd_reference_number=pd.number,
            title=title,
            lower_result_name=output,
            location_name=location,
        )


@pytest.fixture
def world(db):
    partner = PartnerOrganization.objects.create(etl_id="81", name="Partner Omega", partner_type="CSO")
    a = _pd(partner, "LEB/A", "active")
    b = _pd(partner, "LEB/B", "active")
    signed = _pd(partner, "LEB/S", "signed")
    suspended = _pd(partner, "LEB/U", "suspended")
    ended = _pd(partner, "LEB/E", "ended")
    children = (CHILDREN, "101")
    _report(a, "QPR1", D(2026, 1, 1), D(2026, 3, 31), [children, (CAREGIVERS, "102"), (TEACHERS, "")],
            "Accepted", submitted=D(2026, 4, 10))  # fmt: skip
    _report(a, "QPR2", D(2026, 4, 1), D(2026, 6, 30), [children, ("teachers  Trained", "")], "Submitted",
            submitted=D(2026, 7, 10))  # fmt: skip
    _report(a, "QPR3", D(2026, 7, 1), D(2026, 9, 30), [children], due=D(2026, 10, 30))  # Due
    _report(a, "QPR4", D(2026, 10, 1), D(2026, 12, 31), [children])  # Not yet due
    _report(a, "QPR4", D(2025, 10, 1), D(2025, 12, 31), [children], "Accepted", submitted=D(2026, 1, 10))
    _report(
        a, "HR1", D(2026, 7, 1), D(2026, 7, 31), [children], "Accepted", kind="HR", submitted=D(2026, 8, 5)
    )
    _report(
        a, "HR2", D(2026, 8, 1), D(2026, 8, 31), [children], "Sent back", kind="HR", submitted=D(2026, 9, 5)
    )
    _report(a, "HR3", D(2026, 9, 1), D(2026, 9, 30), [children], kind="HR", due=D(2026, 10, 10))  # Overdue
    _report(b, "QPR1", D(2026, 1, 1), D(2026, 3, 31), [(CHILDREN, "201"), (TEACHERS, "")], "Accepted",
            submitted=D(2026, 4, 10))  # fmt: skip
    _report(
        b, "QPR2", D(2026, 4, 1), D(2026, 6, 30), [(CHILDREN, "201")], "Accepted", submitted=D(2026, 7, 9)
    )
    _report(signed, "QPR1", D(2026, 1, 1), D(2026, 3, 31), [("# of kits", "301")], "Accepted",
            submitted=D(2026, 4, 10))  # fmt: skip
    _report(suspended, "QPR2", D(2026, 4, 1), D(2026, 6, 30), [("# of tents", "401")])  # Overdue
    _report(ended, "QPR1", D(2026, 1, 1), D(2026, 3, 31), [("# of chairs", "501")], "Accepted",
            submitted=D(2026, 4, 10))  # fmt: skip
    return {"a": a, "b": b, "signed": signed, "suspended": suspended, "ended": ended}


@pytest.fixture
def validator(client, roles):
    user = User.objects.create_user(username="checker", password="pass-123456-x")
    user.groups.set(Group.objects.filter(name__in=(VIEWER, VALIDATORS)))
    client.force_login(user)
    return client


# the running programme documents, as Home reads them (Progress reports alone opens on the active ones)
RUNNING = {"pd_status": ["active", "signed", "suspended"]}


def _counts(params=None) -> dict:
    return services.partner_reporting(RUNNING if params is None else params, TODAY, options=False)["counts"]


def _tiles(counts: dict, figure: str) -> dict[str, int]:
    """Each tile's figure: ``progress_reports`` or ``indicators``, the total under "total"."""
    return {"total": counts[figure], **{s["key"]: s[figure] for s in counts["statuses"]}}


def _hints(html: str) -> dict[str, tuple[str, str]]:
    """The hints under the seven tiles: {figure key: (words, hover title)}."""
    grid = html[html.index('class="kpi-grid kpi-grid--7"') :]
    out = {}
    for attributes, words in HINT.findall(grid)[:7]:
        found = dict(ATTRIBUTE.findall(attributes))
        out[found["data-figure"]] = (words.strip(), found.get("title", ""))
    return out


def _words(html: str) -> str:
    """The text a person reads: the figure trail's keys taken away, the spaces as one."""
    return " ".join(re.sub(r'<span data-figure="[^"]*">([^<]*)</span>', r"\1", html).split())


# --------------------------------------------------------------------------------------------- the indicator
@pytest.fixture
def placed(db):
    """One active programme document as eTools holds its indicators: two of one title under two PD outputs
    (7001, 7002), and "# of kits" (7101)."""
    partner = PartnerOrganization.objects.create(etl_id="82", name="Partner Sigma", partner_type="CSO")
    pd = _pd(partner, "LEB/T", "active")
    _pd_indicator(pd, 7001, CHILDREN, "Output 1")
    _pd_indicator(pd, 7002, CHILDREN, "Output 2")
    _pd_indicator(pd, 7101, KITS, "Output 1")
    return pd


def _accepted(pd, number, end, indicators):
    """An Accepted quarterly progress report ending on ``end``."""
    start = D(end.year, end.month - 2, 1)
    _report(pd, number, start, end, indicators, "Accepted", submitted=end + datetime.timedelta(days=10))


def test_an_indicator_is_the_pd_indicator_partner_monitoring_places_its_row_on(placed):
    """Partner monitoring's rule (``identity.Rule.of``): the PD indicator of the row's eTools id, else of its
    title, else of its PD output and title; a row it does not place, by its title (and its PD output when two
    of the PD's indicators share the title), case and spaces aside."""
    rule = identity.Rule.read([placed.pk])
    bare = {
        "intervention_id": placed.pk,
        "pd_reference_number": "LEB/T",
        "etools_indicator_id": "",
        "pd_output": "",
    }

    def key(**row):
        return reporting.pd_indicator_key({**bare, **row}, rule)

    pd = f"pd:{placed.pk}"
    assert key(etools_indicator_id="7101", indicator="Another title") == key(indicator=KITS) == f"{pd}|s7101"
    assert key(indicator=CHILDREN, pd_output="Output 1") == f"{pd}|s7001"
    assert key(indicator=" # OF children  reached", pd_output="output  2") == f"{pd}|s7002"
    # a title two indicators of the PD share, with a PD output neither has: the title and the output
    assert key(indicator=CHILDREN, pd_output="Output 9") == f"{pd}|t# of children reached|ooutput 9"
    assert key(indicator=CHILDREN, pd_output="Output 9") != key(indicator=CHILDREN, pd_output="Output 8")
    # a title no indicator of the PD has: the title, whatever eTools id the row carries
    assert key(etools_indicator_id="999", indicator=TEACHERS) == key(indicator=" teachers  TRAINED ")
    assert key(indicator=TEACHERS) == f"{pd}|tteachers trained"
    assert key(indicator=TEACHERS) != key(intervention_id=placed.pk + 1, indicator=TEACHERS)
    # a programme document NeuroDB does not hold: by the report's reference number and the title
    assert (
        key(intervention_id=None, etools_indicator_id="7101", indicator=TEACHERS)
        == "ref:LEB/T|tteachers trained"
    )


def test_two_indicators_of_one_title_under_two_outputs_count_two(placed):
    """Their rows carry no eTools id (none does in production): Partner monitoring places them by their PD
    output, and so are they counted, as the report's own Indicators column counts them."""
    _accepted(placed, "QPR1", D(2026, 3, 31), [(CHILDREN, "", "Output 1"), (CHILDREN, "", "Output 2")])
    data = services.partner_reporting({}, TODAY, options=False)
    assert [r["indicators"] for r in data["reports"]] == [2]
    assert data["counts"]["indicators"] == 2 and _tiles(data["counts"], "indicators")["accepted"] == 2


def test_an_indicator_whose_rows_carry_its_etools_id_only_sometimes_counts_once(placed):
    _accepted(placed, "QPR1", D(2026, 3, 31), [(KITS, "7101", "Output 1")])
    _accepted(placed, "QPR2", D(2026, 6, 30), [(KITS, "", "Output 1")])
    counts = _counts()
    assert counts["progress_reports"] == 2 and counts["indicators"] == 1


def test_rows_of_a_shared_title_partner_monitoring_cannot_place_count_by_their_pd_output(placed):
    _accepted(placed, "QPR1", D(2026, 3, 31), [(CHILDREN, "", "Output 8"), (CHILDREN, "", "Output 9")])
    _accepted(placed, "QPR2", D(2026, 6, 30), [(CHILDREN, "", "output  9")])
    assert _counts()["indicators"] == 2


def test_rows_of_a_title_no_indicator_has_count_by_their_title_with_or_without_an_etools_id(placed):
    _accepted(placed, "QPR1", D(2026, 3, 31), [(TENTS, "999", "Output 3")])
    _accepted(placed, "QPR2", D(2026, 6, 30), [(" # of TENTS", "", "Output 3")])
    assert _counts()["indicators"] == 1


def test_each_indicator_counts_once_in_the_total_and_once_in_each_tile_it_is_in(world):
    counts = _counts()
    # the progress reports are as before: the running programme documents, the ended one left out
    assert _tiles(counts, "progress_reports") == {
        "total": 12,
        "accepted": 6,
        "submitted": 1,
        "sent_back": 1,
        "due": 1,
        "overdue": 2,
        "not_yet_due": 1,
    }
    assert counts["indicator_reports"] == 16  # one indicator in one progress report, as before
    # LEB/A's three and LEB/B's two: "# of children reached" once however many reports, periods, types and
    # years carry it; the two programme documents' "# of children reached" and "Teachers trained" are four
    assert _tiles(counts, "indicators") == {
        "total": 5,
        "accepted": 5,
        "submitted": 2,
        "sent_back": 1,
        "due": 1,
        "overdue": 1,
        "not_yet_due": 1,
    }
    assert counts["indicator_pd_statuses"] == ["active"] and counts["indicators_of_active"]


def test_signed_and_suspended_reports_count_but_not_their_indicators(world):
    counts = _counts()
    reports = services.partner_reporting(RUNNING, TODAY, options=False)["reports"]
    assert {r["pd_reference_number"] for r in reports} == {"LEB/A", "LEB/B", "LEB/S", "LEB/U"}
    # the page's own default, Active, leaves them out
    bare = services.partner_reporting({}, TODAY, options=False)["reports"]
    assert {r["pd_reference_number"] for r in bare} == {"LEB/A", "LEB/B"}
    # LEB/S's Accepted report and LEB/U's Overdue one are counted; their indicators are not
    assert _tiles(counts, "progress_reports")["overdue"] == 2 and _tiles(counts, "indicators")["overdue"] == 1
    every = _counts({"pd_status": ""})  # every status: the ended PD's report too, never its indicator
    assert every["progress_reports"] == 13 and every["indicators"] == 5


def test_a_report_status_chosen_counts_the_indicators_of_its_reports_only(world):
    counts = _counts({**RUNNING, "status": "accepted"})
    assert counts["progress_reports"] == 6 and counts["indicators"] == 5
    assert _tiles(counts, "indicators") == {"total": 5, "accepted": 5, **dict.fromkeys(
        ("submitted", "sent_back", "due", "overdue", "not_yet_due"), 0)}  # fmt: skip
    assert _counts({**RUNNING, "status": "overdue"})["indicators"] == 1  # LEB/U's is not of an active PD


@pytest.mark.parametrize(
    ("pd_status", "reports", "indicators", "statuses", "words"),
    [
        (["active", "signed"], 11, 5, ["active"], "active"),
        (["active"], 10, 5, ["active"], "active"),
        ("", 13, 5, ["active"], "active"),  # every status
        (["signed"], 1, 1, ["signed"], "signed"),
        (["signed", "suspended"], 2, 2, ["signed", "suspended"], "signed or suspended"),
        (["ended"], 1, 1, ["ended"], "ended"),
    ],
)
def test_pd_statuses_chosen_count_the_active_among_them_else_those_chosen(
    world, pd_status, reports, indicators, statuses, words
):
    counts = _counts({"pd_status": pd_status})
    assert counts["progress_reports"] == reports and counts["indicators"] == indicators
    assert counts["indicator_pd_statuses"] == statuses
    assert counts["indicator_pd_words"] == words
    assert counts["indicators_of_active"] == (statuses == ["active"])


# ---------------------------------------------------------------------------------------------- the pages
def test_home_and_progress_reports_show_the_same_indicators_and_say_how_they_are_counted(
    client_viewer, world
):
    home = client_viewer.get("/home/?section=")
    assert home.status_code == 200
    figures = home.context["reporting"]["counts"]
    page = client_viewer.get(home.context["tile_urls"]["total"])
    assert page.context["data"]["counts"] == figures
    hints = _hints(text(home))
    assert hints == _hints(text(page))
    title = "Each indicator once, in active programme documents"
    assert hints == {
        "pr.indicators": ("5 indicators", title),
        "pr.indicators.accepted": ("5 indicators", title),
        "pr.indicators.submitted": ("2 indicators", title),
        "pr.indicators.sent_back": ("1 indicator", title),
        "pr.indicators.due": ("1 indicator", title),
        "pr.indicators.overdue": ("1 indicator", title),
        "pr.indicators.not_yet_due": ("1 indicator", title),
    }
    for key in ("accepted", "overdue"):  # each status tile opens the page on that status, with the same hint
        status_page = client_viewer.get(home.context["tile_urls"][key])
        assert _hints(text(status_page))[f"pr.indicators.{key}"] == hints[f"pr.indicators.{key}"]
    home_words, page_words = _words(text(home)), _words(text(page))
    assert OLD_NOTE not in home_words and OLD_NOTE not in page_words
    assert "indicator report" not in " ".join(words for words, _ in hints.values())
    # Home says how they are counted; Progress reports shows no note (the owner's decision of 21 Nov 2026)
    assert "An indicator can be in reports of several statuses" in home_words
    assert "An indicator can be in reports of several statuses" not in page_words


def test_progress_reports_says_which_pd_statuses_its_indicators_are_of(client_viewer, world):
    url = reverse("reports:partner_reporting")
    page = client_viewer.get(url, {"pd_status": ["signed", "suspended"]})
    hints = _hints(text(page))
    assert hints["pr.indicators"] == (
        "2 indicators",
        "Each indicator once, in signed or suspended programme documents",
    )
    # the hint's hover title says it; the page has no note under the tiles
    assert "Active is not among the PD statuses chosen" not in _words(text(page))
    chosen = client_viewer.get(url, {"pd_status": ["active", "signed"]})
    assert _hints(text(chosen))["pr.indicators"] == (
        "5 indicators",
        "Each indicator once, in active programme documents",
    )


def test_the_table_s_indicators_column_still_counts_each_report_s_own(client_viewer, world):
    page = client_viewer.get(reverse("reports:partner_reporting"), RUNNING)
    by_report = {(r["pd_reference_number"], r["report_number"], r["period_end"]): r["indicators"]
                 for r in page.context["data"]["reports"]}  # fmt: skip
    assert by_report[("LEB/A", "QPR1", D(2026, 3, 31))] == 3
    assert by_report[("LEB/S", "QPR1", D(2026, 3, 31))] == 1  # a signed PD's report: its own indicators


def test_ask_neurodb_gives_the_tiles_indicators_too(world):
    """Ask reads Progress reports as the page opens: the active programme documents (LEB/S's Accepted report
    and LEB/U's Overdue one are not of them)."""
    out = tools.run("partner_reporting", {})
    assert out["indicators"] == 5
    assert out["tiles"]["Accepted"] == {"progress_reports": 5, "indicators": 5, "indicator_reports": 8}
    assert out["tiles"]["Overdue"] == {"progress_reports": 1, "indicators": 1, "indicator_reports": 1}


# ------------------------------------------------------------------------------------------ the figure trail
def _csv(client, page: str, params: dict, what: str) -> list[dict[str, str]]:
    response = client.get(reverse("reports:check_rows", args=[page]), {**params, "what": what})
    assert response.status_code == 200, (what, response.content[:300])
    return list(csv.DictReader(io.StringIO(b"".join(response.streaming_content).decode("utf-8-sig"))))


def test_the_figure_trail_explains_and_lists_each_indicator_once(validator, world):
    found = trail.progress_trail(PR, {}, TODAY)
    keys = {f.key for f in found.figures}
    assert "pr.indicator_reports" not in keys and not any(k.startswith("pr.status_indicators.") for k in keys)
    total = found.figure("pr.indicators")
    assert total.unit == trail.INDICATOR and total.page == total.rebuilt == 5
    assert "each indicator once" in total.how and "active programme documents only" in total.how
    assert found.figure("pr.indicators.accepted").page == found.figure("pr.indicators.accepted").rebuilt == 5
    assert found.figure("pr.indicators.overdue").rebuilt == 1
    assert not found.differences
    rows = _csv(validator, PR, {}, total.what)
    assert len(rows) == 5 and sum(int(r["counts_once"]) for r in rows) == 5
    assert len({r["unit_key"] for r in rows}) == 5
    children = next(
        r for r in rows if r["programme_document"] == "LEB/A" and r["etools_indicator_id"] == "101"
    )
    assert children["indicator"] == CHILDREN and children["pd_status_in_neurodb"] == "Active"
    assert children["progress_reports"] == "8"  # QPR1 to QPR4, last year's QPR4 and HR1 to HR3
    assert len(children["progress_report_keys"].split(" ; ")) == 8
    teachers = [r for r in rows if r["etools_indicator_id"] == ""]
    assert sorted((r["programme_document"], r["progress_reports"]) for r in teachers) == [
        ("LEB/A", "2"),
        ("LEB/B", "1"),
    ]
    assert not any(r["programme_document"] in ("LEB/S", "LEB/U", "LEB/E") for r in rows)
    # a tile's own: the Overdue reports' indicators (LEB/U's left out: a suspended PD)
    overdue = _csv(validator, PR, {}, found.figure("pr.indicators.overdue").what)
    assert [(r["programme_document"], r["etools_indicator_id"]) for r in overdue] == [("LEB/A", "101")]
    # the rows kept: one pivot table gives the figure again (indicator_counted, counts_once_indicator)
    kept = _csv(validator, PR, {}, "kept")
    chosen = [r for r in kept if r["report_status_chosen"] == "yes" and r["indicator_counted"] == "yes"]
    assert len({r["indicator_unit"] for r in chosen}) == 5
    assert sum(int(r["counts_once_indicator"]) for r in kept) == 5


def test_home_s_trail_has_the_same_indicators(validator, world):
    part = get_part(validator, "/home/?section=", "reporting")
    found = trail.progress_trail("home", {"section": [""], "block": "reporting"}, TODAY)
    counts = part.context["reporting"]["counts"]
    assert found.figure("pr.indicators").page == counts["indicators"] == 5
    for s in counts["statuses"]:
        figure = found.figure(f"pr.indicators.{s['key']}")
        assert figure.page == figure.rebuilt == s["indicators"], s["key"]


def test_the_trail_places_each_row_as_the_page_does(validator, placed):
    _accepted(placed, "QPR1", D(2026, 3, 31), [(CHILDREN, "", "Output 1"), (CHILDREN, "", "Output 2"),
                                               (KITS, "7101", "Output 1")])  # fmt: skip
    _report(placed, "QPR2", D(2026, 4, 1), D(2026, 6, 30), [(KITS, "", "Output 1")], "Submitted",
            submitted=D(2026, 7, 10))  # fmt: skip
    counts = _counts()
    assert _tiles(counts, "indicators") == {"total": 3, "accepted": 3, "submitted": 1, **dict.fromkeys(
        ("sent_back", "due", "overdue", "not_yet_due"), 0)}  # fmt: skip
    found = trail.progress_trail(PR, {}, TODAY)
    for key in ("pr.indicators", "pr.indicators.accepted", "pr.indicators.submitted"):
        figure = found.figure(key)
        assert figure.page == figure.rebuilt, key
    assert found.figure("pr.indicators").rebuilt == 3 and not found.differences
    rows = _csv(validator, PR, {}, found.figure("pr.indicators").what)
    # each indicator's own eTools id and PD output, also when its rows carry no id
    assert sorted((r["etools_indicator_id"], r["pd_output"], r["progress_reports"]) for r in rows) == [
        ("7001", "Output 1", "1"),
        ("7002", "Output 2", "1"),
        ("7101", "Output 1", "2"),
    ]
    kept = _csv(validator, PR, {}, "kept")
    assert sum(int(r["counts_once_indicator"]) for r in kept) == 3


def test_the_indicators_csv_lists_them_by_programme_document_then_title(validator, db):
    """The browser check of 19 Nov 2026 read the CSV in the order of its keys as text ("pd:12|..." before
    "pd:1|...", "s1000" before "s900"): the programme documents out of order. A person reads it by programme
    document, then by indicator title."""
    partner = PartnerOrganization.objects.create(etl_id="83", name="Partner Tau", partner_type="CSO")
    first = PCA.objects.create(id=99_999, etl_id="LEB/AA", partner=partner, number="LEB/AA", status="active")
    second = PCA.objects.create(
        id=100_000, etl_id="LEB/BB", partner=partner, number="LEB/BB", status="active"
    )
    for pd in (second, first):
        _accepted(pd, "QPR1", D(2026, 3, 31), [("Zebra crossings built", "1000"), ("access ramps built", "900"),
                                               ("Benches placed", "")])  # fmt: skip
    found = trail.progress_trail(PR, {}, TODAY)
    rows = _csv(validator, PR, {}, found.figure("pr.indicators").what)
    assert [(r["programme_document"], r["indicator"]) for r in rows] == [
        ("LEB/AA", "access ramps built"),
        ("LEB/AA", "Benches placed"),
        ("LEB/AA", "Zebra crossings built"),
        ("LEB/BB", "access ramps built"),
        ("LEB/BB", "Benches placed"),
        ("LEB/BB", "Zebra crossings built"),
    ]


# ------------------------------------------------------------------------------------------------ the Excel file
def test_the_excel_by_status_sheet_holds_the_first_tile_and_says_its_indicators_do_not_add_up(
    validator, world
):
    """Summing the indicators of the status rows gives 11, while the first tile's hint is 5: the sheet holds
    the first tile in a Total row, and says why the status rows' indicators do not add up to it."""
    response = validator.get(reverse("reports:partner_reporting"), {**RUNNING, "export": "xlsx"})
    rows = list(load_workbook(io.BytesIO(response.content))["By status"].iter_rows(values_only=True))
    assert rows[0] == ("status", "progress_reports", "indicators", "indicator_reports", "note")
    statuses, total = rows[1:-1], rows[-1]
    assert [r[0] for r in statuses] == ["Accepted", "Submitted", "Sent back", "Due", "Overdue", "Not yet due"]
    assert total[:4] == ("Total", 12, 5, 16)
    # the progress reports and indicator reports add up to it; the indicators do not, and the note says so
    assert sum(r[1] for r in statuses) == 12 and sum(r[3] for r in statuses) == 16
    assert sum(r[2] for r in statuses) == 11
    assert total[4] == (
        "Each indicator once, in active programme documents: an indicator in reports of several statuses is "
        "in each of their rows, so the indicators above do not add up to this total."
    )
