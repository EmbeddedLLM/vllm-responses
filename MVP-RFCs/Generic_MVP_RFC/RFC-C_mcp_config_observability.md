# RFC-C — MCP Integration, Configuration, and Observability

> **Status:** Draft — open for community feedback
> **Covers:** MCP hosted and request-remote modes, security policy, configuration model, deployment modes, and observability
> **Previous:** [RFC-B — Built-in Tools: Code Interpreter & Web Search](RFC-B_built_in_tools.md)
> **See also:** [RFC-A — System Architecture & Stateful Conversation Memory](RFC-A_architecture_and_statefulness.md)

---

## Table of Contents

1. [Overview](#1-overview)
2. [MCP Integration: The Two Modes](#2-mcp-integration-the-two-modes)
3. [Request MCP Declaration Format](#3-request-mcp-declaration-format)
4. [Hosted MCP — Config File Format](#4-hosted-mcp--config-file-format)
5. [Hosted Registry Lifecycle](#5-hosted-registry-lifecycle)
6. [Request-Remote MCP and Per-Request Resolution](#6-request-remote-mcp-and-per-request-resolution)
7. [Security Policy](#7-security-policy)
8. [MCP SSE Event Lifecycle](#8-mcp-sse-event-lifecycle)
9. [End-to-End MCP Request Flow](#9-end-to-end-mcp-request-flow)
10. [Configuration Model](#10-configuration-model)
11. [Deployment Modes](#11-deployment-modes)
12. [Observability](#12-observability)
13. [Open Questions](#13-open-questions)

---

## 1. Overview

This RFC covers three related infrastructure areas for **agentic-stack**:

- **MCP Integration:** the gateway's support for the Model Context Protocol, including operator-configured hosted servers and client-supplied request-remote servers, along with security enforcement.
- **Configuration:** a single immutable configuration object passed to all subsystems at startup, with support for multiple deployment modes.
- **Observability:** Prometheus metrics, OpenTelemetry tracing, structured logging, and startup health checks.

These three areas are grouped together because they share a common theme: they are all horizontal concerns that affect every part of the system, rather than belonging to a single feature area.

---

## 2. MCP Integration: The Two Modes

Every MCP tool declaration in a request belongs to one of two modes:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Mode              │  How the server is identified                  │
├─────────────────────────────────────────────────────────────────────┤
│  Hosted            │  No server URL in the declaration.             │
│                    │  Server was pre-configured by the operator at  │
│                    │  gateway startup via a config file.            │
│                    │  Identified by a server_label that must match  │
│                    │  a label in the config file.                   │
├─────────────────────────────────────────────────────────────────────┤
│  Request-remote    │  A server URL is present in the declaration.   │
│                    │  Client supplies the URL at request time.      │
│                    │  Gateway connects outbound to that URL.        │
└─────────────────────────────────────────────────────────────────────┘
```

The routing decision is determined entirely by whether a server URL is present in the declaration. No other configuration is needed on the client side to distinguish the two modes.

---

## 3. Request MCP Declaration Format

A client attaches MCP tools to a request by including entries in the `tools` array with `type: "mcp"`. The wire format is:

```json
{
  "tools": [
    {
      "type": "mcp",
      "server_label": "my_search",
      "server_url": "https://mcp.example.com/search",
      "authorization": "sk-...",
      "headers": { "X-Custom": "value" },
      "allowed_tools": ["search", "fetch"]
    },
    {
      "type": "mcp",
      "server_label": "code_tools",
      "allowed_tools": ["run_query"]
    }
  ]
}
```

Key fields:

```
┌──────────────────┬────────────────────────────────────────────────────────────┐
│  Field           │  Meaning                                                   │
├──────────────────┼────────────────────────────────────────────────────────────┤
│  server_label    │  Identifier used in SSE events and error messages.         │
│                  │  We suggest restricting to alphanumeric, underscore,       │
│                  │  and hyphen characters.                                    │
│  server_url      │  Present → request-remote mode.                            │
│                  │  Absent  → hosted mode (label must match a pre-configured  │
│                  │            server in the operator's config file)           │
│  authorization   │  Bearer token. Gateway injects as Authorization: Bearer …  │
│  headers         │  Additional outbound HTTP headers (request-remote only)    │
│  allowed_tools   │  Subset filter. Absent or null = all tools from server     │
│  require_approval│  Only "never" is accepted for MVP (approval flows are      │
│                  │  not yet supported — other values should return an error)  │
└──────────────────┴────────────────────────────────────────────────────────────┘
```

---

## 4. Hosted MCP — Config File Format

Hosted MCP servers are declared in a JSON config file whose path is specified in the gateway configuration. We propose the following format:

```json
{
  "mcpServers": {
    "my_search": {
      "url": "https://internal-search.corp/mcp",
      "headers": { "Authorization": "Bearer sk-internal" },
      "transport": "streamable-http"
    },
    "db_tools": {
      "command": "python",
      "args": ["-m", "my_mcp_server"],
      "env": { "DB_URL": "postgresql://..." },
      "cwd": "/opt/tools"
    }
  }
}
```

### 4.1 Transport Detection

We propose inferring the transport type from the entry shape, so an explicit `transport` field is usually not required:

```
┌──────────────────────────────────────────────┬──────────────────┐
│  Entry has…                                  │  Transport       │
├──────────────────────────────────────────────┼──────────────────┤
│  url field                                   │  HTTP (SSE or    │
│                                              │  streamable-HTTP)│
│  command field (and/or args / env / cwd)     │  stdio           │
└──────────────────────────────────────────────┴──────────────────┘
```

Mixing HTTP and stdio fields (`url` + `command`) in one entry should be rejected at config load time with a clear error.

### 4.2 Server Entry Types

```
HTTP server entry
├── url         string — required, absolute http/https URL
├── headers     object (string → string) — optional outbound headers
├── auth        string — optional bearer token (takes precedence over headers)
└── transport   string — optional (auto-detected if absent)

stdio server entry
├── command     string — required executable
├── args        list[string] — optional command arguments
├── env         object (string → string) — optional environment variables
├── cwd         string — optional working directory
└── transport   always "stdio"
```

### 4.3 Label Validation

Server labels should be validated at config load time. We suggest restricting labels to alphanumeric characters, underscores, and hyphens. Labels with spaces, dots, or other special characters should be rejected with a clear error message identifying which label is invalid.

---

## 5. Hosted Registry Lifecycle

We propose a singleton hosted registry created at gateway startup that owns one MCP connection per configured server and keeps the tool inventory in memory.

### 5.1 Startup Sequence

```
Gateway starts
    │
    ▼
Hosted Registry startup()
    │
    ├── For each configured server label:
    │       │
    │       ├── Open connection to server (HTTP or stdio)
    │       ├── Fetch tool inventory
    │       │   (with configurable timeout)
    │       │
    │       ├── SUCCESS → mark server available
    │       │             cache tool inventory in memory
    │       │             record startup metric: status="ok"
    │       │
    │       └── FAILURE → mark server unavailable
    │                     record startup error
    │                     record startup metric: status="error"
    │                     (gateway continues — partial failures are tolerated)
    ▼
Registry marked as started
```

Startup failures are **non-fatal**: the gateway starts regardless. A server that fails startup is marked unavailable; any request that references it receives a client error response. We welcome feedback on whether there should be a mode where unavailable servers cause requests to degrade gracefully (tool omitted) rather than failing.

### 5.2 Availability States

```
┌────────────────────────────────┬──────────────────────────┬────────────────────────┐
│  Server state                  │  is available?           │  Effect on request     │
├────────────────────────────────┼──────────────────────────┼────────────────────────┤
│  Startup succeeded             │  Yes                     │  Usable                │
│  Startup failed / timed out    │  No                      │  Request gets error    │
│  Label not in config           │  N/A (unknown label)     │  Request gets error    │
└────────────────────────────────┴──────────────────────────┴────────────────────────┘
```

### 5.3 Stale Tool Refresh

MCP servers may update their tool list at runtime. We propose a one-retry refresh pattern to handle this gracefully without adding unnecessary complexity:

```
call_tool(server_label, tool_name, arguments)
    │
    ├── Attempt tool call
    │       │
    │       ├── OK  ──────────────────────────► return result
    │       │
    │       └── Tool not found in cached inventory
    │                   │
    │                   └── trigger stale error
    │
    └── On stale error:
            │
            ├── Re-fetch tool inventory from server
            │
            ├── Tool still missing ──────────► raise "tool not found" error
            │
            └── Tool found ──────────────────► update in-memory inventory
                                              ► retry call with refreshed handle
                                              ► return result
```

The refreshed inventory is persisted back into the in-memory cache so subsequent calls do not trigger another refresh unnecessarily. For high-throughput deployments there is a risk of a thundering herd if many concurrent requests hit the stale condition simultaneously — see Open Questions.

---

## 6. Request-Remote MCP and Per-Request Resolution

At the start of every request that includes MCP tool declarations, the gateway performs per-request resolution to determine which MCP servers and tools are available for that request.

```
Per-request resolution (for each MCP tool declaration):
    │
    ├── Validate declaration format
    │   (reject unsupported fields, invalid labels)
    │
    ├── server_url absent → hosted mode
    │       │
    │       ├── Hosted registry enabled? (if not → error)
    │       ├── Server available? (if not → error)
    │       └── Return tool inventory from registry
    │
    └── server_url present → request-remote mode
            │
            ├── Request-remote enabled? (if not → error)
            ├── Validate URL (see Security Policy)
            ├── Build outbound auth headers
            ├── Open connection to server
            └── Fetch tool inventory
    │
    ├── Apply allowed_tools filter (if specified)
    │   (intersect declared allowed_tools with server's actual tools)
    │
    └── empty final tool set → error (no usable tools from this server)
```

The result of resolution is a mapping from server label to resolved tool inventory. This mapping is passed to the orchestrator, which registers the tools with the LLM for that request.

**Tool name mapping.** MCP tool names must be globally unique within a request. The gateway needs to track which server each tool call belongs to. We suggest maintaining a mapping from tool name to `(server_label, tool_name, mode)` so that each tool call can be attributed to its originating server in SSE events and logs.

---

## 7. Security Policy

### 7.1 Request-Remote URL Validation

When URL validation is enabled (we propose enabling it by default), the gateway should enforce:

```
┌────────────────────────────────────────┬────────────────────────────────────────┐
│  Check                                 │  Result on failure                     │
├────────────────────────────────────────┼────────────────────────────────────────┤
│  Scheme must be https                  │  Client error (400)                    │
│  Host must be present                  │  Client error (400)                    │
│  Host must not be "localhost" or       │  Client error (400)                    │
│  any *.localhost variant               │                                        │
│  Host must not be an IPv4 literal      │  Client error (400)                    │
│  Host must not be an IPv6 literal      │  Client error (400)                    │
└────────────────────────────────────────┴────────────────────────────────────────┘
```

The IP literal check (both IPv4 and IPv6) is intended to prevent SSRF (Server-Side Request Forgery) attacks where a client supplies a URL pointing at internal network services. We suggest using the platform's IP address parsing library to cover edge cases.

URL validation should be configurable so that operators running in trusted internal environments (e.g. air-gapped deployments, service meshes) can disable it. We welcome discussion on whether a more granular policy (e.g. an allowlist of trusted CIDR ranges) would be more appropriate than a binary on/off switch.

### 7.2 Authorization Header Precedence

When an `authorization` field is provided in the declaration alongside a `headers` map, we propose:

1. Remove any existing `Authorization` header (in any case variant) from the `headers` map.
2. Inject `Authorization: Bearer {authorization_value}`.

This ensures the token from the `authorization` field always takes precedence over anything in the `headers` map, preventing accidental double-auth configurations. If `headers` contains multiple `Authorization` variants without an `authorization` field, that should be treated as an error.

### 7.3 Secret Redaction

Secrets (tokens, header values) must never appear in client-facing error messages. We propose collecting all secret values at resolution time and using them to redact any error text before it is returned to the client.

For hosted servers, secrets to redact include:
- All HTTP header values from the server config entry.
- The `auth` field value.
- Both the raw token and the full `Bearer {token}` form.
- For stdio servers: all environment variable values.

We suggest sorting secrets longest-first before applying redaction, to prevent shorter substrings from masking longer ones (e.g. avoid a common prefix matching before the full token).

---

## 8. MCP SSE Event Lifecycle

When the model calls an MCP tool, the gateway emits a `mcp_call` output item with the following lifecycle:

```
mcp_call item lifecycle:

├── response.mcp_call.in_progress
│   { item_id, type, server_label, tool_name, arguments_text (empty initially) }
│
├── response.mcp_call_arguments.delta     (one per argument token fragment)
│   (streamed in real time as the model produces the arguments JSON)
│
├── response.mcp_call_arguments.done
│   (full arguments JSON assembled)
│
│  [gateway executes the tool call]
│
├── response.mcp_call.completed           (tool returned a result)
│   { output }
│
│  OR
│
└── response.mcp_call.failed              (tool raised an error)
    { error }
```

`server_label` and `tool_name` should appear in every event so the client can associate output with its originating server without needing to parse the arguments JSON.

---

## 9. End-to-End MCP Request Flow

```
POST /v1/responses
    (tools: [{ type:"mcp", server_label:"X", server_url?:"..." }])
    │
    ▼
Request Orchestrator
    │
    ├── Per-request resolution
    │       │
    │       ├── hosted → fetch tool inventory from hosted registry
    │       │
    │       └── request-remote → validate URL
    │                           open connection, fetch tool inventory
    │
    ├── Register resolved tools with LLM for this request
    │
    ├── LLM streams tokens
    │       │
    │       ├── LLM decides to call tool "tool_name" on server "X"
    │       │
    │       ├── Gateway executes tool call
    │       │     (hosted: via registry connection)
    │       │     (request-remote: via per-request connection)
    │       │
    │       └── Result returned, LLM continues
    │
    ├── Normalizer maps tool call events → McpCall intermediate events
    │
    ├── Composer emits mcp_call SSE items
    │
    └── SSE Encoder → Client stream
```

---

## 10. Configuration Model

### 10.1 A Single Immutable Configuration Object

We propose that all configuration for a running gateway live in a single frozen object constructed once at startup, passed to every subsystem, and never mutated. This makes configuration explicit and testable — subsystems receive it as a constructor argument rather than reading from global singletons.

For the MVP, we suggest the following configuration groups:

```
Configuration Object (proposed groups)
│
├── Deployment
│   ├── Runtime mode (standalone / supervisor / integrated / testing)
│   ├── Bind host and port
│   ├── Worker process count
│   └── Maximum concurrent requests
│
├── Upstream LLM
│   ├── Base URL for the Chat Completions API
│   └── Optional bearer token for upstream authentication
│
├── Tool Settings
│   ├── Web search profile (name of the active profile, or disabled)
│   ├── Code interpreter mode (spawn / external / disabled)
│   ├── Code interpreter port
│   ├── Code interpreter worker count
│   ├── WebAssembly cache directory
│   └── Code interpreter startup timeout
│
├── MCP Settings
│   ├── Path to hosted MCP config file (optional)
│   ├── Request-remote mode enabled flag
│   ├── Request-remote URL validation enabled flag
│   ├── Per-server startup timeout
│   └── Per-tool call timeout
│
├── Response Store
│   ├── Database connection URL
│   ├── Redis host and port
│   ├── Redis cache enabled flag
│   └── Cache entry TTL
│
├── Observability
│   ├── Metrics enabled flag and endpoint path
│   ├── Request timing logging flag
│   ├── Model message logging flag (verbose/debug)
│   ├── Tracing enabled flag
│   ├── OTel service name
│   ├── Trace sample ratio
│   └── OTel collector host and port
│
└── Miscellaneous
    ├── Log directory
    ├── Upstream health check timeout and poll interval
    └── Internal request header name (for integrated-mode loopback bypass)
```

### 10.2 Environment Variable Groups

All environment variables should share a common prefix (we suggest `AS_` for agentic-stack, though the community may prefer a different convention). Variables should be grouped logically:

- **Core gateway variables:** bind address and port, worker count, concurrency limit, log directory, upstream LLM URL and auth.
- **Code interpreter variables:** mode (spawn/external/disabled), port, worker count, startup timeout, cache directory, dev fallback flag.
- **Web search variables:** profile selection.
- **MCP variables:** config file path, sidecar URL override, request-remote enabled/disabled, URL validation enabled/disabled, timeout settings.
- **Response store variables:** database URL, Redis host/port, cache enabled/disabled, cache TTL.
- **Observability variables:** metrics enabled/disabled and path, timing and message logging flags, tracing enabled/disabled, OTel service name, sample ratio, collector host/port.

### 10.3 Config Precedence

We propose:

```
CLI arguments  →  highest priority  (where applicable, supervisor + integrated modes)
Env variables  →  second
Hardcoded defaults  →  lowest
```

We suggest there is no gateway-specific config file (aside from the MCP server config file). All tuning is done via CLI arguments and environment variables.

---

## 11. Deployment Modes

We propose four deployment modes. The mode is determined by the entrypoint used, not by a configuration variable that operators set directly.

```
┌────────────────────┬──────────────────────────────────────────────────────────┐
│  Mode              │  Description                                             │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Standalone        │  Gateway only. The operator points configuration at an  │
│                    │  existing upstream LLM instance. No upstream process     │
│                    │  management. Config from environment variables only.     │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Supervisor        │  A supervisor process launches gateway workers plus      │
│                    │  optional sidecars (code interpreter, MCP runtime).     │
│                    │  Config from CLI args + environment variables.           │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Integrated        │  Gateway runs inside the upstream LLM process (e.g.     │
│                    │  as a vLLM plugin). Port and host inherited from the     │
│                    │  host process. Upstream URL always points to loopback.  │
│                    │  Worker count always 1.                                  │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Testing / Mock    │  Like standalone, but uses a mock LLM backend instead   │
│                    │  of a real upstream. For test suites and CI.             │
└────────────────────┴──────────────────────────────────────────────────────────┘
```

### 11.1 Config Construction Per Mode

```
Standalone / Testing:
    └── Read environment variables → apply defaults

Supervisor:
    ├── Parse CLI arguments
    ├── Read environment variables
    └── Apply defaults (CLI takes precedence over env)

Integrated:
    ├── Receive host/port from host process CLI args
    ├── Read environment variables
    └── Apply defaults (host CLI args take highest precedence)
```

All paths converge at a common config construction step that resolves any remaining fields from environment variables and applies hardcoded defaults.

---

## 12. Observability

### 12.1 Prometheus Metrics

We propose enabling metrics by default, exposed at a configurable scrape endpoint (default `/metrics`). Metrics should not be recorded for health check or metrics scrape requests themselves, to avoid skewing measurements.

Proposed metrics:

```
┌──────────────────────────────────────────────────────────┬───────────┬────────────────────────────┐
│  Metric (conceptual name)                                │  Type     │  Labels                    │
├──────────────────────────────────────────────────────────┼───────────┼────────────────────────────┤
│  HTTP requests (total)                                   │  Counter  │  method, route, status     │
│  HTTP request duration (time to first SSE byte)          │  Histogram│  method, route             │
│  HTTP in-flight requests                                 │  Gauge    │  (none)                    │
│  SSE connections in flight                               │  Gauge    │  (none)                    │
│  SSE stream duration (full stream lifetime)              │  Histogram│  route                     │
│  Tool calls requested                                    │  Counter  │  tool_type                 │
│  Tool calls executed                                     │  Counter  │  tool_type                 │
│  Tool execution duration                                 │  Histogram│  tool_type                 │
│  Tool execution errors                                   │  Counter  │  tool_type                 │
│  MCP hosted server startups                              │  Counter  │  server_label, status      │
└──────────────────────────────────────────────────────────┴───────────┴────────────────────────────┘
```

`tool_type` values: `function`, `code_interpreter`, `mcp`, `web_search`.
`mcp_server_startup` `status` values: `ok`, `error`.

**Note:** `http_request_duration` measures time to the first SSE byte, not the full stream lifetime. Use `sse_stream_duration` for end-to-end response time. This distinction matters especially for requests with long-running tool executions.

### 12.2 Multi-Worker Prometheus Aggregation

When running with multiple worker processes, each process has its own metric counters. To get a unified view across all workers, we propose using a shared directory for mmap-backed metric files (following the standard Prometheus multi-process pattern). When this directory is configured, the metrics endpoint should use an aggregating collector that merges all workers' counters into a single response.

```
┌────────────────────────────────────────────────────────────────────┐
│  Single worker (default)                                           │
│  Metrics directory not configured                                  │
│  /metrics → this worker's counters only                            │
├────────────────────────────────────────────────────────────────────┤
│  Multi-worker                                                      │
│  Metrics directory configured (e.g. a shared tmpfs path)           │
│  /metrics → all workers' counters aggregated                       │
└────────────────────────────────────────────────────────────────────┘
```

Gauge metrics in multi-worker mode should report the sum across live workers.

### 12.3 Histogram Bucket Rationale

We suggest tuning histogram buckets to the expected range of each measurement type:

- **HTTP request duration (time to first SSE byte):** tight lower bound (a few milliseconds for fast responses) to a moderate upper bound (one minute for requests that involve slow tool execution before generating any text).
- **SSE stream duration (full stream lifetime):** wider range, covering both quick text-only responses and long tool-heavy conversations.
- **Tool execution duration:** broad range covering both fast built-in tools and potentially slow MCP calls or network-dependent web searches.

The exact bucket boundaries are an implementation decision. We welcome community input from operators who have observability experience with similar gateways.

### 12.4 OpenTelemetry Tracing

We propose tracing as **opt-in** (disabled by default). When enabled, the gateway should configure an OTLP gRPC exporter pointing at a configurable collector endpoint.

**What gets instrumented:**

```
┌──────────────────────────────────────┬──────────────────────────────────────────┐
│  Component                           │  Instrumentation                         │
├──────────────────────────────────────┼──────────────────────────────────────────┤
│  HTTP request handlers               │  Auto-instrumentation (framework spans)  │
│  Outbound HTTP clients               │  Auto-instrumentation (client spans)     │
│  Database queries                    │  Auto-instrumentation (DB spans)         │
│  Service identity                    │  Configurable service name resource attr │
│  Process identity                    │  Unique instance ID per process startup  │
└──────────────────────────────────────┴──────────────────────────────────────────┘
```

**Sampling.** We suggest a parent-based, ratio-based sampler with a low default ratio (e.g. 1%). `ParentBased` means that if an upstream request already has an active trace context, the gateway respects that and always samples — allowing specific requests to be forced into tracing from the client side regardless of the gateway's own ratio.

**Span export.** We suggest batching spans via a batch span processor and exporting over OTLP gRPC. The tracing infrastructure should be cleanly shut down on process exit to avoid losing buffered spans.

**Optional dependency.** Tracing should be an optional install-time dependency. If the tracing library is not installed, the gateway should log a warning at startup and continue without tracing rather than failing to start.

### 12.5 Logging

**Structured logging.** We propose using a structured logging library. Log lines should include at minimum: log level, timestamp, logger name, message, and any structured fields relevant to the context (request ID, model name, tool type, etc.).

**Verbosity controls.** We suggest two opt-in verbose logging flags:

```
┌──────────────────────────────┬───────────────────────────────────────────────────────┐
│  Flag                        │  Effect                                               │
├──────────────────────────────┼───────────────────────────────────────────────────────┤
│  Timing logging              │  Logs per-request latency breakdown at INFO level.    │
│                              │  Includes time to first token, tool execution time.  │
├──────────────────────────────┼───────────────────────────────────────────────────────┤
│  Model message logging       │  Logs full model input/output content at DEBUG level. │
│                              │  WARNING: may log sensitive user data.               │
│                              │  Only for development environments.                  │
└──────────────────────────────┴───────────────────────────────────────────────────────┘
```

**OTel log forwarding.** When tracing is enabled, we propose forwarding log lines to the OTel collector as well. This enables correlated logs-and-traces in observability backends that support both (e.g. Grafana Tempo + Loki). Each forwarded log line should carry the active trace ID if one is present.

### 12.6 Startup Health Check

Before serving requests, the gateway should verify that the upstream LLM is reachable:

```
Poll GET {upstream_llm_base_url}/health
    every {poll_interval} seconds
    until success OR timeout after {timeout} seconds
```

If the timeout is reached, the supervisor should exit with a clear error message indicating which upstream URL was being checked and how long the wait lasted. For integrated mode (where the gateway and upstream LLM are co-located), this health check may be unnecessary and should be skippable.

---

## 13. Open Questions

The following questions are left explicitly open for community discussion.

**On MCP integration:**

1. **Hosted server availability vs. request failure.** When a hosted server fails startup, any request referencing it receives an error. Should there be a `required: false` mode where an unavailable hosted server causes the tool to be silently omitted rather than failing the request?

2. **Stale tool refresh thundering herd.** If many concurrent requests hit the stale condition simultaneously, they will all trigger a refresh. Should there be a per-server refresh lock? Or should the inventory be periodically refreshed in the background to prevent staleness entirely?

3. **URL validation granularity.** The proposal is a binary on/off for URL validation. For deployments with a mix of trusted internal servers and untrusted external servers, a more granular policy (allowlist by CIDR range or by hostname pattern) might be more appropriate. Is this worth the complexity for the MVP?

4. **Request-remote connection lifecycle.** Per-request connections to remote MCP servers are opened and closed within the request. For high-throughput deployments this could be expensive. Should there be connection pooling for frequently used request-remote servers?

5. **MCP approval workflows.** `require_approval != "never"` is rejected today. These are OpenAI API fields representing human-in-the-loop approval flows. Should the MVP document this as a known gap with a forward roadmap?

**On configuration:**

6. **Env variable prefix.** We suggest `AS_` as the prefix. Is this the right convention? Should the community standardize on a different prefix?

7. **Multi-worker SQLite.** SQLite does not support multiple concurrent writers. Should multi-worker mode with SQLite be rejected at startup (hard guardrail), or should it be a warning that allows read-heavy deployments to proceed at their own risk?

8. **Config file support.** We propose configuration via CLI args and environment variables only, with no gateway config file. Is there a use case for a gateway config file that environment variables cannot address?

**On observability:**

9. **Metrics naming.** What should the metric name prefix be? We welcome input from operators who will need to update existing dashboards.

10. **Model message logging safety.** Logging full model conversation content is useful for debugging but risks leaking sensitive user data in shared environments. Should this require an additional explicit confirmation flag, or should it be disabled entirely in production builds?

11. **OTel transport security.** The OTLP gRPC exporter is proposed to use unencrypted transport (`insecure=True`) by default. For deployments where the OTel collector is not on localhost, this transmits trace data unencrypted. Should the RFC recommend mTLS or add a config option for secure transport?

12. **Trace context propagation.** Should the gateway propagate trace context to the upstream LLM? This would enable end-to-end tracing through the full stack (client → gateway → LLM), but requires the upstream to support W3C trace context headers.

13. **Startup health check timeout.** The proposal defaults to a long startup timeout to accommodate environments where LLM loading is slow. Should there be guidance on appropriate timeout values for different deployment types?

---

**Previous:** [RFC-B — Built-in Tools: Code Interpreter & Web Search](RFC-B_built_in_tools.md)
**See also:** [RFC-A — System Architecture & Stateful Conversation Memory](RFC-A_architecture_and_statefulness.md)
