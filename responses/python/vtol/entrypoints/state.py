from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Any

from vtol.types.api import UserAgent

if TYPE_CHECKING:
    from asyncio.subprocess import Process

    from vtol.utils.cassette_replay import CassetteReplayer


@dataclass(slots=True)
class VtolAppState:
    """Typed container for `FastAPI.app.state.vtol`.

    Starlette's `app.state` is a dynamic attribute bag. We store all vtol-owned state under a
    single stable attribute (`app.state.vtol`) so the rest of the codebase can use direct
    attribute access without defensive `getattr(...)` checks.
    """

    code_interpreter_process: Process | None = None
    cassette_replayer: CassetteReplayer | None = None


@dataclass(slots=True)
class VtolRequestState:
    """Typed container for `Request.state.vtol`.

    This is initialized for every request by middleware so downstream code can safely access
    request-scoped values (e.g. request id) via `request.state.vtol`.
    """

    id: str
    user_agent: UserAgent
    request_start_time: float = field(default_factory=perf_counter)
    timing: dict[str, float] = field(default_factory=dict)
    model_start_time: float | None = None
    billing: Any | None = None
