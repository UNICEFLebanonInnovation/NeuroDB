"""The moment of the eTools figures: what the keys of the eTools pages' stored figures are made of.

Partner monitoring (its indicators and filter options, its map) and partner reporting (its Report/Location
and Report/Disaggregation tables, its filter options) keep what they compute in the cache every worker
shares (:func:`neurodb.core.compute.once`), so the parts of one page, asked for separately and answered by
different workers, read one computation. Those figures come from tables that only the eTools jobs write, so
their keys hold the latest run of each of those jobs (:func:`jobs`), and not the last run of any job: a
NeuroDB Watch run or an ActivityInfo import moves nothing here (SPEC7_review item 23d):

- ``etools``: programme documents and partners;
- ``etools_datamart``: PD indicators, partner reports, disaggregation labels, action points, TPM, monitoring
  findings, funds (its latest partner reports run also sets the first reporting period NeuroDB holds,
  ``reached.reports_read_from``);
- ``datamart_backfill``: fills columns of rows already synced (after a deployment);
- ``locations``: the gazetteer.

A run that starts or ends gives a new key (its status and end are in it): a sync writes as it goes. The
indicator flags (:func:`flags`) are in it too, as on the overview, though none of these pages reads them
yet.

Staff never edit these tables by hand (every eTools and Datamart model is read-only in the admin), with one
exception: the gazetteer (Admin → Locations and Location types), which the map reads to place each location;
the map's key holds :func:`gazetteer` as well.

Nothing here depends on the person asking, and nothing is translated: a key never holds the user or the
language (the values are stored with ``lang_neutral``: their labels are English constants, translated in the
templates). Each of these is read once per request (:func:`neurodb.core.compute.per_request`): a frame's
look at what is stored and the part it then draws inline share one reading.
"""

from __future__ import annotations

from django.db import connection
from django.db.models import Count, Max

from neurodb.core import compute
from neurodb.core.models import SyncRun

# the jobs that write what the eTools pages read
ETOOLS_JOBS = (
    SyncRun.Job.ETOOLS,
    SyncRun.Job.ETOOLS_DATAMART,
    SyncRun.Job.DATAMART_BACKFILL,
    SyncRun.Job.LOCATIONS,
)


def _jobs() -> str:
    """The latest run of each job of :data:`ETOOLS_JOBS`, with its status and end. Read through the index
    (job, status, newest start first): the latest run of each job and status, then the latest of those per
    job, in one query; per job alone the index would be walked to its end for a job without recent runs, and
    sorting a year of Datamart runs (about 19,000) takes 30 ms."""
    table = connection.ops.quote_name(SyncRun._meta.db_table)
    query = (
        "SELECT DISTINCT ON (r.job) r.job, r.id, r.status, r.finished_at "  # noqa: S608 - the model's table
        "FROM unnest(%s::text[]) AS j(job) CROSS JOIN unnest(%s::text[]) AS st(status) CROSS JOIN LATERAL ("
        f"SELECT s.job, s.id, s.status, s.finished_at, s.started_at FROM {table} s "
        "WHERE s.job = j.job AND s.status = st.status ORDER BY s.started_at DESC, s.id DESC LIMIT 1) r "
        "ORDER BY r.job, r.started_at DESC, r.id DESC"
    )
    with connection.cursor() as cursor:
        cursor.execute(query, [[job.value for job in ETOOLS_JOBS], list(SyncRun.Status.values)])
        latest = cursor.fetchall()
    return ";".join(
        f"{job}={pk}.{status}.{finished.timestamp() if finished else '-'}"
        for job, pk, status, finished in latest
    )


def jobs() -> str:
    """The latest run of each eTools job (see the module docstring), as text. Read once per request."""
    return compute.per_request("etools-jobs", _jobs)


def flags() -> str:
    """The indicator flags (count and last change). Read once per request."""

    def read() -> str:
        from .models import IndicatorFlag

        found = IndicatorFlag.objects.aggregate(n=Count("pk"), last=Max("updated_at"))
        return f"{found['n']}:{found['last'].isoformat() if found['last'] else ''}"

    return compute.per_request("etools-flags", read)


def fingerprint() -> str:
    """The moment of the eTools figures, :func:`jobs` and :func:`flags`, as 16 hex digits
    (:func:`neurodb.core.compute.fingerprint`): in the keys of the stored figures, and in the address of
    each part of a page whose parts read one computation (``as_of``,
    :func:`neurodb.core.compute.once_as_of`)."""
    return compute.per_request("etools-fingerprint", lambda: compute.fingerprint(f"{jobs()}|{flags()}"))


def gazetteer() -> str:
    """The gazetteer as staff may have left it in the admin: how many locations and location types, and
    their last change (``modified`` is set on every save). A location deleted, renamed, moved or given
    coordinates, or an admin level changed, gives a new text. Read once per request."""

    def read() -> str:
        from neurodb.geo.models import Location, LocationType

        places = Location.objects.aggregate(n=Count("pk"), last=Max("modified"))
        kinds = LocationType.objects.aggregate(n=Count("pk"), last=Max("modified"))
        return ";".join(
            f"{found['n']}.{found['last'].timestamp() if found['last'] else '-'}" for found in (places, kinds)
        )

    return compute.per_request("etools-gazetteer", read)
