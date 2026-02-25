from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Response

from vtol.entrypoints import llm as mock_llm
from vtol.responses_core.store import DBResponseStore
from vtol.routers import serving
from vtol.utils.cassette_replay import CassetteQueue, CassetteReplayer, load_cassette_yaml


@pytest.fixture
def chat_completion_cassettes_dir() -> Path:
    # `responses/tests/...` → `responses/` → `responses/python/tests/cassettes/chat_completion`
    return (
        Path(__file__).resolve().parents[1] / "python" / "tests" / "cassettes" / "chat_completion"
    )


@pytest.fixture
def cassette_replayer_factory(
    chat_completion_cassettes_dir: Path,
) -> Callable[[str], CassetteReplayer]:
    def _make(*filenames: str) -> CassetteReplayer:
        cassettes = [
            load_cassette_yaml(chat_completion_cassettes_dir / name) for name in filenames
        ]
        return CassetteReplayer(
            scenarios={"default": CassetteQueue(name="default", cassettes=cassettes)},
            default_scenario="default",
            strict=False,
        )

    return _make


@pytest.fixture
def stub_code_interpreter_app() -> FastAPI:
    app = FastAPI(title="Stub Code Interpreter")

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"pyodide_loaded": True}

    @app.post("/python")
    async def python(body: dict) -> Response:
        # Return the exact JSON string our recorded vLLM tool-loop cassettes use.
        code = str(body.get("code", "")).strip()
        if code == "2+2":
            payload = '{"status":"success","result":"4","execution_time_ms":8}'
        else:
            payload = '{"status":"success","result":null,"execution_time_ms":1}'
        return Response(content=payload, media_type="application/json")

    return app


@pytest.fixture
def gateway_app() -> FastAPI:
    app = FastAPI(title="VTOL Gateway (test)")
    app.include_router(serving.router)
    return app


@pytest.fixture
async def patched_gateway_clients(
    monkeypatch: pytest.MonkeyPatch,
    stub_code_interpreter_app: FastAPI,
    tmp_path: Path,
) -> AsyncIterator[None]:
    """
    Patch:
    - upstream LLM calls → in-process ASGI mock (`vtol.entrypoints.llm.app`)
    - code interpreter HTTP calls → in-process ASGI stub
    """
    llm_transport = httpx.ASGITransport(app=mock_llm.app)
    llm_client = httpx.AsyncClient(transport=llm_transport, base_url="http://mock/v1")

    tool_transport = httpx.ASGITransport(app=stub_code_interpreter_app)
    tool_client = httpx.AsyncClient(transport=tool_transport, base_url="http://localhost:5970")

    from pydantic_ai.providers.openai import OpenAIProvider

    def _provider_override():
        return OpenAIProvider(api_key="test", base_url="http://mock/v1", http_client=llm_client)

    import vtol.lm as lm
    import vtol.tools.code_interpreter as code_interpreter

    store = DBResponseStore.from_db_url(
        db_url=f"sqlite+aiosqlite:///{tmp_path / 'responses_state.db'}"
    )

    monkeypatch.setattr(lm, "get_openai_provider", _provider_override)
    monkeypatch.setattr(code_interpreter, "HTTP_ACLIENT", tool_client)
    monkeypatch.setattr(lm, "get_default_response_store", lambda: store)

    try:
        yield
    finally:
        await store.aclose()
        await llm_client.aclose()
        await tool_client.aclose()


@pytest.fixture
async def gateway_client(
    gateway_app: FastAPI,
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=gateway_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        yield client
