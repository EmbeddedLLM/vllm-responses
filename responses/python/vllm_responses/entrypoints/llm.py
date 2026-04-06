from __future__ import annotations

import json
from contextlib import asynccontextmanager
from time import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse, StreamingResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from vllm_responses.configs.builders import build_runtime_config_for_mock_llm
from vllm_responses.configs.mock_llm import MockLLMConfig
from vllm_responses.configs.sources import EnvSource
from vllm_responses.entrypoints._state import VRAppState
from vllm_responses.utils import uuid7_str
from vllm_responses.utils.cassette_replay import (
    CassetteReplayer,
    CassetteReplayError,
    stream_sse_chunks,
)

RUNTIME_CONFIG = build_runtime_config_for_mock_llm(env=EnvSource.from_env())


def _get_cassette_replayer(app: FastAPI) -> CassetteReplayer | None:
    return app.state.vllm_responses.cassette_replayer


class ChatCompletionRequest(BaseModel):
    """Subset of OpenAI-compatible Chat Completions request.

    The mock server is primarily used for deterministic cassette replay. For replay we only
    need `stream` and the path/method; other fields are accepted and ignored.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False


class ResponsesRequest(BaseModel):
    """Subset of OpenAI-compatible Responses request used for cassette replay."""

    model_config = ConfigDict(extra="allow")

    model: str
    input: Any = None
    stream: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Using runtime config: {RUNTIME_CONFIG}")
    mock_cfg = MockLLMConfig()
    if mock_cfg.mode == "replay":
        scenarios_json = mock_cfg.scenarios_json
        if not scenarios_json:
            raise RuntimeError("VR_MOCK_LLM_MODE=replay requires VR_MOCK_LLM_SCENARIOS to be set.")
        app.state.vllm_responses.cassette_replayer = CassetteReplayer.from_env(
            cassette_dir=mock_cfg.cassette_dir_path,
            scenarios_json=scenarios_json,
            default_scenario=mock_cfg.default_scenario,
            strict=mock_cfg.strict,
        )
        logger.info(
            (
                "Mock LLM cassette replay enabled.\n"
                f"- cassette_dir={mock_cfg.cassette_dir_path}\n"
                f"- default_scenario={mock_cfg.default_scenario}\n"
                f"- strict={mock_cfg.strict}"
            )
        )
    yield
    logger.info("Shutting down...")


app = FastAPI(title="Mock LLM (cassette replay)", lifespan=lifespan)
app.state.vllm_responses = VRAppState()
app.state.vllm_responses.runtime_config = RUNTIME_CONFIG


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _replay_or_error(
    *,
    request: Request,
    method: str,
    path: str,
    stream: bool,
    request_body: dict[str, Any],
) -> CassetteReplayError | tuple[dict[str, str], Any, bool] | None:
    replayer = _get_cassette_replayer(app)
    if replayer is None:
        return None

    try:
        cassette_resp = replayer.next_response(
            scenario=request.headers.get("X-VR-Scenario"),
            method=method,
            path=path,
            stream=stream,
            request_body=request_body,
        )
    except CassetteReplayError as exc:
        return exc

    return dict(cassette_resp.headers), cassette_resp, cassette_resp.is_stream


@app.post("/v1/chat/completions")
async def chat_completion(request: Request, body: ChatCompletionRequest):
    replay = _replay_or_error(
        request=request,
        method="POST",
        path="/v1/chat/completions",
        stream=bool(body.stream),
        request_body=body.model_dump(),
    )
    if isinstance(replay, CassetteReplayError):
        return ORJSONResponse(status_code=500, content={"error": {"message": str(replay)}})
    if replay is not None:
        headers, cassette_resp, is_stream = replay
        if is_stream:
            return StreamingResponse(
                stream_sse_chunks(cassette_resp.sse or []),
                status_code=cassette_resp.status_code,
                media_type=headers.get("content-type", "text/event-stream; charset=utf-8"),
                headers=headers,
            )
        return ORJSONResponse(
            status_code=cassette_resp.status_code,
            content=cassette_resp.body or {},
            headers=headers,
        )

    # Fallback behavior (useful for manual smoke tests when replay isn't configured).
    completion_id = f"chatcmpl-{uuid7_str()}"
    created = int(time())
    content = "OK"

    if body.stream:

        async def _stream():
            yield (
                "data: "
                + _json_dumps(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": body.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                + "\n\n"
            )
            yield (
                "data: "
                + _json_dumps(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": body.model,
                        "choices": [
                            {"index": 0, "delta": {"content": content}, "finish_reason": None}
                        ],
                    }
                )
                + "\n\n"
            )
            yield (
                "data: "
                + _json_dumps(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": body.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream; charset=utf-8")

    return ORJSONResponse(
        status_code=200,
        content={
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": body.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
    )


@app.post("/v1/responses")
async def responses_create(request: Request, body: ResponsesRequest):
    replay = _replay_or_error(
        request=request,
        method="POST",
        path="/v1/responses",
        stream=bool(body.stream),
        request_body=body.model_dump(),
    )
    if isinstance(replay, CassetteReplayError):
        return ORJSONResponse(status_code=500, content={"error": {"message": str(replay)}})
    if replay is not None:
        headers, cassette_resp, is_stream = replay
        if is_stream:
            return StreamingResponse(
                stream_sse_chunks(cassette_resp.sse or []),
                status_code=cassette_resp.status_code,
                media_type=headers.get("content-type", "text/event-stream; charset=utf-8"),
                headers=headers,
            )
        return ORJSONResponse(
            status_code=cassette_resp.status_code,
            content=cassette_resp.body or {},
            headers=headers,
        )

    response_id = uuid7_str("resp_")
    created = int(time())
    content = "OK"

    if body.stream:

        async def _stream():
            yield (
                "data: "
                + _json_dumps(
                    {
                        "type": "response.created",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created,
                            "status": "in_progress",
                            "model": body.model,
                            "output": [],
                        },
                        "sequence_number": 0,
                    }
                )
                + "\n\n"
            )
            yield (
                "data: "
                + _json_dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": response_id,
                            "object": "response",
                            "created_at": created,
                            "status": "completed",
                            "model": body.model,
                            "output": [
                                {
                                    "id": uuid7_str("msg_"),
                                    "type": "message",
                                    "role": "assistant",
                                    "status": "completed",
                                    "content": [{"type": "output_text", "text": content}],
                                }
                            ],
                        },
                        "sequence_number": 1,
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream; charset=utf-8")

    return ORJSONResponse(
        status_code=200,
        content={
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "model": body.model,
            "output": [
                {
                    "id": uuid7_str("msg_"),
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": content}],
                }
            ],
        },
    )
