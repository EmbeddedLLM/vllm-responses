from __future__ import annotations

from typing import Any, AsyncGenerator

from loguru import logger
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    ModelHTTPError,
    UnexpectedModelBehavior,
    capture_run_messages,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.retries import RetryConfig

from vtol.configs import ENV_CONFIG
from vtol.responses_core.composer import ResponseComposer
from vtol.responses_core.normalizer import PydanticAINormalizer
from vtol.responses_core.sse import stream_responses_sse
from vtol.responses_core.store import ResponseStore, get_default_response_store
from vtol.tools import CODE_INTERPRETER_TOOL
from vtol.types.openai import (
    OpenAIResponsesError,
    OpenAIResponsesResponse,
    OpenAIResponsesResponseError,
    OpenAIResponsesStream,
    OpenAIResponsesStreamOutput,
    OpenAIResponsesStreamPart,
    OpenAIResponsesStreamText,
    vLLMResponsesRequest,
)
from vtol.utils.exceptions import BadInputError
from vtol.utils.io import get_async_client, json_loads

LM_CLIENT = get_async_client()


def get_openai_provider(
    base_url: str = ENV_CONFIG.llm_api_base,
    *,
    api_key: str = ENV_CONFIG.openai_api_key_plain,
) -> OpenAIProvider:
    return OpenAIProvider(
        api_key=api_key,
        base_url=base_url,
        http_client=LM_CLIENT,
    )


class LMEngine:
    """Orchestrate one Responses request using vLLM Chat Completions via Pydantic AI.

    MVP staging notes:
    - keep the alpha tool execution model (Option A: code interpreter executed via `pydantic_ai` tool registration)
    - `previous_response_id` is supported via a shared ResponseStore (Stage 2)
    - move Responses contract correctness into `vtol.responses_core` (Normalizer → Composer)
    """

    def __init__(
        self,
        body: vLLMResponsesRequest,
        *,
        retry_config: RetryConfig | None = None,
        store: ResponseStore | None = None,
    ) -> None:
        self._body = body
        self._store = store or get_default_response_store()
        self._hydrated_body: vLLMResponsesRequest | None = None
        # NOTE: the installed `pydantic_ai` version in this repo does not accept `retry_config`
        # on `OpenAIProvider.__init__`. Keep the parameter for future use, but do not pass it.
        self._agent = Agent(
            OpenAIChatModel(
                model_name=body.model,
                provider=get_openai_provider(),
            ),
            model_settings=body.as_openai_chat_settings(),
        )
        self._response: OpenAIResponsesResponse | None = None

    async def run(
        self,
    ) -> (
        AsyncGenerator[
            str,
            None,
        ]
        | OpenAIResponsesResponse
    ):
        if self._body.stream:
            return self._run_stream()
        return await self._run()

    async def _run_stream(
        self,
    ) -> AsyncGenerator[str, None]:
        async for frame in stream_responses_sse(
            self._tap_events(self._iter_responses_events_stream())
        ):
            yield frame

    async def _run(self) -> OpenAIResponsesResponse:
        async for chunk in self._iter_responses_events_non_stream():
            if isinstance(chunk, OpenAIResponsesStream) and chunk.type == "response.completed":
                self._response = chunk.response
                if self._hydrated_body is not None:
                    await self._store.put_completed(
                        request=self._body,
                        hydrated_request=self._hydrated_body,
                        response=chunk.response,
                    )
        if self._response is None:
            raise BadInputError("No response generated from LMEngine.")
        return self._response

    async def _tap_events(
        self,
        events: AsyncGenerator[
            OpenAIResponsesStream
            | OpenAIResponsesStreamOutput
            | OpenAIResponsesStreamPart
            | OpenAIResponsesStreamText,
            None,
        ],
    ) -> AsyncGenerator[
        OpenAIResponsesStream
        | OpenAIResponsesStreamOutput
        | OpenAIResponsesStreamPart
        | OpenAIResponsesStreamText,
        None,
    ]:
        async for event in events:
            if isinstance(event, OpenAIResponsesStream) and event.type == "response.completed":
                self._response = event.response
                if self._hydrated_body is not None:
                    await self._store.put_completed(
                        request=self._body,
                        hydrated_request=self._hydrated_body,
                        response=event.response,
                    )
            yield event

    async def _build_response_pipeline(
        self,
    ) -> tuple[
        OpenAIResponsesResponse,
        vLLMResponsesRequest,
        dict[str, Any],
        PydanticAINormalizer,
        ResponseComposer,
    ]:
        # Seed the response from request fields, but do not allow `None` request values
        # to clobber schema-required response defaults (e.g. `tools: []`, `truncation: "disabled"`).
        response = OpenAIResponsesResponse.model_validate(self._body.model_dump(exclude_none=True))

        hydrated_body = await self._store.rehydrate_request(request=self._body)
        self._hydrated_body = hydrated_body
        run_settings, builtin_tools = hydrated_body.as_run_settings()
        builtin_tool_names = {t.name for t in builtin_tools}

        normalizer = PydanticAINormalizer(
            builtin_tool_names=builtin_tool_names,
            code_interpreter_tool_name=CODE_INTERPRETER_TOOL,
        )
        include_set = set(hydrated_body.include or [])
        composer = ResponseComposer(response=response, include=include_set)
        return response, hydrated_body, run_settings, normalizer, composer

    async def _iter_responses_events_non_stream(
        self,
    ) -> AsyncGenerator[
        OpenAIResponsesStream
        | OpenAIResponsesStreamOutput
        | OpenAIResponsesStreamPart
        | OpenAIResponsesStreamText,
        None,
    ]:
        # Non-stream mode: exceptions should propagate so the HTTP layer can return a non-200 response.
        # (There is no SSE stream to carry error events.)
        _, _, run_settings, normalizer, composer = await self._build_response_pipeline()

        # Emit created/in_progress even if the upstream call fails immediately, to match the streaming contract.
        for chunk in composer.start():
            yield chunk

        with capture_run_messages() as messages:
            async for event in self._agent.run_stream_events(
                output_type=[self._agent.output_type, DeferredToolRequests],
                **run_settings,
            ):
                for normalized in normalizer.on_event(event):
                    for out in composer.feed(normalized):
                        yield out

            # Helpful context for upstream errors.
            _ = messages

    async def _iter_responses_events_stream(
        self,
    ) -> AsyncGenerator[
        OpenAIResponsesStream
        | OpenAIResponsesStreamOutput
        | OpenAIResponsesStreamPart
        | OpenAIResponsesStreamText,
        None,
    ]:
        # Stream mode: convert upstream failures into Responses stream error ordering.
        response, _, run_settings, normalizer, composer = await self._build_response_pipeline()

        # Emit created/in_progress even if the upstream call fails immediately, to match the streaming contract.
        for chunk in composer.start():
            yield chunk

        with capture_run_messages() as messages:
            try:
                async for event in self._agent.run_stream_events(
                    output_type=[self._agent.output_type, DeferredToolRequests],
                    **run_settings,
                ):
                    for normalized in normalizer.on_event(event):
                        for out in composer.feed(normalized):
                            yield out
            except ModelHTTPError as e:
                logger.warning(
                    (
                        "Upstream model HTTP error during LMEngine stream.\n"
                        f"Error: {repr(e)}\n"
                        f"Cause: {e.__cause__}\n"
                        f"Messages: {messages}"
                    )
                )

                err_body: dict[str, Any] = {}
                raw_body = e.body
                if raw_body:
                    try:
                        err_body = json_loads(raw_body)
                    except Exception:
                        pass

                err_code, err_message, err_param = _extract_openai_error_fields(
                    err_body,
                    fallback_message=str(e),
                )

                yield OpenAIResponsesError(
                    code=err_code,
                    message=err_message,
                    param=err_param,
                    sequence_number=composer.alloc_sequence_number(),
                )
                response.error = OpenAIResponsesResponseError(code=err_code, message=err_message)
                response.status = "failed"
                yield OpenAIResponsesStream(
                    type="response.failed",
                    response=response,
                    sequence_number=composer.alloc_sequence_number(),
                )
                return
            except UnexpectedModelBehavior as e:
                logger.warning(
                    (
                        "An error occurred during LMEngine run stream.\n"
                        f"Error: {repr(e)}\n"
                        f"Cause: {e.__cause__}\n"
                        f"Messages: {messages}"
                    )
                )
                err_body: dict[str, Any] = {}
                if e.body:
                    try:
                        err_body = json_loads(e.body)
                    except Exception:
                        pass

                err_code, err_message, err_param = _extract_openai_error_fields(
                    err_body,
                    fallback_message=e.message,
                )
                yield OpenAIResponsesError(
                    code=err_code,
                    message=err_message,
                    param=err_param,
                    sequence_number=composer.alloc_sequence_number(),
                )
                response.error = OpenAIResponsesResponseError(code=err_code, message=err_message)
                response.status = "failed"
                yield OpenAIResponsesStream(
                    type="response.failed",
                    response=response,
                    sequence_number=composer.alloc_sequence_number(),
                )
                return


def _extract_openai_error_fields(
    err_body: dict[str, Any] | None,
    *,
    fallback_message: str,
) -> tuple[str, str, str]:
    """Best-effort parse an OpenAI-style error object.

    Expected shape: {"error": {"message": ..., "type": ..., "param": ..., "code": ...}}
    """

    body = err_body or {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    if not isinstance(err, dict):
        return "", fallback_message, ""

    code_raw = err.get("code")
    # Upstreams vary: OpenAI uses string-or-null, but other providers sometimes return ints (e.g. 404).
    code = "" if code_raw is None else str(code_raw)
    message_raw = err.get("message")
    message = (
        str(message_raw) if isinstance(message_raw, str) and message_raw else fallback_message
    )
    param_raw = err.get("param")
    param = "" if param_raw is None else str(param_raw)
    return code, message, param
