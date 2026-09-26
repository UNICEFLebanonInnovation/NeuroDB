"""The daily reviews in the admin: read-only, with their findings, and a button to run one now."""

from django.contrib import admin, messages
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from unfold.admin import TabularInline
from unfold.decorators import action
from unfold.forms import BaseDialogForm

from neurodb.web.admin_helpers import ReadOnlyModelAdmin, badge

from .models import DailyReview, ReviewFinding


class RunReviewForm(BaseDialogForm):
    """The confirmation step of "Run the daily review now" (it makes the action a POST)."""


class ReviewFindingInline(TabularInline):
    model = ReviewFinding
    fields = ("rank", "severity", "state", "check_id", "section", "title", "children", "url")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(DailyReview)
class DailyReviewAdmin(ReadOnlyModelAdmin):
    actions_list = ["run_review_now"]
    list_display = (
        "date",
        "status_badge",
        "findings_count",
        "narrated_by",
        "duration_display",
        "triggered_by",
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

    @admin.display(description=_("Findings"))
    def findings_count(self, obj):
        return obj.findings.count()

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
                "Runs the fourteen checks in the background and replaces today's review when it "
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
            messages.success(request, _("The daily review started. Refresh this page in a minute to see it."))
        url = reverse("admin:review_dailyreview_changelist")
        if request.headers.get("HX-Request"):  # the dialog posts with HTMX: redirect the whole page
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)


@admin.register(ReviewFinding)
class ReviewFindingAdmin(ReadOnlyModelAdmin):
    list_display = ("review", "rank", "severity", "state", "check_id", "section", "title", "children")
    list_filter = ("severity", "state", "check_id", "review__date")
    search_fields = ("title", "detail", "section", "key")
    list_per_page = 50
