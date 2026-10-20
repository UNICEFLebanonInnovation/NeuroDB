"""The eTools master indicator tables (datamart 0015) and their job's schedule (core 0031): the constraints that
keep one counted home per PD indicator, what clean() refuses, and the nightly schedule seeded due at once."""

from __future__ import annotations

import importlib

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from neurodb.core.models import ScheduledJob, SyncRun
from neurodb.datamart import models as dm
from neurodb.graph import refresh
from neurodb.reports import services

from . import masters_world as w
from .test_backfill import QUICK_MIGRATIONS

pytestmark = pytest.mark.django_db


@pytest.fixture
def pd(db):
    found = w.programme("A", 1)
    w.indicator(found, 101, "# of children enrolled in NFE")
    w.indicator(found, 102, "% of children passing the exam", display_type="percentage")
    w.indicator(found, 103, "Ratio of girls to boys enrolled", display_type="ratio")
    w.indicator(found, 104, "# of children reached with PSS", section=w.CP)
    return found


def new_link(master, pd, ident, **fields):
    return dm.EtoolsMasterLink(master=master, pd=pd, ident=ident, **fields)


def refused(item) -> dict[str, list[str]]:
    with pytest.raises(ValidationError) as caught:
        item.full_clean()
    return caught.value.message_dict


# ------------------------------------------------------------------------------------------------ the job
def test_the_job_and_its_schedule_are_registered():
    assert SyncRun.Job.ETOOLS_MASTERS == "etools_masters"
    assert SyncRun.Job.ETOOLS_MASTERS.label == "eTools master indicators"
    assert SyncRun.Job.ETOOLS_MASTERS in services.HEALTH_SKIPPED_JOBS  # worked out from the synced data
    assert ("core", "0031_etools_masters_job", "create") in QUICK_MIGRATIONS
    assert ("datamart", "0016_etools_masters", "CreateModel") in QUICK_MIGRATIONS
    # after the PRP business area code (core 0030, which adds the Datamart probe's job): both jobs are choices
    assert {SyncRun.Job.DATAMART_PROBE, SyncRun.Job.ETOOLS_MASTERS} <= set(SyncRun.Job.values)
    migration = importlib.import_module("neurodb.core.migrations.0031_etools_masters_job").Migration
    assert migration.dependencies == [("core", "0030_datamart_prp_setting")]


# The migration stamped the job due at the real moment the test database was created: "due now" is a statement
# about the real clock.
@pytest.mark.real_clock
def test_the_schedule_runs_every_night_after_the_checks_and_is_due_at_once():
    job = ScheduledJob.objects.get(key="etools-masters")
    assert (job.command, job.schedule, job.enabled) == ("etools_masters", "40 2 * * *", True)
    assert job.next_run_at is not None  # due now: not left for the scheduler to plan later
    checks = ScheduledJob.objects.get(key="reporting-checks")
    assert checks.schedule == "15 2 * * *"  # the suggestions come after the night's checks


def test_a_run_of_the_job_rebuilds_no_knowledge_hub(monkeypatch):
    asked = []
    monkeypatch.setattr(refresh, "request", lambda *args: asked.append(args))
    for job in (SyncRun.Job.ETOOLS_MASTERS, SyncRun.Job.ETOOLS_DATAMART):
        run = SyncRun.objects.create(job=job, status=SyncRun.Status.SUCCEEDED, rows_written=3)
        run.finished_at = timezone.now()
        run.save(update_fields=["finished_at"])
    assert asked == [("eTools Datamart sync",)]  # the sync's data, never the suggestions


# ----------------------------------------------------------------------------------------------- constraints
def test_a_section_has_one_active_master_of_a_name():
    w.master("Children enrolled")
    w.master("Children enrolled", status="dismissed")  # a dismissed proposal keeps its name
    w.master("Children enrolled", section=w.CP)
    with transaction.atomic(), pytest.raises(IntegrityError):
        w.master("Children enrolled", status="archived")
    same = dm.EtoolsMaster(section=w.EDUCATION, name="Children enrolled")
    assert refused(same)["__all__"] == ["This section already has a master indicator with this name."]


def test_a_pd_indicator_counts_in_one_total_at_most(pd):
    first, second, third = w.master("First"), w.master("Second"), w.master("Third", "percent")
    w.link(first, pd, 101)
    with transaction.atomic(), pytest.raises(IntegrityError):
        w.link(second, pd, 101)  # a second total
    with transaction.atomic(), pytest.raises(IntegrityError):
        w.link(second, pd, 101, status="suggested")  # a suggestion counts as a home too
    with transaction.atomic(), pytest.raises(IntegrityError):
        w.link(first, pd, 101, role="shown")  # one link per master and indicator
    w.link(second, pd, 101, role="shown")  # shown elsewhere
    w.link(third, pd, 101, role="numerator")  # or a numerator
    w.link(dm.EtoolsMaster.objects.create(section=w.EDUCATION, name="Fourth"), pd, 101, status="rejected")
    duplicate = new_link(w.master("Fifth"), pd, "s101", role="total", status="confirmed")
    assert "already counts in the total of another master" in refused(duplicate)["__all__"][0]


# ----------------------------------------------------------------------------------------------------- clean
def test_clean_fills_what_the_indicator_was_when_linked(pd):
    m = w.master("Children enrolled")
    link = new_link(m, pd, "s101", status="confirmed")
    link.full_clean()
    assert (link.title, link.pd_number, link.unit_class) == (
        "# of children enrolled in NFE",
        "PD/2026/001",
        "number",
    )


@pytest.mark.parametrize(
    "method, ident, role, field, words",
    [
        ("sum", "s102", "total", "role", "A sum adds numbers only"),
        ("sum", "s101", "numerator", "role", "only in a percentage or a ratio master"),
        ("percent", "s101", "total", "role", "takes percentages"),
        ("percent", "s103", "total", "role", "takes percentages"),
        ("ratio", "s102", "total", "role", "takes ratios"),
        ("percent", "s102", "denominator", "role", "must be a number"),
        ("sum", "s999", "total", "ident", "not one of the programme document's indicators"),
        ("sum", "s104", "total", "ident", "in section Child Protection, not in this master's section"),
    ],
)
def test_clean_refuses_a_link_that_does_not_fit(pd, method, ident, role, field, words):
    m = w.master("Master", method)
    errors = refused(new_link(m, pd, ident, role=role, status="confirmed"))
    assert words in " ".join(errors[field])


def test_clean_accepts_what_fits(pd):
    total = w.master("Passing", "percent")
    for ident, role in (("s102", "total"), ("s101", "numerator")):
        new_link(total, pd, ident, role=role, status="confirmed").full_clean()
    shown = w.master("Enrolled")
    new_link(shown, pd, "s102", role="shown", status="confirmed").full_clean()  # shown takes anything
    new_link(
        shown, pd, "s102", role="total", status="rejected"
    ).full_clean()  # a rejected link is not checked
    # an indicator that moved section stays linked: only a new link must be in the master's section
    moved = w.link(w.master("Protection", section=w.CP), pd, 101)
    moved.full_clean()


def test_an_average_keeps_its_totals_of_one_type(pd):
    m = w.master("Average", "average")
    w.link(m, pd, 102, unit_class="percentage")
    errors = refused(new_link(m, pd, "s103", status="confirmed"))
    assert errors["role"] == ["Every total of an average has one type: this master's are percentage."]
    new_link(m, pd, "s103", role="shown", status="confirmed").full_clean()


def test_a_method_change_that_breaks_a_link_is_refused(pd):
    m = w.master("Children enrolled")
    w.link(m, pd, 101)
    w.link(m, pd, 102, role="shown", unit_class="percentage")
    w.link(m, pd, 103, role="total", status="rejected", unit_class="ratio")  # rejected: never checked
    m.method = "percent"
    errors = refused(m)
    assert errors["method"][0].startswith(
        "1 linked indicator(s) would not fit this method (for example PD/2026/001:"
    )
    m.method = "maximum"  # takes numbers: fits
    m.full_clean()
    m.save()
    w.link(m, pd, 104, unit_class="percentage", status="suggested")
    m.method = "minimum"
    assert refused(m)["method"] == [
        "Every total of a minimum has one type: this master's are number, percentage."
    ]


def test_a_master_names_its_section():
    for section in ("", " ", "—"):
        assert "section" in refused(dm.EtoolsMaster(section=section, name="Children enrolled"))
