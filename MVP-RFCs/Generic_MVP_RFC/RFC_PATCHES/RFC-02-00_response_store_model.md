# RFC-02-00 — ResponseStore: Storage Model & Rehydration
> **Status:** Draft — open for community review
> **Part of:** RFC-02 (ResponseStore: Conversation Memory)
> **Previous:** [RFC-01 — Project Structure](RFC-01_project_structure.md)
> **Next:** [RFC-02-01 — DB Schema, Engine Factory & Cache](RFC-02-01_response_store_infrastructure.md)

---

## 6. Stateful Conversation Memory

### 6.1 The Problem

Without statefulness, clients must send their entire conversation history on every request. The Responses API solves this with a single field: `previous_response_id`. The gateway would need to store enough state after each response to fully reconstruct the conversation for the next turn.

```
Turn 1:  client sends { input: "My name is Alice." }
         gateway returns { id: "resp_abc123", output: [...] }

Turn 2:  client sends { previous_response_id: "resp_abc123",
                        input: "What is my name?" }
         gateway reconstructs full history internally
         client sends ONLY the new message
```

### 6.2 What Gets Stored

We propose persisting a payload containing everything needed to continue the conversation. For the MVP, we suggest storing at minimum:

```
Stored Payload
├── schema_version         integer — governs backward compatibility
├── full input history     the complete message list sent to the upstream LLM
├── model output           all output items produced (text, tool calls, reasoning)
├── active tools           tools that were enabled for this turn
├── tool choice            the tool_choice setting active in this turn
└── system instructions    any system prompt active in this turn
```

Tools, tool choice, and system instructions are stored alongside the response so the next turn can inherit them if the client omits them.

### 6.3 The Rehydration Rule

When `previous_response_id` is present, we propose assembling the full history using:

```
full_history_for_llm =
    stored.full_input_history        ← everything the LLM saw in the previous turn
  + stored.model_output              ← what the LLM produced in the previous turn
  + new_input                        ← what the client is sending now
```

Because each stored `full_input_history` already contains all prior turns flattened, a lookup is always a single database read — there is no recursive chain traversal at request time.

```
┌─────────────────────────────────────────────────────────────────┐
│  Turn 1                                                         │
│  new_input:            ["Hi, I'm Alice"]                        │
│  sent to LLM:          ["Hi, I'm Alice"]                        │
│  stored input history: ["Hi, I'm Alice"]                        │
│  stored output:        ["Hello Alice!"]                         │
├─────────────────────────────────────────────────────────────────┤
│  Turn 2  (previous_response_id = turn 1)                        │
│  new_input:   ["What is my name?"]                              │
│  sent to LLM: ["Hi, I'm Alice",                                 │
│                "Hello Alice!",          ← from stored output    │
│                "What is my name?"]      ← new input             │
│  stored input history: [all 3 messages]                         │
│  stored output:        ["Your name is Alice."]                  │
├─────────────────────────────────────────────────────────────────┤
│  Turn 3  (previous_response_id = turn 2)                        │
│  new_input:   ["Say it again"]                                  │
│  sent to LLM: ["Hi, I'm Alice",                                 │
│                "Hello Alice!",                                   │
│                "What is my name?",                               │
│                "Your name is Alice.",   ← from stored output    │
│                "Say it again"]          ← new input             │
└─────────────────────────────────────────────────────────────────┘
```

### 6.4 What Is and Is Not Stored

```
┌──────────────────────────────────────────────┬──────────┐
│  Condition                                   │  Stored? │
├──────────────────────────────────────────────┼──────────┤
│  status = "completed"  AND  store = true     │  Yes     │
│  status = "incomplete" AND  store = true     │  Yes     │
│  status = "failed"                           │  No      │
│  store = false  (any status)                 │  No      │
└──────────────────────────────────────────────┴──────────┘
```

`store=false` is a client-controlled opt-out. We suggest that failed responses not be stored — a client would then be unable to chain off a response that errored. We are open to feedback on this policy.

### 6.5 Full Read/Write Flow

```
POST /v1/responses
    │
    ▼
Request Orchestrator
    │
    ├── 1. Load stored payload from store (if previous_response_id present)
    │        Redis (if enabled) → Database
    │
    ├── 2. Assemble full history using rehydration rule
    │
    ├── 3. Send to upstream LLM (Responses API)
    │        stream delta chunks through the event pipeline
    │
    ├── 4. Stream translated SSE events to client (real-time)
    │
    └── 5. On response.completed: persist payload to store
             Database write + Redis write-through (if enabled)
```

Steps 4 and 5 happen concurrently in streaming mode — the store write is triggered by intercepting the terminal event in the same async generator that is being streamed to the client.

---

## Open Questions

The following questions are left explicitly open for community discussion.

**On stateful conversation memory:**

6. **Single-table vs. normalized schema.** All state lives in a single JSON column. This keeps the schema stable but makes it hard to query individual fields. Should we add indexed columns for common query fields in the MVP? We would love to hear from the community on this.
7. **TTL enforcement.** An `expires_at` column is proposed but there is no cleanup job specified. Should the MVP include a background TTL enforcement task, or explicitly defer this? Your input here would be especially valuable.
8. **`store=false` default.** Should `store=true` require an explicit opt-in to protect user privacy, or should storing be the default with an opt-out? We would love to hear from the community on this.
9. **Redis requirement.** Should Redis be recommended as a production requirement above a certain worker count threshold?
