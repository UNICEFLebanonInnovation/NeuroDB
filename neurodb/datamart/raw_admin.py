"""Raw eTools data (Admin → Data and sync): the pages of :mod:`neurodb.datamart.raw`, for the people who check
the figures (``roles.can_validate``: superusers and the Validators group; the admin itself asks for staff
status). Anyone else gets a 404.

* ``datamart/raw/``: every source, with what its last sync received and kept (``runfacts``), by page;
* ``datamart/raw/<source>/``: one source: its sync's figures, the fields it sends, its records (filtered,
  50 a page) and the CSV of the filter;
* ``datamart/raw/<source>/<pk>/``: one record, as received and as NeuroDB read it;
* ``datamart/raw/<source>/rows.csv``: the CSV of the filter (streamed, capped, two at once on the site);
* ``datamart/raw/<source>/fields/`` (POST, superusers): a field reviewed, or hidden again;
* ``datamart/raw/rest/read/`` (POST, superusers and Administrators): "Read the eTools REST API now" starts the
  job that reads the eTools REST API into Raw eTools data (``integrations.etools.rest_raw``), in the
  background; without the eTools token the page says so and shows no button.

Every record goes through the people rule (``raw_people``). Read-only, but for the reviews and the button.
Where a sync or a read failed, the pages say why in a few words (``runfacts.failure_words``) and link the run
for those who open runs; an eTools REST entity with no read of its own says why (being read now, waiting for
the read under way, not reached by a read that stopped, not read because its list could not be read...).
Printed or saved as PDF, the three pages print whole on A4 landscape (admin.css, ``nd-raw``).
"""

from __future__ import annotations

import datetime
import json
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import formats, timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from neurodb.accounts.roles import ADMIN, can_validate, role_of
from neurodb.core.models import SyncRun

from . import catalogue, raw, raw_people, runfacts
from . import models as dm

CONTENT_HASH = re.compile(r"[0-9a-f]{64}")
STARTING_S = 120  # how long a read started counts as starting while its process has written no run yet


def _guard(request) -> None:
    if not can_validate(request.user):
        raise Http404


def _source(name: str) -> str:
    if not raw.known(name):
        raise Http404
    return name


def _sees_runs(user) -> bool:
    """Superusers and Administrators open the sync runs (Import and sync runs)."""
    return bool(user.is_superuser or role_of(user) == ADMIN)


def _context(request, title: str, **extra: Any) -> dict[str, Any]:
    return {
        **admin.site.each_context(request),
        "title": title,
        "subtitle": None,
        "index_url": reverse("admin:datamart_raw"),
        # no "opts": the header is the page's title, never a link to the developer tables
        **extra,
    }


def _status_word(run: SyncRun | None) -> str:
    return run.get_status_display() if run is not None else ""


# ------------------------------------------------------------------------------------------- the notes
def _when(moment: datetime.datetime) -> str:
    """A moment as a note says it (24 Oct 2026, 20:05), its words joined by non-breaking spaces: a printed
    column breaks between words, never inside a date."""
    return formats.date_format(timezone.localtime(moment), "j M Y, H:i").replace(" ", "\u00a0")


def _run_url(request, run: SyncRun | None) -> str:
    """The run's page, for those who open runs ("" for the others: the note is plain text)."""
    if run is None or not _sees_runs(request.user):
        return ""
    return reverse("admin:core_syncrun_change", args=[run.pk])


def _note(request, text: str, run: SyncRun | None = None, *, cause: str = "", warn: bool = True) -> dict:
    return {"text": text, "cause": cause, "url": _run_url(request, run), "warn": warn}


def _run_words(run: SyncRun) -> str:
    """A run by its job and start: "the sync of …", "the read of …" (into Raw eTools data), "the eTools REST
    sync of …"."""
    if run.job == SyncRun.Job.ETOOLS_REST_RAW:
        return f"the read of {_when(run.started_at)}"
    if run.job == SyncRun.Job.ETOOLS:
        return f"the eTools REST sync of {_when(run.started_at)}"
    return f"the sync of {_when(run.started_at)}"


def _failed_note(request, name: str, run: SyncRun) -> dict:
    """A run that failed: when, why in a few words (``runfacts.failure_words``), linked for those who open
    runs; an eTools REST entity whose list could not be read, "not read on …". A diagnostic dataset (no page
    or check uses it) is no alarm."""
    if runfacts.not_read(run):
        text = f"not read on {_when(run.started_at)}"
    else:
        text = f"{_run_words(run)} failed"
    return _note(request, text, run, cause=runfacts.failure_words(run), warn=name not in catalogue.DIAGNOSTIC)


def _datamart_notes(request, name: str, facts: runfacts.RunFacts) -> list[dict]:
    """What a Datamart dataset's row says beside its last sync: a newer sync that failed, and why."""
    if facts.newer_failed is None:
        return []
    notes = [_failed_note(request, name, facts.newer_failed)]
    if name in catalogue.DIAGNOSTIC:
        notes.append(_note(request, _("diagnostic: no page or check uses it"), warn=False))
    return notes


@dataclass(frozen=True)
class _Read:
    """The latest read of the eTools REST API into Raw eTools data, from its runs' details
    (``rest_raw.READ_STARTED``, ``READ_OF``): when it started, what it reads, its latest run, its run under
    way."""

    started: datetime.datetime
    of: frozenset[str]
    last: SyncRun
    running: SyncRun | None


def _rest_running() -> SyncRun | None:
    """The run of the read under way, if any. Called before the runs are read: a run a stopped process left
    running is closed there (``background.is_running``), so the page says it was cut off at once."""
    from neurodb.integrations import background

    job = SyncRun.Job.ETOOLS_REST_RAW
    if not background.is_running(job):
        return None
    return (
        SyncRun.objects.filter(job=job, status=SyncRun.Status.RUNNING).order_by("-started_at", "-pk").first()
    )


def _latest_read(running: SyncRun | None) -> _Read | None:
    """The latest read (None before any, or for a read of an earlier release, whose runs do not say)."""
    from neurodb.integrations.etools import rest_raw

    last = running or (
        SyncRun.objects.filter(job=SyncRun.Job.ETOOLS_REST_RAW).order_by("-started_at", "-pk").first()
    )
    details = last.details if last is not None and isinstance(last.details, dict) else {}
    of = details.get(rest_raw.READ_OF)
    try:
        started = datetime.datetime.fromisoformat(str(details.get(rest_raw.READ_STARTED)))
    except ValueError:
        return None
    if last is None or not isinstance(of, list):
        return None
    return _Read(started, frozenset(str(entity) for entity in of), last, running)


def _rest_notes(
    request, name: str, facts: runfacts.RunFacts, last: SyncRun | None, read: _Read | None
) -> list[dict]:
    """What an eTools REST entity's row says beside its last read, or, never read, instead of it: being read
    now, waiting for the read under way, not reached by the latest read (it stopped, or failed before any
    source), a newer read that failed (and why) or that received nothing. None at all: "Not read yet."."""
    from neurodb.integrations.background import CUT_OFF

    notes = []
    reached = last is not None and read is not None and last.started_at >= read.started
    if read is not None and name in read.of and not reached:
        if read.running is not None and read.running.target == name:
            notes.append(
                _note(request, f"being read now (since {_when(read.running.started_at)})", warn=False)
            )
        elif read.running is not None:
            notes.append(_note(request, "waiting: the read under way reaches it later", warn=False))
        elif not read.last.target and read.last.status == SyncRun.Status.FAILED:
            notes.append(_failed_note(request, name, read.last))  # the read failed at its start (no token)
        else:
            cut = read.last.status == SyncRun.Status.FAILED and read.last.error == CUT_OFF
            notes.append(
                _note(
                    request,
                    f"the read of {_when(read.started)} stopped before it reached it",
                    read.last,
                    cause=runfacts.failure_words(read.last) if cut else "",
                )
            )
    if facts.newer_failed is not None:
        notes.append(_failed_note(request, name, facts.newer_failed))
    if facts.newer_empty is not None:
        notes.append(
            _note(request, f"{_run_words(facts.newer_empty)}: {facts.empty_words}", facts.newer_empty)
        )
    return notes


def _not_kept(facts: runfacts.RunFacts) -> str:
    if raw.rest(facts.source):  # the eTools REST API sends this country's records only
        return f"{facts.not_saved:,} could not be saved"
    words = (
        f"{facts.other_country:,} of another country; {facts.unnamed:,} named no country; "
        f"{facts.not_saved:,} could not be saved"
    )
    if facts.copies_proven:  # the Datamart's own copies: NeuroDB keeps one of each
        words += f"; {facts.copies_proven:,} the Datamart sent twice (identical)"
    if facts.twice_unproven:
        words += f"; {facts.twice_unproven:,} came twice (counted once)"
    if facts.not_written:
        words += f"; {facts.not_written:,} could not be saved into NeuroDB's {facts.written_into}"
    return words


def _row(request, name: str, facts: runfacts.RunFacts, notes: list[dict] | None = None) -> dict[str, Any]:
    run = facts.run
    return {
        "name": name,
        "label": raw.NAMES.get(name, name),
        "path": raw.path_of(name),
        "url": reverse("admin:datamart_raw_source", args=[name]),
        "used_by": [raw.PAGES[p] for p in raw.USED_BY.get(name, ())],
        "facts": facts,
        "not_kept": _not_kept(facts),
        "status": _status_word(run),
        "job": run.get_job_display() if run is not None and raw.rest(name) else "",
        "run_url": _run_url(request, run),
        "notes": _datamart_notes(request, name, facts) if notes is None else notes,
        "diagnostic": name in catalogue.DIAGNOSTIC,
    }


def _rest_reading(request) -> dict[str, Any]:
    """The eTools REST API part of the first page: its entities (what the last read that kept each copy
    received and kept), and "Read the eTools REST API now" for superusers and Administrators, or why not."""
    from neurodb.integrations import background
    from neurodb.integrations.etools import rest_raw

    job = SyncRun.Job.ETOOLS_REST_RAW
    running = _rest_running()  # first: a run a stopped process left running is closed before they are read
    names = list(raw.REST_ENTITIES)
    reads = runfacts.rest_reads(names)
    counts = raw.rest_counts({name: (found.run, found.newer_failed) for name, found in reads.items()})
    read = _latest_read(running)
    rows = []
    for name, found in reads.items():
        facts = runfacts.build(name, found.run, found.newer_failed, tuple(counts[name]), last=found.last)
        rows.append(_row(request, name, facts, _rest_notes(request, name, facts, found.last, read)))
    asked = background.started_at(rest_raw.COMMAND)
    not_started = ""
    if asked is not None:
        pressed = datetime.datetime.fromtimestamp(asked, datetime.UTC)
        if running or SyncRun.objects.filter(job=job, started_at__gte=pressed).exists():
            background.forget_started(rest_raw.COMMAND)  # its run is there (or ended): the page follows it
            asked = None
        elif time.time() - asked > STARTING_S:  # still no run: its process did not start, or stopped at once
            not_started = _when(pressed)  # said until the note expires (background.STARTED_TTL)
            asked = None
    return {
        "rest": rows,
        "rest_token": rest_raw.token_set(),
        "rest_can_read": _sees_runs(request.user),
        "rest_read_url": reverse("admin:datamart_raw_rest_read"),
        "rest_running": running,
        "rest_since": read.started if read is not None and running is not None else None,
        "rest_starting": running is None and asked is not None,
        "rest_not_started": not_started,
    }


# ---------------------------------------------------------------------------------------------- every source
def index_view(request):
    """Every source NeuroDB reads, in four tables (what Progress reports and Partner monitoring read, the
    other
    Datamart datasets, the eTools REST API, what NeuroDB does not read), and by page."""
    _guard(request)
    names = list(raw.sources())
    page = request.GET.get("page") or ""
    if page and page not in raw.PAGE_SOURCES:
        page = ""
    runs = runfacts.runs(names)
    counts = raw.held_counts(runs)
    facts = {
        name: runfacts.build(name, run, newer, tuple(counts.get(name) or (0, 0, 0)))
        for name, (run, newer) in runs.items()
    }
    shown = set(names)
    if page:
        wanted = raw.PAGE_SOURCES[page]
        shown = set(names) if wanted == (raw.EVERY_DATASET,) else set(wanted)
    tables = [
        {
            "title": _("Read by Progress reports and Partner monitoring"),
            "rows": [_row(request, n, facts[n]) for n in raw.PRP_SOURCES if n in shown],
        },
        {
            "title": _("Other eTools Datamart datasets"),
            "rows": [_row(request, n, facts[n]) for n in raw.other_sources() if n in shown],
        },
    ]
    by_page = [
        {
            "key": key,
            "label": label,
            "every": raw.PAGE_SOURCES[key] == (raw.EVERY_DATASET,),
            "sources": [
                {
                    "name": n,
                    "label": raw.NAMES.get(n, n),
                    "url": reverse("admin:datamart_raw_source", args=[n]),
                }
                for n in raw.PAGE_SOURCES[key]
                if n != raw.EVERY_DATASET
            ],
            "url": f"{reverse('admin:datamart_raw')}?{urlencode({'page': key})}",
        }
        for key, label in raw.PAGES.items()
    ]
    context = _context(
        request,
        _("Raw eTools data"),
        tables=tables,
        **(_rest_reading(request) if not page else {}),
        excluded=sorted(catalogue.EXCLUDED.items()),
        by_page=by_page,
        page=page,
        page_label=raw.PAGES.get(page, ""),
        checks_url=reverse("admin:datamart_reporting_checks"),
    )
    return render(request, "admin/datamart/raw/index.html", context)


# ------------------------------------------------------------------------------------------------- one source
def _check_ten() -> dict[str, Any] | None:
    """Check 10's two counts of the stored result: running PDs with no partner report row, and of them those
    nothing explains."""
    from . import checks

    stored = checks.latest_run()
    if stored is None:
        return None
    card = next((c for c in stored.details.get("checks") or [] if str(c.get("key")) == "10"), None)
    if card is None:
        return None
    found: dict[str, Any] = {}
    for label, value in card.get("figures") or []:
        if str(label).startswith("… with no partner report row"):
            found["none"] = value
        elif str(label).startswith("… of which nothing explains"):
            found["unexplained"] = value
    return found if "none" in found else None


def _history_from() -> Any:
    from . import reached as rc

    return rc.history_from().date


def source_view(request, source: str):
    """One source: what its last sync received and kept, the fields it sends, its records (the filter, 50 a
    page, newest first) and the CSV of the filter."""
    from neurodb.watch import people

    _guard(request)
    name = _source(source)
    if raw.rest(name):
        running = _rest_running()  # first: a run a stopped process left running is closed before it is read
        found = runfacts.rest_reads([name])[name]
        counts = runfacts.counts(name, found.run)
        facts = runfacts.build(name, found.run, found.newer_failed, counts, last=found.last)
        notes = _rest_notes(request, name, facts, found.last, _latest_read(running))
    else:
        facts = runfacts.facts(name)
        notes = _datamart_notes(request, name, facts)
    run = facts.run
    filters = raw.Filters.from_params(request.GET)
    rows = filters.apply(name, raw.base(name), run)
    n = raw.counted(name, rows)
    names = people.known_names()
    counted, left_out = raw.field_counts(name, names, run)
    kept = raw.keys_kept(run, name)
    columns = raw.columns_of(name, counted)
    reviewed = raw.reviewed_keys(name)
    records: list[dict[str, Any]] = []
    page_obj = None
    if n is not None:
        paginator = Paginator(range(n), raw.PAGE_SIZE)
        page_obj = paginator.get_page(request.GET.get("p"))
        start = (page_obj.number - 1) * raw.PAGE_SIZE
        found = list(raw.related(name, rows).order_by("-pk")[start : start + raw.PAGE_SIZE])
        records = [
            {
                "url": reverse("admin:datamart_raw_record", args=[name, row.pk]),
                "cells": raw.cells(name, row, columns, names, reviewed, run),
            }
            for row in found
        ]
    query = filters.query()
    limit = raw.csv_limit(name)
    csv_url = f"{reverse('admin:datamart_raw_csv', args=[name])}?{urlencode(query)}"
    extra: dict[str, Any] = {}
    if name == "partner_reports":
        extra = {"history_from": _history_from(), "check_ten": _check_ten()}
    context = _context(
        request,
        raw.NAMES.get(name, name),
        name=name,
        path=raw.path_of(name),
        facts=facts,
        not_kept=_not_kept(facts),
        status=_status_word(run),
        run_url=_run_url(request, run),
        notes=notes,
        diagnostic=name in catalogue.DIAGNOSTIC,
        filters=filters,
        shown=raw.filters_shown(name),
        choices=raw.choices(name, run),
        pd_links=raw.PD_LINK_WORDS,
        updated_choices=raw.UPDATED,
        n=n,
        too_slow=raw.TOO_SLOW.format(n=f"{facts.held:,}") if n is None else "",
        too_many=raw.too_many(name, n) if n is not None and n > limit else "",
        csv_url=csv_url,
        columns=[_heading(label, what) for label, what in columns],
        records=records,
        page_obj=page_obj,
        page_query=urlencode(query),
        counted=counted,
        left_out=left_out,
        unsent=raw.unsent(name, counted),
        fields_counted=raw.FIELDS_COUNTED,
        fields_by_sync=bool(kept),
        other_fields=(kept.get(raw.OTHER_FIELDS) or [0])[0] if kept else 0,
        more_fields=bool(kept.get(raw.MORE_FIELDS)) if kept else False,
        superuser=request.user.is_superuser,
        by_hand=raw.reviewed_by_hand(name),
        reviewable=name not in catalogue.SAFEGUARDING,
        field_url=reverse("admin:datamart_raw_field", args=[name]),
        developer_url=_developer_url(name),
        checks_url=reverse("admin:datamart_reporting_checks"),
        typed=raw.typed(name),
        rest=raw.rest(name),
        **extra,
    )
    return render(request, "admin/datamart/raw/source.html", context)


def _heading(label: str, what: str) -> str:
    """A list column's heading. A field named by its key (``d:<key>``, ``raw.columns_of``) may break after
    each "_" when printed: financial_contribution_currency is one long word, and held the list past the
    sheet. A heading in words keeps its words whole."""
    if what != f"d:{label}":
        return label
    return mark_safe(escape(label).replace("_", "_<wbr>"))  # escape() writes no "_"


def _developer_url(name: str) -> str:
    """The developer table of a source (superusers only), where a field's values are read before a review
    ("" for an eTools REST entity: its copy has no developer table)."""
    if raw.rest(name):
        return ""
    if raw.typed(name):
        model = raw.model_of(name)._meta
        return reverse(f"admin:{model.app_label}_{model.model_name}_changelist") + "?"
    return (
        reverse("admin:datamart_datamartdocument_changelist")
        + "?"
        + urlencode({"dataset__exact": name})
        + "&"
    )


# ------------------------------------------------------------------------------------------------- one record
def _pretty(value: Any) -> tuple[str, bool]:
    """A value as the record page shows it, and whether it is nested (shown in a block)."""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str), True
    if value is None:
        return "null", False
    return str(value), False


def _typed_fields(name: str, row: Any, names: frozenset[str], own: frozenset[str]) -> list[dict[str, Any]]:
    """NeuroDB's own columns of a typed record, through the pinned lists (``raw_people``)."""
    out = []
    for f in raw.model_of(name)._meta.concrete_fields:
        if f.name in ("id", "data", "synced_at") or f.is_relation:
            continue
        if (name, f.name) in raw_people.NEVER_COLUMNS:
            continue
        value = raw_people.column(name, f.name, getattr(row, f.attname), names, own)
        label = f"{f.name} (people)" if (name, f.name) in raw_people.PERSON_COLUMNS else f.name
        text, nested = _pretty(value)
        out.append({"label": label, "value": text, "nested": nested})
    return out


def _report_status(row: Any, names: frozenset[str], own: frozenset[str]) -> dict[str, Any]:
    """A partner report row's progress report and indicator report ids (``raw.report_keys``), and its progress
    report's status as every page reads it (``reporting.progress_reports``: the word of the report's rows
    changed last)."""
    from . import reporting
    from .services import reporting_today

    found = reporting.progress_reports(dm.ReportedIndicator.objects.filter(pk=row.pk), reporting_today())
    keys = raw.report_keys(row, names, own)
    report = found[0] if found else {}
    return {
        "report_key": keys["progress_report_key"],
        "indicator_report_key": keys["indicator_report_key"],
        "status": report.get("status_label", ""),
        "word": raw_people.text(
            report.get("report_status", ""), "partner_reports", "report_status", names, own
        ),
    }


def _written_by(name: str, row: Any, run: SyncRun | None) -> dict[str, Any]:
    """The sync that last updated a record: the last one, or an earlier one (its date)."""
    from django.db.models import Q

    if run is not None and raw.updated_by_last(row, run):
        return {"last": True, "when": run.finished_at}
    jobs = Q(job=SyncRun.Job.ETOOLS_DATAMART, target=name)
    if raw.rest(name):  # the read into Raw eTools data, or the eTools REST sync
        jobs = Q()
        for job, target in runfacts.rest_jobs(name):
            jobs |= Q(job=job, target=target)
    earlier = (
        SyncRun.objects.filter(jobs, started_at__lte=row.synced_at)
        .order_by("-started_at", "-pk")
        .values_list("started_at", flat=True)
        .first()
    )
    return {"last": False, "when": earlier or row.synced_at}


def record_view(request, source: str, pk: int):
    """One record: its fields as received (through the people rule), what NeuroDB made of it, and the sync
    that last updated it."""
    from neurodb.watch import people

    _guard(request)
    name = _source(source)
    row = get_object_or_404(raw.related(name, raw.base(name)), pk=pk)
    if raw.rest(name):  # the programme document or partner NeuroDB holds under its eTools ids
        raw.link_rest(name, [row])
    run = runfacts.runs([name])[name][0]
    names = people.known_names()
    data = catalogue.scrub(row.data if isinstance(row.data, dict) else {})
    shown, withheld = raw_people.public_record(data, name, names, reviewed=raw.reviewed_keys(name))
    own = raw.dmq._own_names(data)
    fields = []
    for key in data:
        if key in shown:
            text, nested = _pretty(shown[key])
            fields.append({"key": key, "value": text, "nested": nested})
        elif key in withheld:
            fields.append({"key": raw.header_name(key), "value": withheld[key].words, "withheld": True})
    inside = [{"key": k, "words": w.words} for k, w in withheld.items() if w.inside]
    special = [
        withheld[k].words for k in (raw_people.FIELDS_NAMING, raw_people.WITHHELD_KEYS) if k in withheld
    ]
    as_counts = [
        f"{k} ({w.count:,})" for k, w in withheld.items() if w.kind == raw_people.PEOPLE and not w.inside
    ]
    pd = getattr(row, "intervention", None)
    partner = raw.partner_of(row)
    pd_line, partner_line = raw.rest_linked(name) if raw.rest(name) else (True, True)
    location = getattr(row, "location", None) if raw.has(name, "location") else None
    if location is not None and not hasattr(location, "p_code"):
        location = None
    made = _typed_fields(name, row, names, own) if raw.typed(name) else []
    if raw.rest(name):
        made = [
            {
                "label": "reference",
                "value": raw_people.column(name, "reference", row.reference, names, own) or "—",
            },
            {"label": "title", "value": raw_people.column(name, "title", row.title, names, own) or "—"},
            {"label": "source_id", "value": row.source_id or "—"},
            {"label": "parent_key", "value": row.parent_key or "—"},
        ]
    elif not raw.typed(name):
        title = raw_people.column(name, "title", row.title, names, own)
        made = [
            {"label": "title", "value": title or "—"},
            {"label": "date", "value": row.date or "—"},
            {"label": "source_id", "value": row.source_id if row.source_id is not None else "—"},
        ]
    context = _context(
        request,
        raw.NAMES.get(name, name),
        name=name,
        source_url=reverse("admin:datamart_raw_source", args=[name]),
        record_id=raw.record_id(name, row),
        fields=fields,
        inside=inside,
        special=special,
        as_counts=as_counts,
        made=made,
        pd=pd,
        pd_url=reverse("reports:programme_detail", args=[pd.pk]) if pd is not None else "",
        pd_words=raw.pd_words(row),
        partner=partner,
        pd_line=pd_line,
        partner_line=partner_line,
        location=location,
        report=_report_status(row, names, own) if name == "partner_reports" else None,
        written=_written_by(name, row, run),
        content_hash=not raw.typed(name) and bool(CONTENT_HASH.fullmatch(row.record_key or "")),
        field_count=len(data),
        rest=raw.rest(name),
    )
    return render(request, "admin/datamart/raw/record.html", context)


# --------------------------------------------------------------------------------------------- the download
def _plain(text: str, status: int) -> HttpResponse:
    return HttpResponse(text, status=status, content_type="text/plain; charset=utf-8")


def csv_view(request, source: str):
    """The CSV of a source's filter: refused above its limit (400), when the count takes too long (400), or
    when two large downloads are being prepared already (503); otherwise streamed."""
    from neurodb.reports import exports
    from neurodb.watch import people

    _guard(request)
    name = _source(source)
    filters = raw.Filters.from_params(request.GET)
    if filters.refused:
        return _plain(filters.refused, 400)
    run = runfacts.runs([name])[name][0]
    rows = filters.apply(name, raw.base(name), run)
    n = raw.counted(name, rows)
    if n is None:
        return _plain(raw.TOO_SLOW.format(n=f"{raw.base(name).count():,}"), 400)
    if n > raw.csv_limit(name):
        return _plain(raw.too_many(name, n), 400)
    slot = raw.take_slot()
    if slot is None:
        return _plain(raw.BUSY, 503)
    try:
        names = people.known_names()
        counted, _left_out = raw.field_counts(name, names, run)
        keys = raw.header_keys(name, rows, counted)
        columns = raw.csv_columns(name, keys)
    except Exception:
        slot.free()
        raise

    def stream():
        try:
            yield from raw.csv_rows(name, rows, keys, run, names)
        finally:
            slot.free()

    response = exports.stream_csv(raw.filename(name, run), columns, stream())
    response._resource_closers.append(slot.free)  # a response closed before it was read frees it too
    return response


# ----------------------------------------------------------------------------------------------- the reviews
@require_POST
def field_view(request, source: str):
    """A superuser reviews a field of a source ("Reviewed: show it"), or hides it again: its values are then
    shown, through the people rule, or show only numbers, yes/no and dates again. Never a field whose name
    names a person, nor one of a PSEA answer outside the safeguarding fields."""
    from neurodb.watch import people

    _guard(request)
    name = _source(source)
    if not request.user.is_superuser:
        raise Http404
    key = str(request.POST.get("key") or "")
    back = reverse("admin:datamart_raw_source", args=[name]) + "#fields"
    rules = raw.dmq.Rules(people.known_names(), name)
    if (
        not raw_people.identifier(key, rules.fm)
        or raw.dmq._key_withheld(key, rules, frozenset())
        or name in catalogue.SAFEGUARDING
    ):
        messages.error(request, _("This field cannot be reviewed."))
        return redirect(back)
    if request.POST.get("action") == "hide":
        dm.RawFieldReview.objects.filter(source=name, key=key).delete()
        messages.success(
            request, _("%(key)s is hidden again: it shows only numbers, yes/no and dates.") % {"key": key}
        )
    else:
        dm.RawFieldReview.objects.get_or_create(
            source=name, key=key, defaults={"reviewed_by": request.user.get_username()}
        )
        messages.success(
            request, _("%(key)s is reviewed: its values are shown, without people.") % {"key": key}
        )
    return redirect(back)


# ------------------------------------------------------------------------------------------ the REST API read
@require_POST
def rest_read_view(request):
    """ "Read the eTools REST API now": the job that reads the eTools REST API into Raw eTools data, started
    in the background (superusers and Administrators who open Raw eTools data; anyone else may not). Refused
    without the eTools token, and while a read is running."""
    from neurodb.integrations import background
    from neurodb.integrations.etools import rest_raw

    _guard(request)
    if not _sees_runs(request.user):
        raise PermissionDenied
    back = reverse("admin:datamart_raw") + "#rest"
    if not rest_raw.token_set():
        messages.error(request, rest_raw.NO_TOKEN)
    elif background.is_running(SyncRun.Job.ETOOLS_REST_RAW):
        messages.warning(request, _("The eTools REST API is already being read into Raw eTools data."))
    else:
        background.start_command(rest_raw.COMMAND, "--triggered-by", request.user.get_username())
        messages.success(
            request,
            _(
                "The eTools REST API is being read into Raw eTools data, in the background. Reload this page "
                "in a few minutes to see what it received."
            ),
        )
    return redirect(back)
