# RFC-02-00 — ResponseStore: Storage Model & Rehydration

> **Status:** Draft — open for community review
> **Part of:** RFC-02 (ResponseStore: Conversation Memory)
> **Next:** [RFC-02-01 — DB, Engine Factory & Redis Cache](RFC-02-01_response_store_infrastructure.md)
> **Component:** `responses_core/store.py`
> **Depends on:** RFC-01 (project structure)
> **Referenced by:** RFC-03 (protocol translation uses the store at request start and end)

---

## 1. What This RFC Covers

How `agentic-stack` remembers conversations across requests.

The Responses API lets a client send only its *new* message and a `previous_response_id` — the gateway reconstructs the full conversation history from storage. This part covers:

- The `previous_response_id` contract and rehydration rule
- What exactly gets stored (`StoredResponsePayload`)
- Which responses are stored and which are not
- The full read/write flow

The DB schema, engine factory, and Redis cache are covered in [RFC-02-01](RFC-02-01_response_store_infrastructure.md).

---

## 2. The Problem This Solves

Standard Chat Completions requires the client to send the **entire conversation history** with every request. As conversations grow, this becomes expensive in bandwidth and client complexity.

The Responses API solves this with a single field:

```
Turn 1:  client sends { input: "My name is Alice." }
         gateway returns { id: "resp_abc123", output: [...] }

Turn 2:  client sends { previous_response_id: "resp_abc123",
                        input: "What is my name?" }
         gateway reconstructs full history internally
         client sends ONLY the new message
```

The gateway must store enough state after Turn 1 to fully reconstruct what the model needs to see for Turn 2.

---

## 3. What Gets Stored

After a request completes, the gateway persists a `StoredResponsePayload` — a single JSON blob containing everything needed to continue the conversation:

```
StoredResponsePayload
├── schema_version         integer — governs backward compatibility
├── hydrated_input         list[Message] — the full input history sent to the model
├── response               OpenAIResponsesResponse — the full response object
│                          (includes all output items: messages, tool calls, reasoning)
├── effective_tools        list[Tool] | None — tools active in this turn
├── effective_tool_choice  ToolChoice — tool_choice active in this turn
└── effective_instructions str | None — system instructions active in this turn
```

Tools and tool_choice are stored alongside the response so the next turn can inherit them if the client omits them.

---

## 4. The Rehydration Rule

When `previous_response_id` is present, the gateway assembles the full history using this rule:

```
hydrated_input =
    stored.hydrated_input          ← everything the model saw in the previous turn
  + stored.response.output         ← what the model produced in the previous turn
  + new_input                      ← what the client is sending now
```

This is applied recursively across any chain of `previous_response_id` links, because each stored `hydrated_input` already contains all prior turns flattened. A lookup is always a single DB read — there is no recursive chain traversal at request time.

```
┌─────────────────────────────────────────────────────────────────┐
│  Turn 1                                                         │
│  new_input:   ["Hi, I'm Alice"]                                 │
│  sent to vLLM: ["Hi, I'm Alice"]                                │
│  stored hydrated_input: ["Hi, I'm Alice"]                       │
│  stored response.output: ["Hello Alice!"]                       │
├─────────────────────────────────────────────────────────────────┤
│  Turn 2  (previous_response_id = turn 1)                        │
│  new_input:   ["What is my name?"]                              │
│  sent to vLLM: ["Hi, I'm Alice",                                │
│                 "Hello Alice!",          ← from stored output   │
│                 "What is my name?"]      ← new input            │
│  stored hydrated_input: [all 3 messages]                        │
│  stored response.output: ["Your name is Alice."]               │
├─────────────────────────────────────────────────────────────────┤
│  Turn 3  (previous_response_id = turn 2)                        │
│  new_input:   ["Say it again"]                                  │
│  sent to vLLM: ["Hi, I'm Alice",                                │
│                 "Hello Alice!",                                  │
│                 "What is my name?",                              │
│                 "Your name is Alice.",   ← from stored output   │
│                 "Say it again"]          ← new input            │
└─────────────────────────────────────────────────────────────────┘
```

---

## 5. What Is and Is Not Stored

Not every response is persisted. Two conditions must both be true:

```
┌──────────────────────────────────────────────┬──────────┐
│  Condition                                   │  Stored? │
├──────────────────────────────────────────────┼──────────┤
│  status = "completed"  AND  store = true     │  ✓ Yes   │
│  status = "incomplete" AND  store = true     │  ✓ Yes   │
│  status = "failed"                           │  ✗ No    │
│  store = false  (any status)                 │  ✗ No    │
└──────────────────────────────────────────────┴──────────┘
```

`store=false` is a client-controlled opt-out. `failed` responses are never continuation anchors — a client cannot chain off a response that errored.

---

## 6. Full Read/Write Flow

```
                    ┌────────────────────────────────┐
  POST /v1/responses│         LMEngine               │
  ─────────────────►│  _build_response_pipeline()    │
                    │                                │
                    │  1. rehydrate_request()         │──► ResponseStore.get()
                    │     assemble hydrated_input    │    Redis → DB
                    │                                │
                    │  2. run pydantic-ai agent      │──► vLLM /v1/chat/completions
                    │     stream events through      │
                    │     Normalizer → Composer      │
                    │                                │
                    │  3. stream SSE to client       │──► Client
                    │                                │
                    │  4. put_completed()            │──► ResponseStore.put()
                    │     persist state_json         │    DB + Redis write-through
                    └────────────────────────────────┘
```

Steps 3 and 4 happen concurrently in stream mode — the gateway writes to the store via `_tap_events()`, which intercepts the `response.completed` event in the same async generator that is being streamed to the client.
