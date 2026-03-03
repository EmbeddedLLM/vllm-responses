from vllm_responses.tools import CODE_INTERPRETER_TOOL, TOOLS


def test_registered_tool_name_matches_registry_key() -> None:
    tool = TOOLS[CODE_INTERPRETER_TOOL]
    assert tool.name == CODE_INTERPRETER_TOOL
