from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import ORJSONResponse

from vllm_responses.mcp.runtime_client import (
    BuiltinMcpRuntimeClient,
    BuiltinMcpRuntimeTransportError,
    BuiltinMcpRuntimeUnavailableServerError,
    BuiltinMcpRuntimeUnknownServerError,
)

router = APIRouter()


def _get_runtime_client(request: Request) -> BuiltinMcpRuntimeClient | None:
    app_state = getattr(request.app.state, "vllm_responses", None)
    if app_state is None:
        return None
    return getattr(app_state, "builtin_mcp_runtime_client", None)


@router.get("/v1/mcp/servers")
async def list_mcp_servers(request: Request) -> ORJSONResponse:
    runtime_client = _get_runtime_client(request)
    if runtime_client is None or not runtime_client.is_enabled():
        return ORJSONResponse(status_code=200, content={"object": "list", "data": []})

    try:
        servers = await runtime_client.list_servers()
    except (
        BuiltinMcpRuntimeTransportError,
        BuiltinMcpRuntimeUnknownServerError,
        BuiltinMcpRuntimeUnavailableServerError,
    ) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    data = [
        {
            "server_label": info.server_label,
            "enabled": info.enabled,
            "available": info.available,
            "required": info.required,
            "transport": info.transport,
        }
        for info in servers
    ]
    return ORJSONResponse(status_code=200, content={"object": "list", "data": data})


@router.get("/v1/mcp/servers/{server_label}/tools")
async def list_mcp_server_tools(request: Request, server_label: str) -> ORJSONResponse:
    runtime_client = _get_runtime_client(request)
    if runtime_client is None or not runtime_client.is_enabled():
        raise HTTPException(status_code=404, detail=f"Unknown MCP server_label: {server_label}")

    try:
        tools = await runtime_client.list_tools(server_label)
    except BuiltinMcpRuntimeUnknownServerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BuiltinMcpRuntimeUnavailableServerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BuiltinMcpRuntimeTransportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    tools = sorted(tools, key=lambda tool: tool.name)
    return ORJSONResponse(
        status_code=200,
        content={
            "server_label": server_label,
            "available": True,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ],
        },
    )
