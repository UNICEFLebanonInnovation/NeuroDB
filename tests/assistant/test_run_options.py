"""RunOptions' two additions of Monitoring insights' AI (stage 6a): a run's own whole prompt
(``base_prompt``) and sampling parameters (``sampling``). Both are off by default, so Ask NeuroDB's and
NeuroDB Watch's requests stay exactly what they were."""

import pytest
from django.test import override_settings
from django.utils import timezone

from neurodb.assistant import agent
from neurodb.watch import investigate
from tests.assistant.test_assistant import ENABLED, FakeClient, reply, say

pytestmark = pytest.mark.django_db

# The keys of a background run's request before stage 6a: none may be added by default.
BACKGROUND_KEYS = {
    "model",
    "instructions",
    "tools",
    "reasoning",
    "max_output_tokens",
    "parallel_tool_calls",
    "store",
    "include",
    "prompt_cache_key",
    "text",
}


def _before(extra: str = "") -> str:
    """The instructions as ``agent._instructions`` wrote them before stage 6a (pinned)."""
    from neurodb.indicators.services.navigation import current_year

    today = timezone.localdate()
    year = current_year()
    text = (
        f"{agent.SYSTEM_PROMPT}\nToday is {today:%A %d %B %Y}. "
        f"The current reporting year is {year.name if year else 'unknown'}."
    )
    return f"{text}\n\n{extra.strip()}" if extra.strip() else text


def test_asks_instructions_are_unchanged(reporting_year):
    assert agent._instructions() == _before()
    assert agent._request(None)["instructions"] == _before()
    assert agent.effective_instructions(agent.RunOptions()) == _before()
    assert agent.effective_instructions(agent.RunOptions(instructions="More.")) == _before("More.")


def test_watchs_investigate_request_is_unchanged(reporting_year):
    options = investigate.run_options([], [])
    assert options.base_prompt is None and options.sampling == ()
    params = agent._background_request(options)
    assert params["instructions"] == _before(investigate.INSTRUCTIONS)
    assert set(params) == BACKGROUND_KEYS
    assert "temperature" not in params and "top_p" not in params


def test_a_run_with_its_own_prompt_sends_it_in_place_of_asks(reporting_year):
    options = agent.RunOptions(base_prompt="X", instructions="The visits in scope: all.")
    today = timezone.localdate()
    text = agent.effective_instructions(options)
    assert text == f"X\nToday is {today:%A %d %B %Y}.\n\nThe visits in scope: all."
    assert agent._background_request(options)["instructions"] == text
    assert agent.SYSTEM_PROMPT not in text and "reporting year" not in text
    assert "list_databases" not in text and "etools_query" not in text
    # without extra instructions, the date ends it
    assert (
        agent.effective_instructions(agent.RunOptions(base_prompt="X")) == f"X\nToday is {today:%A %d %B %Y}."
    )


def test_sampling_is_merged_only_when_given():
    assert "temperature" not in agent._background_request(agent.RunOptions())
    params = agent._background_request(agent.RunOptions(sampling=(("temperature", 0.3),)))
    assert params["temperature"] == 0.3 and "top_p" not in params
    both = agent._background_request(agent.RunOptions(sampling=(("temperature", 0.3), ("top_p", 0.9))))
    assert (both["temperature"], both["top_p"]) == (0.3, 0.9)
    assert set(both) == BACKGROUND_KEYS - {"text", "tools", "parallel_tool_calls"} | {"temperature", "top_p"}


@override_settings(**ENABLED)
def test_the_request_sent_carries_exactly_the_effective_instructions(monkeypatch, reporting_year):
    model = FakeClient([reply(say("Two visits."))])
    monkeypatch.setattr(agent, "client", lambda: model)
    options = agent.RunOptions(
        base_prompt="FMM prompt", instructions="Scope.", sampling=(("temperature", 0.3),)
    )
    outcome = agent.Outcome()
    events = list(agent.answer("How many?", [], outcome, options=options))
    assert events[-1]["type"] == "done"
    sent = model.requests[0]
    assert sent["instructions"] == agent.effective_instructions(options)
    assert sent["temperature"] == 0.3 and "top_p" not in sent
