import asyncio
import os
import shutil
from asyncio.subprocess import Process
from pathlib import Path
from time import perf_counter

import httpx
from loguru import logger

from vtol.configs import ENV_CONFIG
from vtol.observability.metrics import record_tool_executed
from vtol.tools import register
from vtol.utils.exceptions import BadInputError
from vtol.utils.io import get_async_client

HTTP_ACLIENT = get_async_client()

CODE_INTERPRETER_TOOL = "code_interpreter"


def _get_pyodide_cache_dir() -> str:
    if ENV_CONFIG.pyodide_cache_dir and ENV_CONFIG.pyodide_cache_dir.strip():
        return ENV_CONFIG.pyodide_cache_dir.strip()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".cache"
    return str(base / "vllm-responses" / "pyodide")


def _ensure_executable(path: Path) -> None:
    if os.access(path, os.X_OK):
        return
    try:
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)
    except Exception:
        pass
    if not os.access(path, os.X_OK):
        raise RuntimeError(
            f"Code interpreter binary is not executable: {str(path)!r}. "
            "Try fixing permissions (chmod +x) or reinstalling the package."
        )


def _get_spawn_command(
    *, port: int, workers: int, pyodide_cache_dir: str
) -> tuple[list[str], str]:
    """
    Returns (argv, cwd) for spawning the code interpreter server.

    Policy (v1):
    - Prefer the bundled native executable (Linux x86_64 wheels).
    - Optional dev fallback to `bun src/index.ts` when explicitly enabled.
    """
    code_interpreter_dir = Path(__file__).resolve().parent
    bundled = code_interpreter_dir / "bin" / "linux" / "x86_64" / "code-interpreter-server"
    if bundled.exists():
        _ensure_executable(bundled)
        argv = [
            str(bundled),
            "--port",
            str(port),
            "--pyodide-cache",
            pyodide_cache_dir,
        ]
        if workers > 0:
            argv.extend(["--workers", str(workers)])
        return argv, str(code_interpreter_dir)

    if ENV_CONFIG.code_interpreter_dev_bun_fallback:
        bun_bin = shutil.which("bun")
        if not bun_bin:
            raise RuntimeError(
                "VTOL_CODE_INTERPRETER_DEV_BUN_FALLBACK=1 but `bun` was not found on PATH."
            )
        if not (code_interpreter_dir / "src/index.ts").exists():
            raise RuntimeError(
                "VTOL_CODE_INTERPRETER_DEV_BUN_FALLBACK=1 but TS sources were not found. "
                "Expected `vtol/tools/code_interpreter/src/index.ts`."
            )
        argv = [
            bun_bin,
            "src/index.ts",
            "--port",
            str(port),
            "--pyodide-cache",
            pyodide_cache_dir,
        ]
        if workers > 0:
            argv.extend(["--workers", str(workers)])
        return argv, str(code_interpreter_dir)

    raise RuntimeError(
        "No bundled code interpreter binary was found for this platform/install.\n"
        "If you're running from source, you can set VTOL_CODE_INTERPRETER_DEV_BUN_FALLBACK=1 "
        "and install Bun, or disable the tool via VTOL_CODE_INTERPRETER_MODE=disabled."
    )


async def start_server(*, port: int | None = None, workers: int = 0) -> Process:
    effective_port = ENV_CONFIG.code_interpreter_port if port is None else port
    logger.info(f"Starting code interpreter server on port {effective_port}...")
    pyodide_cache_dir = _get_pyodide_cache_dir()
    command, cwd = _get_spawn_command(
        port=effective_port, workers=workers, pyodide_cache_dir=pyodide_cache_dir
    )
    process: Process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    logger.info(f"Code interpreter server started with PID {process.pid}")

    # The Bun server starts listening only after Pyodide initialization completes.
    # First run may download/extract a large Pyodide tarball, so allow a longer startup window.
    startup_timeout_s = 10 * 60
    deadline = perf_counter() + startup_timeout_s
    attempt = 0

    while perf_counter() < deadline:
        attempt += 1
        if process.returncode is not None:
            stdout, stderr = await process.communicate()
            raise RuntimeError(
                (
                    "Code interpreter server exited during startup.\n"
                    f"exit_code={process.returncode}\n"
                    f"stdout={stdout.decode(errors='replace')}\n"
                    f"stderr={stderr.decode(errors='replace')}"
                )
            )

        try:
            response = await HTTP_ACLIENT.get(
                f"http://localhost:{effective_port}/health", timeout=2.0
            )
            if response.status_code == 200:
                health_data = response.json()
                if health_data.get("pyodide_loaded"):
                    logger.success("Code interpreter server is ready.")
                    return process
                logger.info(f"Code interpreter initializing... (attempt {attempt})")
        except Exception:
            logger.debug(f"Code interpreter not reachable yet... (attempt {attempt})")

        await asyncio.sleep(1.0)

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10.0)
    except Exception:
        process.kill()
        await process.wait()

    raise TimeoutError(
        "Code interpreter server did not become ready before startup timeout "
        f"({startup_timeout_s}s)."
    )


@register(CODE_INTERPRETER_TOOL)
async def run_code(code: str) -> str:
    """
    Execute Python code in a sandboxed WebAssembly environment.

    Pre-loaded packages:
        - Data science: numpy, pandas, matplotlib, scikit-image
        - HTTP requests: requests, httpx, aiohttp
        - Image processing: Pillow, opencv-python
        - Data formats: beautifulsoup4, pyyaml, orjson
        - Math & symbolic: sympy, tiktoken

    Return payload (JSON string):
        {
          "status": "success" | "exception",
          "stdout": "<captured stdout (print output)>",
          "stderr": "<captured stderr>",
          "result": "<final expression display>" | null,
          "execution_time_ms": <int>
        }

    Semantics:
    - `stdout`/`stderr` include everything written during execution (e.g. `print(...)`).
    - `result` is the display value of the final expression (if any). Intermediate bare expressions
      are not returned separately.
    - On Python exceptions, `status="exception"` and `result` contains the exception text (best-effort),
      while `stdout`/`stderr` still reflect any output produced before the failure.

    Args:
        code (str): Python code to execute (single expression, statement(s), or multi-line block).

    Notes:
        In the gateway's Responses API, when `include=["code_interpreter_call.outputs"]` is set, the
        captured stdio and the final expression display (if any) are mapped into
        `code_interpreter_call.outputs` as `{"type":"logs","logs":"..."}` entries.
    """
    if ENV_CONFIG.code_interpreter_mode == "disabled":
        raise BadInputError("`code_interpreter` is disabled by configuration.")
    logger.debug(f"Evaluating code: `{code!r}`")
    url = f"http://localhost:{ENV_CONFIG.code_interpreter_port}/python"
    start = perf_counter()
    errored = False

    # Testability: some gateway tests patch `HTTP_ACLIENT` to an ASGITransport client to avoid binding a real port.
    # Runtime safety: avoid reusing a module-global AsyncClient across pytest event loops (can cause "Event loop is closed").
    transport = getattr(HTTP_ACLIENT, "_transport", None)
    try:
        if isinstance(transport, httpx.ASGITransport):
            response = await HTTP_ACLIENT.post(url, json={"code": code})
        else:
            async with get_async_client() as client:
                response = await client.post(url, json={"code": code})
        return response.text
    except Exception:
        errored = True
        raise
    finally:
        record_tool_executed(
            tool_type="code_interpreter",
            duration_s=perf_counter() - start,
            errored=errored,
        )
