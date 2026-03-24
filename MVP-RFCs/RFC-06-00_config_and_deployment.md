# RFC-06-00 — Config & Infrastructure: RuntimeConfig, Deployment & Environment Variables

> **Status:** Draft — open for community review
> **Part of:** RFC-06 (Config & Infrastructure Reference)
> **Next:** [RFC-06-01 — Observability: Metrics, Tracing & Logging](RFC-06-01_observability.md)
> **Component:** `configs/runtime.py`, `configs/defaults.py`, `configs/builders.py`, `entrypoints/`
> **Depends on:** RFC-01 (project structure)

---

## 1. What This RFC Covers

- `RuntimeConfig`: the single frozen dataclass that holds all tunables
- Deployment modes and how config is built for each
- Full environment variable reference

Prometheus metrics, OpenTelemetry tracing, logging, and the startup health check are in [RFC-06-01](RFC-06-01_observability.md).

---

## 2. RuntimeConfig — The Single Source of Truth

All configuration for a running gateway lives in one frozen dataclass: `RuntimeConfig` (`configs/runtime.py`). It is constructed once at startup, passed to every subsystem, and never mutated.

```
RuntimeConfig (frozen dataclass)
│
├── Deployment
│   ├── runtime_mode          "standalone" | "supervisor" | "integrated" | "mock_llm"
│   ├── gateway_host          Bind host (default: 0.0.0.0)
│   ├── gateway_port          Bind port (default: 5969)
│   ├── gateway_workers       Gunicorn worker count (default: 1)
│   └── gateway_max_concurrency  Request concurrency limit (default: 300)
│
├── Upstream
│   ├── llm_api_base          vLLM /v1 base URL (required)
│   └── openai_api_key        Bearer token for upstream auth (optional)
│
├── Tools
│   ├── web_search_profile    "exa_mcp" | "duckduckgo_plus_fetch" | None
│   ├── code_interpreter_mode "spawn" | "external" | "disabled"
│   ├── code_interpreter_port Sidecar HTTP port (default: 5970)
│   ├── code_interpreter_workers  Worker pool size (0 = single-threaded)
│   ├── pyodide_cache_dir     Pyodide package cache path
│   ├── code_interpreter_dev_bun_fallback  Use system bun when bundled binary absent
│   └── code_interpreter_startup_timeout_s  (default: 600s)
│
├── MCP
│   ├── mcp_config_path       Path to hosted MCP config JSON (optional)
│   ├── mcp_builtin_runtime_url  Internal MCP sidecar URL (auto-set when config present)
│   ├── mcp_request_remote_enabled    (default: true)
│   ├── mcp_request_remote_url_checks (default: true)
│   ├── mcp_hosted_startup_timeout_sec  (default: 10s)
│   └── mcp_hosted_tool_timeout_sec     (default: 60s)
│
├── Store (see RFC-02)
│   ├── db_path               SQLAlchemy URL (default: sqlite+aiosqlite:///vllm_responses.db)
│   ├── redis_host            (default: localhost)
│   ├── redis_port            (default: 6379)
│   ├── response_store_cache  Enable Redis hot cache (default: false)
│   └── response_store_cache_ttl_seconds  (default: 3600)
│
├── Observability
│   ├── metrics_enabled       Prometheus metrics (default: true)
│   ├── metrics_path          Scrape endpoint path (default: /metrics)
│   ├── log_timings           Log per-request latency breakdown (default: false)
│   ├── log_model_messages    Log model input/output messages (default: false)
│   ├── tracing_enabled       OpenTelemetry tracing (default: false)
│   ├── otel_service_name     (default: "vllm-responses")
│   ├── tracing_sample_ratio  0.0–1.0 (default: 0.01)
│   ├── opentelemetry_host    OTLP collector host (default: otel-collector)
│   └── opentelemetry_port    OTLP gRPC port (default: 4317)
│
└── Misc
    ├── log_dir               Log file directory (default: "logs")
    ├── upstream_ready_timeout_s   Health-check wait at startup (default: 1800s)
    ├── upstream_ready_interval_s  Health-check poll interval (default: 5s)
    └── internal_upstream_header_name  Header for integrated-mode loopback bypass
```

`RuntimeConfig` is **immutable** — it uses `frozen=True, slots=True`. Subsystems receive it as a constructor argument; there are no global singletons for configuration.

---

## 3. Deployment Modes

`agentic-stack` supports four runtime modes. The mode is set internally by the entrypoint — operators do not set `runtime_mode` directly.

```
┌─────────────────┬──────────────────────────────────────────────────────────┐
│  Mode           │  Description                                             │
├─────────────────┼──────────────────────────────────────────────────────────┤
│  standalone     │  Gateway only. Operator points --upstream at an existing │
│                 │  vLLM instance. No vLLM process management.              │
│                 │  Config built from env vars only.                        │
├─────────────────┼──────────────────────────────────────────────────────────┤
│  supervisor     │  `agentic-stack serve` — supervisor process launches     │
│                 │  Gunicorn workers + optional code interpreter + optional  │
│                 │  MCP sidecar. Config built from CLI args + env vars.      │
├─────────────────┼──────────────────────────────────────────────────────────┤
│  integrated     │  `agentic-stack vllm serve --responses` — gateway runs   │
│                 │  inside the vLLM process. Port and host inherited from    │
│                 │  vLLM's own CLI. gateway_workers always 1.               │
│                 │  llm_api_base is always http://127.0.0.1:{port}/v1.      │
├─────────────────┼──────────────────────────────────────────────────────────┤
│  mock_llm       │  Testing only. Behaves like standalone but uses a mock   │
│                 │  LLM backend instead of a real vLLM instance.            │
└─────────────────┴──────────────────────────────────────────────────────────┘
```

### 3.1 Config Construction Path Per Mode

```
standalone / mock_llm
    └── build_runtime_config_for_standalone()
            └── reads env vars via EnvSource
            └── delegates to build_common_runtime_config()

supervisor  (agentic-stack serve ...)
    └── build_runtime_config_for_supervisor()
            ├── parses CLI args (argparse Namespace)
            ├── reads env vars via EnvSource
            └── delegates to build_common_runtime_config()

integrated  (agentic-stack vllm serve --responses)
    └── build_runtime_config_for_integrated()
            ├── receives values from vLLM's parsed CLI args
            ├── reads env vars via EnvSource
            └── delegates to build_common_runtime_config()
```

All three paths converge at `build_common_runtime_config()`, which resolves every remaining field from env vars and applies defaults.

### 3.2 Config Precedence

```
CLI args  →  highest priority  (supervisor + integrated modes only)
Env vars  →  second
Defaults  →  lowest (from RuntimeDefaults dataclass in configs/defaults.py)
```

There is no config file for the gateway itself — only the MCP server config (`AS_MCP_CONFIG_PATH`) uses a file.

---

## 4. Environment Variable Reference

All environment variables use the `AS_` prefix (for `agentic-stack`). Variables inherited from the old `VR_` prefix are accepted during the migration period.

### 4.1 Core Gateway

```
┌───────────────────────────┬─────────────────────────────────┬──────────────────────────┐
│  Variable                 │  Description                    │  Default                 │
├───────────────────────────┼─────────────────────────────────┼──────────────────────────┤
│  AS_HOST                  │  Bind host                      │  0.0.0.0                 │
│  AS_PORT                  │  Bind port                      │  5969                    │
│  AS_WORKERS               │  Gunicorn worker processes      │  1                       │
│  AS_MAX_CONCURRENCY       │  Concurrent request limit       │  300                     │
│  AS_LOG_DIR               │  Log file directory             │  logs                    │
│  AS_LLM_API_BASE          │  vLLM /v1 base URL              │  http://localhost:8080/v1│
│  AS_OPENAI_API_KEY        │  Upstream bearer token          │  (none)                  │
└───────────────────────────┴─────────────────────────────────┴──────────────────────────┘
```

### 4.2 Code Interpreter

```
┌──────────────────────────────────────────┬──────────────────────────────────────┬──────────────┐
│  Variable                                │  Description                         │  Default     │
├──────────────────────────────────────────┼──────────────────────────────────────┼──────────────┤
│  AS_CODE_INTERPRETER_MODE                │  spawn | external | disabled         │  spawn       │
│  AS_CODE_INTERPRETER_PORT                │  Sidecar HTTP port                   │  5970        │
│  AS_CODE_INTERPRETER_WORKERS             │  Worker pool size (0 = single)       │  0           │
│  AS_CODE_INTERPRETER_STARTUP_TIMEOUT     │  Startup wait in seconds             │  600         │
│  AS_PYODIDE_CACHE_DIR                    │  Pyodide package cache               │  ~/.cache/…  │
│  AS_CODE_INTERPRETER_DEV_BUN_FALLBACK    │  Use system bun if no bundled binary │  false       │
└──────────────────────────────────────────┴──────────────────────────────────────┴──────────────┘
```

`spawn` — gateway manages the sidecar process lifecycle.
`external` — sidecar is pre-started externally; gateway connects to `AS_CODE_INTERPRETER_PORT`.
`disabled` — code interpreter tool is unavailable.

### 4.3 Web Search

```
┌───────────────────────────┬──────────────────────────────────────────┬──────────────┐
│  Variable                 │  Description                             │  Default     │
├───────────────────────────┼──────────────────────────────────────────┼──────────────┤
│  AS_WEB_SEARCH_PROFILE    │  exa_mcp | duckduckgo_plus_fetch | (off) │  (none)      │
└───────────────────────────┴──────────────────────────────────────────┴──────────────┘
```

Web search is disabled when `AS_WEB_SEARCH_PROFILE` is unset. Setting it to `exa_mcp` also auto-enables the MCP built-in runtime.

### 4.4 MCP

```
┌────────────────────────────────────────┬─────────────────────────────────────────┬──────────┐
│  Variable                              │  Description                            │  Default │
├────────────────────────────────────────┼─────────────────────────────────────────┼──────────┤
│  AS_MCP_CONFIG_PATH                    │  Path to hosted MCP config JSON         │  (none)  │
│  AS_MCP_BUILTIN_RUNTIME_URL            │  Override MCP sidecar URL               │  auto    │
│  AS_MCP_REQUEST_REMOTE_ENABLED         │  Allow client-supplied server_url       │  true    │
│  AS_MCP_REQUEST_REMOTE_URL_CHECKS      │  Enforce SSRF URL policy                │  true    │
│  AS_MCP_HOSTED_STARTUP_TIMEOUT_SEC     │  Per-server startup timeout             │  10      │
│  AS_MCP_HOSTED_TOOL_TIMEOUT_SEC        │  Per-tool call timeout                  │  60      │
└────────────────────────────────────────┴─────────────────────────────────────────┴──────────┘
```

### 4.5 Response Store

```
┌──────────────────────────────────────┬──────────────────────────────────────────┬──────────────────────────────────┐
│  Variable                            │  Description                             │  Default                         │
├──────────────────────────────────────┼──────────────────────────────────────────┼──────────────────────────────────┤
│  AS_DB_PATH                          │  SQLAlchemy DB URL                       │  sqlite+aiosqlite:///as_store.db  │
│  AS_REDIS_HOST                       │  Redis host                              │  localhost                       │
│  AS_REDIS_PORT                       │  Redis port                              │  6379                            │
│  AS_RESPONSE_STORE_CACHE             │  Enable Redis hot cache                  │  false                           │
│  AS_RESPONSE_STORE_CACHE_TTL_SECONDS │  Cache entry TTL                         │  3600                            │
└──────────────────────────────────────┴──────────────────────────────────────────┴──────────────────────────────────┘
```

### 4.6 Observability

```
┌────────────────────────────────┬──────────────────────────────────────────────┬──────────────────┐
│  Variable                      │  Description                                 │  Default         │
├────────────────────────────────┼──────────────────────────────────────────────┼──────────────────┤
│  AS_METRICS_ENABLED            │  Enable Prometheus metrics endpoint          │  true            │
│  AS_METRICS_PATH               │  Scrape endpoint path                        │  /metrics        │
│  AS_LOG_TIMINGS                │  Log per-request latency breakdown           │  false           │
│  AS_LOG_MODEL_MESSAGES         │  Log model input/output (verbose)            │  false           │
│  AS_TRACING_ENABLED            │  Enable OpenTelemetry tracing                │  false           │
│  AS_OTEL_SERVICE_NAME          │  OTel service.name resource attribute        │  vllm-responses  │
│  AS_TRACING_SAMPLE_RATIO       │  Trace sample ratio (0.0–1.0)               │  0.01            │
│  AS_OPENTELEMETRY_HOST         │  OTLP collector host                         │  otel-collector  │
│  AS_OPENTELEMETRY_PORT         │  OTLP gRPC port                              │  4317            │
└────────────────────────────────┴──────────────────────────────────────────────┴──────────────────┘
```

---

## 5. Open Questions for Community Review

**Q1 — Variable prefix migration**
Should `AS_` be the final prefix, or should the community adopt a different convention? The previous `VR_` prefix is accepted during migration but we need a clear cutoff date for dropping it.

**Q2 — `AS_WORKERS` and SQLite**
SQLite does not support multiple concurrent writers. Multi-worker mode (`AS_WORKERS > 1`) with SQLite is currently rejected at startup with a clear error. Should this be softened to a warning for read-heavy deployments, or is the hard rejection the right guardrail?
