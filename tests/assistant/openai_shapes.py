"""The response shapes a feature on OpenAI meets, for the parity tests (test_openai_parity.py in
tests/assistant, tests/fmm and tests/knowledge).

Each feature that calls the Responses API reads its answer by its own rules. When it moves onto the
provider layer (``llm``) it must read every shape exactly as it did: a completed answer, an incomplete one
for each reason, a refusal part, a failed response, and a partial fake with no status at all. Each shape
carries the same answer text, so a rule that looks at the status shows; ``empty`` is a completed answer
with no text. The outcomes each feature stores were recorded once from the code before the provider layer
(fbe0b64), and the parity tests compare against them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

SHAPES = (
    "completed",
    "max_output_tokens",
    "content_filter",
    "other_reason",
    "refusal",
    "failed",
    "partial_fake",
    "empty",
)
USAGE = {"input_tokens": 150, "cached": 50, "output_tokens": 20}


def shaped(shape: str, text: str) -> Any:
    """A Responses API response of ``shape`` whose output_text is ``text`` (none for ``empty``)."""
    if shape == "partial_fake":
        return SimpleNamespace(output_text=text)
    if shape == "empty":
        text = ""
    status = {"completed": "completed", "refusal": "completed", "empty": "completed", "failed": "failed"}.get(
        shape, "incomplete"
    )
    reason = {"max_output_tokens": "max_output_tokens", "content_filter": "content_filter"}.get(shape)
    if shape == "other_reason":
        reason = "something_new"
    part = (
        SimpleNamespace(type="refusal", refusal="I can't help with that.")
        if shape == "refusal"
        else SimpleNamespace(type="output_text", text=text)
    )
    return SimpleNamespace(
        output_text=text,
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output=[SimpleNamespace(type="message", content=[part])],
        error=SimpleNamespace(code="server_error", message="The server had an error.")
        if shape == "failed"
        else None,
        usage=SimpleNamespace(
            input_tokens=USAGE["input_tokens"],
            input_tokens_details=SimpleNamespace(cached_tokens=USAGE["cached"]),
            output_tokens=USAGE["output_tokens"],
        ),
        model="gpt-5.5",
    )


class Scripted:
    """``agent.client()`` for the parity tests: every call answers ``response``; ``calls`` records the
    requests sent."""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)
        self.max_retries = 2

    def with_options(self, **options: Any) -> Scripted:
        return self

    def _create(self, **params: Any) -> Any:
        self.calls.append(params)
        return self.response


def install(monkeypatch, response: Any) -> Scripted:
    """Make ``agent.client()`` return a :class:`Scripted` client answering ``response``."""
    from neurodb.assistant import agent

    api = Scripted(response)
    monkeypatch.setattr(agent, "client", lambda: api)
    return api


def outcome_of(call) -> Any:
    """What ``call()`` gave, or the name of what it raised (a feature's own failure is part of its
    outcome)."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - the outcome is the exception's name and message
        return ["raised", type(exc).__name__, str(exc)[:200]]
