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
from requests.auth import HTTPBasicAuth

from neurodb.integrations.http import IntegrationError, get_json, make_session

logger = logging.getLogger(__name__)


class DatamartNotConfigured(IntegrationError):
    """The username or password is missing."""


def configured() -> bool:
    return bool(settings.ETOOLS_USERNAME and settings.ETOOLS_PASSWORD)


class _HostBoundBasicAuth(HTTPBasicAuth):
    """Basic auth that is only added to requests for one host (scheme + host + port)."""

    def __init__(self, username: str, password: str, origin: tuple[str, str]) -> None:
        super().__init__(username, password)
        self.origin = origin

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        parts = urlsplit(request.url or "")
        if (parts.scheme, parts.netloc) == self.origin:
            return super().__call__(request)
        return request


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

    def url(self, dataset: str) -> str:
        """``partners/assessment`` -> ``<base>/api/<version>/datamart/partners/assessment/``."""
        return f"{self.base_url}/api/{self.version}/datamart/{dataset.strip('/')}/"

    def _same_origin(self, link: str) -> str:
        """The ``next`` link on the Datamart host (a proxy may report it as http://), or raise."""
        parts = urlsplit(link)
        if parts.netloc != self.origin[1]:
            raise IntegrationError(f"refusing to follow a next link to another host ({parts.netloc})")
        return urlunsplit((self.origin[0], *parts[1:]))

    def list(self, dataset: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        """Yield every record of a dataset for the configured country, across all pages."""
        query: dict[str, Any] = {"page_size": self.page_size, **(params or {})}
        if self.country:
            query.setdefault("country_name", self.country)
        url = self.url(dataset)
        while True:
            payload = get_json(self.session, url, **query)
            if isinstance(payload, list):  # an unpaginated list
                yield from payload
                return
            if not isinstance(payload, dict) or "results" not in payload:
                raise IntegrationError(f"unrecognised list payload from datamart/{dataset}")
            yield from payload["results"] or []
            next_url = payload.get("next")
            if not next_url:
                return
            url, query = self._same_origin(next_url), {}
