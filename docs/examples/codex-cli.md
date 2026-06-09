# Codex CLI

Use `vllm-responses` as the model provider for [Codex CLI](https://github.com/openai/codex).

______________________________________________________________________

## Prerequisites

- A running `vllm-responses` gateway. See [Quickstart](../getting-started/quickstart.md).
- Codex CLI installed (`codex` on `$PATH`).

______________________________________________________________________

## 1. Gateway

Start the gateway with the `--codex-approval-model` flag so Codex's
guardian auto-review feature works:

=== "`vllm-responses serve`"

    ```bash
    vllm-responses serve \
      --upstream http://<vllm-host>:<port>/v1 \
      --codex-approval-model Qwen/Qwen3.6-35B-A3B
    ```

=== "`vllm serve --responses`"

    ```bash
    vllm serve Qwen/Qwen3.6-35B-A3B \
      --responses \
      --responses-codex-approval-model Qwen/Qwen3.6-35B-A3B
    ```

Replace the model name with whatever your backend exposes.

______________________________________________________________________

## 2. Codex Config

Add the provider to `~/.codex/config.toml`
(or `$CODEX_HOME/config.toml`):

```toml
[model_providers.vllm-responses]
name = "vllm-responses"
base_url = "http://127.0.0.1:8457/v1"
wire_api = "responses"
```

______________________________________________________________________

## 3. Run Codex

```bash
# -c model_context_window is needed to ensure the codex auto compaction threshold follows the model context length
codex --disable image_generation \
  -c model_provider=vllm-responses \
  -m Qwen/Qwen3.6-35B-A3B \
  -c model_context_window=262144
```

______________________________________________________________________

!!! note "Compaction"

    The gateway's implementation of `POST /v1/responses/compact` is not ready.
    However, Codex falls back to its own compaction method for custom
    providers: it sends a normal `POST /v1/responses` with `"tools": []`
    asking the model to summarize prior turns. The gateway accepts this,
    so long interactive sessions still work.

______________________________________________________________________

## References

- [Codex CLI repository](https://github.com/openai/codex)
- [Responses API Contract](../reference/api-reference.md)
- [Command Reference](../usage/command-reference.md)
