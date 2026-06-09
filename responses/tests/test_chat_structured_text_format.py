from __future__ import annotations

import json

from vllm_responses.types.openai import (
    OpenAIFunctionToolChoice,
    OpenAIImageContent,
    OpenAIInputItem,
    OpenAIJsonObjectFormat,
    OpenAIJsonSchemaFormat,
    OpenAIReasoningContent,
    OpenAIReasoningItem,
    OpenAIReasoningSummary,
    OpenAIResponsesFunctionTool,
    OpenAIResponsesResponse,
    OpenAIResponsesStream,
    OpenAITextConfig,
    OpenAITextContent,
    vLLMResponsesRequest,
)
from vllm_responses.utils.exceptions import BadInputError


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
            "model": "vllm/test-model",
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
        model="vllm/test-model",
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
        model="vllm/test-model",
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
        model="vllm/test-model",
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
        model="vllm/test-model",
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
        model="vllm/test-model",
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


async def test_as_run_settings_folds_system_developer_messages_into_instructions() -> None:
    req = vLLMResponsesRequest(
        model="vllm/test-model",
        instructions="Top-level instructions.",
        input=[
            OpenAIInputItem(
                role="developer",
                content=[
                    OpenAITextContent(text="Developer part one."),
                    OpenAITextContent(text="Developer part two."),
                ],
            ),
            OpenAIInputItem(
                role="system",
                content=[OpenAITextContent(text="System item.")],
            ),
            OpenAIInputItem(
                role="user",
                content=[OpenAITextContent(text="Hello.")],
            ),
        ],
    )

    run_settings, _, _, _namespace_map = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    assert (
        run_settings["instructions"]
        == "Top-level instructions.\n\nDeveloper part one.\nDeveloper part two.\n\nSystem item."
    )
    assert len(run_settings["message_history"]) == 1
    assert run_settings["message_history"][0].parts[0].content == ["Hello."]


async def test_as_run_settings_rejects_non_text_system_developer_instruction_content() -> None:
    req = vLLMResponsesRequest(
        model="vllm/test-model",
        input=[
            OpenAIInputItem(
                role="developer",
                content=[
                    OpenAIImageContent(
                        image_url="data:image/png;base64,AAAA",
                    )
                ],
            ),
            OpenAIInputItem(
                role="user",
                content=[OpenAITextContent(text="Hello.")],
            ),
        ],
    )

    import pytest

    with pytest.raises(BadInputError, match="Developer message content must be text-only"):
        await req.as_run_settings(
            builtin_mcp_runtime_client=None,
            request_remote_enabled=False,
            request_remote_url_checks_enabled=False,
        )


async def test_as_run_settings_accepts_codex_tool_followup_replay_shape() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Run the tool."}],
                },
                {"type": "reasoning", "summary": [], "encrypted_content": None},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "\n\n",
                            "annotations": [],
                            "logprobs": None,
                        }
                    ],
                },
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"cmd":"printf CODEX_TOOL_OK"}',
                    "call_id": "call_123",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "CODEX_TOOL_OK",
                },
            ],
        }
    )

    run_settings, _, _, _namespace_map = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    history = run_settings["message_history"]
    assert len(history) == 4
    assert history[0].parts[0].content == ["Run the tool."]
    assert history[1].parts[0].content == "\n\n"
    assert history[2].parts[0].tool_name == "exec_command"
    assert history[3].parts[0].tool_name == "exec_command"
    assert history[3].parts[0].content == "CODEX_TOOL_OK"


async def test_as_run_settings_flattens_codex_namespaced_tool_replay_shape() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": [
                {
                    "type": "function_call",
                    "namespace": "mcp__demo__",
                    "name": "lookup_order",
                    "arguments": '{"order_id":"ord_123"}',
                    "call_id": "call_namespaced",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_namespaced",
                    "output": "shipped",
                },
            ],
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__demo__",
                    "description": "Demo tools",
                    "tools": [
                        {
                            "type": "function",
                            "name": "lookup_order",
                            "description": "Look up an order",
                            "strict": False,
                            "parameters": {
                                "type": "object",
                                "properties": {"order_id": {"type": "string"}},
                            },
                        }
                    ],
                }
            ],
        }
    )

    run_settings, _, _, _namespace_map = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    history = run_settings["message_history"]
    assert len(history) == 2
    assert history[0].parts[0].tool_name == "mcp__demo__lookup_order"
    assert history[1].parts[0].tool_name == "mcp__demo__lookup_order"
    assert history[1].parts[0].content == "shipped"


async def test_as_run_settings_accepts_codex_custom_tool_replay_shape() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call_custom",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** End Patch\n",
                    "status": "completed",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_custom",
                    "output": "patch applied",
                },
            ],
        }
    )

    run_settings, _, _, _namespace_map = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    history = run_settings["message_history"]
    assert len(history) == 2
    assert json.loads(history[0].parts[0].content) == {
        "call_id": "call_custom",
        "name": "apply_patch",
        "input": "*** Begin Patch\n*** End Patch\n",
        "type": "custom_tool_call",
        "status": "completed",
    }
    assert json.loads(history[1].parts[0].content) == {
        "type": "custom_tool_call_output",
        "call_id": "call_custom",
        "name": None,
        "output": "patch applied",
    }


async def test_as_run_settings_accepts_codex_tool_search_replay_shape() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": [
                {
                    "type": "tool_search_call",
                    "call_id": "search_1",
                    "execution": "client",
                    "status": "completed",
                    "arguments": {"query": "calendar create", "limit": 1},
                },
                {
                    "type": "tool_search_output",
                    "call_id": "search_1",
                    "execution": "client",
                    "status": "completed",
                    "tools": [{"name": "calendar.create", "description": "Create event"}],
                },
            ],
        }
    )

    run_settings, _, _, _namespace_map = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    history = run_settings["message_history"]
    assert len(history) == 2
    assert history[0].parts[0].tool_name == "tool_search"
    assert history[0].parts[0].args == {"query": "calendar create", "limit": 1}
    assert history[1].parts[0].tool_name == "tool_search"
    tool_search_output = json.loads(history[1].parts[0].content)
    assert tool_search_output == {
        "status": "completed",
        "execution": "client",
        "tools": [{"name": "calendar.create", "description": "Create event"}],
        "type": "tool_search_output",
        "call_id": "search_1",
    }


async def test_as_run_settings_accepts_codex_server_tool_search_output_without_call_id() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": [
                {
                    "type": "tool_search_output",
                    "call_id": None,
                    "execution": "server",
                    "status": "completed",
                    "tools": [{"name": "remote.tool"}],
                },
            ],
        }
    )

    run_settings, _, _, _namespace_map = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    history = run_settings["message_history"]
    assert len(history) == 1
    assert json.loads(history[0].parts[0].content) == {
        "status": "completed",
        "execution": "server",
        "tools": [{"name": "remote.tool"}],
        "type": "tool_search_output",
    }


async def test_as_run_settings_accepts_codex_local_shell_call_with_function_output() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": [
                {
                    "type": "local_shell_call",
                    "call_id": "call_shell",
                    "status": "completed",
                    "action": {
                        "type": "exec",
                        "command": ["sed", "-n", "1,20p", "file with spaces.py"],
                        "working_directory": "/repo",
                    },
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_shell",
                    "output": "first twenty lines",
                },
            ],
        }
    )

    run_settings, _, _, _namespace_map = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    history = run_settings["message_history"]
    assert len(history) == 2
    assert history[0].parts[0].tool_name == "exec_command"
    assert history[0].parts[0].args == {
        "cmd": "sed -n 1,20p 'file with spaces.py'",
        "workdir": "/repo",
    }
    assert history[1].parts[0].tool_name == "exec_command"
    assert history[1].parts[0].content == "first twenty lines"


async def test_as_run_settings_prefers_reasoning_content_over_summary_for_rehydration() -> None:
    req = vLLMResponsesRequest(
        model="vllm/test-model",
        input=[
            OpenAIReasoningItem(
                id="rs_1",
                content=[OpenAIReasoningContent(text="full reasoning")],
                summary=[OpenAIReasoningSummary(text="summary only")],
            )
        ],
    )

    run_settings, _, _, _namespace_map = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    history = run_settings["message_history"]
    assert history is not None
    thinking_part = history[0].parts[0]
    assert thinking_part.content == "full reasoning"
    assert thinking_part.provider_details == {"raw_content": ["full reasoning"]}


async def test_as_run_settings_flattens_codex_namespace_tools() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": "Use the namespaced tool.",
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__demo__",
                    "description": "Demo tools",
                    "tools": [
                        {
                            "type": "function",
                            "name": "lookup_order",
                            "description": "Look up an order",
                            "strict": False,
                            "parameters": {
                                "type": "object",
                                "properties": {"order_id": {"type": "string"}},
                            },
                        }
                    ],
                }
            ],
        }
    )

    run_settings, _, _, codex_compat = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    toolsets = run_settings["toolsets"]
    assert toolsets is not None
    external_toolset = toolsets[0]
    tool_defs = external_toolset.tool_defs
    assert [tool.name for tool in tool_defs] == ["mcp__demo__lookup_order"]
    assert codex_compat.namespace_tool_map == {
        "mcp__demo__lookup_order": ("mcp__demo__", "lookup_order")
    }


async def test_as_run_settings_adapts_codex_custom_lark_tool() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": "Use apply_patch.",
            "tools": [
                {
                    "type": "custom",
                    "name": "apply_patch",
                    "description": "Use apply_patch.",
                    "format": {
                        "type": "grammar",
                        "syntax": "lark",
                        "definition": "start: /.+/",
                    },
                }
            ],
        }
    )

    run_settings, _, _, codex_compat = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    toolsets = run_settings["toolsets"]
    assert toolsets is not None
    tool_defs = toolsets[0].tool_defs
    assert [tool.name for tool in tool_defs] == ["apply_patch"]
    assert tool_defs[0].parameters_json_schema == {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Raw custom tool input. Must match the declared Lark grammar.",
            }
        },
        "required": ["input"],
        "additionalProperties": False,
    }
    assert "start: /.+/" in (tool_defs[0].description or "")
    assert req.codex_compat_context.custom_tool_names == {"apply_patch"}
    assert codex_compat.namespace_tool_map == {}


async def test_as_run_settings_ignores_unsupported_codex_custom_format_until_selected() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": "No callable tools.",
            "tools": [
                {
                    "type": "custom",
                    "name": "freeform",
                    "description": "Unsupported custom tool.",
                    "format": {
                        "type": "grammar",
                        "syntax": "not-lark",
                        "definition": "start: /.+/",
                    },
                }
            ],
        }
    )

    run_settings, _, _, codex_compat = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    assert run_settings["toolsets"] is None
    assert req.codex_compat_context.custom_tool_names == set()
    assert codex_compat.namespace_tool_map == {}


async def test_required_tool_choice_rejects_only_unsupported_codex_custom_tool() -> None:
    import pytest

    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": "This requires a tool.",
            "tool_choice": "required",
            "tools": [
                {
                    "type": "custom",
                    "name": "freeform",
                    "description": "Unsupported custom tool.",
                    "format": {
                        "type": "grammar",
                        "syntax": "not-lark",
                        "definition": "start: /.+/",
                    },
                }
            ],
        }
    )

    with pytest.raises(BadInputError, match="requires at least one effective tool"):
        await req.as_run_settings(
            builtin_mcp_runtime_client=None,
            request_remote_enabled=False,
            request_remote_url_checks_enabled=False,
        )


async def test_as_run_settings_rejects_codex_namespace_collision_with_function() -> None:
    import pytest

    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": "Use the tool.",
            "tools": [
                {
                    "type": "function",
                    "name": "demo__lookup_order",
                    "parameters": {"type": "object"},
                },
                {
                    "type": "namespace",
                    "name": "demo__",
                    "tools": [
                        {
                            "type": "function",
                            "name": "lookup_order",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
            ],
        }
    )

    with pytest.raises(BadInputError, match="Duplicate function tool name"):
        await req.as_run_settings(
            builtin_mcp_runtime_client=None,
            request_remote_enabled=False,
            request_remote_url_checks_enabled=False,
        )


async def test_as_run_settings_rejects_codex_namespace_flattened_collision() -> None:
    import pytest

    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": "Use the tool.",
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__demo__",
                    "tools": [
                        {
                            "type": "function",
                            "name": "lookup_order",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
                {
                    "type": "namespace",
                    "name": "mcp__demo__lookup_",
                    "tools": [
                        {
                            "type": "function",
                            "name": "order",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
            ],
        }
    )

    with pytest.raises(BadInputError, match="Duplicate function tool name"):
        await req.as_run_settings(
            builtin_mcp_runtime_client=None,
            request_remote_enabled=False,
            request_remote_url_checks_enabled=False,
        )


async def test_as_run_settings_accepts_explicit_empty_tools_as_no_tools() -> None:
    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": "Summarize the conversation without tools.",
            "tools": [],
            "tool_choice": "auto",
        }
    )

    run_settings, builtin_tools, mcp_map, codex_compat = await req.as_run_settings(
        builtin_mcp_runtime_client=None,
        request_remote_enabled=False,
        request_remote_url_checks_enabled=False,
    )

    assert req.tools == []
    assert run_settings["toolsets"] is None
    assert builtin_tools == []
    assert mcp_map == {}
    assert codex_compat.namespace_tool_map == {}


async def test_required_tool_choice_rejects_explicit_empty_tools() -> None:
    import pytest

    req = vLLMResponsesRequest.model_validate(
        {
            "model": "vllm/test-model",
            "input": "This request requires a tool but declares none.",
            "tools": [],
            "tool_choice": "required",
        }
    )

    with pytest.raises(BadInputError, match="requires at least one effective tool"):
        await req.as_run_settings(
            builtin_mcp_runtime_client=None,
            request_remote_enabled=False,
            request_remote_url_checks_enabled=False,
        )


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
