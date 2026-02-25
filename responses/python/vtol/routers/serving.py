from typing import AsyncGenerator

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from vtol.lm import LMEngine
from vtol.observability.metrics import get_route_label, instrument_sse_stream
from vtol.types.openai import vLLMResponsesRequest
from vtol.utils.exceptions import (
    handle_exception,
)

router = APIRouter()


async def _empty_async_generator():
    """Returns an empty asynchronous generator."""
    return
    # This line is never reached, but makes it an async generator
    yield


@router.post(
    "/v1/responses",
    summary="Create a model response.",
    description=(
        "Creates a model response. "
        "Provide text or image inputs to generate text or JSON outputs. "
        "Have the model call your own custom code or use built-in tools like code interpreter."
    ),
)
@handle_exception
async def create_model_response(
    request: Request,
    # session: Annotated[AsyncSession, Depends(yield_async_session)],
    body: vLLMResponsesRequest,
) -> Response:
    # as_responses_chunk()
    engine = LMEngine(body=body)
    if body.stream:
        agen: AsyncGenerator[str, None] = await engine.run()
        agen = instrument_sse_stream(route=get_route_label(request), agen=agen)
        try:
            # Get the first chunk outside of the loop so that errors can be raised immediately
            # Otherwise, streaming requests will always return 200
            chunk = await anext(agen)
        except StopAsyncIteration:
            return StreamingResponse(
                content=_empty_async_generator(),
                status_code=200,
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no"},
            )

        async def _generate():
            nonlocal chunk
            yield chunk
            async for chunk in agen:
                yield chunk

        response = StreamingResponse(
            content=_generate(),
            status_code=200,
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )
    else:
        response = await engine.run()
    return response
