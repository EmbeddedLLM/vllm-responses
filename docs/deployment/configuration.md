# Configuration Guide

Configure the gateway's database, caching, workers, and service architecture for your deployment needs.

## Overview

This guide covers configuration options for:

- **Storage backend** (SQLite vs PostgreSQL)
- **Worker processes** (single vs multiple)
- **Response caching optimization** (optional Redis integration)
- **Service architecture** (single-command runtime vs disaggregated)

For complete environment variable reference, see [Configuration Reference](../reference/configuration.md).

## Storage Backend

The gateway stores conversation state for `previous_response_id` functionality. Choose the storage backend that fits your deployment model.

Stored continuation anchors include terminal responses with `status="completed"` and `status="incomplete"` (when `store=true`).

### SQLite (Default)

Zero-configuration storage using a local SQLite database file.

```bash
# Default - no configuration needed
--8<-- "snippets/serve_external_upstream_cmd.txt"
```

**Characteristics:**

- Zero setup required
- Single file database (`vtol.db`)
- Works with multiple workers on the same machine (uses WAL mode)
- Does NOT work across multiple machines

### PostgreSQL

Required for multi-machine deployments and high-availability scenarios.

```bash
export VTOL_DB_PATH="postgresql+asyncpg://user:password@db-host:5432/vtol"
--8<-- "snippets/serve_external_upstream_cmd.txt"
```

**Migration notes:** When moving from SQLite to PostgreSQL:

1. Set `VTOL_DB_PATH` to your PostgreSQL connection string
1. Restart the gateway - tables will be created automatically
1. Existing SQLite data will NOT be migrated

______________________________________________________________________

## Worker Configuration

Control gateway throughput by adjusting the number of worker processes.

### Single Worker (Default)

The default configuration runs one worker process.

```bash
--8<-- "snippets/serve_external_upstream_cmd.txt"
```

**When this is sufficient:**

- Local development
- Low to moderate traffic (\<100 concurrent requests)
- Testing and experimentation

### Multiple Workers

Increase concurrency by running multiple worker processes.

```bash
vllm-responses serve --gateway-workers 4 --upstream http://127.0.0.1:8457
```

**What this does:**

- Handles more concurrent requests
- Utilizes multiple CPU cores
- Each worker shares the same database

**Compatibility notes:**

- **SQLite:** Works fine with multiple workers on the same machine (uses WAL mode for concurrent access)
- **PostgreSQL:** Required for multiple workers across multiple machines (Kubernetes, multi-VM setups)

______________________________________________________________________

## Response Caching Optimization (Optional)

Add Redis caching to reduce database load for `previous_response_id` lookups.

### Configuration

```bash
export VTOL_RESPONSE_STORE_CACHE=1
export VTOL_REDIS_HOST=localhost
export VTOL_REDIS_PORT=6379
export VTOL_RESPONSE_STORE_CACHE_TTL_SECONDS=3600  # 1 hour

--8<-- "snippets/serve_external_upstream_cmd.txt"
```

### How It Works

Recent responses are cached in Redis. When a request includes `previous_response_id`, the gateway checks Redis first before querying the database. This significantly reduces database load and latency for active conversations.

**Performance impact:**

- Cache hits: fast retrieval
- Reduces database connection pool pressure
- Especially beneficial with PostgreSQL over network

______________________________________________________________________

## MCP Configuration (Optional)

Enable Built-in MCP by providing a runtime config file and setting `VTOL_MCP_CONFIG_PATH`.

### Minimal Setup

```bash
--8<-- "snippets/mcp_enable_config_env.txt"
--8<-- "snippets/serve_external_upstream_cmd.txt"
```

For `mcp.json` examples (URL + stdio styles), see
[MCP Examples -> Built-in MCP Runtime Config](../examples/hosted-mcp-examples.md#built-in-mcp-runtime-config-mcpjson).

### Operational Notes

- If `VTOL_MCP_CONFIG_PATH` is unset, Built-in MCP is disabled.
- With `vllm-responses serve`, Built-in MCP runs in a singleton internal runtime process shared by all gateway workers.
- The supervisor injects `VTOL_MCP_BUILTIN_RUNTIME_URL` for gateway workers automatically.
- Built-in MCP startup and call timeouts are configured globally:
    - `VTOL_MCP_HOSTED_STARTUP_TIMEOUT_SEC`
    - `VTOL_MCP_HOSTED_TOOL_TIMEOUT_SEC`
- Runtime discovery endpoints:
    - `GET /v1/mcp/servers`
    - `GET /v1/mcp/servers/{server_label}/tools`

### Remote MCP Gate

Remote MCP declarations (`tools[].type="mcp"` with `server_url`) are enabled by default.

```bash
export VTOL_MCP_REQUEST_REMOTE_ENABLED=false
```

When disabled, any Remote MCP declaration is rejected as a request-level policy error. Built-in MCP mode is unaffected.

### Remote MCP URL Policy Checks

Gateway URL policy checks for Remote MCP are enabled by default.

```bash
export VTOL_MCP_REQUEST_REMOTE_URL_CHECKS=true
```

Set to `false` to bypass gateway-side URL validation checks.

```bash
export VTOL_MCP_REQUEST_REMOTE_URL_CHECKS=false
```

Warning: disabling URL checks increases SSRF and unsafe-endpoint risk and should only be used in tightly controlled environments.

## Service Architecture Patterns

The gateway can run in different architectural configurations depending on your scaling and operational needs.

### Single-Command Runtime (Default)

The `serve` command runs the gateway with managed local components by default.

```bash
vllm-responses serve -- meta-llama/Llama-3.2-3B-Instruct --port 8457
```

**Components:**

- vLLM subprocess
- Gateway (1+ workers)
- Code interpreter subprocess
- Built-in MCP integration (optional, when `VTOL_MCP_CONFIG_PATH` is set)
    - runs as a singleton loopback runtime process shared by all gateway workers

### Disaggregated

Run each component separately for flexibility and independent scaling.

#### Gateway + External vLLM

Use an existing vLLM deployment or scale inference separately from the gateway.

```bash
# Somewhere else: vLLM is already running
vllm serve meta-llama/Llama-3.2-3B-Instruct --port 8457

# Gateway points to external vLLM
--8<-- "snippets/serve_external_upstream_cmd.txt"
```

**When to use:**

- Separate of inference and gateway
- Using existing vLLM infrastructure
- Avoiding model reload when restarting gateway

______________________________________________________________________

## Configuration Quick Reference

| Configuration             | Command/Environment                               |
| ------------------------- | ------------------------------------------------- |
| **Database (PostgreSQL)** | `export VTOL_DB_PATH="postgresql+asyncpg://..."`  |
| **Multiple workers**      | `--gateway-workers 4`                             |
| **Redis cache**           | `export VTOL_RESPONSE_STORE_CACHE=1`              |
| **Built-in MCP config**   | `export VTOL_MCP_CONFIG_PATH="/path/mcp.json"`    |
| **Remote MCP**            | `export VTOL_MCP_REQUEST_REMOTE_ENABLED=false`    |
| **Remote URL checks**     | `export VTOL_MCP_REQUEST_REMOTE_URL_CHECKS=false` |
| **External vLLM**         | `--upstream http://vllm:8000`                     |

______________________________________________________________________

## Next Steps

- **For complete environment variables:** See [Configuration Reference](../reference/configuration.md)
