from __future__ import annotations

from jsonschema import Draft202012Validator
from jsonschema import exceptions as jsonschema_exceptions

from vllm_responses.tools.mcp.gateway_toolset import McpGatewayToolset, ResolvedMcpTool
from vllm_responses.tools.mcp.resolver import ResolvedMcpServerTools
from vllm_responses.tools.mcp.types import McpToolInfo, McpToolRef
from vllm_responses.tools.mcp.utils import (
    build_internal_mcp_tool_name,
    normalize_mcp_input_schema,
)
from vllm_responses.utils.exceptions import BadInputError


def build_mcp_toolset_for_request(
    *,
    mcp_servers: dict[str, ResolvedMcpServerTools],
    selected_mcp_tool_infos_by_server: dict[str, dict[str, McpToolInfo]],
    mcp_tool_name_map: dict[str, McpToolRef],
    toolset_id: str = "vllm_responses_mcp",
) -> McpGatewayToolset | None:
    """Build the request-local pydantic-ai MCP toolset.

    The caller owns `mcp_tool_name_map` because it is also used by response
    normalization to map internal pydantic-ai tool names back to MCP refs.
    """

    mcp_resolved_tools: list[ResolvedMcpTool] = []
    for server_label, server_runtime in sorted(mcp_servers.items()):
        tool_infos = selected_mcp_tool_infos_by_server.get(
            server_label, server_runtime.allowed_tool_infos
        )
        for tool_name, tool_info in tool_infos.items():
            normalized_schema = _normalize_mcp_input_schema(
                server_label=server_label,
                tool_name=tool_name,
                input_schema=tool_info.input_schema,
            )
            ref = McpToolRef(
                server_label=server_label,
                tool_name=tool_name,
                mode=server_runtime.mode,
            )
            internal_tool_name = register_internal_mcp_tool_name(
                ref=ref,
                mcp_tool_name_map=mcp_tool_name_map,
            )

            description = (
                tool_info.description.strip()
                if isinstance(tool_info.description, str) and tool_info.description.strip()
                else f"MCP tool {server_label}:{tool_name}"
            )
            mcp_resolved_tools.append(
                ResolvedMcpTool(
                    internal_name=internal_tool_name,
                    ref=ref,
                    backend=server_runtime.backend,
                    description=description,
                    input_schema=normalized_schema,
                    schema_validator=Draft202012Validator(normalized_schema),
                    secret_values=server_runtime.secret_values,
                )
            )

    if not mcp_resolved_tools:
        return None
    return McpGatewayToolset(tools=mcp_resolved_tools, id=toolset_id)


def register_internal_mcp_tool_name(
    *,
    ref: McpToolRef,
    mcp_tool_name_map: dict[str, McpToolRef],
) -> str:
    """Return a stable pydantic-ai-safe internal name for an MCP ref."""

    mcp_internal_name_by_ref = {
        existing_ref: name for name, existing_ref in mcp_tool_name_map.items()
    }
    existing_name = mcp_internal_name_by_ref.get(ref)
    if existing_name is not None:
        return existing_name
    try:
        internal_tool_name = build_internal_mcp_tool_name(
            ref=ref,
            existing_map=mcp_tool_name_map,
        )
    except ValueError as exc:
        raise BadInputError(str(exc)) from exc
    mcp_tool_name_map[internal_tool_name] = ref
    return internal_tool_name


def _normalize_mcp_input_schema(
    *,
    server_label: str,
    tool_name: str,
    input_schema: dict[str, object],
) -> dict[str, object]:
    try:
        normalized = normalize_mcp_input_schema(input_schema)
    except (TypeError, ValueError) as exc:
        raise BadInputError(
            f"MCP tool {server_label!r}:{tool_name!r} has invalid `input_schema`: {exc}"
        ) from exc

    if normalized["type"] != "object":
        raise BadInputError(
            f"MCP tool {server_label!r}:{tool_name!r} has invalid `input_schema`; "
            "root `type` must be `object`."
        )

    try:
        Draft202012Validator.check_schema(normalized)
    except jsonschema_exceptions.SchemaError as exc:
        raise BadInputError(
            f"MCP tool {server_label!r}:{tool_name!r} has invalid `input_schema`: {exc.message}"
        ) from exc

    return normalized
