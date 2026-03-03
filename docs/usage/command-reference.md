# Command Reference

Comprehensive reference for the `vllm-responses` CLI.

## Synopsis

```bash
vllm-responses serve [OPTIONS] [-- vllm_args ...]
```

## Description

The `vllm-responses serve` command acts as a supervisor. It can run in two primary modes:

1. **External Upstream**: Connects to an existing, already-running vLLM server.
1. **Spawn vLLM**: Starts and manages a vLLM process as a subprocess.

It also manages:

1. the **Code Interpreter** runtime (unless disabled),
1. the singleton **Built-in MCP** runtime process (when `VR_MCP_CONFIG_PATH` is set).

______________________________________________________________________

## Options

### Upstream Configuration

These options control where the gateway finds the inference server.

#### `--upstream URL`

**Description**: Base URL of an external OpenAI-compatible server. **Default**: `None` **Example**: `--upstream http://127.0.0.1:8457` **Notes**: If this is set, the gateway will **not** spawn vLLM. The `/v1` suffix is optional; the gateway normalizes it automatically.

### Gateway Configuration

These options control the `vllm-responses` server itself.

#### `--gateway-host HOST`

**Description**: The interface to bind the gateway server to. **Default**: `0.0.0.0` (See `VR_HOST`)

#### `--gateway-port PORT`

**Description**: The port to listen on. **Default**: `5969` (See `VR_PORT`)

#### `--gateway-workers N`

**Description**: Number of Gunicorn workers to spawn. **Default**: `1` (See `VR_WORKERS`) **Notes**: For production, use multiple workers (e.g., `2 * CPU_CORES + 1`).

### vLLM Spawning

These options apply only when **not** using `--upstream`. Everything after `--` is forwarded to `vllm serve`.

#### `--vllm-startup-timeout SECONDS`

**Description**: Maximum time to wait for vLLM to become ready. **Default**: `1800` (30 minutes)

#### `--vllm-ready-interval SECONDS`

**Description**: How often to poll the vLLM health endpoint during startup. **Default**: `5`

### Code Interpreter Configuration

#### `--code-interpreter MODE`

**Description**: Runtime policy for the code interpreter. **Default**: `spawn` **Values**:

- `spawn`: The `vllm-responses serve` supervisor starts and manages the Bun/Pyodide server, then wires gateway workers to it.
- `external`: Connects to an already-running server (supervisor does not spawn one).
- `disabled`: Disables the tool entirely.

!!! note "Developer-only fallback"

    On platforms without a bundled Code Interpreter binary (or when running from a source checkout), you can allow a
    Bun-based fallback by setting `VR_CODE_INTERPRETER_DEV_BUN_FALLBACK=1`. This is intended for development.

#### `--code-interpreter-port PORT`

**Description**: Port for the code interpreter server. **Default**: `5970`

#### `--code-interpreter-workers N`

**Description**: Worker pool size for the code interpreter service when `--code-interpreter=spawn`. **Default**: `0` (in-process; no workers) **Notes**:

- This uses a [Bun Worker](https://bun.com/docs/runtime/workers) pool.
- Use `2+` for actual parallelism. `1` enables worker mode but does not increase throughput.
- Each worker loads its own Pyodide runtime, so increasing workers increases RAM and startup time.

#### `--code-interpreter-startup-timeout SECONDS`

**Description**: Maximum time to wait for the code interpreter to become ready. **Default**: `600` (10 minutes)

______________________________________________________________________

## Configuration Precedence

`vllm-responses serve` resolves config in this order:

1. CLI flags
1. Environment variables (with `os.environ` taking precedence over `.env`)
1. Built-in defaults

For scalar values (ports, workers, timeouts), precedence is presence-based:

- If a CLI arg is provided, it wins even when the value is falsy (for example `0` or `0.0`).
- Env/default fallback is only used when a CLI arg is absent.

Built-in MCP runtime configuration is environment-only in this command:

- Set `VR_MCP_CONFIG_PATH=/path/to/mcp.json` to enable Built-in MCP.
- There is currently no dedicated `serve` CLI flag for MCP config path.
- `VR_MCP_BUILTIN_RUNTIME_URL` is the single runtime-address knob (default `http://127.0.0.1:5981` when unset).
- When enabled, `serve` starts one loopback Built-in MCP runtime and injects `VR_MCP_BUILTIN_RUNTIME_URL` into gateway workers.
- Set `VR_MCP_BUILTIN_RUNTIME_URL` only when you need a different loopback port/host in `serve`, or when manually wiring workers to a separately managed runtime.

Upstream selection precedence:

1. `--upstream` (external upstream; `/v1` normalized). Error if used together with `--`.
1. `-- <vllm args...>` (spawn vLLM; ignores `VR_LLM_API_BASE` with a notice).
1. `VR_LLM_API_BASE` (external upstream from env / `.env`).
1. Otherwise: configuration error ("no upstream configured").

## Examples

### Connect to External Server

```bash
--8<-- "snippets/serve_external_upstream_cmd.txt"
```

### Spawn vLLM (Simple)

```bash
vllm-responses serve -- \
  meta-llama/Llama-3.2-3B-Instruct
```

### Spawn vLLM (Custom Configuration)

Spawn with 4 gateway workers, 4 vLLM GPUs, and a custom port.

```bash
vllm-responses serve --gateway-workers 4 -- \
  meta-llama/Llama-3.2-3B-Instruct \
  --tensor-parallel-size 4 \
  --port 9000
```

Notes:

- To change where the spawned vLLM process binds, pass `--host`/`--port` after `--` (they are forwarded to
    `vllm serve`).
