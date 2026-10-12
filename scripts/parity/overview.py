"""Stage 0's group: the country overview (``/``), its filter matrix and where its figures live (SPEC7 §3.5).

**Users.** An administrator (the first active superuser, by username) and a viewer with a section (the
first active, non-superuser account in the Viewer group with a section): on a first load the viewer sees
its own section, the administrator every section.

**Matrix.** First load (own section), every section, the viewer's section, another section, two sections, one
governorate, a section and a governorate, the previous reporting year (alone, and with a section and a
governorate), a section with no data at all, and the daily review's tabs (yesterday, the week, a date with a
review, a date without one).

**Context paths.** The old page (one response) and the page in parts (shell plus parts) keep the same values
under these names; the right-hand side says where the page in parts has them:

=====================================  ==========================================================
``data`` (the build, without the        ``data`` of the ``kpis`` and ``figures`` parts (both must
freshness strip)                        agree), or of the shell when it drew them inline
``data.freshness``                      ``freshness`` of the shell (``finished_at`` in place of the
                                        ``last_success`` run: compared as ``finished_at``)
``data.freshness_note``                 ``freshness_note`` of the shell
``chart_data``                          ``chart`` of the ``figures`` part
``review``                              ``review`` of the ``review`` part (``review-more`` must agree)
``whats_new``, ``forecast_risk``        the ``news`` part
everything else in ``SHELL_KEYS``       the shell
=====================================  ==========================================================

JSON: ``overview-chart-data`` became ``overview-figures-data`` (the same dict, ``labels`` included). Ids that
only the page in parts has (``part-<name>``, the wrappers of the loaded parts) are not compared.
"""

from __future__ import annotations

import re
from typing import Any

from scripts.parity import core

JSON_IDS = {"overview-chart-data": "overview-figures-data"}
NEW_IDS = re.compile(r"part-[a-z][a-z0-9-]*")
SHELL_KEYS = (
    "page_title",
    "page_subtitle",
    "year",
    "today",
    "options",
    "selected",
    "own_section",
    "labels",
    "keep_query",
    "programmes_query",
    "base_query",
    "page_query",
)
NO_SUCH_SECTION = "No such section"
TIMED = ("first load", "every section", "one section", "one governorate", "previous year")  # timing.py
FIRST_FIGURE = "kpis"  # the part whose arrival is the page's first figure (the group leader)


def users() -> dict[str, Any]:
    from neurodb.accounts.models import User
    from neurodb.accounts.roles import VIEWER

    admin = User.objects.filter(is_superuser=True, is_active=True).order_by("username").first()
    viewer = (
        User.objects.filter(groups__name=VIEWER, is_active=True, is_superuser=False, section__isnull=False)
        .order_by("username")
        .first()
    )
    if admin is None or viewer is None:
        raise SystemExit("the world needs an active superuser and an active viewer with a section")
    return {"admin": admin, "viewer": viewer}


def matrix(people: dict[str, Any]) -> list[tuple[str, str]]:
    """The filters, as (label, URL). Computed when recording and stored with the recording."""
    from urllib.parse import urlencode

    from neurodb.indicators.models import ReportingYear
    from neurodb.reports import overview as service
    from neurodb.reports import services
    from neurodb.review.models import DailyReview

    year = services.resolve_year(None)
    if year is None:
        raise SystemExit("the world has no current reporting year")
    previous = ReportingYear.objects.exclude(pk=year.pk).filter(name__lt=year.name).order_by("-name").first()
    year_number = int(str(year.year or year.name)[:4])
    options = service.options(year_number)
    own = people["viewer"].section.name
    other = next(s for s in options["sections"] if s != own)
    place = "Beirut" if "Beirut" in options["governorates"] else options["governorates"][0]
    reviews = list(
        DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED)
        .order_by("-date")
        .values_list("date", flat=True)
    )
    no_review = f"{year_number}-01-01"
    assert no_review not in {d.isoformat() for d in reviews}

    def url(*pairs: tuple[str, str]) -> str:
        return "/?" + urlencode(pairs)

    rows = [
        ("first load", "/"),
        ("every section", url(("section", ""))),
        ("one section", url(("section", own))),
        ("another section", url(("section", other))),
        ("two sections", url(("section", other), ("section", own))),
        ("one governorate", url(("section", ""), ("governorate", place))),
        ("section and governorate", url(("section", own), ("governorate", place))),
    ]
    if previous is not None:
        rows += [
            ("previous year", url(("year", previous.name))),
            (
                "previous year, section, governorate",
                url(("section", own), ("year", previous.name), ("governorate", place)),
            ),
        ]
    rows += [
        ("no result", url(("section", NO_SUCH_SECTION))),
        ("review yesterday", url(("review", "yesterday"))),
        ("review week, one section", url(("section", own), ("review", "week"))),
    ]
    if len(reviews) > 1:
        rows.append(("review on a date", url(("review", reviews[1].isoformat()))))
    rows.append(("review on a date without one", url(("review", no_review))))
    return rows


# --------------------------------------------------------------------------------------------- the figures
def _freshness(jobs: Any) -> Any:
    """The freshness strip in one form: the old page kept the last run (``last_success``), the new one its
    ``finished_at``."""
    if jobs is None:
        return None
    out = []
    for job in jobs:
        if "finished_at" in job:
            finished = job["finished_at"]
        else:
            finished = getattr(job.get("last_success"), "finished_at", None)
        out.append(
            {
                "job": job["job"],
                "label": str(job["label"]),
                "finished_at": core.plain(finished),
                "state": job["state"],
                "state_label": str(job["state_label"]),
            }
        )
    return out


def _contexts(response: Any) -> Any:
    return getattr(response, "context", None)


def _get(context: Any, key: str) -> Any:
    if context is None:
        return None
    try:
        return context[key] if key in context else None
    except (KeyError, TypeError):
        return None


def figures(page: core.Page, mode: str) -> tuple[dict[str, Any], list[str]]:
    """The page's figures under the old page's names, and the problems found on the way (parts that
    should agree and do not)."""
    shell = _contexts(page.shell)
    out = {key: core.plain(_get(shell, key)) for key in SHELL_KEYS}
    problems: list[str] = []
    if mode == "record":
        data = _get(shell, "data")
        if data is not None:
            data = dict(data)
            out["freshness"] = _freshness(data.pop("freshness", None))
            out["freshness_note"] = data.pop("freshness_note", None)
        else:
            out["freshness"] = out["freshness_note"] = None
        out["data"] = core.plain(data)
        out["chart"] = core.plain(_get(shell, "chart_data"))
        out["review"] = core.plain(_get(shell, "review"))
        out["whats_new"] = core.plain(_get(shell, "whats_new"))
        out["forecast_risk"] = core.plain(_get(shell, "forecast_risk"))
        return out, problems

    parts = {
        name: _contexts(response) for name, response in page.parts.items() if response.status_code == 200
    }

    def one(key: str, *names: str) -> Any:
        """``key`` from the first of ``names`` that has it (the shell when it drew them inline), checking
        that every one of them that has it agrees."""
        found = [
            (name, parts[name][key]) for name in names if name in parts and _get(parts[name], key) is not None
        ]
        if not found and _get(shell, key) is not None:
            found = [("shell", shell[key])]
        if not found:
            return None
        first = core.plain(found[0][1])
        for name, value in found[1:]:
            if core.plain(value) != first:
                problems.append(f"{key}: the {found[0][0]} and {name} parts disagree")
        return first

    out["freshness"] = _freshness(_get(shell, "freshness"))
    out["freshness_note"] = _get(shell, "freshness_note")
    out["data"] = one("data", "kpis", "figures")
    out["chart"] = one("chart", "figures")
    out["review"] = one("review", "review", "review-more")
    out["whats_new"] = one("whats_new", "news")
    out["forecast_risk"] = one("forecast_risk", "news")
    return out, problems
