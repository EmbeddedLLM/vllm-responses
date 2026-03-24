# RFC-06-01 — Observability
> **Status:** Draft — open for community review
> **Part of:** RFC-06 (Config & Infrastructure)
> **Previous:** [RFC-06-00 — Config & Deployment](RFC-06-00_config_and_deployment.md)

---

## 12. Observability

### 12.1 Prometheus Metrics

We propose enabling metrics by default, exposed at a configurable scrape endpoint (default `/metrics`). Ideally, metrics would not be recorded for health check or metrics scrape requests themselves, to avoid skewing measurements.

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

**Optional dependency.** We suggest tracing be an optional install-time dependency. If the tracing library is not installed, the gateway should log a warning at startup and continue without tracing rather than failing to start.

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

Before serving requests, we suggest the gateway verify that the upstream LLM is reachable:

```
Poll GET {upstream_llm_base_url}/health
    every {poll_interval} seconds
    until success OR timeout after {timeout} seconds
```

If the timeout is reached, we suggest the supervisor exit with a clear error message indicating which upstream URL was being checked and how long the wait lasted. For integrated mode (where the gateway and upstream LLM are co-located), this health check may be unnecessary and should ideally be skippable.

---

## Open Questions

The following questions are left explicitly open for community discussion.

**On observability:**

9. **Metrics naming.** What should the metric name prefix be? We welcome input from operators who will need to update existing dashboards. Your input here would be especially valuable.

10. **Model message logging safety.** Logging full model conversation content is useful for debugging but risks leaking sensitive user data in shared environments. Should this require an additional explicit confirmation flag, or should it be disabled entirely in production builds? We would love to hear from the community on this.

11. **OTel transport security.** The OTLP gRPC exporter is proposed to use unencrypted transport (`insecure=True`) by default. For deployments where the OTel collector is not on localhost, this transmits trace data unencrypted. Should the RFC recommend mTLS or add a config option for secure transport? We would love to hear from the community on this.

12. **Trace context propagation.** Should the gateway propagate trace context to the upstream LLM? This would enable end-to-end tracing through the full stack (client → gateway → LLM), but would need the upstream to support W3C trace context headers.

13. **Startup health check timeout.** The proposal defaults to a long startup timeout to accommodate environments where LLM loading is slow. Should there be guidance on appropriate timeout values for different deployment types? Your input here would be especially valuable.
