"""Compiler's youth indicator figures API: GET <COMPILER_API_URL>/api/youth/indicator-figures/?year=.

The token belongs to a Compiler service account in the "NeuroDB API" group; it is sent as
``Authorization: Token <key>`` and never logged (see ``integrations/http.py``).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from neurodb.integrations.http import IntegrationError, get_json, make_session

PATH = "/api/youth/indicator-figures/"


def configured() -> bool:
    return bool(settings.COMPILER_API_URL and settings.COMPILER_API_TOKEN)


class CompilerClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, *, session=None) -> None:
        self.base_url = (base_url or settings.COMPILER_API_URL).rstrip("/")
        token = settings.COMPILER_API_TOKEN if token is None else token
        self.session = session or make_session(f"Token {token}" if token else "")

    def figures(self, year: str | None = None) -> dict[str, Any] | None:
        """The figures of ``year`` (Compiler's current year when None); None when Compiler has no such
        year (a 404 for the current year is an error: the address is wrong)."""
        try:
            data = get_json(self.session, self.base_url + PATH, **({"year": year} if year else {}))
        except IntegrationError as exc:
            if exc.status == 404 and year:
                return None
            raise
        if not isinstance(data, dict) or "figures" not in data or "year" not in data:
            raise IntegrationError("Compiler youth figures: unexpected response", url=self.base_url + PATH)
        return data
