"""The sampling guard (fmm.ai.sampling): temperature and top_p are sent only when a prompt version sets
them and the model has not refused them; a refusal the API names drops that parameter alone, is kept per
model and effort, and the call is made again (at most twice). Ask NeuroDB never sends either."""

import datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx2
import openai
import pytest
from django.test import override_settings
from django.utils import timezone

from neurodb.assistant import agent
from neurodb.fmm.ai import sampling
from neurodb.fmm.models import ModelCapability

pytestmark = pytest.mark.django_db

MODEL, EFFORT = "gpt-5.5", "low"


def _version(temperature=None, top_p=None):
    return SimpleNamespace(
        temperature=None if temperature is None else Decimal(str(temperature)),
        top_p=None if top_p is None else Decimal(str(top_p)),
    )


def refusal(param=None, code="unsupported_parameter", message=None):
    """The API's 400 for a parameter the model does not take."""
    message = message or f"Unsupported parameter: '{param}' is not supported with this model."
    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.openai.com/v1/responses"))
    body = {"message": message, "type": "invalid_request_error", "param": param, "code": code}
    return openai.BadRequestError(message, response=response, body=body)


class FakeInsightsClient:
    """``responses.create`` records each call's parameters and returns (or raises) the next answer."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[dict] = []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **params):
        self.requests.append(params)
        answer = self.answers.pop(0) if self.answers else SimpleNamespace(output_text="{}")
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _call(client, version, model=MODEL, effort=EFFORT):
    plan = sampling.plan(version, model, effort)
    return sampling.call(client, {"model": model, "input": "x"}, plan, model, effort)


def _accepted(parameter, model=MODEL, effort=EFFORT):
    row = ModelCapability.objects.filter(model=model, effort=effort, parameter=parameter).first()
    return None if row is None else row.accepted


# ------------------------------------------------------------------------------------------ the plan
def test_not_set_means_the_keys_are_absent():
    client = FakeInsightsClient()
    _, states = _call(client, _version())
    assert "temperature" not in client.requests[0] and "top_p" not in client.requests[0]
    assert states == {"temperature": "not_set", "top_p": "not_set"}
    assert not ModelCapability.objects.exists()


def test_a_set_value_not_known_is_sent_and_recorded_as_accepted():
    client = FakeInsightsClient()
    _, states = _call(client, _version(temperature=0.3))
    assert client.requests[0]["temperature"] == 0.3 and "top_p" not in client.requests[0]
    assert states == {"temperature": "applied", "top_p": "not_set"}
    assert _accepted("temperature") is True and _accepted("top_p") is None
    assert sampling.summary(_version(temperature=0.3), states, MODEL, EFFORT)["temperature"]["asked"] == 0.3


@override_settings(FMM_SAMPLING="off")
def test_sampling_off_never_sends_them():
    plan = sampling.plan(_version(temperature=0.3, top_p=0.9), MODEL, EFFORT)
    assert plan.params == {} and plan.states == {"temperature": "off", "top_p": "off"}


# ------------------------------------------------------------------------------------------ refusals
def test_a_refused_parameter_is_dropped_alone_and_the_call_made_again():
    client = FakeInsightsClient(refusal("temperature"), SimpleNamespace(output_text="{}"))
    response, states = _call(client, _version(temperature=0.3, top_p=0.9))
    assert response.output_text == "{}"
    first, second = client.requests
    assert first["temperature"] == 0.3 and first["top_p"] == 0.9
    assert "temperature" not in second and second["top_p"] == 0.9  # top_p still sent
    assert states == {"temperature": "not_applied", "top_p": "applied"}
    assert _accepted("temperature") is False and _accepted("top_p") is True
    row = ModelCapability.objects.get(parameter="temperature")
    assert "not supported" in row.detail
    why = sampling.why("temperature", "not_applied", MODEL, EFFORT)
    assert why.startswith("OpenAI refused temperature for gpt-5.5 at effort low (checked ")


def test_a_second_refusal_of_the_other_one_gets_a_second_retry():
    client = FakeInsightsClient(refusal("temperature"), refusal("top_p"), SimpleNamespace(output_text="{}"))
    _, states = _call(client, _version(temperature=0.3, top_p=0.9))
    assert len(client.requests) == 3
    assert "temperature" not in client.requests[2] and "top_p" not in client.requests[2]
    assert states == {"temperature": "not_applied", "top_p": "not_applied"}
    assert _accepted("temperature") is False and _accepted("top_p") is False


def test_never_more_than_two_retries():
    client = FakeInsightsClient(refusal("temperature"), refusal("top_p"), refusal("top_p"))
    with pytest.raises(openai.BadRequestError):
        _call(client, _version(temperature=0.3, top_p=0.9))
    assert len(client.requests) == 3


def test_a_parameter_is_never_marked_refused_unless_the_api_names_it():
    # a 400 about something else: no retry, nothing recorded
    client = FakeInsightsClient(refusal("reasoning.effort", code="unsupported_value", message="Bad effort."))
    with pytest.raises(openai.BadRequestError):
        _call(client, _version(temperature=0.3))
    assert len(client.requests) == 1 and not ModelCapability.objects.exists()
    # a refusal naming a parameter that was not sent is raised as it is
    client = FakeInsightsClient(refusal("top_p"))
    with pytest.raises(openai.BadRequestError):
        _call(client, _version(temperature=0.3))
    assert len(client.requests) == 1 and not ModelCapability.objects.exists()


def test_the_parameter_is_read_from_the_message_when_param_is_empty():
    named = refusal(None, code="", message="Unsupported value: 'top-p' is not supported with this model.")
    assert sampling.unsupported_param(named) == "top_p"
    assert sampling.unsupported_param(refusal("temperature")) == "temperature"
    assert sampling.unsupported_param(refusal(None, code="other", message="Something else.")) is None
    assert sampling.unsupported_param(ValueError("temperature not supported")) is None


# ------------------------------------------------------------------------------------------ known refusals
def test_a_known_refusal_is_not_sent_until_it_is_checked_again():
    sampling.record(MODEL, EFFORT, "temperature", accepted=False, detail="refused")
    plan = sampling.plan(_version(temperature=0.3), MODEL, EFFORT)
    assert plan.params == {} and plan.states["temperature"] == "known_rejected"
    # another effort is tried
    assert sampling.plan(_version(temperature=0.3), MODEL, "high").params == {"temperature": 0.3}
    # after FMM_SAMPLING_RECHECK_DAYS it is tried again
    ModelCapability.objects.update(checked_at=timezone.now() - datetime.timedelta(days=31))
    assert sampling.plan(_version(temperature=0.3), MODEL, EFFORT).params == {"temperature": 0.3}
    with override_settings(FMM_SAMPLING_RECHECK_DAYS=40):
        assert sampling.plan(_version(temperature=0.3), MODEL, EFFORT).params == {}


def test_record_keeps_one_row_per_model_effort_and_parameter():
    sampling.record(MODEL, EFFORT, "temperature", accepted=False, detail="x" * 500)
    sampling.record(MODEL, EFFORT, "temperature", accepted=True)
    row = ModelCapability.objects.get()
    assert row.accepted is True and row.detail == ""
    sampling.record(MODEL, EFFORT, "top_p", accepted=False, detail="y" * 500)
    assert len(ModelCapability.objects.get(parameter="top_p").detail) == 300


# ------------------------------------------------------------------------------------------ Ask is unchanged
def test_asks_request_never_carries_sampling():
    params = agent._request(None)
    assert "temperature" not in params and "top_p" not in params
    assert "temperature" not in agent._background_request(agent.RunOptions())
    merged = agent._background_request(agent.RunOptions(sampling=(("top_p", 0.9),)))
    assert merged["top_p"] == 0.9 and "temperature" not in merged
