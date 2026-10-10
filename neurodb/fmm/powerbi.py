"""The Power BI live feed of Monitoring insights (FMS "Connect Live"): ``/powerbi/fmm/<dataset>.csv`` for
``records`` (one row per record, as FMS's export), ``visits``, ``rule_results``, ``action_points`` and
``partners``, the tables of the Power BI package over every visit (no person's section is applied),
narrowed by ``?year=`` or ``?since=YYYY-MM-DD`` (the visit date: its start, else its end, as every period
reads it; a record follows its visit). A report built on ``visits`` before the records keeps working.

The feed is read with a key an Administrator creates in the admin (*Power BI keys*,
:class:`~neurodb.fmm.models.PowerBIKey`), never with a session: these addresses alone are left out of the
sign-in requirement and of the donor lock-down (:func:`key_only`), and a signed-in person without a key
gets nothing more than anyone else. The key comes as ``Authorization: Bearer <key>`` or as ``?key=<key>``
(what Power BI's ``Web.Contents(..., [ApiKeyName = "key"])`` sends, the only way a scheduled refresh in
Power BI Service sends one). Only its SHA-256 hash is kept and compared in constant time; it is never
written to a log or a message after it was shown. Without any key the feed answers 404; a missing, wrong
or revoked key gets 401; each key may make ``FMM_POWERBI_REQUESTS_PER_HOUR`` requests an hour (then 429
with ``Retry-After``). Every answer is ``Cache-Control: no-store``.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import replace

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.db.models import F
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from . import exports
from .models import PowerBIKey
from .scope import Scope

logger = logging.getLogger(__name__)
PREFIX_CHARS = 8
_YEAR = re.compile(r"^(19|20)\d\d$")


# ------------------------------------------------------------------------------------------ keys
def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key() -> str:
    """A new random key (43 characters)."""
    return secrets.token_urlsafe(32)


def create_key(name: str, user=None) -> tuple[PowerBIKey, str]:
    """A new key: the row (its hash and prefix) and the key itself, to show once."""
    key = new_key()
    row = PowerBIKey.objects.create(
        name=name[:100], prefix=key[:PREFIX_CHARS], key_hash=key_hash(key), created_by=user
    )
    return row, key


def revoke(rows) -> int:
    """Revoke the keys of ``rows`` not revoked yet; how many."""
    return rows.filter(revoked_at=None).update(revoked_at=timezone.now())


def _given_key(request: HttpRequest) -> str:
    header = request.headers.get("Authorization", "")
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return request.GET.get("key", "").strip()


def find_key(key: str) -> PowerBIKey | None:
    """The key row ``key`` opens (revoked or not), compared on its hash in constant time."""
    if not key or len(key) > 200:
        return None
    wanted = key_hash(key)
    found = None
    for row in PowerBIKey.objects.filter(prefix=key[:PREFIX_CHARS]):
        if hmac.compare_digest(row.key_hash, wanted):
            found = row
    return found


def _throttled(row: PowerBIKey) -> int:
    """0 when ``row`` may make one more request this hour (and that request is counted), else the
    seconds until it may. The count is kept on the key's row, in single UPDATE statements, so the limit
    holds across every worker and container (a cache would count each worker on its own)."""
    now = timezone.now()
    hour = now.replace(minute=0, second=0, microsecond=0)
    limit = settings.FMM_POWERBI_REQUESTS_PER_HOUR
    keys = PowerBIKey.objects.filter(pk=row.pk)
    for _attempt in range(2 if limit > 0 else 0):  # two requests that start the hour together: count again
        if keys.filter(hour_started=hour, hour_uses__lt=limit).update(hour_uses=F("hour_uses") + 1):
            return 0
        if keys.exclude(hour_started=hour).update(hour_started=hour, hour_uses=1):
            return 0
    next_hour = hour + datetime.timedelta(hours=1)
    return max(1, int((next_hour - now).total_seconds()))


# ------------------------------------------------------------------------------------------ the feed
def key_only(view):
    """A view read with a Power BI key: left out of the sign-in requirement (``login_not_required``) and
    of the donor lock-down (``powerbi_feed``, read by ``donors.middleware``); nothing else is."""
    view = login_not_required(view)
    view.powerbi_feed = True
    return view


def _refused(status: int, text: str, **headers: str) -> HttpResponse:
    response = HttpResponse(text, status=status, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "no-store"
    for name, value in headers.items():
        response[name] = value
    return response


def feed_scope(params) -> Scope | None:
    """Every visit, or those of ``?year=``, from ``?since=`` on (visit date: start, else end); None
    for a value that is not a year or an ISO date."""
    year = str(params.get("year", "")).strip()
    since = str(params.get("since", "")).strip()
    if year and not _YEAR.match(year):
        return None
    base = {"year": year} if year else {"preset": "all_time"}
    scope = Scope.from_params({**base, "section": ""})
    if since:
        try:
            day = datetime.date.fromisoformat(since)
        except ValueError:
            return None
        scope = replace(scope, preset="custom", year=None, start=max(scope.start, day))
    return scope


@key_only
@require_GET
def feed(request: HttpRequest, dataset: str) -> HttpResponse:
    """One table of the live feed as CSV (see the module's notes)."""
    if not getattr(settings, "FMM_ENABLED", False) or dataset not in exports.DATASETS:
        return _refused(404, "Not found.")
    if not PowerBIKey.objects.filter(revoked_at=None).exists():
        return _refused(404, "Not found.")
    row = find_key(_given_key(request))
    if row is None:
        logger.warning("Power BI feed refused: no key or an unknown key (%s)", dataset)
        return _refused(401, "A valid Power BI key is needed.", **{"WWW-Authenticate": "Bearer"})
    if row.revoked_at is not None:
        logger.warning("Power BI feed refused: key %s… is revoked (%s)", row.prefix, dataset)
        return _refused(401, "This Power BI key is revoked.", **{"WWW-Authenticate": "Bearer"})
    wait = _throttled(row)
    if wait:
        logger.warning("Power BI feed refused: key %s… made too many requests this hour", row.prefix)
        return _refused(429, "Too many requests this hour.", **{"Retry-After": str(wait)})
    scope = feed_scope(request.GET)
    if scope is None:
        return _refused(400, "year is a year (2026); since is a date (2026-01-31).")
    PowerBIKey.objects.filter(pk=row.pk).update(uses=F("uses") + 1, last_used_at=timezone.now())
    ctx = exports.Context.read()
    columns, rows = exports.dataset(dataset, scope, ctx)
    response = StreamingHttpResponse(exports.csv_lines(columns, rows), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'inline; filename="{dataset}.csv"'
    response["Cache-Control"] = "no-store"
    return response


def site_url(request: HttpRequest) -> str:
    """The site's address, for the scripts: ``SITE_URL``, else the request's."""
    return (getattr(settings, "SITE_URL", "") or request.build_absolute_uri("/")).rstrip("/") + "/"


def live_script(request: HttpRequest) -> str:
    """The Power Query script that reads the live feed of this site."""
    return exports.m_script(exports.all_columns(exports.Context.read()), base_url=site_url(request))
