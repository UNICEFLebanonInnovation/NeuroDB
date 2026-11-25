"""Monitoring insights' "Programmatic visits and HACT" block lists only partners visited in the filter: it
links the HACT visit plan of the Field monitoring page, where the programme documents never visited are. The
block has no PD status filter (the eTools HACT figures are the partner's, whatever its programme documents'
statuses): its link writes every status out (``status=``), so the plan, which opens on the active
programme documents since 25 Nov 2026, lists the signed, suspended and ended ones too."""

from django.urls import reverse


def test_the_hact_block_links_the_visit_plan_of_its_year(built, client_viewer):
    html = client_viewer.get(
        reverse("fmm:dashboard"), {"tab": "analysis", "year": "2026"}, HTTP_HX_REQUEST="true"
    ).content.decode()
    plan = f'href="{reverse("reports:monitoring")}?tab=plan&amp;zero=1&amp;year=2026&amp;status="'
    assert f"{plan}>see the HACT visit plan →</a>" in html
    assert "Partners and programme documents with no visit in the filter are not listed here:" in html
