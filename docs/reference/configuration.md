# Configuration Reference

The gateway is configured using environment variables. All variables are prefixed with `VTOL_`.

## Core Configuration

| Variable                | Description                                                             | Default                    |
| :---------------------- | :---------------------------------------------------------------------- | :------------------------- |
| **`VTOL_LLM_API_BASE`** | The URL of the upstream vLLM server (e.g., `http://localhost:8457/v1`). | `http://localhost:8080/v1` |
| **`VTOL_HOST`**         | The interface the gateway should listen on.                             | `0.0.0.0`                  |
| **`VTOL_PORT`**         | The port the gateway should listen on.                                  | `5969`                     |
| **`VTOL_WORKERS`**      | Number of Gunicorn workers processes.                                   | `1`                        |
| **`VTOL_LOG_TIMINGS`**  | Enable logging of request timings and overhead.                         | `False`                    |

## Storage Configuration

| Variable                                    | Description                                                                        | Default                       |
| :------------------------------------------ | :--------------------------------------------------------------------------------- | :---------------------------- |
| **`VTOL_DB_PATH`**                          | Database connection string. Use `sqlite+aiosqlite:///` or `postgresql+asyncpg://`. | `sqlite+aiosqlite:///vtol.db` |
| **`VTOL_RESPONSE_STORE_CACHE`**             | Enable Redis caching for the ResponseStore.                                        | `False`                       |
| **`VTOL_RESPONSE_STORE_CACHE_TTL_SECONDS`** | Cache TTL in seconds.                                                              | `3600`                        |
| **`VTOL_REDIS_HOST`**                       | Redis host (if cache enabled).                                                     | `localhost`                   |
| **`VTOL_REDIS_PORT`**                       | Redis port.                                                                        | `6379`                        |

## Code Interpreter Configuration

| Variable                                     | Description                                                                         | Default    |
| :------------------------------------------- | :---------------------------------------------------------------------------------- | :--------- |
| **`VTOL_CODE_INTERPRETER_MODE`**             | Runtime mode: `spawn`, `external`, or `disabled`.                                   | `spawn`    |
| **`VTOL_CODE_INTERPRETER_PORT`**             | Port for the code interpreter server.                                               | `5970`     |
| **`VTOL_CODE_INTERPRETER_WORKERS`**          | Worker pool size for the spawned code interpreter. (Bun Workers).                   | `0`        |
| **`VTOL_PYODIDE_CACHE_DIR`**                 | Directory for the Pyodide runtime cache (download + extracted files).               | (see docs) |
| **`VTOL_CODE_INTERPRETER_DEV_BUN_FALLBACK`** | Development-only: if `1`, allow `bun` fallback when no bundled binary is available. | `0`        |

Notes:

- `0` (default) runs **in-process** (no Bun Workers): single-threaded execution.
- `1` enables the WorkerPool path, but does not add parallelism (useful mainly to validate worker mode).
- `2+` enables parallel execution via Bun Workers (experimental).
- Each worker initializes its own Pyodide runtime, so RAM usage and startup time scale with worker count.

## Observability Configuration

| Variable                        | Description                                                           | Default          |
| :------------------------------ | :-------------------------------------------------------------------- | :--------------- |
| **`VTOL_METRICS_ENABLED`**      | Enable Prometheus-compatible metrics and the `GET /metrics` endpoint. | `True`           |
| **`VTOL_METRICS_PATH`**         | Metrics endpoint path.                                                | `/metrics`       |
| **`VTOL_TRACING_ENABLED`**      | Enable OpenTelemetry tracing (OTLP gRPC exporter).                    | `False`          |
| **`VTOL_OTEL_SERVICE_NAME`**    | Service name used in OpenTelemetry resources.                         | `vtol`           |
| **`VTOL_TRACING_SAMPLE_RATIO`** | Trace sampling ratio in `[0.0, 1.0]` (ratio-based).                   | `0.01`           |
| **`VTOL_OPENTELEMETRY_HOST`**   | OTLP endpoint host (gRPC).                                            | `otel-collector` |
| **`VTOL_OPENTELEMETRY_PORT`**   | OTLP endpoint port (gRPC).                                            | `4317`           |

## Example Configurations

### Local Development (Default)

```bash
export VTOL_LLM_API_BASE="http://127.0.0.1:8457/v1"
export VTOL_DB_PATH="sqlite+aiosqlite:///vtol.db"
```

### Production with PostgreSQL & Redis

```bash
export VTOL_LLM_API_BASE="http://vllm-service:8000/v1"
export VTOL_DB_PATH="postgresql+asyncpg://user:pass@db-host:5432/vtol"
export VTOL_WORKERS=8
export VTOL_RESPONSE_STORE_CACHE=1
export VTOL_REDIS_HOST="redis-host"
```
