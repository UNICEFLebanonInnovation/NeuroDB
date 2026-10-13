"""Stage A's group: the management brief, the partner profile and data health (SPEC7 §3.5, STAGEA).

**Pages.** The triage of step 2 (the x20 world, on the base commit) converts these three; every other page it
measured (ActivityInfo databases, a database dashboard, the report dashboard and its HPM view, the programme
documents' indicators tab, a programme document's page and modal, field monitoring) stays under 300 ms and
keeps its code, so it is not in this group.

**Users.** The overview group's: an administrator (the first active superuser) and a viewer with a section.
Both may open every page here (data health is open to every signed-in member of staff).

**Matrix** (computed when recording, stored with the recording):

- the brief: first load (the brief has no own-section default: every section, for both users), every
  section, one section (the viewer's), two sections, the previous reporting year (alone and with one
  section), a section with no data;
- the partner profile: the busiest partner (the most programme documents), a partner with Monitoring
  insights visits (the first by id, after the busiest, that a field monitoring visit monitored) and a
  partner without data (the first by id with no programme document, report or visit); the page has no
  filter;
- data health (no filter).

**Context paths.** One function per page (:func:`brief_paths`, :func:`partner_paths`, :func:`health_paths`)
lists the old page's context values, by their names in the old view's ``render`` context; a dotted path
reads inside a dict (``health.jobs``). ``record`` reads each from the old page's one response.

``check`` reads each path where the page in parts keeps it: by default the same name in the frame or in any
part (every holder that has it must agree, otherwise a "consistency" problem), unless the page's entry in
:data:`MOVED` names another place. Where the pages in parts keep them:

=====================================  ==========================================================
the brief's ``data`` (the build)        ``data`` of the ``brief`` and ``sections`` parts (both must
                                        agree), or of the frame when it drew them inline; the
                                        frame's own ``lineage`` and ``dictionary`` (the "Figures as
                                        of" line, the Reference block) must equal the build's
the brief's ``chart_data``              ``chart`` of the ``sections`` part (it gained ``labels``)
the partner's figures                   the ``datamart`` part (``profile``, ``datamart``,
                                        ``activityinfo``, ``chart_data``)
the partner's ``fmm_partner``           the ``fmm`` part, drawn only when a visit ever monitored
                                        the partner (``fmm_seen``): otherwise None, as before
data health's ``health.<block>``        ``<block>`` of the part of that name (jobs, databases, runs,
                                        datasets); ``staleness_hours`` of the frame
``health.year``,                        drawn by neither page (:data:`NOT_DRAWN`), not compared
``health.stale_database_days``
everything else                         the frame, under its own name
=====================================  ==========================================================

JSON: ``brief-chart-data`` became ``brief-sections-data`` (the same dict plus ``labels``, which must be the
status labels), ``chart-data`` became ``partner-datamart-data`` (:data:`JSON_IDS`). Element ids only the
pages in parts have (``part-<name>``, and data health's ``jobs-title``, the jobs part's label) are not
compared. :func:`comparable` brings both sides of an observation to one form before they are compared
(``core.compare`` calls it).

**The world's two gaps** (found by the triage): the x20 world has no eTools Datamart run at all (``seed_demo``
never runs that sync), so data health's datasets table shows "Never" everywhere, and no ActivityInfo partner
link, so the partner page's ActivityInfo block is empty. A second recording covers both, on a world given
them, before ``record`` and before ``check`` alike (after ``migrate``, with the same checkout)::

    python -m scripts.parity.reports_core gaps [--checkout DIR]

It is deterministic (fixed times, every new run finishing before the world's latest run, so the overview's
``latest_sync_run`` does not move) and refuses a world that already has either.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from scripts.parity import core
from scripts.parity import overview as _overview

NO_SUCH_SECTION = "No such section"


# ------------------------------------------------------------------------------------- the context paths
def brief_paths() -> tuple[str, ...]:
    """``/brief/`` (``reports.views.brief``): everything its ``render`` context holds. ``data`` is the brief
    build (``reports.brief.build``: headline, scorecard, pace, confidence, who, partners, money, action,
    lineage, dictionary, scope, text); ``chart_data`` is what ``brief-chart-data`` carries."""
    return (
        "page_title",
        "page_subtitle",
        "breadcrumbs",
        "year",
        "today",
        "options",
        "selected",
        "tabs",
        "keep_query",
        "base_query",
        "overview_query",
        "data",
        "chart_data",
    )


def partner_paths() -> tuple[str, ...]:
    """``/partners/<pk>/`` (``reports.views.partner_profile``): everything its ``render`` context holds.
    ``profile`` is ``partnerships.partner_profile``, ``datamart`` is ``datamart.partner_datamart`` (model
    instances, compared by their fields), ``activityinfo`` is ``partner_facts.partner_activityinfo``,
    ``fmm_partner`` the Monitoring insights panel (None when there is nothing to show), ``chart_data`` what
    ``chart-data`` carries."""
    return (
        "page_title",
        "page_subtitle",
        "breadcrumbs",
        "partner",
        "knowledge_documents",
        "profile",
        "datamart",
        "activityinfo",
        "action_points_url",
        "fmm_partner",
        "labels",
        "chart_data",
    )


def health_paths() -> tuple[str, ...]:
    """``/data/health/`` (``reports.views.data_health``): its ``render`` context, with ``health``
    (``reports.services.data_health``) read key by key, since each block of the page reads one key."""
    return (
        "page_title",
        "page_subtitle",
        "breadcrumbs",
        "health.jobs",
        "health.databases",
        "health.runs",
        "health.datasets",
        "health.staleness_hours",
        "health.stale_database_days",
        "health.year",
        "readiness",
    )


# A page: its name, the address it answers (the path, without the query) and its context paths.
PAGES: dict[str, tuple[re.Pattern[str], Callable[[], tuple[str, ...]]]] = {
    "brief": (re.compile(r"/brief/"), brief_paths),
    "partner": (re.compile(r"/partners/\d+/"), partner_paths),
    "health": (re.compile(r"/data/health/"), health_paths),
}

# Where the page in parts keeps a path that moved, per page: ``{path: (its name there, (part, ...))}``,
# "shell" naming the frame (it also holds the parts it drew inline: their contexts are the response's). A
# path not listed is read under its own name, from the shell or any part; every holder must agree.
MOVED: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    # the build (``data``) is in both parts, read once and stored; the chart JSON is the sections part's
    "brief": {"chart_data": ("chart", ("sections", "shell"))},
    # the figures are the datamart part's; the Monitoring insights panel is the fmm part's, None when the page
    # does not draw that part (:func:`_absent_is_none`)
    "partner": {"fmm_partner": ("fmm_partner", ("fmm", "shell"))},
    # each block of ``health`` is a part of its own, under the same key; the staleness note is the frame's
    "health": {
        "health.jobs": ("jobs", ("jobs",)),
        "health.databases": ("databases", ("databases",)),
        "health.runs": ("runs", ("runs",)),
        "health.datasets": ("datasets", ("datasets",)),
        "health.staleness_hours": ("staleness_hours", ("shell",)),
    },
}
# Paths of the old context that neither page draws (no template reads them): dropped from both sides, with
# the reason. ``services.data_health()`` still returns both for Ask NeuroDB, the daily review and the API.
NOT_DRAWN: dict[str, dict[str, str]] = {
    "health": {
        "health.year": "the databases block's year: each database row (compared) carries its reporting year",
        "health.stale_database_days": "a constant of the databases block's states (compared row by row)",
    },
}
# The old pages' json_script ids, renamed by the rule <page>-<part>-data (the same payload; the brief's
# gained ``labels``, see :func:`comparable`)
JSON_IDS: dict[str, str] = {
    "brief-chart-data": "brief-sections-data",
    "chart-data": "partner-datamart-data",
}
# Element ids only the pages in parts have: the parts' own wrappers, and data health's "Sync jobs" heading,
# which the jobs part names as its label (aria-labelledby)
NEW_IDS = re.compile(r"part-[a-z][a-z0-9-]*|jobs-title")
# Data health's datasets panel: its heading, row count and explanation are the frame's, and the table's Copy and
# CSV buttons come with the table, in its part, right above it (they read the table when pressed, so in the
# frame they did nothing before it was in, or beside the part's error); the old page drew them beside the count
DATASETS_PANEL = "eTools Datamart datasets"
DATASETS_BUTTONS = "Copy CSV"
DATASETS_TABLE_STARTS = "Dataset Last run "  # the table's first column headings
# timing.py times these rows: every row of the matrix
TIMED = (
    "brief: first load",
    "brief: every section",
    "brief: one section",
    "brief: two sections",
    "brief: previous year",
    "brief: previous year, one section",
    "brief: no result",
    "partner: busiest",
    "partner: Monitoring insights visits",
    "partner: without data",
    "data health",
)
# The part whose arrival is a page's first figure (timing.py): the brief's group leader, the partner's one
# part, data health's job cards
FIRST_FIGURES = {"brief": "brief", "partner": "datamart", "health": "jobs"}


def first_figure(url: str) -> str:
    """The part whose arrival is ``url``'s first figure (timing.py)."""
    return FIRST_FIGURES[page_of(url)]


def page_of(url: str) -> str:
    """The name of the page ``url`` opens (a key of :data:`PAGES`)."""
    path = urlsplit(url).path
    for name, (pattern, _paths) in PAGES.items():
        if pattern.fullmatch(path):
            return name
    raise ValueError(f"{url} is not a page of this group")


# ------------------------------------------------------------------------------------- users and matrix
def users() -> dict[str, Any]:
    return _overview.users()


def _partners() -> dict[str, int]:
    """The three partner pages of the matrix, chosen as the triage chose them: the busiest (the most
    programme documents), the first by id after it that a field monitoring visit monitored (its Monitoring
    insights panel shows), and the first by id with no programme document, report or visit."""
    from django.db.models import Count

    from neurodb.datamart import models as dm
    from neurodb.fmm.models import Visit
    from neurodb.partnerships.models import PCA, PartnerOrganization

    ids = list(PartnerOrganization.objects.order_by("pk").values_list("pk", flat=True))
    if not ids:
        raise SystemExit("the world has no partner")
    pds = dict(PCA.objects.values_list("partner_id").annotate(n=Count("pk")).order_by())

    def reports(pk: int) -> int:
        return dm.ReportedIndicator.objects.filter(partner_id=pk).count()

    def visits(pk: int) -> int:
        return Visit.objects.filter(partner_ids__contains=[pk]).count()

    busiest = max(ids, key=lambda pk: (pds.get(pk, 0), -pk))
    seen = next((pk for pk in ids if pk != busiest and visits(pk)), None)
    empty = next((pk for pk in ids if not pds.get(pk) and not reports(pk) and not visits(pk)), None)
    if seen is None or empty is None:
        raise SystemExit("the world needs a partner with Monitoring insights visits and one without data")
    return {"busiest": busiest, "Monitoring insights visits": seen, "without data": empty}


def matrix(people: dict[str, Any]) -> list[tuple[str, str]]:
    """The pages and their filters, as (label, URL). Computed when recording and stored with it."""
    from neurodb.indicators.models import ReportingYear
    from neurodb.reports import services

    year = services.resolve_year(None)
    if year is None:
        raise SystemExit("the world has no current reporting year")
    previous = ReportingYear.objects.exclude(pk=year.pk).filter(name__lt=year.name).order_by("-name").first()
    own = people["viewer"].section.name
    sections = _overview_sections(year)
    other = next(s for s in sections if s != own)

    def brief(*pairs: tuple[str, str]) -> str:
        return "/brief/" + ("?" + urlencode(pairs) if pairs else "")

    rows = [
        ("brief: first load", brief()),
        ("brief: every section", brief(("section", ""))),
        ("brief: one section", brief(("section", own))),
        ("brief: two sections", brief(("section", other), ("section", own))),
    ]
    if previous is not None:
        rows += [
            ("brief: previous year", brief(("year", previous.name))),
            ("brief: previous year, one section", brief(("section", own), ("year", previous.name))),
        ]
    rows.append(("brief: no result", brief(("section", NO_SUCH_SECTION))))
    rows += [(f"partner: {kind}", f"/partners/{pk}/") for kind, pk in _partners().items()]
    rows.append(("data health", "/data/health/"))
    return rows


def _overview_sections(year: Any) -> list[str]:
    from neurodb.reports import overview as service

    return list(service.options(int(str(year.year or year.name)[:4]))["sections"])


# --------------------------------------------------------------------------------------------- the figures
_ABSENT = object()


def _read(context: Any, path: str) -> Any:
    """The value at ``path`` (dotted: a dict key, then keys inside it) in a template context, or ``_ABSENT``."""
    if context is None:
        return _ABSENT
    first, *rest = path.split(".")
    try:
        if first not in context:
            return _ABSENT
        value = context[first]
    except (KeyError, TypeError):
        return _ABSENT
    for key in rest:
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return _ABSENT
    return value


def figures(page: core.Page, mode: str) -> tuple[dict[str, Any], list[str]]:
    """The page's figures under the old page's context paths, and the problems found on the way (a listed
    path the old page does not have; parts that should agree and do not)."""
    name = page_of(page.url)
    paths = PAGES[name][1]()
    shell = getattr(page.shell, "context", None)
    problems: list[str] = []
    out: dict[str, Any] = {}
    if mode == "record":
        for path in paths:
            value = _read(shell, path)
            if value is _ABSENT:
                if page.shell.status_code == 200:
                    problems.append(f"{path}: not in the old page's context")
                value = None
            out[path] = core.plain(value)
        return out, problems

    contexts = {"shell": shell} | {
        part: getattr(response, "context", None)
        for part, response in page.parts.items()
        if response.status_code == 200
    }
    for path in paths:
        if path in NOT_DRAWN.get(name, {}):
            continue
        where, names = MOVED[name].get(path, (path, tuple(contexts)))
        found = [
            (holder, value)
            for holder in names
            if (value := _read(contexts.get(holder), where)) is not _ABSENT
        ]
        if not found:
            out[path] = None
            if page.shell.status_code == 200 and not _absent_is_none(name, path, contexts):
                problems.append(f"{path}: in no part and not in the shell")
            continue
        first = core.plain(found[0][1])
        for holder, value in found[1:]:
            if core.plain(value) != first:
                problems.append(f"{path}: {found[0][0]} and {holder} disagree")
        out[path] = first
    if name == "brief" and page.shell.status_code == 200:
        problems += _brief_frame_agrees(contexts, out.get("data"))
    return out, problems


def _absent_is_none(name: str, path: str, contexts: dict[str, Any]) -> bool:
    """Whether a path the page in parts does not have means None on the old page. Only the partner's
    Monitoring insights panel: the page keeps its place (the ``fmm`` part) only when a visit ever monitored
    the partner (the datamart part's ``fmm_seen``), the one case where the old panel was not None."""
    if (name, path) != ("partner", "fmm_partner"):
        return False
    seen = [
        value
        for holder in ("datamart", "shell")
        if (value := _read(contexts.get(holder), "fmm_seen")) is not _ABSENT
    ]
    return bool(seen) and not any(seen)


def _brief_frame_agrees(contexts: dict[str, Any], data: Any) -> list[str]:
    """The brief's frame draws the "Figures as of" line and the Reference block from its own lineage
    (``brief.lineage()``) and the dictionary, the old page from the build's: they must be the build's."""
    if not isinstance(data, dict):
        return []
    problems = []
    for key in ("lineage", "dictionary"):
        frame = _read(contexts.get("shell"), key)
        if frame is _ABSENT:
            problems.append(f"{key}: not in the brief's frame")
        elif core.plain(frame) != data.get(key):
            problems.append(f"{key}: the brief's frame and its build disagree")
    return problems


def _labels() -> Any:
    """The status labels every chart JSON carries (``reports.views``: ``pd_monitoring_service.LABELS``), as
    JSON. The partner page's ``labels`` figure, compared with the recording, pins the same words."""
    from neurodb.datamart import monitoring

    return json.loads(json.dumps(monitoring.LABELS))


def comparable(observation: dict[str, Any], side: str) -> dict[str, Any]:
    """One observation as it is compared (``side`` is "record" or "check"), with the deliberate changes of
    the pages in parts taken out:

    - the paths neither page draws (:data:`NOT_DRAWN`), on both sides;
    - the brief's chart data (the ``chart_data`` figure and its JSON) gained ``labels`` (each JSON of a part
      carries them, SPEC7_review item 16): on the check side they must be the status labels (otherwise a
      "consistency" problem) and are then left out, since the old ones had none;
    - data health's datasets panel draws the table's Copy and CSV buttons right above the table, in its
      part, no longer beside the row count (:data:`DATASETS_BUTTONS`): on the check side they must be there,
      once (otherwise a "consistency" problem), and are then read where the old page drew them.
    """
    name = page_of(observation["url"])
    drop = NOT_DRAWN.get(name, {})
    out = {**observation, "problems": list(observation["problems"])}
    out["figures"] = {path: value for path, value in observation["figures"].items() if path not in drop}
    panel = observation["blocks"].get(DATASETS_PANEL)
    if side == "check" and name == "health" and observation["status"] == 200 and panel is not None:
        moved = _datasets_buttons_beside_the_count(panel["text"])
        if moved is None:
            out["problems"].append(f"{DATASETS_PANEL}: the Copy and CSV buttons not right above the table")
        else:
            out["blocks"] = {**observation["blocks"], DATASETS_PANEL: {**panel, "text": moved}}
    if side == "check" and name == "brief" and observation["status"] == 200:
        expected = _labels()
        chart = out["figures"].get("chart_data")
        if isinstance(chart, dict):
            chart = dict(chart)
            if chart.pop("labels", None) != expected:
                out["problems"].append("chart_data: labels missing or not the status labels")
            out["figures"]["chart_data"] = chart
        if "brief-sections-data" in observation["json"]:
            payload = dict(observation["json"]["brief-sections-data"])
            if payload.pop("labels", None) != expected:
                out["problems"].append("brief-sections-data: labels missing or not the status labels")
            out["json"] = {**observation["json"], "brief-sections-data": payload}
    return out


def _datasets_buttons_beside_the_count(text: str) -> str | None:
    """The datasets panel's text with the Copy and CSV buttons read where the old page drew them, beside the
    row count ("52 rows Copy CSV Per dataset: ..."), when the page in parts draws them once, right above the
    table ("52 rows Per dataset: ... Copy CSV Dataset Last run ..."); None otherwise."""
    count, rows, rest = text.partition(" rows ")
    if not rows or text.count(DATASETS_BUTTONS) != 1:
        return None
    explanation, buttons, table = rest.partition(f" {DATASETS_BUTTONS} ")
    if not buttons or not table.startswith(DATASETS_TABLE_STARTS):
        return None
    return f"{count} rows {DATASETS_BUTTONS} {explanation} {table}"


# ------------------------------------------------------------------------------------- the world's gaps
DATAMART_DAYS = 365  # the Datamart sync runs daily, one run per dataset: about 19,000 runs a year
DATAMART_LAST_START = "2026-10-11 20:30:00+03"  # the schedule's "30 20 * * *", the day before the world's day
LINKS_RUN = ("2026-10-12 06:10:00+00", "2026-10-12 06:10:04+00")  # after the world's ActivityInfo import
_DATAMART_HISTORY = """
insert into core_syncrun
    (job, target, status, started_at, finished_at, rows_in, rows_written, rows_failed, error, details,
     triggered_by)
select
    'etools_datamart',
    (%(targets)s::text[])[1 + t],
    case when (d * 52 + t) %% 40 = 7 then 'failed' when (d * 52 + t) %% 25 = 3 then 'partial'
         else 'succeeded' end,
    %(last)s::timestamptz - make_interval(days => d) + make_interval(secs => t * 20),
    %(last)s::timestamptz - make_interval(days => d) + make_interval(secs => t * 20 + 15),
    500,
    case when (d * 52 + t) %% 40 = 7 then 0 else 500 end,
    case when (d * 52 + t) %% 25 = 3 then 5 else 0 end,
    case when (d * 52 + t) %% 40 = 7 then 'Upstream returned 503' else '' end,
    case when (d * 52 + t) %% 40 = 7 then %(errors)s::jsonb when t %% 3 = 0 then %(linked)s::jsonb
         else '{}'::jsonb end,
    'parity history'
from generate_series(0, %(days)s - 1) as d, generate_series(0, cardinality(%(targets)s::text[]) - 1) as t
"""


def fill_gaps() -> dict[str, Any]:
    """Give the parity world (``core.setup`` done) a year of eTools Datamart runs and its ActivityInfo partner
    links, as production has them: one run per dataset a day (1 in 40 failed, with error samples, 1 in 25
    partial, ``not_linked`` counts on a third of them), then the real linker
    (``partnerships.linking.link_activityinfo_partners``) with its run and its rows dated at fixed times."""
    from django.db import connection, transaction

    from neurodb.core.models import SyncRun
    from neurodb.graph.models import RefreshRequest
    from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS
    from neurodb.partnerships import linking
    from neurodb.partnerships.models import PartnerLink

    if SyncRun.objects.filter(job=SyncRun.Job.ETOOLS_DATAMART).exists() or PartnerLink.objects.exists():
        raise SystemExit("this world already has eTools Datamart runs or partner links: restore it first")
    latest = SyncRun.objects.exclude(finished_at=None).order_by("-finished_at", "-id").first()
    errors = {
        "errors": [{"id": n, "error": "Upstream row could not be written: value too long"} for n in range(5)],
        "not_linked": {"partner": 2, "programme_document": 4},
    }
    linked = {"not_linked": {"partner": 1, "programme_document_fm": 3}, "removed": 0, "relinked": 2}
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            _DATAMART_HISTORY,
            {
                "targets": list(ENTITY_SYNCS),
                "days": DATAMART_DAYS,
                "last": DATAMART_LAST_START,
                "errors": json.dumps(errors),
                "linked": json.dumps(linked),
            },
        )
        added = cursor.rowcount
    asked = RefreshRequest.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    run = linking.link_activityinfo_partners(triggered_by="parity")
    if run.status != SyncRun.Status.SUCCEEDED:
        raise SystemExit(f"the partner linker failed: {run.error}")
    # the knowledge hub rebuild its finished run asked for (dated now, and never started here)
    RefreshRequest.objects.filter(pk__gt=asked).delete()
    runs, links = SyncRun._meta.db_table, PartnerLink._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"update {runs} set started_at = %s, finished_at = %s where id = %s",  # noqa: S608 - model names
            [*LINKS_RUN, run.pk],
        )
        cursor.execute(f"update {links} set updated_at = %s", [LINKS_RUN[1]])  # noqa: S608 - model names
        cursor.execute(f"analyze {runs}")
    after = SyncRun.objects.exclude(finished_at=None).order_by("-finished_at", "-id").first()
    if latest is not None and after.pk != latest.pk:
        raise SystemExit(f"the latest finished run moved from {latest.pk} to {after.pk}")
    return {
        "datamart_runs": added,
        "partner_links": PartnerLink.objects.count(),
        "linked": PartnerLink.objects.exclude(partner=None).count(),
        "links_run": run.pk,
        "latest_run": after.pk if after else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.parity.reports_core", description="Stage A's parity group: its world."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    gaps = sub.add_parser("gaps", help="give the parity world Datamart runs and ActivityInfo partner links")
    gaps.add_argument("--checkout", type=Path, default=None, help="the code to run (default: this tree)")
    args = parser.parse_args(argv)
    info = core.setup(args.checkout, contexts=False)
    done = fill_gaps()
    print(f"{info['database']} ({info['checkout']}, {info['commit'][:9]}): {done}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
