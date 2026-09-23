"""The assistant with the real Anthropic SDK against a local stand-in for the Messages API.

The scripted-client tests cover the loop; this one checks what the SDK actually puts on the wire
(beta header, fallbacks, tools, tool results built from the SDK's own message objects) and that the
SSE stream it parses drives the loop, without network access or an API key.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from django.test import override_settings

from neurodb.assistant import agent


def _sse(*events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _start(message_id):
    return {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 120, "output_tokens": 1, "cache_read_input_tokens": 80},
        },
    }


TOOL_TURN = _sse(
    _start("msg_1"),
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Checking."}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "toolu_A", "name": "data_freshness", "input": {}},
    },
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
    {"type": "content_block_stop", "index": 1},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 30},
    },
    {"type": "message_stop"},
)
ANSWER_TURN = _sse(
    _start("msg_2"),
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "The data is "}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "**fresh**."}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 12},
    },
    {"type": "message_stop"},
)


@pytest.fixture
def fake_api():
    seen = []
    replies = [TOOL_TURN, ANSWER_TURN]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(
                {"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
            )
            payload = replies.pop(0)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()


def test_real_sdk_round_trip(fake_api, monkeypatch, db):
    base_url, seen = fake_api
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
    for var in ("NO_PROXY", "no_proxy"):  # reach the local stand-in directly even behind a proxy
        monkeypatch.setenv(var, "127.0.0.1,localhost")
    with override_settings(AI_ASSISTANT_ENABLED=True, ANTHROPIC_API_KEY="test-key-not-real"):
        outcome = agent.Outcome()
        events = list(agent.answer("Is the data up to date?", [], outcome))

    assert [e["type"] for e in events] == ["text", "tool", "text", "text", "done"]
    assert events[-1]["answer"] == "The data is **fresh**."
    assert "<strong>fresh</strong>" in events[-1]["html"]
    assert outcome.tools[0]["tool"] == "data_freshness" and outcome.tools[0]["ok"]
    assert outcome.input_tokens == 240 and outcome.cache_read_tokens == 160 and outcome.output_tokens == 42

    first, second = seen
    assert first["path"].startswith("/v1/messages")
    assert "server-side-fallback-2026-07-01" in first["headers"].get("anthropic-beta", "")
    assert first["headers"].get("x-api-key") == "test-key-not-real"
    assert first["body"]["fallbacks"] == "default"
    assert first["body"]["stream"] is True
    assert first["body"]["output_config"] == {"effort": "medium"}
    assert first["body"]["tools"][0]["eager_input_streaming"] is True
    # turn 2 replays the assistant turn (from the SDK's own objects) and answers the tool call
    assistant_turn, tool_results = second["body"]["messages"][-2:]
    assert [b["type"] for b in assistant_turn["content"]] == ["text", "tool_use"]
    assert tool_results["content"][0]["tool_use_id"] == "toolu_A"
    assert json.loads(tool_results["content"][0]["content"])["url"] == "/data/health/"
