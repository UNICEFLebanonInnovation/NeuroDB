"""The country overview page: filters, own-section default, tiles, charts JSON and the money filter.

The overview service is replaced by the fakes of ``overview_fake.py`` so the page is tested on its own;
one test renders against the real service and is skipped while the service is a stub.
"""

import json

import pytest
from django.urls import reverse

from neurodb.reports import overview as overview_service
from neurodb.web.templatetags.ui import money

from .overview_fake import GOVERNORATES, empty_data, fake_data, fake_options
from .overview_fixture import make_overview_data

pytestmark = pytest.mark.django_db


@pytest.fixture
def fake_service(monkeypatch):
    """``build`` and ``options`` replaced by the fakes; the scopes passed to ``build`` are recorded."""
    calls: list = []

    def build(scope, **kwargs):
        calls.append(scope)
        return fake_data()

    monkeypatch.setattr(overview_service, "build", build)
    monkeypatch.setattr(overview_service, "options", lambda year: fake_options())
    return calls


def _get(client, url):
    response = client.get(url)
    assert response.status_code == 200, f"{url} -> {response.status_code}"
    return response


def test_renders_every_block_with_data(client_viewer, reporting_year, fake_service):
    response = _get(client_viewer, reverse("reports:overview"))
    html = response.text
    assert "Country overview" in html
    for text in (
        "Children reached",
        "Cost per child",
        "TPM visits",
        "Value for money",
        "Delivery and assurance",
    ):
        assert text in html, text
    for name in ("Akkar", "Bekaa", "Mount Lebanon"):
        assert f'<span class="gov-tile__name">{name}</span>' in html
    assert 'class="gov-tile gov-tile--l6"' in html and "9,800" in html  # reached formatted with intcomma
    assert "compare sections, not absolute values" in html
    assert "$7.2M" in html and "$12.4M" in html  # disbursed of reserved
    assert 'id="overview-chart-data"' in html
    assert "Data freshness" in html and "Daily AI review" in html
    assert "Databases" in html and "No databases for this year" in html  # the ActivityInfo cards, unchanged
    assert "LEB/PD2026001" in html and "Himaya" in html  # decisions and attention lists
    assert "reports/partials/_daily_review.html" not in html
    scope = fake_service[0]
    assert scope.year == 2026 and scope.reporting_year == reporting_year
    assert scope.sections == [] and scope.governorate == ""


def test_chart_data_has_the_shapes_the_builders_read(client_viewer, reporting_year, fake_service):
    html = _get(client_viewer, reverse("reports:overview")).text
    start = html.index('id="overview-chart-data"')
    payload = html[html.index(">", start) + 1 : html.index("</script>", start)]
    charts = json.loads(payload)
    assert {"impact", "money", "delivery", "progress", "status_counts", "labels"} <= set(charts)
    assert charts["impact"]["by_section"][0]["section"] == "Child Protection"
    assert charts["impact"]["monthly"]["labels"][0] == "Jan"
    assert set(charts["impact"]["monthly"]["series"]) == {"eTools", "ActivityInfo"}
    assert charts["money"]["by_section"] == [
        ["Child Protection", 185.7],
        ["Education", 327.4],
    ]  # WASH has None
    assert [p["name"] for p in charts["money"]["scatter"]] == ["Child Protection", "Education"]
    assert charts["money"]["scatter"][0]["ahead"] is True
    assert charts["money"]["by_donor"][0] == ["Germany", 4200000.0]
    assert charts["delivery"]["by_section"][0]["total"] == 12
    assert charts["delivery"]["findings_by_rating"][1] == ["Off Track", 9]
    assert set(charts["progress"]["tpm"]["series"]) == {"Planned", "Completed", "Overdue"}
    assert charts["progress"]["tpm"]["colors"]["Overdue"] == "--nd-danger"
    assert set(charts["progress"]["action_points"]["series"]) == {"Due", "Closed", "Past due"}
    assert charts["status_counts"]["off_track"] == 6
    assert charts["labels"]["not_reported"] == "Not reported"


def test_own_section_is_the_default_on_first_load(client_viewer, viewer, reporting_year, fake_service):
    from neurodb.accounts.models import Section

    viewer.section = Section.objects.create(name="Child protection", code="CP")
    viewer.save()
    page = _get(client_viewer, reverse("reports:overview"))
    assert "Your section, Child Protection, by default" in page.text
    assert page.context["own_section"] == ["Child Protection"]
    assert page.context["selected"]["section"] == ["Child Protection"]
    assert fake_service[-1].sections == ["Child Protection"]
    assert "Child Protection" in page.context["page_subtitle"]
    # every section: one click clears the default
    everything = _get(client_viewer, reverse("reports:overview") + "?section=")
    assert "by default" not in everything.text
    assert everything.context["own_section"] == [] and fake_service[-1].sections == []
    assert "every section" in everything.context["page_subtitle"]


def test_explicit_section_overrides_the_default(client_viewer, viewer, reporting_year, fake_service):
    from neurodb.accounts.models import Section

    viewer.section = Section.objects.create(name="Child protection", code="CP")
    viewer.save()
    page = _get(client_viewer, reverse("reports:overview") + "?section=Education&section=WASH")
    assert page.context["own_section"] == []
    assert page.context["selected"]["section"] == ["Education", "WASH"]
    assert fake_service[-1].sections == ["Education", "WASH"]
    assert 'value="Education" selected' in page.text and 'value="WASH" selected' in page.text


def test_governorate_filter_keeps_the_section_in_tile_links(client_viewer, reporting_year, fake_service):
    url = reverse("reports:overview") + "?section=Education&governorate=Akkar"
    page = _get(client_viewer, url)
    assert fake_service[-1].governorate == "Akkar" and fake_service[-1].sections == ["Education"]
    assert page.context["selected"]["governorate"] == "Akkar"
    assert "Akkar" in page.context["page_subtitle"]
    html = page.text
    assert 'href="?section=Education&amp;year=2026&amp;governorate=Bekaa"' in html
    assert 'governorate=Akkar" aria-current="true"' in html
    assert 'value="Akkar" selected' in html
    assert "every governorate" in html
    # drill-down links carry the section too
    assert (
        reverse("reports:pd_monitoring")
        + "?section=Education&amp;year=2026&amp;scope=year&amp;status=off_track"
        in html
    )
    assert all(name in GOVERNORATES for name in page.context["options"]["governorates"])


def test_empty_data_renders_the_empty_states(client_viewer, reporting_year, monkeypatch):
    monkeypatch.setattr(overview_service, "build", lambda scope, **kw: empty_data())
    monkeypatch.setattr(overview_service, "options", lambda year: {"sections": [], "governorates": []})
    html = _get(client_viewer, reverse("reports:overview")).text
    for text in (
        "No children reached by governorate yet",
        "No coverage to show",
        "No decisions pending",
        "Nothing flagged",
        "No sync has run yet",
        "No databases for this year",
        "No indicators for this scope yet",
    ):
        assert text in html, text
    assert 'data-empty-title="No monthly values yet"' in html
    assert "$0" in html  # disbursed of reserved
    assert "of $0 reserved" in html
    start = html.index('id="overview-chart-data"')
    charts = json.loads(html[html.index(">", start) + 1 : html.index("</script>", start)])
    assert charts["impact"]["monthly"] == {} and charts["progress"]["tpm"] == {}
    assert charts["money"]["by_section"] == [] and charts["money"]["scatter"] == []


def test_no_reporting_year_keeps_the_empty_state(client_viewer, monkeypatch):
    def build(scope, **kw):
        raise AssertionError("build must not be called without a reporting year")

    monkeypatch.setattr(overview_service, "build", build)
    monkeypatch.setattr(overview_service, "options", lambda year: fake_options())
    html = _get(client_viewer, reverse("reports:overview")).text
    assert "No reporting year is configured" in html
    assert 'id="overview-chart-data"' in html


def test_year_parameter_selects_the_reporting_year(client_viewer, reporting_year, fake_service):
    from neurodb.indicators.models import ReportingYear

    previous = ReportingYear.objects.create(name="2025", year="2025", current=False)
    page = _get(client_viewer, reverse("reports:overview") + "?year=2025")
    assert fake_service[-1].year == 2025 and fake_service[-1].reporting_year == previous
    assert page.context["selected"]["year"] == "2025"
    assert 'value="2025" selected' in page.text


def test_money_filter():
    assert money(1_234_567) == "$1.2M"
    assert money(820_000) == "$820k"
    assert money(950) == "$950"
    assert money(None) == "—"
    assert money("") == "—"
    assert money(0) == "$0"
    assert money(1_000_000) == "$1M"
    assert money(12_345) == "$12.3k"
    assert money(150_000_000) == "$150M"
    assert money(-2_500) == "-$2.5k"
    assert money("abc") == "abc"


def test_renders_against_the_real_service(client_viewer, reporting_year, hierarchy):
    make_overview_data()
    html = _get(client_viewer, reverse("reports:overview")).text
    for text in (
        "Children reached",
        "Cost per child",
        "TPM visits",
        "Child Protection",
        'id="overview-chart-data"',
    ):
        assert text in html, text
