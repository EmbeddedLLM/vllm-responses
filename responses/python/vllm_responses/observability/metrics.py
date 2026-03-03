from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.exposition import generate_latest

from vllm_responses.configs import ENV_CONFIG

HTTP_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2.5,
    5,
    10,
    20,
    30,
    60,
)

SSE_DURATION_BUCKETS = (
    0.25,
    0.5,
    0.75,
    1,
    1.5,
    2,
    3,
    5,
    7.5,
    10,
    15,
    20,
    30,
    45,
    60,
    90,
    120,
    180,
    240,
    300,
    420,
    600,
)

TOOL_DURATION_BUCKETS = (
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2.5,
    5,
    10,
    20,
    30,
    60,
    120,
)

ToolType = Literal["function", "code_interpreter", "mcp"]


@dataclass(frozen=True)
class _GatewayMetrics:
    http_requests_total: Counter
    http_request_duration_seconds: Histogram
    http_in_flight_requests: Gauge

    sse_connections_in_flight: Gauge
    sse_stream_duration_seconds: Histogram

    tool_calls_requested_total: Counter
    tool_calls_executed_total: Counter
    tool_execution_duration_seconds: Histogram
    tool_errors_total: Counter
    mcp_server_startup_total: Counter


_METRICS: _GatewayMetrics | None = None


def _get_metrics() -> _GatewayMetrics:
    global _METRICS
    if _METRICS is not None:
        return _METRICS

    # Metrics are defined once per process. In Prometheus multiprocess mode, the underlying
    # client uses mmap-backed files under PROMETHEUS_MULTIPROC_DIR.
    _METRICS = _GatewayMetrics(
        http_requests_total=Counter(
            "vllm_responses_http_requests_total",
            "Total HTTP requests completed.",
            labelnames=("method", "route", "status"),
        ),
        http_request_duration_seconds=Histogram(
            "vllm_responses_http_request_duration_seconds",
            "HTTP request handler duration in seconds (does not include SSE stream lifetime).",
            labelnames=("method", "route"),
            buckets=HTTP_DURATION_BUCKETS,
        ),
        http_in_flight_requests=Gauge(
            "vllm_responses_http_in_flight_requests",
            "Requests currently being handled by the worker (not open SSE streams).",
            multiprocess_mode="livesum",
        ),
        sse_connections_in_flight=Gauge(
            "vllm_responses_sse_connections_in_flight",
            "SSE connections currently open (stream iterators in-flight).",
            multiprocess_mode="livesum",
        ),
        sse_stream_duration_seconds=Histogram(
            "vllm_responses_sse_stream_duration_seconds",
            "SSE stream lifetime in seconds.",
            labelnames=("route",),
            buckets=SSE_DURATION_BUCKETS,
        ),
        tool_calls_requested_total=Counter(
            "vllm_responses_tool_calls_requested_total",
            "Tool calls requested by the model (seen in the model output stream).",
            labelnames=("tool_type",),
        ),
        tool_calls_executed_total=Counter(
            "vllm_responses_tool_calls_executed_total",
            "Tool calls executed by the gateway.",
            labelnames=("tool_type",),
        ),
        tool_execution_duration_seconds=Histogram(
            "vllm_responses_tool_execution_duration_seconds",
            "Tool execution duration in seconds (gateway-executed only).",
            labelnames=("tool_type",),
            buckets=TOOL_DURATION_BUCKETS,
        ),
        tool_errors_total=Counter(
            "vllm_responses_tool_errors_total",
            "Tool execution errors (gateway-executed only).",
            labelnames=("tool_type",),
        ),
        mcp_server_startup_total=Counter(
            "vllm_responses_mcp_server_startup_total",
            "Hosted MCP server startup outcomes.",
            labelnames=("server_label", "status"),
        ),
    )
    return _METRICS


def _derive_route_label(request: Request) -> str:
    scope_route = request.scope.get("route")
    route_path = getattr(scope_route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return request.url.path


def _exposition_registry() -> CollectorRegistry | None:
    """
    Return a registry suitable for `/metrics`.

    If `PROMETHEUS_MULTIPROC_DIR` is set, we expose aggregated multiprocess metrics (single coherent view
    across Gunicorn workers). Otherwise, we fall back to the default in-process registry.
    """
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return None
    try:
        from prometheus_client import multiprocess
    except Exception:
        return None

    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return registry


def install_prometheus_metrics(app: FastAPI) -> None:
    """
    Install:
    - `GET {VR_METRICS_PATH}` Prometheus scrape endpoint (not in OpenAPI schema)
    - one HTTP middleware for low-cardinality Golden Signals
    """
    if not ENV_CONFIG.metrics_enabled:
        return

    metrics = _get_metrics()
    metrics_path = ENV_CONFIG.metrics_path

    @app.get(metrics_path, include_in_schema=False)
    async def metrics_endpoint() -> Response:
        registry = _exposition_registry()
        body = generate_latest(registry) if registry is not None else generate_latest()
        return Response(content=body, media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def prometheus_middleware(request: Request, call_next):
        # Exclude self-scrapes/health checks to avoid recursion and reduce noise.
        if request.url.path in {metrics_path, "/health"}:
            return await call_next(request)

        metrics.http_in_flight_requests.inc()
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_s = max(0.0, time.perf_counter() - start)
            method = request.method
            route = _derive_route_label(request)
            metrics.http_request_duration_seconds.labels(method=method, route=route).observe(
                duration_s
            )
            metrics.http_requests_total.labels(
                method=method, route=route, status=str(status_code)
            ).inc()
            metrics.http_in_flight_requests.dec()


def instrument_sse_stream(
    *,
    route: str,
    agen: AsyncIterator[str],
) -> AsyncIterator[str]:
    """
    Wrap an SSE async iterator to record stream lifetime and connection in-flight gauge.

    Notes:
    - Must be called *before* the first `anext(...)` on the iterator to include the full lifetime.
    - Decrements gauges and records duration on normal completion, disconnect, and errors.
    """
    if not ENV_CONFIG.metrics_enabled:
        return agen

    metrics = _get_metrics()
    metrics.sse_connections_in_flight.inc()
    start = time.perf_counter()

    async def _wrapped() -> AsyncIterator[str]:
        try:
            async for chunk in agen:
                yield chunk
        finally:
            duration_s = max(0.0, time.perf_counter() - start)
            metrics.sse_stream_duration_seconds.labels(route=route).observe(duration_s)
            metrics.sse_connections_in_flight.dec()

    return _wrapped()


def record_tool_call_requested(tool_type: ToolType) -> None:
    if not ENV_CONFIG.metrics_enabled:
        return
    _get_metrics().tool_calls_requested_total.labels(tool_type=tool_type).inc()


def record_tool_executed(*, tool_type: ToolType, duration_s: float, errored: bool) -> None:
    if not ENV_CONFIG.metrics_enabled:
        return

    metrics = _get_metrics()
    metrics.tool_calls_executed_total.labels(tool_type=tool_type).inc()
    metrics.tool_execution_duration_seconds.labels(tool_type=tool_type).observe(
        max(0.0, float(duration_s))
    )
    if errored:
        metrics.tool_errors_total.labels(tool_type=tool_type).inc()


def record_mcp_server_startup(*, server_label: str, status: Literal["ok", "error"]) -> None:
    if not ENV_CONFIG.metrics_enabled:
        return
    _get_metrics().mcp_server_startup_total.labels(
        server_label=server_label,
        status=status,
    ).inc()


def get_route_label(request: Request) -> str:
    """
    Public helper for places that need a low-cardinality route label.
    """
    return _derive_route_label(request)
