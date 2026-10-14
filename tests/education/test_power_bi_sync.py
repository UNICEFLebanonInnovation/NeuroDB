"""The education sync with Power BI's views (SPEC_PBI §4.1-4.2): which years NeuroDB asks Compiler to count,
which payloads it keeps (formats 2 and 3, Makani and Dirasa only), and the comparison with the view kept in
small rows (PowerBiCheck, PowerBiView), with the date a view's definition changed. No network: the payloads
are built here, as Compiler builds them (tests/education/payloads.py)."""

import json

import pytest
import responses
from django.db import DataError, IntegrityError, transaction

from neurodb.core.models import SyncRun
from neurodb.education import power_bi
from neurodb.education.models import EducationFigures, PowerBiCard, PowerBiCheck, PowerBiView
from neurodb.education.sync import sync, targets, wanted_years
from neurodb.youth.compiler import CompilerClient

from .payloads import (
    catalogue_entry,
    dirasa_payload,
    format_three,
    makani_payload,
    power_bi_block,
    view_definition,
)

pytestmark = pytest.mark.django_db

BASE = "https://compiler.test"
REGISTRATIONS = [
    {"child": 1, "center": 100, "sex": "Female", "nationality": 1, "disability": 1},
    {"child": 2, "center": 101, "sex": "Male", "nationality": 5, "disability": 3},
]
DIRASA = [{"child": 1, "school": 300, "partner": 7, "governorate": 20, "sex": "Female"}]
ROUND = "Bridging 2025-2026"


def index(*, new=True, mscc_years=None, bridging_rounds=None, extra=()):
    """Compiler's figures index: from format 3 each year says its format and whether it is stale."""

    def year(name, counted, stale=False, fmt=3):
        out = {"year": name, "counted": counted}
        if new:
            out.update(format=fmt if counted else None, stale=stale)
        return out

    mscc = mscc_years or [year("2026", True), year("2025", True, stale=True, fmt=2), year("2024", True)]
    rounds = bridging_rounds or [year("Bridging 2026-2027", False, stale=True), year(ROUND, True)]
    entries = [
        {"programme": "mscc", "label": "Makani (MSCC)", "current_year": "2026", "years": mscc},
        {"programme": "bridging", "label": "Bridging (Dirasa)", "current_year": ROUND, "years": rounds},
        *extra,
    ]
    for entry in entries:
        if new:
            entry["format_version"] = 3
    return entries


class Compiler:
    """A Compiler serving ``payloads`` ({(programme, year): payload}); it records what it was asked."""

    def __init__(self, payloads, the_index=None, status="succeeded"):
        self.payloads, self.the_index, self.status = payloads, the_index or index(), status
        self.runs, self.read = [], []

    def start_run(self, kind, payload=None):
        self.runs.append((kind, payload))
        return {"id": 1, "status": self.status, "targets": (payload or {}).get("targets")}

    def education_index(self):
        return self.the_index

    def education(self, programme, year):
        self.read.append((programme, year))
        return self.payloads.get((programme, year))


def mscc3(year="2026", **block):
    return format_three(makani_payload(REGISTRATIONS, year=year), power_bi_block("mscc", year, **block))


def mscc2(year="2025"):
    return makani_payload(REGISTRATIONS, year=year)


def dirasa3(**block):
    return format_three(dirasa_payload(DIRASA, round_name=ROUND), power_bi_block("bridging", ROUND, **block))


# ------------------------------------------------------------------------------------------- targets
def test_past_years_are_counted_again_when_stale_or_never_counted(settings):
    settings.COMPILER_EDUCATION_YEARS = 4
    the_index = index(
        mscc_years=[
            {"year": "2026", "counted": True, "format": 3, "stale": False},
            {"year": "2025", "counted": True, "format": 3, "stale": True},  # counted over a week ago
            {"year": "2024", "counted": True, "format": 3, "stale": False},
            {"year": "2023", "counted": False, "format": None, "stale": True},  # never counted
        ],
    )
    assert wanted_years(the_index[0]) == ["2026", "2025", "2024", "2023"]
    # a round listed before the current one (not started, never counted) is neither read nor counted
    assert wanted_years(the_index[1]) == [ROUND]
    assert targets(the_index) == [["mscc", "2026"], ["bridging", ROUND], ["mscc", "2025"], ["mscc", "2023"]]


def test_a_past_year_counted_in_an_older_format_is_counted_again(settings):
    settings.COMPILER_EDUCATION_YEARS = 3
    the_index = index(
        mscc_years=[
            {"year": "2026", "counted": True, "format": 3, "stale": False},
            {"year": "2025", "counted": True, "format": 2, "stale": False},
        ]
    )
    assert ["mscc", "2025"] in targets(the_index)


def test_an_older_compiler_is_asked_for_its_current_years_only(settings):
    settings.COMPILER_EDUCATION_YEARS = 3
    old = index(new=False)  # no "stale" nor "format": only "counted"
    assert wanted_years(old[0]) == ["2026", "2025", "2024"]  # the counted years are read, as before
    assert targets(old) == [["mscc", "2026"], ["bridging", ROUND]]
    compiler = Compiler({("mscc", "2026"): mscc2("2026")}, the_index=old)
    run = sync(triggered_by="test", client=compiler)
    assert compiler.runs == [("education", {"targets": [["mscc", "2026"], ["bridging", ROUND]]})]
    assert run.details["targets"] == [["mscc", "2026"], ["bridging", ROUND]]


def test_targets_never_name_another_programme_and_stop_at_twenty(settings):
    settings.COMPILER_EDUCATION_YEARS = 30
    years = [{"year": str(2026 - n), "counted": False, "format": None, "stale": True} for n in range(25)]
    the_index = index(
        mscc_years=years,
        extra=[{"programme": "tls", "label": "TLS", "current_year": "2026", "years": years[:1]}],
    )
    asked = targets(the_index)
    assert len(asked) == 20 and asked[:2] == [["mscc", "2026"], ["bridging", ROUND]]
    assert all(programme in ("mscc", "bridging") for programme, _year in asked)


# ---------------------------------------------------------------------------------------- the sync
def test_the_sync_reads_the_index_first_asks_for_its_targets_then_reads(settings):
    settings.COMPILER_EDUCATION_YEARS = 3
    compiler = Compiler(
        {("mscc", "2026"): mscc3(), ("mscc", "2025"): mscc2(), ("bridging", ROUND): dirasa3()}
    )
    run = sync(triggered_by="test", client=compiler)
    assert run.status == SyncRun.Status.SUCCEEDED, run.error
    assert compiler.runs == [
        ("education", {"targets": [["mscc", "2026"], ["bridging", ROUND], ["mscc", "2025"]]})
    ]
    assert compiler.read == [("mscc", "2026"), ("mscc", "2025"), ("mscc", "2024"), ("bridging", ROUND)]
    assert run.details["calculation"]["status"] == "succeeded"
    assert run.details["pending"] == ["mscc 2024"]  # not counted yet in Compiler: read next time


class Busy(Compiler):
    """A Compiler counting youth when the education sync asks: it answers with that count, which ends
    ``other`` (failed by default); then, once it is done, with the count asked for (``always``: the youth
    count again)."""

    def __init__(self, payloads, *, other="failed", always=False):
        super().__init__(payloads)
        self.other, self.always = other, always

    def start_run(self, kind, payload=None):
        self.runs.append((kind, payload))
        if len(self.runs) == 1 or self.always:
            youth = [["youth", None], ["youth", "all"]]
            return {"id": 7, "status": self.other, "targets": youth, "error": "youth all: QueryCanceled"}
        return {"id": 8, "status": self.status, "targets": (payload or {}).get("targets")}


def test_a_youth_count_going_is_waited_for_then_the_education_count_is_asked_again(settings):
    settings.COMPILER_EDUCATION_YEARS = 1
    compiler = Busy({("mscc", "2026"): mscc3(), ("bridging", ROUND): dirasa3()})
    run = sync(triggered_by="test", client=compiler)
    assert run.status == SyncRun.Status.SUCCEEDED, run.error  # the youth count's failure is not this run's
    asked = ("education", {"targets": [["mscc", "2026"], ["bridging", ROUND]]})
    assert compiler.runs == [asked, asked]
    assert (run.details["calculation_other"]["run"], run.details["calculation"]["run"]) == (7, 8)
    assert "calculation_note" not in run.details and run.details["stored"] == [
        "mscc 2026",
        f"bridging {ROUND}",
    ]


def test_still_answered_with_another_count_the_sync_reads_what_compiler_has(settings):
    settings.COMPILER_EDUCATION_YEARS = 1
    compiler = Busy({("mscc", "2026"): mscc3(), ("bridging", ROUND): dirasa3()}, always=True)
    run = sync(triggered_by="test", client=compiler)
    assert run.status == SyncRun.Status.SUCCEEDED and run.error == ""  # asked twice, never more
    assert (
        len(compiler.runs) == 2 and run.details["calculation_note"] == "Compiler was busy with another count"
    )
    assert run.details["stored"] == ["mscc 2026", f"bridging {ROUND}"]


def test_formats_two_and_three_are_both_kept_and_only_three_is_compared(settings):
    settings.COMPILER_EDUCATION_YEARS = 2
    compiler = Compiler(
        {("mscc", "2026"): mscc3(), ("mscc", "2025"): mscc2(), ("bridging", ROUND): dirasa3()}
    )
    sync(triggered_by="test", client=compiler)
    kept = {(f.programme, f.year): f.payload["format"] for f in EducationFigures.objects.all()}
    assert kept == {("mscc", "2026"): 3, ("mscc", "2025"): 2, ("bridging", ROUND): 3}
    assert set(PowerBiCheck.objects.values_list("programme", "period")) == {
        ("mscc", "2026"),
        ("bridging", ROUND),
    }
    assert power_bi.check("mscc", "2025") is None  # format 2: not compared yet

    check = power_bi.check("mscc", "2026")
    assert (check.view, check.mode, check.reason) == ("vw_mscc_data", "view", "")
    assert check.base["deleted"] == "exclude" and check.key_check["max_rows_per_id"] == 1
    assert (
        check.reconciliation["totals"]["counted"] == 4
        and check.reconciliation["totals"]["deleted_in_view"] == 1
    )
    assert check.periods[0]["view_rows"] == 5 and check.related["vw_center_program_staff"]["rows"] == [
        ["Yes", 3, 3]
    ]
    assert check.errors == []
    # every card version at the period total, computed at sync time (power_bi.VARIANTS): the staff's view_rows
    # come from the profile of vw_center_program_staff; Dirasa's teachers profile is not in this payload
    assert check.variants["children"] == {"registrations": 2, "view_rows": 2}
    assert check.variants["staff"] == {"all": 0, "active": 0, "at_centres_with_children": 0, "view_rows": 3}
    assert check.variants["working"] == {"yes": 0, "not_no": 2}  # both left blank ("Not specified")
    dirasa = power_bi.check("bridging", ROUND)
    assert dirasa.variants["children"] == {"unique": 1, "registrations": 1, "view_rows": 1}
    assert dirasa.variants["teachers"]["view_rows"] is None
    assert dirasa.variants["in_school"]["schools_all_listed"] == 400 + 250  # the schools partner 7 lists
    assert check.checked_at.isoformat() == "2026-10-16T02:31:10+00:00"
    assert check.counted_at is not None

    view = PowerBiView.objects.get(name="vw_mscc_data")
    assert (view.exists, view.can_select, view.kind) == (True, True, "view")
    assert view.columns[0] == ["id", "integer"] and view.reads == [["public", "mscc_registration", "table"]]
    assert view.definition == view_definition("mscc") and view.sha256 == f"{1:064x}"
    assert view.changed_at is None and view.previous_sha256 == "" and view.first_seen == view.checked_at
    assert PowerBiView.objects.filter(name="vw_bridging_data", exists=True).exists()


def test_only_makani_and_dirasa_are_kept_whatever_the_index_lists(settings):
    settings.COMPILER_EDUCATION_YEARS = 1
    other = {
        "programme": "youth",
        "label": "Youth",
        "current_year": "2026",
        "years": [{"year": "2026", "counted": True}],
    }
    compiler = Compiler(
        {("mscc", "2026"): mscc3(), ("bridging", ROUND): dirasa3(), ("youth", "2026"): mscc3()},
        the_index=index(extra=[other]),
    )
    sync(triggered_by="test", client=compiler)
    assert ("youth", "2026") not in compiler.read and ["youth", "2026"] not in compiler.runs[0][1]["targets"]
    assert set(EducationFigures.objects.values_list("programme", flat=True)) == {"mscc", "bridging"}


def test_a_changed_definition_is_dated_and_named_on_the_run(settings):
    settings.COMPILER_EDUCATION_YEARS = 1
    sync(triggered_by="test", client=Compiler({("mscc", "2026"): mscc3()}))
    first = PowerBiView.objects.get(name="vw_mscc_data")

    later = mscc3(version=2, checked_at="2026-10-17T02:31:10+00:00")
    run = sync(triggered_by="test", client=Compiler({("mscc", "2026"): later}))
    view = PowerBiView.objects.get(name="vw_mscc_data")
    assert run.details["power_bi_changed"] == ["vw_mscc_data"]
    assert (view.previous_sha256, view.sha256) == (first.sha256, f"{2:064x}")
    assert view.changed_at.isoformat() == "2026-10-17T02:31:10+00:00" and view.first_seen == first.first_seen
    assert view.definition == view_definition("mscc", 2)

    again = mscc3(version=2, checked_at="2026-10-18T02:31:10+00:00")
    run = sync(triggered_by="test", client=Compiler({("mscc", "2026"): again}))
    assert "power_bi_changed" not in run.details  # the same definition: no change
    assert PowerBiView.objects.get(name="vw_mscc_data").changed_at.isoformat() == "2026-10-17T02:31:10+00:00"


def test_an_older_description_never_turns_a_definition_back(settings):
    """A past year counted before the view changed describes the old definition: it is not a change."""
    settings.COMPILER_EDUCATION_YEARS = 2
    payloads = {
        ("mscc", "2026"): mscc3("2026", version=2, checked_at="2026-10-17T02:31:10+00:00"),
        ("mscc", "2025"): mscc3("2025", version=1, checked_at="2026-10-10T02:31:10+00:00", current=False),
    }
    run = sync(triggered_by="test", client=Compiler(payloads))
    assert "power_bi_changed" not in run.details
    assert PowerBiView.objects.get(name="vw_mscc_data").sha256 == f"{2:064x}"
    past = PowerBiCheck.objects.get(
        programme="mscc", period="2025"
    )  # its own comparison is kept all the same
    assert (past.mode, past.periods, past.related) == ("view", [], {})
    assert past.checked_at.isoformat() == "2026-10-10T02:31:10+00:00"


def test_a_view_that_disappears_keeps_its_last_definition_and_counts_as_a_change(settings):
    settings.COMPILER_EDUCATION_YEARS = 1
    sync(triggered_by="test", client=Compiler({("mscc", "2026"): mscc3()}))
    gone = mscc3(mode="rule", reason="view missing", checked_at="2026-10-17T02:31:10+00:00")
    run = sync(triggered_by="test", client=Compiler({("mscc", "2026"): gone}))
    view = PowerBiView.objects.get(name="vw_mscc_data")
    assert run.details["power_bi_changed"] == ["vw_mscc_data"]
    assert (view.exists, view.can_select, view.sha256, view.previous_sha256) == (
        False,
        False,
        "",
        f"{1:064x}",
    )
    assert view.definition == view_definition("mscc") and view.kind == "view"
    check = power_bi.check("mscc", "2026")
    assert (check.mode, check.reason, check.key_check) == ("rule", "view missing", {})


def test_a_payload_without_the_block_leaves_the_stored_check(settings):
    settings.COMPILER_EDUCATION_YEARS = 1
    sync(triggered_by="test", client=Compiler({("mscc", "2026"): mscc3()}))
    before = power_bi.check("mscc", "2026")
    sync(triggered_by="test", client=Compiler({("mscc", "2026"): mscc2("2026")}))  # Compiler rolled back
    after = power_bi.check("mscc", "2026")
    assert (after.pk, after.checked_at, after.mode) == (before.pk, before.checked_at, "view")
    assert EducationFigures.objects.get(programme="mscc", year="2026").payload["format"] == 2


def test_related_views_are_kept_and_a_newer_block_version_is_read(settings):
    settings.COMPILER_EDUCATION_YEARS = 1
    payload = mscc3(related={"vw_mscc_data_no_round": catalogue_entry("mscc", kind="materialized view")})
    payload["power_bi"]["version"] = 2  # a newer Compiler: only adds keys
    payload["power_bi"]["something_new"] = {"rows": 3}
    sync(triggered_by="test", client=Compiler({("mscc", "2026"): payload}))
    assert PowerBiView.objects.get(name="vw_mscc_data_no_round").kind == "materialized view"
    assert power_bi.check("mscc", "2026").mode == "view"


def test_a_failing_comparison_keeps_the_figures_and_marks_the_run_partial(settings, monkeypatch):
    settings.COMPILER_EDUCATION_YEARS = 1

    def broken(programme, period, payload):
        raise ValueError("bad block")

    monkeypatch.setattr(power_bi, "store", broken)
    run = sync(triggered_by="test", client=Compiler({("mscc", "2026"): mscc3()}))
    assert run.status == SyncRun.Status.PARTIAL and run.rows_failed == 0
    assert run.details["errors"][0]["examples"] == ["mscc 2026: comparison with Power BI"]
    assert EducationFigures.objects.filter(programme="mscc", year="2026").exists()


def test_card_variants_are_computed_at_sync_time_from_the_card_definitions(settings, monkeypatch):
    settings.COMPILER_EDUCATION_YEARS = 1

    def registrations(figures, filters):
        return figures.cube("enrolment").sum("registrations")

    def view_rows(figures, filters):
        return figures.cube("enrolment").sum("view_rows")

    def broken(figures, filters):
        raise KeyError("measure")

    cards = {"children": power_bi.Card("registrations", {"registrations": registrations, "view_rows": view_rows,
                                                          "broken": broken})}  # fmt: skip
    monkeypatch.setitem(power_bi.VARIANTS, "mscc", cards)
    sync(triggered_by="test", client=Compiler({("mscc", "2026"): mscc3()}))
    assert power_bi.check("mscc", "2026").variants == {
        "children": {"registrations": 2, "view_rows": 2, "broken": None}
    }


def test_the_check_and_the_card_choice_hold_counts_and_no_sql(settings, admin_user):
    """The masked definition lives in PowerBiView only (shown to administrators); the check holds counts."""
    settings.COMPILER_EDUCATION_YEARS = 1
    sync(triggered_by="test", client=Compiler({("mscc", "2026"): mscc3()}))
    check = power_bi.check("mscc", "2026")
    stored = json.dumps([check.base, check.key_check, check.reconciliation, check.periods, check.related])
    assert "SELECT" not in stored and "masked" not in stored and check.reason == ""
    card = PowerBiCard.objects.create(
        programme="mscc", card="children", variant="registrations", powerbi_value=39445, noted_by=admin_user
    )
    assert str(card) == "mscc children: registrations"
    with pytest.raises(IntegrityError), transaction.atomic():  # one choice per card
        PowerBiCard.objects.create(programme="mscc", card="children", variant="view_rows")


@responses.activate
def test_the_client_posts_the_targets():
    responses.post(BASE + "/api/figures/runs/", status=202, json={"id": 3, "status": "queued", "targets": []})
    client = CompilerClient(BASE, "abc123")
    client.start_run("education", {"targets": [["mscc", "2026"], ["mscc", "2025"]]})
    assert json.loads(responses.calls[0].request.body) == {"targets": [["mscc", "2026"], ["mscc", "2025"]]}


def test_a_comparison_that_cannot_be_kept_changes_nothing(settings):
    sync(triggered_by="test", client=Compiler({("mscc", "2026"): mscc3()}))
    broken = mscc3(version=2, checked_at="2026-10-17T02:31:10+00:00")
    broken["power_bi"]["catalogue"]["vw_mscc_data"]["definition"] = "SELECT 1\x00"  # a text column refuses it
    broken["power_bi"]["base"]["mode"] = "rule"
    with pytest.raises((DataError, ValueError)):
        power_bi.store("mscc", "2026", broken)
    view = PowerBiView.objects.get(name="vw_mscc_data")  # the database still answers: nothing was kept
    assert view.sha256 == f"{1:064x}" and power_bi.check("mscc", "2026").mode == "view"
