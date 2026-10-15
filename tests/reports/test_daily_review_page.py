"""The daily review's page (``/daily-review/``, SPEC_HOME §3.3): its frame (scope line, purpose with the time the
schedule gives), the review card (part ``review``) and its folded findings (``review-more``), which moved here from
the country overview, and Home's digest of it (``review.services.digest``).

On ``overview_fixture``: one programme document (Amel, Child Protection) with 500 children reached in 2026, plus the
ActivityInfo hierarchy; a daily review is run on it where a test needs the card.
"""

from __future__ import annotations

import dataclasses
import datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from django.utils import timezone
from django.utils.html import escape

from neurodb.core.models import ScheduledJob
from neurodb.review import checks
from neurodb.review import services as review_services
from neurodb.review.models import DailyReview, ReviewFinding
from tests.reports.overview_fixture import TODAY, make_overview_data
from tests.web.parts import HX, get_part, placeholder_attributes, placeholders, raw_get, text

pytestmark = pytest.mark.django_db

PAGE = "/daily-review/"  # reports:daily_review
# Query ceilings (SPEC_HOME §3.3): measured on this world, plus 2. The frame warm is 11: the request's floor
# (session, user, donor check), the filter sections' stamp (last sync run, flags), the role check and section of
# the first-load default, the daily review's schedule, whether a review exists, and the navigation's year and role;
# cold (16) it also reads the filter sections (1) and the navigation (4). Each part reads the review (6).
SHELL_QUERIES_WARM = 13
SHELL_QUERIES_COLD = 18
PART_QUERIES = {"review": 8, "review-more": 8}


@pytest.fixture
def world(hierarchy, reporting_year, settings):
    settings.AI_ASSISTANT_ENABLED = False
    return make_overview_data()


@pytest.fixture
def reviewed(world, monkeypatch):
    """The world with a daily review. The card shows one finding and folds the others (``review-more``), as the
    real card does past its first findings."""
    review_services.run(date=TODAY, today=TODAY)
    monkeypatch.setattr(review_services, "PAGE_FINDINGS", 1)
    return world


@pytest.fixture
def own_section(viewer, section):
    viewer.section = section
    viewer.save()
    return viewer


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


# ------------------------------------------------------------------------------------------------- the frame
@pytest.mark.shell
def test_the_frame_says_when_the_review_runs(client_viewer, reviewed):
    job = ScheduledJob.objects.get(command="daily_review")
    job.schedule, job.next_run_at = "40 5 * * *", None
    job.save()
    html = text(client_viewer.get(PAGE))
    assert "<title>Daily review" in html and '<h1 class="page-title">Daily review</h1>' in html
    assert "Fourteen checks run every morning at 05:40 on the night's data." in html
    assert "Findings are prompts to look, never verdicts on partners or staff." in html
    assert 'id="daily-review"' in html and list(placeholders(html)) == ["review"]
    assert 'href="/">Home</a>' in html  # Home › Daily review
    job.enabled = False
    job.save()
    html = text(client_viewer.get(PAGE))
    assert "it is switched off in Scheduled jobs." in html


@pytest.mark.shell
def test_without_a_review_the_page_says_it_has_not_run(client_viewer, world):
    html = text(client_viewer.get(PAGE))
    assert placeholders(html) == {}  # no room kept for a card that cannot come
    assert "The daily review has not run yet." in html and "It runs every morning at 06:00." in html


@pytest.mark.shell
def test_a_first_load_takes_the_own_section_and_the_tabs_keep_it(client_viewer, own_section, reviewed):
    first = text(client_viewer.get(PAGE))
    assert "Your section, Child Protection, by default" in first
    assert _query(placeholders(first)["review"]) == {"section": ["Child Protection"], "_part": ["review"]}
    card = text(get_part(client_viewer, PAGE, "review"))
    assert "?section=Child+Protection&amp;review=week#daily-review" in card  # the tabs keep the section
    every = text(client_viewer.get(PAGE + "?section="))
    assert "Every section" in every and "only Child Protection" in every
    week = text(client_viewer.get(PAGE + "?section=&review=week"))
    assert _query(placeholders(week)["review"]) == {"section": [""], "review": ["week"], "_part": ["review"]}


@pytest.mark.shell
def test_the_frame_stays_within_its_query_ceiling(
    client_viewer, own_section, reviewed, django_assert_max_num_queries
):
    with django_assert_max_num_queries(SHELL_QUERIES_COLD):
        assert client_viewer.get(PAGE).status_code == 200
    with django_assert_max_num_queries(SHELL_QUERIES_WARM):
        assert client_viewer.get(PAGE).status_code == 200


@pytest.mark.parametrize("name", ["review", "review-more"])
def test_each_part_stays_within_its_query_ceiling(
    client_viewer, reviewed, name, django_assert_max_num_queries
):
    with django_assert_max_num_queries(PART_QUERIES[name]):
        response = client_viewer.get(f"{PAGE}?section=&_part={name}", **HX)
    assert response.status_code == 200 and f'id="part-{name}"' in text(response)


def test_the_review_card_is_drawn(client_viewer, reviewed):
    html = client_viewer.get(PAGE + "?review=today").text
    assert 'id="daily-review"' in html and "How the daily review works" in html and "TPM/2" in html


def test_the_review_more_part_holds_only_the_folded_findings(client_viewer, reviewed):
    page = review_services.for_page("today", [])
    shown = escape(page["findings"][0].title)
    folded = [escape(f.title) for f in page["more_findings"]]
    assert len(page["findings"]) == 1 and folded and shown not in folded
    item = '<li class="review-finding">'
    card = text(get_part(client_viewer, PAGE, "review"))
    # the card lists its first finding only (its summary may name the others) ...
    assert card.count(item) == 1 and shown in card.split(item, 1)[1].split("</li>", 1)[0]
    assert f"Show {len(folded)} more finding" in card
    inner = placeholder_attributes(card)
    assert list(inner) == ["review-more"] and inner["review-more"]["data-part-lazy"] == "strict"
    assert _query(inner["review-more"]["hx-get"])["_part"] == ["review-more"]
    more = text(get_part(client_viewer, PAGE, "review-more"))
    # ... and the folded ones come in their own part, alone
    assert more.count(item) == len(folded) and all(title in more for title in folded)
    assert shown not in more and 'id="review-title"' not in more and "review-summary" not in more


@pytest.mark.parametrize("tab", ["", "yesterday", "week"])
def test_the_folded_findings_stay_those_of_their_card(client_viewer, reviewed, tab, monkeypatch):
    """The card and its folded findings are two requests, maybe hours apart (``review-more`` loads when
    *Show more* is opened, or for print): a review that succeeds in between never puts its findings under the
    card drawn before it, whatever the card's tab. A newer day's review (the next morning's job) leaves the
    card's findings as they were; a run of the card's own day (the admin's *Run the daily review now*, a
    second run of the morning's job) replaces its review, and the part then asks the reader to reload."""
    if tab == "yesterday":  # a review the day before, so that the tab has a card with folded findings
        review_services.run(date=TODAY - datetime.timedelta(days=1), today=TODAY - datetime.timedelta(days=1))
    page = review_services.for_page(tab or "today", [])
    folded = [escape(f.title) for f in page["more_findings"]]
    assert folded
    card = text(get_part(client_viewer, PAGE + (f"?section=&review={tab}" if tab else ""), "review"))
    assert f"Show {len(folded)} more finding" in card
    address = placeholder_attributes(card)["review-more"]["hx-get"]
    before = text(raw_get(client_viewer, address, **HX))
    assert before.count('<li class="review-finding">') == len(folded) and all(t in before for t in folded)
    # the next review succeeds while the card is open, with findings of its own
    newer = DailyReview.objects.create(
        date=TODAY + datetime.timedelta(days=1),
        status=DailyReview.Status.SUCCEEDED,
        finished_at=timezone.now(),
        checks_run=1,
    )
    for n in range(3):
        ReviewFinding.objects.create(
            review=newer,
            key=f"new_off_track:NEXT-{n}",
            check_id="new_off_track",
            severity=ReviewFinding.Severity.WARNING,
            title=f"Found by the next review, number {n}",
            rank=n,
        )
    assert review_services.for_page(tab or "today", [])["review"] != page["review"]  # the tab moved on
    assert text(raw_get(client_viewer, address, **HX)) == before  # still the card's own findings

    # the card's own day is run again (DailyReview.date is unique: the run replaces that day's review)
    run_all = checks.run_all

    def run_again(ctx):
        drafts, errors = run_all(ctx)
        return [dataclasses.replace(d, title=f"Run again: {d.title}") for d in drafts], errors

    monkeypatch.setattr(checks, "run_all", run_again)
    shown = page["review"]
    again = review_services.run(date=shown.date, today=shown.date, narrate=False)
    assert again.status == DailyReview.Status.SUCCEEDED and again.date == shown.date and again.pk != shown.pk
    after = text(raw_get(client_viewer, address, **HX))
    assert "Run again:" not in after and '<li class="review-finding">' not in after  # never the new run's
    assert "This review has been run again since the page opened. Reload the page" in after


# ------------------------------------------------------------------------------------------ Home's digest
def test_the_digest_groups_by_check_in_the_order_of_their_best_rank(reviewed):
    review = DailyReview.objects.get()
    for check_id, rank, severity in (
        ("zz_late", 1, "warning"),
        ("zz_late", 50, "critical"),
        ("aa_first", 2, "info"),
    ):
        ReviewFinding.objects.create(
            review=review, key=f"{check_id}:{rank}", check_id=check_id, severity=severity, section="",
            title=f"{check_id} {rank}", url="/x/", rank=rank,
        )  # fmt: skip
    ReviewFinding.objects.create(
        review=review, key="good:1", check_id="improvements", severity=ReviewFinding.Severity.GOOD, title="Good",
        rank=0,
    )  # fmt: skip
    ReviewFinding.objects.create(
        review=review, key="gone:1", check_id="gone", severity="critical", title="Gone",
        state=ReviewFinding.State.RESOLVED, rank=0,
    )  # fmt: skip
    found = review_services.digest([], limit=10)
    rows = [r for r in found["rows"] if r["check"] in {"zz_late", "aa_first"}]  # the run's own come first
    assert [r["check"] for r in rows] == ["zz_late", "aa_first"]  # by their best rank: 1, then 2
    assert rows[0]["title"] == "zz_late 1" and rows[0]["more"] == 1  # "(and 1 more like it)"
    assert "improvements" not in [r["check"] for r in rows] and "gone" not in [r["check"] for r in rows]
    labels = {severity: n for severity, _label, n in found["counts"]}
    assert "good" not in labels and labels["critical"] >= 2
    assert len(found["changed"]) <= 2
    # findings already shown as decisions are left out, the rest of their check stays
    without = review_services.digest([], limit=10, exclude_keys=["zz_late:1"])
    late = next(r for r in without["rows"] if r["check"] == "zz_late")
    assert late["title"] == "zz_late 50" and late["more"] == 0
    # sections narrow the findings; country-wide ones always count
    education = review_services.digest(["Education"], limit=10)
    assert education["rows"] and all(r["section"] in ("", "Education") for r in education["rows"])
    assert any(r["section"] == "Child Protection" for r in found["rows"])


def test_no_review_no_digest(world):
    assert review_services.digest([]) is None
