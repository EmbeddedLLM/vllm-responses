from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass
from pathlib import Path

from vtol.entrypoints.serve_utils import (
    EnvLookup,
    find_flag_value,
    get_pyodide_cache_dir,
    is_ready_url_host,
    normalize_upstream,
)


class ServeSpecError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = int(exit_code)


@dataclass(frozen=True, slots=True)
class GatewaySpec:
    host: str
    port: int
    workers: int


@dataclass(frozen=True, slots=True)
class TimeoutSpec:
    vllm_startup_timeout_s: float
    vllm_ready_interval_s: float
    code_interpreter_startup_timeout_s: float


@dataclass(frozen=True, slots=True)
class MetricsSpec:
    enabled: bool


@dataclass(frozen=True, slots=True)
class ExternalUpstreamSpec:
    base_url: str
    ready_url: str
    headers: dict[str, str] | None


@dataclass(frozen=True, slots=True)
class SpawnVllmSpec:
    cmd: list[str]
    bind_host: str
    bind_port: int
    ready_url: str
    headers: dict[str, str] | None


UpstreamSpec = ExternalUpstreamSpec | SpawnVllmSpec


@dataclass(frozen=True, slots=True)
class DisabledCodeInterpreterSpec:
    pass


@dataclass(frozen=True, slots=True)
class ExternalCodeInterpreterSpec:
    port: int
    ready_url: str


@dataclass(frozen=True, slots=True)
class SpawnCodeInterpreterSpec:
    cmd: list[str]
    cwd: Path
    port: int
    workers: int
    ready_url: str


CodeInterpreterSpec = (
    DisabledCodeInterpreterSpec | ExternalCodeInterpreterSpec | SpawnCodeInterpreterSpec
)


@dataclass(frozen=True, slots=True)
class ServeSpec:
    notices: list[str]
    gateway: GatewaySpec
    upstream: UpstreamSpec
    code_interpreter: CodeInterpreterSpec
    code_interpreter_workers: int
    metrics: MetricsSpec
    timeouts: TimeoutSpec


def _code_interpreter_dir_from_spec() -> Path:
    spec = importlib.util.find_spec("vtol.tools.code_interpreter")
    if spec is None or not spec.submodule_search_locations:
        raise ServeSpecError(
            "[serve] error: failed to locate `vtol.tools.code_interpreter` package data. "
            "This installation may be incomplete. Try reinstalling `vllm-responses`.",
            exit_code=2,
        )
    return Path(spec.submodule_search_locations[0]).resolve()


def _build_code_interpreter_spawn_spec(
    *,
    env: EnvLookup,
    port: int,
    workers: int,
) -> SpawnCodeInterpreterSpec:
    code_interpreter_dir = _code_interpreter_dir_from_spec()
    pyodide_cache_dir = get_pyodide_cache_dir(env)

    ci_bin = code_interpreter_dir / "bin" / "linux" / "x86_64" / "code-interpreter-server"
    cmd: list[str] | None = None

    if ci_bin.exists():
        cmd = [
            str(ci_bin),
            "--port",
            str(port),
            "--pyodide-cache",
            pyodide_cache_dir,
        ]
        if workers > 0:
            cmd.extend(["--workers", str(workers)])
    else:
        dev_bun_fallback = env.get_bool("VTOL_CODE_INTERPRETER_DEV_BUN_FALLBACK", False)
        if dev_bun_fallback and (code_interpreter_dir / "src/index.ts").exists():
            bun_bin = shutil.which("bun")
            if not bun_bin:
                raise ServeSpecError(
                    "[serve] error: VTOL_CODE_INTERPRETER_DEV_BUN_FALLBACK=1 but `bun` was not found on PATH.",
                    exit_code=2,
                )
            cmd = [
                bun_bin,
                "src/index.ts",
                "--port",
                str(port),
                "--pyodide-cache",
                pyodide_cache_dir,
            ]
            if workers > 0:
                cmd.extend(["--workers", str(workers)])
        else:
            raise ServeSpecError(
                "[serve] error: no bundled code-interpreter binary was found for this platform.\n"
                "  - On Linux x86_64 PyPI wheels, this should be present.\n"
                "  - For source checkouts, you can set VTOL_CODE_INTERPRETER_DEV_BUN_FALLBACK=1 and install Bun.\n"
                "  - Or disable the tool via --code-interpreter=disabled.\n",
                exit_code=2,
            )

    return SpawnCodeInterpreterSpec(
        cmd=cmd,
        cwd=code_interpreter_dir,
        port=port,
        workers=workers,
        ready_url=f"http://localhost:{port}/health",
    )


def build_serve_spec(
    args,
    vllm_argv: list[str],
    *,
    had_delimiter: bool,
    env: EnvLookup | None = None,
) -> ServeSpec:
    """
    Build a pure description of what `vllm-responses serve` should run.

    This function must remain side-effect free: it should not spawn subprocesses, create
    directories, or perform network calls.
    """
    env = EnvLookup.from_cwd() if env is None else env
    notices: list[str] = []

    upstream_env, upstream_env_set = env.get("VTOL_LLM_API_BASE")
    upstream_flag = getattr(args, "upstream", None)

    if upstream_flag is not None and had_delimiter:
        raise ServeSpecError("[serve] error: `--upstream` cannot be used with `--`.", exit_code=2)

    gateway_host_arg = getattr(args, "gateway_host", None)
    gateway_host = (
        gateway_host_arg if gateway_host_arg is not None else env.get_str("VTOL_HOST", "0.0.0.0")
    )
    gateway_port_arg = getattr(args, "gateway_port", None)
    gateway_port = (
        gateway_port_arg if gateway_port_arg is not None else env.get_int("VTOL_PORT", 5969)
    )
    gateway_workers_arg = getattr(args, "gateway_workers", None)
    gateway_workers = (
        gateway_workers_arg if gateway_workers_arg is not None else env.get_int("VTOL_WORKERS", 1)
    )
    metrics_enabled = env.get_bool("VTOL_METRICS_ENABLED", True)

    ci_mode_arg = getattr(args, "code_interpreter", None)
    ci_mode = (
        ci_mode_arg
        if ci_mode_arg is not None
        else env.get_str("VTOL_CODE_INTERPRETER_MODE", "spawn")
    )
    if ci_mode not in {"spawn", "external", "disabled"}:
        raise ServeSpecError(
            "[serve] error: invalid code interpreter mode. "
            "use --code-interpreter {spawn,external,disabled}.",
            exit_code=2,
        )
    ci_port_arg = getattr(args, "code_interpreter_port", None)
    ci_port = (
        ci_port_arg if ci_port_arg is not None else env.get_int("VTOL_CODE_INTERPRETER_PORT", 5970)
    )
    ci_workers_arg = getattr(args, "code_interpreter_workers", None)
    ci_workers = (
        ci_workers_arg
        if ci_workers_arg is not None
        else env.get_int("VTOL_CODE_INTERPRETER_WORKERS", 0)
    )

    vllm_startup_timeout_arg = getattr(args, "vllm_startup_timeout", None)
    vllm_startup_timeout_s = (
        vllm_startup_timeout_arg if vllm_startup_timeout_arg is not None else 30 * 60
    )
    vllm_ready_interval_arg = getattr(args, "vllm_ready_interval", None)
    vllm_ready_interval_s = vllm_ready_interval_arg if vllm_ready_interval_arg is not None else 5.0
    ci_startup_timeout_arg = getattr(args, "code_interpreter_startup_timeout", None)
    ci_startup_timeout_s = (
        ci_startup_timeout_arg if ci_startup_timeout_arg is not None else 10 * 60
    )

    openai_key, openai_key_set = env.get("VTOL_OPENAI_API_KEY")
    upstream_headers: dict[str, str] | None = None
    if openai_key_set and openai_key:
        upstream_headers = {"Authorization": f"Bearer {openai_key}"}

    upstream: UpstreamSpec
    if upstream_flag is not None:
        upstream_base_url = normalize_upstream(upstream_flag)
        if upstream_env_set and upstream_env and upstream_env != upstream_base_url:
            notices.append(
                f"[serve] notice: ignoring VTOL_LLM_API_BASE={upstream_env!r} in favor of --upstream."
            )
        upstream = ExternalUpstreamSpec(
            base_url=upstream_base_url,
            ready_url=f"{upstream_base_url}/models",
            headers=upstream_headers,
        )
    elif vllm_argv:
        if upstream_env_set and upstream_env:
            notices.append(
                "[serve] notice: `--` was provided; ignoring VTOL_LLM_API_BASE in favor of spawning vLLM."
            )

        model = vllm_argv[0]
        if model.startswith("-"):
            raise ServeSpecError(
                "[serve] error: first arg after `--` must be <MODEL_ID_OR_PATH>.",
                exit_code=2,
            )

        vllm_bin = shutil.which("vllm")
        if not vllm_bin:
            raise ServeSpecError(
                "[serve] error: `vllm` was not found on PATH. Install vLLM or provide --upstream.",
                exit_code=2,
            )

        vllm_bind_host = find_flag_value(vllm_argv, "--host") or "127.0.0.1"
        vllm_port_str = find_flag_value(vllm_argv, "--port")
        vllm_bind_port = int(vllm_port_str) if vllm_port_str else 8457

        vllm_cmd = [vllm_bin, "serve", *vllm_argv]
        if find_flag_value(vllm_argv, "--host") is None:
            vllm_cmd.extend(["--host", vllm_bind_host])
        if find_flag_value(vllm_argv, "--port") is None:
            vllm_cmd.extend(["--port", str(vllm_bind_port)])

        connect_host = is_ready_url_host(vllm_bind_host)
        upstream = SpawnVllmSpec(
            cmd=vllm_cmd,
            bind_host=vllm_bind_host,
            bind_port=vllm_bind_port,
            ready_url=f"http://{connect_host}:{vllm_bind_port}/v1/models",
            headers=upstream_headers,
        )
    elif had_delimiter:
        raise ServeSpecError(
            "[serve] error: `--` requires at least <MODEL_ID_OR_PATH>.", exit_code=2
        )
    elif upstream_env_set and upstream_env:
        upstream_base_url = normalize_upstream(upstream_env)
        upstream = ExternalUpstreamSpec(
            base_url=upstream_base_url,
            ready_url=f"{upstream_base_url}/models",
            headers=upstream_headers,
        )
    else:
        raise ServeSpecError(
            "[serve] error: no upstream configured. Provide one of:\n"
            "  - `--upstream http://host:port/v1` (external upstream)\n"
            "  - `VTOL_LLM_API_BASE=...` (external upstream via env/.env)\n"
            "  - `-- <MODEL_ID_OR_PATH> ...` (spawn vLLM)\n",
            exit_code=2,
        )

    if ci_mode == "disabled":
        code_interpreter: CodeInterpreterSpec = DisabledCodeInterpreterSpec()
    elif ci_mode == "external":
        code_interpreter = ExternalCodeInterpreterSpec(
            port=ci_port,
            ready_url=f"http://localhost:{ci_port}/health",
        )
    elif ci_mode == "spawn":
        code_interpreter = _build_code_interpreter_spawn_spec(
            env=env, port=ci_port, workers=ci_workers
        )
    else:
        raise AssertionError(f"unreachable ci_mode: {ci_mode!r}")

    return ServeSpec(
        notices=notices,
        gateway=GatewaySpec(
            host=gateway_host, port=int(gateway_port), workers=int(gateway_workers)
        ),
        upstream=upstream,
        code_interpreter=code_interpreter,
        code_interpreter_workers=int(ci_workers),
        metrics=MetricsSpec(enabled=bool(metrics_enabled)),
        timeouts=TimeoutSpec(
            vllm_startup_timeout_s=float(vllm_startup_timeout_s),
            vllm_ready_interval_s=float(vllm_ready_interval_s),
            code_interpreter_startup_timeout_s=float(ci_startup_timeout_s),
        ),
    )
