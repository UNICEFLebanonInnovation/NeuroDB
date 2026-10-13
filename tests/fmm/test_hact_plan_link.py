"""Monitoring insights' "Programmatic visits and HACT" block lists only partners visited in the filter: it
links the HACT visit plan of the Field monitoring page, where the programme documents never visited are."""

from django.urls import reverse


def test_the_hact_block_links_the_visit_plan_of_its_year(built, client_viewer):
    html = client_viewer.get(
        reverse("fmm:dashboard"), {"tab": "analysis", "year": "2026"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    plan = f'href="{reverse("reports:monitoring")}?tab=plan&amp;zero=1&amp;year=2026"'
    assert f"{plan}>see the HACT visit plan →</a>" in html
    assert "Partners and programme documents with no visit in the filter are not listed here:" in html
