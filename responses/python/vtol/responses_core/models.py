from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ItemKind = Literal["message", "reasoning", "function_call", "code_interpreter_call"]


@dataclass(frozen=True, slots=True)
class MessageStarted:
    item_key: str


@dataclass(frozen=True, slots=True)
class MessageDelta:
    item_key: str
    delta: str


@dataclass(frozen=True, slots=True)
class MessageDone:
    item_key: str
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningStarted:
    item_key: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    item_key: str
    delta: str


@dataclass(frozen=True, slots=True)
class ReasoningDone:
    item_key: str
    text: str


@dataclass(frozen=True, slots=True)
class FunctionCallStarted:
    item_key: str
    call_id: str
    name: str
    initial_arguments_json: str


@dataclass(frozen=True, slots=True)
class FunctionCallArgumentsDelta:
    item_key: str
    delta: str


@dataclass(frozen=True, slots=True)
class FunctionCallDone:
    item_key: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class CodeInterpreterCallStarted:
    item_key: str
    initial_code: str | None


@dataclass(frozen=True, slots=True)
class CodeInterpreterCallCodeDelta:
    item_key: str
    delta: str


@dataclass(frozen=True, slots=True)
class CodeInterpreterCallCodeDone:
    item_key: str
    code: str | None


@dataclass(frozen=True, slots=True)
class CodeInterpreterCallInterpreting:
    item_key: str


@dataclass(frozen=True, slots=True)
class CodeInterpreterCallCompleted:
    item_key: str
    stdout: str | None
    stderr: str | None
    result: str | None


@dataclass(frozen=True, slots=True)
class UsageFinal:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int


NormalizedEvent = (
    MessageStarted
    | MessageDelta
    | MessageDone
    | ReasoningStarted
    | ReasoningDelta
    | ReasoningDone
    | FunctionCallStarted
    | FunctionCallArgumentsDelta
    | FunctionCallDone
    | CodeInterpreterCallStarted
    | CodeInterpreterCallCodeDelta
    | CodeInterpreterCallCodeDone
    | CodeInterpreterCallInterpreting
    | CodeInterpreterCallCompleted
    | UsageFinal
)
