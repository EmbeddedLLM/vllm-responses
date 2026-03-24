# RFC-B — Built-in Tools: Code Interpreter & Web Search

> **Status:** Draft — open for community feedback
> **Covers:** How built-in tools fit into the request lifecycle, the code interpreter sidecar, and the web search profile/adapter pattern
> **Previous:** [RFC-A — System Architecture & Stateful Conversation Memory](RFC-A_architecture_and_statefulness.md)
> **Next:** [RFC-C — MCP Integration, Config, and Observability](RFC-C_mcp_config_observability.md)

---

## Table of Contents

1. [Overview](#1-overview)
2. [How Built-in Tools Fit Into the Request Lifecycle](#2-how-built-in-tools-fit-into-the-request-lifecycle)
3. [Per-Request Tool State](#3-per-request-tool-state)
4. [Code Interpreter](#4-code-interpreter)
5. [Web Search](#5-web-search)
6. [Open Questions](#6-open-questions)

---

## 1. Overview

Built-in tools are executed entirely by the gateway itself, mid-request, without the client needing to do anything beyond declaring a tool type in their request. From the client's perspective, a single `POST /v1/responses` produces a coherent SSE stream that includes tool execution events interleaved with text — the underlying tool execution loop is invisible.

For the MVP we propose two built-in tools:

- **Code Interpreter:** executes Python code in a sandboxed WebAssembly runtime.
- **Web Search:** retrieves web content via configurable search backends.

This RFC describes both tools and how they integrate with the request lifecycle described in RFC-A.

---

## 2. How Built-in Tools Fit Into the Request Lifecycle

Built-in tools are registered at gateway startup as callable functions under well-known tool names. When the upstream LLM requests one of these tools, the orchestration layer calls the corresponding function, waits for the result, and feeds it back to the LLM to continue generation — all inside the same streaming request.

```
  LLM generates tokens
        │
        │  model requests tool call
        ▼
  Tool dispatch loop (inside orchestrator)
        │
        ├── tool == "code_interpreter"  ──►  execute code in sidecar
        │                                    ◄── result
        │
        ├── tool == "web_search"        ──►  execute search/fetch action
        │                                    ◄── result
        │
        └── result fed back to LLM
             LLM continues generation
```

The gateway never exposes this loop to the client. From the client's perspective, a single request produces a single coherent SSE stream that happens to include code execution and web search events.

Built-in tools are registered only when the client explicitly declares them in the `tools` array. If a client does not request a built-in tool, it is not included in the LLM's available tools for that request.

---

## 3. Per-Request Tool State

Each request should receive an isolated tool state container, injected so that every tool function can access the current request's configuration and per-request state without any global mutable state.

For the MVP, this container needs to hold at minimum:

- Gateway-wide configuration settings (read-only reference).
- The web search runtime state for this request (page cache, adapter reference) — `None` if web search is not enabled for this request.

One approach is a context variable (analogous to a thread-local) that the orchestrator sets at the start of each request and tool functions read during execution. We welcome suggestions on whether a dependency injection approach would be more testable.

---

## 4. Code Interpreter

### 4.1 Architecture

We propose a **sidecar process** architecture for the code interpreter. A separate HTTP server handles code execution; the main gateway process calls it over localhost. This keeps the language runtimes isolated and allows the sidecar to be managed, restarted, and monitored independently.

For the MVP, we suggest TypeScript with Bun as the sidecar runtime, embedding Python execution via Pyodide (Python compiled to WebAssembly). This gives a sandboxed Python environment without requiring a Python subprocess or container.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Gateway process                                                      │
│                                                                       │
│  execute_code(code: str)                                              │
│    │                                                                  │
│    │  POST http://localhost:{port}/python                             │
│    │  { "code": "..." }                                               │
│    │                                                                  │
│    ▼                                                                  │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │  Code Interpreter sidecar                                    │    │
│  │                                                              │    │
│  │  GET  /health   →  { ready: true }                           │    │
│  │  POST /python   →  execute code in WebAssembly sandbox       │    │
│  │                                                              │    │
│  │  ┌───────────────────────────────────────────────────────┐  │    │
│  │  │  Python runtime (WebAssembly)                         │  │    │
│  │  │  Isolated from host filesystem                        │  │    │
│  │  │  Limited network access (see Open Questions)          │  │    │
│  │  └───────────────────────────────────────────────────────┘  │    │
│  └──────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

### 4.2 Startup and Health Check

We propose the following startup sequence:

1. The supervisor process spawns the code interpreter sidecar before any gateway workers begin accepting requests.
2. The gateway polls `GET /health` on the sidecar at regular intervals until a ready response is returned.
3. Gateway workers only begin serving requests after the sidecar is healthy.

The initial startup may take time if the WebAssembly runtime must be downloaded and initialized on first run. We suggest a configurable startup timeout (defaulting to several minutes to accommodate slow environments) and clear error messaging if the timeout is exceeded.

**Binary selection strategy.** For ease of deployment, we suggest shipping a pre-compiled native binary of the sidecar for common platforms (e.g. Linux x86_64). For development or unsupported platforms, a fallback to running the TypeScript source directly with Bun should be available. A configuration option should allow disabling the code interpreter entirely.

```
Binary selection order (proposed):

1. Pre-compiled native binary  (present for supported platforms)

2. TypeScript source via Bun   (dev fallback — requires Bun on PATH,
                                opt-in via configuration)

3. Disabled                    (via configuration — requests declaring
                                code_interpreter will receive an error)
```

### 4.3 Execution Model

Every code execution request runs Python code in the WebAssembly sandbox. We propose the following response format:

```json
{
  "status": "success",
  "stdout": "4\n",
  "stderr": "",
  "result": null,
  "execution_time_ms": 12
}
```

```
┌──────────────────────────────────────────────────────────────────┐
│  Response fields                                                 │
├──────────────────────────────────────────────────────────────────┤
│  status           "success" | "exception"                        │
│  stdout           everything written to stdout                   │
│  stderr           everything written to stderr                   │
│  result           display value of the final expression, or null │
│                   (last expression only, not intermediates)      │
│  execution_time_ms  wall time in milliseconds                    │
└──────────────────────────────────────────────────────────────────┘
```

On exception: `status="exception"`, `result` contains the exception text, and `stdout`/`stderr` reflect output produced before the failure.

We suggest pre-loading a set of commonly used scientific and data-processing packages so they are available without explicit installation. The exact set is an implementation decision, but common candidates include numeric computing, data manipulation, visualization, HTTP, and image processing libraries.

### 4.4 Worker Pool

By default we suggest the sidecar runs in single-threaded mode — one execution at a time. For higher concurrency, a worker pool can be configured where each worker holds its own independent WebAssembly runtime instance.

```
┌────────────────────────────────────────────────────────────────┐
│  Single-threaded (default)                                     │
│                                                                │
│  POST /python ──► Single runtime instance                      │
│                   (one execution at a time)                    │
├────────────────────────────────────────────────────────────────┤
│  Worker pool  (N workers, N ≥ 2)                               │
│                                                                │
│  POST /python ──► Worker pool                                  │
│                   ├── Worker 1 ──► own runtime instance        │
│                   ├── Worker 2 ──► own runtime instance        │
│                   └── Worker N ──► own runtime instance        │
│                                                                │
│  Note: each worker loads its own runtime independently.        │
│  Higher worker count = more memory + longer startup.           │
└────────────────────────────────────────────────────────────────┘
```

We consider the worker pool an advanced feature and suggest documenting it as experimental for the MVP, given that multi-threaded WebAssembly worker support in Bun is itself still maturing.

### 4.5 SSE Events Produced

When the model calls the code interpreter, the event pipeline (described in RFC-A §9) produces the following SSE event sequence for the client:

```
1. response.code_interpreter_call.in_progress
   (code interpreter output item created, execution about to begin)

2. response.code_interpreter_call_code.delta  (one per token chunk)
   (model is writing the code — streamed in real time)

3. response.code_interpreter_call_code.done
   (full code string assembled)

4. response.code_interpreter_call.interpreting
   (code is finalized, sidecar is now executing it)

5. response.code_interpreter_call.completed
   response.output_item.done
   (execution complete, outputs available if requested)
```

The `outputs` field on `output_item.done` is populated only when the client explicitly requests it via an `include` parameter. This avoids bloating the event stream by default.

### 4.6 Full Code Interpreter Execution Flow

```
  Client: tools=[{"type": "code_interpreter"}], stream=true
          │
          ▼
  Request Orchestrator
          │  registers code interpreter as an available tool
          │  creates per-request tool state container
          ▼
  LLM streams tokens
          │
          │  LLM emits tool_call: code_interpreter(code="2+2")
          ▼
  Normalizer: tool call events
          │  → CodeInterpreterCallStarted
          │  → CodeInterpreterCallCodeDelta (per token chunk)
          │  → CodeInterpreterCallCodeDone
          │
  Composer: emits SSE events
          │  response.code_interpreter_call.in_progress
          │  response.code_interpreter_call_code.delta  (×N)
          │  response.code_interpreter_call_code.done
          ▼
  Normalizer: tool execution begun
          │  → CodeInterpreterCallInterpreting
  Composer:  response.code_interpreter_call.interpreting
          │
          ▼
  execute_code("2+2") called
          │  POST http://localhost:{port}/python  {"code": "2+2"}
          │  ◄── {"status":"success","stdout":"","result":"4","execution_time_ms":8}
          ▼
  Normalizer: tool result received
          │  → CodeInterpreterCallCompleted(result="4")
  Composer:  response.code_interpreter_call.completed
             response.output_item.done
          │
          ▼
  Tool result fed back to LLM
  LLM continues generating text
          │
          ▼
  response.output_text.delta (×N) → response.completed
```

---

## 5. Web Search

### 5.1 Architecture

Web search uses a **profile + adapter** design. A profile is selected at gateway startup and determines which backend handles each action type. The client always uses the same public tool declaration (`{"type": "web_search"}`) — the profile is invisible to them.

```
┌────────────────────────────────────────────────────────────────────┐
│  Web Search Architecture                                           │
│                                                                    │
│  Client declares:  tools=[{"type": "web_search"}]                  │
│                                                                    │
│  At startup: profile selected from configuration                   │
│                                                                    │
│  Profile maps each action type to an adapter:                      │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Profile A                                                   │  │
│  │    search    → Adapter A1 (e.g. a search-engine MCP server)  │  │
│  │    open_page → Adapter A2 (e.g. a fetch MCP server)          │  │
│  ├──────────────────────────────────────────────────────────────┤  │
│  │  Profile B                                                   │  │
│  │    search    → Adapter B1 (e.g. DuckDuckGo direct)           │  │
│  │    open_page → Adapter B2 (e.g. a fetch MCP server)          │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
```

This design means the community can add new search backends by implementing an adapter without changing the core tool logic. The profile system ensures operators can switch backends with a configuration change and without code changes.

### 5.2 Three Action Types

The web search tool exposes three action types to the model. The model decides which to use based on context.

```
┌──────────────────┬────────────────────────────────────────────────┐
│  Action          │  What it does                                  │
├──────────────────┼────────────────────────────────────────────────┤
│  search          │  Query a search engine.                        │
│                  │  Input: query string + optional query list.     │
│                  │  Returns: list of sources (url, title, snippet) │
├──────────────────┼────────────────────────────────────────────────┤
│  open_page       │  Fetch and extract text from a URL.            │
│                  │  Input: url.                                   │
│                  │  Returns: page title + full text content.       │
│                  │  Side effect: stores page in request-local cache│
├──────────────────┼────────────────────────────────────────────────┤
│  find_in_page    │  Search text inside a previously opened page.  │
│                  │  Input: url + search pattern.                  │
│                  │  Returns: matches with surrounding context.     │
│                  │  Reads from request cache — no network call.   │
└──────────────────┴────────────────────────────────────────────────┘
```

### 5.3 The Request-Local Page Cache

`open_page` stores fetched page content in a request-local cache. This cache lives only for the duration of a single request and is discarded after the response is complete. `find_in_page` reads from this cache — it will fail if called on a URL that was never opened in the same request, which prevents stale data from leaking across requests.

```
  Request starts
      │
      │  model calls open_page(url="https://example.com")
      ▼
  Web Search Executor
      │  fetches page content, extracts text
      │  stores in request-local page cache
      │    key: canonicalized URL
      │    value: { url, title, text }
      ▼
  model calls find_in_page(url="https://example.com", pattern="RFC")
      │
      ▼
  Web Search Executor
      │  reads from page cache (no network call)
      │  case-insensitive text search
      │  returns matches with surrounding context window
      ▼
  Request ends → cache discarded
```

### 5.4 Adapter Interface

Each adapter handles one action type. Adapters should declare their runtime requirements (e.g. a specific MCP server that must be available) so the gateway can provision and validate them at startup.

We propose a minimal adapter interface:

```
SearchAdapter
    execute_search(context, query, queries, options) → action outcome
    supported_hints: which request options this adapter respects

OpenPageAdapter
    execute_open_page(context, url, options) → action outcome
```

The `supported_hints` mechanism allows adapters to declare which search options they actually respect (e.g. user location, result count). If the client provides an option that the active adapter does not support, the gateway can log a warning rather than silently ignoring it.

### 5.5 SSE Events Produced

When the model calls web search, the event pipeline produces:

```
1. response.web_search_call.in_progress
   (web search output item created)

2. response.web_search_call.searching
   (gateway is executing the action against the backend)

3. response.web_search_call.completed
   response.output_item.done
   (action complete, results incorporated into tool result fed back to LLM)
```

The action type (`search`, `open_page`, or `find_in_page`) and action-specific data (query, sources, URL, pattern) are included in `output_item.done`. Search sources are only included in the response when the client explicitly requests them via an `include` parameter, to avoid bloating the event stream by default.

### 5.6 End-to-End Web Search Flow

```
  Client: tools=[{"type": "web_search"}], stream=true
          │
          ▼
  Request Orchestrator
          │  registers web search as available tool
          │  creates per-request tool state (includes page cache)
          ▼
  LLM streams tokens
          │
          │  LLM emits tool_call: web_search(action="search", query="...")
          ▼
  Normalizer: tool call started → WebSearchCallStarted
  Composer: response.web_search_call.in_progress
          │
          ▼
  Normalizer: tool execution begun → WebSearchCallSearching
  Composer: response.web_search_call.searching
          │
          ▼
  Web Search Executor
          │  routes to configured adapter for "search" action
          │  adapter queries backend, returns sources
          ▼
  Normalizer: tool result received → WebSearchCallCompleted
  Composer: response.web_search_call.completed
            response.output_item.done
          │
          ▼
  Tool result fed back to LLM (sources as JSON)
  LLM incorporates results, continues generating text
          │
          ▼
  response.output_text.delta (×N) → response.completed
```

---

## 6. Open Questions

The following questions are left explicitly open for community discussion.

**On the code interpreter:**

1. **Sidecar language.** We propose TypeScript/Bun as the sidecar runtime. Is this acceptable to the community, or is there a strong preference for a different language or approach? Alternatives could include a Python subprocess with restricted globals, a WASM runner in a different language, or a container-based approach.

2. **Sidecar shipping.** Shipping a pre-compiled binary inside a Python package is convenient but unusual. Should the sidecar be a separate installable artifact, fetched at install time or at first use?

3. **Sandbox network access.** The WebAssembly sandbox prevents direct host filesystem access but the proposal allows HTTP requests from within executed code. Is this the right security boundary for the MVP, or should outbound network access from user code be restricted by default?

4. **Worker pool stability.** Multi-threaded WebAssembly worker support in Bun is experimental. Should the worker pool be documented as experimental with a recommendation to use single-threaded mode in production?

5. **Pre-loaded packages.** What set of packages should be pre-loaded in the WebAssembly Python environment? What is the right process for the community to propose additions or removals?

**On web search:**

6. **Profile selection scope.** The web search profile is selected once at startup. Should profiles be selectable per-request, or is startup-time selection the right model for the MVP?

7. **Page cache scope.** The page cache is currently per-request. If a multi-turn conversation opens a page in turn 1 and tries to search within it in turn 2, it will fail. Should the page cache be extended to persist across turns in the response store?

8. **Adapter ecosystem.** Should adapters be a first-class extension point with a plugin registry, or is a fixed set of bundled adapters sufficient for the MVP?

**On both tools:**

9. **Tool execution timeout.** Should there be a configurable per-tool execution timeout? What should the default be, and how should a timeout surface to the client?

10. **Error visibility.** When a tool execution fails, should the error be exposed to the LLM (allowing it to describe the failure to the user), or should it surface as a gateway error that terminates the request?

---

**Previous:** [RFC-A — System Architecture & Stateful Conversation Memory](RFC-A_architecture_and_statefulness.md)
**Next:** [RFC-C — MCP Integration, Config, and Observability](RFC-C_mcp_config_observability.md)
