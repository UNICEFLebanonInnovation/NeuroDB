"""Stage F3a check (records): what the independent check fixed.

- The AI brief's breakdowns (partners, modalities, governorates) count records, as the page does: a
  governorate's or a partner's average quality in the facts is the one the page shows for the same filter
  (the mean of its records, not of its visits' means), and every breakdown entry says how many records
  and visits it holds; the facts say once what each figure counts.
- The programme document page lists the FM visits with a record of the document, the visits its panel
  counts and its link opens (the page's ``pd`` filter reads each record's document).
- A link to a record opens its visit's window at the record's card, as the page itself would.

On ``fm_world`` built on 5 October 2026 (conftest ``built``): 8 visits holding 12 records.
"""

from __future__ import annotations

import datetime
import re
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.http import QueryDict
from django.test import override_settings
from django.urls import reverse

from neurodb.fmm import metrics, services
from neurodb.fmm import scope as scope_module
from neurodb.fmm.ai import facts, profiles
from neurodb.fmm.models import Visit, VisitEntity
from neurodb.fmm.scope import Scope

pytestmark = pytest.mark.django_db
TODAY = datetime.date(2026, 10, 5)
PAGE = reverse("fmm:dashboard")
AI_ON = {"FMM_AI": True, "AI_ASSISTANT_ENABLED": True, "OPENAI_API_KEY": "x"}


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr(scope_module, "_today", lambda today=None: today or TODAY)
    cache.clear()


@pytest.fixture
def ai_on():
    from neurodb.watch import people

    people.forget()
    with override_settings(**AI_ON):
        yield profiles.published()


def _scope(query: str = "year=2026&section=") -> Scope:
    return Scope.from_params(QueryDict(query), None, TODAY)


def _num(value) -> float | None:
    return None if value is None else float(value)


# ------------------------------------------------------------------------------------------ the AI facts
@pytest.mark.parametrize("query", ("year=2026&section=", "year=2026&section=&entity_type=pd"))
def test_the_facts_break_the_records_down_as_the_page_does(built, ai_on, query):
    scope = _scope(query)
    payload = facts.build(scope, ai_on, TODAY).payload
    # partners: each record under its own partner, as the exports' and the report's partner table count it
    expected = {
        f"partner:{row['key']}": row for row in metrics.breakdown(scope, "partner") if row["key"] != "none"
    }
    assert set(payload["partners"]) == set(expected)
    for key, entry in payload["partners"].items():
        row = expected[key]
        assert (entry["records"], entry["visits"]) == (row["records"], row["visits"]), key
        assert entry["avg_quality"] == _num(row["avg"]), key
        for code in ("on_track", "constrained", "off_track", "not_monitored"):
            assert entry[code] == row["ratings"][code], (key, code)
        assert entry["rated"] == row["rated"], key
    # governorates: the average of the governorate filter's records, the page's figure
    for key, entry in payload["places"].items():
        if not key.startswith("gov:"):
            continue
        governorate = _scope(f"{query}&governorate={key.split(':', 1)[1]}")
        kpis = metrics.kpis(governorate)
        assert (entry["records"], entry["visits"]) == (kpis["records"], kpis["visits"]), key
        assert entry["avg_quality"] == _num(kpis["avg_quality"]), key
    # the modalities hold every record of the filter once
    modalities = payload["modalities"].values()
    assert sum(m["records"] for m in modalities) == metrics.kpis(scope)["records"]
    # sections and offices give their records next to their visits
    offices = metrics.offices(scope)
    for name, rows in (
        ("sections", metrics.sections(scope)),
        ("offices", [*offices["rows"], *([offices["unknown"]] if offices["unknown"] else [])]),
    ):
        assert sorted((e["records"], e["visits"]) for e in payload[name].values()) == sorted(
            (r["records"], r["visits"]) for r in rows
        ), name
    assert facts.RECORDS_NOTE in payload["notes"]


def test_the_facts_rules_and_issues_count_records_as_the_page_does(built, ai_on):
    scope = _scope()
    payload = facts.build(scope, ai_on, TODAY).payload
    # each recurring issue gives its records (the page's Records column) and their visits
    issues = {f"issue:{row['drill']}": row for row in metrics.top_issues(scope, 50)}
    assert payload["issues"]
    for key, entry in payload["issues"].items():
        assert (entry["records"], entry["visits"]) == (issues[key]["records"], issues[key]["visits"]), key
    # a rule's flagged and checked counts are the rule analysis's (records)
    analysis = {row["code"]: row for row in metrics.rule_analysis(scope)}
    assert payload["rules"]
    for key, entry in payload["rules"].items():
        row = analysis[key.split(":", 1)[1]]
        assert (entry["flagged"], entry["evaluated"]) == (row["flagged"], row["evaluated"]), key


def test_a_governorate_s_average_in_the_facts_is_the_mean_of_its_records(built, ai_on):
    payload = facts.build(_scope(), ai_on, TODAY).payload
    bekaa = payload["places"]["gov:beqaa"]
    records = VisitEntity.objects.filter(visit__governorate_key="beqaa", visit__visit_date__year=2026)
    scores = [s for s in records.values_list("quality_score", flat=True) if s is not None]
    # 1722's 95, 95 and 93, 1723's 48 and 48, 1727's 80, 1728's 93: 78.9, not the visits' means' 78.8
    assert len(scores) == 7 and bekaa["avg_quality"] == 78.9 == _num(metrics.mean_quality(sum(scores), 7))
    # its 8 records of 5 visits (1725's cancelled record has no score)
    assert (bekaa["records"], bekaa["visits"]) == (records.count(), 5) == (8, 5)
    # the morning briefing's governorate chip shows the same figure
    chips = {g["key"]: g for g in metrics.briefing(_scope())["governorates"]}
    assert _num(chips["beqaa"]["avg_quality"]) == bekaa["avg_quality"]


# ------------------------------------------------------------------------------------------ the PD page
def test_the_pd_page_lists_the_visits_with_a_record_of_the_document(built, fm_world):
    education = fm_world.pds["education"]
    with_record = set(_scope(f"year=2026&section=&pd={education.pk}").visits().values_list("key", flat=True))
    # a visit that names the document only in its other columns (a PD reference of the activity), with no
    # record of it: the panel and the page's pd filter do not count it, so the list does not show it
    other = Visit.objects.exclude(key__in=with_record).filter(visit_date__year=2026).first()
    Visit.objects.filter(pk=other.pk).update(pd_ids=[*other.pd_ids, education.pk])
    panel = services.pd_summary(education.pk, 2026)
    block = panel["context"][0]
    assert {line["key"] for line in block["visits"]} == with_record
    assert block["visits_count"] == panel["visits"] == len(with_record)
    # the visit page's block of the same document: the other visits with a record of it, near the date
    one = Visit.objects.get(key=sorted(with_record)[0])
    near = services.pd_context([education.pk], around=one.visit_date, exclude_key=one.key)[0]
    assert {line["key"] for line in near["visits"]} <= with_record - {one.key}


# ------------------------------------------------------------------------------------------ record links
def test_a_record_link_opens_its_visit_window_at_the_record(built, client_viewer):
    # the records list, the drill window and the critical cards ask for the visit with the record's anchor
    # (htmx hands it to app.js, which scrolls the window to the card and marks it)
    lists = [
        client_viewer.get(PAGE, {"year": "2026", "section": "", "tab": "visits"}, HTTP_HX_REQUEST="true"),
        client_viewer.get(reverse("fmm:drill"), {"year": "2026", "section": ""}, HTTP_HX_REQUEST="true"),
    ]
    for response in lists:
        html = response.content.decode()
        links = re.findall(r'<a href="(/fmm/visits/[\w-]+/#record-\d+)" hx-get="([^"]+)"', html)
        assert links and all(href == get for href, get in links)
    critical = metrics.critical_items(_scope("year=2026&section="))
    assert critical["items"]  # the amber records of 1723
    html = client_viewer.get(PAGE, {"year": "2026", "section": ""}).content.decode()
    card = html.split('id="fmm-critical"', 1)[1]
    found = re.findall(r'href="(/fmm/visits/[\w-]+/#record-\d+)" hx-get="([^"]+)"', card)
    assert found and all(href == get for href, get in found)
    # the window's "Open as page" link takes the anchor along, and the record's own line stays small
    window = client_viewer.get(reverse("fmm:visit", args=["1722"]), HTTP_HX_REQUEST="true").content.decode()
    assert re.search(r'<a [^>]*href="/fmm/visits/1722/" data-page-link>', window)
    assert 'class="fmm-record__sub"' in window and '<span class="cell-sub">matched' not in window


def test_app_js_reveals_the_anchor_of_a_window():
    from pathlib import Path

    from django.conf import settings

    source = (Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "js" / "app.js").read_text()
    assert "revealAnchor(modal, e.detail.pathInfo?.anchor)" in source
    css = (Path(settings.BASE_DIR) / "neurodb" / "web" / "static" / "css" / "app.css").read_text()
    assert ".fmm-record.is-target" in css


def test_the_record_average_is_not_the_visits_average_here(built):
    """The difference the facts fix is real in fm_world: Bekaa's records average 78.9, its visits'
    means 78.8 (the guard for the test above)."""
    visits = Visit.objects.filter(governorate_key="beqaa", visit_date__year=2026).exclude(quality_score=None)
    means = list(visits.values_list("quality_score", flat=True))
    assert metrics.mean_quality(sum(means), len(means)) == Decimal("78.8")
