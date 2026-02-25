from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, Self

from pydantic import BaseModel, TypeAdapter
from sqlalchemy import Column, DateTime, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from vtol.configs import CACHE, ENV_CONFIG
from vtol.db import create_db_engine_async, postgres_advisory_lock
from vtol.types.openai import (
    OpenAIAllowedToolsChoice,
    OpenAIFunctionToolChoice,
    OpenAIHostedToolChoice,
    OpenAIResponsesResponse,
    OpenAIToolChoice,
    vLLMInput,
    vLLMResponsesRequest,
    vLLMResponsesTool,
)
from vtol.utils.exceptions import BadInputError
from vtol.utils.io import json_dumps, json_loads

"""
ResponseStore (Layer 4) implementation.

Design intent:
- Production code should use the shared DB engine plumbing from `vtol.db` (instrumentation,
  pooling, SQLite PRAGMAs) and construct `DBResponseStore` with an injected engine via
  `get_default_response_store()`.
- `DBResponseStore.from_db_url()` exists only as a convenience for tests and one-off tools
  that want an isolated DB URL; it intentionally creates a private engine and therefore
  bypasses the shared engine plumbing.
"""

SCHEMA_VERSION = 1
POSTGRES_SCHEMA_LOCK_NAME = "vllm-responses:responses_state_schema_v1"
# `schema_version` is the version of the serialized ResponseStore payload contract (the JSON blob).
# Any change that modifies `StoredResponsePayload` shape should bump this and either:
# - provide a backward-compatible upgrader for old rows, or
# - (dev-only) require clearing the store.


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_vllm_input_list_adapter = TypeAdapter(list[vLLMInput])
_tools_list_adapter = TypeAdapter(list[vLLMResponsesTool])
_tool_choice_adapter = TypeAdapter(OpenAIToolChoice)


def _normalize_input(value: str | list[vLLMInput]) -> list[vLLMInput]:
    if isinstance(value, str):
        # Spec allows `input` to be a shorthand string. Persist normalized list form so we can rehydrate consistently.
        return _vllm_input_list_adapter.validate_python([{"role": "user", "content": value}])
    return value


class StoredResponsePayload(BaseModel):
    schema_version: int = SCHEMA_VERSION

    hydrated_input: list[vLLMInput]
    response: OpenAIResponsesResponse
    # Store tools in the request-compatible shape (`vLLMResponsesTool`) so we can reuse them on the next request.
    effective_tools: list[vLLMResponsesTool] | None = None
    effective_tool_choice: OpenAIToolChoice
    effective_instructions: str | None = None


@dataclass(frozen=True, slots=True)
class StoredResponse:
    response_id: str
    previous_response_id: str | None
    model: str
    created_at: datetime
    expires_at: datetime | None
    store: bool
    schema_version: int
    state: dict[str, Any]

    def payload(self) -> StoredResponsePayload:
        return StoredResponsePayload.model_validate(self.state)


class ResponseStore(Protocol):
    async def ensure_schema(self) -> None: ...
    async def get(self, *, response_id: str) -> StoredResponse | None: ...
    async def put_completed(
        self,
        *,
        request: vLLMResponsesRequest,
        hydrated_request: vLLMResponsesRequest,
        response: OpenAIResponsesResponse,
    ) -> None: ...

    async def rehydrate_request(
        self, *, request: vLLMResponsesRequest
    ) -> vLLMResponsesRequest: ...


class ResponsesState(SQLModel, table=True):
    __tablename__ = "responses_state"

    response_id: str = Field(primary_key=True)
    previous_response_id: str | None = Field(default=None, index=True)
    model: str
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True)
    )
    expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True)
    )
    store: bool
    schema_version: int
    state_json: Any = Field(
        sa_column=Column(
            JSONB if ENV_CONFIG.db_dialect == "postgresql" else Text,
            nullable=False,
        )
    )


class DBResponseStore:
    """DB-backed ResponseStore.

    MVP Stage 2 scope:
    - SQLite (dev/embedded) + Postgres (prod) only (via ENV_CONFIG.db_path)
    - schema is intentionally minimal and stable; state lives primarily in `state_json`
    """

    def __init__(self, *, engine: AsyncEngine, owns_engine: bool = False) -> None:
        self._engine = engine
        self._owns_engine = owns_engine
        self._sessionmaker = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False
        self._dialect_name = self._engine.dialect.name
        self._use_native_json = self._dialect_name == "postgresql"

    def _cache_enabled(self) -> bool:
        return ENV_CONFIG.response_store_cache

    def _cache_ttl_seconds(self) -> int:
        return max(1, ENV_CONFIG.response_store_cache_ttl_seconds)

    def _cache_key(self, *, response_id: str) -> str:
        return f"vtol:responses_state:v{SCHEMA_VERSION}:{response_id}"

    def _encode_cache_entry(self, stored: StoredResponse) -> dict[str, Any]:
        return {
            "response_id": stored.response_id,
            "previous_response_id": stored.previous_response_id,
            "model": stored.model,
            "created_at": stored.created_at.astimezone(timezone.utc).isoformat(),
            "expires_at": (
                None
                if stored.expires_at is None
                else stored.expires_at.astimezone(timezone.utc).isoformat()
            ),
            "store": stored.store,
            "schema_version": stored.schema_version,
            "state": stored.state,
        }

    def _decode_cache_entry(self, raw: object) -> StoredResponse:
        if not isinstance(raw, dict):
            raise TypeError(f"cache entry is not an object: {type(raw)!r}")
        created_at_raw = raw.get("created_at")
        if not isinstance(created_at_raw, str):
            raise TypeError("cache entry missing created_at")
        expires_at_raw = raw.get("expires_at")
        if expires_at_raw is not None and not isinstance(expires_at_raw, str):
            raise TypeError("cache entry has invalid expires_at")
        state = raw.get("state")
        if not isinstance(state, dict):
            raise TypeError("cache entry missing state object")
        return StoredResponse(
            response_id=str(raw["response_id"]),
            previous_response_id=raw.get("previous_response_id"),
            model=str(raw["model"]),
            created_at=datetime.fromisoformat(created_at_raw),
            expires_at=None if expires_at_raw is None else datetime.fromisoformat(expires_at_raw),
            store=bool(raw["store"]),
            schema_version=int(raw["schema_version"]),
            state=state,
        )

    async def _cache_get(self, *, response_id: str) -> StoredResponse | None:
        key = self._cache_key(response_id=response_id)
        raw = await CACHE.get_json(key)
        if raw is None:
            return None
        return self._decode_cache_entry(raw)

    async def _cache_set(self, stored: StoredResponse) -> None:
        key = self._cache_key(response_id=stored.response_id)
        await CACHE.set_json(key, self._encode_cache_entry(stored), ex=self._cache_ttl_seconds())

    @classmethod
    def from_db_url(cls, *, db_url: str) -> Self:
        """
        Convenience constructor for tests and one-off tools.

        Note: this bypasses `vtol.db` engine plumbing (instrumentation and SQLite PRAGMAs).
        Production code should use `get_default_response_store()`.
        """
        engine = create_async_engine(db_url, future=True)
        return cls(engine=engine, owns_engine=True)

    async def aclose(self) -> None:
        if self._owns_engine:
            await self._engine.dispose()

    def _schema_is_marked_ready(self) -> bool:
        # Set by `vllm-responses serve` after it initializes the DB schema once in the supervisor.
        # This is intentionally an env var (cross-process) rather than an in-memory flag.
        value = os.environ.get("VTOL_DB_SCHEMA_READY", "").strip().lower()
        return value in {"1", "true", "t", "yes", "y", "on"}

    async def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            if self._schema_is_marked_ready():
                self._schema_ready = True
                return
            if ENV_CONFIG.db_dialect == "sqlite" and ENV_CONFIG.workers > 1:
                raise RuntimeError(
                    "SQLite schema initialization is not multi-worker safe when started directly. "
                    "Use `vllm-responses serve` (recommended) or run with VTOL_WORKERS=1."
                )
            # DDL must be committed on Postgres. Using `engine.begin()` ensures the DDL is
            # executed within a transaction that is committed on success.
            async with self._engine.begin() as conn:
                if ENV_CONFIG.db_dialect == "postgresql":
                    async with postgres_advisory_lock(conn, name=POSTGRES_SCHEMA_LOCK_NAME):
                        await conn.run_sync(SQLModel.metadata.create_all)
                else:
                    await conn.run_sync(SQLModel.metadata.create_all)
                # Note: SQLModel.metadata.create_all creates indexes defined with index=True in the model.
                # Explicit index creation is removed in favor of SQLModel definitions.
            self._schema_ready = True

    async def get(self, *, response_id: str) -> StoredResponse | None:
        if self._cache_enabled():
            try:
                cached = await self._cache_get(response_id=response_id)
            except Exception:
                cached = None
            if cached is not None:
                return cached

        await self.ensure_schema()
        async with self._sessionmaker() as session:
            result = await session.exec(
                select(ResponsesState).where(ResponsesState.response_id == response_id)
            )
            row = result.first()
            if row is None:
                return None
            state_raw = row.state_json
            state = _coerce_state(state_raw)
            stored = StoredResponse(
                response_id=str(row.response_id),
                previous_response_id=row.previous_response_id,
                model=str(row.model),
                created_at=row.created_at,
                expires_at=row.expires_at,
                store=bool(row.store),
                schema_version=int(row.schema_version),
                state=state,
            )
            if self._cache_enabled():
                try:
                    await self._cache_set(stored)
                except Exception:
                    pass
            return stored

    async def put_completed(
        self,
        *,
        request: vLLMResponsesRequest,
        hydrated_request: vLLMResponsesRequest,
        response: OpenAIResponsesResponse,
    ) -> None:
        await self.ensure_schema()

        if response.status != "completed":
            return
        if not response.id:
            return

        hydrated_input = _normalize_input(hydrated_request.input)
        payload = StoredResponsePayload(
            hydrated_input=hydrated_input,
            response=response,
            effective_tools=hydrated_request.tools,
            effective_tool_choice=hydrated_request.tool_choice,
            effective_instructions=hydrated_request.instructions,
        )
        state_obj = payload.model_dump(mode="json", exclude_none=True)
        state_value: Any = state_obj if self._use_native_json else json_dumps(state_obj)

        created_at = _utcnow()
        store_intent = request.store

        async with self._sessionmaker() as session:
            try:
                state_entry = ResponsesState(
                    response_id=response.id,
                    previous_response_id=response.previous_response_id,
                    model=response.model,
                    created_at=created_at,
                    expires_at=None,
                    store=store_intent,
                    schema_version=SCHEMA_VERSION,
                    state_json=state_value,
                )
                session.add(state_entry)
                await session.commit()
            except IntegrityError as e:
                await session.rollback()
                raise BadInputError(f"Response id already exists: {response.id}") from e

        if self._cache_enabled():
            try:
                await self._cache_set(
                    StoredResponse(
                        response_id=response.id,
                        previous_response_id=response.previous_response_id,
                        model=response.model,
                        created_at=created_at,
                        expires_at=None,
                        store=store_intent,
                        schema_version=SCHEMA_VERSION,
                        state=state_obj,
                    )
                )
            except Exception:
                pass

    async def rehydrate_request(self, *, request: vLLMResponsesRequest) -> vLLMResponsesRequest:
        """Return an upstream-ready request whose `input` is a fully hydrated conversation history.

        This implements the Stage 2 rule:
        `hydrated_input = stored.hydrated_input + stored.response.output + new.input`.
        """

        if not request.previous_response_id:
            # Normalize `input` to list form so we can store deterministically.
            if isinstance(request.input, str):
                return request.model_copy(update={"input": _normalize_input(request.input)})
            return request

        stored = await self.get(response_id=request.previous_response_id)
        if stored is None:
            raise BadInputError(f"Unknown previous_response_id: {request.previous_response_id}")

        payload = stored.payload()

        new_input = _normalize_input(request.input)
        hydrated_input = [*payload.hydrated_input, *payload.response.output, *new_input]

        # Determine effective tools / tool_choice.
        # Use `model_fields_set` to distinguish "omitted" from "explicitly provided".
        fields_set = request.model_fields_set
        tools_omitted = "tools" not in fields_set
        tool_choice_omitted = "tool_choice" not in fields_set

        effective_tools = request.tools if not tools_omitted else payload.effective_tools
        effective_tool_choice = (
            request.tool_choice if not tool_choice_omitted else payload.effective_tool_choice
        )

        # OpenAI behavior evidence: if tools are omitted, forcing a specific function tool_choice is invalid.
        if (
            tools_omitted
            and not tool_choice_omitted
            and isinstance(
                request.tool_choice,
                (OpenAIFunctionToolChoice, OpenAIHostedToolChoice, OpenAIAllowedToolsChoice),
            )
        ):
            raise BadInputError(
                "tool_choice cannot reference a specific tool when tools are omitted; provide tools or omit tool_choice."
            )

        # Ensure effective types are valid Pydantic objects.
        effective_tools_validated = (
            None
            if effective_tools is None
            else _tools_list_adapter.validate_python(effective_tools)
        )
        effective_tool_choice_validated = _tool_choice_adapter.validate_python(
            effective_tool_choice
        )

        # Build upstream request: `previous_response_id` is consumed for hydration and should not influence
        # request-to-message conversion.
        return request.model_copy(
            update={
                "previous_response_id": None,
                "input": hydrated_input,
                "tools": effective_tools_validated,
                "tool_choice": effective_tool_choice_validated,
            }
        )


_DEFAULT_STORE: DBResponseStore | None = None


def get_default_response_store() -> DBResponseStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = DBResponseStore(engine=create_db_engine_async(), owns_engine=False)
    return _DEFAULT_STORE


def _coerce_state(value: Any) -> dict[str, Any]:
    """
    Normalize the `responses_state.state_json` DB value into a Python dict.

    Storage strategy:
    - SQLite: store JSON text (TEXT); read back and parse.
    - Postgres: store native JSONB; read back as a Python dict (driver-dependent).
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        raw = json_loads(value.decode("utf-8", errors="replace"))
    elif isinstance(value, str):
        raw = json_loads(value)
    else:
        raw = value
    if not isinstance(raw, dict):
        raise BadInputError(f"Invalid stored ResponseStore payload type: {type(raw)!r}")
    return raw
