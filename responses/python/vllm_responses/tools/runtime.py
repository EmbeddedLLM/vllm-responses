from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vllm_responses.configs.runtime import RuntimeConfig

if TYPE_CHECKING:
    from vllm_responses.tools.web_search.runtime import WebSearchToolRuntime


@dataclass(frozen=True, slots=True)
class ToolRuntimeContext:
    """Request-local runtime state for gateway-executed built-ins.

    Built-in tool functions are registered with pydantic-ai as plain callables,
    so they do not receive the LMEngine request object directly. A context var
    keeps per-request services available only while the model run is executing.

    New built-ins should add their request-scoped runtime object here, build it
    in `LMEngine._build_response_pipeline(...)`, and read it from their tool
    callable through `require_tool_runtime_context()`. Keep backend-specific
    validation inside the owning tool's `runtime.py`; this context is only the
    carrier, not a generic binder.
    """

    runtime_config: RuntimeConfig
    web_search: WebSearchToolRuntime | None = None


_REQUEST_TOOL_RUNTIME_CONTEXT: ContextVar[ToolRuntimeContext | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)


@contextmanager
def bind_tool_runtime_context(tool_runtime_context: ToolRuntimeContext):
    """Bind built-in tool runtime state for the current model run."""

    token: Token[ToolRuntimeContext | None] = _REQUEST_TOOL_RUNTIME_CONTEXT.set(
        tool_runtime_context
    )
    try:
        yield
    finally:
        _REQUEST_TOOL_RUNTIME_CONTEXT.reset(token)


def get_tool_runtime_context() -> ToolRuntimeContext | None:
    return _REQUEST_TOOL_RUNTIME_CONTEXT.get()


def require_tool_runtime_context() -> ToolRuntimeContext:
    """Return request-local tool runtime state or fail as a programming error."""

    tool_runtime_context = get_tool_runtime_context()
    if tool_runtime_context is None:
        raise RuntimeError("Tool runtime context is not bound for this request.")
    return tool_runtime_context
