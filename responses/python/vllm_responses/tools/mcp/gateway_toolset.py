from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema import exceptions as jsonschema_exceptions
from pydantic import TypeAdapter
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets.abstract import AbstractToolset, ToolsetTool

from vllm_responses.tools.mcp.backend import McpExecutionBackend
from vllm_responses.tools.mcp.types import McpExecutionResult, McpToolRef
from vllm_responses.tools.mcp.utils import (
    build_mcp_tool_result_payload,
    redact_and_truncate_error_text,
)

_DICT_ARGS_VALIDATOR = TypeAdapter(dict[str, Any]).validator


@dataclass(frozen=True, slots=True)
class ResolvedMcpTool:
    """Gateway-facing MCP tool metadata plus the backend that executes it."""

    internal_name: str
    ref: McpToolRef
    backend: McpExecutionBackend
    description: str
    input_schema: dict[str, object]
    schema_validator: Draft202012Validator
    secret_values: tuple[str, ...] = ()


class McpGatewayToolset(AbstractToolset[Any]):
    """Single pydantic-ai toolset that exposes all effective MCP tools."""

    def __init__(
        self,
        *,
        tools: list[ResolvedMcpTool],
        id: str | None = None,
    ) -> None:
        self._tools_by_name = {
            tool.internal_name: tool for tool in sorted(tools, key=lambda t: t.internal_name)
        }
        self._toolset_tools = {
            internal_name: ToolsetTool(
                toolset=self,
                tool_def=ToolDefinition(
                    name=internal_name,
                    description=tool.description,
                    parameters_json_schema=tool.input_schema,
                ),
                max_retries=0,
                args_validator=_DICT_ARGS_VALIDATOR,
            )
            for internal_name, tool in self._tools_by_name.items()
        }
        self._id = id

    @property
    def id(self) -> str | None:
        return self._id

    async def get_tools(self, ctx) -> dict[str, ToolsetTool[Any]]:
        _ = ctx
        return dict(self._toolset_tools)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx,
        tool: ToolsetTool[Any],
    ) -> Any:
        _ = ctx, tool
        resolved = self._tools_by_name.get(name)
        if resolved is None:  # pragma: no cover - defensive
            raise RuntimeError("MCP tool resolution failed for internal tool name.")

        validation_error = _validate_mcp_tool_arguments(
            validator=resolved.schema_validator,
            arguments=tool_args,
        )
        if validation_error is not None:
            # Return item-level MCP failure payloads instead of raising; a
            # failed MCP call should not become a fatal model stream error.
            return build_mcp_tool_result_payload(
                ref=resolved.ref,
                result=McpExecutionResult(
                    ok=False,
                    output_text=None,
                    error_text=validation_error,
                ),
            )

        result: McpExecutionResult
        try:
            result = await resolved.backend.call_tool(
                resolved.ref.tool_name,
                dict(tool_args),
            )
        except Exception as exc:
            result = McpExecutionResult(
                ok=False,
                output_text=None,
                error_text=redact_and_truncate_error_text(
                    text=str(exc).strip() or exc.__class__.__name__,
                    secret_values=resolved.secret_values,
                ),
            )

        return build_mcp_tool_result_payload(ref=resolved.ref, result=result)


def _validate_mcp_tool_arguments(
    *,
    validator: Draft202012Validator,
    arguments: dict[str, Any],
) -> str | None:
    try:
        validator.validate(arguments)
        return None
    except jsonschema_exceptions.ValidationError as exc:
        path = ".".join(str(part) for part in exc.path)
        if path:
            return f"input_validation_error: {exc.message} (path={path})"
        return f"input_validation_error: {exc.message}"
