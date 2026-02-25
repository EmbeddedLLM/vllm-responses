# Installation

Get started with `vLLM Responses` by installing the package and its dependencies.

## Prerequisites

- **Python 3.12+**: Ensure you have a compatible Python version installed.
- **uv** (Recommended): We recommend using [uv](https://github.com/astral-sh/uv) for fast, reliable dependency management.
- **tar**: If you use the built-in **Code Interpreter** (enabled by default), the first start may download the Pyodide
    runtime (~400MB) and extract it; `tar` must be available.
- **Bun** (Development): Required for source checkouts if you want the built-in Code Interpreter to work (enabled by
    default). Wheels for Linux x86_64 bundle a native Code Interpreter binary and do not require Bun.
- **(Recommended) vLLM**: If you plan to spawn vLLM directly from the gateway, you'll need [`vllm`](https://docs.vllm.ai/en/latest/getting_started/installation/) installed.

## Install the CLI

We recommend setting up a virtual environment using `uv`.

### Install from a prebuilt wheel (Linux x86_64) (Recommended)

Download a prebuilt wheel (`vllm_responses-*.whl`) from GitHub Releases (preferred) or a CI run artifact, then install it:

```bash
uv venv --python=3.12
source .venv/bin/activate
uv pip install path/to/vllm_responses-*.whl
vllm-responses --help
```

On Linux x86_64 wheels, the Code Interpreter server binary is bundled, so **Bun is not required**.

!!! note "Non-Linux platforms"

    The gateway is a Python service and can run on other platforms, but the bundled Code Interpreter binary is currently
    only shipped in Linux x86_64 wheels. On other platforms, either disable the tool via `--code-interpreter disabled`,
    or run from a source checkout and use the (development-only) Bun fallback.

### Install from source (repo checkout)

If you are working from a source checkout and want the gateway to work with the default configuration (Code Interpreter
enabled), use the Bun fallback:

```bash
git clone https://github.com/EmbeddedLLM/vllm-responses
cd vllm-responses

uv venv --python=3.12
source .venv/bin/activate
uv pip install -e ./responses

cd responses/python/vtol/tools/code_interpreter
bun install
export VTOL_CODE_INTERPRETER_DEV_BUN_FALLBACK=1
cd -

vllm-responses --help
```

### First start: Pyodide download (Code Interpreter)

If `code_interpreter` is enabled (default), the first start may download the Pyodide runtime (~400MB) into a cache
directory and extract it. Subsequent starts reuse the cache.

- Default cache: `${XDG_CACHE_HOME:-$HOME/.cache}/vllm-responses/pyodide`
- Override: set `VTOL_PYODIDE_CACHE_DIR` to a persistent directory with enough free disk space.

## Optional dependency sets

Some features require additional optional dependencies.

### OpenTelemetry tracing (optional)

If you want to enable OpenTelemetry tracing (`VTOL_TRACING_ENABLED=true`), install with the `tracing` extra.

### Documentation toolchain (contributors)

If you want to build/serve the MkDocs site locally, install with the `docs` extra.
