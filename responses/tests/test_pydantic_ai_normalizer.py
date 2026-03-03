from __future__ import annotations

from pydantic_ai import (
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ToolCallPart,
    ToolCallPartDelta,
)

from vllm_responses.responses_core.models import (
    CodeInterpreterCallCodeDelta,
    CodeInterpreterCallCodeDone,
    CodeInterpreterCallStarted,
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
