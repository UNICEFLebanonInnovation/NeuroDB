"""The youth sync with Power BI's view (SPEC_PBI §4.2): NeuroDB asks Compiler for a youth count first (an older
Compiler refuses the programme: "unavailable", and its youth API is read as before), reads the years and all
years together, keeps payloads of format 1 and 2, and keeps "all" out of every reader of the latest year. No
network except through ``responses``; the payloads are built here, as Compiler builds them."""

import json

import pytest
import responses

from neurodb.core.models import SyncRun
from neurodb.cpd import sources
from neurodb.education import power_bi
from neurodb.education.models import PowerBiCheck, PowerBiView
from neurodb.graph.builders import Names, add_compiler
from neurodb.graph.store import Collector
from neurodb.integrations.http import IntegrationError
from neurodb.youth import services
from neurodb.youth.compiler import CompilerClient
from neurodb.youth.models import YouthAllYears, YouthFigures, YouthIndicatorLink
from neurodb.youth.sync import sync, targets
from tests.cpd.test_cpd import cycle, framework  # noqa: F401 - fixtures
from tests.education.payloads import power_bi_block
from tests.youth.test_youth import ENROLMENTS, etools, payload  # noqa: F401 - etools is a fixture

pytestmark = pytest.mark.django_db

BASE = "https://compiler.test"
URL = BASE + "/api/youth/indicator-figures/"


def format_two(year="2026", **block):
    """Youth figures as Compiler sends them from format 2: ``registered`` (every counted registration, with or
    without an enrolment) and ``enrolments`` blocks, and the ``power_bi`` block; "all" has no targets."""
    out = {**payload(year), "format": 2}
    people = {e["youth"] for e in ENROLMENTS}
    out["blocks"] = {
        "registered": {
            "measures": ["people", "registrations", "enrolled_people"],
            "figures": [
                {"by": [], "rows": [[len(people) + 1, len(people) + 1, len(people)]]},
                {"by": ["home_governorate"], "rows": [[20, 2, 2, 1], [22, 2, 2, 2]]},
            ],
        },
        "enrolments": {
            "measures": ["enrolments", "people"],
            "figures": [{"by": [], "rows": [[len(ENROLMENTS), len(people)]]},
                        {"by": ["master"], "rows": [[10, len(ENROLMENTS), len(people)]]}],
        },
    }  # fmt: skip
    out["power_bi"] = power_bi_block("youth", year, **block)
    if year == YouthFigures.ALL:
        out["targets"] = []
    return out


class Compiler:
    """A Compiler that counts youth in its figures runs (format 2): ``status`` is the run's end, ``other`` the
    targets of another count already going (Compiler answers with that one)."""

    def __init__(self, years=("2026", "2025"), status="succeeded", other=None, every_year=None):
        self.years, self.status, self.other = years, status, other
        self.every_year = every_year
        self.runs, self.asked = [], []

    def start_run(self, kind, payload=None):
        self.runs.append((kind, payload))
        asked = self.other if self.other is not None else (payload or {}).get("targets")
        return {
            "id": 5,
            "status": self.status,
            "targets": asked,
            "error": "stopped" if self.status == "failed" else "",
        }

    def figures(self, year=None):
        self.asked.append(year)
        if year == YouthFigures.ALL:
            if isinstance(self.every_year, Exception):
                raise self.every_year
            return self.every_year if self.every_year is not None else format_two(YouthFigures.ALL)
        year = year or self.years[0]
        return format_two(year) if year in self.years else None


# --------------------------------------------------------------------------------------- the count
def test_the_youth_count_is_asked_for_first_with_the_years_kept(settings):
    settings.COMPILER_YOUTH_YEARS = 2
    first = Compiler()
    run = sync(triggered_by="test", client=first)
    assert run.status == SyncRun.Status.SUCCEEDED, run.error
    # nothing kept yet: the current year and all years (a year Compiler does not have would refuse the count)
    assert first.runs == [
        ("education", {"programme": "youth", "targets": [["youth", None], ["youth", "all"]]})
    ]
    assert first.asked == [None, "2025", "all"]
    assert run.details["calculation"]["status"] == "succeeded" and run.details["years"] == [
        "2026",
        "2025",
        "all",
    ]

    second = Compiler()
    sync(triggered_by="test", client=second)
    assert second.runs[0][1]["targets"] == [["youth", None], ["youth", "2025"], ["youth", "all"]]
    assert targets() == [["youth", None], ["youth", "2025"], ["youth", "all"]]


def test_formats_one_and_two_are_kept_and_compared_with_the_view(settings):
    settings.COMPILER_YOUTH_YEARS = 2

    class Mixed(Compiler):
        def figures(self, year=None):
            got = super().figures(year)
            return payload("2025") if year == "2025" else got  # the year before still in format 1

    sync(triggered_by="test", client=Mixed())
    kept = dict(YouthFigures.objects.values_list("year", "payload__format"))
    assert kept == {"2026": 2, "2025": 1}
    assert list(YouthAllYears.objects.values_list("year", "payload__format")) == [("all", 2)]
    assert set(PowerBiCheck.objects.values_list("programme", "period")) == {
        ("youth", "2026"),
        ("youth", "all"),
    }
    check = power_bi.check("youth", "2026")
    assert (check.view, check.mode, check.base["period_column"]) == ("vw_youth_data_all", "view", "year_id")
    assert check.reconciliation["by"][-2:] == ["no_enrolment", "no_master"]
    assert check.counted_at is not None  # the payload's generated_at
    assert PowerBiView.objects.get(name="vw_youth_data_all").reads == [
        ["public", "youth_registration", "table"]
    ]


def test_all_years_are_never_the_latest_year(settings, etools, framework):  # noqa: F811 - fixtures
    settings.COMPILER_YOUTH_YEARS = 2
    sync(triggered_by="test", client=Compiler())
    # every youth figures row is a year: the release before this one reads them all as years, and "all" sorts
    # after "2026", so going back to it must find no "all" there (its years(), the youth page's default year
    # years[0], the assistant's answer and the knowledge hub's youth record all read the rows this way)
    assert list(YouthFigures.objects.order_by("-year").values_list("year", flat=True)) == ["2026", "2025"]
    assert YouthFigures.objects.order_by("-year").first().year == "2026"
    assert YouthAllYears.objects.get().payload["year"] == "all"  # kept apart, read by the youth page
    assert services.years() == ["2026", "2025"] and services.stored_years() == (["2026", "2025"], True)
    assert services.load("all").payload["year"] == "all" and services.stored_row("all").year == "all"

    collector = Collector()
    add_compiler(collector, Names())
    youth = [e for (kind, _key), e in collector.entities.items() if kind == "youth_indicator"]
    assert youth and {e["attrs"]["year"] for e in youth} == {"2026"}

    values = [v for _group, options in sources.choices(framework.reached) for v, _label in options]
    assert any(v.startswith("youth:2026:") for v in values) and not any(
        v.startswith("youth:all:") for v in values
    )
    # links are per year: none is suggested for all years together
    assert not YouthIndicatorLink.objects.filter(year=YouthFigures.ALL).exists()
    assert YouthIndicatorLink.objects.filter(year="2026").exists()


EDUCATION = [["mscc", "2026"], ["bridging", "Bridging 2025-2026"]]


def test_another_count_going_is_waited_for_and_not_taken_for_the_youth_one(settings):
    settings.COMPILER_YOUTH_YEARS = 1
    busy = Compiler(status="failed", other=EDUCATION)
    run = sync(triggered_by="test", client=busy)
    # the education count failed, not the youth one: the youth API counted on the spot, the run is fine
    assert run.status == SyncRun.Status.SUCCEEDED and run.error == ""
    assert run.details["calculation_note"] == "Compiler was busy with another count"
    assert len(busy.runs) == 2  # asked again once the other count was done, never more
    assert run.details["years"] == ["2026", "all"]


def test_once_the_other_count_is_done_the_youth_count_is_asked_again(settings):
    settings.COMPILER_YOUTH_YEARS = 1

    class Busy(Compiler):
        def start_run(self, kind, payload=None):
            self.other = EDUCATION if not self.runs else None  # busy with education the first time only
            return super().start_run(kind, payload)

    busy = Busy()
    run = sync(triggered_by="test", client=busy)
    assert run.status == SyncRun.Status.SUCCEEDED, run.error
    asked = ("education", {"programme": "youth", "targets": [["youth", None], ["youth", "all"]]})
    assert busy.runs == [asked, asked]  # so "all", which the youth API never counts on the spot, is counted
    assert run.details["calculation_other"]["targets"] == EDUCATION
    assert (
        run.details["calculation"]["targets"] == asked[1]["targets"] and "calculation_note" not in run.details
    )


def test_a_failed_youth_count_is_noted_and_the_figures_are_read(settings):
    settings.COMPILER_YOUTH_YEARS = 1
    run = sync(triggered_by="test", client=Compiler(status="failed"))
    assert run.status == SyncRun.Status.PARTIAL and run.error == "BMA calculation failed: stopped"
    assert run.details["years"] == ["2026", "all"]


def test_a_count_stopped_from_the_admin_reads_nothing(settings, monkeypatch):
    settings.COMPILER_YOUTH_YEARS, settings.COMPILER_RUN_POLL_SECONDS = 1, 0
    monkeypatch.setattr(SyncRun, "stopped", lambda self: True)
    waiting = Compiler(status="running")
    run = sync(triggered_by="test", client=waiting)
    assert run.status == SyncRun.Status.FAILED and waiting.asked == []
    assert not YouthFigures.objects.exists()


def test_all_years_answered_with_another_year_are_not_kept(settings):
    settings.COMPILER_YOUTH_YEARS = 1
    run = sync(triggered_by="test", client=Compiler(every_year=format_two("2026")))
    assert run.status == SyncRun.Status.PARTIAL
    assert "for all years" in run.details["errors"][0]["error"]
    assert list(YouthFigures.objects.values_list("year", flat=True)) == ["2026"]


def test_all_years_that_cannot_be_read_leave_the_years(settings):
    settings.COMPILER_YOUTH_YEARS = 1
    failing = IntegrationError("GET /api/youth/indicator-figures/: HTTP 503", status=503)
    run = sync(triggered_by="test", client=Compiler(every_year=failing))
    assert run.status == SyncRun.Status.PARTIAL and run.details["years"] == ["2026"]
    assert run.details["errors"][0]["examples"] == ["all"]


# ------------------------------------------------------------------------------ an older Compiler (HTTP)
@responses.activate
def test_an_older_compiler_refusing_the_youth_programme_is_read_as_before(settings):
    settings.COMPILER_YOUTH_YEARS = 2
    responses.post(
        BASE + "/api/figures/runs/",
        status=400,
        json={"detail": "Unknown programme", "programmes": ["bridging", "mscc"]},
    )
    responses.get(URL, json=payload("2026"), match=[responses.matchers.query_param_matcher({})])
    for year in ("2025", "all"):  # an older Compiler has neither: "No such reporting year"
        responses.get(
            URL,
            status=404,
            json={"detail": f"No such reporting year: {year}"},
            match=[responses.matchers.query_param_matcher({"year": year})],
        )
    run = sync(triggered_by="test", client=CompilerClient(BASE, "abc123"))
    assert run.status == SyncRun.Status.SUCCEEDED, run.error
    assert run.details["calculation"] == {
        "status": "unavailable",
        "error": "this BMA cannot be asked to calculate",
    }
    assert json.loads(responses.calls[0].request.body) == {
        "programme": "youth",
        "targets": [["youth", None], ["youth", "all"]],
    }
    assert list(YouthFigures.objects.values_list("year", flat=True)) == ["2026"]
    assert not PowerBiCheck.objects.exists()  # format 1: not compared yet


@responses.activate
def test_a_refused_count_for_another_reason_is_a_failure():
    responses.post(BASE + "/api/figures/runs/", status=400, json={"detail": "No such year: 2019"})
    client = CompilerClient(BASE, "abc123")
    with pytest.raises(IntegrationError):
        client.start_run("education", {"programme": "youth", "targets": [["youth", "2019"]]})
    responses.post(BASE + "/api/wellbeing/runs/", status=400, body="not json")
    with pytest.raises(IntegrationError):
        client.start_run("wellbeing")


def test_the_migration_moves_an_all_years_row_out_of_the_years():
    """youth 0002: the "all" figures a build of this release kept among the years move to their own table."""
    import importlib

    from django.apps import apps
    from django.utils import timezone

    migration = importlib.import_module("neurodb.youth.migrations.0002_youth_all_years")
    YouthFigures.objects.create(year="2026", fetched_at=timezone.now(), payload=format_two("2026"))
    YouthFigures.objects.create(year="all", fetched_at=timezone.now(), payload=format_two("all"))
    migration.move_all_years(apps, None)
    assert list(YouthFigures.objects.values_list("year", flat=True)) == ["2026"]
    assert YouthAllYears.objects.get().payload["year"] == "all"
    migration.move_all_years(apps, None)  # nothing left to move
    assert YouthAllYears.objects.count() == 1
