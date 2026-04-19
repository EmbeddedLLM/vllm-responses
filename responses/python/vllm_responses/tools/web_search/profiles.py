from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from vllm_responses.tools.base.types import (
    ActionBindingSpec,
    BuiltinMcpServerDefinition,
    ProfiledBuiltinProfileResolutionProvider,
    ResolvedActionBinding,
    ResolvedProfiledBuiltinTool,
)
from vllm_responses.tools.ids import WEB_SEARCH_TOOL
from vllm_responses.tools.web_search.adapters import WEB_SEARCH_ADAPTER_SPECS
from vllm_responses.tools.web_search.mcp_provision import WEB_SEARCH_BUILTIN_MCP_SERVERS

DEFAULT_WEB_SEARCH_PROFILE_ID = "exa_mcp"


@dataclass(frozen=True, slots=True)
class WebSearchProfileSpec:
    profile_id: str
    action_bindings: tuple[ActionBindingSpec, ...]


_WEB_SEARCH_PROFILES: dict[str, WebSearchProfileSpec] = {
    "exa_mcp": WebSearchProfileSpec(
        profile_id="exa_mcp",
        action_bindings=(
            ActionBindingSpec(
                action_name="search",
                adapter_id="exa_mcp_search",
            ),
            ActionBindingSpec(
                action_name="open_page",
                adapter_id="exa_mcp_open_page",
            ),
        ),
    ),
    "duckduckgo_plus_fetch": WebSearchProfileSpec(
        profile_id="duckduckgo_plus_fetch",
        action_bindings=(
            ActionBindingSpec(
                action_name="search",
                adapter_id="duckduckgo_common_search",
            ),
            ActionBindingSpec(
                action_name="open_page",
                adapter_id="fetch_mcp_open_page",
            ),
        ),
    ),
}


def get_web_search_profile_ids() -> tuple[str, ...]:
    return tuple(sorted(_WEB_SEARCH_PROFILES))


@lru_cache(maxsize=1)
def validate_web_search_planning_descriptors() -> None:
    default_profile_id = DEFAULT_WEB_SEARCH_PROFILE_ID
    if default_profile_id is not None:
        try:
            _WEB_SEARCH_PROFILES[default_profile_id]
        except KeyError as exc:
            raise RuntimeError(
                "web_search default profile metadata points to an unknown profile "
                f"{default_profile_id!r}."
            ) from exc

    for profile in _WEB_SEARCH_PROFILES.values():
        for binding in profile.action_bindings:
            try:
                adapter_spec = WEB_SEARCH_ADAPTER_SPECS[binding.adapter_id]
            except KeyError as exc:
                raise RuntimeError(
                    "web_search profile metadata references an unknown adapter "
                    f"{binding.adapter_id!r} for action {binding.action_name!r}."
                ) from exc
            if binding.action_name != adapter_spec.action_name:
                raise RuntimeError(
                    "web_search profile metadata references adapter "
                    f"{binding.adapter_id!r} for action {binding.action_name!r}, "
                    f"but that adapter handles {adapter_spec.action_name!r}."
                )
            for server_label in adapter_spec.builtin_mcp_server_labels:
                if server_label not in WEB_SEARCH_BUILTIN_MCP_SERVERS:
                    raise RuntimeError(
                        "web_search planning metadata references an unknown Built-in MCP "
                        f"server label {server_label!r}."
                    )


class WebSearchProfileResolutionProvider(ProfiledBuiltinProfileResolutionProvider):
    def resolve(self, profile_id: str) -> ResolvedProfiledBuiltinTool:
        validate_web_search_planning_descriptors()
        try:
            profile = _WEB_SEARCH_PROFILES[profile_id]
        except KeyError as exc:
            raise ValueError(
                f"Unknown profile {profile_id!r} for built-in tool {WEB_SEARCH_TOOL!r}."
            ) from exc

        action_bindings: list[ResolvedActionBinding] = []
        builtin_mcp_server_labels: set[str] = set()
        for binding in profile.action_bindings:
            adapter_spec = WEB_SEARCH_ADAPTER_SPECS[binding.adapter_id]
            builtin_mcp_server_labels.update(adapter_spec.builtin_mcp_server_labels)
            action_bindings.append(
                ResolvedActionBinding(
                    action_name=binding.action_name,
                    adapter_id=binding.adapter_id,
                    builtin_mcp_server_labels=adapter_spec.builtin_mcp_server_labels,
                )
            )

        return ResolvedProfiledBuiltinTool(
            tool_type=WEB_SEARCH_TOOL,
            profile_id=profile_id,
            action_bindings=tuple(action_bindings),
            builtin_mcp_server_labels=tuple(sorted(builtin_mcp_server_labels)),
        )

    def validate_profile(self, profile_id: str | None) -> None:
        if profile_id is None:
            return
        self.resolve(profile_id)

    def required_mcp_definitions(
        self,
        profile_id: str | None,
    ) -> tuple[BuiltinMcpServerDefinition, ...]:
        validate_web_search_planning_descriptors()
        if profile_id is None:
            return ()

        resolved = self.resolve(profile_id)
        return tuple(
            WEB_SEARCH_BUILTIN_MCP_SERVERS[label] for label in resolved.builtin_mcp_server_labels
        )


WEB_SEARCH_PROFILE_RESOLUTION_PROVIDER = WebSearchProfileResolutionProvider()
