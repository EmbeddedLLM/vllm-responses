# RFC-03-00 — Protocol Translation: Problem, Entry Point & LMEngine

> **Status:** Draft — open for community review
> **Part of:** RFC-03 (Protocol Translation & SSE Event Pipeline)
> **Next:** [RFC-03-01 — Three-Stage Pipeline & NormalizedEvent](RFC-03-01_protocol_translation_pipeline.md)
> **Component:** `routers/serving.py`, `lm.py`
> **Depends on:** RFC-01 (structure), RFC-02 (ResponseStore — used at request start and end)

---

## 1. What This RFC Covers

This part answers: **how does a `POST /v1/responses` request arrive and get orchestrated?**

Specifically:
- The fundamental translation problem: Chat Completions vs Responses API
- How the HTTP layer receives and dispatches requests
- How `LMEngine` orchestrates one full request lifecycle

The three-stage event pipeline and NormalizedEvent type system are in [RFC-03-01](RFC-03-01_protocol_translation_pipeline.md). Stream paths, failure handling, and the end-to-end flow are in [RFC-03-02](RFC-03-02_protocol_translation_stream_and_failures.md).

---

## 2. The Translation Problem

vLLM speaks **Chat Completions**. Clients want **Responses API**. These are fundamentally different:

```
┌─────────────────────────────┬──────────────────────────────────────┐
│  Chat Completions           │  Responses API                       │
├─────────────────────────────┼──────────────────────────────────────┤
│  Stateless — client sends   │  Stateful — client sends only new    │
│  full history every time    │  input + previous_response_id        │
├─────────────────────────────┼──────────────────────────────────────┤
│  delta chunks, finish_reason│  Typed SSE events with sequence      │
│  No stable item IDs         │  numbers and stable item IDs         │
├─────────────────────────────┼──────────────────────────────────────┤
│  Tool calls: function name  │  Tool calls: typed output items      │
│  + JSON args as one chunk   │  with lifecycle events (started,     │
│                             │  delta, done, interpreting…)         │
├─────────────────────────────┼──────────────────────────────────────┤
│  Reasoning: no standard     │  Reasoning: dedicated output item    │
│  representation             │  with delta/done events              │
└─────────────────────────────┴──────────────────────────────────────┘
```

The gateway bridges this gap entirely in software, without touching vLLM.

---

## 3. Request Entry Point

```
routers/serving.py
│
├── POST /v1/responses      → create_model_response()
└── GET  /v1/responses/{id} → retrieve_model_response()
```

`create_model_response()` does two things and nothing more:
1. Validates the request body into a typed `vLLMResponsesRequest` Pydantic model
2. Hands off to `LMEngine` and streams the result back as `text/event-stream`

The first chunk is awaited *before* the `StreamingResponse` is returned. This ensures that any immediate validation errors (e.g. bad `previous_response_id`) surface as HTTP errors rather than being buried inside a 200 SSE stream.

---

## 4. LMEngine — Request Lifecycle Orchestrator

`lm.py` contains `LMEngine`. One instance is created per request. It owns the full lifecycle from request receipt to response persistence.

```
LMEngine.__init__()
  │  Creates a pydantic-ai Agent bound to the request's model
  │  Injects vLLM as the OpenAI-compatible provider
  │
LMEngine.run()
  │
  ├── stream=True  → _run_stream()
  │     │  async generator: yields raw SSE text frames
  │     │  wraps _tap_events(_iter_responses_events_stream())
  │     │
  └── stream=False → _run()
        │  awaits full response object
        │  wraps _iter_responses_events_non_stream()
```

### `_build_response_pipeline()`

Called once at the start of every request. Assembles all context needed to process the request:

```
_build_response_pipeline()
│
├── 1. rehydrate_request()          ← ResponseStore (RFC-02)
│      assemble hydrated_input from previous_response_id chain
│
├── 2. as_run_settings()            ← resolve tools
│      built-in tools (code_interpreter, web_search)
│      MCP tools (hosted + remote)
│      pydantic-ai message_history, instructions, toolsets, usage_limits
│
├── 3. ToolRuntimeContext           ← per-request tool state
│      web search runtime (request-local page cache)
│      runtime_config reference
│
├── 4. PydanticAINormalizer         ← Stage 1 of pipeline
│      knows which tool names are built-ins vs MCP vs user-defined
│
└── 5. ResponseComposer             ← Stage 2 of pipeline
       seeded with the initial response object
```
