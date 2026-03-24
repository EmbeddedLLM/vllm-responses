# RFC-04-00 — Built-in Tools: Code Interpreter

> **Status:** Draft — open for community review
> **Part of:** RFC-04 (Built-in Tools: Code Interpreter & Web Search)
> **Next:** [RFC-04-01 — Web Search](RFC-04-01_built_in_tools_web_search.md)
> **Component:** `tools/code_interpreter/`, `tools/runtime.py`, `tools/bootstrap.py`
> **Depends on:** RFC-01 (structure), RFC-03 (protocol translation — tools execute inside the request lifecycle)

---

## 1. What This RFC Covers

Built-in tools are executed by the gateway itself, mid-request, without the client doing anything beyond declaring `{"type": "code_interpreter"}` in their `tools` array.

This part covers:

- How built-in tools plug into the request lifecycle via pydantic-ai
- Code Interpreter: architecture, the Pyodide/Bun sidecar, execution model, worker pool
- The SSE events produced during code execution

Web Search is covered in [RFC-04-01](RFC-04-01_built_in_tools_web_search.md).

---

## 2. How Built-in Tools Fit Into the Request Lifecycle

Built-in tools are registered as pydantic-ai `Tool` functions at gateway startup. When the model requests one, pydantic-ai calls the function, waits for the result, and automatically feeds it back to vLLM to continue generation — all inside the same streaming request.

```
  vLLM generates tokens
        │
        │  model requests tool call
        ▼
  pydantic-ai tool loop
        │
        ├── tool == "code_interpreter"  ──►  run_code(code)
        │                                    POST http://localhost:{port}/python
        │                                    ◄── JSON result
        │
        ├── tool == "web_search"        ──►  run_web_search(action, query, url…)
        │                                    WebSearchExecutor.execute()
        │                                    ◄── JSON result
        │
        └── result fed back to vLLM
             vLLM continues generation
```

The gateway never exposes this loop to the client. From the client's perspective, a single `POST /v1/responses` produces a single coherent SSE stream that includes code execution events interleaved with text.

### Per-request tool state (`tools/runtime.py`)

Each request gets a `ToolRuntimeContext` injected via a Python context variable (`contextvars.ContextVar`). This gives every tool function access to the current request's config and web search state without any global mutable state.

```
ToolRuntimeContext
├── runtime_config      RuntimeConfig — gateway-wide settings
└── web_search          WebSearchRuntime | None — request-local search state
                        (None if web_search not enabled for this request)
```

---

## 3. Code Interpreter

### 3.1 Architecture

The Code Interpreter is a **sidecar process** — a separate HTTP server written in TypeScript (Bun + Pyodide) that the Python gateway calls over localhost. Python and TypeScript stay in separate processes; they communicate over a simple HTTP API.

```
┌─────────────────────────────────────────────────────────────────────┐
│  Gateway process (Python)                                           │
│                                                                     │
│  run_code(code: str)                                                │
│    │                                                                │
│    │  POST http://localhost:{port}/python                           │
│    │  { "code": "print(2+2)" }                                      │
│    │                                                                │
│    ▼                                                                │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Code Interpreter sidecar (TypeScript / Bun)                │   │
│  │                                                             │   │
│  │  GET  /health   →  { pyodide_loaded: true }                 │   │
│  │  POST /python   →  execute code in Pyodide                  │   │
│  │                                                             │   │
│  │  ┌──────────────────────────────────────────────────────┐  │   │
│  │  │  Pyodide (Python compiled to WebAssembly)            │  │   │
│  │  │  Runs inside the Bun process                         │  │   │
│  │  │  No host filesystem access                           │  │   │
│  │  │  No host network access (except httpx/requests)      │  │   │
│  │  └──────────────────────────────────────────────────────┘  │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 Sidecar Startup

The sidecar is started by the gateway supervisor before the first worker is ready to serve requests. Startup waits until `GET /health` returns `{ "pyodide_loaded": true }` — Pyodide initialization can take time on first run because it downloads the runtime (~400MB) and extracts it to a cache directory.

```
Startup sequence
│
├── supervisor spawns Code Interpreter sidecar process
│   (bundled native binary on Linux x86_64, or bun src/index.ts for dev)
│
├── poll GET /health every 1s until pyodide_loaded=true
│   (timeout: AS_CODE_INTERPRETER_STARTUP_TIMEOUT_S, default 120s)
│
└── gateway workers start accepting requests
```

**Binary selection order:**
```
1. Bundled native binary  tools/code_interpreter/bin/linux/x86_64/code-interpreter-server
   (present in Linux x86_64 wheels — Bun not required)

2. Bun fallback            bun src/index.ts
   (requires AS_CODE_INTERPRETER_DEV_BUN_FALLBACK=1 and Bun on PATH)
   (for source installs and non-Linux platforms)

3. Disabled               AS_CODE_INTERPRETER_MODE=disabled
   (skip tool entirely — requests with code_interpreter will error)
```

### 3.3 Execution Model

Every `POST /python` call runs code in Pyodide inside a WebAssembly sandbox. The response is a JSON object:

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
│  stdout           everything written to stdout (e.g. print())    │
│  stderr           everything written to stderr                   │
│  result           display value of the final expression, or null │
│                   (only the last expression, not intermediates)  │
│  execution_time_ms  wall time in milliseconds                    │
└──────────────────────────────────────────────────────────────────┘
```

On exception: `status="exception"`, `result` contains the exception text, `stdout`/`stderr` reflect output produced before the failure.

**Pre-loaded packages available to every execution:**
```
Data science:     numpy, pandas, matplotlib, scikit-image
HTTP:             requests, httpx, aiohttp
Image processing: Pillow, opencv-python
Data formats:     beautifulsoup4, pyyaml, orjson
Math & symbolic:  sympy, tiktoken
```

### 3.4 Worker Pool

By default the sidecar runs in single-threaded mode — one execution at a time. For higher throughput, a worker pool can be configured:

```
┌────────────────────────────────────────────────────────────────┐
│  Single-threaded (default)                                     │
│                                                                │
│  POST /python ──► PyodideManager ──► Pyodide WASM              │
│                   (one execution at a time)                    │
├────────────────────────────────────────────────────────────────┤
│  Worker pool  (--code-interpreter-workers N, N ≥ 2)           │
│                                                                │
│  POST /python ──► WorkerPool                                   │
│                   ├── Worker 1 ──► own Pyodide WASM instance   │
│                   ├── Worker 2 ──► own Pyodide WASM instance   │
│                   └── Worker N ──► own Pyodide WASM instance   │
│                                                                │
│  ⚠ Each worker loads its own Pyodide runtime.                 │
│    Higher worker count = more RAM + longer startup.            │
└────────────────────────────────────────────────────────────────┘
```

Workers are Bun Workers (experimental). `N=1` enables worker mode but does not increase throughput — use `N≥2` for actual parallelism.

### 3.5 SSE Events Produced

When the model calls code_interpreter, the Normalizer (RFC-03-01) translates pydantic-ai tool events into these NormalizedEvents, which the Composer then turns into SSE events the client sees:

```
1. CodeInterpreterCallStarted      →  response.code_interpreter_call.in_progress
2. CodeInterpreterCallCodeDelta    →  response.code_interpreter_call_code.delta
   (one per chunk as model writes code)
3. CodeInterpreterCallCodeDone     →  response.code_interpreter_call_code.done
4. CodeInterpreterCallInterpreting →  response.code_interpreter_call.interpreting
   (code is done, sidecar is now executing)
5. CodeInterpreterCallCompleted    →  response.code_interpreter_call.completed
                                      response.output_item.done
```

The `outputs` field on `output_item.done` is populated only when the client sets `include=["code_interpreter_call.outputs"]`:

```json
"outputs": [
  { "type": "logs", "logs": "4\n" },
  { "type": "logs", "logs": "The answer is 4" }
]
```

---

## 4. Tool Registration at Startup

Both tools register themselves as pydantic-ai functions via `tools/bootstrap.py` at gateway startup:

```
bootstrap.py
├── register_code_interpreter_tool()   →  run_code registered as "code_interpreter"
└── register_web_search_tool()         →  run_web_search registered as "web_search"
```

Registration binds the Python function to the tool name constant (`tools/ids.py`). At request time, `as_run_settings()` in `types/openai.py` checks which tools the client declared and constructs the pydantic-ai toolsets accordingly — only registered tools that the client explicitly requested are included in the Agent.

---

## 5. Full Code Interpreter Execution Flow

```
  Client: tools=[{"type": "code_interpreter"}], stream=true
          │
          ▼
  LMEngine._build_response_pipeline()
          │  resolves tool → pydantic-ai FunctionToolset
          │  creates ToolRuntimeContext
          ▼
  pydantic-ai Agent streams vLLM tokens
          │
          │  model emits tool_call: code_interpreter(code="2+2")
          ▼
  Normalizer: PartStartEvent(ToolCallPart)
          │  → CodeInterpreterCallStarted
          │  → CodeInterpreterCallCodeDelta (per chunk)
          │  → CodeInterpreterCallCodeDone
          ▼
  Composer: emits SSE →  response.code_interpreter_call.in_progress
                         response.code_interpreter_call_code.delta  (×N)
                         response.code_interpreter_call_code.done
          │
          ▼
  pydantic-ai: FunctionToolCallEvent (about to execute)
          │
  Normalizer: → CodeInterpreterCallInterpreting
  Composer:   → response.code_interpreter_call.interpreting
          │
          ▼
  run_code("2+2") called
          │  POST http://localhost:{port}/python  {"code": "2+2"}
          │  ◄── {"status":"success","stdout":"","result":"4","execution_time_ms":8}
          ▼
  pydantic-ai: FunctionToolResultEvent
  Normalizer: → CodeInterpreterCallCompleted(stdout=None, result="4")
  Composer:   → response.code_interpreter_call.completed
              → response.output_item.done (with outputs if include= set)
          │
          ▼
  pydantic-ai feeds result back to vLLM
  vLLM continues generating text
          │
          ▼
  response.output_text.delta (×N) → response.completed
```

---

## 6. Open Questions for Community Review

**Q1 — TypeScript sidecar inside a Python package**
The Code Interpreter server (`src/*.ts`) ships as TypeScript source inside the Python wheel. On Linux x86_64 it is compiled to a native binary. On other platforms it requires Bun. Should this live in a separate repository and be fetched as a binary artifact only at build time, keeping the Python package pure Python?

**Q2 — Code Interpreter isolation**
Pyodide runs in WebAssembly, which prevents direct host filesystem access. However, HTTP requests are allowed (httpx, requests are pre-loaded). Is this the right security boundary for MVP, or should network access be restricted by default?

**Q3 — Worker pool stability**
The Code Interpreter worker pool uses Bun Workers, which are marked experimental in Bun. Should the worker pool be considered MVP-stable, or documented as experimental with a recommendation to use single-threaded mode in production?
