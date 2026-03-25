# RFC-03-01 — Protocol Translation: Three-Stage Event Pipeline
> **Status:** Draft — open for community review
> **Part of:** RFC-03 (Protocol Translation & SSE Event Pipeline)
> **Previous:** [RFC-03-00 — Problem & Request Orchestration](RFC-03-00_protocol_translation_problem_and_lmengine.md)
> **Next:** [RFC-03-02 — Stream Paths, Failures & End-to-End Flow](RFC-03-02_protocol_translation_stream_and_failures.md)

---

## 9. The Three-Stage Event Pipeline

### 9.1 Design Rationale

We propose a three-stage pipeline with a framework-neutral intermediate representation sitting between the LLM framework layer and the wire-format layer. Neither end knows about the other:

```
  LLM framework events           Internal representation           Wire format
  (framework-specific)           (framework-neutral)              (Responses API)
        │                                │                               │
        │   framework events      ────►  │  intermediate events  ────►  │  SSE events
        │                                │  (typed value objects)        │  "data:{...}\n\n"
        │                                │                               │
   Normalizer                   Internal event types              Composer + SSE Encoder
   (Stage 1)                    (Stage 2 boundary)                (Stages 2 & 3)
```

The benefit of this design is that swapping the LLM framework (Stage 1) or the wire format (Stage 3) does not require changing the other. We acknowledge this adds abstraction cost and welcome feedback on whether a simpler two-stage approach would be preferable for the MVP.

### 9.2 Stage 1 — Normalizer

The Normalizer translates framework-specific streaming events into intermediate typed events. Under this proposal, it would be the primary component that knows about the LLM framework internals.

**GPT-OSS / Harmony note.** Under the proposed D1 decision (gateway calls `POST /v1/responses` on vLLM), the Normalizer is already model-agnostic: vLLM handles all Harmony-specific rendering and parsing before events reach the gateway, so the Normalizer receives standard Responses API output items regardless of whether the upstream model is GPT-OSS or a Jinja-based model. If a future decision moves below `POST /v1/responses` (e.g. toward an internal vLLM protocol as proposed in Q4 of ADR-01), a GPT-OSS-aware Normalizer branch would be needed to handle Harmony `channel` and `recipient` fields directly. The event categories in the table below already cover the full range of GPT-OSS output types — reasoning, function calls, web search, and MCP calls are all represented.

It should handle at minimum the following event categories:

| Source event type | Intermediate event emitted |
|-------------------|---------------------------|
| Text token started | Message started |
| Text token delta | Message delta |
| Text token done | Message done |
| Reasoning token started | Reasoning started |
| Reasoning token delta | Reasoning delta |
| Reasoning token done | Reasoning done |
| Tool call started (built-in: code interpreter) | Code interpreter call started |
| Tool call delta (code content) | Code interpreter code delta |
| Tool call done (code content) | Code interpreter code done |
| Tool execution begun | Code interpreter interpreting |
| Tool result received (code interpreter) | Code interpreter call completed |
| Tool call started (web search) | Web search call started |
| Tool execution begun (web search) | Web search searching |
| Tool result received (web search) | Web search call completed |
| Tool call started (MCP) | MCP call started |
| Tool call delta (MCP arguments) | MCP call arguments delta |
| Tool call done (MCP arguments) | MCP call arguments done |
| Tool result received (MCP, success) | MCP call completed |
| Tool result received (MCP, error) | MCP call failed |
| User-defined function call started | Function call started |
| User-defined function call delta | Function call arguments delta |
| User-defined function call done | Function call done |
| Run completed | Usage final |

**Special case — code argument streaming.** Upstream LLMs typically send code arguments inside a JSON fragment (e.g. `{"code": "print(1)"}`). The Responses API spec expects raw code text as the delta. The Normalizer should extract the string value of the code key incrementally from the byte stream without buffering the full JSON, enabling real-time code streaming.

### 9.3 Stage 2 — Composer

The Composer translates intermediate events into typed Responses API SSE objects. Under this proposal, it would be the primary component that knows the Responses API wire format.

Per request, the Composer maintains:

- A monotonically increasing sequence number, allocated per SSE event emitted.
- A monotonically increasing output index, one per output item added.
- An accumulation map from item key to in-progress item state (text, code, arguments).
- An ordered list of completed output items, assembled into `response.output` at completion.

**Item IDs.** We propose that each output item receive a unique stable ID of the form `{type_prefix}{uuid}`. Using a type-specific prefix makes IDs human-readable and self-describing. For example, message output items might use one prefix, reasoning items another, code interpreter call items another, and so on. The exact prefix values are an implementation detail.

The full mapping from intermediate events to SSE event types is:

```
Intermediate Event               →   SSE event type(s) emitted
──────────────────────────────────────────────────────────────────────────────
MessageStarted                   →   response.output_item.added
                                     response.content_part.added
MessageDelta                     →   response.output_text.delta
MessageDone                      →   response.output_text.done
                                     response.content_part.done
                                     response.output_item.done

ReasoningStarted                 →   response.output_item.added
                                     response.output_item.done  (emitted early
                                       for spec parity — item marked done before
                                       reasoning deltas stream)
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
                                     (or response.incomplete if stopped early)
```

### 9.4 Stage 3 — SSE Encoder

The SSE Encoder serializes typed SSE objects to raw text frames for the HTTP response body:

```
Typed SSE object
      │
      │  serialize to JSON
      ▼
"data: {json}\n\n"
      │
      ▼  (on response.completed or response.failed)
"data: [DONE]\n\n"
```

**Defensive terminal marker.** If the upstream stream ends without emitting `response.completed` or `response.failed`, we suggest still appending `[DONE]` so clients are not left with a hanging stream.

**Spec note.** The real OpenAI Responses API does not emit `[DONE]`. We propose emitting it for [OpenResponses spec](https://www.openresponses.org/specification) compliance. This is a deliberate divergence and should be documented as such. We welcome feedback on whether this should be configurable.

---

## Open Questions

The following questions are left explicitly open for community discussion.

**On protocol translation:**

10. **Two-stage vs. three-stage pipeline.** Is the intermediate representation worth the abstraction cost for the MVP? A simpler two-stage approach would produce SSE objects directly from LLM framework events. We would love to hear from the community on this.
11. **Reasoning item lifecycle.** The spec emits `response.output_item.done` for reasoning before reasoning deltas stream (the item is marked done early). Should we replicate this behavior for parity, or wait until reasoning is truly complete? Your input here would be especially valuable.
12. **`[DONE]` configurability.** Should the `[DONE]` terminal marker be configurable per-request or per-deployment? We would love to hear from the community on this.
13. **First-chunk await overhead.** Awaiting the first chunk before returning the `StreamingResponse` adds latency to every streaming request. Is this trade-off acceptable?
14. **GPT-OSS and the internal protocol question.** If ADR-01 Q4 (Tun Jian's proposed vLLM Agentic Protocol) is adopted and the gateway moves below `POST /v1/responses`, the Normalizer would need a GPT-OSS-aware branch to handle Harmony `channel` and `recipient` semantics directly. Should the Normalizer interface be designed now to make that extension point obvious, even if the GPT-OSS branch is not implemented for the MVP?
