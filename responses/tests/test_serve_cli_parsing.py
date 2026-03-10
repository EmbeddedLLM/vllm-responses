from __future__ import annotations

from vllm_responses.entrypoints.serve import _build_root_parser


def test_serve_parser_accepts_remote_upstream_flags() -> None:
    parser = _build_root_parser()
    ns = parser.parse_args(
        [
            "serve",
            "--gateway-port",
            "8458",
            "--upstream",
            "http://127.0.0.1:8000/v1",
            "--code-interpreter-startup-timeout",
            "12.5",
            "--upstream-ready-timeout",
            "90",
            "--upstream-ready-interval",
            "2.5",
            "--mcp-config",
            "/tmp/mcp.json",
            "--mcp-port",
            "6101",
        ]
    )
    assert ns.command == "serve"
    assert ns.gateway_port == 8458
    assert ns.upstream == "http://127.0.0.1:8000/v1"
    assert ns.code_interpreter_startup_timeout == "12.5"
    assert ns.upstream_ready_timeout == "90"
    assert ns.upstream_ready_interval == "2.5"
    assert ns.mcp_config == "/tmp/mcp.json"
    assert ns.mcp_port == "6101"
