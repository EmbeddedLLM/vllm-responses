from __future__ import annotations

from pydantic_ai import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
    ToolReturnPart,
)

from vllm_responses.responses_core.models import (
    CodeInterpreterCallCodeDelta,
    CodeInterpreterCallCodeDone,
    CodeInterpreterCallStarted,
    FunctionCallArgumentsDelta,
    FunctionCallDone,
    ReasoningDelta,
    ReasoningDone,
    ReasoningStarted,
    WebSearchCallCompleted,
    WebSearchCallSearching,
    WebSearchCallStarted,
)
from vllm_responses.responses_core.normalizer import PydanticAINormalizer


def test_code_interpreter_code_deltas_emitted_from_tool_call_args_json_fragments():
    normalizer = PydanticAINormalizer(
        builtin_tool_names={"code_interpreter"},
        code_interpreter_tool_name="code_interpreter",
    )

    events = [
        PartStartEvent(
            index=0,
            part=ToolCallPart(tool_name="code_interpreter", args=None, tool_call_id="call_1"),
        ),
        PartDeltaEvent(
            index=0,
            delta=ToolCallPartDelta(tool_call_id="call_1", args_delta='{"code":"print('),
        ),
        PartDeltaEvent(
            index=0,
            delta=ToolCallPartDelta(tool_call_id="call_1", args_delta="1"),
        ),
        PartDeltaEvent(
            index=0,
            delta=ToolCallPartDelta(tool_call_id="call_1", args_delta=')"}'),
        ),
        PartEndEvent(
            index=0,
            part=ToolCallPart(
                tool_name="code_interpreter",
                args='{"code":"print(1)"}',
                tool_call_id="call_1",
            ),
        ),
    ]

    out = []
    for e in events:
        out.extend(list(normalizer.on_event(e)))

    assert any(isinstance(e, CodeInterpreterCallStarted) for e in out)

    deltas = [e for e in out if isinstance(e, CodeInterpreterCallCodeDelta)]
    assert [d.delta for d in deltas] == ["print(", "1", ")"]

    done = [e for e in out if isinstance(e, CodeInterpreterCallCodeDone)]
    assert len(done) == 1
    assert done[0].code == "print(1)"


def test_web_search_normalizer_emits_web_search_events_without_function_arg_events():
    normalizer = PydanticAINormalizer(
        builtin_tool_names={"web_search"},
        code_interpreter_tool_name="code_interpreter",
    )

    events = [
        PartStartEvent(
            index=0,
            part=ToolCallPart(tool_name="web_search", args=None, tool_call_id="call_ws_1"),
        ),
        PartDeltaEvent(
            index=0,
            delta=ToolCallPartDelta(
                tool_call_id="call_ws_1",
                args_delta='{"action":"search","query":"example query"}',
            ),
        ),
        PartEndEvent(
            index=0,
            part=ToolCallPart(
                tool_name="web_search",
                args='{"action":"search","query":"example query"}',
                tool_call_id="call_ws_1",
            ),
        ),
        FunctionToolCallEvent(
            part=ToolCallPart(
                tool_name="web_search",
                args='{"action":"search","query":"example query"}',
                tool_call_id="call_ws_1",
            )
        ),
        FunctionToolResultEvent(
            result=ToolReturnPart(
                tool_name="web_search",
                content=(
                    '{"action":{"type":"search","query":"example query","sources":'
                    '[{"type":"url","url":"https://example.com/a"}]}}'
                ),
                tool_call_id="call_ws_1",
            )
        ),
    ]

    out = []
    for event in events:
        out.extend(list(normalizer.on_event(event)))

    assert any(isinstance(event, WebSearchCallStarted) for event in out)
    assert any(isinstance(event, WebSearchCallSearching) for event in out)
    completed = [event for event in out if isinstance(event, WebSearchCallCompleted)]
    assert len(completed) == 1
    assert completed[0].action_type == "search"
    assert completed[0].query == "example query"
    assert completed[0].sources == ({"type": "url", "url": "https://example.com/a"},)
    assert not any(isinstance(event, FunctionCallArgumentsDelta) for event in out)
    assert not any(isinstance(event, FunctionCallDone) for event in out)


def test_reasoning_normalizer_prefers_raw_reasoning_content_from_provider_details():
    normalizer = PydanticAINormalizer(
        builtin_tool_names=set(),
        code_interpreter_tool_name="code_interpreter",
    )

    events = [
        PartStartEvent(
            index=0,
            part=ThinkingPart(content="", provider_details={"raw_content": ["alpha", "beta"]}),
        ),
        PartEndEvent(
            index=0,
            part=ThinkingPart(content="", provider_details={"raw_content": ["alpha", "beta"]}),
        ),
    ]

    out = []
    for event in events:
        out.extend(list(normalizer.on_event(event)))

    assert any(isinstance(event, ReasoningStarted) for event in out)
    deltas = [event.delta for event in out if isinstance(event, ReasoningDelta)]
    assert deltas == ["alphabeta"]
    done = [event for event in out if isinstance(event, ReasoningDone)]
    assert len(done) == 1
    assert done[0].text == "alphabeta"


def test_reasoning_normalizer_emits_raw_reasoning_deltas_from_provider_details_updates():
    normalizer = PydanticAINormalizer(
        builtin_tool_names=set(),
        code_interpreter_tool_name="code_interpreter",
    )

    def _append_raw(delta: str, index: int):
        def _update(existing):
            details = {**(existing or {})}
            raw_content = list(details.get("raw_content", []))
            while len(raw_content) <= index:
                raw_content.append("")
            raw_content[index] += delta
            details["raw_content"] = raw_content
            return details

        return _update

    events = [
        PartStartEvent(index=0, part=ThinkingPart(content="")),
        PartDeltaEvent(
            index=0,
            delta=ThinkingPartDelta(
                provider_name="openai", provider_details=_append_raw("abc", 0)
            ),
        ),
        PartDeltaEvent(
            index=0,
            delta=ThinkingPartDelta(
                provider_name="openai", provider_details=_append_raw("def", 0)
            ),
        ),
        PartEndEvent(index=0, part=ThinkingPart(content="")),
    ]

    out = []
    for event in events:
        out.extend(list(normalizer.on_event(event)))

    deltas = [event.delta for event in out if isinstance(event, ReasoningDelta)]
    assert deltas == ["abc", "def"]
    done = [event for event in out if isinstance(event, ReasoningDone)]
    assert len(done) == 1
    assert done[0].text == "abcdef"
