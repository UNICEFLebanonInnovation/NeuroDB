"""Home (``/`` once signed in): what needs the person today, what changed since the last working day, and
where their section (or the country) stands this year. The page is ``reports.home_views.home_page``; this
module holds what its parts read besides the Country overview's own build.

**First-load scope, one rule for Home, the Country overview and the daily review** (:func:`default_sections`):
every section for Management members and for people without a section, else the person's own section, as the
partner monitoring page matches it to eTools section names. Every link Home draws to those pages and to the
management brief carries Home's scope written out, so the next page never applies a first-load default of its
own and shows the same figures.

Nothing here is stored: ``tasks``, ``since`` and ``programmes`` are computed per request (small; the country
programme reads its own shared build). Nothing shown names a person: decisions never show their owner, the
review's digest shows findings, and "Your tasks" shows counts only.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from django.db.models import Max
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from neurodb.accounts.roles import ADMIN, SECTION_EDITOR, can_set_up, in_management, role_of
from neurodb.datamart import monitoring

WELLBEING_SECTIONS = ("Education",)  # the sections whose editors follow up Makani wellbeing flags
SINCE_SHOWN = 6  # changes "Since {weekday}" lists
# The first screen at 1440 x 900 shows the five key figures (SPEC_HOME §3.1): measured on the x20 world, an
# Administrator with three For you points did not see them with 3 + 3 rows, so both caps gave way to 2, in the
# order the spec sets ("Needs a decision" first, then the review's rows).
DECISIONS_SHOWN = 2  # rows of "Needs a decision" on the first screen
REVIEW_ROWS = 2  # rows of the daily review's digest
CHANGED_SHOWN = 2  # of the review's "changed" sentences
REVIEW_STALE_DAYS = 2  # an older review is introduced with its date
NAME_MATCH = 3  # letters a name needs before "contained either way" counts (monitoring.default_sections)


# ------------------------------------------------------------------------------------------- scope
def default_sections(user, options: list[str]) -> list[str]:
    """The eTools sections a first load shows (Home, the Country overview, the daily review): every section
    (``[]``) for Management members and for people without a section, else the person's own section as
    :func:`neurodb.datamart.monitoring.default_sections` matches it."""
    if in_management(user):
        return []
    return monitoring.default_sections(user, options)


def own_names(user, options: Callable[[], dict[str, list[str]]]) -> list[str]:
    """The eTools section names of the person's own section ("only {own}"), whatever the first-load rule;
    ``options`` is called only when the person has a section."""
    if getattr(user, "section_id", None) is None:
        return []
    return monitoring.default_sections(user, options()["sections"])


def section_ids(names: list[str], *, own: int | None = None) -> tuple[list[int] | None, bool]:
    """NeuroDB sections (ids) for eTools section names, by
    :func:`neurodb.datamart.monitoring.default_sections`'s rule per name (the same name or code, else
    contained either way from three letters): ``(ids, True)``;
    ``(None, True)`` for every section (no names); ``(None, False)`` when the names match no NeuroDB section,
    so a page says "every section" rather than show the country under a section's name. ``own``: the person's
    own section on a first load, used as it is."""
    if own is not None:
        return [own], True
    wanted = [monitoring.norm(name) for name in names if monitoring.norm(name)]
    if not wanted:
        return None, True
    from neurodb.accounts.models import Section

    rows = [
        (pk, {monitoring.norm(name or ""), monitoring.norm(code or "")} - {""})
        for pk, name, code in Section.objects.values_list("pk", "name", "code")
    ]
    found: list[int] = []
    for name in wanted:
        exact = [pk for pk, keys in rows if name in keys]
        near = exact or [
            pk
            for pk, keys in rows
            if any(len(key) >= NAME_MATCH and (key in name or name in key) for key in keys)
        ]
        found.extend(pk for pk in near if pk not in found)
    return (found, True) if found else (None, False)


# ------------------------------------------------------------------------------------------- since
def since_yesterday(start: datetime.datetime, today: datetime.date) -> bool:
    """Whether the previous working day (``start``) is yesterday: Home then says "yesterday" in the heading
    and in the empty line alike, else the weekday in both."""
    return timezone.localtime(start).date() == today - datetime.timedelta(days=1)


def since_label(start: datetime.datetime, today: datetime.date) -> str:
    """ "Since Wednesday", or "Since yesterday" when the previous working day is yesterday."""
    if since_yesterday(start, today):
        return _("Since yesterday")
    return _("Since %(weekday)s") % {"weekday": date_format(timezone.localtime(start).date(), "l")}


def since(ids: list[int] | None, start: datetime.datetime) -> list[tuple[Any, str]]:
    """The notable changes since ``start`` (:func:`neurodb.core.years.since_working_day`) in the sections
    ``ids`` (None: every section), newest first, at most :data:`SINCE_SHOWN`, each with its sentence."""
    from neurodb.graph import news

    return [(change, news.sentence(change)) for change in news.recent(start, sections=ids)[:SINCE_SHOWN]]


# ------------------------------------------------------------------------------------------- tasks
def review_schedule() -> dict[str, Any]:
    """When the daily review runs: ``{"on": True, "time": "06:00"}`` from the switched-on scheduled job that
    runs it (its next run, in Beirut), ``{"on": False, "time": ""}`` when it is switched off."""
    from neurodb.core.cron import next_after
    from neurodb.core.models import ScheduledJob

    job = (
        ScheduledJob.objects.filter(command="daily_review", enabled=True)
        .order_by("next_run_at", "key")
        .only("schedule", "next_run_at")
        .first()
    )
    if job is None:
        return {"on": False, "time": ""}
    when = job.next_run_at
    if when is None:
        try:
            when = next_after(job.schedule, timezone.now())
        except (ValueError, TypeError):
            return {"on": True, "time": ""}
    return {"on": True, "time": timezone.localtime(when).strftime("%H:%M")}


def decisions(sections: list[str], year: int) -> dict[str, Any]:
    """ "Needs a decision": the management brief's decisions (:func:`neurodb.reports.brief.decisions`) for
    the sections, at most :data:`DECISIONS_SHOWN`, as plain rows without who owns them (never ``owner``,
    ``who`` or the Assign link). The due date and the review's date are dates (the brief stores them as ISO
    text), so the page writes them in words as the rest of the panel does."""
    from neurodb.reports import brief

    found = brief.decisions(sections, year)
    rows = [
        {
            "key": item["key"],
            "severity": item["severity"],
            "title": item["title"],
            "url": item["url"],
            "section": item["section"],
            "status_label": item["status_label"],
            "due_date": _date(item["due_date"]),
        }
        for item in found["items"][:DECISIONS_SHOWN]
    ]
    return {
        "rows": rows,
        "more": max(len(found["items"]) - DECISIONS_SHOWN, 0),
        "date": _date(found["review_date"]),
    }


def _date(value: Any) -> datetime.date | None:
    """A date from the brief's ISO text (or a date already); None when empty or not a date."""
    if isinstance(value, datetime.date) or not value:
        return value or None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def your_tasks(user, year: int) -> list[dict[str, Any]]:
    """ "Your tasks" (Section editors and Administrators; counts only, each with its page): document review
    findings not reviewed yet, Makani wellbeing flags to follow up, and, while ActivityInfo is in use, the HPM
    reports of ``year`` to comment on. A line whose count is 0 is left out."""
    role = role_of(user)
    admin = role == ADMIN
    editor = role == SECTION_EDITOR
    if not (admin or editor):
        return []
    section_id = getattr(user, "section_id", None)
    lines: list[dict[str, Any]] = []
    # 1. the document review's findings not reviewed yet: the editor's section, or the whole country
    from neurodb.knowledge import access as knowledge_access
    from neurodb.knowledge.models import DocumentReviewSettings, Verdict
    from neurodb.knowledge.review import shown_findings

    review_on = DocumentReviewSettings.objects.filter(pk=1).values_list("enabled", flat=True).first()
    if review_on and knowledge_access.can_add(user) and (admin or section_id is not None):
        found = shown_findings().filter(verdict=Verdict.UNREVIEWED)
        if not admin:
            found = found.filter(document__section_id=section_id)
        n = found.count()
        if n:
            lines.append(
                {
                    "count": n,
                    "kind": "documents",
                    "url": reverse("knowledge:review"),
                }
            )
    # 2. Makani wellbeing flags to follow up: Administrators, and the editors of an education section
    if admin or (editor and monitoring.default_sections(user, list(WELLBEING_SECTIONS))):
        from neurodb.wellbeing.models import Flag

        n = Flag.objects.filter(status=Flag.OPEN).count()
        if n:
            lines.append({"count": n, "kind": "wellbeing", "url": reverse("wellbeing:flags")})
    # 3. while ActivityInfo is in use: the HPM reports of this year whose masters sit in the editor's section
    if editor and section_id is not None:
        from neurodb.indicators.models import NeuroReport
        from neurodb.indicators.services import activityinfo

        state = activityinfo.state()
        ryear = activityinfo.reporting_year_for(year) if state.in_use else None
        if ryear is not None:
            n = (
                NeuroReport.objects.filter(
                    is_active=True,
                    is_hpm=True,
                    ryear=ryear,
                    neuroreportmasterindicator__master__database__section_id=section_id,
                )
                .distinct()
                .count()
            )
            if n:
                lines.append(
                    {
                        "count": n,
                        "kind": "hpm",
                        "year": year,
                        "url": _databases_url(state.names[year]),
                    }
                )
    return lines


# ------------------------------------------------------------------------------------------- programmes
def programmes(user, today: datetime.date) -> dict[str, Any]:
    """ "Programmes and sources": the country programme cycle, the Compiler figures and ActivityInfo, each one
    line with its page, for the whole country. A source that is not live says nothing to a viewer; a person
    who can set it up (:func:`neurodb.accounts.roles.can_set_up`) reads a muted line that says what is
    missing."""
    from neurodb.indicators.services import navigation

    setup = can_set_up(user)
    sources = navigation.sources()
    rows: list[dict[str, Any]] = []
    rows.extend(_cpd_rows(today, setup) if sources.get("cpd_cycle") or setup else [])
    rows.extend(_compiler_rows(sources, setup))
    rows.extend(_activityinfo_rows())
    return {"rows": rows}


def _cpd_rows(today: datetime.date, setup: bool) -> list[dict[str, Any]]:
    from neurodb.cpd import services as cpd

    cycle = cpd.current_cycle()
    url = reverse("cpd:dashboard")
    if cycle is None:
        return [{"kind": "cpd_none", "muted": True}] if setup else []
    span = f"{cycle.start_year}–{cycle.end_year}"
    if cycle.end_year < today.year:
        return [{"kind": "cpd_ended", "span": span, "end": cycle.end_year, "url": url, "setup": setup}]
    if cycle.start_year > today.year:
        return [{"kind": "cpd_later", "span": span, "start": cycle.start_year, "url": url}]
    data = cpd.build(cycle, today, as_of="")  # its own fingerprint, never the overview's
    counts = data.get("counts") or {}
    return [
        {
            "kind": "cpd",
            "span": span,
            "year": today.year - cycle.start_year + 1,
            "years": cycle.end_year - cycle.start_year + 1,
            "on": counts.get(cpd.ON_TRACK, 0) + counts.get(cpd.AHEAD, 0),
            "off": counts.get(cpd.OFF_TRACK, 0),
            "total": data.get("indicator_total", 0),
            "url": url,
        }
    ]


def _compiler_rows(sources: dict[str, Any], setup: bool) -> list[dict[str, Any]]:
    if not sources.get("compiler"):
        return [{"kind": "compiler_off", "muted": True}] if setup else []
    from neurodb.education import power_bi

    urls = {programme: url for programme, _label, url in power_bi.PROGRAMMES}
    rows = []
    for entry in power_bi.summary():
        if not entry.get("compared") or not isinstance(entry.get("counted"), int):
            continue
        rows.append(
            {
                "kind": "compiler",
                "label": entry["label"],
                "counted": entry["counted"],
                "period": entry.get("period") or "",
                "url": reverse(urls[entry["programme"]]),
            }
        )
    return rows


def _activityinfo_rows() -> list[dict[str, Any]]:
    from neurodb.indicators.services import activityinfo, navigation

    state = activityinfo.state()
    # the menu's own link to the archive's databases page: the year the import serves, else the last
    # ActivityInfo year (never the bare address, which opens the reporting year ticked current)
    databases_url = navigation.archive(state)["databases_url"]
    if state.in_use and state.serving is not None:
        return [_activityinfo_row(state.serving, state.pks[state.serving], databases_url)]
    if state.in_use:
        # set "In use" while the import serves no year of records (this year has none; last year only during
        # January): in use, as the menu says, never "archive". The reporting year ticked current (what the
        # import reads) is named: with its records, as any ActivityInfo year; without them, waiting for its
        # first ones. Nothing to link to without a reporting year ticked current
        if not state.current_name:
            return []
        current = next((year for year, name in state.names.items() if name == state.current_name), None)
        if current is not None:  # the year ticked current has records (Rule 1)
            return [_activityinfo_row(current, state.pks[current], _databases_url(state.current_name))]
        return [
            {
                "kind": "activityinfo_waiting",
                "year": state.current_name,
                "url": _databases_url(state.current_name),
            }
        ]
    if state.last is not None:
        return [
            {
                "kind": "activityinfo_archive",
                "span": state.span,
                "url": databases_url,
            }
        ]
    return []


def _activityinfo_row(year: int, reporting_year_pk: int, url: str) -> dict[str, Any]:
    """The in-use line of one ActivityInfo year (one with records): its results tracked against targets (the
    active master indicators of its displayed databases) and its last import; no result tracked yet (no
    master indicator): the line says the records came in, never "0"."""
    from neurodb.indicators.models import Database, MasterIndicator

    shown = Database.objects.filter(reporting_year_id=reporting_year_pk, display=True)
    last = shown.aggregate(last=Max("last_monthly_update_date"))["last"]
    masters = MasterIndicator.objects.filter(database__in=shown, is_active=True).count()
    return {
        "kind": "activityinfo",
        "year": year,
        "masters": masters,
        "last": timezone.localtime(last).date() if last else None,
        "url": url,
    }


def _databases_url(name: str) -> str:
    """The ActivityInfo databases of one reporting year: never the bare address, which opens the reporting
    year ticked current (maybe a new, empty one)."""
    return f"{reverse('reports:databases')}?{urlencode({'year': name})}"


__all__ = [
    "WELLBEING_SECTIONS",
    "decisions",
    "default_sections",
    "own_names",
    "programmes",
    "review_schedule",
    "section_ids",
    "since",
    "since_label",
    "since_yesterday",
    "your_tasks",
]
