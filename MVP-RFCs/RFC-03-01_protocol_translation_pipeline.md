# RFC-03-01 — Protocol Translation: Three-Stage Pipeline & NormalizedEvent

> **Status:** Draft — open for community review
> **Part of:** RFC-03 (Protocol Translation & SSE Event Pipeline)
> **Previous:** [RFC-03-00 — Problem, Entry Point & LMEngine](RFC-03-00_protocol_translation_problem_and_lmengine.md)
> **Next:** [RFC-03-02 — Stream Paths, Failures & End-to-End Flow](RFC-03-02_protocol_translation_stream_and_failures.md)
> **Component:** `responses_core/normalizer.py`, `responses_core/composer.py`, `responses_core/sse.py`, `responses_core/models.py`

---

## 1. What This RFC Covers

The architectural centrepiece of the gateway — how pydantic-ai events become OpenAI SSE frames:

- The three-stage event pipeline design and why it has a middle layer
- Stage 1: Normalizer — pydantic-ai events → NormalizedEvents
- Stage 2: Composer — NormalizedEvents → OpenAI SSE objects
- Stage 3: SSE Encoder — SSE objects → raw text frames
- The complete NormalizedEvent type system (all 22 types)

---

## 2. The Three-Stage Event Pipeline

The key design decision: a **framework-neutral internal representation** (`NormalizedEvent`) sits between the LLM framework layer and the OpenAI spec layer. Neither side knows about the other.

```
  pydantic-ai                 agentic_stack                 OpenAI spec
  (framework)                 (internal)                    (wire format)
      │                           │                              │
      │   PartStartEvent          │                              │
      │   PartDeltaEvent    ────► │  NormalizedEvent      ────► │  SSE event
      │   PartEndEvent            │  (frozen dataclass)          │  "data: {...}\n\n"
      │   FunctionToolCallEvent   │                              │
      │   FunctionToolResultEvent │                              │
      │   AgentRunResultEvent     │                              │
      │                           │                              │
  Normalizer                  models.py                    Composer + SSE encoder
  normalizer.py                                            composer.py + sse.py
```

---

## 3. Stage 1 — Normalizer (`responses_core/normalizer.py`)

Translates pydantic-ai events into `NormalizedEvent` dataclasses. It is the only component that knows about pydantic-ai internals.

```
pydantic-ai event                  →   NormalizedEvent emitted
─────────────────────────────────────────────────────────────────────
PartStartEvent(TextPart)           →   MessageStarted
PartDeltaEvent(TextPartDelta)      →   MessageDelta
PartEndEvent(TextPart)             →   MessageDone

PartStartEvent(ThinkingPart)       →   ReasoningStarted
PartDeltaEvent(ThinkingPartDelta)  →   ReasoningDelta
PartEndEvent(ThinkingPart)         →   ReasoningDone

PartStartEvent(ToolCallPart)
  tool_name in mcp_tool_name_map   →   McpCallStarted
  tool_name == web_search          →   WebSearchCallStarted
  tool_name == code_interpreter    →   CodeInterpreterCallStarted
  tool_name == user function       →   FunctionCallStarted

PartDeltaEvent(ToolCallPartDelta)
  kind == code_interpreter_call    →   CodeInterpreterCallCodeDelta
  kind == mcp_call                 →   McpCallArgumentsDelta
  kind == web_search_call          →   (suppressed — no delta events)
  kind == function_call            →   FunctionCallArgumentsDelta

PartEndEvent(ToolCallPart)
  kind == code_interpreter_call    →   CodeInterpreterCallCodeDone
  kind == mcp_call                 →   McpCallArgumentsDone
  kind == web_search_call          →   (suppressed)
  kind == function_call            →   FunctionCallDone

FunctionToolCallEvent
  tool == code_interpreter         →   CodeInterpreterCallInterpreting
  tool == web_search               →   WebSearchCallSearching

FunctionToolResultEvent
  tool == code_interpreter         →   CodeInterpreterCallCompleted
  tool == web_search               →   WebSearchCallCompleted
  tool == mcp                      →   McpCallCompleted  or  McpCallFailed

AgentRunResultEvent                →   UsageFinal
```

**Special case — code streaming:**
vLLM sends code inside a JSON fragment: `{"code": "print(1)"}`. The Responses API spec wants raw code text as the delta, not JSON. The Normalizer uses `_CodeJsonArgsExtractor` — a stateful streaming parser that extracts the string value of the `"code"` key incrementally as byte chunks arrive, without buffering the full JSON. This is necessary for real-time code streaming.

---

## 4. Stage 2 — Composer (`responses_core/composer.py`)

Translates `NormalizedEvent` stream into typed OpenAI SSE objects. It is the only component that knows the OpenAI Responses API spec.

For each event it manages:

```
┌──────────────────────────────────────────────────────────────────────┐
│  Composer state per request                                          │
├──────────────────────────────────────────────────────────────────────┤
│  _sequence_number    monotonically increasing int, allocated per     │
│                      SSE event emitted                               │
│  _next_output_index  monotonically increasing int, one per output    │
│                      item added                                      │
│  _items              dict[item_key → _ItemState]                     │
│                      accumulates text, code, args across deltas      │
│  _output_items       ordered list of completed output items          │
│                      assembled into response.output at completion    │
└──────────────────────────────────────────────────────────────────────┘
```

**Item IDs** are UUID v7 strings with a type prefix so they are human-readable and time-sortable:

```
msg_   →  message output items
rs_    →  reasoning output items
fc_    →  function call output items
ci_    →  code interpreter call output items
ws_    →  web search call output items
mcp_   →  mcp call output items
cntr_  →  code interpreter container IDs
```

Full NormalizedEvent → SSE event mapping:

```
NormalizedEvent                  →   SSE event type(s) emitted
──────────────────────────────────────────────────────────────────────────────
MessageStarted                   →   response.output_item.added
                                     response.content_part.added
MessageDelta                     →   response.output_text.delta
MessageDone                      →   response.output_text.done
                                     response.content_part.done
                                     response.output_item.done

ReasoningStarted                 →   response.output_item.added
                                     response.output_item.done  ← emitted early
                                       (OpenAI parity: reasoning item is "done"
                                        before reasoning deltas stream)
ReasoningDelta                   →   response.reasoning.delta
ReasoningDone                    →   response.reasoning.done

FunctionCallStarted              →   response.output_item.added
FunctionCallArgumentsDelta       →   response.function_call_arguments.delta
FunctionCallDone                 →   response.function_call_arguments.done
                                     response.output_item.done

CodeInterpreterCallStarted       →   response.output_item.added
                                     response.code_interpreter_call.in_progress
CodeInterpreterCallCodeDelta     →   response.code_interpreter_call_code.delta
CodeInterpreterCallCodeDone      →   response.code_interpreter_call_code.done
CodeInterpreterCallInterpreting  →   response.code_interpreter_call.interpreting
CodeInterpreterCallCompleted     →   response.code_interpreter_call.completed
                                     response.output_item.done

WebSearchCallStarted             →   response.output_item.added
                                     response.web_search_call.in_progress
WebSearchCallSearching           →   response.web_search_call.searching
WebSearchCallCompleted           →   response.web_search_call.completed
                                     response.output_item.done

McpCallStarted                   →   response.output_item.added
                                     response.mcp_call.in_progress
McpCallArgumentsDelta            →   response.mcp_call_arguments.delta
McpCallArgumentsDone             →   response.mcp_call_arguments.done
McpCallCompleted                 →   response.mcp_call.completed
                                     response.output_item.done
McpCallFailed                    →   response.mcp_call.failed
                                     response.output_item.done  (status: "failed")

UsageFinal                       →   response.completed
                                     (or response.incomplete if max tokens / filter)
```

---

## 5. Stage 3 — SSE Encoder (`responses_core/sse.py`)

Serialises typed SSE objects to raw text frames for the HTTP response body.

```
typed SSE object
      │
      │  .as_responses_chunk()
      ▼
"data: {json}\n\n"
      │
      ▼  (on response.completed or response.failed)
"data: [DONE]\n\n"
```

**Defensive terminal marker:** if the upstream stream ends without ever emitting `response.completed` or `response.failed`, `[DONE]` is still appended. Clients are never left hanging.

> **Spec divergence note:** Real OpenAI Responses streams do not emit `[DONE]`. We emit it for [OpenResponses spec](https://www.openresponses.org/specification) conformance. This is a deliberate choice, documented as the single source of truth in `sse.py`.

---

## 6. The NormalizedEvent Type System

All 22 event types are frozen dataclasses in `responses_core/models.py`. Every type carries `item_key` — a string that tracks which output item the event belongs to across the full pipeline.

```
NormalizedEvent
│
├── Message
│   ├── MessageStarted          item_key
│   ├── MessageDelta            item_key · delta: str
│   └── MessageDone             item_key · text: str
│
├── Reasoning
│   ├── ReasoningStarted        item_key
│   ├── ReasoningDelta          item_key · delta: str
│   └── ReasoningDone           item_key · text: str
│
├── FunctionCall  (user tools — gateway does not execute these)
│   ├── FunctionCallStarted     item_key · call_id · name · initial_arguments_json
│   ├── FunctionCallArgumentsDelta  item_key · delta: str
│   └── FunctionCallDone        item_key · arguments_json: str
│
├── CodeInterpreterCall  (gateway executes — see RFC-04)
│   ├── CodeInterpreterCallStarted      item_key · initial_code: str|None
│   ├── CodeInterpreterCallCodeDelta    item_key · delta: str
│   ├── CodeInterpreterCallCodeDone     item_key · code: str|None
│   ├── CodeInterpreterCallInterpreting item_key
│   └── CodeInterpreterCallCompleted    item_key · stdout · stderr · result
│
├── WebSearchCall  (gateway executes — see RFC-04)
│   ├── WebSearchCallStarted    item_key
│   ├── WebSearchCallSearching  item_key
│   └── WebSearchCallCompleted  item_key · action_type · query · queries
│                               · sources · url · pattern
│
├── McpCall  (gateway executes — see RFC-05)
│   ├── McpCallStarted          item_key · server_label · name
│   │                           · initial_arguments_json · mode
│   ├── McpCallArgumentsDelta   item_key · delta: str
│   ├── McpCallArgumentsDone    item_key · arguments_json: str
│   ├── McpCallCompleted        item_key · output_text: str
│   └── McpCallFailed           item_key · error_text: str
│
└── UsageFinal
        input_tokens · output_tokens · total_tokens
        cache_read_tokens · cache_write_tokens · reasoning_tokens
        incomplete_reason: "max_output_tokens" | "content_filter" | None
```
