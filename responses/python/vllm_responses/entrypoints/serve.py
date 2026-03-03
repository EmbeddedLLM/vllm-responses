from __future__ import annotations

import argparse
import sys

from vllm_responses.entrypoints._serve_spec import ServeSpecError, build_serve_spec
from vllm_responses.entrypoints._serve_supervisor import run_serve_spec
from vllm_responses.entrypoints._serve_utils import EnvLookup


def _add_serve_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--upstream",
        type=str,
        default=None,
        help="External upstream base URL (OpenAI-compatible). '/v1' is optional; we normalize it.",
    )

    parser.add_argument("--gateway-host", type=str, default=None, help="Gateway bind host.")
    parser.add_argument("--gateway-port", type=int, default=None, help="Gateway bind port.")
    parser.add_argument("--gateway-workers", type=int, default=None, help="Gunicorn worker count.")

    parser.add_argument(
        "--code-interpreter",
        type=str,
        choices=["spawn", "external", "disabled"],
        default=None,
        help="Code interpreter runtime policy (default: spawn).",
    )
    parser.add_argument(
        "--code-interpreter-port",
        type=int,
        default=None,
        help="Code interpreter port (when spawn|external).",
    )
    parser.add_argument(
        "--code-interpreter-workers",
        type=int,
        default=None,
        help="Bun server --workers (only meaningful when --code-interpreter=spawn).",
    )

    parser.add_argument(
        "--vllm-startup-timeout",
        type=float,
        default=None,
        help="Max seconds to wait for /v1/models (default: 1800).",
    )
    parser.add_argument(
        "--vllm-ready-interval",
        type=float,
        default=None,
        help="Seconds between readiness status messages (default: 5).",
    )
    parser.add_argument(
        "--code-interpreter-startup-timeout",
        type=float,
        default=None,
        help="Max seconds to wait for code interpreter /health (default: 600).",
    )


def _build_root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vllm-responses",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Local runtime supervisor for the vLLM Responses gateway.\n\n"
            "Use `vllm-responses serve` to run the gateway, optionally spawning vLLM and the\n"
            "code-interpreter singleton service."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser(
        "serve",
        help="Run gateway + (optional) vLLM + code interpreter.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Run the full local stack.\n\n"
            "If you provide `--`, everything after it is forwarded to `vllm serve`.\n"
            "If you provide --upstream, vLLM is not spawned."
        ),
        epilog=(
            "Examples:\n"
            "  External upstream (do not spawn vLLM):\n"
            "    vllm-responses serve --upstream http://127.0.0.1:8457\n"
            "    # '/v1' is optional; we normalize it.\n\n"
            "  Spawn vLLM (args after `--` go to `vllm serve`):\n"
            "    vllm-responses serve --gateway-workers 4 -- \\\n"
            "      meta-llama/Llama-3.2-3B-Instruct \\\n"
            "      --dtype auto \\\n"
            "      --port 8457\n\n"
            "Notes:\n"
            "  - When spawning vLLM, the first arg after `--` must be <MODEL_ID_OR_PATH>.\n"
            "  - If you set a custom vLLM --port/--host, the supervisor uses those values for readiness and wiring.\n"
        ),
    )
    _add_serve_arguments(serve)
    serve.add_argument(
        "vllm_args",
        nargs=argparse.REMAINDER,
        help="Arguments for `vllm serve` (must be preceded by `--`).",
    )
    return parser


def _run_serve(args: argparse.Namespace, vllm_argv: list[str], *, had_delimiter: bool) -> int:
    env = EnvLookup.from_cwd()
    try:
        spec = build_serve_spec(args, vllm_argv, had_delimiter=had_delimiter, env=env)
    except ServeSpecError as e:
        print(str(e), file=sys.stderr)
        return e.exit_code

    return run_serve_spec(spec)


def main(argv: list[str] | None = None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _build_root_parser()
    had_delimiter = "--" in raw
    ns = parser.parse_args(raw)

    if ns.command != "serve":
        parser.error("unknown subcommand")

    # argparse eats the `--` delimiter; enforce it explicitly for vLLM passthrough.
    if not had_delimiter and ns.vllm_args:
        print("[serve] error: vLLM args must be provided after `--`.", file=sys.stderr)
        sys.exit(2)

    vllm_args = _normalize_remainder_args(list(ns.vllm_args))

    sys.exit(_run_serve(ns, vllm_args, had_delimiter=had_delimiter))


def _normalize_remainder_args(args: list[str]) -> list[str]:
    """
    Normalize argparse REMAINDER for vLLM args.

    When using `nargs=argparse.REMAINDER`, `argparse` may include the literal `--` delimiter
    in the resulting list. For our "pass-through after --" contract, we strip exactly one
    leading delimiter token if present.
    """
    if args and args[0] == "--":
        return args[1:]
    return args


if __name__ == "__main__":
    main()
