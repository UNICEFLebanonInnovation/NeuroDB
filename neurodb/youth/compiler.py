"""Compiler's figures APIs: the youth indicator figures (GET /api/youth/indicator-figures/?year=) and
the education programmes' stored counts (GET /api/figures/ and /api/figures/<programme>/?year=).

The token belongs to a Compiler service account in the "NeuroDB API" group; it is sent as
``Authorization: Token <key>`` and never logged (see ``integrations/http.py``).
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from neurodb.integrations.http import IntegrationError, get_json, make_session, send

PATH = "/api/youth/indicator-figures/"
EDUCATION_PATH = "/api/figures/"
WELLBEING_PATH = "/api/wellbeing/"


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

    def education_index(self) -> list[dict[str, Any]]:
        """The education programmes Compiler counts, with their years and which are counted."""
        data = get_json(self.session, self.base_url + EDUCATION_PATH)
        if not isinstance(data, dict) or not isinstance(data.get("programmes"), list):
            raise IntegrationError(
                "Compiler figures index: unexpected response", url=self.base_url + EDUCATION_PATH
            )
        return data["programmes"]

    def education(self, programme: str, year: str) -> dict[str, Any] | None:
        """The stored counts of ``programme`` for ``year``; None while Compiler is still counting them
        (it answers 202 and counts in the background) or when it has no such year."""
        url = f"{self.base_url}{EDUCATION_PATH}{programme}/"
        try:
            response = send(self.session, "GET", url, params={"year": year})
        except IntegrationError as exc:
            if exc.status == 404:
                return None
            raise
        if response.status_code == 202:
            return None
        data = response.json()
        if not isinstance(data, dict) or "blocks" not in data or "year" not in data:
            raise IntegrationError("Compiler education figures: unexpected response", url=url)
        return data

    # ------------------------------------------------------------- Makani wellbeing flags
    def wellbeing_flags(
        self, *, modified_since: str | None = None, after: int | None = None, limit: int = 500
    ) -> dict[str, Any]:
        """One page of flags (children by registration number only); ``next_after`` gives the next."""
        params: dict[str, Any] = {"limit": limit}
        if modified_since:
            params["modified_since"] = modified_since
        if after:
            params["after"] = after
        url = self.base_url + WELLBEING_PATH + "flags/"
        data = get_json(self.session, url, **params)
        if not isinstance(data, dict) or not isinstance(data.get("flags"), list):
            raise IntegrationError("Compiler wellbeing flags: unexpected response", url=url)
        return data

    def wellbeing_summaries(self, month: str | None = None) -> dict[str, Any]:
        url = self.base_url + WELLBEING_PATH + "summaries/"
        data = get_json(self.session, url, **({"month": month} if month else {}))
        if not isinstance(data, dict) or not isinstance(data.get("summaries"), list):
            raise IntegrationError("Compiler wellbeing summaries: unexpected response", url=url)
        return data

    def wellbeing_follow_up(self, flag_id: int, values: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Record a follow-up in Compiler. Returns (status code, body): 200 recorded, 400 refused
        (field errors), 409 the flag is no longer open (body has the flag as it is)."""
        url = f"{self.base_url}{WELLBEING_PATH}flags/{int(flag_id)}/follow-up/"
        try:
            response = send(self.session, "POST", url, json=values)
        except IntegrationError as exc:
            if exc.status in (400, 409) and exc.response is not None:
                return exc.status, exc.response.json()
            raise
        return response.status_code, response.json()
