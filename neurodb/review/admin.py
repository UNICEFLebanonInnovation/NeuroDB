"""The daily reviews in the admin: read-only, with their findings, and a button to run one now; and the
daily review's checks, each in trial or on, switched on by an administrator."""

import datetime
from urllib.parse import urlencode

from django.contrib import admin, messages
from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.text import Truncator
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action
from unfold.forms import BaseDialogForm

from neurodb.core.admin import triggered_by_label
from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from . import checks
from .models import CheckSetting, DailyReview, FindingAssignment, ReviewFinding

MISSING_DAYS_LOOKBACK = 14  # the list flags the days without a review in this many past days


class RunReviewForm(BaseDialogForm):
    """The confirmation step of "Run the daily review now" (it makes the action a POST)."""


def assignment_link(finding: ReviewFinding):
    """ "Assign" opens a new assignment for the finding's key (prefilled), or the existing one."""
    existing = FindingAssignment.objects.filter(key=finding.key).only("id", "owner", "status").first()
    if existing is not None:
        url = reverse("admin:review_findingassignment_change", args=[existing.pk])
        text = f"{existing.owner or '—'} · {existing.get_status_display()}"
        return format_html('<a href="{}">{}</a>', url, text)
    url = reverse("admin:review_findingassignment_add")
    params = urlencode({"key": finding.key, "title": finding.title, "section": finding.section})
    return format_html('<a href="{}?{}">{}</a>', url, params, _("Assign"))


class ReviewFindingInline(TabularInline):
    model = ReviewFinding
    fields = (
        "rank",
        "severity",
        "state",
        "check_id",
        "trial",
        "section",
        "title",
        "children",
        "url",
        "owner",
    )
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description=_("Owner"))
    def owner(self, obj):
        return assignment_link(obj)


@admin.register(DailyReview)
class DailyReviewAdmin(ReadOnlyModelAdmin):
    actions_list = ["run_review_now"]
    list_before_template = "admin/review/dailyreview/missing_days.html"
    list_display = (
        "date",
        "status_badge",
        "summary_preview",
        "findings_count",
        "narrated_by_display",
        "duration_display",
        "triggered_by_display",
    )
    list_filter = ("status", "narrated_by")
    date_hierarchy = "date"
    ordering = ("-date",)
    readonly_fields = tuple(f.name for f in DailyReview._meta.fields)
    inlines = [ReviewFindingInline]

    @admin.display(description=_("Status"), ordering="status")
    def status_badge(self, obj):
        return badge(
            obj.get_status_display(), {"succeeded": "ok", "failed": "bad", "running": "info"}.get(obj.status)
        )

    @admin.display(description=_("Summary"))
    def summary_preview(self, obj):
        text = obj.summary or obj.error
        if not text:
            return "—"
        first = text.split(". ", 1)[0]  # the first sentence, at most two lines of the list
        return format_html('<span class="nd-summary-preview">{}</span>', Truncator(first).chars(160))

    @admin.display(description=_("Findings"))
    def findings_count(self, obj):
        return obj.findings.count()

    @admin.display(description=_("Narrated by"), ordering="narrated_by")
    def narrated_by_display(self, obj):
        if obj.narrated_by == DailyReview.TEMPLATE:
            return format_html(
                '<span title="{}">{}</span>',
                _(
                    "The AI assistant is switched off or did not answer, so the summary was written from a "
                    "standard template. The checks and findings are the same."
                ),
                _("AI off: standard summary"),
            )
        return obj.narrated_by or "—"

    @admin.display(description=_("Triggered by"), ordering="triggered_by")
    def triggered_by_display(self, obj):
        return triggered_by_label(obj.triggered_by)

    def changelist_view(self, request, extra_context=None):
        today = timezone.localdate()
        start = today - datetime.timedelta(days=MISSING_DAYS_LOOKBACK)
        done = set(
            DailyReview.objects.filter(date__gte=start, status=DailyReview.Status.SUCCEEDED).values_list(
                "date", flat=True
            )
        )
        first = DailyReview.objects.order_by("date").values_list("date", flat=True).first()
        missing = []
        if first:  # from the first review on: before it, the daily review did not exist yet
            day = max(start, first)
            while day < today:  # today's review may simply not have run yet
                if day not in done:
                    missing.append(day)
                day += datetime.timedelta(days=1)
        extra_context = {
            "missing_days": missing,
            "lookback_days": MISSING_DAYS_LOOKBACK,
            **(extra_context or {}),
        }
        return super().changelist_view(request, extra_context)

    @admin.display(description=_("Duration"))
    def duration_display(self, obj):
        return f"{obj.duration_ms / 1000:.1f} s" if obj.duration_ms else "—"

    def has_run_review_permission(self, request):
        from neurodb.accounts.roles import ADMIN, role_of

        return request.user.is_superuser or role_of(request.user) == ADMIN

    @action(
        description=_("Run the daily review now"),
        url_path="run-daily-review",
        permissions=["run_review"],
        icon="play_arrow",
        dialog={
            "title": _("Run the daily review now"),
            "description": _(
                "Runs the seventeen checks in the background and replaces today's review when it "
                "succeeds. It takes about a minute; refresh this page to see it."
            ),
            "form_class": RunReviewForm,
            "form_submit_text": _("Run"),
        },
    )
    def run_review_now(self, request, form):
        from neurodb.core.models import SyncRun
        from neurodb.integrations import background

        if background.is_running(SyncRun.Job.DAILY_REVIEW):
            messages.warning(request, _("A daily review is already running."))
        else:
            background.start_command("daily_review", "--triggered-by", request.user.get_username())
            messages.success(
                request,
                format_html(
                    '{} <a href="{}">{}</a>.',
                    _("The daily review started. Refresh this page in a minute to see it here or on"),
                    reverse("reports:daily_review"),
                    _("the Daily review page"),
                ),
            )
        url = reverse("admin:review_dailyreview_changelist")
        if request.headers.get("HX-Request"):  # the dialog posts with HTMX: redirect the whole page
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


@admin.register(ReviewFinding)
class ReviewFindingAdmin(ReadOnlyModelAdmin):
    list_display = (
        "review",
        "rank",
        "severity",
        "state",
        "check_id",
        "trial",
        "section",
        "title",
        "children",
        "owner",
    )
    list_filter = ("severity", "state", "check_id", "trial", "review__date")
    search_fields = ("title", "detail", "section", "key")
    list_per_page = 50

    @admin.display(description=_("Owner"))
    def owner(self, obj):
        return assignment_link(obj)


MODE_TONES = {CheckSetting.Mode.ON: "ok", CheckSetting.Mode.TRIAL: "info"}


@admin.register(CheckSetting)
class CheckSettingAdmin(ModelAdmin):
    """Each new check of the daily review starts in trial: its findings show on the Daily review page, marked
    Trial, and are not sent to NeuroDB Watch. Look at its findings, then switch it on: from the next review,
    its critical findings and warnings also go to NeuroDB Watch. The checks that ran before trials existed
    are on."""

    list_display = ("check_name", "mode_badge", "latest_findings", "on_since", "trial_since", "updated_at")
    list_filter = ("mode",)
    search_fields = ("check_id",)
    fields = ("check_id", "check_name", "mode", "on_since", "trial_since", "updated_by", "updated_at")
    readonly_fields = ("check_id", "check_name", "on_since", "trial_since", "updated_by", "updated_at")
    actions = ("switch_on", "back_to_trial")

    def has_add_permission(self, request):
        return False  # every check is listed from the start (checks.ensure_settings)

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        if request.method == "GET":
            checks.ensure_settings()
        return super().changelist_view(request, extra_context)

    @admin.display(description=_("Check"), ordering="check_id")
    def check_name(self, obj):
        return format_html(
            '{}<br><small class="text-font-subtle-light">{}</small>',
            checks.LABELS.get(obj.check_id, obj.check_id),
            obj.check_id,
        )

    @admin.display(description=_("Mode"), ordering="mode")
    def mode_badge(self, obj):
        return badge(obj.get_mode_display(), MODE_TONES.get(obj.mode))

    def get_queryset(self, request):
        """Each check with its open findings in the latest succeeded review (two queries in all)."""
        latest = (
            DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED)
            .order_by("-date")
            .values_list("pk", flat=True)
            .first()
        )
        found = (
            ReviewFinding.objects.filter(review_id=latest, check_id=OuterRef("check_id"))
            .exclude(state=ReviewFinding.State.RESOLVED)
            .order_by()
            .values("check_id")
            .annotate(n=Count("pk"))
            .values("n")
        )
        return (
            super()
            .get_queryset(request)
            .annotate(
                latest_review=Value(latest, output_field=IntegerField(null=True)),
                latest_open=Coalesce(Subquery(found, output_field=IntegerField()), 0),
            )
        )

    @admin.display(description=_("Findings in the latest review"))
    def latest_findings(self, obj):
        """Its open findings in the latest review, linked to them: what to look at before switching it on."""
        review = getattr(obj, "latest_review", None)
        if review is None:
            return "—"
        url = (
            reverse("admin:review_reviewfinding_changelist")
            + "?"
            + urlencode({"check_id__exact": obj.check_id, "review__id__exact": review})
        )
        return format_html('<a href="{}">{}</a>', url, getattr(obj, "latest_open", 0))

    def _set(self, request, queryset, mode: str) -> int:
        today = timezone.localdate()
        stamp = {"on_since": today} if mode == CheckSetting.Mode.ON else {"trial_since": today}
        return queryset.exclude(mode=mode).update(
            mode=mode, updated_by=request.user.get_username(), updated_at=timezone.now(), **stamp
        )

    @admin.action(
        description=_("Switch on: also send its findings to NeuroDB Watch"), permissions=("change",)
    )
    def switch_on(self, request, queryset):
        done = self._set(request, queryset, CheckSetting.Mode.ON)
        self.message_user(
            request,
            _("%(n)s check(s) switched on. Their findings go to NeuroDB Watch from the next daily review.")
            % {"n": done},
            messages.SUCCESS,
        )

    @admin.action(description=_("Put back in trial: the Daily review page only"), permissions=("change",))
    def back_to_trial(self, request, queryset):
        done = self._set(request, queryset, CheckSetting.Mode.TRIAL)
        self.message_user(
            request,
            _(
                "%(n)s check(s) put back in trial. From the next daily review, their findings stay on its "
                "page only."
            )
            % {"n": done},
            messages.SUCCESS,
        )

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.get_username()
        if "mode" in form.changed_data:  # an administrator decided: stamp the day again
            if obj.mode == CheckSetting.Mode.ON:
                obj.on_since = timezone.localdate()
            else:
                obj.trial_since = timezone.localdate()
        super().save_model(request, obj, form, change)


@admin.register(FindingAssignment)
class FindingAssignmentAdmin(ModelAdmin):
    """Who owns a finding and by when. Reached from the "Assign" link on a finding; also editable
    here. The owner is a role or a team; the lifecycle dates are stamped by the status."""

    list_display = ("title", "section", "owner", "status", "due_date", "updated_by", "updated_at")
    list_filter = ("status", "section", "owner")
    search_fields = ("title", "key", "owner", "note")
    readonly_fields = (
        "created_at",
        "acknowledged_at",
        "assigned_at",
        "closed_at",
        "updated_at",
        "updated_by",
    )
    fields = (
        "key",
        "title",
        "section",
        "owner",
        "due_date",
        "status",
        "note",
        "created_at",
        "acknowledged_at",
        "assigned_at",
        "closed_at",
        "updated_by",
        "updated_at",
    )
    list_per_page = 50

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        for name in ("key", "title", "section"):
            if request.GET.get(name):
                initial[name] = request.GET[name][:300]
        return initial

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user.get_username()
        super().save_model(request, obj, form, change)
