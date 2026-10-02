"""Compiler's figures APIs: the youth indicator figures (GET /api/youth/indicator-figures/?year=) and
the education programmes' stored counts (GET /api/figures/ and /api/figures/<programme>/?year=).

NeuroDB decides when BMA calculates: BMA keeps no schedule of its own. A sync asks BMA for a
calculation (POST /api/figures/runs/ or /api/wellbeing/runs/), follows it until it is done
(GET .../runs/<id>/) and then reads the results. One calculation runs at a time in BMA: asking while
one is going returns that one.

The token belongs to a Compiler service account in the "NeuroDB API" group; it is sent as
``Authorization: Token <key>`` and never logged (see ``integrations/http.py``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from django.conf import settings

from neurodb.integrations.http import IntegrationError, get_json, make_session, send

PATH = "/api/youth/indicator-figures/"
EDUCATION_PATH = "/api/figures/"
WELLBEING_PATH = "/api/wellbeing/"
RUNS = {"education": EDUCATION_PATH + "runs/", "wellbeing": WELLBEING_PATH + "runs/"}
DONE = ("succeeded", "failed")


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
        """The stored counts of ``programme`` for ``year``; None when Compiler has not counted them yet
        (404; an older Compiler answered 202 and counted in the background) or has no such year."""
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

    # ------------------------------------------------------------- calculations NeuroDB asks for
    def start_run(self, kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Ask BMA to calculate (``kind`` "education" or "wellbeing"); the run queued, or the one already
        going. None when this BMA cannot be asked yet (no runs address: an older BMA)."""
        url = self.base_url + RUNS[kind]
        try:
            response = send(self.session, "POST", url, json=payload or {})
        except IntegrationError as exc:
            if exc.status in (404, 405):
                return None
            raise
        return _run(response.json(), url)

    def run_status(self, kind: str, run_id: int) -> dict[str, Any]:
        url = f"{self.base_url}{RUNS[kind]}{int(run_id)}/"
        return _run(get_json(self.session, url), url)


def _run(data: Any, url: str) -> dict[str, Any]:
    if not isinstance(data, dict) or "id" not in data or "status" not in data:
        raise IntegrationError("Compiler calculation: unexpected response", url=url)
    return data


def calculate(
    client: CompilerClient,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Ask BMA to calculate and wait until it is done. Returns what to record on the sync run:
    ``{"status": "succeeded" | "failed" | "timed out" | "unavailable", "run": id, "error": ...}``.
    Never raises: whatever happens, the sync goes on to read what BMA has."""
    try:
        run = client.start_run(kind, payload)
        if run is None:
            return {"status": "unavailable", "error": "this BMA cannot be asked to calculate"}
        deadline = clock() + settings.COMPILER_RUN_TIMEOUT_MINUTES * 60
        while run["status"] not in DONE:
            if clock() >= deadline:
                return {
                    "status": "timed out",
                    "run": run["id"],
                    "error": f"still {run['status']} after {settings.COMPILER_RUN_TIMEOUT_MINUTES} minutes",
                }
            sleep(settings.COMPILER_RUN_POLL_SECONDS)
            run = client.run_status(kind, run["id"])
    except IntegrationError as exc:
        return {"status": "failed", "error": str(exc)[:500]}
    out = {"status": run["status"], "run": run["id"], "finished_at": run.get("finished_at")}
    if run.get("error"):  # a count may succeed in part: the parts that failed are said here
        out["error"] = run["error"][:500]
    return out


def calculation_problem(calculation: dict[str, Any] | None) -> str:
    """What went wrong with the calculation, for the run's error; empty when nothing did (or when this
    BMA cannot be asked: what it has is read as before)."""
    if not calculation or calculation["status"] not in ("failed", "timed out"):
        return ""
    return f"BMA calculation {calculation['status']}: {calculation.get('error') or 'no reason given'}"
