from __future__ import annotations

from vtol.entrypoints.serve import _build_root_parser, _normalize_remainder_args


def test_normalize_remainder_args_strips_single_leading_delimiter() -> None:
    assert _normalize_remainder_args([]) == []
    assert _normalize_remainder_args(["model"]) == ["model"]
    assert _normalize_remainder_args(["--", "model", "--port", "8456"]) == [
        "model",
        "--port",
        "8456",
    ]
    # Only strip a leading delimiter, not internal tokens.
    assert _normalize_remainder_args(["model", "--", "--port", "8456"]) == [
        "model",
        "--",
        "--port",
        "8456",
    ]


def test_argparse_remainder_includes_delimiter_token() -> None:
    parser = _build_root_parser()
    ns = parser.parse_args(
        [
            "serve",
            "--gateway-port",
            "8458",
            "--",
            "QuantTrio/GLM-4.6-AWQ",
            "--port",
            "8456",
        ]
    )
    assert ns.command == "serve"
    # argparse includes the literal `--` in REMAINDER; we strip it later.
    assert ns.vllm_args[0] == "--"
    assert _normalize_remainder_args(list(ns.vllm_args))[0] == "QuantTrio/GLM-4.6-AWQ"
