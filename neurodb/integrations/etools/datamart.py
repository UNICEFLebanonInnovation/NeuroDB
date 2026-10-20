"""eTools Datamart REST client (``datamart.unicef.io``, the "new eTools APIs").

The Datamart publishes the eTools data of every UNICEF country office as read-only, paginated DRF
lists (``count``/``next``/``results``) under ``/api/<version>/datamart/...``. Every request is
limited to one country with the ``country_name`` filter (``settings.ETOOLS_DATAMART_COUNTRY``).

Authentication is HTTP basic with ``settings.ETOOLS_USERNAME`` / ``settings.ETOOLS_PASSWORD``, which
come from the environment or Key Vault only. The credentials are attached per request and only to
the Datamart host: a ``next`` link pointing anywhere else is refused rather than followed, so they
can never be sent to another server. They never appear in logs or errors (``http.py`` logs the
method, path, status and time only).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from django.conf import settings

from neurodb.integrations.http import HostBoundBasicAuth, IntegrationError, get_json, make_session

logger = logging.getLogger(__name__)


class DatamartNotConfigured(IntegrationError):
    """The username or password is missing."""


def configured() -> bool:
    return bool(settings.ETOOLS_USERNAME and settings.ETOOLS_PASSWORD)


_HostBoundBasicAuth = HostBoundBasicAuth  # moved to integrations.http, shared with ActivityInfo


class DatamartClient:
    def __init__(
        self,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        country: str | None = None,
        version: str | None = None,
        page_size: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = (base_url or settings.ETOOLS_DATAMART_URL).rstrip("/")
        self.version = version or settings.ETOOLS_DATAMART_API_VERSION
        self.country = settings.ETOOLS_DATAMART_COUNTRY if country is None else country
        self.page_size = page_size or settings.ETOOLS_DATAMART_PAGE_SIZE
        username = settings.ETOOLS_USERNAME if username is None else username
        password = settings.ETOOLS_PASSWORD if password is None else password
        if not (username and password):
            raise DatamartNotConfigured(
                "eTools Datamart credentials are not set (ETOOLS_USERNAME and ETOOLS_PASSWORD)"
            )
        parts = urlsplit(self.base_url)
        self.origin = (parts.scheme, parts.netloc)
        self.session = session or make_session("")
        self.session.auth = _HostBoundBasicAuth(username, password, self.origin)
        # the ``count`` the first page of the latest :meth:`list` announced (None: not read yet, or an
        # unpaginated list): a sync compares it with what it read before it removes anything
        self.last_count: int | None = None

    # API roots beside ``datamart/``: the Partner Reporting Portal views, its source tables, the ETL log.
    OTHER_ROOTS = ("prp/", "sources/", "system/", "rapidpro/")

    def url(self, dataset: str) -> str:
        """``partners/assessment`` -> ``<base>/api/<version>/datamart/partners/assessment/``;
        ``prp/datareport`` -> ``<base>/api/<version>/prp/datareport/``."""
        path = dataset.strip("/")
        if not path.startswith(self.OTHER_ROOTS):
            path = f"datamart/{path}"
        return f"{self.base_url}/api/{self.version}/{path}/"

    def _same_origin(self, link: str) -> str:
        """The ``next`` link on the Datamart host (a proxy may report it as http://), or raise."""
        parts = urlsplit(link)
        if parts.netloc != self.origin[1]:
            raise IntegrationError(f"refusing to follow a next link to another host ({parts.netloc})")
        return urlunsplit((self.origin[0], *parts[1:]))

    def get(self, dataset: str, pk: Any) -> dict[str, Any]:
        """One record by its id (``<dataset>/<id>/``, as PRP's source tables offer it). An id names one
        record, so no country filter is added; a record that does not exist raises with status 404."""
        payload = get_json(self.session, self.url(f"{dataset.strip('/')}/{int(pk)}"))
        if not isinstance(payload, dict):
            raise IntegrationError(f"unrecognised record payload from {dataset}")
        return payload

    def list(
        self, dataset: str, params: dict[str, Any] | None = None, *, country: bool = True
    ) -> Iterator[dict[str, Any]]:
        """Yield every record of a dataset across all pages, for the configured country unless
        ``country`` is False (datasets without a ``country_name`` field get their own filter). The first
        page's ``count`` is kept in :attr:`last_count` (read it once the records are read)."""
        query: dict[str, Any] = {"page_size": self.page_size, **(params or {})}
        if self.country and country:
            query.setdefault("country_name", self.country)
        url = self.url(dataset)
        self.last_count = None
        first = True
        while True:
            payload = get_json(self.session, url, **query)
            if isinstance(payload, list):  # an unpaginated list
                yield from payload
                return
            if not isinstance(payload, dict) or "results" not in payload:
                raise IntegrationError(f"unrecognised list payload from datamart/{dataset}")
            if first:
                self.last_count, first = _count(payload.get("count")), False
            yield from payload["results"] or []
            next_url = payload.get("next")
            if not next_url:
                return
            url, query = self._same_origin(next_url), {}

    def first_page(
        self, dataset: str, params: dict[str, Any] | None = None, *, country: bool = True
    ) -> tuple[int | None, list[dict[str, Any]]]:
        """One request: the ``count`` the Datamart announces for this query (None when it says none) and
        the records of its first page (``page_size`` in ``params`` sets how many)."""
        query: dict[str, Any] = {"page_size": self.page_size, **(params or {})}
        if self.country and country:
            query.setdefault("country_name", self.country)
        payload = get_json(self.session, self.url(dataset), **query)
        if isinstance(payload, list):  # an unpaginated list
            return None, [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict) or "results" not in payload:
            raise IntegrationError(f"unrecognised list payload from datamart/{dataset}")
        return _count(payload.get("count")), [
            item for item in payload["results"] or [] if isinstance(item, dict)
        ]


def _count(value: Any) -> int | None:
    """The ``count`` of a DRF page as a number; None when it is missing or not one."""
    try:
        return int(value) if value not in (None, "") and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None
