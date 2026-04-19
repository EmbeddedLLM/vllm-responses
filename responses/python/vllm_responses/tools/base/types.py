from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from vllm_responses.configs.sources import EnvSource


@runtime_checkable
class BuiltinActionAdapter(Protocol):
    """One action implementation behind a profiled built-in tool.

    Example: the public `web_search` tool can bind `search` and `open_page` to
    different adapters depending on profile. An adapter may call direct Python
    code, a pydantic-ai common tool/toolset, or an MCP-backed runtime, but the
    public built-in contract stays stable.
    """

    tool_type: str
    action_name: str
    adapter_id: str
    config_model: type[BaseModel] | None


@runtime_checkable
class ProfiledBuiltinProfileResolutionProvider(Protocol):
    """Static profile planner for profiled built-ins.

    Profiled built-ins intentionally support two backend styles: managed MCP
    servers and tool-owned adapters around direct Python or pydantic-ai helpers.
    Only managed MCP needs shared runtime provisioning here.
    """

    def resolve(self, profile_id: str) -> "ResolvedProfiledBuiltinTool": ...

    def validate_profile(self, profile_id: str | None) -> None: ...

    def required_mcp_definitions(
        self,
        profile_id: str | None,
    ) -> Sequence["BuiltinMcpServerDefinition"]: ...


@dataclass(frozen=True, slots=True)
class ActionBindingSpec:
    action_name: str
    adapter_id: str


@dataclass(frozen=True, slots=True)
class BuiltinMcpServerDefinition:
    server_label: str
    # Raw gateway-managed MCP server entry. This matches the JSON-object shape
    # accepted under `mcpServers.<label>` when the built-in definition is static.
    server_entry: dict[str, object] | None = None
    # Optional for built-ins whose final managed entry depends on operator env,
    # such as API-key-backed remote MCP servers.
    build_server_entry: Callable[[EnvSource], dict[str, object]] | None = None

    def __post_init__(self) -> None:
        if (self.server_entry is None) == (self.build_server_entry is None):
            raise ValueError(
                "BuiltinMcpServerDefinition requires exactly one of "
                "`server_entry` or `build_server_entry`."
            )


@dataclass(frozen=True, slots=True)
class ResolvedActionBinding:
    action_name: str
    adapter_id: str
    builtin_mcp_server_labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedProfiledBuiltinTool:
    tool_type: str
    profile_id: str
    action_bindings: tuple[ResolvedActionBinding, ...]
    builtin_mcp_server_labels: tuple[str, ...] = ()
