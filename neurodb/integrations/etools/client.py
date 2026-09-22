"""eTools REST client.

Ports ``pivoting/utils.py::get_data`` (``http.client`` GET with ``Authorization: Token ...``) onto
the shared session. ``settings.ETOOLS_TOKEN`` may be the bare DRF token or the full ``Token ...``
header value; nothing else is ever read for credentials.

Fixed v2 defects: no timeout/retry (see ``http.py``); the token literal; ``sync_trip_data``
started at page 45 and looped to page 100 - ``list`` paginates from the first page and follows
DRF ``next`` links or ``page``/``page_size`` (t2f) until the last page or a 404.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import requests
from django.conf import settings

from neurodb.integrations.http import IntegrationError, get_json, make_session

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 1000


def token_header(token: str) -> str:
    """``"Token <value>"`` unless the setting already carries a scheme."""
    token = (token or "").strip()
    if not token or " " in token:
        return token
    return f"Token {token}"


class EToolsClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = (base_url or settings.ETOOLS_BASE_URL).rstrip("/")
        token = settings.ETOOLS_TOKEN if token is None else token
        self.session = session or make_session(token_header(token))

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def get(self, path: str, **params: Any) -> Any:
        """GET one resource (list or detail) and return the decoded JSON."""
        return get_json(self.session, self.url(path), **params)

    def list(
        self, path: str, params: dict[str, Any] | None = None, *, page_size: int | None = DEFAULT_PAGE_SIZE
    ) -> Iterator[dict[str, Any]]:
        """Yield every item of a list endpoint across all its pages.

        Handles a plain JSON list, DRF page-number responses (``results`` + ``next``) and the t2f
        style (``data`` with ``page``/``page_size`` query parameters, ``page_count`` when given,
        else until an empty page or a 404).
        """
        query = dict(params or {})
        if page_size:
            query["page_size"] = page_size
        url = self.url(path)
        page = 1
        while True:
            try:
                payload = get_json(self.session, url, **query)
            except IntegrationError as exc:
                if exc.status == 404 and page > 1:
                    return
                raise
            if isinstance(payload, list):
                yield from payload
                return
            if not isinstance(payload, dict):
                raise IntegrationError(f"unexpected list payload from {path}")
            if "results" in payload:
                yield from payload["results"]
                next_url = payload.get("next")
                if not next_url:
                    return
                url, query = next_url, {}
            elif "data" in payload:
                items = payload["data"] or []
                yield from items
                page_count = payload.get("page_count")
                if not items or (page_count is not None and page >= int(page_count)):
                    return
                query["page"] = page + 1
            else:
                raise IntegrationError(f"unrecognised list payload from {path}")
            page += 1
