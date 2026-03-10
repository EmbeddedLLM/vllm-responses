from __future__ import annotations

from fastapi import FastAPI

from vllm_responses.configs.builders import (
    build_runtime_config_for_integrated,
    build_runtime_config_for_standalone,
)
from vllm_responses.configs.sources import EnvSource
from vllm_responses.entrypoints.gateway._app import augment_standalone_gateway_app
from vllm_responses.lm import INTEGRATED_LM_CLIENT, LM_CLIENT, get_openai_provider


def test_build_runtime_config_for_standalone_reads_env_overrides() -> None:
    runtime_config = build_runtime_config_for_standalone(
        env=EnvSource(
            environ={
                "VR_LLM_API_BASE": "http://127.0.0.1:9000/v1",
                "VR_OPENAI_API_KEY": "runtime-key",
                "VR_CODE_INTERPRETER_MODE": "external",
                "VR_CODE_INTERPRETER_PORT": "6111",
                "VR_CODE_INTERPRETER_WORKERS": "2",
                "VR_CODE_INTERPRETER_STARTUP_TIMEOUT": "12.5",
            }
        )
    )

    assert runtime_config.runtime_mode == "standalone"
    assert runtime_config.llm_api_base == "http://127.0.0.1:9000/v1"
    assert runtime_config.openai_api_key == "runtime-key"
    assert runtime_config.code_interpreter_mode == "external"
    assert runtime_config.code_interpreter_port == 6111
    assert runtime_config.code_interpreter_workers == 2
    assert runtime_config.code_interpreter_startup_timeout_s == 12.5
    assert runtime_config.mcp_builtin_runtime_url is None


def test_augment_standalone_gateway_app_initializes_runtime_config() -> None:
    runtime_config = build_runtime_config_for_standalone(
        env=EnvSource(environ={"VR_LLM_API_BASE": "http://127.0.0.1:8457/v1"})
    )

    app = FastAPI()
    augment_standalone_gateway_app(
        app,
        runtime_config=runtime_config,
        include_upstream_proxy=False,
        include_metrics_route=False,
        include_cors=False,
        customize_openapi=False,
    )

    attached = app.state.vllm_responses.runtime_config
    assert attached is not None
    assert attached.runtime_mode == "standalone"
    assert attached.llm_api_base == "http://127.0.0.1:8457/v1"


def test_get_openai_provider_uses_integrated_http_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeProvider:
        def __init__(self, *, api_key, base_url, http_client) -> None:  # type: ignore[no-untyped-def]
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["http_client"] = http_client

    monkeypatch.setattr("vllm_responses.lm.OpenAIProvider", _FakeProvider)

    runtime_config = build_runtime_config_for_integrated(
        env=EnvSource(environ={"VR_OPENAI_API_KEY": "ctx-key"}),
        host="0.0.0.0",
        port=8000,
        code_interpreter_mode="disabled",
        code_interpreter_port=5970,
        code_interpreter_workers=0,
        code_interpreter_startup_timeout_s=30.0,
        mcp_config_path=None,
        mcp_builtin_runtime_url=None,
    )

    _ = get_openai_provider(runtime_config)

    assert captured["api_key"] == "ctx-key"
    assert captured["base_url"] == "http://127.0.0.1:8000/v1"
    assert captured["http_client"] is INTEGRATED_LM_CLIENT


def test_get_openai_provider_defaults_to_standalone_client(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeProvider:
        def __init__(self, *, api_key, base_url, http_client) -> None:  # type: ignore[no-untyped-def]
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            captured["http_client"] = http_client

    monkeypatch.setattr("vllm_responses.lm.OpenAIProvider", _FakeProvider)

    runtime_config = build_runtime_config_for_standalone(env=EnvSource(environ={}))

    _ = get_openai_provider(runtime_config)

    assert captured["http_client"] is LM_CLIENT
