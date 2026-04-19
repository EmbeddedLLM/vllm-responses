from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

McpMode: TypeAlias = Literal["hosted", "request_remote"]


@dataclass(frozen=True, slots=True)
class McpToolRef:
    server_label: str
    tool_name: str
    mode: McpMode = "hosted"


@dataclass(frozen=True, slots=True)
class RequestRemoteMcpServerBinding:
    server_label: str
    server_url: str
    authorization: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpExecutionResult:
    ok: bool
    output_text: str | None
    error_text: str | None


@dataclass(frozen=True, slots=True)
class McpToolInfo:
    name: str
    description: str | None
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class McpServerInfo:
    server_label: str
    enabled: bool
    available: bool
    required: bool
    transport: str
