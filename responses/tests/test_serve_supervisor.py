from __future__ import annotations

from dataclasses import dataclass

import pytest

from vllm_responses.entrypoints._serve_spec import (
    DisabledCodeInterpreterSpec,
    ExternalUpstreamSpec,
    GatewaySpec,
    McpRuntimeSpec,
    MetricsSpec,
    ServeSpec,
    TimeoutSpec,
)
from vllm_responses.entrypoints._serve_supervisor import run_serve_spec


@dataclass
class _FakeProc:
    poll_code: int | None
    stdout: object | None = None

    def poll(self) -> int | None:
        return self.poll_code


class _FakeStore:
    async def ensure_schema(self) -> None:
        return None


def _base_spec(*, mcp_runtime: McpRuntimeSpec | None) -> ServeSpec:
    return ServeSpec(
        notices=[],
        gateway=GatewaySpec(host="127.0.0.1", port=5969, workers=1),
        mcp_runtime=mcp_runtime,
        upstream=ExternalUpstreamSpec(
            base_url="http://127.0.0.1:8457/v1",
            ready_url="http://127.0.0.1:8457/v1/models",
            headers=None,
        ),
        code_interpreter=DisabledCodeInterpreterSpec(),
        code_interpreter_workers=0,
        metrics=MetricsSpec(enabled=False),
        timeouts=TimeoutSpec(
            vllm_startup_timeout_s=10.0,
            vllm_ready_interval_s=1.0,
            code_interpreter_startup_timeout_s=10.0,
        ),
    )


def _patch_supervisor_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, object]]:
    import vllm_responses.entrypoints._serve_supervisor as supervisor_module
    import vllm_responses.responses_core.store as store_module

    popen_calls: list[dict[str, object]] = []

    def _fake_popen(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        _ = args
        cmd_list = [str(c) for c in cmd]
        is_mcp_runtime = "vllm_responses.entrypoints.mcp_runtime:app" in cmd_list
        proc = _FakeProc(poll_code=None if is_mcp_runtime else 0)
        popen_calls.append(
            {
                "cmd": cmd_list,
                "env": kwargs.get("env"),
                "is_mcp_runtime": is_mcp_runtime,
                "proc": proc,
            }
        )
        return proc

    monkeypatch.setattr(store_module, "get_default_response_store", lambda: _FakeStore())
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(supervisor_module, "wait_http_ready", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor_module, "is_port_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(supervisor_module, "terminate_process", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor_module, "stream_lines", lambda *args, **kwargs: None)

    return popen_calls


def test_run_serve_spec_without_mcp_runtime_does_not_spawn_or_inject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popen_calls = _patch_supervisor_runtime_dependencies(monkeypatch)
    spec = _base_spec(mcp_runtime=None)

    code = run_serve_spec(spec)

    assert code == 1
    assert len(popen_calls) == 1
    assert popen_calls[0]["is_mcp_runtime"] is False
    gateway_env = popen_calls[0]["env"]
    assert isinstance(gateway_env, dict)
    assert "VR_MCP_BUILTIN_RUNTIME_URL" not in gateway_env


def test_run_serve_spec_with_mcp_runtime_spawns_and_injects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popen_calls = _patch_supervisor_runtime_dependencies(monkeypatch)
    spec = _base_spec(
        mcp_runtime=McpRuntimeSpec(
            host="127.0.0.1",
            port=5981,
            ready_url="http://127.0.0.1:5981/health",
        )
    )

    code = run_serve_spec(spec)

    assert code == 1
    assert len(popen_calls) == 2
    assert any(call["is_mcp_runtime"] is True for call in popen_calls)

    gateway_calls = [call for call in popen_calls if call["is_mcp_runtime"] is False]
    assert len(gateway_calls) == 1
    gateway_env = gateway_calls[0]["env"]
    assert isinstance(gateway_env, dict)
    assert gateway_env.get("VR_MCP_BUILTIN_RUNTIME_URL") == "http://127.0.0.1:5981"
