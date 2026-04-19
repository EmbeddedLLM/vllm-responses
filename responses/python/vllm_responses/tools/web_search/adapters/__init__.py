from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from vllm_responses.tools.base.types import BuiltinActionAdapter
from vllm_responses.tools.web_search.adapters.duckduckgo_common import (
    DuckDuckGoCommonSearchAdapter,
)
from vllm_responses.tools.web_search.adapters.exa_mcp import (
    ExaMcpOpenPageAdapter,
    ExaMcpSearchAdapter,
)
from vllm_responses.tools.web_search.adapters.fetch_mcp import FetchMcpOpenPageAdapter


@dataclass(frozen=True, slots=True)
class WebSearchAdapterSpec:
    action_name: str
    adapter_id: str
    build_adapter: Callable[[], BuiltinActionAdapter]
    builtin_mcp_server_labels: tuple[str, ...] = ()


WEB_SEARCH_ADAPTER_SPECS: dict[str, WebSearchAdapterSpec] = {
    "exa_mcp_search": WebSearchAdapterSpec(
        action_name="search",
        adapter_id="exa_mcp_search",
        builtin_mcp_server_labels=("exa",),
        build_adapter=ExaMcpSearchAdapter,
    ),
    "exa_mcp_open_page": WebSearchAdapterSpec(
        action_name="open_page",
        adapter_id="exa_mcp_open_page",
        builtin_mcp_server_labels=("exa",),
        build_adapter=ExaMcpOpenPageAdapter,
    ),
    "duckduckgo_common_search": WebSearchAdapterSpec(
        action_name="search",
        adapter_id="duckduckgo_common_search",
        build_adapter=DuckDuckGoCommonSearchAdapter,
    ),
    "fetch_mcp_open_page": WebSearchAdapterSpec(
        action_name="open_page",
        adapter_id="fetch_mcp_open_page",
        builtin_mcp_server_labels=("fetch",),
        build_adapter=FetchMcpOpenPageAdapter,
    ),
}
