# Configuration Reference

The gateway is configured using environment variables. All variables are prefixed with `VR_`.

## Core Configuration

| Variable              | Description                                                             | Default                    |
| :-------------------- | :---------------------------------------------------------------------- | :------------------------- |
| **`VR_LLM_API_BASE`** | The URL of the upstream vLLM server (e.g., `http://localhost:8457/v1`). | `http://localhost:8080/v1` |
| **`VR_HOST`**         | The interface the gateway should listen on.                             | `0.0.0.0`                  |
| **`VR_PORT`**         | The port the gateway should listen on.                                  | `5969`                     |
| **`VR_WORKERS`**      | Number of Gunicorn workers processes.                                   | `1`                        |
| **`VR_LOG_TIMINGS`**  | Enable logging of request timings and overhead.                         | `False`                    |

## Storage Configuration

| Variable                                  | Description                                                                        | Default                                 |
| :---------------------------------------- | :--------------------------------------------------------------------------------- | :-------------------------------------- |
| **`VR_DB_PATH`**                          | Database connection string. Use `sqlite+aiosqlite:///` or `postgresql+asyncpg://`. | `sqlite+aiosqlite:///vllm_responses.db` |
| **`VR_RESPONSE_STORE_CACHE`**             | Enable Redis caching for the ResponseStore.                                        | `False`                                 |
| **`VR_RESPONSE_STORE_CACHE_TTL_SECONDS`** | Cache TTL in seconds.                                                              | `3600`                                  |
| **`VR_REDIS_HOST`**                       | Redis host (if cache enabled).                                                     | `localhost`                             |
| **`VR_REDIS_PORT`**                       | Redis port.                                                                        | `6379`                                  |

## Code Interpreter Configuration

| Variable                                   | Description                                                                         | Default    |
| :----------------------------------------- | :---------------------------------------------------------------------------------- | :--------- |
| **`VR_CODE_INTERPRETER_MODE`**             | Runtime mode: `spawn`, `external`, or `disabled`.                                   | `spawn`    |
| **`VR_CODE_INTERPRETER_PORT`**             | Port for the code interpreter server.                                               | `5970`     |
| **`VR_CODE_INTERPRETER_WORKERS`**          | Worker pool size for the spawned code interpreter. (Bun Workers).                   | `0`        |
| **`VR_PYODIDE_CACHE_DIR`**                 | Directory for the Pyodide runtime cache (download + extracted files).               | (see docs) |
| **`VR_CODE_INTERPRETER_DEV_BUN_FALLBACK`** | Development-only: if `1`, allow `bun` fallback when no bundled binary is available. | `0`        |

Notes:

- `0` (default) runs **in-process** (no Bun Workers): single-threaded execution.
- `1` enables the WorkerPool path, but does not add parallelism (useful mainly to validate worker mode).
- `2+` enables parallel execution via Bun Workers (experimental).
- Each worker initializes its own Pyodide runtime, so RAM usage and startup time scale with worker count.

## MCP Configuration (Built-in + Remote)

| Variable                                | Description                                                                                          | Default |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------- | ------- |
| **`VR_MCP_CONFIG_PATH`**                | Path to Built-in MCP runtime JSON configuration file.                                                | unset   |
| **`VR_MCP_BUILTIN_RUNTIME_URL`**        | Loopback base URL for the singleton Built-in MCP runtime (`serve` default: `http://127.0.0.1:5981`). | unset   |
| **`VR_MCP_REQUEST_REMOTE_ENABLED`**     | Enable Remote MCP (`tools[].mcp.server_url`) handling.                                               | `True`  |
| **`VR_MCP_REQUEST_REMOTE_URL_CHECKS`**  | Enable Remote MCP URL policy checks (`https`, denylist hosts).                                       | `True`  |
| **`VR_MCP_HOSTED_STARTUP_TIMEOUT_SEC`** | Built-in MCP startup/discovery timeout in seconds (applies to all hosted servers).                   | `10`    |
| **`VR_MCP_HOSTED_TOOL_TIMEOUT_SEC`**    | Built-in MCP call timeout in seconds (applies to all hosted servers).                                | `60`    |

If `VR_MCP_CONFIG_PATH` is unset, Built-in MCP is disabled.
Built-in MCP is designed for `vllm-responses serve`, which starts a singleton runtime and injects `VR_MCP_BUILTIN_RUNTIME_URL` for gateway workers.
In normal `serve` usage, leave `VR_MCP_BUILTIN_RUNTIME_URL` unset to use `http://127.0.0.1:5981`.
Set it only when you need a different loopback port (for example, local port clashes) or when manually wiring gateway workers to an externally managed runtime.
If `VR_MCP_REQUEST_REMOTE_ENABLED=false`, Remote MCP declarations are rejected while Built-in MCP remains available.
If `VR_MCP_REQUEST_REMOTE_URL_CHECKS=false`, gateway URL policy checks are fully disabled for Remote MCP declarations.

For the canonical `mcp.json` examples (URL + stdio styles), see
[MCP Examples -> Built-in MCP Runtime Config](../examples/hosted-mcp-examples.md#built-in-mcp-runtime-config-mcpjson).

Notes:

- Labels under `mcpServers` are request-visible `server_label` values.
- Built-in MCP supports two server entry shapes:
    - URL-based HTTP: `url` (required, accepts `http://` or `https://`), `headers` (optional), `transport` (optional).
    - Command-style stdio: `command` (required), `args`/`env`/`cwd` (optional), `transport` optional but only `"stdio"`.
- Nested `transport` objects are rejected (for example, `"transport": {"type":"stdio", ...}`).
- `transport: "stdio"` without command-style keys is rejected.
- Mixing HTTP and stdio keys in one entry (for example `command` + `url`) is rejected.
- Hosted startup and tool timeouts are configured globally with:
    - `VR_MCP_HOSTED_STARTUP_TIMEOUT_SEC`
    - `VR_MCP_HOSTED_TOOL_TIMEOUT_SEC`
- Unknown non-runtime server fields are forwarded to FastMCP.
- In `serve` mode, `VR_MCP_BUILTIN_RUNTIME_URL` must be loopback `http://127.0.0.1:<port>` (or `http://localhost:<port>`), with no path/query/fragment.

## Observability Configuration

| Variable                      | Description                                                           | Default          |
| :---------------------------- | :-------------------------------------------------------------------- | :--------------- |
| **`VR_METRICS_ENABLED`**      | Enable Prometheus-compatible metrics and the `GET /metrics` endpoint. | `True`           |
| **`VR_METRICS_PATH`**         | Metrics endpoint path.                                                | `/metrics`       |
| **`VR_TRACING_ENABLED`**      | Enable OpenTelemetry tracing (OTLP gRPC exporter).                    | `False`          |
| **`VR_OTEL_SERVICE_NAME`**    | Service name used in OpenTelemetry resources.                         | `vllm-responses` |
| **`VR_TRACING_SAMPLE_RATIO`** | Trace sampling ratio in `[0.0, 1.0]` (ratio-based).                   | `0.01`           |
| **`VR_OPENTELEMETRY_HOST`**   | OTLP endpoint host (gRPC).                                            | `otel-collector` |
| **`VR_OPENTELEMETRY_PORT`**   | OTLP endpoint port (gRPC).                                            | `4317`           |

## Example Configurations

### Local Development (Default)

```bash
export VR_LLM_API_BASE="http://127.0.0.1:8457/v1"
export VR_DB_PATH="sqlite+aiosqlite:///vllm_responses.db"
```

### Production with PostgreSQL & Redis

```bash
export VR_LLM_API_BASE="http://vllm-service:8000/v1"
export VR_DB_PATH="postgresql+asyncpg://user:pass@db-host:5432/vllm_responses"
export VR_WORKERS=8
export VR_RESPONSE_STORE_CACHE=1
export VR_REDIS_HOST="redis-host"
```

### Enable Built-in MCP

```bash
--8<-- "snippets/mcp_enable_config_env.txt"
```
