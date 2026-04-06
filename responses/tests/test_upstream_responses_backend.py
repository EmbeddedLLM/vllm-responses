from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sse_test_utils import extract_completed_response, parse_sse_frames, parse_sse_json_events

from vllm_responses.entrypoints import llm as mock_llm


def _use_upstream_responses_backend(app: FastAPI) -> None:
    runtime_config = app.state.vllm_responses.runtime_config
    app.state.vllm_responses.runtime_config = replace(
        runtime_config,
        upstream_api_kind="responses",
    )


def _weather_tool(*, parameter_name: str) -> dict[str, object]:
    return {
        "type": "function",
        "name": "get_weather",
        "parameters": {
            "type": "object",
            "properties": {parameter_name: {"type": "string"}},
            "required": [parameter_name],
            "additionalProperties": False,
        },
    }


@pytest.mark.anyio
async def test_gateway_streams_text_and_reasoning_via_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "qwen35a3b-responses-text-stream.yaml"
    )

    async with gateway_client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "Qwen/Qwen3.5-35B-A3B",
            "stream": True,
            "input": [{"role": "user", "content": "hello"}],
        },
    ) as resp:
        assert resp.status_code == 200
        body = await resp.aread()

    frames = parse_sse_frames(body.decode("utf-8", errors="replace"))
    events = parse_sse_json_events(frames)
    completed = extract_completed_response(events)

    message_texts = [
        part.get("text", "")
        for item in completed.get("output", [])
        if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "output_text"
    ]
    reasoning_texts = [
        content.get("text", "")
        for item in completed.get("output", [])
        if isinstance(item, dict) and item.get("type") == "reasoning"
        for content in item.get("content", [])
        if isinstance(content, dict) and content.get("type") == "reasoning_text"
    ]

    assert any(text.strip() == "hello" for text in message_texts)
    assert any(text.strip() for text in reasoning_texts)


@pytest.mark.anyio
async def test_gateway_non_stream_text_and_reasoning_via_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    # LMEngine still drives upstream Responses through streaming collection even when the
    # downstream request is non-streaming.
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "qwen35a3b-responses-text-stream.yaml"
    )

    resp = await gateway_client.post(
        "/v1/responses",
        json={
            "model": "Qwen/Qwen3.5-35B-A3B",
            "stream": False,
            "input": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 200
    completed = resp.json()

    message_texts = [
        part.get("text", "")
        for item in completed.get("output", [])
        if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "output_text"
    ]
    reasoning_texts = [
        content.get("text", "")
        for item in completed.get("output", [])
        if isinstance(item, dict) and item.get("type") == "reasoning"
        for content in item.get("content", [])
        if isinstance(content, dict) and content.get("type") == "reasoning_text"
    ]

    assert any(text.strip() == "hello" for text in message_texts)
    assert any(text.strip() for text in reasoning_texts)


@pytest.mark.anyio
async def test_gateway_streams_function_tool_events_via_harmony_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "gptoss20b-responses-function-stream-auto.yaml"
    )

    async with gateway_client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "openai/gpt-oss-20b",
            "stream": True,
            "input": [{"role": "user", "content": "What is the weather in Paris?"}],
            "tools": [_weather_tool(parameter_name="location")],
            "tool_choice": "auto",
        },
    ) as resp:
        assert resp.status_code == 200
        body = await resp.aread()

    events = parse_sse_json_events(parse_sse_frames(body.decode("utf-8", errors="replace")))
    completed = extract_completed_response(events)

    event_types = [event.get("type") for event in events]
    assert "response.function_call_arguments.delta" in event_types
    assert "response.function_call_arguments.done" in event_types
    assert any(
        isinstance(item, dict) and item.get("type") == "function_call"
        for item in completed.get("output", [])
    )


@pytest.mark.anyio
async def test_gateway_non_stream_function_tool_request_via_harmony_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "gptoss20b-responses-function-stream-auto.yaml"
    )

    resp = await gateway_client.post(
        "/v1/responses",
        json={
            "model": "openai/gpt-oss-20b",
            "stream": False,
            "input": [{"role": "user", "content": "What is the weather in Paris?"}],
            "tools": [_weather_tool(parameter_name="location")],
            "tool_choice": "auto",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert any(
        isinstance(item, dict) and item.get("type") == "function_call"
        for item in data.get("output", [])
    )


@pytest.mark.anyio
async def test_gateway_rejects_non_auto_function_tool_choice_for_harmony_upstream_responses(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
):
    _use_upstream_responses_backend(gateway_app)

    resp = await gateway_client.post(
        "/v1/responses",
        json={
            "model": "openai/gpt-oss-20b",
            "input": [{"role": "user", "content": "What is the weather in Paris?"}],
            "tools": [_weather_tool(parameter_name="city")],
            "tool_choice": "required",
        },
    )

    assert resp.status_code == 422
    assert 'tool_choice="auto"' in resp.json()["message"]


@pytest.mark.anyio
async def test_gateway_non_harmony_streams_auto_function_tool_events_via_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "qwen35a3b-responses-function-stream-auto.yaml"
    )

    async with gateway_client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "Qwen/Qwen3.5-35B-A3B",
            "stream": True,
            "input": [{"role": "user", "content": "What is the weather in Boston?"}],
            "tools": [_weather_tool(parameter_name="city")],
            "tool_choice": "auto",
        },
    ) as resp:
        assert resp.status_code == 200
        body = await resp.aread()

    events = parse_sse_json_events(parse_sse_frames(body.decode("utf-8", errors="replace")))
    completed = extract_completed_response(events)

    event_types = [event.get("type") for event in events]
    assert "response.function_call_arguments.delta" in event_types
    assert "response.function_call_arguments.done" in event_types
    assert any(
        isinstance(item, dict) and item.get("type") == "function_call"
        for item in completed.get("output", [])
    )


@pytest.mark.anyio
async def test_gateway_non_harmony_non_stream_function_tool_request_via_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "qwen35a3b-responses-function-stream-required.yaml"
    )

    resp = await gateway_client.post(
        "/v1/responses",
        json={
            "model": "Qwen/Qwen3.5-35B-A3B",
            "stream": False,
            "input": [{"role": "user", "content": "What is the weather in Boston?"}],
            "tools": [_weather_tool(parameter_name="city")],
            "tool_choice": "required",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert any(
        isinstance(item, dict) and item.get("type") == "function_call"
        for item in data.get("output", [])
    )


@pytest.mark.anyio
async def test_gateway_non_harmony_streams_required_function_tool_events_via_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "qwen35a3b-responses-function-stream-required.yaml"
    )

    async with gateway_client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "Qwen/Qwen3.5-35B-A3B",
            "stream": True,
            "input": [{"role": "user", "content": "What is the weather in Boston?"}],
            "tools": [_weather_tool(parameter_name="city")],
            "tool_choice": "required",
        },
    ) as resp:
        assert resp.status_code == 200
        body = await resp.aread()

    events = parse_sse_json_events(parse_sse_frames(body.decode("utf-8", errors="replace")))
    completed = extract_completed_response(events)

    assert any(
        isinstance(item, dict) and item.get("type") == "function_call"
        for item in completed.get("output", [])
    )


@pytest.mark.anyio
async def test_gateway_non_harmony_named_function_choice_via_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "qwen35a3b-responses-function-named-choice-stream.yaml"
    )

    resp = await gateway_client.post(
        "/v1/responses",
        json={
            "model": "Qwen/Qwen3.5-35B-A3B",
            "stream": False,
            "input": [{"role": "user", "content": "What is the weather in Boston?"}],
            "tools": [_weather_tool(parameter_name="city")],
            "tool_choice": {"type": "function", "name": "get_weather"},
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    function_call = next(
        item
        for item in data.get("output", [])
        if isinstance(item, dict) and item.get("type") == "function_call"
    )
    assert function_call["name"] == "get_weather"


@pytest.mark.anyio
async def test_gateway_retrieve_response_uses_local_store_with_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "qwen35a3b-responses-text-stream.yaml"
    )

    create_resp = await gateway_client.post(
        "/v1/responses",
        json={
            "model": "Qwen/Qwen3.5-35B-A3B",
            "stream": False,
            "input": [{"role": "user", "content": "hello"}],
        },
    )
    assert create_resp.status_code == 200
    created = create_resp.json()

    retrieve_resp = await gateway_client.get(f"/v1/responses/{created['id']}")
    assert retrieve_resp.status_code == 200
    retrieved = retrieve_resp.json()
    assert retrieved["id"] == created["id"]
    assert retrieved["status"] == created["status"]


@pytest.mark.anyio
async def test_gateway_previous_response_id_missing_stays_local_with_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
):
    _use_upstream_responses_backend(gateway_app)

    resp = await gateway_client.post(
        "/v1/responses",
        json={
            "model": "Qwen/Qwen3.5-35B-A3B",
            "stream": False,
            "previous_response_id": "resp_missing",
            "input": [{"role": "user", "content": "hello"}],
        },
    )

    assert resp.status_code == 400
    assert resp.json() == {
        "error": {
            "message": "No response found with id 'resp_missing'.",
            "type": "invalid_request_error",
            "param": "previous_response_id",
            "code": "previous_response_not_found",
        }
    }


@pytest.mark.anyio
async def test_gateway_previous_response_id_continuation_stays_local_with_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "gptoss20b-responses-function-stream-auto.yaml",
        "gptoss20b-responses-followup-function-call-output-stream.yaml",
    )

    async with gateway_client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "openai/gpt-oss-20b",
            "stream": True,
            "input": [{"role": "user", "content": "What is the weather in Paris?"}],
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"location": {"type": "string"}},
                        "required": ["location"],
                        "additionalProperties": False,
                    },
                }
            ],
            "tool_choice": "auto",
        },
    ) as resp:
        assert resp.status_code == 200
        step1_body = await resp.aread()

    step1_events = parse_sse_json_events(
        parse_sse_frames(step1_body.decode("utf-8", errors="replace"))
    )
    completed = extract_completed_response(step1_events)
    response_id = completed["id"]
    function_call = next(
        item
        for item in completed["output"]
        if isinstance(item, dict) and item.get("type") == "function_call"
    )

    step2 = await gateway_client.post(
        "/v1/responses",
        json={
            "model": "openai/gpt-oss-20b",
            "previous_response_id": response_id,
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": function_call["call_id"],
                    "output": '{"temperature_c":21,"conditions":"sunny"}',
                }
            ],
        },
    )

    assert step2.status_code == 200
    data = step2.json()
    assert any(
        isinstance(item, dict)
        and item.get("type") == "message"
        and any(
            isinstance(part, dict) and part.get("type") == "output_text" and part.get("text")
            for part in item.get("content", [])
        )
        for item in data.get("output", [])
    )


@pytest.mark.anyio
async def test_gateway_non_harmony_previous_response_id_continuation_stays_local_with_upstream_responses_backend(
    patched_gateway_clients,
    gateway_app: FastAPI,
    gateway_client: httpx.AsyncClient,
    upstream_responses_replayer_factory,
):
    _use_upstream_responses_backend(gateway_app)
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "qwen35a3b-responses-function-stream-required.yaml",
        "qwen35a3b-responses-followup-with-user-message-stream.yaml",
    )

    async with gateway_client.stream(
        "POST",
        "/v1/responses",
        json={
            "model": "Qwen/Qwen3.5-35B-A3B",
            "stream": True,
            "input": [{"role": "user", "content": "What is the weather in Boston?"}],
            "tools": [_weather_tool(parameter_name="city")],
            "tool_choice": "required",
        },
    ) as resp:
        assert resp.status_code == 200
        step1_body = await resp.aread()

    step1_events = parse_sse_json_events(
        parse_sse_frames(step1_body.decode("utf-8", errors="replace"))
    )
    completed = extract_completed_response(step1_events)
    response_id = completed["id"]
    function_call = next(
        item
        for item in completed["output"]
        if isinstance(item, dict) and item.get("type") == "function_call"
    )

    step2 = await gateway_client.post(
        "/v1/responses",
        json={
            "model": "Qwen/Qwen3.5-35B-A3B",
            "previous_response_id": response_id,
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": function_call["call_id"],
                    "output": "The weather in Boston is sunny, 72F.",
                }
            ],
        },
    )

    assert step2.status_code == 200
    data = step2.json()
    message_texts = [
        part.get("text", "")
        for item in data.get("output", [])
        if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "output_text"
    ]
    assert any("Sunny, 72F" in text for text in message_texts)


@pytest.mark.anyio
async def test_mock_upstream_non_harmony_replays_tool_result_only_followup_stream_rejection(
    upstream_responses_replayer_factory,
):
    previous_replayer = mock_llm.app.state.vllm_responses.cassette_replayer
    mock_llm.app.state.vllm_responses.cassette_replayer = upstream_responses_replayer_factory(
        "qwen35a3b-responses-followup-tool-result-only-stream-400.yaml"
    )

    transport = httpx.ASGITransport(app=mock_llm.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as client:
        resp = await client.post(
            "/v1/responses",
            json={
                "model": "Qwen/Qwen3.5-35B-A3B",
                "stream": True,
                "input": [
                    {
                        "type": "function_call",
                        "id": "fc_a4ec8e35f0416e8a",
                        "call_id": "chatcmpl-tool-880f41c6876eb36f",
                        "name": "get_weather",
                        "arguments": '{"city": "Boston"}',
                        "status": "completed",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "chatcmpl-tool-880f41c6876eb36f",
                        "output": "The weather in Boston is sunny, 72F.",
                    },
                ],
            },
        )

    mock_llm.app.state.vllm_responses.cassette_replayer = previous_replayer

    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "No user query found in messages."
