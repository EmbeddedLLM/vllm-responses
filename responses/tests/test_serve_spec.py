from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from vllm_responses.entrypoints._serve_spec import (
    ExternalUpstreamSpec,
    ServeSpecError,
    SpawnCodeInterpreterSpec,
    SpawnVllmSpec,
    build_serve_spec,
)
from vllm_responses.entrypoints._serve_utils import EnvLookup


def _base_args(**overrides) -> argparse.Namespace:
    data = dict(
        upstream=None,
        gateway_host=None,
        gateway_port=None,
        gateway_workers=None,
        code_interpreter="disabled",
        code_interpreter_port=None,
        code_interpreter_workers=None,
        vllm_startup_timeout=None,
        vllm_ready_interval=None,
        code_interpreter_startup_timeout=None,
    )
    data.update(overrides)
    return argparse.Namespace(**data)


def test_build_serve_spec_errors_on_upstream_with_delimiter() -> None:
    args = _base_args(upstream="http://127.0.0.1:8457")
    env = EnvLookup(environ={}, dotenv={})
    with pytest.raises(ServeSpecError, match=r"`--upstream` cannot be used with `--`"):
        build_serve_spec(args, [], had_delimiter=True, env=env)


def test_build_serve_spec_errors_on_missing_model_after_delimiter() -> None:
    args = _base_args(upstream=None)
    env = EnvLookup(environ={}, dotenv={})
    with pytest.raises(ServeSpecError, match=r"`--` requires at least <MODEL_ID_OR_PATH>"):
        build_serve_spec(args, [], had_delimiter=True, env=env)


def test_build_serve_spec_upstream_flag_overrides_env_with_notice() -> None:
    args = _base_args(upstream="http://127.0.0.1:8457")
    env = EnvLookup(environ={"VR_LLM_API_BASE": "http://example.invalid:9999"}, dotenv={})
    spec = build_serve_spec(args, [], had_delimiter=False, env=env)
    assert isinstance(spec.upstream, ExternalUpstreamSpec)
    assert spec.upstream.base_url == "http://127.0.0.1:8457/v1"
    assert spec.notices == [
        "[serve] notice: ignoring VR_LLM_API_BASE='http://example.invalid:9999' in favor of --upstream."
    ]


def test_build_serve_spec_vllm_args_override_env_with_notice(monkeypatch) -> None:
    import vllm_responses.entrypoints._serve_spec as serve_spec_mod

    monkeypatch.setattr(serve_spec_mod.shutil, "which", lambda name: "/usr/bin/vllm")

    args = _base_args(upstream=None)
    env = EnvLookup(environ={"VR_LLM_API_BASE": "http://example.invalid:9999"}, dotenv={})
    spec = build_serve_spec(args, ["model"], had_delimiter=True, env=env)
    assert isinstance(spec.upstream, SpawnVllmSpec)
    assert spec.notices == [
        "[serve] notice: `--` was provided; ignoring VR_LLM_API_BASE in favor of spawning vLLM."
    ]
    assert spec.upstream.cmd[:2] == ["/usr/bin/vllm", "serve"]
    assert spec.upstream.bind_host == "127.0.0.1"
    assert spec.upstream.bind_port == 8457
    assert spec.upstream.ready_url == "http://127.0.0.1:8457/v1/models"


def test_build_serve_spec_vllm_wildcard_bind_uses_loopback_for_urls(monkeypatch) -> None:
    import vllm_responses.entrypoints._serve_spec as serve_spec_mod

    monkeypatch.setattr(serve_spec_mod.shutil, "which", lambda name: "/usr/bin/vllm")

    args = _base_args(upstream=None)
    env = EnvLookup(environ={}, dotenv={})
    spec = build_serve_spec(
        args,
        ["model", "--host", "0.0.0.0", "--port", "9000"],
        had_delimiter=True,
        env=env,
    )
    assert isinstance(spec.upstream, SpawnVllmSpec)
    assert spec.upstream.bind_host == "0.0.0.0"
    assert spec.upstream.bind_port == 9000
    assert spec.upstream.ready_url == "http://127.0.0.1:9000/v1/models"


def test_build_serve_spec_code_interpreter_prefers_bundled_binary(
    tmp_path: Path, monkeypatch
) -> None:
    import vllm_responses.entrypoints._serve_spec as serve_spec_mod

    class _FakeSpec:
        def __init__(self, path: Path) -> None:
            self.submodule_search_locations = [str(path)]

    monkeypatch.setattr(
        serve_spec_mod.importlib.util, "find_spec", lambda name: _FakeSpec(tmp_path)
    )

    bundled = tmp_path / "bin" / "linux" / "x86_64" / "code-interpreter-server"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("stub", encoding="utf-8")

    args = _base_args(
        upstream="http://127.0.0.1:8457",
        code_interpreter="spawn",
        code_interpreter_port=5971,
        code_interpreter_workers=2,
    )
    env = EnvLookup(environ={"VR_PYODIDE_CACHE_DIR": str(tmp_path / "cache")}, dotenv={})
    spec = build_serve_spec(args, [], had_delimiter=False, env=env)
    assert isinstance(spec.code_interpreter, SpawnCodeInterpreterSpec)
    assert spec.code_interpreter.cmd[0] == str(bundled)
    assert spec.code_interpreter.cmd[-2:] == ["--workers", "2"]
    assert spec.code_interpreter.cwd == tmp_path


def test_build_serve_spec_code_interpreter_uses_bun_fallback(tmp_path: Path, monkeypatch) -> None:
    import vllm_responses.entrypoints._serve_spec as serve_spec_mod

    class _FakeSpec:
        def __init__(self, path: Path) -> None:
            self.submodule_search_locations = [str(path)]

    monkeypatch.setattr(
        serve_spec_mod.importlib.util, "find_spec", lambda name: _FakeSpec(tmp_path)
    )
    monkeypatch.setattr(serve_spec_mod.shutil, "which", lambda name: "/usr/bin/bun")

    src = tmp_path / "src" / "index.ts"
    src.parent.mkdir(parents=True)
    src.write_text("console.log('hi')", encoding="utf-8")

    args = _base_args(
        upstream="http://127.0.0.1:8457",
        code_interpreter="spawn",
        code_interpreter_port=5971,
        code_interpreter_workers=0,
    )
    env = EnvLookup(
        environ={
            "VR_PYODIDE_CACHE_DIR": str(tmp_path / "cache"),
            "VR_CODE_INTERPRETER_DEV_BUN_FALLBACK": "1",
        },
        dotenv={},
    )
    spec = build_serve_spec(args, [], had_delimiter=False, env=env)
    assert isinstance(spec.code_interpreter, SpawnCodeInterpreterSpec)
    assert spec.code_interpreter.cmd[:2] == ["/usr/bin/bun", "src/index.ts"]
    assert spec.code_interpreter.cwd == tmp_path


def test_build_serve_spec_code_interpreter_errors_without_binary_or_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    import vllm_responses.entrypoints._serve_spec as serve_spec_mod

    class _FakeSpec:
        def __init__(self, path: Path) -> None:
            self.submodule_search_locations = [str(path)]

    monkeypatch.setattr(
        serve_spec_mod.importlib.util, "find_spec", lambda name: _FakeSpec(tmp_path)
    )

    args = _base_args(
        upstream="http://127.0.0.1:8457",
        code_interpreter="spawn",
        code_interpreter_port=5971,
        code_interpreter_workers=0,
    )
    env = EnvLookup(environ={"VR_PYODIDE_CACHE_DIR": str(tmp_path / "cache")}, dotenv={})
    with pytest.raises(ServeSpecError, match=r"no bundled code-interpreter binary"):
        build_serve_spec(args, [], had_delimiter=False, env=env)


def test_build_serve_spec_cli_zero_gateway_port_overrides_env() -> None:
    args = _base_args(upstream="http://127.0.0.1:8457", gateway_port=0)
    env = EnvLookup(environ={"VR_PORT": "7777"}, dotenv={})
    spec = build_serve_spec(args, [], had_delimiter=False, env=env)
    assert spec.gateway.port == 0


def test_build_serve_spec_cli_zero_values_override_env() -> None:
    args = _base_args(
        upstream="http://127.0.0.1:8457",
        code_interpreter="external",
        code_interpreter_workers=0,
        vllm_ready_interval=0.0,
    )
    env = EnvLookup(
        environ={
            "VR_CODE_INTERPRETER_WORKERS": "3",
        },
        dotenv={},
    )
    spec = build_serve_spec(args, [], had_delimiter=False, env=env)
    assert spec.code_interpreter_workers == 0
    assert spec.timeouts.vllm_ready_interval_s == 0.0


def test_build_serve_spec_enables_builtin_mcp_runtime_only_with_config_path() -> None:
    args = _base_args(upstream="http://127.0.0.1:8457")
    spec_disabled = build_serve_spec(
        args,
        [],
        had_delimiter=False,
        env=EnvLookup(environ={}, dotenv={}),
    )
    assert spec_disabled.mcp_runtime is None

    env = EnvLookup(
        environ={
            "VR_MCP_CONFIG_PATH": "/tmp/mcp.json",
            "VR_MCP_BUILTIN_RUNTIME_URL": "http://127.0.0.1:6101",
        },
        dotenv={},
    )
    spec_enabled = build_serve_spec(args, [], had_delimiter=False, env=env)
    assert spec_enabled.mcp_runtime is not None
    assert spec_enabled.mcp_runtime.host == "127.0.0.1"
    assert spec_enabled.mcp_runtime.port == 6101
    assert spec_enabled.mcp_runtime.ready_url == "http://127.0.0.1:6101/health"


def test_build_serve_spec_builtin_mcp_runtime_uses_default_url_when_unset() -> None:
    args = _base_args(upstream="http://127.0.0.1:8457")
    env = EnvLookup(environ={"VR_MCP_CONFIG_PATH": "/tmp/mcp.json"}, dotenv={})

    spec = build_serve_spec(args, [], had_delimiter=False, env=env)

    assert spec.mcp_runtime is not None
    assert spec.mcp_runtime.host == "127.0.0.1"
    assert spec.mcp_runtime.port == 5981
    assert spec.mcp_runtime.ready_url == "http://127.0.0.1:5981/health"


@pytest.mark.parametrize(
    ("runtime_url", "error_pattern"),
    [
        ("https://127.0.0.1:5981", r"must use `http://`"),
        ("http://example.com:5981", r"must use loopback host"),
        ("http://127.0.0.1", r"must include an explicit port"),
        ("http://127.0.0.1:5981/internal", r"must not include path, query, or fragment"),
        ("http://127.0.0.1:70000", r"invalid VR_MCP_BUILTIN_RUNTIME_URL"),
    ],
)
def test_build_serve_spec_builtin_mcp_runtime_url_validation(
    runtime_url: str, error_pattern: str
) -> None:
    args = _base_args(upstream="http://127.0.0.1:8457")
    env = EnvLookup(
        environ={
            "VR_MCP_CONFIG_PATH": "/tmp/mcp.json",
            "VR_MCP_BUILTIN_RUNTIME_URL": runtime_url,
        },
        dotenv={},
    )

    with pytest.raises(ServeSpecError, match=error_pattern):
        build_serve_spec(args, [], had_delimiter=False, env=env)
