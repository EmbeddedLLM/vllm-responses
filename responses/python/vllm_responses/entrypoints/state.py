from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Any

from vllm_responses.types.api import UserAgent

if TYPE_CHECKING:
    from asyncio.subprocess import Process

    from vllm_responses.mcp.runtime_client import BuiltinMcpRuntimeClient
    from vllm_responses.utils.cassette_replay import CassetteReplayer


@dataclass(slots=True)
class VRAppState:
    """Typed container for `FastAPI.app.state.vllm_responses`.

    Starlette's `app.state` is a dynamic attribute bag. We store all vllm_responses-owned state under a
    single stable attribute (`app.state.vllm_responses`) so the rest of the codebase can use direct
    attribute access without defensive `getattr(...)` checks.
    """

    code_interpreter_process: Process | None = None
    cassette_replayer: CassetteReplayer | None = None
    builtin_mcp_runtime_client: BuiltinMcpRuntimeClient | None = None


@dataclass(slots=True)
class VRRequestState:
    """Typed container for `Request.state.vllm_responses`.

    This is initialized for every request by middleware so downstream code can safely access
    request-scoped values (e.g. request id) via `request.state.vllm_responses`.
    """

    id: str
    user_agent: UserAgent
    request_start_time: float = field(default_factory=perf_counter)
    timing: dict[str, float] = field(default_factory=dict)
    model_start_time: float | None = None
    billing: Any | None = None
