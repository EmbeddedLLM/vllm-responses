from __future__ import annotations

from vllm_responses.tools.code_interpreter import register_code_interpreter_tool
from vllm_responses.tools.web_search.tool import register_web_search_tool


def register_runtime_tool_handlers() -> None:
    # Keep built-in registration explicit so importing a tool module does not
    # mutate the process-global pydantic-ai tool registry.
    register_code_interpreter_tool()
    register_web_search_tool()
