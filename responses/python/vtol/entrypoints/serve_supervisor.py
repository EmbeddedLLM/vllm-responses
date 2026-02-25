from __future__ import annotations

import asyncio
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from vtol.entrypoints.serve_spec import (
    DisabledCodeInterpreterSpec,
    ExternalCodeInterpreterSpec,
    ExternalUpstreamSpec,
    ServeSpec,
    SpawnCodeInterpreterSpec,
    SpawnVllmSpec,
)
from vtol.entrypoints.serve_utils import (
    cleanup_prometheus_multiproc_dir,
    cleanup_stale_prometheus_multiproc_dirs,
    create_prometheus_multiproc_dir,
    is_port_available,
    is_ready_url_host,
    stream_lines,
    terminate_process,
    wait_http_ready,
)


def run_serve_spec(spec: ServeSpec) -> int:
    procs: list[tuple[str, subprocess.Popen[str]]] = []
    cleaned_up = False
    prometheus_multiproc_dir: Path | None = None

    def _cleanup() -> None:
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        for name, proc in reversed(procs):
            terminate_process(proc, name=name)
        if prometheus_multiproc_dir is not None:
            cleanup_prometheus_multiproc_dir(prometheus_multiproc_dir)

    previous_signal_handlers: dict[int, object] = {}

    def _install_signal_handlers() -> None:
        def _handler(signum: int, frame) -> None:  # type: ignore[no-untyped-def]
            try:
                sig_name = signal.Signals(signum).name
            except Exception:
                sig_name = str(signum)
            print(f"[serve] received {sig_name}. shutting down.", file=sys.stderr)
            raise SystemExit(128 + signum)

        for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if sig is None:
                continue
            previous_signal_handlers[int(sig)] = signal.getsignal(sig)
            signal.signal(sig, _handler)

    def _restore_signal_handlers() -> None:
        for signum, handler in previous_signal_handlers.items():
            try:
                signal.signal(signum, handler)  # type: ignore[arg-type]
            except Exception:
                continue

    try:
        _install_signal_handlers()

        for line in spec.notices:
            print(line, file=sys.stderr)

        if spec.metrics.enabled:
            cleanup_stale_prometheus_multiproc_dirs()
            prometheus_multiproc_dir = create_prometheus_multiproc_dir(supervisor_pid=os.getpid())

        # --- Ensure DB schema exists once (multi-worker safe) ---
        try:
            from vtol.responses_core.store import get_default_response_store

            asyncio.run(get_default_response_store().ensure_schema())
        except Exception as e:
            print(f"[serve] error: failed to initialize DB schema: {e!r}", file=sys.stderr)
            return 2

        # --- Preflight port checks (best-effort, fail fast) ---
        if not is_port_available(spec.gateway.host, spec.gateway.port):
            print(
                f"[serve] error: gateway port already in use: {spec.gateway.host}:{spec.gateway.port}",
                file=sys.stderr,
            )
            print("[serve] hint: choose another port via --gateway-port.", file=sys.stderr)
            return 2

        if isinstance(spec.code_interpreter, SpawnCodeInterpreterSpec):
            if not is_port_available("127.0.0.1", spec.code_interpreter.port):
                print(
                    "[serve] error: code-interpreter port already in use: "
                    f"127.0.0.1:{spec.code_interpreter.port}",
                    file=sys.stderr,
                )
                print(
                    "[serve] hint: choose another port via --code-interpreter-port.",
                    file=sys.stderr,
                )
                return 2

        if isinstance(spec.upstream, SpawnVllmSpec):
            if not is_port_available(spec.upstream.bind_host, spec.upstream.bind_port):
                print(
                    "[serve] error: vLLM port already in use: "
                    f"{spec.upstream.bind_host}:{spec.upstream.bind_port}",
                    file=sys.stderr,
                )
                print(
                    "[serve] hint: pass a different vLLM --port after `--`.",
                    file=sys.stderr,
                )
                return 2

        upstream_base_url: str
        if isinstance(spec.upstream, ExternalUpstreamSpec):
            wait_http_ready(
                name="upstream",
                url=spec.upstream.ready_url,
                timeout_s=spec.timeouts.vllm_startup_timeout_s,
                interval_s=spec.timeouts.vllm_ready_interval_s,
                headers=spec.upstream.headers,
            )
            upstream_base_url = spec.upstream.base_url
            print(f"[serve] upstream ready: {upstream_base_url}", file=sys.stderr)
        elif isinstance(spec.upstream, SpawnVllmSpec):
            print(f"[serve] starting vLLM: {shlex.join(spec.upstream.cmd)}", file=sys.stderr)
            vllm_proc = subprocess.Popen(
                spec.upstream.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            procs.append(("vllm", vllm_proc))
            if vllm_proc.stdout is not None:
                threading.Thread(
                    target=stream_lines, args=("vllm| ", vllm_proc.stdout), daemon=True
                ).start()

            try:
                wait_http_ready(
                    name="vLLM",
                    url=spec.upstream.ready_url,
                    timeout_s=spec.timeouts.vllm_startup_timeout_s,
                    interval_s=spec.timeouts.vllm_ready_interval_s,
                    headers=spec.upstream.headers,
                    abort_proc=vllm_proc,
                )
            except Exception:
                code = vllm_proc.poll()
                if code is not None:
                    print(
                        f"[serve] vLLM exited during startup (code={code}). shutting down.",
                        file=sys.stderr,
                    )
                    return code or 1
                raise
            upstream_base_url = (
                f"http://{is_ready_url_host(spec.upstream.bind_host)}:{spec.upstream.bind_port}/v1"
            )
            print(f"[serve] vLLM ready: upstream={upstream_base_url}", file=sys.stderr)
        else:
            raise AssertionError(f"unreachable upstream spec: {type(spec.upstream)!r}")

        if isinstance(spec.code_interpreter, DisabledCodeInterpreterSpec):
            pass
        else:
            bun_proc: subprocess.Popen[str] | None = None
            if isinstance(spec.code_interpreter, SpawnCodeInterpreterSpec):
                print(
                    f"[serve] starting code interpreter: {shlex.join(spec.code_interpreter.cmd)}",
                    file=sys.stderr,
                )
                bun_proc = subprocess.Popen(
                    spec.code_interpreter.cmd,
                    cwd=str(spec.code_interpreter.cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                procs.append(("code-interpreter", bun_proc))
                if bun_proc.stdout is not None:
                    threading.Thread(
                        target=stream_lines,
                        args=("code-interpreter| ", bun_proc.stdout),
                        daemon=True,
                    ).start()
            elif isinstance(spec.code_interpreter, ExternalCodeInterpreterSpec):
                bun_proc = None
            else:
                raise AssertionError(
                    f"unreachable code_interpreter spec: {type(spec.code_interpreter)!r}"
                )

            try:
                wait_http_ready(
                    name="code-interpreter",
                    url=spec.code_interpreter.ready_url,
                    timeout_s=spec.timeouts.code_interpreter_startup_timeout_s,
                    interval_s=5.0,
                    check_json=lambda payload: isinstance(payload, dict)
                    and bool(payload.get("pyodide_loaded")),
                    abort_proc=bun_proc,
                )
            except Exception:
                if bun_proc is not None:
                    code = bun_proc.poll()
                    if code is not None:
                        print(
                            f"[serve] code interpreter exited during startup (code={code}). shutting down.",
                            file=sys.stderr,
                        )
                        return code or 1
                raise
            mode = (
                "spawn"
                if isinstance(spec.code_interpreter, SpawnCodeInterpreterSpec)
                else "external"
            )
            print(
                f"[serve] code interpreter ready: mode={mode} port={spec.code_interpreter.port}",
                file=sys.stderr,
            )

        gateway_env = dict(os.environ)
        gateway_env["VTOL_LLM_API_BASE"] = upstream_base_url
        gateway_env["VTOL_HOST"] = spec.gateway.host
        gateway_env["VTOL_PORT"] = str(spec.gateway.port)
        gateway_env["VTOL_WORKERS"] = str(spec.gateway.workers)
        gateway_env["VTOL_DB_SCHEMA_READY"] = "1"
        if prometheus_multiproc_dir is not None:
            gateway_env["PROMETHEUS_MULTIPROC_DIR"] = str(prometheus_multiproc_dir)

        if isinstance(spec.code_interpreter, DisabledCodeInterpreterSpec):
            gateway_env["VTOL_CODE_INTERPRETER_MODE"] = "disabled"
        else:
            gateway_env["VTOL_CODE_INTERPRETER_MODE"] = "external"
            gateway_env["VTOL_CODE_INTERPRETER_PORT"] = str(spec.code_interpreter.port)
            gateway_env["VTOL_CODE_INTERPRETER_WORKERS"] = str(spec.code_interpreter_workers)

        gateway_cmd = [
            sys.executable,
            "-m",
            "gunicorn",
            "--config",
            str(Path(__file__).resolve().with_name("gunicorn_conf.py")),
            "--bind",
            f"{spec.gateway.host}:{spec.gateway.port}",
            "--workers",
            str(spec.gateway.workers),
            "--worker-class",
            "uvicorn.workers.UvicornWorker",
            "vtol.entrypoints.api:app",
        ]

        print(f"[serve] starting gateway: {shlex.join(gateway_cmd)}", file=sys.stderr)
        gateway_proc = subprocess.Popen(
            gateway_cmd,
            env=gateway_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        procs.append(("gateway", gateway_proc))
        if gateway_proc.stdout is not None:
            threading.Thread(
                target=stream_lines, args=("gateway| ", gateway_proc.stdout), daemon=True
            ).start()

        try:
            wait_http_ready(
                name="gateway",
                url=f"http://{is_ready_url_host(spec.gateway.host)}:{spec.gateway.port}/health",
                timeout_s=60.0,
                interval_s=2.0,
                abort_proc=gateway_proc,
            )
        except Exception:
            code = gateway_proc.poll()
            if code is not None:
                print(
                    f"[serve] gateway exited during startup (code={code}). shutting down.",
                    file=sys.stderr,
                )
                return code or 1
            raise

        ready_bind = f"{spec.gateway.host}:{spec.gateway.port}"
        if spec.gateway.host in {"0.0.0.0", "::"}:
            ready_local = f"http://127.0.0.1:{spec.gateway.port}/v1/responses"
            print(
                f"[serve] ready: gateway_bind={ready_bind} endpoint={ready_local}",
                file=sys.stderr,
            )
        else:
            print(
                f"[serve] ready: gateway=http://{spec.gateway.host}:{spec.gateway.port}/v1/responses",
                file=sys.stderr,
            )

        # Main supervision loop: exit if any child exits.
        while True:
            for name, proc in procs:
                code = proc.poll()
                if code is not None:
                    if code == 0:
                        print(
                            f"[serve] {name} exited (code=0). shutting down.",
                            file=sys.stderr,
                        )
                        _cleanup()
                        return 1
                    print(
                        f"[serve] {name} exited unexpectedly (code={code}). shutting down.",
                        file=sys.stderr,
                    )
                    _cleanup()
                    return code
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("[serve] received Ctrl+C. shutting down.", file=sys.stderr)
        _cleanup()
        return 130
    except Exception as e:
        print(f"[serve] error: {e!r}", file=sys.stderr)
        _cleanup()
        return 1
    finally:
        _cleanup()
        _restore_signal_handlers()
