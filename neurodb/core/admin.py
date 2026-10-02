import datetime
import re

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.humanize.templatetags.humanize import naturaltime
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse
from django.utils import formats, timezone
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin
from unfold.decorators import action
from unfold.forms import BaseDialogForm
from unfold.widgets import UnfoldAdminCheckboxSelectMultipleWidget, UnfoldAdminRadioSelectWidget

from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from .admin_jobs import JobActionsMixin
from .models import PopulationFigure, SavedView, ScheduledJob, SyncRun

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
UNAVAILABLE = re.compile(r"\b(?:HTTP|returned) (?:5\d\d|429)\b")
REFUSED = re.compile(r"\b(?:HTTP|returned) (?:401|403)\b")
UNREACHABLE = re.compile(r"Timeout|ConnectionError|SSLError|timed out", re.IGNORECASE)


def error_hint(run: SyncRun) -> str:
    """What a known error means for the administrator, or "" when the raw error is all we know."""
    error = run.error or ""
    source = SOURCES.get(run.job) or _("The data source")
    if UNAVAILABLE.search(error):
        return _("%(source)s was unavailable: try again later.") % {"source": source}
    if UNREACHABLE.search(error):
        return _("%(source)s did not answer in time: try again later.") % {"source": source}
    if REFUSED.search(error):
        return _(
            "%(source)s refused NeuroDB's credentials: they may have expired. Ask the team that runs "
            "NeuroDB's hosting to renew them."
        ) % {"source": source}
    return ""


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
    actions_detail = ["stop_run"]
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
    readonly_fields = tuple(f.name for f in SyncRun._meta.fields) + (
        "error_meaning",
        "duration",
        "errors_display",
    )
    list_per_page = 50

    @admin.display(description=_("Triggered by"), ordering="triggered_by")
    def triggered_by_display(self, obj):
        return triggered_by_label(obj.triggered_by)

    @admin.display(description=_("What the error means"))
    def error_meaning(self, obj):
        return error_hint(obj) or "—"

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
            only = form.only()
            if only is not None:
                args += ["--only", ",".join(only)]
            background.start_command(*args)
            messages.success(request, _("eTools Datamart sync started. Refresh this page to follow it."))
        url = reverse("admin:core_syncrun_changelist")
        if request.headers.get("HX-Request"):  # the dialog posts with HTMX: redirect the whole page
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


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
                text = format_html(
                    '<a href="{}">{}</a> {}',
                    url,
                    badge(run.get_status_display()),
                    naturaltime(run.started_at),
                )
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
            job.updated_by = request.user.get_username()
            job.save()
        self.message_user(request, _("Switched on."), messages.SUCCESS)

    @admin.action(description=_("Switch off the selected jobs"))
    def disable_jobs(self, request, queryset):
        queryset.update(enabled=False, next_run_at=None, updated_by=request.user.get_username())
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
