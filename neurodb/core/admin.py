import datetime
import re

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.humanize.templatetags.humanize import naturaltime
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect
from django.urls import NoReverseMatch, path, reverse
from django.utils import formats, timezone
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.forms import BaseDialogForm
from unfold.widgets import UnfoldAdminCheckboxSelectMultipleWidget, UnfoldAdminRadioSelectWidget

from neurodb.integrations.http import RETRY_STATUSES
from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from .admin_jobs import JobActionsMixin
from .models import ActivityInfoSetting, DatamartSetting, PopulationFigure, SavedView, ScheduledJob, SyncRun

# The scheduler checks in every 30 seconds: silent for longer than this, it has stopped.
SCHEDULER_STALLED_AFTER = datetime.timedelta(minutes=3)
# A job starts within a minute of its time; later than this past its time, it is overdue.
OVERDUE_AFTER = datetime.timedelta(minutes=5)


def scheduler_heartbeat() -> tuple[datetime.datetime | None, str, bool]:
    """The scheduler's last check-in, its host, and whether it checked in recently."""
    from .models import SchedulerState

    state = SchedulerState.objects.filter(pk=1).first()
    seen = state.last_seen_at if state else None
    alive = bool(seen and timezone.now() - seen < SCHEDULER_STALLED_AFTER)
    return seen, state.host if state else "", alive


def is_overdue(job: ScheduledJob, now=None) -> bool:
    """An enabled job whose planned time passed without the scheduler starting it."""
    now = now or timezone.now()
    return bool(job.enabled and job.next_run_at and job.next_run_at < now - OVERDUE_AFTER)


def triggered_by_label(value: str) -> str:
    """Who started a run, in words: the scheduler, a command typed on the server, or a person."""
    if value == "schedule":
        return _("Scheduler")
    if value == "command":
        return _("Command line")
    if not value:
        return "—"
    return _("Manual (%(user)s)") % {"user": value}


# Which service a job reads, for the plain-words error hints.
SOURCES = {
    SyncRun.Job.ACTIVITYINFO_STRUCTURE: "ActivityInfo",
    SyncRun.Job.ACTIVITYINFO_DATA: "ActivityInfo",
    SyncRun.Job.ETOOLS: "eTools",
    SyncRun.Job.ETOOLS_DATAMART: "eTools Datamart",
    SyncRun.Job.LOCATIONS: "eTools",
    SyncRun.Job.PARTNER_LINKS: "eTools",
}
# The HTTP status the saved error names ("... HTTP 500 Internal Server Error", "... returned 503"): a run
# keeps only its error's text, so the status is read back from it.
HTTP_STATUS = re.compile(r"\b(?:HTTP|returned) (\d{3})\b")
UNREACHABLE = re.compile(r"Timeout|ConnectionError|SSLError|timed out", re.IGNORECASE)


def _probe_verdict(run: SyncRun) -> str:
    """What a probe of the eTools Datamart found (``details.verdict``), or "" for any other run."""
    if run.job != SyncRun.Job.DATAMART_PROBE or not isinstance(run.details, dict):
        return ""
    return str(run.details.get("verdict") or "")


def error_hint(run: SyncRun) -> str:
    """What a known error means for the administrator, or "" when the raw error is all we know. A 502, 503
    or 504 is the service being unavailable; any other 5xx is an error inside the service for this one
    request, said to have been asked again only for the statuses NeuroDB retries (``RETRY_STATUSES``: 429,
    500, 502, 503 and 504); a 429 is too many requests."""
    error = run.error or ""
    name = SOURCES.get(run.job)
    found = HTTP_STATUS.search(error)
    status = int(found.group(1)) if found else None
    if status in (502, 503, 504):
        hint = _("%(source)s was unavailable: try again later.")
    elif status == 429:
        hint = _("%(source)s refused more requests for now (too many in a short time): try again later.")
    elif status and 500 <= status < 600 and status in RETRY_STATUSES:
        hint = _(
            "%(source)s answered with a server error (HTTP %(status)s), even after NeuroDB asked again. "
            "If the next run fails the same way, tell the team that runs %(team)s."
        )
    elif status and 500 <= status < 600:
        hint = _(
            "%(source)s answered with a server error (HTTP %(status)s). If the next run fails the same way, "
            "tell the team that runs %(team)s."
        )
    elif UNREACHABLE.search(error):
        hint = _("%(source)s did not answer in time: try again later.")
    elif status in (401, 403):
        hint = _(
            "%(source)s refused NeuroDB's credentials: they may have expired. Ask the team that runs "
            "NeuroDB's hosting to renew them."
        )
    else:
        return ""
    return hint % {
        "source": name or _("The data source"),
        "team": name or _("the data source"),
        "status": status,
    }


def dataset_note(run: SyncRun) -> str:
    """The run page only (too long for the list): a failed eTools Datamart dataset is its own run. Says that
    only this dataset stopped, whether a page, figure or check uses it, and how to sync it alone; "" for any
    other run, and for a run cut off by a restart (the whole sync stopped then)."""
    from neurodb.datamart.catalogue import DIAGNOSTIC
    from neurodb.integrations.background import CUT_OFF
    from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS

    if (
        run.job != SyncRun.Job.ETOOLS_DATAMART
        or run.status != SyncRun.Status.FAILED
        or run.target not in ENTITY_SYNCS
        or run.error == CUT_OFF
    ):
        return ""
    values = {"dataset": run.target}
    parts = [
        _(
            "Only this dataset (%(dataset)s) stopped: each eTools Datamart dataset is its own run, and a "
            "failed dataset does not stop the others."
        )
        % values
    ]
    if run.target in DIAGNOSTIC:
        parts.append(
            _(
                "No page, figure or check uses this dataset (only Ask NeuroDB's general eTools look-ups can "
                "read it): it raises no alarm."
            )
        )
    parts.append(
        _(
            "The next sync tries it again. To sync it alone now, press “Sync this dataset again” above, or "
            "“Sync eTools Datamart now” on Import and sync runs, choose “Only the datasets ticked below, in "
            "their usual order” and tick %(dataset)s."
        )
        % values
    )
    return " ".join(str(part) for part in parts)


class ReloadPopulationForm(BaseDialogForm):
    """The confirmation step of "Reload population figures" (it makes the action a POST)."""


class DatamartSyncForm(BaseDialogForm):
    scope = forms.ChoiceField(
        label=_("What to sync"),
        choices=(
            ("core", _("Partners and programme documents (a few minutes)")),
            (
                "all",
                _("Everything: funds, audits, reporting, monitoring and every other dataset (up to 2 hours)"),
            ),
            ("selected", _("Only the datasets ticked below, in their usual order")),
        ),
        initial="core",
        widget=UnfoldAdminRadioSelectWidget,
    )
    datasets = forms.MultipleChoiceField(
        label=_("Datasets"),
        required=False,
        choices=(),  # filled in __init__: the sync registry is not imported at module load
        widget=UnfoldAdminCheckboxSelectMultipleWidget,
        help_text=_("For a single dataset such as locations or partners; the rest is left as it is."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS

        self.fields["datasets"].choices = [(name, name) for name in ENTITY_SYNCS]

    def clean(self):
        data = super().clean()
        if data.get("scope") == "selected" and not data.get("datasets"):
            self.add_error("datasets", _("Tick at least one dataset."))
        return data

    def only(self) -> list[str] | None:
        """The ``--only`` datasets of the chosen scope; ``None`` for everything."""
        from neurodb.integrations.management.commands.sync_etools_datamart import CORE

        scope = self.cleaned_data["scope"]
        if scope == "core":
            return list(CORE)
        if scope == "selected":
            return list(self.cleaned_data["datasets"])
        return None


class StopRunForm(BaseDialogForm):
    """Nothing to fill in: the dialog asks for confirmation."""


@admin.register(SyncRun)
class SyncRunAdmin(JobActionsMixin, ReadOnlyModelAdmin):
    # "Sync eTools Datamart now" (the nightly sync) stays a button of its own; every other command
    # an operator may need is in the "Run a job" menu (admin_jobs.py).
    actions_list = ["sync_etools_datamart", JobActionsMixin.JOB_MENU]
    actions_detail = ["stop_run", "sync_dataset_again"]
    list_before_template = "admin/core/syncrun/etools_syncs.html"

    list_display = (
        "job",
        "target",
        "status_badge",
        "started_at",
        "duration_display",
        "rows_written",
        "rows_failed",
        "triggered_by_display",
        "error_short",
    )
    list_filter = ("job", "status", "started_at", "triggered_by")
    search_fields = ("target", "error", "triggered_by")
    date_hierarchy = "started_at"
    ordering = ("-started_at",)
    # every field, with what a probe found (its plain-words verdict) just before the details it comes from
    readonly_fields = tuple(
        name
        for f in SyncRun._meta.fields
        for name in (("probe_verdict", f.name) if f.name == "details" else (f.name,))
    ) + (
        "error_meaning",
        "duration",
        "errors_display",
    )
    list_per_page = 50

    @admin.display(description=_("Triggered by"), ordering="triggered_by")
    def triggered_by_display(self, obj):
        return triggered_by_label(obj.triggered_by)

    @admin.display(description=_("What the probe found"))
    def probe_verdict(self, obj):
        """A probe's verdict (``details.verdict``: what the Datamart's answers mean and what to do), in plain
        words; "—" for any other run."""
        return _probe_verdict(obj) or "—"

    @admin.display(description=_("What the error means"))
    def error_meaning(self, obj):
        return " ".join(filter(None, (str(error_hint(obj)), dataset_note(obj)))) or "—"

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        return badge(
            obj.get_status_display(),
            {"succeeded": "ok", "partial": "warn", "failed": "bad", "running": "info"}.get(obj.status),
        )

    @admin.display(description=_("Duration"))
    def duration_display(self, obj):
        d = obj.duration
        if not d:
            return "—"
        seconds = int(d.total_seconds())
        return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"

    @admin.display(description=_("Why rows failed"))
    def errors_display(self, obj):
        errors = (obj.details or {}).get("errors") or []
        if not errors:
            return "—"
        items = format_html_join(
            "",
            "<li><strong>{} ×</strong> {}<br><small>{}: {}</small></li>",
            (
                (e["count"], e["error"], _("examples"), ", ".join(str(x) for x in e["examples"]))
                for e in sorted(errors, key=lambda e: -e["count"])
            ),
        )
        other = obj.details.get("other_errors")
        tail = format_html("<li>{}</li>", _("%(n)s more with other messages") % {"n": other}) if other else ""
        return format_html('<ul class="nd-error-list">{}{}</ul>', items, tail)

    @admin.display(description=_("Error"))
    def error_short(self, obj):
        verdict = _probe_verdict(obj)
        if verdict and not obj.error:  # a probe that was answered: what it found, in the list
            return format_html('<span class="nd-error-hint">{}</span>', verdict)
        raw = (obj.error[:90] + "…") if len(obj.error) > 90 else (obj.error or "")
        hint = error_hint(obj)
        if hint:
            return format_html(
                '<span class="nd-error-hint">{}</span><small class="text-subtle">{}</small>', hint, raw
            )
        return raw

    def has_run_sync_permission(self, request):
        from neurodb.accounts.roles import ADMIN, role_of

        return request.user.is_superuser or role_of(request.user) == ADMIN

    def has_stop_run_permission(self, request, object_id=None):
        return self.has_run_sync_permission(request)

    def has_sync_dataset_again_permission(self, request, object_id=None):
        """An administrator, on the page of a finished eTools Datamart run of a dataset the sync still has
        (Unfold asks both when it draws the button and when it is posted)."""
        if not self.has_run_sync_permission(request):
            return False
        if object_id is None:
            return True
        from neurodb.integrations.etools.datamart_sync import ENTITY_SYNCS

        try:
            run = SyncRun.objects.filter(pk=object_id).first()
        except (ValueError, ValidationError):  # not a run's id: the change page says it does not exist
            return False
        return bool(
            run
            and run.job == SyncRun.Job.ETOOLS_DATAMART
            and run.target in ENTITY_SYNCS
            and run.status != SyncRun.Status.RUNNING
        )

    @action(
        description=_("Stop"),
        url_path="stop",
        permissions=["stop_run"],
        icon="stop_circle",
        dialog={
            "title": _("Stop this run"),
            "description": _(
                "Marks the run as stopped, so the job can be started again. A job waiting for another "
                "system (the Compiler/BMA calculations) quits within a minute; any other job finishes the "
                "work it is doing in the background, without changing this run's status."
            ),
            "form_class": StopRunForm,
            "form_submit_text": _("Stop"),
        },
    )
    def stop_run(self, request, form, object_id):
        run = SyncRun.objects.filter(pk=object_id).first()
        if run is None or not run.stop(request.user.get_username()):
            messages.warning(request, _("This run is not running."))
        else:
            messages.success(request, _("Stopped. The job can be started again."))
        url = reverse("admin:core_syncrun_change", args=[object_id])
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)

    @action(
        description=_("Sync eTools Datamart now"),
        url_path="sync-etools-datamart",
        permissions=["run_sync"],
        icon="sync",
        dialog={
            "title": _("Sync from the eTools Datamart"),
            "description": _(
                "Runs the nightly eTools sync now, in the background: funds, partners, programme "
                "documents, audits and monitoring, which feed the dashboards and the donor page. Each "
                "dataset appears in this list as it runs; refresh the page to follow it."
            ),
            "form_class": DatamartSyncForm,
            "form_submit_text": _("Start"),
        },
    )
    def sync_etools_datamart(self, request, form):
        return _start_datamart(request, form.only())

    @action(
        description=_("Sync this dataset again"),
        url_path="sync-dataset-again",
        permissions=["sync_dataset_again"],
        icon="sync",
        dialog={
            "title": _("Sync this dataset again"),
            "description": _(
                "Runs the eTools Datamart sync of this dataset alone, now, in the background; the other "
                "datasets are left as they are. Its new run appears in the list of runs."
            ),
            "form_class": StopRunForm,
            "form_submit_text": _("Start"),
        },
    )
    def sync_dataset_again(self, request, form, object_id):
        run = SyncRun.objects.get(pk=object_id)  # the permission check found it
        return _start_datamart(request, [run.target])


def _start_datamart(request, only: list[str] | None):
    """Start the eTools Datamart sync of ``only`` (``None``: every dataset) in the background, unless its
    credentials are missing or a sync is running, and go back to the list of runs."""
    from neurodb.integrations import background
    from neurodb.integrations.etools.datamart import configured

    if not configured():
        messages.error(
            request,
            _(
                "The eTools Datamart credentials are not set: add ETOOLS_USERNAME and ETOOLS_PASSWORD "
                "to the application settings (Key Vault secrets etools-username and etools-password)."
            ),
        )
    elif background.is_running(SyncRun.Job.ETOOLS_DATAMART):
        messages.warning(request, _("An eTools Datamart sync is already running."))
    else:
        args = ["sync_etools_datamart", "--triggered-by", request.user.get_username()]
        if only is not None:
            args += ["--only", ",".join(only)]
        background.start_command(*args)
        if background.datamart_cleanup_is_running():  # the sync waits for it (same lock)
            messages.info(
                request,
                _(
                    "The eTools update clean-up is running: the sync starts as soon as it has "
                    "finished. Refresh this page to follow it."
                ),
            )
        else:
            messages.success(request, _("eTools Datamart sync started. Refresh this page to follow it."))
    url = reverse("admin:core_syncrun_changelist")
    if request.headers.get("HX-Request"):  # the dialog posts with HTMX: redirect the whole page
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


@admin.register(ActivityInfoSetting)
class ActivityInfoSettingAdmin(ModelAdmin):
    """Admin → Reporting setup → "ActivityInfo: in use or archive": one row, Automatic until an administrator
    sets "In use" or "Archived". The page says what Automatic gives now and why
    (:func:`neurodb.indicators.services.activityinfo.explain`)."""

    fields = ("mode", "now", "years_with_records", "import_state", "updated_by", "updated_at")
    readonly_fields = ("now", "years_with_records", "import_state", "updated_by", "updated_at")
    radio_fields = {"mode": admin.VERTICAL}

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        setting = ActivityInfoSetting.load()  # the one row: open it
        return redirect(reverse("admin:core_activityinfosetting_change", args=[setting.pk]))

    def save_model(self, request, obj, form, change):
        from neurodb.indicators.services import activityinfo, navigation

        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
        activityinfo.invalidate()
        navigation.invalidate()

    def _state(self):
        """The state read from the database for this page (once per request), not the one this worker
        keeps: another worker may have saved the mode a moment ago, and this page explains what is stored."""
        from neurodb.indicators.services import activityinfo

        return activityinfo.refreshed()

    def _explained(self) -> list[str]:
        from neurodb.indicators.services import activityinfo

        return activityinfo.explain(self._state())

    @admin.display(description=_("Now"))
    def now(self, obj):
        return self._explained()[0]

    @admin.display(description=_("Years with ActivityInfo records"))
    def years_with_records(self, obj):
        return self._state().span or _("none")  # the label says the rest

    @admin.display(description=_("ActivityInfo import"))
    def import_state(self, obj):
        return format_html_join(" ", "{}", ((line,) for line in self._explained()[2:]))


@admin.register(DatamartSetting)
class DatamartSettingAdmin(ModelAdmin):
    """Admin → Data and sync → "eTools Datamart: PRP business area code": one row, empty until an
    administrator enters the code. The page says which code the next sync of the PRP datasets takes, the
    codes NeuroDB finds by itself (with *Use 2490* to enter the one found), and what their latest runs used
    and found (counts only)."""

    fields = ("prp_business_area", "next_sync", "found_codes", "latest_runs", "updated_by", "updated_at")
    readonly_fields = ("next_sync", "found_codes", "latest_runs", "updated_by", "updated_at")
    # *Use 2490* posts a form of its own, after the change form: a submit button inside the change form would
    # be its default one, and Enter in the code field would save the code found instead of the one typed
    change_form_outer_after_template = "admin/core/datamartsetting/use_found_code.html"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_fields(self, request, obj=None):
        """The codes NeuroDB finds come with *Use 2490* only for someone who may change the code."""
        fields = super().get_fields(request, obj)
        if self.has_change_permission(request, obj):
            return fields
        return [("codes_found" if name == "found_codes" else name) for name in fields]

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        setting = DatamartSetting.load()  # the one row: open it
        return redirect(reverse("admin:core_datamartsetting_change", args=[setting.pk]))

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def get_urls(self):
        return [
            path(
                "<path:object_id>/use-found-code/",
                self.admin_site.admin_view(self.use_found_code),
                name="core_datamartsetting_use_found_code",
            ),
            *super().get_urls(),
        ]

    def use_found_code(self, request, object_id):
        """*Use 2490*: enter the code NeuroDB finds (the workspace it holds, else the partner report rows),
        derived again here, never read from the request. POST only, with the change permission."""
        from neurodb.integrations.etools.datamart_sync import found_business_area

        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        setting = self.get_object(request, object_id)
        if setting is None:
            raise Http404
        if not self.has_change_permission(request, setting):
            raise PermissionDenied
        change = reverse("admin:core_datamartsetting_change", args=[setting.pk])
        code = found_business_area().code
        if not code:
            messages.warning(
                request,
                _(
                    "NeuroDB finds no code in the workspace it holds or on the partner report rows: nothing "
                    "was changed."
                ),
            )
            return redirect(change)
        setting.prp_business_area, setting.updated_by = code, request.user
        setting.save(update_fields=["prp_business_area", "updated_by", "updated_at"])
        self.log_change(request, setting, [{"changed": {"fields": ["prp_business_area"]}}])
        messages.success(request, _("%(code)s is now the code entered here.") % {"code": code})
        return redirect(change)

    @admin.display(description=_("The next sync takes"))
    def next_sync(self, obj):
        from neurodb.integrations.etools import datamart_sync as sync
        from neurodb.reports.services import BUSINESS_AREA_FROM

        choice = sync.resolve_business_area()  # without the Datamart's workspaces: a page asks nothing
        hosting, entered = choice.set_aside.get(sync.FROM_ENVIRONMENT), choice.set_aside.get(sync.FROM_ADMIN)
        if choice.source == sync.FROM_ADMIN:
            said = _("%(code)s, the code entered here.") % {"code": choice.code}
            other = sync.area_code(settings.ETOOLS_DATAMART_BUSINESS_AREA)
            if sync.is_area_code(other) and other != choice.code:
                said += " " + _(
                    "The hosting team's ETOOLS_DATAMART_BUSINESS_AREA (%(other)s) is not used while a code "
                    "is entered here."
                ) % {"other": other}
            elif hosting:
                said += " " + _(
                    "The hosting team's ETOOLS_DATAMART_BUSINESS_AREA (“%(value)s”) is %(words)s: it is not "
                    "used."
                ) % {"value": hosting, "words": sync.not_a_code(hosting)}
            return said
        if choice.source == sync.FROM_ENVIRONMENT:
            return _(
                "%(code)s, set by the hosting team (ETOOLS_DATAMART_BUSINESS_AREA). A code entered here "
                "comes first."
            ) % {"code": choice.code}
        here = (
            _("the code entered here, “%(value)s”, is %(words)s")
            % {"value": entered, "words": sync.not_a_code(entered)}
            if entered
            else _("nothing is entered here")
        )
        if choice.code:  # found by NeuroDB
            where = str(BUSINESS_AREA_FROM.get(choice.source, ""))
            if choice.source == sync.FROM_PARTNER_REPORTS:
                n, held = choice.rows
                where += " " + _("on %(n)s of %(held)s rows") % {"n": f"{n:,}", "held": f"{held:,}"}
            set_by = (
                _(", and the hosting team's ETOOLS_DATAMART_BUSINESS_AREA, “%(value)s”, is %(words)s")
                % {"value": hosting, "words": sync.not_a_code(hosting)}
                if hosting
                else ""
            )
            return _(
                "%(code)s, %(where)s (%(here)s%(hosting)s). Enter it here, or press Use %(code)s below, to "
                "keep it whatever the Datamart sends."
            ) % {"code": choice.code, "where": where, "here": here, "hosting": set_by}
        set_by = (
            _("the hosting team set “%(value)s”, %(words)s")
            % {"value": hosting, "words": sync.not_a_code(hosting)}
            if hosting
            else _("the hosting team set none")
        )
        return _(
            "No code: %(here)s, %(hosting)s, the workspace NeuroDB holds has none and the partner report "
            "rows carry none. The next sync of a PRP dataset asks the Datamart's list of workspaces; if "
            "that has none either, the sync fails, saying so."
        ) % {"here": here, "hosting": set_by}

    @admin.display(description=_("Codes NeuroDB finds"))
    def codes_found(self, obj):
        """What NeuroDB finds, for someone who may only view the page: no button."""
        return self.found_codes(obj, button=False)

    @admin.display(description=_("Codes NeuroDB finds"))
    def found_codes(self, obj, button=True):
        from neurodb.datamart.models import DatamartDocument
        from neurodb.integrations.etools import datamart_sync as sync

        held = sync.held_business_area(settings.ETOOLS_DATAMART_COUNTRY)
        if held:
            workspaces = DatamartDocument.objects.filter(dataset="workspaces").order_by("-synced_at")
            synced = workspaces.values_list("synced_at", flat=True).first()
            when = formats.date_format(timezone.localtime(synced), "j M Y") if synced else ""
            in_workspace = (
                _("In the workspace NeuroDB holds: %(code)s (synced %(when)s).")
                % {"code": held, "when": when}
                if when
                else _("In the workspace NeuroDB holds: %(code)s.") % {"code": held}
            )
        else:
            in_workspace = _("In the workspace NeuroDB holds: none.")
        code, n, rows = sync.reported_business_area()
        on_rows = (
            _("On the partner report rows held: %(code)s on %(n)s of %(held)s.")
            % {"code": code, "n": f"{n:,}", "held": f"{rows:,}"}
            if code
            else _("On the partner report rows held: none.")
        )
        found = held or code  # what Use … enters: the workspace held first
        entered = sync.area_code(obj.prp_business_area) if obj else ""
        if not button or not found or found == entered:
            return format_html("{}<br>{}", in_workspace, on_rows)
        # the button belongs to the form after the change form (use_found_code.html), never to this one
        return format_html(
            '{}<br>{}<br><button type="submit" form="use_found_code_form" class="btn btn-outline-secondary '
            'btn-sm mt-2" name="use_found_code" value="1">{}</button>',
            in_workspace,
            on_rows,
            _("Use %(code)s") % {"code": found},
        )

    @admin.display(description=_("Latest runs of the PRP datasets"))
    def latest_runs(self, obj):
        from neurodb.datamart.catalogue import DOCUMENTS
        from neurodb.integrations.etools.datamart_sync import is_area_code, not_a_code
        from neurodb.reports.services import BUSINESS_AREA_FROM, latest_datamart_runs

        names = [name for name, spec in DOCUMENTS.items() if spec.scope == "business_area"]
        runs = latest_datamart_runs(names, finished=True)
        items = []
        for name in names:
            run = runs.get(name)
            if run is None:
                items.append(format_html('<li><span class="mono">{}</span>: {}</li>', name, _("not synced")))
                continue
            details = run.details or {}
            if run.status == SyncRun.Status.FAILED:
                said = run.error[:200]
            elif details.get("business_area"):
                code = str(details["business_area"])
                found = {
                    "code": code,
                    "source": BUSINESS_AREA_FROM.get(details.get("business_area_from"), ""),
                    "kept": run.rows_in,
                    "read": details.get("read", run.rows_in),
                }
                if is_area_code(code):
                    said = _("business area %(code)s (%(source)s): %(kept)s kept of %(read)s read") % found
                else:  # asked before the sync set such a value aside
                    said = format_html(
                        '<span class="text-warning">{}</span>',
                        _("business area %(code)s (%(source)s), %(words)s: %(kept)s kept of %(read)s read")
                        % {**found, "words": not_a_code(code)},
                    )
                aside = details.get("business_area_set_aside")
                if isinstance(aside, dict) and aside.get("environment"):
                    said = format_html(
                        "{} · {}",
                        said,
                        _("the hosting team's “%(value)s” set aside") % {"value": aside["environment"]},
                    )
                unnamed = details.get("country_unnamed")
                if isinstance(unnamed, int) and unnamed:  # kept, but nothing confirms the country
                    said = format_html(
                        '{} · <span class="text-warning">{}</span>',
                        said,
                        _(
                            "%(n)s named no business area, kept unconfirmed (the Datamart may ignore the "
                            "filter): press Run a job → Probe a Datamart dataset on Import and sync runs"
                        )
                        % {"n": unnamed},
                    )
            else:  # a run made before the code was recorded
                said = _("%(kept)s kept") % {"kept": run.rows_in}
            when = formats.date_format(timezone.localtime(run.started_at), "DATETIME_FORMAT")
            items.append(
                format_html(
                    '<li><span class="mono">{}</span>: <a href="{}">{}, {}</a> · {}</li>',
                    name,
                    reverse("admin:core_syncrun_change", args=[run.pk]),
                    run.get_status_display(),
                    when,
                    said,
                )
            )
        listed = format_html_join("", "{}", ((item,) for item in items))
        return format_html('<ul class="nd-error-list">{}</ul>', listed)


@admin.register(SavedView)
class SavedViewAdmin(ModelAdmin):
    list_display = ("name", "owner", "page", "object_id", "is_shared", "updated_at")
    list_filter = ("page", "is_shared")
    search_fields = ("name", "owner__username")
    list_select_related = ("owner",)
    autocomplete_fields = ("owner",)


@admin.register(PopulationFigure)
class PopulationFigureAdmin(ModelAdmin):
    actions_list = ["reload_bundled"]
    list_display = ("year", "category", "nationality", "level", "area_name", "age_group", "sex", "value")
    list_filter = ("year", "category", "nationality", "level")
    search_fields = ("area_name", "area_code")
    list_per_page = 100

    def has_reload_population_permission(self, request):
        from neurodb.accounts.roles import ADMIN, role_of

        return request.user.is_superuser or role_of(request.user) == ADMIN

    @action(
        description=_("Reload population figures"),
        url_path="reload-population",
        permissions=["reload_population"],
        icon="refresh",
        dialog={
            "title": _("Reload population figures"),
            "description": _(
                "Reloads every year from the files shipped with NeuroDB, replacing their total and "
                "children figures. Vulnerable population figures entered here are kept. It takes a few "
                "seconds."
            ),
            "form_class": ReloadPopulationForm,
            "form_submit_text": _("Reload"),
        },
    )
    def reload_bundled(self, request, form):
        from neurodb.core.management.commands.load_population_figures import reload_bundled

        try:
            lines = reload_bundled(triggered_by=request.user.get_username())
        except Exception as exc:  # the run is recorded as failed on Data health; say so here too
            messages.error(request, _("The reload failed: %(error)s") % {"error": exc})
        else:
            messages.success(request, "; ".join(lines) or _("No population file is shipped with NeuroDB."))
        url = reverse("admin:core_populationfigure_changelist")
        if request.headers.get("HX-Request"):  # the dialog posts with HTMX: redirect the whole page
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


@admin.register(LogEntry)
class AuditTrailAdmin(ReadOnlyModelAdmin):
    """Every change made in the admin: who, when, what (Django records it; this makes it browsable)."""

    list_display = (
        "action_time",
        "user",
        "action_badge",
        "content_type",
        "object_link",
        "change_message_text",
    )
    list_filter = ("action_flag", "user")
    search_fields = ("object_repr", "change_message", "user__username")
    date_hierarchy = "action_time"
    list_select_related = ("user", "content_type")
    ordering = ("-action_time",)
    list_per_page = 50

    def changelist_view(self, request, extra_context=None):
        extra_context = {"title": _("Audit trail: who changed what in the admin"), **(extra_context or {})}
        return super().changelist_view(request, extra_context)

    def has_view_permission(self, request, obj=None):
        return (
            request.user.is_active and request.user.is_superuser or super().has_view_permission(request, obj)
        )

    @admin.display(description=_("Action"), ordering="action_flag")
    def action_badge(self, obj):
        text, tone = {
            ADDITION: (_("Added"), "ok"),
            CHANGE: (_("Changed"), "info"),
            DELETION: (_("Deleted"), "bad"),
        }[obj.action_flag]
        return badge(text, tone)

    @admin.display(description=_("Object"))
    def object_link(self, obj):
        if obj.action_flag != DELETION:
            try:
                url = obj.get_admin_url()
            except NoReverseMatch:
                url = None
            if url:
                return format_html('<a href="{}">{}</a>', url, obj.object_repr)
        return obj.object_repr

    @admin.display(description=_("Change"))
    def change_message_text(self, obj):
        return obj.get_change_message()


class ScheduledJobForm(forms.ModelForm):
    command = forms.ChoiceField(label=_("Runs"), choices=())

    class Meta:
        model = ScheduledJob
        fields = ("key", "command", "schedule", "enabled")

    def __init__(self, *args, **kwargs):
        from .jobs import COMMAND_CHOICES

        super().__init__(*args, **kwargs)
        self.fields["command"].choices = COMMAND_CHOICES

    def clean_schedule(self):
        from .cron import CronError, next_after

        schedule = " ".join(self.cleaned_data["schedule"].split())
        try:
            next_after(schedule, timezone.now())
        except CronError as exc:
            raise forms.ValidationError(str(exc)) from None
        return schedule


def _forget_unknown_command(job: ScheduledJob) -> None:
    """A person switched the job on or edited it: it runs at its next time. A note left by a release
    that did not know its command would otherwise make the scheduler start it at once (its catch-up
    after a deployment, ``neurodb.core.scheduler.tick``)."""
    from .jobs import UNKNOWN_COMMAND

    if job.last_outcome.startswith(UNKNOWN_COMMAND):
        job.last_outcome = ""


class RunScheduledJobForm(BaseDialogForm):
    """The confirmation step of "Run now" on a scheduled job, and of "Run due jobs now"."""


@admin.register(ScheduledJob)
class ScheduledJobAdmin(ModelAdmin):
    """Admin → Scheduled jobs: what runs when (Beirut time), switched on and off here."""

    form = ScheduledJobForm
    list_before_template = "admin/core/scheduledjob/scheduler_status.html"
    list_display = (
        "key",
        "runs",
        "when",
        "enabled",
        "next_run",
        "last_run",
    )
    list_editable = ("enabled",)  # the switch saves at once (scheduler_status.html)
    list_filter = ("enabled",)
    actions = ["enable_jobs", "disable_jobs"]
    actions_list = ["run_due_jobs"]
    actions_row = ["run_now"]
    readonly_fields = ("next_run_at", "last_started_at", "start_attempt", "updated_by", "updated_at")

    @admin.display(description=_("Next run at"), ordering="next_run_at")
    def next_run(self, obj):
        if not obj.next_run_at:
            return "—"
        when = formats.date_format(timezone.localtime(obj.next_run_at), "DATETIME_FORMAT")
        if is_overdue(obj):
            return format_html("{}<br>{}", badge(_("Overdue"), "bad"), when)
        return when

    @admin.display(description=_("Scheduler's last start"))
    def start_attempt(self, obj):
        return obj.last_outcome or _("The scheduler has not started this job yet.")

    @admin.display(description=_("Runs"), ordering="command")
    def runs(self, obj):
        from .jobs import COMMANDS

        command = COMMANDS.get(obj.command)
        return command.label if command else obj.command

    @admin.display(description=_("When (Beirut time)"), ordering="schedule")
    def when(self, obj):
        from .cron import describe

        text = describe(obj.schedule)
        if text == obj.schedule:
            return format_html("<code>{}</code>", obj.schedule)
        return format_html("{}<br><code class='text-xs'>{}</code>", text, obj.schedule)

    @admin.display(description=_("Last run"))
    def last_run(self, obj):
        from .jobs import COMMANDS

        command = COMMANDS.get(obj.command)
        if not command or not command.sync_job:
            text = format_html("{}", naturaltime(obj.last_started_at)) if obj.last_started_at else None
        else:
            run = SyncRun.objects.filter(job=command.sync_job).order_by("-started_at").first()
            text = None
            if run:
                url = reverse("admin:core_syncrun_change", args=[run.pk])
                label = badge(run.get_status_display())
                if run.job == SyncRun.Job.ETOOLS_DATAMART and run.status != SyncRun.Status.RUNNING:
                    # one run per dataset: the latest is only the last dataset of the order; read them all
                    from neurodb.reports.services import datamart_sync_state

                    state = datamart_sync_state()
                    if state.status:  # else no dataset counted has finished: the run as it is
                        tone = {"succeeded": "ok", "partial": "warn", "failed": "bad"}[state.status]
                        label = badge(state.label, tone)
                        url = state.runs_url()
                text = format_html('<a href="{}">{}</a> {}', url, label, naturaltime(run.started_at))
        if text is None:
            text = badge(_("Never run"), "muted")
        # The scheduler's own note, only when it could not start the job (it says "started" otherwise).
        problem = obj.last_outcome if obj.last_outcome.startswith(("skipped", "not started", "error")) else ""
        if problem:
            return format_html('{}<br><small class="text-subtle">{}</small>', text, problem)
        return text

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.get_username()
        obj.plan_next()  # a new or edited schedule counts from now
        _forget_unknown_command(obj)
        super().save_model(request, obj, form, change)

    def changelist_view(self, request, extra_context=None):
        seen, host, alive = scheduler_heartbeat()
        extra_context = {
            "scheduler_alive": alive,
            "scheduler_seen": seen,
            "scheduler_host": host,
            "scheduler_enabled": settings.SCHEDULER_ENABLED,
            "support_email": settings.SUPPORT_EMAIL,
            **(extra_context or {}),
        }
        return super().changelist_view(request, extra_context)

    @admin.action(description=_("Switch on the selected jobs"))
    def enable_jobs(self, request, queryset):
        for job in queryset:
            job.enabled = True
            job.plan_next()
            _forget_unknown_command(job)
            job.updated_by = request.user.get_username()
            job.save()
        self.message_user(request, _("Switched on."), messages.SUCCESS)

    @admin.action(description=_("Switch off the selected jobs"))
    def disable_jobs(self, request, queryset):
        from neurodb.indicators.services import activityinfo

        queryset.update(enabled=False, next_run_at=None, updated_by=request.user.get_username())
        activityinfo.invalidate()  # an update() sends no signal: the ActivityInfo import may be among them
        self.message_user(request, _("Switched off."), messages.SUCCESS)

    def has_run_now_permission(self, request):
        from neurodb.accounts.roles import ADMIN, role_of

        return request.user.is_superuser or role_of(request.user) == ADMIN

    @action(
        description=_("Run due jobs now"),
        url_path="run-due-jobs",
        permissions=["run_now"],
        icon="play_circle",
        dialog={
            "title": _("Run the jobs that are due"),
            "description": _(
                "Does what the scheduler does every 30 seconds, once: starts, in the background, every "
                "switched-on job whose time has passed. Use it while the scheduler is not checking in. "
                "Follow the runs in Import and sync runs."
            ),
            "form_class": RunScheduledJobForm,
            "form_submit_text": _("Run due jobs"),
        },
    )
    def run_due_jobs(self, request, form):
        from neurodb.integrations import background

        from . import scheduler
        from .models import SchedulerState

        seen, host, alive = scheduler_heartbeat()
        if alive and background.lock_is_held(scheduler.LOCK_ID):
            messages.info(
                request, _("The scheduler is running: due jobs start on their own within a minute.")
            )
        else:
            started = scheduler.tick()
            # tick() records a check-in; this was a person, not the scheduler: keep the banner truthful.
            if seen is None:
                SchedulerState.objects.filter(pk=1).delete()
            else:
                SchedulerState.objects.filter(pk=1).update(last_seen_at=seen, host=host)
            if started:
                messages.success(
                    request,
                    _("Started: %(jobs)s. Follow them in Import and sync runs.")
                    % {"jobs": ", ".join(started)},
                )
            else:
                messages.info(request, _("No job was due: nothing was started."))
        url = reverse("admin:core_scheduledjob_changelist")
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)

    @action(
        description=_("Run now"),
        url_path="run-now",
        permissions=["run_now"],
        icon="play_arrow",
        dialog={
            "title": _("Run this job now"),
            "description": _(
                "Starts the job's command in the background, as its schedule would. Its schedule does "
                "not change. Follow the run in Import and sync runs."
            ),
            "form_class": RunScheduledJobForm,
            "form_submit_text": _("Run"),
        },
    )
    def run_now(self, request, form, object_id):
        from .jobs import COMMANDS, start

        job = ScheduledJob.objects.filter(pk=object_id).first()
        if job is None or job.command not in COMMANDS:
            messages.error(request, _("This job cannot run: its command is unknown."))
        elif start(job.command, triggered_by=request.user.get_username()) == "running":
            messages.warning(request, _("%(job)s is already running.") % {"job": job.key})
        else:
            job.last_started_at = timezone.now()
            job.last_outcome = f"started by {request.user.get_username()}"
            job.save(update_fields=["last_started_at", "last_outcome"])
            message = _("%(job)s started. Follow it in Import and sync runs.") % {"job": job.key}
            messages.success(request, message)
        url = reverse("admin:core_scheduledjob_changelist")
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)
