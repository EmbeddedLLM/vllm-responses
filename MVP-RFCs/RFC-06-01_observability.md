# RFC-06-01 — Config & Infrastructure: Observability

> **Status:** Draft — open for community review
> **Part of:** RFC-06 (Config & Infrastructure Reference)
> **Previous:** [RFC-06-00 — RuntimeConfig, Deployment & Environment Variables](RFC-06-00_config_and_deployment.md)
> **Component:** `observability/metrics.py`, `observability/tracing.py`, `utils/logging.py`, `utils/loguru_otlp_handler.py`

---

## 1. What This RFC Covers

- Prometheus metrics: available metrics, multi-worker aggregation, histogram bucket rationale
- OpenTelemetry tracing: instrumentation, sampling, span export
- Logging: Loguru, verbosity controls, OTel log forwarding
- Startup health check
- Open questions

---

## 2. Prometheus Metrics

Metrics are enabled by default and exposed at `GET /metrics`. They are disabled for health-check (`/health`) and metrics scrape requests, and for internal loopback requests in integrated mode.

### 2.1 Available Metrics

```
┌────────────────────────────────────────────────────────┬───────────┬──────────────────────────────────────┐
│  Metric name                                           │  Type     │  Labels                              │
├────────────────────────────────────────────────────────┼───────────┼──────────────────────────────────────┤
│  vllm_responses_http_requests_total                    │  Counter  │  method, route, status               │
│  vllm_responses_http_request_duration_seconds          │  Histogram│  method, route                       │
│  vllm_responses_http_in_flight_requests                │  Gauge    │  (none)                              │
│  vllm_responses_sse_connections_in_flight              │  Gauge    │  (none)                              │
│  vllm_responses_sse_stream_duration_seconds            │  Histogram│  route                               │
│  vllm_responses_tool_calls_requested_total             │  Counter  │  tool_type                           │
│  vllm_responses_tool_calls_executed_total              │  Counter  │  tool_type                           │
│  vllm_responses_tool_execution_duration_seconds        │  Histogram│  tool_type                           │
│  vllm_responses_tool_errors_total                      │  Counter  │  tool_type                           │
│  vllm_responses_mcp_server_startup_total               │  Counter  │  server_label, status                │
└────────────────────────────────────────────────────────┴───────────┴──────────────────────────────────────┘
```

`tool_type` values: `function`, `code_interpreter`, `mcp`, `web_search`.
`mcp_server_startup_total` `status` values: `ok`, `error`.

**Note:** `http_request_duration_seconds` measures the time until the first SSE chunk is flushed — it does not include the full SSE stream lifetime. Use `sse_stream_duration_seconds` for end-to-end response time.

### 2.2 Multi-Worker Prometheus (Gunicorn)

Each Gunicorn worker is a separate process with its own metric counters. When `PROMETHEUS_MULTIPROC_DIR` is set to a shared directory, the Prometheus client uses mmap-backed files to aggregate across workers. The `/metrics` endpoint then uses `MultiProcessCollector` to return a single coherent view.

```
┌────────────────────────────────────────────────────────────────────┐
│  Single worker (default)                                           │
│  PROMETHEUS_MULTIPROC_DIR not set                                  │
│  /metrics → in-process registry (only this worker's counters)      │
├────────────────────────────────────────────────────────────────────┤
│  Multi-worker                                                      │
│  PROMETHEUS_MULTIPROC_DIR=/tmp/prom_multiproc                      │
│  /metrics → MultiProcessCollector (all workers aggregated)         │
└────────────────────────────────────────────────────────────────────┘
```

Gauge metrics use `multiprocess_mode="livesum"` — they sum live values across workers.

### 2.3 Histogram Bucket Rationale

The three bucket sets are tuned to the expected range of each measurement type:

```
HTTP request duration:  5ms – 60s   (time to first SSE byte)
SSE stream duration:    250ms – 600s (full stream lifetime, incl. tool execution)
Tool execution:         10ms – 120s  (broad range covers both fast functions and slow MCP calls)
```

---

## 3. OpenTelemetry Tracing

Tracing is **off by default** (`AS_TRACING_ENABLED=false`). When enabled, the gateway configures an OTLP gRPC exporter pointing at `AS_OPENTELEMETRY_HOST:AS_OPENTELEMETRY_PORT`.

### 3.1 What Gets Instrumented

```
┌──────────────────────────────────────┬──────────────────────────────────────────┐
│  Component                           │  Instrumentation                         │
├──────────────────────────────────────┼──────────────────────────────────────────┤
│  FastAPI request handlers            │  FastAPIInstrumentor (auto-spans)        │
│  Outbound HTTP (httpx)               │  HTTPXClientInstrumentor (auto-spans)    │
│  SQLAlchemy DB queries               │  SQLAlchemyInstrumentor (via db.py)      │
│  service.name resource attribute     │  AS_OTEL_SERVICE_NAME                    │
│  service.instance.id                 │  UUID v7, unique per process startup     │
└──────────────────────────────────────┴──────────────────────────────────────────┘
```

### 3.2 Sampling

The sampler uses `ParentBased(TraceIdRatioBased(ratio))`:

- Ratio `0.0` → no traces sampled (tracing infrastructure configured but silent)
- Ratio `1.0` → 100% sampling
- Ratio `0.01` (default) → 1% head-based sampling

`ParentBased` means if an upstream span is already sampled, the gateway respects that decision and always samples. This allows tracing to be forced from the client side for specific requests regardless of the gateway's own ratio.

### 3.3 Span Export

Spans are batched via `BatchSpanProcessor` and exported over OTLP gRPC (`insecure=True`). The shutdown callback (`configure_tracing()` return value) flushes and terminates the provider cleanly on process exit.

OTel instrumentation is **optional at install time** — `agentic-stack[tracing]` is required. If the dependencies are absent, the gateway logs a warning and continues without tracing.

---

## 4. Logging

The gateway uses **Loguru** as its logging library.

### 4.1 Log Verbosity Controls

```
┌──────────────────────────┬───────────────────────────────────────────────────────┐
│  Variable                │  Effect                                               │
├──────────────────────────┼───────────────────────────────────────────────────────┤
│  AS_LOG_TIMINGS=true     │  Logs per-request latency breakdown at INFO level     │
│                          │  (includes time to first token, tool execution time)  │
│  AS_LOG_MODEL_MESSAGES=  │  Logs full model input and output message content     │
│  true                    │  at DEBUG level (WARNING: may log sensitive data)     │
└──────────────────────────┴───────────────────────────────────────────────────────┘
```

`AS_LOG_MODEL_MESSAGES` should only be enabled in development. It logs the full conversation history sent to vLLM, including system prompts and tool results.

### 4.2 OpenTelemetry Log Forwarding

When tracing is enabled, Loguru logs are also forwarded to the OTel collector via `loguru_otlp_handler.py`. This connects structured log lines to the active trace span, enabling correlated logs-and-traces in backends like Grafana Tempo + Loki.

---

## 5. Startup Health Check

Before serving requests, `agentic-stack serve` waits for the upstream vLLM to become healthy:

```
poll GET {llm_api_base}/health
    every AS_UPSTREAM_READY_INTERVAL_S seconds (default: 5s)
    until success OR timeout after AS_UPSTREAM_READY_TIMEOUT_S seconds (default: 1800s)
```

If the timeout is reached, the supervisor exits with an error. For integrated mode, the gateway is co-located with vLLM and starts after vLLM's own initialization, so the health check wait is not needed.

---

## 6. Open Questions for Community Review

**Q1 — Metrics name prefix**
Metric names currently retain the `vllm_responses_` prefix from the project's old name. These will need renaming to `agentic_stack_` as part of the `agentic-stack` rename. Is this a breaking change that requires a deprecation window for existing dashboards?

**Q2 — `AS_LOG_MODEL_MESSAGES` safety**
Logging model messages is useful for debugging but risks leaking user data in shared environments. Should this require an additional explicit confirmation flag or be disabled entirely in production configurations?

**Q3 — Tracing `insecure=True`**
The OTLP gRPC exporter uses `insecure=True` (no TLS). For deployments where the collector is not on localhost, this transmits spans unencrypted. Should the RFC recommend mTLS or a config option for secure transport?
