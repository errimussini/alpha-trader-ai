"""Verifica la forma delle richieste HTTP verso l'API Anthropic senza rete (trasporto finto)."""
import json

import httpx2 as httpx

from app.ai.llm import AnthropicLLM


def _llm(responses, seen):
    def handler(req: httpx.Request):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=responses.pop(0), headers={"request-id": "req_test"})
    llm = AnthropicLLM("sk-test", "claude-opus-5-5", "medium", 1000, 30, True)
    import anthropic
    llm.client = anthropic.Anthropic(api_key="sk-test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return llm


def _msg(content, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 3}}


def test_structured_request_shape():
    seen = []
    llm = _llm([_msg([{"type": "text", "text": '{"ok": true}'}])], seen)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    r = llm.structured(system="s", user="u", schema=schema)
    assert r.data == {"ok": True} and r.meta["request_id"] == "req_test" and r.meta["usage"]["input_tokens"] == 12
    body = seen[0]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": schema}}
    assert "temperature" not in body and "thinking" not in body


def test_tool_loop_round_trip():
    seen = []
    llm = _llm([
        _msg([{"type": "tool_use", "id": "tu_1", "name": "get_positions", "input": {}}], stop="tool_use"),
        _msg([{"type": "text", "text": "Nessuna posizione."}]),
    ], seen)
    r = llm.run_tools(system="s", messages=[{"role": "user", "content": "q"}],
                      tools=[{"name": "get_positions", "description": "d", "strict": True,
                              "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}],
                      handler=lambda n, a: {"count": 0}, max_steps=3)
    assert r.data == "Nessuna posizione." and r.meta["tool_calls"][0]["output"] == {"count": 0}
    second = seen[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][0]["type"] == "tool_use"
    assert second[2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu_1", "content": '{"count": 0}'}
    assert seen[1]["tool_choice"] == {"type": "auto"}
