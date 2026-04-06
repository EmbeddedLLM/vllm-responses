from __future__ import annotations

from vllm_responses.types.openai import (
    OpenAIFunctionToolChoice,
    OpenAIJsonObjectFormat,
    OpenAIJsonSchemaFormat,
    OpenAIReasoningContent,
    OpenAIReasoningItem,
    OpenAIReasoningSummary,
    OpenAIResponsesFunctionTool,
    OpenAIResponsesResponse,
    OpenAIResponsesStream,
    OpenAITextConfig,
    vLLMResponsesRequest,
)


def _json_schema_format_payload() -> dict:
    return {
        "type": "json_schema",
        "name": "simple_obj",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "string"},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
    }


def test_request_model_accepts_json_schema_text_format() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "some-model",
            "input": "Return an object with x=1 and y='ok'.",
            "text": {"format": _json_schema_format_payload()},
        }
    )

    assert isinstance(req.text, OpenAITextConfig)
    assert isinstance(req.text.format, OpenAIJsonSchemaFormat)
    assert req.text.format.schema_["type"] == "object"


def test_as_openai_chat_settings_omits_response_format_for_plain_text() -> None:
    req = vLLMResponsesRequest(
        model="some-model",
        input="Say hello.",
    )

    settings = req.as_openai_chat_settings()

    assert "extra_body" not in settings


def test_as_openai_chat_settings_maps_json_object_to_extra_body_response_format() -> None:
    req = vLLMResponsesRequest(
        model="some-model",
        input="Return JSON.",
        text=OpenAITextConfig(format=OpenAIJsonObjectFormat()),
    )

    settings = req.as_openai_chat_settings()

    assert settings["extra_body"] == {
        "response_format": {
            "type": "json_object",
        }
    }


def test_as_openai_chat_settings_maps_json_schema_to_extra_body_response_format() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "some-model",
            "input": "Return an object with x=1 and y='ok'.",
            "text": {"format": _json_schema_format_payload()},
        }
    )

    settings = req.as_openai_chat_settings()

    assert settings["extra_body"] == {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "simple_obj",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer"},
                        "y": {"type": "string"},
                    },
                    "required": ["x", "y"],
                    "additionalProperties": False,
                },
            },
        }
    }


def test_as_openai_responses_settings_keeps_text_config_local_and_forces_store_false() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "Qwen/Qwen3.5-35B-A3B",
            "input": "Return an object with x=1 and y='ok'.",
            "store": True,
            "text": {
                "format": _json_schema_format_payload(),
                "verbosity": "high",
            },
            "previous_response_id": "resp_should_not_be_forwarded",
        }
    )

    settings = req.as_openai_responses_settings()

    assert settings["openai_store"] is False
    assert settings["openai_send_reasoning_ids"] is False
    assert "openai_previous_response_id" not in settings
    assert settings["extra_body"]["text"] == {
        "format": _json_schema_format_payload(),
        "verbosity": "high",
    }
    assert "include" not in settings["extra_body"]


def test_as_openai_responses_settings_passes_named_function_tool_choice_via_extra_body() -> None:
    req = vLLMResponsesRequest(
        model="Qwen/Qwen3.5-35B-A3B",
        input="Call get_weather.",
        tools=[
            OpenAIResponsesFunctionTool(
                name="get_weather",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
            )
        ],
        tool_choice=OpenAIFunctionToolChoice(name="get_weather"),
    )

    settings = req.as_openai_responses_settings()

    assert settings["extra_body"]["tool_choice"] == {
        "type": "function",
        "name": "get_weather",
    }


def test_as_openai_responses_settings_passes_required_function_tool_choice_via_extra_body() -> (
    None
):
    req = vLLMResponsesRequest(
        model="Qwen/Qwen3.5-35B-A3B",
        input="Call get_weather.",
        tools=[
            OpenAIResponsesFunctionTool(
                name="get_weather",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
            )
        ],
        tool_choice="required",
    )

    settings = req.as_openai_responses_settings()

    assert settings["extra_body"]["tool_choice"] == "required"


def test_as_openai_responses_settings_merges_verbosity_without_overwriting_required_function_tool_text() -> (
    None
):
    req = vLLMResponsesRequest(
        model="Qwen/Qwen3.5-35B-A3B",
        input="Call get_weather.",
        tools=[
            OpenAIResponsesFunctionTool(
                name="get_weather",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
            )
        ],
        tool_choice="required",
        text=OpenAITextConfig(verbosity="high"),
    )

    settings = req.as_openai_responses_settings()

    assert settings["openai_text_verbosity"] == "high"
    assert settings["extra_body"]["tool_choice"] == "required"
    assert "text" not in settings["extra_body"]


def test_as_openai_responses_settings_rejects_text_format_for_required_function_tool_choice() -> (
    None
):
    req = vLLMResponsesRequest(
        model="Qwen/Qwen3.5-35B-A3B",
        input="Call get_weather.",
        tools=[
            OpenAIResponsesFunctionTool(
                name="get_weather",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
            )
        ],
        tool_choice="required",
        text=OpenAITextConfig(format=OpenAIJsonObjectFormat()),
    )

    import pytest

    with pytest.raises(Exception, match="text.format"):
        req.as_openai_responses_settings()


def test_as_openai_responses_settings_treats_zero_top_logprobs_as_enabled() -> None:
    req = vLLMResponsesRequest(
        model="Qwen/Qwen3.5-35B-A3B",
        input="Say hello.",
        top_logprobs=0,
    )

    settings = req.as_openai_responses_settings()

    assert settings["openai_logprobs"] is True
    assert settings["openai_top_logprobs"] == 0


def test_as_openai_responses_settings_rejects_non_auto_custom_function_tool_choice_for_harmony() -> (
    None
):
    req = vLLMResponsesRequest(
        model="openai/gpt-oss-20b",
        input="Call get_weather.",
        tools=[
            OpenAIResponsesFunctionTool(
                name="get_weather",
                parameters={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
            )
        ],
        tool_choice="required",
    )

    import pytest

    with pytest.raises(Exception, match='tool_choice="auto"'):
        req.as_openai_responses_settings()


async def test_as_run_settings_prefers_reasoning_content_over_summary_for_rehydration() -> None:
    req = vLLMResponsesRequest(
        model="Qwen/Qwen3.5-35B-A3B",
        input=[
            OpenAIReasoningItem(
                id="rs_1",
                content=[OpenAIReasoningContent(text="full reasoning")],
                summary=[OpenAIReasoningSummary(text="summary only")],
            )
        ],
    )

    run_settings, _, _ = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    history = run_settings["message_history"]
    assert history is not None
    thinking_part = history[0].parts[0]
    assert thinking_part.content == "full reasoning"
    assert thinking_part.provider_details == {"raw_content": ["full reasoning"]}


def test_responses_stream_serialization_uses_schema_alias_not_schema_field_name() -> None:
    response = OpenAIResponsesResponse(
        model="some-model",
        text=OpenAITextConfig(
            format=OpenAIJsonSchemaFormat.model_validate(_json_schema_format_payload())
        ),
    )
    event = OpenAIResponsesStream(
        type="response.created",
        sequence_number=1,
        response=response,
    )

    chunk = event.as_responses_chunk()

    assert '"schema":{' in chunk
    assert '"schema_":' not in chunk


def test_response_seed_round_trip_uses_schema_alias_not_internal_field_name() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "some-model",
            "input": "Return an object with x=1 and y='ok'.",
            "text": {"format": _json_schema_format_payload()},
        }
    )

    response = OpenAIResponsesResponse.model_validate(
        req.model_dump(mode="python", exclude_none=True, by_alias=True)
    )

    assert isinstance(response.text.format, OpenAIJsonSchemaFormat)
    assert response.text.format.schema_["type"] == "object"
