"""What every request NeuroDB sends to Claude must look like, checked on the body the stand-in received.

Every key, nested ones included, must be one the anthropic SDK declares (its request TypedDicts), so a
key the Messages API does not know can never reach it; and the request never carries what Claude Opus 5.5
refuses with a 400 (thinking, temperature, top_p, top_k, tool_choice), a structured-output schema keyword
it does not take, nor the fallback parameter without its header (or the header without the parameter).
"""

from __future__ import annotations

import typing

from anthropic.types.beta import (
    BetaBase64PDFSourceParam,
    BetaCacheControlEphemeralParam,
    BetaJSONOutputFormatParam,
    BetaMessageParam,
    BetaMetadataParam,
    BetaOutputConfigParam,
    BetaRedactedThinkingBlockParam,
    BetaRequestDocumentBlockParam,
    BetaTextBlockParam,
    BetaThinkingBlockParam,
    BetaToolParam,
    BetaToolResultBlockParam,
    BetaToolUseBlockParam,
    message_create_params,
)
from django.conf import settings

from neurodb.assistant.llm import claude_backend

REFUSED = {"thinking", "temperature", "top_p", "top_k", "tool_choice"}
# The JSON schema keywords structured outputs take (anthropic.transform_schema keeps these and moves any
# other into the description)
SCHEMA_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "items", "enum", "anyOf", "allOf", "$ref",
    "$defs", "description", "title", "format", "minItems",
}  # fmt: skip
BLOCKS = {
    "text": BetaTextBlockParam,
    "thinking": BetaThinkingBlockParam,
    "redacted_thinking": BetaRedactedThinkingBlockParam,
    "tool_use": BetaToolUseBlockParam,
    "tool_result": BetaToolResultBlockParam,
    "document": BetaRequestDocumentBlockParam,
}


def _keys(typed: type) -> tuple[set[str], set[str]]:
    """The keys a TypedDict declares, and the required ones among them."""
    hints = typing.get_type_hints(typed, include_extras=True)
    return set(hints), {name for name, hint in hints.items() if "Required[" in str(hint)}


def _declared(value: dict, typed: type, where: str) -> None:
    declared, required = _keys(typed)
    assert set(value) <= declared, f"{where}: {sorted(set(value) - declared)} not in {typed.__name__}"
    assert required <= set(value), f"{where}: {sorted(required - set(value))} missing ({typed.__name__})"


def _block(block: dict, where: str) -> None:
    kind = block.get("type")
    assert kind in BLOCKS, f"{where}: a {kind} block"
    _declared(block, BLOCKS[kind], where)
    if "cache_control" in block:
        _declared(block["cache_control"], BetaCacheControlEphemeralParam, f"{where}.cache_control")
    if kind == "document":
        _declared(block["source"], BetaBase64PDFSourceParam, f"{where}.source")
    if kind == "text":
        assert block["text"].strip(), f"{where}: an empty text block (a 400)"
    if kind == "tool_result" and isinstance(block.get("content"), list):
        for i, part in enumerate(block["content"]):
            _block(part, f"{where}.content[{i}]")


def _schema(node: object, where: str) -> None:
    if isinstance(node, list):
        for i, item in enumerate(node):
            _schema(item, f"{where}[{i}]")
        return
    if not isinstance(node, dict):
        return
    assert set(node) <= SCHEMA_KEYWORDS, f"{where}: {sorted(set(node) - SCHEMA_KEYWORDS)}"
    assert not isinstance(node.get("type"), list), f"{where}: a type list"
    if node.get("type") == "object":
        assert node.get("additionalProperties") is False, f"{where}: additionalProperties is not false"
    for key, value in node.items():
        if key in ("properties", "$defs"):
            for name, sub in value.items():
                _schema(sub, f"{where}.{key}.{name}")
        elif key in ("items", "anyOf", "allOf"):
            _schema(value, f"{where}.{key}")


def assert_claude_params(body: dict, headers: dict, *, stream: bool = True) -> None:
    """``body`` and ``headers`` (lower-cased names) of one request to ``/v1/messages``."""
    typed = (
        message_create_params.MessageCreateParamsStreaming
        if stream
        else message_create_params.MessageCreateParamsNonStreaming
    )
    declared, required = _keys(typed)
    declared -= {"betas"}  # a header, never in the body
    assert set(body) <= declared, sorted(set(body) - declared)
    assert required - {"stream"} <= set(body), sorted(required - set(body))
    if stream:
        assert body.get("stream") is True
    assert not REFUSED & set(body), sorted(REFUSED & set(body))

    output = body.get("output_config")
    assert output is not None, "output_config.effort is always set"
    _declared(output, BetaOutputConfigParam, "output_config")
    assert output["effort"] in settings.CLAUDE_EFFORTS
    if "format" in output:
        _declared(output["format"], BetaJSONOutputFormatParam, "output_config.format")
        assert output["format"]["type"] == "json_schema"
        _schema(output["format"]["schema"], "schema")
    if "metadata" in body:
        _declared(body["metadata"], BetaMetadataParam, "metadata")
    if "cache_control" in body:
        _declared(body["cache_control"], BetaCacheControlEphemeralParam, "cache_control")

    for i, block in enumerate(body.get("system") or []):
        _block(block, f"system[{i}]")
    for i, message in enumerate(body["messages"]):
        _declared(message, BetaMessageParam, f"messages[{i}]")
        content = message["content"]
        if isinstance(content, str):
            assert content.strip(), f"messages[{i}]: an empty text"
            continue
        for k, block in enumerate(content):
            _block(block, f"messages[{i}].content[{k}]")
    for i, tool in enumerate(body.get("tools") or []):
        _declared(tool, BetaToolParam, f"tools[{i}]")
        assert tool.get("eager_input_streaming") is True, f"tools[{i}]: eager_input_streaming"

    beta = claude_backend.FALLBACK_BETA in headers.get("anthropic-beta", "")
    assert (body.get("fallbacks") == "default") is beta, "fallbacks and their header go together"
    if stream:
        assert body["max_tokens"] >= claude_backend.MIN_OUTPUT_TOKENS
