"""Raw eTools data (Admin → Data and sync): the pages of :mod:`neurodb.datamart.raw`, for the people who check
the figures (``roles.can_validate``: superusers and the Validators group; the admin itself asks for staff
status). Anyone else gets a 404.

* ``datamart/raw/``: every source, with what its last sync received and kept (``runfacts``), by page;
* ``datamart/raw/<source>/``: one source: its sync's figures, the fields it sends, its records (filtered,
  50 a page) and the CSV of the filter;
* ``datamart/raw/<source>/<pk>/``: one record, as received and as NeuroDB read it;
* ``datamart/raw/<source>/rows.csv``: the CSV of the filter (streamed, capped, two at once on the site);
* ``datamart/raw/<source>/fields/`` (POST, superusers): a field reviewed, or hidden again.

Every record goes through the people rule (``raw_people``). Read-only, but for the reviews.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

from django.contrib import admin, messages
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from neurodb.accounts.roles import ADMIN, can_validate, role_of
from neurodb.core.models import SyncRun

from . import catalogue, raw, raw_people, runfacts
from . import models as dm

CONTENT_HASH = re.compile(r"[0-9a-f]{64}")


def _guard(request) -> None:
    if not can_validate(request.user):
        raise Http404


def _source(name: str) -> str:
    if name not in raw.sources():
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
        "opts": dm.DatamartDocument._meta,
        **extra,
    }


def _status_word(run: SyncRun | None) -> str:
    return run.get_status_display() if run is not None else ""


def _not_kept(facts: runfacts.RunFacts) -> str:
    return (
        f"{facts.other_country:,} of another country; {facts.unnamed:,} named no country; "
        f"{facts.not_saved:,} could not be saved"
    )


def _row(request, name: str, facts: runfacts.RunFacts) -> dict[str, Any]:
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
        "run_url": reverse("admin:core_syncrun_change", args=[run.pk])
        if run is not None and _sees_runs(request.user)
        else "",
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
        rest=[{"name": k, "label": label, "path": path} for k, (label, path) in raw.REST_ENTITIES.items()],
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
    facts = runfacts.facts(name)
    run = facts.run
    filters = raw.Filters.from_params(request.GET)
    rows = filters.apply(name, raw.base(name), run)
    n = raw.counted(name, rows)
    names = people.known_names()
    counted, left_out = raw.field_counts(name, names)
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
        run_url=reverse("admin:core_syncrun_change", args=[run.pk])
        if run and _sees_runs(request.user)
        else "",
        newer_url=reverse("admin:core_syncrun_change", args=[facts.newer_failed.pk])
        if facts.newer_failed is not None and _sees_runs(request.user)
        else "",
        filters=filters,
        shown=raw.filters_shown(name),
        choices=raw.choices(name, run),
        pd_links=raw.PD_LINK_WORDS,
        updated_choices=raw.UPDATED,
        n=n,
        too_slow=raw.TOO_SLOW.format(n=f"{facts.held:,}") if n is None else "",
        too_many=raw.too_many(name, n) if n is not None and n > limit else "",
        csv_url=csv_url,
        columns=[label for label, _what in columns],
        records=records,
        page_obj=page_obj,
        page_query=urlencode(query),
        counted=counted,
        left_out=left_out,
        unsent=raw.unsent(name, counted),
        fields_counted=raw.FIELDS_COUNTED,
        superuser=request.user.is_superuser,
        by_hand=raw.reviewed_by_hand(name),
        reviewable=name not in catalogue.SAFEGUARDING,
        field_url=reverse("admin:datamart_raw_field", args=[name]),
        developer_url=_developer_url(name),
        checks_url=reverse("admin:datamart_reporting_checks"),
        typed=raw.typed(name),
        **extra,
    )
    return render(request, "admin/datamart/raw/source.html", context)


def _developer_url(name: str) -> str:
    """The developer table of a source (superusers only), where a field's values are read before a review."""
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


def _report_status(row: Any) -> dict[str, Any]:
    """A partner report row's progress report and indicator report ids, and its progress report's status as
    every page reads it (``reporting.progress_reports``: the word of the report's rows changed last)."""
    from . import reached as rc
    from . import reporting
    from .services import reporting_today

    found = reporting.progress_reports(dm.ReportedIndicator.objects.filter(pk=row.pk), reporting_today())
    values = {f: getattr(row, f) for f in rc.ROW_FIELDS if hasattr(row, f)}
    report = found[0] if found else {}
    return {
        "report_key": rc.report_key(values),
        "indicator_report_key": rc.indicator_report_key(values),
        "status": report.get("status_label", ""),
        "word": report.get("report_status", ""),
    }


def _written_by(name: str, row: Any, run: SyncRun | None) -> dict[str, Any]:
    """The sync that last updated a record: the last one, or an earlier one (its date)."""
    if run is not None and raw.updated_by_last(row, run):
        return {"last": True, "when": run.finished_at}
    earlier = (
        SyncRun.objects.filter(job=SyncRun.Job.ETOOLS_DATAMART, target=name, started_at__lte=row.synced_at)
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
    location = getattr(row, "location", None) if raw.has(name, "location") else None
    if location is not None and not hasattr(location, "p_code"):
        location = None
    made = _typed_fields(name, row, names, own) if raw.typed(name) else []
    if not raw.typed(name):
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
        location=location,
        report=_report_status(row) if name == "partner_reports" else None,
        written=_written_by(name, row, run),
        content_hash=not raw.typed(name) and bool(CONTENT_HASH.fullmatch(row.record_key or "")),
        field_count=len(data),
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
        counted, _left_out = raw.field_counts(name, names)
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
