# Configuration Guide

Configure the gateway's database, caching, workers, and service architecture for your deployment needs.

## Overview

This guide covers configuration options for:

- **Storage backend** (SQLite vs PostgreSQL)
- **Worker processes** (single vs multiple)
- **Response caching optimization** (optional Redis integration)
- **Service architecture** (all-in-one vs disaggregated)

For complete environment variable reference, see [Configuration Reference](../reference/configuration.md).

## Storage Backend

The gateway stores conversation state for `previous_response_id` functionality. Choose the storage backend that fits your deployment model.

### SQLite (Default)

Zero-configuration storage using a local SQLite database file.

```bash
# Default - no configuration needed
vllm-responses serve --upstream http://127.0.0.1:8457
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
vllm-responses serve --upstream http://127.0.0.1:8457
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
vllm-responses serve --upstream http://127.0.0.1:8457
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

vllm-responses serve --upstream http://127.0.0.1:8457
```

### How It Works

Recent responses are cached in Redis. When a request includes `previous_response_id`, the gateway checks Redis first before querying the database. This significantly reduces database load and latency for active conversations.

**Performance impact:**

- Cache hits: fast retrieval
- Reduces database connection pool pressure
- Especially beneficial with PostgreSQL over network

______________________________________________________________________

## Service Architecture Patterns

The gateway can run in different architectural configurations depending on your scaling and operational needs.

### All-in-One (Default)

The `serve` command spawns everything: vLLM, gateway, and code interpreter.

```bash
vllm-responses serve -- meta-llama/Llama-3.2-3B-Instruct --port 8457
```

**Components:**

- vLLM subprocess
- Gateway (1+ workers)
- Code interpreter subprocess

### Disaggregated

Run each component separately for flexibility and independent scaling.

#### Gateway + External vLLM

Use an existing vLLM deployment or scale inference separately from the gateway.

```bash
# Somewhere else: vLLM is already running
vllm serve meta-llama/Llama-3.2-3B-Instruct --port 8457

# Gateway points to external vLLM
vllm-responses serve --upstream http://127.0.0.1:8457
```

**When to use:**

- Separate of inference and gateway
- Using existing vLLM infrastructure
- Avoiding model reload when restarting gateway

______________________________________________________________________

## Configuration Quick Reference

| Configuration             | Command/Environment                              |
| ------------------------- | ------------------------------------------------ |
| **Database (PostgreSQL)** | `export VTOL_DB_PATH="postgresql+asyncpg://..."` |
| **Multiple workers**      | `--gateway-workers 4`                            |
| **Redis cache**           | `export VTOL_RESPONSE_STORE_CACHE=1`             |
| **External vLLM**         | `--upstream http://vllm:8000`                    |

______________________________________________________________________

## Next Steps

- **For complete environment variables:** See [Configuration Reference](../reference/configuration.md)
