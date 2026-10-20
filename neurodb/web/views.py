import datetime

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.utils import timezone
from django.utils.translation import gettext_lazy as _lazy

from neurodb.core.models import SyncRun
from neurodb.fmm.models import RuleSetting
from neurodb.indicators.services import activityinfo
from neurodb.review import checks
from neurodb.watch.detectors import deadlines, money
from neurodb.watch.models import DetectorSetting

from .services import public_highlights


@login_not_required
def favicon(request):
    """Browsers ask for /favicon.ico directly; send them to the hashed static file."""
    return redirect(static("img/favicon.ico"))


@login_not_required
def healthz(request):
    """Readiness probe: database reachable (503 if not) plus the last successful run of each sync job."""
    status = {
        "database": "ok",
        "version": settings.APP_VERSION,
        "syncs": {},
        "time": timezone.now().isoformat(),
    }
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        for job, _label in SyncRun.Job.choices:
            last = SyncRun.last_success(job)
            status["syncs"][job] = last.finished_at.isoformat() if last else None
    except Exception as exc:  # any database failure means "not ready", never a 500
        status["database"] = f"error: {exc.__class__.__name__}"
        return JsonResponse(status, status=503)
    return JsonResponse(status)


# Public figures, in order of preference; the first six that are not zero are shown. The eTools
# ones are counted as the country overview counts them (programme documents running in the year); the
# ActivityInfo ones exist only while ActivityInfo is in use (services.public_highlights).
LANDING_STATS = [
    ("running_programmes", _lazy("programme documents running this year")),
    ("etools_partners", _lazy("partners with a programme document this year")),
    ("pd_indicators", _lazy("programme document indicators this year")),
    ("progress_reports", _lazy("partner progress reports this year")),
    ("indicators", _lazy("ActivityInfo results tracked against targets")),
    ("governorates", _lazy("governorates in ActivityInfo reports this year")),
    ("records", _lazy("activity records this year")),
    ("partners", _lazy("partners reporting in ActivityInfo this year")),
    ("sections", _lazy("programme sections in ActivityInfo")),
]


def _stat_items(stats: dict | None) -> list[tuple[int, str]]:
    if not stats:
        return []
    return [(stats[key], label) for key, label in LANDING_STATS if stats.get(key)][:6]


# The concept note's table "AI in each area of analysis" (concept-note.pdf pp. 7-8): AI takes part in 13 of
# its 14 areas, all but the donor page. Not a code constant: the twelve AI features of the usage ledger
# (neurodb/assistant/usage.py) cover those 13 areas, and each runs only where AI is switched on.
AI_AREAS = 13
ANALYSIS_AREAS = 14


def _check_mode(detector) -> str:
    """A Watch check's mode (``DetectorSetting``): on (told to staff), trial (seen only by the
    whole-country view) or off; before its first run, the mode it starts in."""
    mode = DetectorSetting.objects.filter(detector=detector.id).values_list("mode", flat=True).first()
    return mode or detector.default_mode


def platform_figures() -> dict:
    """The rules the landing page quotes, read from the code and the settings that apply them, so the page
    never drifts from what NeuroDB does: the daily review's checks (``review.checks.CHECKS``), the field
    visit quality rules switched on (``fmm.RuleSetting``, the AI checks apart), the Watch lead times of
    grants, programme documents and partner reports (``watch.detectors.money`` and ``deadlines``), the
    unspent amount a grant alert needs (``WATCH_GRANT_MIN_UNSPENT``), the day HACT gaps start to be
    raised, and the mode of the grant check (``grant_alert``): only when it is on are the sections of the
    grant's programme documents told; in trial only the whole-country view sees it (the mode every check
    starts in); off, nothing runs. Two small queries (the rules switched on, the grant check's mode);
    counts only."""
    rules = RuleSetting.objects.filter(enabled=True).aggregate(
        total=Count("pk"), ai=Count("pk", filter=Q(type=RuleSetting.Type.NARRATIVE))
    )
    total, ai = rules["total"], rules["ai"]
    lead = {"grant": money.GRANT_DAYS, "pd": deadlines.PD_ENDING_DAYS, "report": deadlines.REPORT_DAYS}
    longest = max(lead.values())
    month, day = money.HACT_FROM
    return {
        "morning_checks": len(checks.CHECKS),
        "ai_areas": AI_AREAS,
        "analysis_areas": ANALYSIS_AREAS,
        "quality_rules": total,
        "quality_ai": ai,
        "quality_fixed": total - ai,
        "quality_split": f"{(total - ai) / total * 360:.1f}deg" if total else "180deg",
        "lead": lead,
        "bars": {name: f"{days / longest * 100:.3f}%" for name, days in lead.items()},
        "grant_min_unspent": settings.WATCH_GRANT_MIN_UNSPENT,
        "hact_from": datetime.date(2000, month, day),  # shown as a day and a month ("1 October")
        "grant_alert": _check_mode(money.GRANT_EXPIRING),
    }


@login_not_required
def landing(request):
    """Public landing page: why NeuroDB exists, the data and the rules behind it, a story to follow and the
    way in. Its live figures are aggregate counts only (``PUBLIC_LANDING_STATS``)."""
    ai = activityinfo.state()  # in use: a current source; an archive: "ActivityInfo archive {span}"
    stats = public_highlights() if settings.PUBLIC_LANDING_STATS else None
    last_sync = SyncRun.last_success(SyncRun.Job.ETOOLS_DATAMART)
    context = {
        "ai": ai,
        "stats": stats,
        "stat_items": _stat_items(stats),
        "public_pages": [p.split(":", 1)[1] for p in settings.PUBLIC_PAGES],
        "etools_synced": last_sync.finished_at if last_sync else None,
        "figures": platform_figures(),
    }
    return render(request, "landing.html", context)
