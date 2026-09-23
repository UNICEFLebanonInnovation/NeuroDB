"""NeuroDB admin site: models grouped by task instead of by internal app label, and a dashboard home.

Every ``@admin.register`` in the project registers on this site (it is the default site through
``NeuroDBAdminConfig``), so no ModelAdmin needs to change to benefit from it.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from django.contrib import admin
from django.db import DatabaseError
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

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
        _("Accounts, roles, sections and sign-in."),
        [
            "users.User",
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
        _("Import history, population figures, saved views and the audit trail."),
        ["core.SyncRun", "core.PopulationFigure", "core.SavedView", "admin.LogEntry"],
    ),
    (
        _("Partnerships (eTools, read-only)"),
        _("Replicated from eTools every night; edit them in eTools."),
        [
            "etools.PartnerOrganization",
            "etools.Agreement",
            "etools.PCA",
            "etools.Engagement",
            "etools.Travel",
            "etools.TravelActivity",
            "etools.ActionPoint",
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
# Clearer menu names for models whose verbose name is technical.
RENAMES = {"admin.LogEntry": _("Audit trail"), "core.SyncRun": _("Import and sync runs")}


class NeuroDBAdminSite(admin.AdminSite):
    site_header = _("NeuroDB administration")
    site_title = _("NeuroDB admin")
    index_title = _("Dashboard")
    enable_nav_sidebar = True
    site_url = "/"

    def get_app_list(self, request, app_label=None):
        if app_label:  # the per-app index page keeps Django's behaviour
            return super().get_app_list(request, app_label)
        apps = self._build_app_dict(request)
        by_key = {f"{app['app_label']}.{m['object_name']}": m for app in apps.values() for m in app["models"]}
        for key, label in RENAMES.items():
            if key in by_key:
                by_key[key]["name"] = label
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

    def index(self, request, extra_context=None):
        extra_context = {**(extra_context or {}), "dashboard": dashboard(request)}
        return super().index(request, extra_context)


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
        user_model.objects.filter(is_active=True, is_superuser=False)
        .exclude(groups__name__in=ALL_ROLES)
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
    stale = shown.filter(last_monthly_update_date__lt=stale_before).count()
    if stale:
        warnings.append(
            {
                "text": ngettext(
                    "%(n)s database was not imported for %(d)s days.",
                    "%(n)s databases were not imported for %(d)s days.",
                    stale,
                )
                % {"n": stale, "d": STALE_DATABASE_DAYS},
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
    failed = [j for j in jobs if j["last"] and j["last"].status == SyncRun.Status.FAILED]
    for j in failed:
        warnings.append(
            {
                "text": _("The last %(job)s failed.") % {"job": j["label"]},
                "url": _admin_url("core_syncrun") + f"?job__exact={j['job']}",
            }
        )

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


def _admin_url(model: str) -> str:
    return reverse(f"admin:{model}_changelist")
