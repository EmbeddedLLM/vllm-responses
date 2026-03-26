from __future__ import annotations

from vllm_responses.types.openai import (
    OpenAIJsonObjectFormat,
    OpenAIJsonSchemaFormat,
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
