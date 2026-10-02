"""NeuroDB decides when BMA calculates: it asks for a calculation, follows it until it is done and
only then reads (BMA keeps no schedule)."""

import pytest
import responses

from neurodb.integrations.http import IntegrationError
from neurodb.youth.compiler import CompilerClient, calculate, calculation_problem

BASE = "https://compiler.test"


class Steps:
    """A BMA whose run goes through ``statuses``, and a clock that moves by the time slept."""

    def __init__(self, *statuses):
        self.statuses, self.now, self.slept, self.asked = list(statuses), 0.0, [], []

    def start_run(self, kind, payload=None):
        self.asked.append(kind)
        return {"id": 7, "status": self.statuses.pop(0)}

    def run_status(self, kind, run_id):
        return {"id": run_id, "status": self.statuses.pop(0), "error": "db gone", "finished_at": "x"}

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now


def run(bma, kind="wellbeing"):
    return calculate(bma, kind, sleep=bma.sleep, clock=bma.clock)


def test_it_waits_until_bma_is_done(settings):
    settings.COMPILER_RUN_POLL_SECONDS = 60
    bma = Steps("queued", "running", "running", "succeeded")
    assert run(bma) == {"status": "succeeded", "run": 7, "finished_at": "x", "error": "db gone"}
    assert bma.slept == [60, 60, 60] and bma.asked == ["wellbeing"]
    assert calculation_problem(run(Steps("succeeded"))) == ""


def test_a_failed_slow_or_unreachable_calculation_is_reported_not_raised(settings):
    settings.COMPILER_RUN_POLL_SECONDS, settings.COMPILER_RUN_TIMEOUT_MINUTES = 60, 3
    failed = run(Steps("queued", "failed"))
    assert calculation_problem(failed) == "BMA calculation failed: db gone"
    slow = run(Steps(*["running"] * 10))
    assert slow["status"] == "timed out" and "after 3 minutes" in calculation_problem(slow)

    class Down(Steps):
        def start_run(self, kind, payload=None):
            raise IntegrationError("POST /api/wellbeing/runs/: HTTP 503")

    assert run(Down())["status"] == "failed"

    class Older(Steps):  # no runs address yet
        def start_run(self, kind, payload=None):
            return None

    older = run(Older())
    assert older["status"] == "unavailable" and calculation_problem(older) == ""


@responses.activate
def test_the_client_starts_and_follows_runs():
    responses.post(BASE + "/api/figures/runs/", status=202, json={"id": 3, "status": "queued"})
    responses.get(BASE + "/api/figures/runs/3/", json={"id": 3, "status": "succeeded", "counted": []})
    responses.post(BASE + "/api/wellbeing/runs/", status=404, json={"detail": "Not found."})
    client = CompilerClient(BASE, "abc123")
    assert client.start_run("education") == {"id": 3, "status": "queued"}
    assert client.run_status("education", 3)["status"] == "succeeded"
    assert client.start_run("wellbeing") is None  # an older BMA: read what it has
    assert responses.calls[0].request.body == b"{}"
    responses.get(BASE + "/api/figures/runs/4/", json={"oops": True})
    with pytest.raises(IntegrationError, match="unexpected"):
        client.run_status("education", 4)
