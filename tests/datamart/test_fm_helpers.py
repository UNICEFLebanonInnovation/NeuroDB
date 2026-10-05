"""The field monitoring rules shared by every page (``neurodb.datamart.fm``): one visit definition in
Python and in SQL, the rating, status and answer vocabularies, the kind of an entity, the PCA/PD pair of
a reference and the completed programmatic visits per partner."""

import datetime
import re

import pytest

from neurodb.datamart import fm
from neurodb.datamart import models as dm
from neurodb.geo.models import Location, LocationType
from neurodb.partnerships.models import PartnerOrganization


def _finding(n, **fields):
    return dm.MonitoringFinding.objects.create(datamart_id=n, **fields)


# ------------------------------------------------------------------------------ visit keys
def test_visit_key_prefers_the_activity_id_then_the_reference_then_the_row():
    assert fm.visit_key(1722, "FM-2026-022", 5) == "1722"
    assert fm.visit_key("1722", "", 5) == "1722"
    assert fm.visit_key(" 01722 ", "", 5) == "1722"
    for not_an_id in (0, -3, None, "", "abc", True):
        key = fm.visit_key(not_an_id, "FM-2026-022", 5)
        assert re.fullmatch(r"r-[0-9a-f]{12}", key), not_an_id
    # stable, and the same reference however it is spaced or cased
    assert fm.visit_key(None, "fm-2026-022", 5) == fm.visit_key(0, "  FM-2026-022 ", 9)
    assert fm.visit_key(None, "FM  2026 022", 5) == fm.visit_key(None, "fm 2026\t022", 6)
    assert fm.visit_key(None, "FM-2026-022", 5) != fm.visit_key(None, "FM-2026-023", 5)
    assert fm.visit_key(None, "   ", 88) == "f-88" and fm.visit_key(None, None, 88) == "f-88"
    assert all(len(fm.visit_key(10**18, "", 1)) <= fm.VISIT_KEY_MAX for _ in range(1))


def test_norm_reference():
    assert fm.norm_reference(None) == ""
    assert fm.norm_reference("  fm-2026  /  22 \n") == "FM-2026 / 22"


@pytest.mark.django_db
def test_visit_key_expr_partitions_like_visit_key():
    rows = [
        _finding(1, monitoring_activity_id=1722, monitoring_activity="FM-1"),
        _finding(2, monitoring_activity_id=1722, monitoring_activity="FM-1 other spelling"),
        _finding(3, monitoring_activity_id=None, monitoring_activity="FM/2026/9"),
        _finding(4, monitoring_activity_id=0, monitoring_activity=" fm/2026/9 "),
        _finding(5, monitoring_activity_id=-1, monitoring_activity="FM/2026/9\t"),
        _finding(6, monitoring_activity_id=None, monitoring_activity="FM  2026  10"),
        _finding(7, monitoring_activity_id=None, monitoring_activity="fm 2026 10"),
        _finding(8, monitoring_activity_id=None, monitoring_activity=""),
        _finding(9, monitoring_activity_id=None, monitoring_activity="   "),
        _finding(10, monitoring_activity_id=1723, monitoring_activity="FM/2026/9"),
    ]
    python = {r.pk: fm.visit_key(r.monitoring_activity_id, r.monitoring_activity, r.pk) for r in rows}
    sql = dict(dm.MonitoringFinding.objects.annotate(vk=fm.visit_key_expr()).values_list("pk", "vk"))

    def partition(keys):
        groups = {}
        for pk, key in keys.items():
            groups.setdefault(key, set()).add(pk)
        return sorted(sorted(g) for g in groups.values())

    assert partition(python) == partition(sql)
    assert fm.count_visits(dm.MonitoringFinding.objects.all()) == len(set(python.values())) == 6


@pytest.mark.django_db
def test_count_visits_and_visits_by_year():
    _finding(1, monitoring_activity_id=1, end_date=datetime.date(2025, 12, 30))
    _finding(2, monitoring_activity_id=1, end_date=datetime.date(2026, 1, 2))  # the visit's date: 2026
    _finding(3, monitoring_activity_id=2, end_date=datetime.date(2025, 5, 1))
    _finding(4, monitoring_activity="FM-9", end_date=datetime.date(2026, 3, 1))
    _finding(5, monitoring_activity_id=3)  # no end date: left out of the years
    assert fm.count_visits(dm.MonitoringFinding.objects.all()) == 4
    assert fm.count_visits(dm.MonitoringFinding.objects.filter(end_date__year=2026)) == 2
    assert fm.visits_by_year(dm.MonitoringFinding.objects.all()) == {2025: 1, 2026: 2}


# ------------------------------------------------------------------------------ vocabularies
@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("On Track", "on_track"),
        ("on-track", "on_track"),
        ("ONTRACK", "on_track"),
        ("green", "on_track"),
        ("Off Track", "off_track"),
        ("off_track", "off_track"),
        ("red", "off_track"),
        ("Constrained", "constrained"),
        ("partially", "constrained"),
        ("Partially on track", "constrained"),
        ("amber", "constrained"),
        ("Yellow", "constrained"),
        ("", "not_monitored"),
        (None, "not_monitored"),
        ("Not Monitored", "not_monitored"),
        ("n/a", "not_monitored"),
        ("NA", "not_monitored"),
        ("none", "not_monitored"),
        ("Not applicable", "not_monitored"),
        ("weird", "other"),
    ],
)
def test_normalize_rating(raw, code):
    assert fm.normalize_rating(raw) == code


def test_normalize_status_and_group():
    for status in fm.STATUSES:
        assert fm.normalize_status(status) == status
        assert fm.normalize_status(status.replace("_", " ").title()) == status
    assert fm.normalize_status("Data Collection") == "data_collection"
    assert fm.normalize_status("canceled") == "cancelled"
    assert fm.normalize_status("in review by HQ") == ""
    assert fm.normalize_status(None) == ""
    groups = {s: fm.status_group(s) for s in fm.STATUSES}
    assert {s for s, g in groups.items() if g == "planned"} == {"draft", "checklist", "review", "assigned"}
    assert {s for s, g in groups.items() if g == "in_progress"} == {"data_collection", "report_finalization"}
    assert {s for s, g in groups.items() if g == "reported"} == {"submitted", "completed"}
    assert groups["cancelled"] == "cancelled"
    assert fm.status_group("") == fm.status_group("in review") == "unknown"
    assert sorted(fm.STATUS_RANK, key=fm.STATUS_RANK.get)[-1] == "completed"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("Yes", "yes"),
        ("y", "yes"),
        ("TRUE", "yes"),
        (True, "yes"),
        ("oui", "yes"),
        ("نعم", "yes"),
        ("No", "no"),
        ("n", "no"),
        (False, "no"),
        ("لا", "no"),
        ("Off track", "off_track"),
        ("On Track", "on_track"),
        ("Constrained", "constrained"),
        ("n/a", ""),
        ("", ""),
        ("Children attended the sessions", ""),
    ],
)
def test_normalize_answer(raw, code):
    assert fm.normalize_answer(raw) == code


@pytest.mark.parametrize(
    ("entity_type", "entity", "kind"),
    [
        ("PD/SSFA", "", "pd"),
        ("SSFA", "", "pd"),
        ("intervention", "LEB/PD1", "pd"),
        ("Programme Document", "", "pd"),
        ("CP Output", "Output 1", "cp_output"),
        ("Partner", "Amel Association", "partner"),
        ("partner organization", "", "partner"),
        ("", "2.2 INCREASED ACCESS TO EDUCATION", "cp_output"),
        ("", "LEBA/PCA2023597/PD2025123: Education", "pd"),
        ("", "Amel Association", "other"),
        (None, None, "other"),
    ],
)
def test_entity_kind(entity_type, entity, kind):
    assert fm.entity_kind(entity_type, entity) == kind


def test_pd_token():
    assert fm.pd_token("LEBA/PCA2023597/PD2025123") == ("PCA2023597", "PD2025123")
    assert fm.pd_token("LEB/PCA2023597/SPD2025123-2") == ("PCA2023597", "SPD2025123")
    assert fm.pd_token("PCA2023597 / PD2025123") == ("PCA2023597", "PD2025123")
    assert fm.pd_token("leb/pca2023597/ssfa2024001") == ("PCA2023597", "SSFA2024001")
    assert fm.pd_token("LEB/SSFA2024001") is None
    assert fm.pd_token(None) is None


# ------------------------------------------------------------------------------ assurance and places
@pytest.mark.django_db
def test_programmatic_visits_by_partner_counts_completed_programmatic_visits_per_row_partner():
    a = PartnerOrganization.objects.create(etl_id="1", name="A", partner_type="CSO", vendor_number="V1")
    b = PartnerOrganization.objects.create(etl_id="2", name="B", partner_type="CSO", vendor_number="V2")
    done = {"is_programmatic_visit": True, "status": "Completed", "end_date": datetime.date(2026, 5, 1)}
    _finding(1, partner=a, monitoring_activity_id=10, **done)
    _finding(2, partner=a, monitoring_activity_id=10, **done)  # same visit, second entity: once
    _finding(3, partner=b, monitoring_activity_id=10, **done)  # a multi-partner visit counts for each
    _finding(4, partner=a, monitoring_activity="FM-11", **done)
    _finding(5, partner=a, monitoring_activity_id=12, **{**done, "is_programmatic_visit": False})
    _finding(6, partner=a, monitoring_activity_id=13, **{**done, "status": "submitted"})
    _finding(7, partner=a, monitoring_activity_id=14, **{**done, "end_date": datetime.date(2025, 5, 1)})
    _finding(8, monitoring_activity_id=15, **done)  # no partner
    assert fm.programmatic_visits_by_partner(2026) == {a.pk: 2, b.pk: 1}
    assert fm.programmatic_visits_by_partner(2025) == {a.pk: 1}


@pytest.mark.django_db
def test_in_location_subtree_counts_a_site_only_row_by_its_site():
    kind = LocationType.objects.create(name="Cadaster", admin_level=3)
    tree = {"lft": 1, "rght": 2, "level": 0, "tree_id": 1}
    place = Location.objects.create(id=5, name="Halba", p_code="LB1", type=kind, **tree)
    other = Location.objects.create(id=6, name="Tyre", p_code="LB8", type=kind, **tree)
    site = dm.MonitoringSite.objects.create(datamart_id=1, name="Halba School", parent=place)
    _finding(1, location=place)
    _finding(2, monitoring_site=site)  # the location arrived as a name only
    _finding(3, location=other, monitoring_site=site)  # its own location wins
    found = dm.MonitoringFinding.objects.filter(fm.in_location_subtree([place.pk]))
    assert sorted(found.values_list("datamart_id", flat=True)) == [1, 2]


@pytest.mark.django_db
def test_place_gives_the_governorate_and_district_ids():
    from neurodb.datamart.monitoring import _gazetteer, _place

    tree = {"lft": 1, "rght": 2, "level": 0, "tree_id": 1}
    types = {n: LocationType.objects.create(name=f"L{n}", admin_level=n) for n in (0, 1, 2, 3)}
    country = Location.objects.create(id=1, name="Lebanon", type=types[0], **tree)
    gov = Location.objects.create(id=10, name="Akkar", type=types[1], parent=country, **tree)
    district = Location.objects.create(
        id=20, name="Halba", type=types[2], parent=gov, latitude=34.5, longitude=36.1, **tree
    )
    village = Location.objects.create(id=30, name="Halba village", type=types[3], parent=district, **tree)
    place = _place(village.pk, _gazetteer({village.pk}))
    assert (place["governorate"], place["governorate_id"]) == ("Akkar", gov.pk)
    assert (place["district"], place["district_id"]) == ("Halba", district.pk)
    assert (place["approximate"], place["located_by"]) == (True, "Halba")  # unchanged keys
    assert _place(None, {})["governorate_id"] is None


def test_field_monitoring_ratings_and_status_groups_have_colours():
    from pathlib import Path

    from neurodb.web.templatetags.ui import STATUS_VARIANTS

    assert STATUS_VARIANTS["constrained"] == "warning" and STATUS_VARIANTS["not_monitored"] == "neutral"
    assert {k: STATUS_VARIANTS[k] for k in ("reviewed", "follow_up", "data_issue")} == {
        "reviewed": "success",
        "follow_up": "warning",
        "data_issue": "danger",
    }
    assert [STATUS_VARIANTS[k] for k in ("planned", "in_progress", "reported")] == [
        "neutral",
        "info",
        "success",
    ]
    charts = (Path(__file__).parents[2] / "neurodb/web/static/js/charts.js").read_text()
    assert 'constrained: "--nd-warning"' in charts and 'not_monitored: "--nd-muted"' in charts
