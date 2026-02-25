import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from vtol.tools.code_interpreter import run_code, start_server

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
async def code_interpreter_server() -> None:
    cache_dir = os.environ.get("VTOL_PYODIDE_CACHE_DIR", "").strip()
    if cache_dir:
        cache_path = Path(os.path.expanduser(cache_dir))
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        if xdg:
            base = Path(os.path.expanduser(xdg))
        else:
            base = Path.home() / ".cache"
        cache_path = base / "vllm-responses" / "pyodide"

    marker = cache_path / ".pyodide_version"
    if not marker.exists():
        repo_root = Path(__file__).resolve().parents[2]
        bootstrap = repo_root / "scripts" / "ci" / "bootstrap_pyodide_cache.py"
        raise RuntimeError(
            "Pyodide cache is not initialized. The code interpreter tests require Pyodide to be installed "
            "ahead of time (we do not auto-download ~400MB during tests).\n\n"
            "Bootstrap it with:\n"
            f"  VTOL_PYODIDE_CACHE_DIR={str(cache_path)!r} {sys.executable} {bootstrap}\n"
        )

    # Use multiple workers to cover the WorkerPool path (when supported by the runtime).
    process = await start_server(port=5970, workers=2)
    try:
        yield process
    finally:
        if process:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=10.0)
                # print("Graceful termination")
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                # print("Kill termination")
            except Exception as e:
                print(f"Error stopping code interpreter server: {repr(e)}")
        await asyncio.wait_for(process.wait(), timeout=10.0)


async def test_code_interpreter_numpy(code_interpreter_server) -> None:
    response = json.loads(await run_code("import numpy as np; np.array([1,2,3]).mean()"))
    assert response["status"] == "success"
    assert response["result"] == "2"
    assert response["stdout"] == ""
    assert response["stderr"] == ""


async def test_code_interpreter_ctypes_patch(code_interpreter_server) -> None:
    response = json.loads(await run_code('import ctypes; ctypes.CDLL(None).system(b"whoami")'))
    assert response["status"] == "exception"
    assert "'NoneType' object is not callable" in response["result"]


async def test_code_interpreter_captures_print_stdout(code_interpreter_server) -> None:
    response = json.loads(await run_code('print("P1"); print("P2"); 2+2'))
    assert response["status"] == "success"
    assert response["stdout"] == "P1\nP2\n"
    assert response["stderr"] == ""
    assert response["result"] == "4"


async def test_code_interpreter_base_eval_patch(code_interpreter_server) -> None:
    response = json.loads(
        await run_code(
            'import _pyodide; _pyodide._base.eval_code("import os; os.system(\\"whoami\\")")'
        )
    )
    assert response["status"] == "success"
    assert 'import os; os.system("whoami")' in response["result"]
