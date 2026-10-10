"""The Records tab of Monitoring insights: the records list and, grouped by visit, the visits table
(order, urgency rows, pages, CSV, team cells), the visit page with a card per record and its window, the
look-up, the reviews, the drill-down window (records), and the action points of a visit."""

from __future__ import annotations

import csv
import datetime
import io
import re
from urllib.parse import urlencode

import pytest
from django.urls import reverse

from neurodb.accounts.models import Section, User
from neurodb.accounts.roles import SECTION_EDITOR, ensure_groups
from neurodb.datamart import fm
from neurodb.fmm import refresh, views
from neurodb.fmm import scope as scope_module
from neurodb.fmm.models import ScoreSetting, Visit, VisitActionPoint, VisitEntity, VisitReview

from .conftest import CANARY_TEXT, LEAD, MEMBER, MEMBER_EMAIL

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
TABLE = reverse("fmm:visits")
REFERENCE_KEY = fm.visit_key(None, "FM/2026/9", 0)
# by urgency (FMS's formula, a visit's being its most urgent record's: 58, 39, 37, 23, 19, 14), then the
# two without a score (and so without urgency) by visit date (start, else end), newest first
URGENCY_ORDER = ["1723", "1727", "1726", REFERENCE_KEY, "1722", "1728", "1724", "1725"]
# the records list: by each record's own urgency (1723's two records 58, 1727's 39, 1726's two 37, the
# reference visit's 23, 1722's 19, 18 and 13 around 1728's 14), then the records without one
RECORD_ORDER = [111, 112, 131, 121, 122, 161, 103, 102, 171, 101, 141, 151]
VISITS = {"view": "visits"}  # the list grouped by visit


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today: today or TODAY)


def _rows(html: str) -> list[str]:
    body = html.split('id="fmm-visits-table"', 1)[1].split("</table>", 1)[0]
    return re.findall(r"<tr data-href.*?</tr>", body, re.S)


def _keys(html: str) -> list[str]:
    return [re.search(r'data-href="/fmm/visits/([^/]+)/', row).group(1) for row in _rows(html)]


def _records(html: str) -> list[int]:
    return [int(re.search(r'data-record="(\d+)"', row).group(1)) for row in _rows(html)]


def _user(username: str, role: str, section=None) -> User:
    user = User.objects.create_user(
        username=username, email=f"{username}@example.org", password="x-pass-123456", section=section
    )
    user.groups.add(ensure_groups()[role])
    return user


# ------------------------------------------------------------------------------------------ the table
def test_the_records_list_is_sorted_by_urgency_then_date(built, client_viewer):
    html = client_viewer.get(TABLE).content.decode()
    assert _records(html) == RECORD_ORDER
    assert _keys(html) == ["1723", "1723", "1727", "1726", "1726", REFERENCE_KEY, "1722", "1722", "1728"] + [
        "1722",
        "1724",
        "1725",
    ]
    assert _records(client_viewer.get(TABLE, {"sort": "-date"}).content.decode())[:4] == [141, 131, 121, 122]
    assert _records(client_viewer.get(TABLE, {"sort": "quality"}).content.decode())[:3] == [111, 112, 161]
    assert _records(client_viewer.get(TABLE, {"sort": "nonsense"}).content.decode()) == RECORD_ORDER


def test_grouped_by_visit_the_list_is_the_visits_table(built, client_viewer):
    html = client_viewer.get(TABLE, VISITS).content.decode()
    assert _keys(html) == URGENCY_ORDER and 'data-record="' not in html.split('id="fmm-visits-table"')[1]
    assert _keys(client_viewer.get(TABLE, {"sort": "-date", **VISITS}).content.decode())[:3] == [
        "1724",
        "1727",
        "1726",
    ]
    assert _keys(client_viewer.get(TABLE, {"sort": "quality", **VISITS}).content.decode())[:2] == [
        "1723",
        REFERENCE_KEY,
    ]
    # a visit's quality is the mean of its records, beside its lowest record and its records
    row = dict(zip(URGENCY_ORDER, _rows(html), strict=True))["1722"]
    text = " ".join(re.sub(r"<[^>]+>", " ", row).split())
    assert "94.3%" in text and "93.0%" in text and 'title="mean of 3 records"' in row
    # every sort and page link keeps the view
    assert "view=visits" in re.search(r'<th scope="col" [^>]*>\s*<a href="([^"]+)"', html).group(1)


def test_red_and_amber_rows_follow_the_configured_thresholds(built, client_viewer):
    rows = dict(zip(RECORD_ORDER, _rows(client_viewer.get(TABLE).content.decode()), strict=True))
    assert 'class="row--amber"' in rows[111] and 'class="row--amber"' in rows[112]
    assert not [k for k in RECORD_ORDER if k not in (111, 112) and "row--" in rows[k]]
    html = client_viewer.get(TABLE).content.decode()
    assert "Red rows = high urgency (≥ 70) · Amber rows = medium (40–69)" in html
    ScoreSetting.objects.filter(pk=1).update(urgency_red=55, urgency_amber=30)
    rows = dict(zip(RECORD_ORDER, _rows(client_viewer.get(TABLE).content.decode()), strict=True))
    assert 'class="row--red"' in rows[111] and 'class="row--amber"' in rows[131]
    assert "row--" not in rows[103] and "row--" not in rows[141]  # 19; no urgency
    assert (
        "Red rows = high urgency (≥ 55) · Amber rows = medium (30–54)"
        in client_viewer.get(TABLE).content.decode()
    )


def test_the_urgency_pill_explains_its_parts(built, client_viewer):
    rows = dict(zip(RECORD_ORDER, _rows(client_viewer.get(TABLE).content.decode()), strict=True))
    title = re.search(r'title="([^"]*)">58<', rows[111]).group(1)
    assert title == "quality gap 26 · recency 12.2 · red flags 20" and "pill--warning" in rows[111]
    assert "No urgency: the record has no score" in rows[141] and ">—<" in rows[141]
    visits = dict(zip(URGENCY_ORDER, _rows(client_viewer.get(TABLE, VISITS).content.decode()), strict=True))
    assert "No urgency: the visit has no score" in visits["1724"]


def test_pages_of_the_table(built, client_viewer, monkeypatch):
    monkeypatch.setattr(views, "PAGE_SIZE", 3)
    first = client_viewer.get(TABLE).content.decode()
    assert _records(first) == RECORD_ORDER[:3]
    assert _records(client_viewer.get(TABLE, {"page": 2}).content.decode()) == RECORD_ORDER[3:6]
    # page links fetch the table and put the page's own address in the history
    link = re.search(
        r'<a class="page-link" href="([^"]+page=2)" hx-get="([^"]+)"[^>]*hx-push-url="([^"]+)"', first
    )
    assert link.group(1).startswith("/fmm/?") and "tab=visits" in link.group(1)
    assert link.group(2).startswith("/fmm/visits/?") and "section=" in link.group(2)
    assert link.group(3) == link.group(1)
    grouped = client_viewer.get(TABLE, VISITS).content.decode()
    assert _keys(grouped) == URGENCY_ORDER[:3]
    link = re.search(r'<a class="page-link" href="([^"]+page=2)"', grouped).group(1)
    assert "view=visits" in link
    assert _keys(client_viewer.get(TABLE, {"page": 2, **VISITS}).content.decode()) == URGENCY_ORDER[3:6]


def test_the_table_inside_the_page_uses_the_key_figures_count(built, client_viewer):
    html = client_viewer.get(reverse("fmm:dashboard"), {"tab": "visits"}).content.decode()
    assert _records(html) == RECORD_ORDER and "12 records" in html
    # grouped by visit, the visits equal the Monitoring visits figure
    html = client_viewer.get(reverse("fmm:dashboard"), {"tab": "visits", **VISITS}).content.decode()
    assert _keys(html) == URGENCY_ORDER and "8 visits" in html
    assert '<input type="hidden" name="view" value="visits" form="fmm-filters">' in html


@pytest.mark.parametrize("params", [{}, VISITS])
def test_team_cells_are_never_exported_and_show_names_only(built, client_viewer, params):
    html = client_viewer.get(TABLE, params).content.decode()
    table = html.split('id="fmm-visits-table"', 1)[1].split("</table>", 1)[0]
    headers = re.findall(r"<th\b[^>]*>", table)
    team_header = [h for h in headers if 'data-export="no"' in h]
    assert len(team_header) == 1 and "d-none d-md-table-cell" in team_header[0]
    for row in _rows(html):
        cells = re.findall(r"<td\b[^>]*>", row)
        assert sum('data-export="no"' in c for c in cells) == 1, row
        assert len(cells) == len(re.findall(r"<th\b", table)), row  # a cell under every header
    assert MEMBER in html and LEAD in html  # names, shown to staff
    assert MEMBER_EMAIL not in html  # never an e-mail address


def _csv(client, **params) -> list[list[str]]:
    response = client.get(TABLE, {"export": "csv", "section": "", **params})
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    text = response.content.decode("utf-8-sig")
    for canary in (LEAD, MEMBER, MEMBER_EMAIL, "Classes held as planned", "Met Mrs Layla Saab"):
        assert canary not in text
    rows = list(csv.reader(io.StringIO(text)))
    for word in ("team", "lead", "narrative", "member"):
        assert all(word not in h.lower() for h in rows[0])
    return rows


def test_the_csv_follows_the_list_and_has_no_team_lead_or_narrative(built, client_viewer):
    # the records list: one row per record, with its visit's columns
    rows = _csv(client_viewer)
    assert tuple(rows[0]) == views.RECORD_CSV_HEADER
    assert [int(r[0]) for r in rows[1:]] == RECORD_ORDER
    one = dict(zip(rows[0], next(r for r in rows if r[0] == "103"), strict=True))
    assert (one["Key"], one["Entity type"], one["Entity"], one["Rating"]) == (
        "1722",
        "Partner",
        "Amel Association",
        "on_track",
    )
    assert (one["Quality score"], one["Flags"], one["Urgency"]) == ("93.0", "R1 R7", "19")
    # grouped by visit: one row per visit
    rows = _csv(client_viewer, view="visits")
    assert tuple(rows[0]) == views.CSV_HEADER
    assert rows[0] == [
        "Visit", "Key", "eTools activity id", "Reference", "Reference number", "Start date", "End date",
        "Status", "Status group", "Partner", "Programme documents", "CP outputs", "Place", "Governorate",
        "District", "Sections", "Field offices", "Rating", "HACT Q1", "Quality score", "Score band",
        "Lowest record score", "Records", "Records scored", "Flags", "Urgency", "Action points",
        "Open action points", "Overdue action points", "Review", "Reviewed on",
    ]  # fmt: skip
    assert [r[1] for r in rows[1:]] == URGENCY_ORDER
    one = dict(zip(rows[0], next(r for r in rows if r[1] == "1722"), strict=True))
    assert (one["Rating"], one["Quality score"], one["Flags"], one["Urgency"]) == (
        "off_track",
        "94.3",  # the mean of its records' (95, 95, 93)
        "R1 R7 R23",
        "19",  # its most urgent record's
    )
    assert (one["Lowest record score"], one["Records"], one["Records scored"]) == ("93.0", "3", "3")
    # a record filter keeps the records that match, and the visits with one of them
    assert len(_csv(client_viewer, rating="off_track")) == 3  # 1722's CP output, the reference visit's PD
    assert len(_csv(client_viewer, rating="off_track", view="visits")) == 3


# ------------------------------------------------------------------------------------------ visit page
def test_the_visit_page_shows_narratives_in_full_without_emails(built, client_viewer):
    response = client_viewer.get(reverse("fmm:visit", args=["1722"]))
    html = response.content.decode()
    assert response.status_code == 200 and "<html" in html
    expected = CANARY_TEXT.replace(MEMBER_EMAIL, "[email withheld]")
    assert expected.replace("&", "&amp;") in html or expected in html.replace("&#x27;", "'")
    assert MEMBER_EMAIL not in html
    assert "matched by PCA/PD number" in html
    assert "Shown to NeuroDB users only; never sent to the AI." in html
    text = " ".join(html.split())
    # the visit's quality: the mean of its records, the lowest and the most urgent
    assert "94.3% (High) · mean of 3 records · lowest 93.0% · most urgent 19" in re.sub(r"<[^>]+>", "", text)
    assert "Why urgency 19" in html
    # a card per record, each with its own score, deductions and rule results (R1 took 2 off the partner's)
    cards = dict(re.findall(r'<article class="fmm-record[^"]*" id="record-(\d+)"(.*?)</article>', html, re.S))
    assert set(cards) == {"101", "102", "103"}
    partner = " ".join(re.sub(r"<[^>]+>", " ", cards["103"]).split())
    assert "93.0% (High)" in partner and "100 less Q3 quality 5, Completeness 2" in partner
    assert "R1: Incomplete monitoring report — missing: Q1 – Implementation status" in cards["103"]
    pd = " ".join(re.sub(r"<[^>]+>", " ", cards["101"]).split())
    assert "95.0% (High)" in pd and "R7" in pd and "R1: Incomplete" not in pd  # its own results only
    assert "/action-points/?module=fm&amp;visit=1722" in html or "module=fm&amp;visit=1722" in html
    assert "Switched off: R4, R9, R10" in html  # the rules switched off keep no result
    # R19 reads the whole visit: listed once, after the records, never on a card
    whole = html.split("Checks of the whole visit", 1)[1].split("</section>", 1)[0]
    assert whole.count('data-rule="R19"') == 1 and "applies to every record" in whole
    assert not any('data-rule="R19"' in card for card in cards.values())
    assert "Questions and answers" in html and "5 of 5 answered" in html
    assert "Open in eTools" not in html  # no address configured


def test_open_in_etools_appears_once_its_address_is_set(built, client_viewer, settings):
    settings.FMM_ETOOLS_ACTIVITY_URL = "https://etools.example.org/fm/activities/{id}/details"
    html = client_viewer.get(reverse("fmm:visit", args=["1722"])).content.decode()
    assert "https://etools.example.org/fm/activities/1722/details" in html
    other = client_viewer.get(reverse("fmm:visit", args=[REFERENCE_KEY])).content.decode()
    assert "Open in eTools" not in other  # no activity id


def test_the_visit_window_is_a_partial(built, client_viewer):
    response = client_viewer.get(reverse("fmm:visit", args=["1726"]), HTTP_HX_REQUEST="true")
    html = response.content.decode()
    assert response.status_code == 200 and "<html" not in html
    assert 'class="modal-header visit-modal"' in html and "Open as page" in html


def test_an_unknown_visit_is_404(built, client_viewer):
    assert client_viewer.get(reverse("fmm:visit", args=["9999"])).status_code == 404


def test_the_visit_page_lists_its_action_points_and_hact_context(built, client_viewer):
    html = client_viewer.get(reverse("fmm:visit", args=["1726"])).content.decode()
    assert "due 1 Sep 2026" in html and "linked to the activity in eTools" in html
    html = client_viewer.get(reverse("fmm:visit", args=["1722"])).content.decode()
    assert "1 of 2 programmatic visits completed in eTools; NeuroDB counts 2 FM programmatic visits" in html


# ------------------------------------------------------------------------------------------ look-up
@pytest.mark.parametrize(
    "text", ["1722", "#1722", "Visit 1722", "visit #1722", "FM-2026-022", "fm-2026-022", " FM-2026-022 "]
)
def test_the_look_up_finds_a_visit(built, client_viewer, text):
    response = client_viewer.get(reverse("fmm:lookup"), {"id": text})
    assert response.status_code == 302 and response["Location"] == "/fmm/visits/1722/"
    response = client_viewer.get(reverse("fmm:lookup"), {"id": text}, HTTP_HX_REQUEST="true")
    assert response["HX-Redirect"] == "/fmm/visits/1722/"


def test_the_look_up_finds_keys_and_reference_numbers(built, client_viewer):
    found = client_viewer.get(reverse("fmm:lookup"), {"id": REFERENCE_KEY})
    assert found["Location"] == f"/fmm/visits/{REFERENCE_KEY}/"
    Visit.objects.filter(key="1723").update(reference_number="REF-NUMBER-7")
    assert client_viewer.get(reverse("fmm:lookup"), {"id": "ref-number-7"})["Location"] == "/fmm/visits/1723/"


def test_a_visit_not_found_offers_the_nearest_ids(built, client_viewer):
    response = client_viewer.get(reverse("fmm:lookup"), {"id": "1799", "section": ""})
    html = response.content.decode()
    assert response.status_code == 200
    assert "No visit 1799 in NeuroDB — eTools field monitoring syncs nightly." in html
    assert re.findall(r">(Visit \d+)</a>", html) == ["Visit 1728", "Visit 1727", "Visit 1726"]
    # only the ids of the filter
    html = client_viewer.get(
        reverse("fmm:lookup"), {"id": "#1799", "governorate": "north", "section": ""}
    ).content.decode()
    assert re.findall(r">(Visit \d+)</a>", html) == ["Visit 1726", "Visit 1724"]


# ------------------------------------------------------------------------------------------ reviews
def _post(client, key="1726", **data):
    return client.post(reverse("fmm:review", args=[key]), {"status": "reviewed", "note": "Checked.", **data})


def test_an_administrator_can_review_any_visit(built, client, admin_user):
    client.force_login(admin_user)
    response = _post(client, "1722")
    assert response.status_code == 302 and response["Location"] == "/fmm/visits/1722/"
    review = VisitReview.objects.get()
    assert (review.visit_key, review.status, review.note, review.reviewed_by) == (
        "1722",
        "reviewed",
        "Checked.",
        admin_user,
    )


def test_a_section_editor_reviews_the_visits_of_their_section_only(built, client, fm_world):
    editor = _user("edu", SECTION_EDITOR, fm_world.section)
    client.force_login(editor)
    assert _post(client, "1726").status_code == 302  # an Education visit
    other = _user("cp", SECTION_EDITOR, Section.objects.create(name="Child Protection", code="CP"))
    client.force_login(other)
    assert _post(client, "1726").status_code == 403
    assert VisitReview.objects.count() == 1
    html = client.get(reverse("fmm:visit", args=["1726"])).content.decode()
    assert "Reviewed" in html and 'name="status"' not in html  # no form for them


def test_a_viewer_cannot_review(built, client_viewer):
    assert _post(client_viewer, "1726").status_code == 403
    html = client_viewer.get(reverse("fmm:visit", args=["1726"])).content.decode()
    assert "Not reviewed yet." in html and "Save review" not in html


def test_a_review_through_htmx_returns_the_review_block(built, client, admin_user):
    client.force_login(admin_user)
    response = client.post(
        reverse("fmm:review", args=["1722"]),
        {"status": "follow_up", "note": "Call the partner."},
        HTTP_HX_REQUEST="true",
    )
    html = response.content.decode()
    assert response.status_code == 200 and 'id="fmm-review"' in html and "Review saved." in html
    assert "Needs follow-up" in html and "Call the partner." in html
    # htmx swaps no 4xx answer: the form comes back with its error so the user sees it
    bad = client.post(reverse("fmm:review", args=["1722"]), {"status": "nope"}, HTTP_HX_REQUEST="true")
    assert (
        bad.status_code == 200 and "Choose reviewed, needs follow-up or data issue." in bad.content.decode()
    )
    assert 'role="alert"' in bad.content.decode() and "Review saved." not in bad.content.decode()
    long = client.post(reverse("fmm:review", args=["1722"]), {"status": "reviewed", "note": "x" * 501})
    assert long.status_code == 400 and VisitReview.objects.count() == 1


def test_a_review_survives_a_refresh_and_shows_in_the_table(built, client, admin_user):
    client.force_login(admin_user)
    _post(client, "1722", status="data_issue")
    refresh.run(triggered_by="test", today=TODAY)
    assert VisitReview.objects.get().visit_key == "1722"
    rows = dict(zip(URGENCY_ORDER, _rows(client.get(TABLE, VISITS).content.decode()), strict=True))
    assert 'data-status="data_issue"' in rows["1722"] and "Data issue" in rows["1722"]
    records = dict(zip(RECORD_ORDER, _rows(client.get(TABLE).content.decode()), strict=True))
    assert all("Data issue" in records[pk] for pk in (101, 102, 103))  # the review is the visit's
    text = client.get(TABLE, {"export": "csv"}).content.decode("utf-8-sig")
    assert text.count("data_issue") == 3


# ------------------------------------------------------------------------------------------ action points
def test_the_action_points_page_lists_the_visits_action_points(built, client_viewer, fm_world):
    linked = set(VisitActionPoint.objects.filter(visit__key="1722").values_list("action_point_id", flat=True))
    assert linked == {fm_world.action_points.by_id.pk}
    response = client_viewer.get(reverse("reports:action_points"), {"module": "fm", "visit": "1722"})
    html = response.content.decode()
    assert response.status_code == 200
    assert "From Visit 1722" in html
    assert re.search(r'class="kpi__label">Action points</div>\s*<div class="kpi__value">1<', html)
    assert '<input type="hidden" name="visit" value="1722">' in html  # kept by the filter bar
    empty = client_viewer.get(reverse("reports:action_points"), {"visit": "no-such-visit"}).content.decode()
    assert re.search(r'class="kpi__label">Action points</div>\s*<div class="kpi__value">0<', empty)


def test_the_action_points_search_finds_module_references_and_activity_ids(built, client_viewer, fm_world):
    from neurodb.datamart import services

    by_reference = services.action_points({"q": "FM-2026-023"})["points"]
    assert list(by_reference) == [fm_world.action_points.by_reference]
    by_id = services.action_points({"q": "1726"})["points"]
    assert fm_world.action_points.overdue in list(by_id)
    assert services.action_points({})["visit"] is None


def test_the_latest_of_two_reviews_saved_together_is_shown(built, client, admin_user):
    client.force_login(admin_user)
    VisitReview.objects.create(visit_key="1722", status="reviewed", reviewed_by=admin_user)
    later = VisitReview.objects.create(visit_key="1722", status="data_issue", reviewed_by=admin_user)
    VisitReview.objects.filter(visit_key="1722").update(created_at=later.created_at)  # the same instant
    html = client.get(reverse("fmm:visit", args=["1722"])).content.decode()
    block = html.split('id="fmm-review"', 1)[1]
    assert block.index("Data issue") < block.index("Earlier reviews")


def test_the_team_cell_counts_every_member_beyond_the_first_two(built, client_viewer):
    Visit.objects.filter(key="1722").update(team=["A Name", "B Name", "C Name"], team_unnamed=2)
    rows = dict(zip(RECORD_ORDER, _rows(client_viewer.get(TABLE).content.decode()), strict=True))
    cell = re.search(r'<td class="small d-none d-md-table-cell" data-export="no">(.*?)</td>', rows[103], re.S)
    text = " ".join(re.sub(r"<[^>]+>", " ", cell.group(1)).split())
    assert text == "A Name, B Name +3"  # one more name and two members known by e-mail only


# ------------------------------------------------------------------------------------------ drill-downs
DRILL = reverse("fmm:drill")


def _drill_counts(client, url: str) -> tuple[int, int]:
    """(records, visits) of a drill-down window: its records (one row each) and the visits they belong to."""
    response = client.get(url, HTTP_HX_REQUEST="true")
    assert response.status_code == 200, url
    html = response.content.decode()
    found = re.search(r'id="modal-title">(\d+) records? <span[^>]*>· (\d+) visits?</span>', html)
    total, visits = int(found.group(1)), int(found.group(2))
    rows = re.findall(r'<tr data-key="([^"]+)" data-record=', html)
    assert len(rows) == min(total, views.DRILL_ROWS)
    if total <= views.DRILL_ROWS:
        assert len(set(rows)) == visits
    return total, visits


def _drill_total(client, url: str) -> int:
    return _drill_counts(client, url)[0]


@pytest.mark.parametrize(
    "params",
    [
        {"month": "2026-05"},
        {"bucket": "80-100"},
        {"bucket": "none"},
        {"hact_q1": "constrained"},
        {"flag": "R1"},
        {"flags": "3+"},
        {"issue": "R1:missing:narrative"},
        {"rating": "not_monitored", "status": "reported"},
        {"office": "none"},
        {"location": "30"},
    ],
)
def test_the_drill_view_reads_codes(built, client_viewer, params):
    assert _drill_total(client_viewer, f"{DRILL}?section=&{urlencode(params)}") >= 0


@pytest.mark.parametrize(
    "params",
    [
        {"bucket": "80–100"},
        {"month": "May 2026"},
        {"hact_q1": "On track"},
        {"flag": "R1 Completeness"},
        {"rating": "On track"},
        {"status": "Reported"},
        {"flags": "3 or more"},
        {"issue": "R1: Incomplete monitoring report"},
    ],
)
def test_the_drill_view_refuses_a_display_label(built, client_viewer, params):
    response = client_viewer.get(DRILL, {"section": "", **params}, HTTP_HX_REQUEST="true")
    assert response.status_code == 400


def test_a_refused_drill_value_is_echoed_as_plain_text(built, client_viewer):
    response = client_viewer.get(DRILL, {"month": "<script>x</script>"}, HTTP_HX_REQUEST="true")
    assert response.status_code == 400 and response["Content-Type"].startswith("text/plain")


def test_a_drill_without_htmx_opens_the_records_list(built, client_viewer):
    response = client_viewer.get(DRILL, {"section": "", "flag": "R1"})
    assert response.status_code == 302
    assert response["Location"] == "/fmm/?section=&flag=R1&tab=visits"


def test_the_drill_window_lists_the_records_and_links_the_records_list(built, client_viewer):
    html = client_viewer.get(
        DRILL, {"section": "", "hact_q1": "constrained"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    assert "<html" not in html and _drill_counts(client_viewer, f"{DRILL}?section=&hact_q1=constrained") == (
        2,
        2,
    )
    assert "HACT Q1: Constrained" in html
    # most urgent first: each record with its own entity and type (FMS's columns)
    assert re.findall(r'<tr data-key="([^"]+)" data-record="(\d+)"', html) == [
        ("1723", "111"),
        ("1727", "131"),
    ]
    assert "LEB/SSFA2024001" in html and "PD/SSFA" in html and 'href="/fmm/visits/1723/#record-111"' in html
    assert 'href="/fmm/?section=&amp;hact_q1=constrained&amp;tab=visits"' in html
    assert "Open in the records list" in html
    # a figure of visits opens their records: 1722 holds three
    assert _drill_counts(client_viewer, f"{DRILL}?section=&visit_status=completed&location=30") == (3, 1)


def test_each_drill_type_lists_what_its_block_counts(built, client_viewer):
    """Every link and chart cell of the Quality and Analysis tabs opens exactly the records its block
    counted (a block of visits: the records of exactly those visits)."""
    from django.core.cache import cache

    from neurodb.fmm import metrics
    from neurodb.fmm.models import RuleSetting
    from neurodb.fmm.scope import Scope

    cache.clear()
    scope = Scope.from_params({"section": ""})
    rules = list(RuleSetting.objects.order_by("code"))
    checks: list[tuple[str, int]] = []
    volume = metrics.monthly_volume(scope)
    for month, n in zip(volume["drill"]["labels"], volume["indicators"][0]["values"], strict=True):
        checks.append((f"month={month}", n))
    q1 = metrics.hact_q1_by_month(scope)
    for name, code in q1["drill"]["series"].items():
        for month, n in zip(q1["drill"]["labels"], q1["series"][name], strict=True):
            if n:
                checks.append((f"month={month}&hact_q1={code}", n))
    for item in metrics.score_buckets(scope)["items"]:
        checks.append((f"bucket={item['drill']}", item["value"]))
    checks.append(("bucket=none", metrics.score_buckets(scope)["not_scored"]))
    for row in metrics.top_issues(scope, 10):
        checks.append((f"issue={row['drill']}", row["records"]))
    for row in metrics.rule_analysis(scope, rules):
        checks.append((f"flag={row['code']}", row["flagged"]))
    for row in metrics.flag_distribution(scope)["rows"]:
        checks.append((f"flags={row['drill']}".replace("+", "%2B"), row["n"]))
    summary = metrics.issues_summary(scope)
    checks.append((f"flags={summary['high_flag']['at']}%2B", summary["high_flag"]["n"]))
    data = metrics.summary(scope)
    checks.append(("hact_q1=not_monitored", data["q1_not_monitored"]))
    for row in metrics.locations(scope)["rows"]:
        if row["drill"]:
            checks.append((f"location={row['drill']}", row["records"]))
    offices = metrics.offices(scope)
    for row in offices["rows"] + [offices["unknown"]]:
        checks.append((f"office={row['name']}", row["records"]))
    for row in metrics.sections(scope):
        checks.append((f"section={row['name']}", row["records"]))
    for row in metrics.quality_by_rating(scope):
        checks.append((f"rating={row['code']}", row["records"]))
    assert len(checks) > 40
    for query, expected in checks:
        url = f"{DRILL}?{query}" if query.startswith("section=") else f"{DRILL}?section=&{query}"
        assert _drill_total(client_viewer, url) == expected, query
    # the blocks of visits: the gaps (reported visits none of whose records is rated) and the places
    visit_checks = [("visit_rating=not_monitored", summary["gaps"]["n"])]
    visit_checks += [
        (f"location={r['drill']}", r["visits"]) for r in metrics.locations(scope)["rows"] if r["drill"]
    ]
    visit_checks += [(f"office={r['name']}", r["visits"]) for r in offices["rows"] + [offices["unknown"]]]
    for query, expected in visit_checks:
        assert _drill_counts(client_viewer, f"{DRILL}?section=&{query}")[1] == expected, query


def test_the_links_of_the_tabs_open_their_block_counts(built, client_viewer):
    """The drill links the Quality and Analysis tabs draw carry the figure they show."""
    for tab in ("quality", "analysis"):
        html = client_viewer.get(reverse("fmm:dashboard"), {"tab": tab}).content.decode()
        links = re.findall(
            r'<a href="(/fmm/drill/\?[^"]+)" hx-get="[^"]+" hx-target="#modal-content">(\d+)</a>', html
        )
        assert links, tab
        for url, shown in links:
            assert _drill_total(client_viewer, url.replace("&amp;", "&")) == int(shown), url


def _chip_links(html: str) -> list[tuple[str, str, int]]:
    """The drill links whose text is a label and a figure ("On track 1", a highlight card)."""
    out = []
    for url, inner in re.findall(r'<a [^>]*href="(/fmm/drill/\?[^"]+)"[^>]*>(.*?)</a>', html, re.S):
        text = " ".join(re.sub(r"<[^>]+>", " ", inner).split())
        found = re.match(r"^(\d+) (\D+)$", text) or re.match(r"^(\D+?) (\d+)$", text)
        if found and not text.endswith(("flag", "flags")):  # "1 flag" names its row, the count is beside it
            label, n = (found.group(2), found.group(1)) if text[0].isdigit() else found.groups()
            out.append((url.replace("&amp;", "&"), label, int(n)))
    return out


# the cards that count visits (their window lists the records of exactly those visits)
VISIT_CARDS = ("Reported", "Not monitored (planned, not conducted)")


def test_the_chip_rows_and_cards_open_the_records_they_count(built, client_viewer):
    """The HACT Q1 chip row, the issue summary cards and the highlight cards carry their own figure:
    their drill-down window lists exactly that many records (a card of visits: that many visits)."""
    links = []
    for tab in ("quality", "analysis"):
        html = client_viewer.get(reverse("fmm:dashboard"), {"tab": tab}).content.decode()
        links += _chip_links(html)
    labels = [label for _url, label, _n in links]
    assert {"On track", "Constrained", "Reported", "Records", "Off-track records"} <= set(labels), labels
    assert labels.count("Off track") == 1  # the HACT Q1 chip
    assert any("Not monitored (planned, not conducted)" in label for label in labels)
    assert "Not Monitored" in labels  # the HACT Q1 chart's own chip
    for url, label, shown in links:
        records, visits = _drill_counts(client_viewer, url)
        assert (visits if label.startswith(VISIT_CARDS) else records) == shown, (label, url)


def test_the_overall_rating_chart_opens_the_records_it_counts(built, client_viewer):
    """Without Q1 answers the chart counts the records' overall rating: each cell and each chip of its
    row opens the records it counts (rating=<code>, never a label)."""
    from django.core.cache import cache

    from neurodb.fmm import metrics
    from neurodb.fmm.scope import Scope

    Visit.objects.update(hact_q1="")
    VisitEntity.objects.update(hact_q1="")
    cache.clear()
    scope = Scope.from_params({"section": ""})
    assert metrics.hact_q1_by_month(scope) is None
    chart = metrics.rating_by_month(scope)
    cells = 0
    for name, code in chart["drill"]["series"].items():
        for month, n in zip(chart["drill"]["labels"], chart["series"][name], strict=True):
            if n:
                cells += 1
                assert _drill_total(client_viewer, f"{DRILL}?section=&month={month}&rating={code}") == n
    assert cells >= 4
    html = client_viewer.get(reverse("fmm:dashboard"), {"tab": "quality"}).content.decode()
    # the chart's chips (not the quality issues summary's Not monitored card)
    chips = [(url, n) for url, label, n in _chip_links(html) if "rating=" in url and "planned" not in label]
    assert len(chips) == len([t for t in chart["totals"] if t["n"]]) >= 3
    for url, shown in chips:
        assert _drill_total(client_viewer, url) == shown, url


def test_the_drill_chips_say_what_none_means(built, client_viewer):
    html = client_viewer.get(
        DRILL, {"section": "", "bucket": "none", "hact_q1": "none", "review": "none"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    chips = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    assert "Quality score: not scored" in chips and "HACT Q1: not answered" in chips
    assert "Review: not reviewed" in chips and ": none" not in chips
