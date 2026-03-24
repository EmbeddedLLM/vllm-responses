# RFC-03-00 — Protocol Translation: Problem & Request Orchestration
> **Status:** Draft — open for community review
> **Part of:** RFC-03 (Protocol Translation & SSE Event Pipeline)
> **Previous:** [RFC-02-01 — DB Schema, Engine Factory & Cache](RFC-02-01_response_store_infrastructure.md)
> **Next:** [RFC-03-01 — Three-Stage Event Pipeline](RFC-03-01_protocol_translation_pipeline.md)

---

## 8. Protocol Translation

### 8.1 The Translation Problem

The upstream LLM server produces a raw streaming response format. Clients expect the richer Responses API. These differ in several fundamental ways:

```
┌─────────────────────────────┬──────────────────────────────────────┐
│  Upstream (raw streaming)   │  Responses API                       │
├─────────────────────────────┼──────────────────────────────────────┤
│  Stateless — client sends   │  Stateful — client sends only new    │
│  full history every time    │  input + previous_response_id        │
├─────────────────────────────┼──────────────────────────────────────┤
│  Delta chunks, finish_reason│  Typed SSE events with sequence      │
│  No stable item IDs         │  numbers and stable item IDs         │
├─────────────────────────────┼──────────────────────────────────────┤
│  Tool calls: function name  │  Tool calls: typed output items      │
│  + JSON args as one chunk   │  with lifecycle events (started,     │
│                             │  delta, done, interpreting, ...)     │
├─────────────────────────────┼──────────────────────────────────────┤
│  Reasoning: no standard     │  Reasoning: dedicated output item    │
│  representation             │  with delta/done events              │
└─────────────────────────────┴──────────────────────────────────────┘
```

The gateway would bridge this gap entirely in software, without touching the upstream server.

### 8.2 HTTP Layer

We propose two routes:

```
POST /v1/responses      — create a new response (stream or non-stream)
GET  /v1/responses/{id} — retrieve a previously stored response
```

For the `POST` route, one approach would be to await the first event before opening the HTTP streaming response. This would help ensure that immediate validation errors (e.g. unknown `previous_response_id`) surface as proper HTTP 4xx responses rather than being buried inside a 200 SSE stream. We are open to feedback on whether there are better approaches.

### 8.3 Request Orchestrator

The orchestrator is the central coordinator for one full request lifecycle. It should:

1. Rehydrate conversation history from the store (Section 6).
2. Resolve which tools are active for this request (built-in tools, MCP tools, user-defined function tools).
3. Invoke the upstream LLM with the assembled history.
4. Feed upstream events through the three-stage event pipeline (Section 9).
5. Coordinate tool execution mid-stream when the model requests a tool.
6. Persist the completed response to the store.

One instance of the orchestrator would be created per request. Ideally, it would hold no mutable state that outlives the request.
