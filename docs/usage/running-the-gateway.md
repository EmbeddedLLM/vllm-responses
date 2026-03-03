# Running the Gateway

This guide covers the different ways to run `vLLM Responses` in various environments.

## Supported entrypoint (important)

We recommend running the gateway via `vllm-responses serve`.

!!! warning

    Starting the gateway via other mechanisms (e.g. calling `uvicorn`/`gunicorn` directly) is not intended.

## Operational Modes

### 1. Spawning vLLM (Recommended)

The simplest way to get started. The gateway manages vLLM as a supervised subprocess, giving you a **one-command local runtime**. This is ideal for:

- Local development and testing.
- Quick prototyping and demos.
- Single-instance deployments on a VM.
- Ensuring the model and gateway share a lifecycle (both start/stop together).

```bash
vllm-responses serve -- \
  meta-llama/Llama-3.2-3B-Instruct \
  --port 8457
```

!!! note

    Everything after the `--` separator is passed directly to the `vllm serve` command.

### 2. External Upstream (Advanced)

In this mode, you manage the vLLM server process separately. This is ideal for:

- **Production deployments** where vLLM runs in a separate container/pod.
- You already have a vLLM deployment (e.g., Kubernetes, Ray Serve).
- You want to restart the gateway without restarting the model (which takes time to load weights).
- Connecting to a cloud-hosted inference endpoint.

```bash
# 1. Start vLLM separately
vllm serve meta-llama/Llama-3.2-3B-Instruct --port 8457

# 2. Start the gateway pointing to it
--8<-- "snippets/serve_external_upstream_cmd.txt"
```

______________________________________________________________________

## Configuration

While CLI flags are the primary way to configure the gateway, you can also use environment variables.

| CLI Flag            | Environment Variable               | Description                                 |
| ------------------- | ---------------------------------- | ------------------------------------------- |
| `--upstream`        | `VR_LLM_API_BASE`                  | Upstream vLLM URL                           |
| `--gateway-host`    | `VR_HOST`                          | Bind host                                   |
| `--gateway-port`    | `VR_PORT`                          | Bind port                                   |
| `--gateway-workers` | `VR_WORKERS`                       | Number of workers                           |
| (env only)          | `VR_MCP_CONFIG_PATH`               | Built-in MCP runtime config path            |
| (env only)          | `VR_MCP_BUILTIN_RUNTIME_URL`       | Singleton Built-in MCP runtime loopback URL |
| (env only)          | `VR_MCP_REQUEST_REMOTE_ENABLED`    | Enable/disable Remote MCP declarations      |
| (env only)          | `VR_MCP_REQUEST_REMOTE_URL_CHECKS` | Enable/disable Remote MCP URL policy checks |

When `VR_MCP_CONFIG_PATH` is set, `vllm-responses serve` starts a singleton Built-in MCP runtime process shared by all gateway workers.
If `VR_MCP_BUILTIN_RUNTIME_URL` is unset, `serve` uses `http://127.0.0.1:5981`.
Set it only when you need a different loopback runtime address (for example, local port clashes) or when manually wiring workers to a separately managed runtime.

See [Configuration Reference](../reference/configuration.md) for a complete list.

______________________________________________________________________

## Health Checks

The gateway exposes a health check endpoint useful for load balancers (AWS ALB, Kubernetes probes).

- **Endpoint**: `GET /health`
- **Response**: `200 OK` (JSON: `{}`)

```bash
curl http://127.0.0.1:5969/health
```

The gateway also exposes a `/metrics` endpoint for Prometheus scraping. See [Observability](../deployment/observability.md) for monitoring setup instructions.

## Graceful Shutdown

The gateway handles `SIGINT` (Ctrl+C) and `SIGTERM` gracefully:

1. It stops accepting new connections.
1. It waits for active requests to complete (within a timeout).
1. It terminates the Code Interpreter subprocess (if spawned).
1. It terminates the Built-in MCP runtime subprocess (if started).
1. It terminates the vLLM subprocess (if spawned).
