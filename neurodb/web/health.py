"""What needs an administrator's attention: one list for the admin home and for NeuroDB Watch.

The admin home's "Needs attention" box and NeuroDB Watch's system items (told to the administrators
only, see ``neurodb.watch.detectors.system``) read the same lines from :func:`warnings`, so the two
never disagree. Each line is a :class:`Warning` whose ``key`` stays the same from one day to the next
while the problem lasts (``no_current_year``, ``job_failed:etools_datamart``, ``scheduler_silent``...):
the watch follows it as the item ``system:<key>`` and closes it once the line goes. The lines:

- **set-up**: no reporting year marked as current; displayed ActivityInfo databases never imported or
  not imported for more than 40 days; active master indicators without a target; active users
  without a role;
- **jobs**: the last run of a job failed or lost rows. A Compiler job counts only while a switched-on
  schedule runs it: the Compiler reads are off by default, and a trial run that failed should not
  stay listed;
- **the schedule**: the scheduler not checking in, a switched-on job overdue or never run, no daily
  review since yesterday;
- **NeuroDB Watch**: no successful morning run for 26 hours, its AI paused, eTools section names
  without a confirmed NeuroDB section, a check that went back to trial by itself because people found
  it unhelpful (:mod:`neurodb.watch.precision`).

The scheduler not checking in and a missing daily review are critical; the other lines are warnings.
Each line also says what it rests on (``read``, ``on``, ``value``, ``numbers``), in values that stay
the same while the problem does: the watch keeps them as the item's evidence.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.timesince import timesince
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

CRITICAL, WARNING = "critical", "warning"  # a line's level; the same words as a watch item's severity
STALE_DATABASE_DAYS = 40  # a displayed ActivityInfo database not imported for longer is listed
WATCH_LATE_HOURS = 26  # NeuroDB Watch runs every morning: no successful run for longer is listed
NAMES_SHOWN = 3  # database names spelled out in a line, then "and N more"
# Jobs that count only while a switched-on schedule runs them (off by default)
SCHEDULED_ONLY = frozenset({"compiler_youth", "compiler_education", "compiler_wellbeing"})


@dataclass(frozen=True)
class Warning:  # shadows the builtin in this module only: import the module and read health.Warning
    """One line of "Needs attention".

    ``key`` stays the same while the problem lasts; ``text`` is the line as the admin home shows it
    and ``title`` the same with dates instead of durations, for where it is kept (the watch item; empty:
    the text). ``read``, ``on``, ``value`` and ``numbers`` say what it rests on, in values that do not
    move by themselves ("Last run of the eTools sync", its day, "Failed")."""

    key: str
    text: str
    url: str = ""
    level: str = WARNING
    title: str = ""
    read: str = ""
    on: datetime.date | None = None
    value: Any = ""
    numbers: dict[str, Any] = field(default_factory=dict, hash=False)

    @property
    def critical(self) -> bool:
        return self.level == CRITICAL


def warnings(now: datetime.datetime | None = None) -> list[Warning]:
    """Every line that needs an administrator now (default: this moment), most structural first: the
    set-up, the jobs, the schedule, then NeuroDB Watch itself."""
    now = now or timezone.now()
    return [*_setup(now), *_jobs(), *_schedule(now), *_watch(now)]


# ---------------------------------------------------------------------------- set-up
def _setup(now: datetime.datetime) -> list[Warning]:
    """The reporting year, the ActivityInfo imports, master indicator targets and users' roles."""
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from neurodb.accounts.roles import ALL_ROLES
    from neurodb.indicators.models import Database, MasterIndicator
    from neurodb.indicators.services.navigation import current_year

    lines = []
    year = current_year()
    if not year:
        lines.append(
            Warning(
                key="no_current_year",
                text=_("No reporting year is marked as current."),
                url=_admin_url("pivoting_reportingyear"),
                read=_("Reporting year marked as current"),
                value=_("none"),
            )
        )
    shown = Database.objects.filter(reporting_year=year, display=True) if year else Database.objects.none()
    never = shown.filter(last_monthly_update_date__isnull=True).count()
    if never:
        lines.append(
            Warning(
                key="databases_never_imported",
                text=ngettext(
                    "%(n)s displayed database was never imported.",
                    "%(n)s displayed databases were never imported.",
                    never,
                )
                % {"n": never},
                url=_admin_url("pivoting_database") + "?freshness=never",
                read=_("Displayed databases never imported"),
                value=never,
            )
        )
    stale_before = now - datetime.timedelta(days=STALE_DATABASE_DAYS)
    stale = list(
        shown.filter(last_monthly_update_date__lt=stale_before)
        .order_by("name")
        .values_list("name", flat=True)
    )
    if stale:
        names = ", ".join(str(n) for n in stale[:NAMES_SHOWN])
        if len(stale) > NAMES_SHOWN:
            names += " " + _("and %(n)s more") % {"n": len(stale) - NAMES_SHOWN}
        lines.append(
            Warning(
                key="databases_stale",
                text=ngettext(
                    "%(names)s was not imported for more than %(d)s days.",
                    "%(names)s were not imported for more than %(d)s days.",
                    len(stale),
                )
                % {"names": names, "d": STALE_DATABASE_DAYS},
                url=_admin_url("pivoting_database") + "?freshness=stale",
                read=_("Displayed databases not imported for more than %(d)s days")
                % {"d": STALE_DATABASE_DAYS},
                value=len(stale),
            )
        )
    masters = MasterIndicator.objects.filter(database__in=shown, is_active=True)
    no_target = masters.filter(Q(awp_target__isnull=True) | Q(awp_target=0)).count()
    if no_target:
        lines.append(
            Warning(
                key="masters_without_target",
                text=ngettext(
                    "%(n)s active master indicator has no target.",
                    "%(n)s active master indicators have no target.",
                    no_target,
                )
                % {"n": no_target},
                url=_admin_url("pivoting_masterindicator") + "?has_target=no&is_active__exact=1",
                read=_("Active master indicators without a target"),
                value=no_target,
            )
        )
    no_role = (
        get_user_model()
        .objects.filter(is_active=True, is_superuser=False, donor_account__isnull=True)
        .exclude(groups__name__in=ALL_ROLES)  # donor accounts have no role on purpose
        .distinct()
        .count()
    )
    if no_role:
        lines.append(
            Warning(
                key="users_without_role",
                text=ngettext(
                    "%(n)s active user has no role and sees the site as a viewer.",
                    "%(n)s active users have no role and see the site as viewers.",
                    no_role,
                )
                % {"n": no_role},
                url=_admin_url("users_user") + "?role=none",
                read=_("Active users without a role"),
                value=no_role,
            )
        )
    return lines


# ---------------------------------------------------------------------------- jobs
def _jobs() -> list[Warning]:
    """A job whose last run failed or lost rows. A Compiler job counts only while a switched-on
    schedule runs it."""
    from neurodb.core.jobs import COMMANDS
    from neurodb.core.models import ScheduledJob, SyncRun

    commands = ScheduledJob.objects.filter(enabled=True).values_list("command", flat=True)
    scheduled = {COMMANDS[command].sync_job for command in commands if command in COMMANDS}
    lines = []
    for job, label in SyncRun.Job.choices:
        if job in SCHEDULED_ONLY and job not in scheduled:
            continue
        # the latest finished run: a run still going (NeuroDB Watch's own, while it checks this) says
        # nothing yet
        last = (
            SyncRun.objects.filter(job=job)
            .exclude(status=SyncRun.Status.RUNNING)
            .order_by("-started_at")
            .first()
        )
        if last and last.status == SyncRun.Status.FAILED:
            key = f"job_failed:{job}"
            text = _("The last %(job)s failed.") % {"job": label}
        elif last and last.status == SyncRun.Status.PARTIAL:
            key = f"job_partial:{job}"
            text = ngettext(
                "The last %(job)s succeeded with errors: %(n)s row failed.",
                "The last %(job)s succeeded with errors: %(n)s rows failed.",
                last.rows_failed,
            ) % {"job": label, "n": last.rows_failed}
        else:
            continue
        lines.append(
            Warning(
                key=key,
                text=text,
                url=reverse("admin:core_syncrun_change", args=[last.pk]),
                read=_("Last run of the %(job)s") % {"job": label},
                on=timezone.localdate(last.started_at),
                value=last.get_status_display(),
                numbers={"run": last.pk, "rows_failed": last.rows_failed},
            )
        )
    return lines


# ---------------------------------------------------------------------------- the schedule
def _schedule(now: datetime.datetime) -> list[Warning]:
    """The scheduler not checking in, switched-on jobs past their time, a missing daily review."""
    from neurodb.core.admin import SCHEDULER_STALLED_AFTER, is_overdue, scheduler_heartbeat
    from neurodb.core.jobs import COMMANDS
    from neurodb.core.models import ScheduledJob, SyncRun
    from neurodb.review.models import DailyReview

    lines = []
    jobs_url = _admin_url("core_scheduledjob")
    enabled = list(ScheduledJob.objects.filter(enabled=True))
    overdue = [job for job in enabled if is_overdue(job, now)]
    stalled = False
    if settings.SCHEDULER_ENABLED and enabled:
        seen, _host, _alive = scheduler_heartbeat()
        stalled = not (seen and now - seen < SCHEDULER_STALLED_AFTER)
        if stalled:
            since = timesince(seen, now) if seen else None
            if since:
                text = _(
                    "The scheduler has not checked in for %(since)s: scheduled jobs are not starting."
                ) % {"since": since}
                title = _(
                    "The scheduler has not checked in since %(when)s: scheduled jobs are not starting."
                ) % {"when": _local(seen)}
            else:
                text = title = _("The scheduler has never checked in: scheduled jobs are not starting.")
            if overdue:
                more = " " + ngettext("%(n)s job is overdue.", "%(n)s jobs are overdue.", len(overdue)) % {
                    "n": len(overdue)
                }
                text, title = text + more, title + more
            lines.append(
                Warning(
                    key="scheduler_silent",
                    text=text,
                    url=jobs_url,
                    level=CRITICAL,
                    title=title,
                    read=_("The scheduler's last check-in"),
                    on=timezone.localdate(seen) if seen else None,
                    value=_local(seen) if seen else _("never"),
                    numbers={"jobs_overdue": len(overdue)},
                )
            )
    for job in overdue:
        command = COMMANDS.get(job.command)
        label = command.label if command else job.command
        ran = bool(job.last_started_at) or bool(
            command and command.sync_job and SyncRun.objects.filter(job=command.sync_job).exists()
        )
        values = {"job": label, "when": _local(job.next_run_at)}
        if not ran:  # listed even under a stalled scheduler: the data it brings was never there
            key, value = f"job_never_run:{job.key}", _("never run")
            text = _("Scheduled job “%(job)s” has never run: it was due %(when)s.") % values
        elif not stalled:  # under a stalled scheduler, counted in its warning
            key, value = f"job_overdue:{job.key}", _("overdue")
            text = _("Scheduled job “%(job)s” is overdue: it was due %(when)s.") % values
        else:
            continue
        lines.append(
            Warning(
                key=key,
                text=text,
                url=jobs_url,
                read=_("Scheduled job “%(job)s”, due %(when)s") % values,
                on=timezone.localdate(job.next_run_at),
                value=value,
            )
        )
    if any(job.command == "daily_review" for job in enabled):
        yesterday = timezone.localdate(now) - datetime.timedelta(days=1)
        if not DailyReview.objects.filter(date__gte=yesterday, status=DailyReview.Status.SUCCEEDED).exists():
            last = DailyReview.objects.filter(status=DailyReview.Status.SUCCEEDED).order_by("-date").first()
            if last:
                text = _("No daily review since %(date)s.") % {"date": date_format(last.date, "j M Y")}
            else:
                text = _("No daily review has been written yet.")
            lines.append(
                Warning(
                    key="no_daily_review",
                    text=text,
                    url=_admin_url("review_dailyreview"),
                    level=CRITICAL,
                    read=_("Last daily review written"),
                    on=last.date if last else None,
                    value=date_format(last.date, "j M Y") if last else _("none"),
                )
            )
    return lines


# ---------------------------------------------------------------------------- NeuroDB Watch
def watch_last_success():
    """The last successful morning run of NeuroDB Watch (``run_watch --daily``; a quick pass after new
    data does not count), or None."""
    from neurodb.core.models import SyncRun

    return SyncRun.last_success(SyncRun.Job.WATCH, target="daily")


def _watch(now: datetime.datetime) -> list[Warning]:
    """NeuroDB Watch itself: late, its AI paused, eTools section names it cannot route, checks that
    went back to trial by themselves. Nothing while it is switched off."""
    if not settings.WATCH_ENABLED:
        return []
    from neurodb.watch import precision
    from neurodb.watch import sections as watch_sections

    lines = [*_watch_late(now), *_watch_ai_paused(now)]
    for line in watch_sections.needs_attention():  # eTools section names NeuroDB Watch cannot route
        lines.append(Warning(key=line["key"], text=line["text"], url=line["url"], read=line["text"]))
    for line in precision.needs_attention():  # checks people found unhelpful, back in trial
        lines.append(Warning(**line))
    return lines


def _watch_late(now: datetime.datetime) -> list[Warning]:
    """No successful morning run for 26 hours, while its schedule is switched on. Before its first
    run ever, the scheduled job's own line ("has never run") says it instead."""
    from neurodb.core.models import ScheduledJob, SyncRun

    if not ScheduledJob.objects.filter(command="watch", enabled=True).exists():
        return []  # the morning run was switched off on purpose
    limit = now - datetime.timedelta(hours=WATCH_LATE_HOURS)
    url = _admin_url("core_syncrun") + f"?job__exact={SyncRun.Job.WATCH}"
    last = watch_last_success()
    if last is not None:
        finished = last.finished_at or last.started_at
        if finished >= limit:
            return []
        when = timezone.localtime(finished)
        text = _(
            "NeuroDB Watch has not run for more than %(hours)s hours (last morning run %(when)s): "
            "the For you pages are out of date."
        ) % {"hours": WATCH_LATE_HOURS, "when": _local(when)}
        read, on, value = _("Last successful morning run of NeuroDB Watch"), when.date(), _local(when)
    else:
        first = SyncRun.objects.filter(job=SyncRun.Job.WATCH).order_by("started_at").first()
        if first is None or first.started_at >= limit:
            return []
        when = timezone.localtime(first.started_at)
        text = _(
            "NeuroDB Watch has not completed a morning run since it first ran on %(when)s: "
            "the For you pages are empty."
        ) % {"when": _local(when)}
        read, on, value = _("Successful morning runs of NeuroDB Watch"), when.date(), _("none")
    return [Warning(key="watch_not_run", text=text, url=url, read=read, on=on, value=value)]


def _watch_ai_paused(now: datetime.datetime) -> list[Warning]:
    """NeuroDB Watch stopped using AI for a while (the daily limit, the OpenAI credit ran out...): the
    morning notes are listed without it until then."""
    from neurodb.watch.models import WatchState

    if not settings.WATCH_AI:
        return []
    state = WatchState.objects.filter(pk=1).first()  # read only: the row is created by a run
    if state is None or not state.ai_paused_until or state.ai_paused_until <= now:
        return []
    until = timezone.localtime(state.ai_paused_until)
    same_day = until.date() == timezone.localdate(now)
    when = date_format(until, "H:i") if same_day else _local(until)
    reason = (state.ai_pause_reason or "").strip().rstrip(".")
    if reason:
        text = _("NeuroDB Watch stopped using AI until %(when)s: %(reason)s.") % {
            "when": when,
            "reason": reason,
        }
    else:
        text = _("NeuroDB Watch stopped using AI until %(when)s.") % {"when": when}
    return [
        Warning(
            key="watch_ai_paused",
            text=text,
            url=_admin_url("assistant_aiusage"),
            read=_("NeuroDB Watch AI paused until"),
            on=until.date(),
            value=f"{_local(until)}{': ' + reason if reason else ''}",
        )
    ]


# ---------------------------------------------------------------------------- helpers
def _local(when: datetime.datetime) -> str:
    return date_format(timezone.localtime(when), "j M, H:i")


def _admin_url(model: str) -> str:
    return reverse(f"admin:{model}_changelist")
