# vLLM Responses

FastAPI gateway that exposes an OpenAI-style **Responses API** (`/v1/responses`) in front of a vLLM **OpenAI-compatible** server (`/v1/chat/completions`), with:

- SSE streaming event shape + ordering
- `previous_response_id` statefulness (ResponseStore)
- gateway-executed built-in tool: `code_interpreter`

**[📚 Full User Documentation](docs/index.md)** (Guides, API Reference, Examples)

Design docs (maintainer-facing): `design_docs/index.md`.

## Install

The `vllm-responses` CLI is provided by the Python package in `responses/`.

**Prerequisites:** Python 3.12+ and `uv`.

### Install from a prebuilt wheel (Linux x86_64) (Recommended)

Download a prebuilt wheel (`vllm_responses-*.whl`) from GitHub Releases (preferred) or a CI run artifact, then install it:

```bash
uv venv --python=3.12
source .venv/bin/activate
uv pip install path/to/vllm_responses-*.whl
```

On Linux x86_64 wheels, the Code Interpreter server binary is bundled, so **Bun is not required**.
Currently, wheels are only built for Linux x86_64.

### Install from source (repo checkout) (Development)

```bash
git clone https://github.com/EmbeddedLLM/vllm-responses
cd vllm-responses

uv venv --python=3.12
source .venv/bin/activate
uv pip install -e ./responses

# Development: enable Code Interpreter via Bun fallback
# - Required for source checkouts when running with `code_interpreter` enabled (default)
cd responses/python/vtol/tools/code_interpreter
bun install
export VTOL_CODE_INTERPRETER_DEV_BUN_FALLBACK=1
cd -

vllm-responses --help
```

Verify installation:

```bash
vllm-responses --help
```

### Optional dependency sets (extras)

Install any combination via:

```bash
uv pip install -e './responses[<extra1>,<extra2>]'
```

Available extras:

- `docs`: MkDocs toolchain (contributors).
- `lint`: Ruff + Markdown formatting.
- `test`: Pytest + coverage + load testing tools.
- `tracing`: OpenTelemetry tracing support (only needed if you enable `VTOL_TRACING_ENABLED=true`).
- `build`: Package build/publish tools.
- `all`: Everything above.

## Run

### one-command local runtime (`vllm-responses serve`)

Prereqs:

- If you want to spawn vLLM: `vllm` must be installed (e.g. `uv pip install vllm`).
- If `code_interpreter` is enabled (default), the first start may download the Pyodide runtime (~400MB) into a cache
  directory (see `VTOL_PYODIDE_CACHE_DIR`). This requires `tar` to be installed.
- For non-Linux platforms (or source installs without the bundled binary), you can disable the tool via
  `--code-interpreter disabled`. For development you can also enable the Bun-based fallback via
  `VTOL_CODE_INTERPRETER_DEV_BUN_FALLBACK=1`.

External upstream (you start vLLM yourself; `/v1` is optional):

```bash
vllm-responses serve --upstream http://127.0.0.1:8457
```

Spawn vLLM (everything after `--` is forwarded to `vllm serve`):

```bash
vllm-responses serve --gateway-workers 4 -- \
  meta-llama/Llama-3.2-3B-Instruct \
  --dtype auto \
  --port 8457
```

The Responses endpoint is:

- `POST http://127.0.0.1:5969/v1/responses`

Remote access note:

- If you bind the gateway with `--gateway-host 0.0.0.0`, use the machine’s IP/hostname to connect (not `0.0.0.0`).

### Optional: ResponseStore hot cache (Redis)

`previous_response_id` hydration reads the previous response state from the DB. For multi-worker deployments, you can optionally enable a Redis-backed hot cache to reduce DB reads/latency.

Env vars (default off):

- `VTOL_RESPONSE_STORE_CACHE=1`
- `VTOL_RESPONSE_STORE_CACHE_TTL_SECONDS=3600`

Redis connection:

- `VTOL_REDIS_HOST`, `VTOL_REDIS_PORT`

## Quick smoke test (OpenAI Python SDK)

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:5969/v1", api_key="dummy")

with client.responses.stream(
    model="MiniMaxAI/MiniMax-M2.1",
    input=[{"role": "user", "content": "You MUST call the code_interpreter tool. Execute: 2+2. Reply with ONLY the number."}],
    tools=[{"type": "code_interpreter"}],
    tool_choice="auto",
    include=["code_interpreter_call.outputs"],
) as stream:
    for evt in stream:
        if getattr(evt, "type", "").endswith(".delta"):
            continue
        print(getattr(evt, "type", evt))
    r1 = stream.get_final_response().id

with client.responses.stream(
    model="MiniMaxAI/MiniMax-M2.1",
    previous_response_id=r1,
    input=[{"role": "user", "content": "What number did you just compute? Reply with ONLY the number."}],
    tool_choice="none",
) as stream:
    for evt in stream:
        if getattr(evt, "type", "").endswith(".delta"):
            continue
        print(getattr(evt, "type", evt))
```
