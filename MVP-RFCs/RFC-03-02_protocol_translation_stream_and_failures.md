# RFC-03-02 — Protocol Translation: Stream Paths, Failures & End-to-End Flow

> **Status:** Draft — open for community review
> **Part of:** RFC-03 (Protocol Translation & SSE Event Pipeline)
> **Previous:** [RFC-03-01 — Three-Stage Pipeline & NormalizedEvent](RFC-03-01_protocol_translation_pipeline.md)
> **Component:** `lm.py`, `lm_failures.py`, `responses_core/sse.py`

---

## 1. What This RFC Covers

- How stream and non-stream request paths diverge
- How failures are handled without breaking a client's open stream
- `lm_failures.py` — structured failure classification and logging
- The complete end-to-end request flow

---

## 2. Stream vs. Non-Stream Paths

The two paths share the same pipeline internals. They differ only in how failures surface.

```
┌──────────────────┬─────────────────────────────────────────────────────┐
│  Mode            │  Failure behaviour                                  │
├──────────────────┼─────────────────────────────────────────────────────┤
│  Non-stream      │  Exception propagates out of LMEngine               │
│                  │  FastAPI exception handler returns HTTP 4xx/5xx     │
│                  │  with a JSON error body                             │
├──────────────────┼─────────────────────────────────────────────────────┤
│  Stream          │  Exception is caught inside the async generator     │
│                  │  Emits  response.error  SSE event (structured)      │
│                  │  Emits  response.failed  (response.status="failed") │
│                  │  Appends  data: [DONE]                              │
│                  │  Client always gets a well-formed, closeable stream  │
└──────────────────┴─────────────────────────────────────────────────────┘
```

The stream path never lets an exception escape the generator. This is intentional — HTTP 200 has already been sent when streaming starts, so the only way to communicate failure to the client is through the event stream itself.

---

## 3. Failure Classification and Logging (`lm_failures.py`)

When an upstream failure occurs, `lm_failures.py` provides structured diagnostics before the error is surfaced.

```
extract_failure_details()
  │  Parses ModelHTTPError or UnexpectedModelBehavior
  │  Extracts: error_class, code, message, param,
  │            upstream_status_code, upstream_error_raw
  │
classify_failure_log_level()
  │  HTTP 4xx from upstream  →  WARNING  (client error, expected)
  │  Everything else         →  ERROR
  │
log_failure_summary()
     Structured log with:
     - request_id, failure_phase (stream / non_stream)
     - upstream_status_code, error_message (truncated to 512 chars)
     - tool_call_parts_seen (how many tool calls were in flight)
     - mcp_failed_count_hosted, mcp_failed_count_request_remote
     - last_failed_mcp_signature (server_label + tool_name)
     - upstream_error_raw (truncated to 2048 chars)
     - full pydantic-ai message dump (only if AS_LOG_MODEL_MESSAGES=true)
```

`FailureCounters` is updated on every `NormalizedEvent` observed during the request. This means the failure log always includes a complete picture of what the model was doing at the time of failure, not just the error itself.

---

## 4. How a Request Flows End to End

```
  Client
    │  POST /v1/responses
    │  { model, input, tools, stream=true, previous_response_id? }
    ▼
  routers/serving.py
    │  parse → vLLMResponsesRequest
    │  await first chunk (so errors surface as HTTP errors, not SSE)
    ▼
  LMEngine._build_response_pipeline()
    │  rehydrate history from ResponseStore (RFC-02)
    │  resolve tools → pydantic-ai toolsets
    │  build PydanticAINormalizer + ResponseComposer
    ▼
  composer.start()
    │  emits: response.created
    │          response.in_progress
    ▼
  pydantic-ai Agent.run_stream_events()
    │  calls vLLM POST /v1/chat/completions (streaming)
    │  tool loop: if model requests tool → execute → feed result back → continue
    ▼
  PydanticAINormalizer.on_event()   [for each pydantic-ai event]
    │  emits NormalizedEvents
    ▼
  ResponseComposer.feed()           [for each NormalizedEvent]
    │  emits typed OpenAI SSE objects
    │  allocates item IDs, output indexes, sequence numbers
    ▼
  stream_responses_sse()
    │  serialises to "data: {...}\n\n" frames
    │  on terminal event: appends "data: [DONE]\n\n"
    ▼
  StreamingResponse → Client (real-time SSE frames)
    │
    │  (concurrently, via _tap_events)
    ▼
  ResponseStore.put_completed()     [on response.completed / response.incomplete]
     persist StoredResponsePayload to DB
     write-through to Redis cache (if enabled)
```

---

## 5. Open Questions for Community Review

**Q1 — pydantic-ai as the LLM framework**
`LMEngine` is tightly coupled to pydantic-ai's `Agent.run_stream_events()`. This gives tool loop handling and streaming for free, but makes the framework non-swappable. Should `LMEngine` be refactored behind an abstract `LLMBackend` interface to allow alternative backends (e.g. raw httpx, LangChain) without changing the pipeline?

**Q2 — Three-stage pipeline vs. two-stage**
The Normalizer → NormalizedEvent → Composer indirection means neither end knows about the other. An alternative two-stage approach would compose SSE events directly from pydantic-ai events — simpler but tightly coupled to pydantic-ai. Is the three-stage design worth the abstraction cost for the MVP?

**Q3 — Reasoning output item lifecycle**
OpenAI emits `response.output_item.done` for reasoning *before* reasoning deltas stream (the item is marked done early). We replicate this behaviour for parity. Is this the right choice, or should we wait until reasoning is complete before marking the item done?

**Q4 — `[DONE]` marker**
We emit `data: [DONE]\n\n` for OpenResponses spec compliance even though real OpenAI Responses streams do not. Should this be configurable per-request or per-deployment?

**Q5 — First-chunk await in the router**
We await the first SSE chunk before returning the `StreamingResponse` so that validation errors surface as proper HTTP errors. The downside is a small latency overhead on every streaming request. Is this trade-off acceptable, or should we handle it differently (e.g. a dedicated validation pass before streaming begins)?
