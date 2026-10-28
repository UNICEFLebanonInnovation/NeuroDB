"""The PD indicators the pages list (``identity.listed``, the user's decision of 28 Oct 2026): of a programme
document partner report rows name, an indicator eTools marks inactive in every row on which none of them is
placed is left out of every page that lists or counts PD indicators; one a row is placed on stays (partners'
values sit on it), and so do the active ones, those inactive in some rows only and every indicator of a
programme document no partner report names yet. The PD indicators and partner reports syncs mark them when
they store the proof, and Raw eTools data → PD indicators says how many. Fake programme documents and titles
only."""

from __future__ import annotations

import pytest
import responses
from django.urls import reverse
from django.utils import timezone

from neurodb.core.models import SyncRun
from neurodb.datamart import identity, monitoring, raw, reporting, services, trail_monitoring
from neurodb.datamart import models as dm
from neurodb.datamart.models import IndicatorFlag
from neurodb.integrations.etools import datamart_sync as sync
from neurodb.partnerships.models import PCA
from neurodb.review import checks as review
from neurodb.web import services as web
from tests.datamart.test_identity import (
    CHILDREN,
    TODAY,
    X,
    Y,
    _validator,
    indicator,
    make_client,
    make_pd,
    row,
    whole,
)

pytestmark = pytest.mark.django_db
OLD = "# of children reached (old wording)"
TEACHERS = "# of teachers trained"
SPARE = "# of kits (spare)"


def _world():
    """PD B: X active with a report row, Y inactive in every row with none (left out). PD C: an inactive indicator a
    row is placed on by its title (kept), and one inactive in one of its rows only (kept). PD D: an inactive
    indicator no report row names (left out) in a section no other indicator names, and an active one a report
    row names (kept)."""
    b = make_pd("B")
    indicator(b, X)
    indicator(b, Y, OLD, active=False)
    row(b)
    c = make_pd("C")
    indicator(c, 601, TEACHERS, "2.1 Teachers", active=False)
    row(c, title=TEACHERS, output="2.1 Teachers")
    dm.PDIndicator.objects.create(datamart_id=870_001, source_id=602, intervention=c, title="Mixed",
                                  is_active=False)  # fmt: skip
    dm.PDIndicator.objects.create(datamart_id=870_002, source_id=602, intervention=c, title="Mixed",
                                  is_active=True)  # fmt: skip
    d = make_pd("D")
    indicator(d, 701, SPARE, "3.1 Kits", active=False, places=("Tripoli",))
    dm.PDIndicator.objects.filter(source_id=701).update(section_name="Spare Section")
    indicator(d, 702, "# of kits", "3.1 Kits")
    row(d, title="# of kits", output="3.1 Kits")
    return b, c, d


def _marked() -> dict:
    """The rows marked as the sync marks them, stored in a partner reports run as the sync stores it."""
    found = identity.calibrate_and_list()
    SyncRun.objects.create(
        job=SyncRun.Job.ETOOLS_DATAMART,
        target="partner_reports",
        status=SyncRun.Status.SUCCEEDED,
        finished_at=timezone.now(),
        details={"identity": found},
    )
    return found


def _listed(source_id: int) -> set[bool]:
    return set(dm.PDIndicator.objects.filter(source_id=source_id).values_list("listed", flat=True))


# ------------------------------------------------------------------------------------------------- the rule
def test_inactive_with_no_report_row_is_left_out_and_every_other_indicator_stays():
    _world()
    before = dict(dm.PDIndicator.objects.values_list("pk", "synced_at"))
    found = _marked()
    assert found["inactive"] == {"indicators": 3, "without_rows": 2, "pds": 3, "rows_left_out": 3}
    assert _listed(Y) == {False} and _listed(701) == {False}  # inactive, no report row: left out
    assert _listed(601) == {True}  # inactive, a row placed on it by its title: kept
    assert _listed(602) == {True}  # inactive in one of its rows only: kept
    assert _listed(X) == {True} and _listed(702) == {True}  # active: kept
    assert (
        dict(dm.PDIndicator.objects.values_list("pk", "synced_at")) == before
    )  # never "updated by the sync"
    assert identity.listed(dm.PDIndicator.objects.all()).count() == dm.PDIndicator.objects.count() - 3
    assert identity.left_out(dm.PDIndicator.objects.all()).count() == 3


def test_a_row_placed_on_it_later_lists_it_again_and_a_row_gone_leaves_it_out():
    b, _c, _d = _world()
    _marked()
    later = row(b, title=OLD)
    _marked()
    assert _listed(Y) == {True}
    later.delete()
    _marked()
    assert _listed(Y) == {False}


def test_an_inactive_indicator_a_shared_title_could_go_to_stays():
    """Two indicators of PD E share a title and an output (no tie-breaker proved): the row could be either's, so
    the inactive one stays listed."""
    e = make_pd("E")
    indicator(e, 801)
    indicator(e, 802, active=False)
    row(e)
    _marked()
    assert _listed(802) == {True}


def test_a_programme_document_no_partner_report_names_keeps_every_indicator():
    """The rule reads the programme documents partner report rows name, as Raw eTools data's count always did
    (the 947 of the user's decision): PD F runs, no partner report names it yet, and eTools marks each of its
    indicators inactive. They stay listed, so Home still counts PD F among the running programme documents and
    their partners, and the filter still offers it."""
    b = make_pd("B")
    indicator(b, X)
    row(b)
    f = make_pd("F")
    indicator(f, 901, SPARE, "3.1 Kits", active=False)
    kept = ("running_programmes", "etools_partners", "pd_indicators")
    before = {k: web._etools_counts(2026)[k] for k in kept}
    assert before == {"running_programmes": 2, "etools_partners": 2, "pd_indicators": 2}
    found = _marked()
    assert found["inactive"] == {"indicators": 0, "without_rows": 0, "pds": 0, "rows_left_out": 0}
    assert _listed(901) == {True}
    assert {k: web._etools_counts(2026)[k] for k in kept} == before
    assert monitoring.filter_options(monitoring.Filters())["pds"] == [b.number, f.number]
    assert raw.inactive_words(identity.inactive_indicators()) == ""  # nothing to say: no PD of them was read


@responses.activate
@pytest.mark.parametrize(
    "name, url",
    [
        ("pd_indicators", "https://datamart.test/api/latest/datamart/pd-indicators/"),
        ("partner_reports", "https://datamart.test/api/latest/prp/datareport/"),
    ],
)
def test_the_pd_indicators_and_partner_reports_syncs_mark_them(name, url):
    _world()
    responses.get(url, json={"count": 0, "next": None, "results": []})
    (run,) = sync.sync_all(only=[name], client=make_client())
    assert run.details["identity"]["inactive"]["rows_left_out"] == 3
    assert _listed(Y) == {False} and _listed(X) == {True}
    assert identity.inactive_indicators()["without_rows"] == 2


# ------------------------------------------------------------------------------------------------- the readers
def test_programmes_indicators_leave_them_out_and_mark_an_inactive_one_kept():
    b, c, d = _world()
    pds = PCA.objects.filter(pk__in=[b.pk, c.pk, d.pk])
    every = {e["indicator"] for e in reporting.pd_indicators(pds, {})}
    assert OLD in every and SPARE in every  # before any sync marks them, as before
    _marked()
    found = {e["indicator"]: e for e in reporting.pd_indicators(pds, {})}
    assert set(found) == {CHILDREN, TEACHERS, "Mixed", "# of kits"}
    assert found[TEACHERS]["inactive"] and not found["Mixed"]["inactive"] and not found[CHILDREN]["inactive"]


def test_before_a_sync_marks_them_no_indicator_says_partner_reports_name_it(client, roles):
    """After the update and before the first sync marks the indicators (every row listed), the pages list what
    they listed before, and none says it is listed because partner reports name it: no report row names Y.
    Once a sync has marked them, Y is left out and the teachers' indicator, which a row names, says so."""
    b, c, _d = _world()
    filters = monitoring.Filters(pd_ids=[b.id, c.id], scope="all", year=None)
    found = {i.title: i for i in monitoring.indicators(filters, TODAY)}
    assert not found[OLD].inactive and identity.INACTIVE_NOTE not in found[OLD].notes
    assert not found[TEACHERS].inactive and identity.INACTIVE_NOTE not in found[TEACHERS].notes
    entries = {
        e["indicator"]: e for e in reporting.pd_indicators(PCA.objects.filter(pk__in=[b.pk, c.pk]), {})
    }
    assert not entries[OLD]["inactive"] and not entries[TEACHERS]["inactive"]
    client.force_login(_validator())
    label = "listed because partner reports name it"
    page = client.get(reverse("reports:programmes"), {"tab": "indicators", "scope": "all"}).content.decode()
    assert OLD in page and label not in page
    page = client.get(reverse("reports:programme_detail", args=[b.pk])).content.decode()
    assert OLD in page and label not in page
    _marked()
    found = {i.title: i for i in monitoring.indicators(filters, TODAY)}
    assert OLD not in found and found[TEACHERS].inactive and identity.INACTIVE_NOTE in found[TEACHERS].notes


def test_programmes_indicators_page_and_its_csv_leave_them_out(client, roles):
    _world()
    _marked()
    client.force_login(_validator())
    url = reverse("reports:programmes")
    page = client.get(url, {"tab": "indicators", "scope": "all"})
    text = page.content.decode()
    assert page.status_code == 200 and OLD not in text and SPARE not in text
    assert "Inactive in eTools: listed because partner reports name it" in text  # the teachers' indicator
    csv = b"".join(client.get(url, {"tab": "indicators", "scope": "all", "export": "csv"}).streaming_content)
    assert OLD.encode() not in csv and TEACHERS.encode() in csv


def test_partner_monitoring_and_data_quality_leave_them_out():
    b, c, d = _world()
    _marked()
    filters = monitoring.Filters(pd_ids=[b.id, c.id, d.id], scope="all", year=None)
    found = {i.title: i for i in monitoring.indicators(filters, TODAY)}
    assert set(found) == {CHILDREN, TEACHERS, "Mixed", "# of kits"}
    assert found[TEACHERS].inactive and identity.INACTIVE_NOTE in found[TEACHERS].notes
    assert not found["Mixed"].inactive and identity.INACTIVE_NOTE not in found["Mixed"].notes
    assert identity.INACTIVE_NOTE in monitoring.as_rows([found[TEACHERS]])[0].notes
    quality = monitoring.data_quality(list(found.values()), filters, TODAY)
    assert quality["indicators"] == 4
    assert "Spare Section" not in monitoring.filter_options(filters)["sections"]
    assert "Child Protection" in monitoring.filter_options(filters)["sections"]


def test_the_figure_trail_shows_the_step_that_leaves_them_out():
    b, _c, _d = _world()
    _marked()
    steps: list = []
    monitoring.indicators(monitoring.Filters(pd_ids=[b.id], scope="all", year=None), TODAY, steps=steps)
    names = [name for name, _value in steps]
    assert names[names.index("rows") + 1] == "listed"
    rows, listed = (value for name, value in steps if name in ("rows", "listed"))
    assert rows.count() - listed.count() == 2  # Y's two rows
    assert trail_monitoring.stage_title("listed", monitoring.Filters()) == (
        "eTools marks the indicator inactive and no partner report row names it (never listed)"
    )


def test_the_programme_document_page_leaves_them_out(client, roles):
    b, _c, d = _world()
    _marked()
    detail = services.programme_datamart(b, TODAY)
    assert [r.title for r in detail["monitoring"]["rows"]] == [CHILDREN]
    assert [i["title"] for i in services.programme_datamart(d, TODAY)["indicators"]] == ["# of kits"]
    client.force_login(_validator())
    page = client.get(reverse("reports:programme_detail", args=[b.pk])).content.decode()
    assert OLD not in page and CHILDREN in page


def test_a_unique_beneficiaries_indicator_the_pages_leave_out_is_not_said_gone_from_etools(client, roles):
    """A section editor chose Y, an older version, as PD B's unique beneficiaries indicator while the pages
    listed it. Once a sync leaves it out, the PD page says why it does not count (still in eTools, but inactive
    with no partner report naming it), never that it is no longer among its indicators in eTools; an indicator
    that has left eTools still reads so."""
    b, _c, _d = _world()
    IndicatorFlag.objects.create(
        source=IndicatorFlag.Source.ETOOLS, key=str(Y), label=OLD, unique_beneficiaries=True, intervention=b
    )
    assert services.programme_datamart(b, TODAY)["monitoring"]["flag_unused"] == ""
    _marked()
    found = services.programme_datamart(b, TODAY)["monitoring"]
    assert found["flag_unused"] == "left_out" and found["flagged_key"] == str(Y)
    client.force_login(_validator())
    page = client.get(reverse("reports:programme_detail", args=[b.pk])).content.decode()
    assert "is no longer among its indicators in eTools" not in page
    assert "is one eTools marks inactive that no partner report names" in page
    IndicatorFlag.objects.filter(key=str(Y)).update(key="599")  # an indicator no longer in eTools
    assert services.programme_datamart(b, TODAY)["monitoring"]["flag_unused"] == "missing"


def test_the_daily_review_counts_no_row_of_them():
    """No row names an eTools location here: the rows that name a place count, but not Y's two nor the spare
    one once they are left out."""
    _world()
    ctx = review.Context(TODAY)
    (draft,) = review.check_locations_unplaced(ctx)
    assert draft.title.startswith("9 indicator location rows ")
    _marked()
    (draft,) = review.check_locations_unplaced(ctx)
    assert draft.title.startswith("6 indicator location rows ")


def test_the_diagnostics_still_read_every_row_and_operations_names_them():
    """Only the pages that list or count PD indicators leave them out: the diagnostics of what eTools sent still
    read every row (the masters admin's section list, the CPD label lookup, an old link's pop-up), and
    docs/OPERATIONS.md names them so no one adds the rule to them."""
    from pathlib import Path

    from neurodb.cpd import sources
    from neurodb.datamart import curation

    b, _c, d = _world()
    _marked()
    assert "Spare Section" in curation.sections()
    assert sources._etools_title(d.id, "701") == SPARE
    assert monitoring.indicator_detail(b, str(Y), "QPR", None, TODAY) is not None
    text = " ".join((Path(__file__).resolve().parents[2] / "docs" / "OPERATIONS.md").read_text().split())
    assert "Every reader of PD indicators goes through" not in text
    for named in ("Raw eTools data (its *Active* filter included)", "check 10", "(`curation`)", "(`cpd.sources`)",
                  "(`monitoring.indicator_detail`)", "Do not add `identity.listed` to them."):  # fmt: skip
        assert named in text, named


# ----------------------------------------------------------------------------------------- Raw eTools data
def test_raw_etools_data_says_they_are_left_out_and_lists_them(client, roles):
    _world()
    whole()  # the PD indicators' last sync (the page says what it received), then the partner reports'
    _marked()
    assert raw.inactive_words(identity.inactive_indicators()) == (
        "eTools marks 3 indicators of 3 programme documents inactive in every row; 2 of them have no partner "
        "report row and are left out of Programmes → Indicators, Partner monitoring (Data quality too) and the "
        "programme document pages; the 1 other stays listed: partner report rows name it."
    )
    client.force_login(_validator())
    url = reverse("admin:datamart_raw_source", args=["pd_indicators"])
    page = client.get(url)
    assert 'href="?active=left_out"' in page.content.decode()
    assert client.get(url, {"active": "left_out"}).context["n"] == 3
    assert (
        client.get(url, {"active": "no"}).context["n"] == 6
    )  # and the teachers' two rows, Mixed's inactive one
    assert raw.Filters(active="left_out").words() == (
        "Active: no, and left out of the pages (no partner report row)"
    )


def test_raw_etools_data_before_a_sync_marks_them_says_the_next_one_will():
    """A run stored before the rule counted them but marked nothing: the pages still list them."""
    assert raw.inactive_words({"indicators": 1276, "without_rows": 947, "pds": 25}) == (
        "eTools marks 1,276 indicators of 25 programme documents inactive in every row; 947 of them have no "
        "partner report row: the next PD indicators or partner reports sync leaves them out of Programmes → "
        "Indicators, Partner monitoring (Data quality too) and the programme document pages."
    )
    assert raw.inactive_words({"indicators": 2, "without_rows": 2, "pds": 1, "rows_left_out": 4}).endswith(
        "have no partner report row and are left out of Programmes → Indicators, Partner monitoring (Data "
        "quality too) and the programme document pages."
    )
