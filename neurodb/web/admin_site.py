"""NeuroDB admin site: models grouped by task instead of by internal app label, and a dashboard home.

Every ``@admin.register`` in the project registers on this site (it is the default site through
``NeuroDBAdminConfig``). The look comes from django-unfold; the sidebar is built from the same
task groups as the dashboard (``sidebar_navigation``, wired in ``settings.UNFOLD``).
"""

from __future__ import annotations

import datetime
import logging
from typing import Any
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.decorators import login_not_required
from django.db import DatabaseError
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.formats import date_format
from django.utils.text import slugify
from django.utils.timesince import timesince
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views.decorators.cache import never_cache
from unfold.sites import UnfoldAdminSite

logger = logging.getLogger(__name__)

# (group name, description, [app_label.ModelName, ...]) in display order.
GROUPS: list[tuple[Any, Any, list[str]]] = [
    (
        _("Reporting setup"),
        _("Reporting years, ActivityInfo databases and the indicator hierarchy."),
        [
            "pivoting.ReportingYear",
            "pivoting.Database",
            "pivoting.MasterIndicator",
            "pivoting.SubIndicator",
            "pivoting.IndicatorNew",
            "pivoting.Activity",
            "pivoting.MasterIndicatorTag",
            "reports.SectionPlan",
            "datamart.IndicatorFlag",
        ],
    ),
    (
        _("Neuro reports and HPM"),
        _("Report composition and section comments."),
        ["pivoting.NeuroReport", "pivoting.NeuroReportComment"],
    ),
    (
        _("Library and maps"),
        _("Published resources, their classification and map products."),
        [
            "pivoting.Resource",
            "pivoting.ResourceType",
            "pivoting.ResourceTopic",
            "pivoting.ResourceTag",
            "pivoting.Map",
        ],
    ),
    (
        _("Users and access"),
        _("Accounts, roles, sections, donor access and sign-in."),
        [
            "users.User",
            "donors.DonorAccount",
            "auth.Group",
            "users.Section",
            "users.Office",
            "account.EmailAddress",
            "socialaccount.SocialAccount",
            "socialaccount.SocialApp",
            "socialaccount.SocialToken",
            "sites.Site",
        ],
    ),
    (
        _("Data and sync"),
        _(
            "Import history, scheduled jobs, the daily review, population figures, saved views, AI "
            "questions, audit trail."
        ),
        [
            "core.SyncRun",
            "core.ScheduledJob",
            "review.DailyReview",
            "review.ReviewFinding",
            "review.FindingAssignment",
            "core.PopulationFigure",
            "core.SavedView",
            "assistant.AssistantQuestion",
            "admin.LogEntry",
        ],
    ),
    (
        _("Partnerships (eTools)"),
        _(
            "Replicated from eTools every night; edit them in eTools. The ActivityInfo partner links are "
            "the exception: they are set here."
        ),
        [
            "etools.PartnerOrganization",
            "etools.PartnerLink",
            "etools.Agreement",
            "etools.PCA",
            "etools.Engagement",
            "etools.Travel",
            "etools.TravelActivity",
            "etools.ActionPoint",
        ],
    ),
    (
        _("eTools Datamart (read-only)"),
        _("Funds, indicators, assurance and monitoring from the eTools Datamart, refreshed every night."),
        [
            "datamart.FundsReservation",
            "datamart.Grant",
            "datamart.PDIndicator",
            "datamart.AuditEngagement",
            "datamart.PartnerAssessment",
            "datamart.PSEAAssessment",
            "datamart.ActionPoint",
            "datamart.TPMVisit",
            "datamart.MonitoringFinding",
            "datamart.HACTAggregate",
            "datamart.FundsReservationHeader",
            "datamart.AuditFinding",
            "datamart.ReportedIndicator",
            "datamart.TPMActivity",
            "datamart.ProgrammaticVisit",
            "datamart.PlannedVisits",
            "datamart.PartnerHACTYear",
            "datamart.PDActivity",
            "datamart.DatamartDocument",
        ],
    ),
    (
        _("Locations"),
        _("Admin areas and sites used by maps and eTools."),
        [
            "locations.Location",
            "locations.LocationType",
            "pivoting.GovernorateLocation",
            "pivoting.DistrictLocation",
            "pivoting.CadasterLocation",
            "pivoting.SimpleLocation",
        ],
    ),
]
STALE_DATABASE_DAYS = 40
# Sign-in plumbing (allauth, sites) and the replicated Datamart tables: listed for superusers only,
# so the administrators' own tools are not lost among them. The pages stay reachable by URL.
DEVELOPER_ONLY = {
    "account.EmailAddress",
    "socialaccount.SocialApp",
    "socialaccount.SocialToken",
    "sites.Site",
}
DEVELOPER_ONLY_APPS = {"datamart"}
DEVELOPER_ONLY_KEEP = {"datamart.IndicatorFlag"}  # edited by administrators (Reporting setup)
# Clearer menu names for models whose verbose name is technical.
RENAMES = {"admin.LogEntry": _("Audit trail"), "core.SyncRun": _("Import and sync runs")}
# Material Symbols names for the sidebar (https://fonts.google.com/icons); unlisted models get a dot.
ICONS = {
    "pivoting.ReportingYear": "calendar_month",
    "pivoting.Database": "database",
    "pivoting.MasterIndicator": "flag",
    "pivoting.SubIndicator": "account_tree",
    "pivoting.IndicatorNew": "list_alt",
    "pivoting.Activity": "assignment",
    "pivoting.MasterIndicatorTag": "sell",
    "pivoting.NeuroReport": "summarize",
    "pivoting.NeuroReportComment": "comment",
    "pivoting.Resource": "menu_book",
    "pivoting.ResourceType": "category",
    "pivoting.ResourceTopic": "topic",
    "pivoting.ResourceTag": "sell",
    "pivoting.Map": "map",
    "users.User": "person",
    "auth.Group": "badge",
    "users.Section": "workspaces",
    "users.Office": "apartment",
    "account.EmailAddress": "mail",
    "socialaccount.SocialAccount": "link",
    "socialaccount.SocialApp": "key",
    "socialaccount.SocialToken": "token",
    "sites.Site": "language",
    "core.SyncRun": "sync",
    "core.ScheduledJob": "schedule",
    "core.PopulationFigure": "groups",
    "core.SavedView": "bookmark",
    "admin.LogEntry": "history",
    "review.DailyReview": "fact_check",
    "review.ReviewFinding": "checklist",
    "review.FindingAssignment": "assignment_ind",
    "reports.SectionPlan": "target",
    "datamart.IndicatorFlag": "child_care",
    "assistant.AssistantQuestion": "smart_toy",
    "datamart.FundsReservation": "account_balance",
    "datamart.Grant": "redeem",
    "datamart.PDIndicator": "monitoring",
    "datamart.AuditEngagement": "fact_check",
    "datamart.PartnerAssessment": "rule",
    "datamart.PSEAAssessment": "shield",
    "datamart.ActionPoint": "task_alt",
    "datamart.TPMVisit": "travel_explore",
    "datamart.MonitoringFinding": "visibility",
    "datamart.HACTAggregate": "insights",
    "datamart.FundsReservationHeader": "payments",
    "datamart.AuditFinding": "report",
    "datamart.ReportedIndicator": "assignment_turned_in",
    "datamart.TPMActivity": "travel_explore",
    "datamart.ProgrammaticVisit": "directions_car",
    "datamart.PlannedVisits": "event",
    "datamart.PartnerHACTYear": "verified",
    "datamart.PDActivity": "checklist",
    "datamart.DatamartDocument": "dataset",
    "etools.PartnerOrganization": "handshake",
    "etools.PartnerLink": "link",
    "etools.Agreement": "contract",
    "etools.PCA": "description",
    "etools.Engagement": "fact_check",
    "etools.Travel": "flight",
    "etools.TravelActivity": "route",
    "etools.ActionPoint": "task_alt",
    "locations.Location": "location_on",
    "locations.LocationType": "layers",
    "pivoting.GovernorateLocation": "location_city",
    "pivoting.DistrictLocation": "holiday_village",
    "pivoting.CadasterLocation": "grid_on",
    "pivoting.SimpleLocation": "place",
}


class NeuroDBAdminSite(UnfoldAdminSite):
    site_header = _("NeuroDB administration")
    site_title = _("NeuroDB admin")
    index_title = _("Dashboard")
    site_url = "/"

    def get_app_list(self, request, app_label=None):
        if app_label:  # the per-app index page keeps Django's behaviour
            return super().get_app_list(request, app_label)
        apps = self._build_app_dict(request)
        by_key = {f"{app['app_label']}.{m['object_name']}": m for app in apps.values() for m in app["models"]}
        if not request.user.is_superuser:
            by_key = {k: m for k, m in by_key.items() if not _developer_only(k)}
        for key, label in RENAMES.items():
            if key in by_key:
                by_key[key]["name"] = label
        for key, model in by_key.items():
            model["icon"] = ICONS.get(key, "circle")
        used: set[str] = set()
        index_url = reverse(f"{self.name}:index")
        grouped = []
        for name, description, keys in GROUPS:
            models = [by_key[k] for k in keys if k in by_key]
            used.update(k for k in keys if k in by_key)
            if models:
                slug = slugify(str(name))
                grouped.append(
                    {
                        "name": name,
                        "description": description,
                        "app_label": slug,
                        "app_url": f"{index_url}#group-{slug}",
                        "has_module_perms": True,
                        "models": models,
                    }
                )
        rest = sorted((m for k, m in by_key.items() if k not in used), key=lambda m: str(m["name"]))
        if rest:
            grouped.append(
                {
                    "name": _("Other"),
                    "description": "",
                    "app_label": "other",
                    "app_url": f"{index_url}#group-other",
                    "has_module_perms": True,
                    "models": rest,
                }
            )
        return grouped

    @method_decorator(never_cache)
    @login_not_required
    def login(self, request, extra_context=None):
        # Unfold's login form has no hidden "next" field, so without ?next= in the URL a sign-in
        # would land on the public site (LOGIN_REDIRECT_URL) instead of the admin.
        if request.method == "GET" and REDIRECT_FIELD_NAME not in request.GET:
            index_url = reverse(f"{self.name}:index")
            return redirect(f"{request.path}?{urlencode({REDIRECT_FIELD_NAME: index_url})}")
        return super().login(request, extra_context)

    def index(self, request, extra_context=None):
        extra_context = {**(extra_context or {}), "dashboard": dashboard(request)}
        return super().index(request, extra_context)


def _developer_only(key: str) -> bool:
    if key in DEVELOPER_ONLY_KEEP:
        return False
    return key in DEVELOPER_ONLY or key.split(".", 1)[0] in DEVELOPER_ONLY_APPS


def adopt_unfold(site) -> None:
    """Give the admins registered by Django and allauth (groups, sites, e-mail, social accounts)
    Unfold's ModelAdmin, so their pages match the theme. Their own options are kept."""
    from unfold.admin import ModelAdmin

    for model, model_admin in list(site._registry.items()):
        if isinstance(model_admin, ModelAdmin):
            continue
        cls = type(model_admin)
        site.unregister(model)
        site.register(model, type(cls.__name__, (cls, ModelAdmin), {"__module__": cls.__module__}))


def sidebar_navigation(request) -> list[dict[str, Any]]:
    """Unfold sidebar: the dashboard link, then one collapsible section per task group."""
    from django.contrib import admin

    groups = [
        {
            "title": None,
            "items": [{"title": _("Dashboard"), "icon": "dashboard", "link": reverse("admin:index")}],
        }
    ]
    for app in admin.site.get_app_list(request):
        groups.append(
            {
                "title": app["name"],
                "collapsible": True,
                "items": [
                    {"title": m["name"], "icon": m["icon"], "link": m["admin_url"]}
                    for m in app["models"]
                    if m.get("admin_url")
                ],
            }
        )
    return groups


def environment_badge(request) -> list[str] | None:
    """The coloured label next to the user menu, so nobody edits production thinking it is a test."""
    return {
        "production": [_("Production"), "danger"],
        "staging": [_("Staging"), "warning"],
        "local": [_("Local"), "info"],
    }.get(settings.ENV)


def dashboard(request) -> dict[str, Any]:
    """Figures, freshness and data-quality warnings for the admin home. Never breaks the admin."""
    try:
        return _dashboard(request)
    except DatabaseError:
        logger.exception("admin dashboard could not be built")
        return {"error": True}


def _dashboard(request) -> dict[str, Any]:
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from neurodb.accounts.roles import ALL_ROLES
    from neurodb.core.models import SyncRun
    from neurodb.facts.models import ActivityReportNew
    from neurodb.indicators.models import Database, MasterIndicator
    from neurodb.indicators.services.navigation import current_year
    from neurodb.library.models import Resource

    user_model = get_user_model()
    now = timezone.now()
    year = current_year()
    databases = Database.objects.filter(reporting_year=year) if year else Database.objects.none()
    shown = databases.filter(display=True)
    masters = MasterIndicator.objects.filter(database__in=shown, is_active=True)
    stale_before = now - datetime.timedelta(days=STALE_DATABASE_DAYS)

    jobs = []
    for job, label in SyncRun.Job.choices:
        last = SyncRun.objects.filter(job=job).order_by("-started_at").first()
        ok = SyncRun.last_success(job)
        jobs.append({"job": job, "label": label, "last": last, "last_success": ok})

    no_role = (
        user_model.objects.filter(is_active=True, is_superuser=False, donor_account__isnull=True)
        .exclude(groups__name__in=ALL_ROLES)  # donor accounts have no role on purpose
        .distinct()
        .count()
    )
    warnings = []
    if not year:
        warnings.append(
            {
                "text": _("No reporting year is marked as current."),
                "url": _admin_url("pivoting_reportingyear"),
            }
        )
    never = shown.filter(last_monthly_update_date__isnull=True).count()
    if never:
        warnings.append(
            {
                "text": ngettext(
                    "%(n)s displayed database was never imported.",
                    "%(n)s displayed databases were never imported.",
                    never,
                )
                % {"n": never},
                "url": _admin_url("pivoting_database") + "?freshness=never",
            }
        )
    stale = list(shown.filter(last_monthly_update_date__lt=stale_before).values_list("name", flat=True))
    if stale:
        names = ", ".join(str(n) for n in stale[:3])
        if len(stale) > 3:
            names += " " + _("and %(n)s more") % {"n": len(stale) - 3}
        warnings.append(
            {
                "text": ngettext(
                    "%(names)s was not imported for more than %(d)s days.",
                    "%(names)s were not imported for more than %(d)s days.",
                    len(stale),
                )
                % {"names": names, "d": STALE_DATABASE_DAYS},
                "url": _admin_url("pivoting_database") + "?freshness=stale",
            }
        )
    no_target = masters.filter(Q(awp_target__isnull=True) | Q(awp_target=0)).count()
    if no_target:
        warnings.append(
            {
                "text": ngettext(
                    "%(n)s active master indicator has no target.",
                    "%(n)s active master indicators have no target.",
                    no_target,
                )
                % {"n": no_target},
                "url": _admin_url("pivoting_masterindicator") + "?has_target=no&is_active__exact=1",
            }
        )
    if no_role:
        warnings.append(
            {
                "text": ngettext(
                    "%(n)s active user has no role and sees the site as a viewer.",
                    "%(n)s active users have no role and see the site as viewers.",
                    no_role,
                )
                % {"n": no_role},
                "url": _admin_url("users_user") + "?role=none",
            }
        )
    for j in jobs:
        last = j["last"]
        if last and last.status == SyncRun.Status.FAILED:
            text = _("The last %(job)s failed.") % {"job": j["label"]}
        elif last and last.status == SyncRun.Status.PARTIAL:
            text = ngettext(
                "The last %(job)s succeeded with errors: %(n)s row failed.",
                "The last %(job)s succeeded with errors: %(n)s rows failed.",
                last.rows_failed,
            ) % {"job": j["label"], "n": last.rows_failed}
        else:
            continue
        warnings.append({"text": text, "url": reverse("admin:core_syncrun_change", args=[last.pk])})
    warnings += _schedule_warnings(now)

    return {
        "year": year,
        "tiles": [
            {
                "label": _("Current year"),
                "value": year.name if year else "—",
                "url": _admin_url("pivoting_reportingyear"),
            },
            {
                "label": _("Databases shown"),
                "value": shown.count(),
                "url": _admin_url("pivoting_database")
                + (f"?reporting_year__id__exact={year.id}&display__exact=1" if year else ""),
            },
            {
                "label": _("Active master indicators"),
                "value": masters.count(),
                "url": _admin_url("pivoting_masterindicator") + "?is_active__exact=1",
            },
            {
                "label": _("Activity records"),
                "value": ActivityReportNew.objects.filter(dbase__in=shown).count(),
                "url": None,
            },
            {
                "label": _("Active users"),
                "value": user_model.objects.filter(is_active=True).count(),
                "url": _admin_url("users_user") + "?is_active__exact=1",
            },
            {
                "label": _("Published resources"),
                "value": Resource.objects.filter(published=True).count(),
                "url": _admin_url("pivoting_resource") + "?published__exact=1",
            },
        ],
        "jobs": jobs,
        "warnings": warnings,
    }


def _schedule_warnings(now) -> list[dict[str, Any]]:
    """The scheduler not checking in, switched-on jobs past their time, a missing daily review."""
    from neurodb.core.admin import is_overdue, scheduler_heartbeat
    from neurodb.core.jobs import COMMANDS
    from neurodb.core.models import ScheduledJob, SyncRun
    from neurodb.review.models import DailyReview

    warnings = []
    jobs_url = _admin_url("core_scheduledjob")
    enabled = list(ScheduledJob.objects.filter(enabled=True))
    overdue = [job for job in enabled if is_overdue(job, now)]
    stalled = False
    if settings.SCHEDULER_ENABLED and enabled:
        seen, _host, alive = scheduler_heartbeat()
        stalled = not alive
        if stalled:
            since = timesince(seen, now) if seen else None
            text = (
                _("The scheduler has not checked in for %(since)s: scheduled jobs are not starting.")
                % {"since": since}
                if since
                else _("The scheduler has never checked in: scheduled jobs are not starting.")
            )
            if overdue:
                text += " " + ngettext("%(n)s job is overdue.", "%(n)s jobs are overdue.", len(overdue)) % {
                    "n": len(overdue)
                }
            warnings.append({"text": text, "url": jobs_url})
    for job in overdue:
        command = COMMANDS.get(job.command)
        label = command.label if command else job.command
        ran = bool(job.last_started_at) or bool(
            command and command.sync_job and SyncRun.objects.filter(job=command.sync_job).exists()
        )
        values = {"job": label, "when": _local(job.next_run_at)}
        if not ran:  # listed even under a stalled scheduler: the data it brings was never there
            text = _("Scheduled job “%(job)s” has never run: it was due %(when)s.") % values
        elif not stalled:  # under a stalled scheduler, counted in its warning
            text = _("Scheduled job “%(job)s” is overdue: it was due %(when)s.") % values
        else:
            continue
        warnings.append({"text": text, "url": jobs_url})
    if any(job.command == "daily_review" for job in enabled):
        yesterday = timezone.localdate(now) - datetime.timedelta(days=1)
        if not DailyReview.objects.filter(date__gte=yesterday, status=DailyReview.Status.SUCCEEDED).exists():
            last = DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED).order_by("-date").first()
            if last:
                text = _("No daily review since %(date)s.") % {"date": date_format(last.date, "j M Y")}
            else:
                text = _("No daily review has been written yet.")
            warnings.append({"text": text, "url": _admin_url("review_dailyreview")})
    return warnings


def _local(when) -> str:
    return date_format(timezone.localtime(when), "j M, H:i")


def _admin_url(model: str) -> str:
    return reverse(f"admin:{model}_changelist")
