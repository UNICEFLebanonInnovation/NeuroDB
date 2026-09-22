from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone

from neurodb.core.models import SyncRun


@login_not_required
def healthz(request):
    """Liveness + freshness probe for App Service / Container Apps."""
    status = {"database": "ok", "syncs": {}, "time": timezone.now().isoformat()}
    code = 200
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover
        status["database"] = f"error: {exc.__class__.__name__}"
        code = 503
    for job, _label in SyncRun.Job.choices:
        last = SyncRun.last_success(job)
        status["syncs"][job] = last.finished_at.isoformat() if last else None
    return JsonResponse(status, status=code)
