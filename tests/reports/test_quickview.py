"""Quick views of library resources and maps: modal partials, full pages, inline files, access."""

import pytest
from django.test import override_settings
from django.urls import reverse

from neurodb.library.models import Map, Resource, ResourceTag, ResourceType

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def resource(db):
    tag = ResourceTag.objects.create(name="Adolescents")
    item = Resource.objects.create(
        title="Learning study",
        description="Full abstract.\nSecond paragraph of the abstract.",
        publication_year="2025",
        section="Education",
        type=ResourceType.objects.create(name="Evaluation"),
        resource_file=b"%PDF-1.4 test",
        resource_file_name="study.pdf",
        resource_image=PNG,
        resource_image_name="cover.png",
    )
    item.tags.add(tag)
    return item


@pytest.fixture
def map_item(db):
    return Map.objects.create(
        name="Schools map",
        status="Completed",
        description="Where schools were supported.",
        link="https://maps.example.org/app/1",
    )


def test_library_cards_offer_a_quick_view(client_viewer, resource):
    html = client_viewer.get(reverse("reports:library")).content.decode()
    url = reverse("reports:library_item", args=[resource.id])
    assert f'hx-get="{url}"' in html
    assert "Quick view" in html


def test_library_quick_view_modal_and_page(client_viewer, resource):
    url = reverse("reports:library_item", args=[resource.id])
    modal = client_viewer.get(url, HTTP_HX_REQUEST="true").content.decode()
    assert 'class="modal-header"' in modal
    assert "Second paragraph of the abstract." in modal
    assert "Adolescents" in modal
    assert reverse("reports:library_file", args=[resource.id]) in modal  # PDF preview frame
    assert reverse("reports:library_cover", args=[resource.id]) in modal
    page = client_viewer.get(url).content.decode()
    assert "<html" in page and "Second paragraph of the abstract." in page


def test_inline_pdf_can_be_framed_by_this_site_only(client_viewer, resource):
    response = client_viewer.get(reverse("reports:library_file", args=[resource.id]))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert "inline" in response["Content-Disposition"]
    assert response["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in response["Content-Security-Policy"]
    assert b"".join(response.streaming_content) == b"%PDF-1.4 test"


def test_only_pdfs_and_images_are_served_inline(client_viewer, resource):
    Resource.objects.filter(pk=resource.pk).update(
        resource_file_name="page.html", resource_image_name="x.svg"
    )
    assert client_viewer.get(reverse("reports:library_file", args=[resource.id])).status_code == 404
    assert client_viewer.get(reverse("reports:library_cover", args=[resource.id])).status_code == 404
    html = client_viewer.get(reverse("reports:library_item", args=[resource.id])).content.decode()
    assert reverse("reports:library_file", args=[resource.id]) not in html


def test_cover_image(client_viewer, resource):
    response = client_viewer.get(reverse("reports:library_cover", args=[resource.id]))
    assert response.status_code == 200 and response["Content-Type"] == "image/png"
    assert response.content == PNG


def test_unpublished_resources_have_no_quick_view(client_viewer, resource):
    Resource.objects.filter(pk=resource.pk).update(published=False)
    for name in ("library_item", "library_file", "library_cover"):
        assert client_viewer.get(reverse(f"reports:{name}", args=[resource.id])).status_code == 404


def test_map_quick_view(client_viewer, map_item):
    maps_page = client_viewer.get(reverse("reports:maps"))
    assert "frame-src 'self' https:" in maps_page["Content-Security-Policy"]
    url = reverse("reports:map_item", args=[map_item.id])
    assert f'hx-get="{url}"' in maps_page.content.decode()
    modal = client_viewer.get(url, HTTP_HX_REQUEST="true").content.decode()
    assert "Where schools were supported." in modal
    assert "maps.example.org" in modal
    assert 'src="https://maps.example.org/app/1"' in modal


def test_draft_maps_have_no_quick_view(client_viewer, map_item):
    Map.objects.filter(pk=map_item.pk).update(status="Draft")
    assert client_viewer.get(reverse("reports:map_item", args=[map_item.id])).status_code == 404


def test_other_pages_still_forbid_frames(client_viewer, resource):
    csp = client_viewer.get(reverse("reports:library"))["Content-Security-Policy"]
    assert "frame-src 'self';" in csp


def test_quick_views_follow_the_public_setting(client, resource, map_item):
    urls = [
        reverse("reports:library_item", args=[resource.id]),
        reverse("reports:library_file", args=[resource.id]),
        reverse("reports:map_item", args=[map_item.id]),
    ]
    for url in urls:
        assert client.get(url).status_code == 302  # sign-in first by default
    with override_settings(
        PUBLIC_PAGES=[
            "reports:library",
            "reports:library_item",
            "reports:library_file",
            "reports:library_cover",
            "reports:maps",
            "reports:map_item",
        ]
    ):
        for url in urls:
            assert client.get(url).status_code == 200
