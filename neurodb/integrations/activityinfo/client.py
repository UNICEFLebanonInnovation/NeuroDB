"""ActivityInfo v4 API client.

Ports ``pivoting/client.py::Client`` (token auth, ``get_resource``/``post_resource``) and the
export-job flow of ``pivoting/exports.py::get_database_data``. The ``Authorization`` header value
is the bare token, exactly as v2 ``pivoting/auth.py::TokenAuth`` sent it (no ``Bearer`` prefix).

Fixed v2 defects: the token literal in the source -> ``settings.ACTIVITYINFO_TOKEN`` only; the
unbounded ``while True`` polling loop -> ``max_attempts`` x ``interval``; downloads returned to the
caller as bytes instead of being written into the package directory.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests
from django.conf import settings

from neurodb.indicators.models import Database
from neurodb.integrations.http import IntegrationError, get_bytes, get_json, make_session, post_json

logger = logging.getLogger(__name__)

POLL_MAX_ATTEMPTS = 150
POLL_INTERVAL_SECONDS = 2.0


class ActivityInfoClient:
    """Thin wrapper over the ActivityInfo REST resources used by the importers."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = (base_url or settings.ACTIVITYINFO_BASE_URL).rstrip("/")
        token = settings.ACTIVITYINFO_TOKEN if token is None else token
        self.session = session or make_session(token)
        self._sleep = sleep

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    # ------------------------------------------------------------------ structure
    def get_database(self, database_id: str) -> dict[str, Any]:
        """``resources/databases/{id}``: the database tree (folders, forms, sub-forms)."""
        return get_json(self.session, self.url(f"resources/databases/{database_id}"))

    def get_form_schema(self, form_id: str) -> dict[str, Any]:
        """``resources/form/{id}/schema``: the form elements."""
        return get_json(self.session, self.url(f"resources/form/{form_id}/schema"))

    # ------------------------------------------------------------------ export job
    def start_export(
        self,
        database_id: str,
        *,
        folder_id: str | None = None,
        record_filter: str | None = None,
        export_format: str = "LONG",
        file_format: str = "TEXT",
    ) -> str:
        """Start an ``exportDatabaseForms`` job and return its id (v2 ``get_database_data``)."""
        descriptor: dict[str, Any] = {
            "databaseId": database_id,
            "folderId": folder_id or database_id,
            "format": export_format,
            "fileFormat": file_format,
        }
        if record_filter is not None:
            descriptor["filter"] = record_filter
        payload = {"type": "exportDatabaseForms", "descriptor": descriptor}
        job = post_json(self.session, self.url("resources/jobs"), payload)
        try:
            return str(job["id"])
        except (KeyError, TypeError) as exc:
            raise IntegrationError("export job response has no id") from exc

    def poll_export(
        self,
        job_id: str,
        *,
        max_attempts: int = POLL_MAX_ATTEMPTS,
        interval: float = POLL_INTERVAL_SECONDS,
    ) -> dict[str, Any]:
        """Poll ``resources/jobs/{id}`` until ``completed`` and return the job ``result``."""
        for attempt in range(1, max_attempts + 1):
            status = get_json(self.session, self.url(f"resources/jobs/{job_id}"))
            state = str(status.get("state", "")).lower()
            if state == "completed":
                return status.get("result") or {}
            if state == "failed":
                message = (status.get("error") or {}).get("message", "")
                raise IntegrationError(f"export job {job_id} failed: {message[:200]}")
            if state != "started":
                raise IntegrationError(f"export job {job_id} returned unknown state {state!r}")
            logger.debug("export job %s still running (attempt %d/%d)", job_id, attempt, max_attempts)
            self._sleep(interval)
        raise IntegrationError(f"export job {job_id} not completed after {max_attempts} attempts")

    def download(self, url: str) -> bytes:
        """Download an export; ``url`` may be the relative ``downloadUrl`` of a job result."""
        if not url.startswith(("http://", "https://")):
            url = self.url(url)
        return get_bytes(self.session, url)

    def export_database(self, database: Database) -> bytes:
        """Run the export exactly as v2 ``r_script_command_line`` requested it and return the file.

        With a ``parent_id`` the folder ``db_id`` of database ``parent_id`` is exported, otherwise
        ``db_id`` is the database itself. Records are filtered to the reporting year.
        """
        year = getattr(database.reporting_year, "year", None)
        record_filter = f"LEFT(Month,4) == '{year}'" if year else None
        if database.parent_id:
            job_id = self.start_export(
                database.parent_id, folder_id=database.db_id, record_filter=record_filter
            )
        else:
            job_id = self.start_export(database.db_id, record_filter=record_filter)
        result = self.poll_export(job_id)
        try:
            download_url = result["downloadUrl"]
        except KeyError as exc:
            raise IntegrationError(f"export job {job_id} completed without a download url") from exc
        return self.download(download_url)
