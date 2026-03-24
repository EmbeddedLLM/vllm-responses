# RFC-03-02 — Protocol Translation: Stream Paths, Failures & End-to-End Flow
> **Status:** Draft — open for community review
> **Part of:** RFC-03 (Protocol Translation & SSE Event Pipeline)
> **Previous:** [RFC-03-01 — Three-Stage Event Pipeline](RFC-03-01_protocol_translation_pipeline.md)
> **Next:** [RFC-04-00 — Built-in Tools: Code Interpreter](RFC-04-00_built_in_tools_code_interpreter.md)

---

## 10. Stream Paths and Failure Handling

### 10.1 Stream vs. Non-Stream Failure Behavior

The two request modes require different failure handling strategies:

```
┌──────────────────┬─────────────────────────────────────────────────────┐
│  Mode            │  Failure behavior                                   │
├──────────────────┼─────────────────────────────────────────────────────┤
│  Non-stream      │  Exception propagates out of the orchestrator.      │
│                  │  HTTP framework exception handler returns 4xx/5xx   │
│                  │  with a structured JSON error body.                 │
├──────────────────┼─────────────────────────────────────────────────────┤
│  Stream          │  Exception is caught inside the async generator.    │
│                  │  Emits  response.error  SSE event (structured)      │
│                  │  Emits  response.failed  (response.status="failed") │
│                  │  Appends  data: [DONE]                              │
│                  │  Client always gets a well-formed, closeable stream. │
└──────────────────┴─────────────────────────────────────────────────────┘
```

We suggest the stream path should not let an exception escape the generator. Once HTTP 200 has been sent, the only way to communicate failure to the client is through the event stream itself.

### 10.2 Failure Classification and Logging

We suggest a structured failure classification approach that, for any upstream error, extracts and logs:

- Error class and code
- Upstream HTTP status code (if applicable)
- Truncated error message (e.g. capped at 512 characters)
- How many tool call items were in flight at the time of failure
- How many MCP calls had failed within the request (hosted vs. request-remote)
- The raw upstream error body (truncated to a safe limit)

Log level should be based on error type: upstream 4xx responses suggest client error and warrant a WARNING; other failures warrant an ERROR.

The failure record should be assembled from counters that are updated on every intermediate event observed during the request, so the log always reflects a complete picture of what the orchestrator was doing at the moment of failure.

### 10.3 End-to-End Request Flow

```
  Client
    │  POST /v1/responses
    │  { model, input, tools, stream=true, previous_response_id? }
    ▼
  HTTP Routing Layer
    │  parse → validated request object
    │  await first event (so errors surface as HTTP errors, not SSE)
    ▼
  Request Orchestrator
    │  rehydrate history from store
    │  resolve tools → registered tool handles
    │  initialize Normalizer + Composer
    ▼
  Composer emits initial events:
    │  response.created
    │  response.in_progress
    ▼
  Upstream LLM call (Chat Completions streaming)
    │  tool loop: if LLM requests tool → execute → feed result back → continue
    ▼
  Normalizer processes each LLM framework event
    │  emits intermediate typed events
    ▼
  Composer processes each intermediate event
    │  emits typed SSE objects
    │  allocates item IDs, output indexes, sequence numbers
    ▼
  SSE Encoder
    │  serializes to "data: {...}\n\n" frames
    │  on terminal event: appends "data: [DONE]\n\n"
    ▼
  StreamingResponse → Client (real-time SSE frames)
    │
    │  (concurrently, via event interception)
    ▼
  Store write on response.completed / response.incomplete
    │  persist payload to database
    │  write-through to Redis cache (if enabled)
```
