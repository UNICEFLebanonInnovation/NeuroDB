"""Population tabs, library filters and data health wording (defects found by the page checker)."""

import pytest
from django.urls import reverse

from neurodb.core.models import PopulationFigure, SyncRun
from neurodb.library.models import Resource, ResourceTag, ResourceTopic, ResourceType

pytestmark = pytest.mark.django_db


def _figure(**kwargs):
    fields = dict(year=2026, nationality="LEB", level="national", value=1000, category="total")
    return PopulationFigure.objects.create(**{**fields, **kwargs})


def test_vulnerable_tab_is_hidden_until_it_has_data(client_viewer):
    _figure()
    url = reverse("reports:population")
    html = client_viewer.get(url + "?year=2026").content.decode()
    assert "Vulnerable population" not in html
    # an old link still answers, with the empty state
    old = client_viewer.get(url + "?view=vulnerable")
    assert old.status_code == 200 and "Vulnerability by district" not in old.content.decode()
    assert "Vulnerable population" in old.content.decode()  # the tab it asked for stays selected

    _figure(
        category="vulnerable",
        level="district",
        area_code="akkar",
        area_name="Akkar",
        vulnerability_level="high",
    )
    html = client_viewer.get(url + "?year=2026").content.decode()
    assert "Vulnerable population" in html
    html = client_viewer.get(url + "?year=2026&view=vulnerable").content.decode()
    assert "Vulnerability by district" in html


@pytest.fixture
def resources(db):
    rtype = ResourceType.objects.create(name="Evaluation")
    topic = ResourceTopic.objects.create(name="Learning")
    tag = ResourceTag.objects.create(name="Schools")
    tagged = Resource.objects.create(title="Learning study", publication_year="2025", type=rtype, topic=topic)
    tagged.tags.add(tag)
    Resource.objects.create(title="Water brief", publication_year="2025")
    return {"type": rtype, "topic": topic, "tag": tag}


@pytest.mark.parametrize(
    "query",
    ["type=abc", "topic=abc", "tag=abc", "tag=1.5", "topic=%20", "type=%C2%B2", "tag=-1"],
)
def test_library_ignores_malformed_ids_instead_of_failing(client_viewer, resources, query):
    for headers in ({}, {"HTTP_HX_REQUEST": "true"}):
        response = client_viewer.get(reverse("reports:library") + "?" + query, **headers)
        assert response.status_code == 200
        html = response.content.decode()
        # the filter is not dropped: nothing matches a malformed id
        assert "Learning study" not in html and "Water brief" not in html


def test_library_keeps_the_valid_ids_of_a_mixed_filter(client_viewer, resources):
    url = reverse("reports:library")
    html = client_viewer.get(f"{url}?type={resources['type'].id}&type=abc").content.decode()
    assert "Learning study" in html and "Water brief" not in html
    html = client_viewer.get(f"{url}?tag=x&tag={resources['tag'].id}").content.decode()
    assert "Learning study" in html and "Water brief" not in html


def test_data_health_prints_read_before_written_in_both_tables(client_viewer, db):
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="pd_indicators",
        status=SyncRun.Status.SUCCEEDED,
        rows_in=1200,
        rows_written=1180,
        rows_failed=20,
    )
    html = client_viewer.get(reverse("reports:data_health")).content.decode()
    assert html.count("1,200 / 1,180") == 2  # the dataset row and the run row, same order
    assert "1,180/1,200" not in html and "1,180 / 1,200" not in html
    assert html.count("Read / written") == 2 and "Rows held" in html


def test_odd_year_values_never_raise(client_viewer, db):
    """Superscript digits pass str.isdigit() but not int(): both pages must answer, not fail."""
    from django.urls import reverse

    for url in (
        reverse("reports:population") + "?year=%C2%B2",
        reverse("reports:assurance") + "?hact_year=%C2%B2",
    ):
        assert client_viewer.get(url).status_code == 200
