"""HTTP transport shared by the ActivityInfo and eTools clients.

Ports the transport behaviour of v2 ``pivoting/client.py`` (``requests`` calls without a timeout)
and ``pivoting/utils.py::get_data`` (raw ``http.client`` without timeout or retry) onto a single
``requests.Session``.

Fixed v2 defects:

* no connect/read timeout -> every request uses ``settings.INTEGRATION_TIMEOUT_SECONDS``;
* no retry -> urllib3 ``Retry`` (5 attempts, exponential backoff, on 429 and 5xx);
* tokens and response bodies printed on failure -> the log line carries only method, path,
  status and elapsed time; the exception carries the status and a short, token-free message.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = 1.0
RETRY_STATUSES = (429, 500, 502, 503, 504)
ERROR_MESSAGE_CHARS = 200


class IntegrationError(Exception):
    """An upstream call failed. ``status`` is the HTTP status when one was received."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class TimeoutSession(requests.Session):
    """A ``requests.Session`` that applies a default ``timeout`` to every request."""

    def __init__(self, timeout: tuple[float, float] | float) -> None:
        super().__init__()
        self.timeout = timeout

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return super().request(method, url, **kwargs)


def make_session(
    token_header: str,
    timeout: tuple[float, float] | float | None = None,
    *,
    retries: int = RETRY_ATTEMPTS,
    backoff: float = RETRY_BACKOFF_SECONDS,
) -> requests.Session:
    """Build a session with timeout, retries and the ``Authorization`` header value given.

    ``token_header`` is the complete header value (``"Token abc"`` for eTools, the bare token for
    ActivityInfo, see ``activityinfo/client.py``). An empty value sends no header.
    """
    timeout = timeout if timeout is not None else settings.INTEGRATION_TIMEOUT_SECONDS
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    session = TimeoutSession(timeout)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers["Accept"] = "application/json"
    if token_header:
        session.headers["Authorization"] = token_header
    return session


def _path(url: str) -> str:
    return urlsplit(url).path or "/"


def send(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
    stream: bool = False,
) -> requests.Response:
    """Perform one request, log it (method, path, status, elapsed ms) and raise on failure."""
    started = time.monotonic()
    path = _path(url)
    try:
        response = session.request(method, url, params=params, json=json, stream=stream)
    except requests.RequestException as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        logger.warning("%s %s -> %s after %d ms", method, path, type(exc).__name__, elapsed)
        raise IntegrationError(f"{method} {path}: {type(exc).__name__}", url=url) from exc
    elapsed = int((time.monotonic() - started) * 1000)
    logger.info("%s %s -> %s in %d ms", method, path, response.status_code, elapsed)
    if not response.ok:
        raise IntegrationError(
            f"{method} {path}: HTTP {response.status_code} {_short_body(response)}",
            status=response.status_code,
            url=url,
        )
    return response


def _short_body(response: requests.Response) -> str:
    """The reason phrase plus a whitespace-collapsed head of the body, never the request."""
    try:
        text = " ".join(response.text.split())
    except (ValueError, UnicodeDecodeError):
        text = ""
    return f"{response.reason or ''} {text[:ERROR_MESSAGE_CHARS]}".strip()


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any = None,
) -> Any:
    """Send a request and decode the JSON body, raising ``IntegrationError`` on any failure."""
    response = send(session, method, url, params=params, json=json)
    try:
        return response.json()
    except ValueError as exc:
        raise IntegrationError(
            f"{method} {_path(url)}: response is not JSON", status=response.status_code, url=url
        ) from exc


def get_json(session: requests.Session, url: str, **params: Any) -> Any:
    """GET ``url`` with ``params`` as the query string and return the decoded JSON."""
    return request_json(session, "GET", url, params=params or None)


def post_json(session: requests.Session, url: str, payload: Any) -> Any:
    """POST ``payload`` as JSON and return the decoded JSON response."""
    return request_json(session, "POST", url, json=payload)


def get_bytes(session: requests.Session, url: str) -> bytes:
    """GET ``url`` (streamed) and return the whole body as bytes."""
    with send(session, "GET", url, stream=True) as response:
        return response.content
