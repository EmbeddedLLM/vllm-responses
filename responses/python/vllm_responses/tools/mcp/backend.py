from __future__ import annotations

from typing import Any, Protocol

from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool

from vllm_responses.tools.mcp.fastmcp_runtime import extract_mcp_tool_infos
from vllm_responses.tools.mcp.runtime_client import (
    BuiltinMcpRuntimeClient,
    BuiltinMcpRuntimeToolMissingError,
)
from vllm_responses.tools.mcp.types import McpExecutionResult, McpToolInfo
from vllm_responses.tools.mcp.utils import (
    canonicalize_output_text,
    is_mcp_tool_keyerror,
    redact_and_truncate_error_text,
)


class McpExecutionBackend(Protocol):
    """Execution boundary used by the gateway toolset.

    Concrete backends may wrap the managed Built-in MCP runtime service or a
    request-declared pydantic-ai toolset, but callers only see MCP inventory and
    call results.
    """

    async def list_tools(self) -> dict[str, McpToolInfo]: ...

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> McpExecutionResult: ...


class ManagedMcpBackend:
    """Backend for gateway-managed MCP servers exposed through the runtime API.

    "Managed" is about ownership/provisioning, not transport. The underlying
    server may be remote HTTP (for example Exa) or local stdio (for example Fetch).
    Responses-facing MCP events still report this mode as `hosted`.
    """

    def __init__(
        self,
        *,
        server_label: str,
        runtime_client: BuiltinMcpRuntimeClient,
    ) -> None:
        self._server_label = server_label
        self._runtime_client = runtime_client

    async def list_tools(self) -> dict[str, McpToolInfo]:
        tool_infos = await self._runtime_client.list_tools(self._server_label)
        return {tool.name: tool for tool in tool_infos}

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> McpExecutionResult:
        try:
            return await self._runtime_client.call_tool(
                server_label=self._server_label,
                tool_name=tool_name,
                arguments=dict(arguments),
            )
        except BuiltinMcpRuntimeToolMissingError:
            return _missing_tool_result(
                server_label=self._server_label,
                tool_name=tool_name,
            )
        except Exception as exc:
            return McpExecutionResult(
                ok=False,
                output_text=None,
                error_text=str(exc).strip() or exc.__class__.__name__,
            )


class RequestRemoteMcpBackend:
    """Backend for request-declared remote MCP servers.

    pydantic-ai requires the original ToolsetTool when calling a tool. Cache the
    inventory found during declaration resolution and refresh it once if the
    upstream toolset reports a stale tool key.
    """

    def __init__(
        self,
        *,
        server_label: str,
        toolset: AbstractToolset[Any],
        secret_values: tuple[str, ...],
    ) -> None:
        self._server_label = server_label
        self._toolset = toolset
        self._secret_values = secret_values
        self._mcp_tools_by_name: dict[str, ToolsetTool[Any]] = {}

    async def list_tools(self) -> dict[str, McpToolInfo]:
        mcp_tools = await self._toolset.get_tools(ctx=None)
        self._mcp_tools_by_name = dict(mcp_tools)
        return extract_mcp_tool_infos(mcp_tools)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> McpExecutionResult:
        try:
            mcp_tool = self._mcp_tools_by_name.get(tool_name)
            if mcp_tool is None:
                mcp_tool = await self._refresh_tool(tool_name)
                if mcp_tool is None:
                    return _missing_tool_result(
                        server_label=self._server_label,
                        tool_name=tool_name,
                    )

            try:
                raw = await self._toolset.call_tool(
                    tool_name,
                    dict(arguments),
                    ctx=None,
                    tool=mcp_tool,
                )
            except Exception as exc:
                if not is_mcp_tool_keyerror(exc, tool_name):
                    raise
                mcp_tool = await self._refresh_tool(tool_name)
                if mcp_tool is None:
                    return _missing_tool_result(
                        server_label=self._server_label,
                        tool_name=tool_name,
                    )
                raw = await self._toolset.call_tool(
                    tool_name,
                    dict(arguments),
                    ctx=None,
                    tool=mcp_tool,
                )

            return McpExecutionResult(
                ok=True,
                output_text=canonicalize_output_text(raw),
                error_text=None,
            )
        except Exception as exc:
            return McpExecutionResult(
                ok=False,
                output_text=None,
                error_text=redact_and_truncate_error_text(
                    text=str(exc).strip() or exc.__class__.__name__,
                    secret_values=self._secret_values,
                ),
            )

    async def _refresh_tool(self, tool_name: str) -> ToolsetTool[Any] | None:
        refreshed = await self._toolset.get_tools(ctx=None)
        self._mcp_tools_by_name = dict(refreshed)
        return self._mcp_tools_by_name.get(tool_name)


def _missing_tool_result(*, server_label: str, tool_name: str) -> McpExecutionResult:
    return McpExecutionResult(
        ok=False,
        output_text=None,
        error_text=f"MCP tool {tool_name!r} is not available for server {server_label!r}.",
    )
