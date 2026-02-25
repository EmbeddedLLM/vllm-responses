# Quickstart

Get your Responses API gateway running in under 5 minutes.

## Prerequisites

- Completed [Installation](installation.md)

______________________________________________________________________

## 1. Start the Gateway

=== "Spawn vLLM"

    Let the gateway start vLLM for you (requires `vllm` installed):

    ```bash
    vllm-responses serve --gateway-workers 1 -- \
      meta-llama/Llama-3.2-3B-Instruct \
      --port 8457
    ```

=== "External vLLM (Advanced)"

    If you already have vLLM running on port 8457:

    ```bash
    vllm-responses serve --upstream http://127.0.0.1:8457
    ```

You should see output indicating the server is running at `http://127.0.0.1:5969`.

______________________________________________________________________

## 2. Send a Request

Now, send a request to the **Responses API** endpoint (`/v1/responses`).

=== "cURL"

    ```bash
    curl -X POST http://127.0.0.1:5969/v1/responses \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer dummy" \
      -d '{
        "model": "meta-llama/Llama-3.2-3B-Instruct",
        "input": [{"role": "user", "content": "Hello! What are you?"}],
        "stream": true
      }'
    ```

=== "Python (OpenAI SDK)"

    ```python
    from openai import OpenAI

    client = OpenAI(
        base_url="http://127.0.0.1:5969/v1",
        api_key="dummy"
    )

    with client.responses.stream(
        model="meta-llama/Llama-3.2-3B-Instruct",
        input=[{"role": "user", "content": "Hello! What are you?"}],
    ) as stream:
        for event in stream:
            print(event)
    ```

______________________________________________________________________

## 3. Observe the Response

If you used `stream=true`, you will see **Server-Sent Events (SSE)**. Unlike standard Chat Completions, the Responses API provides rich lifecycle events:

```text
event: response.created
data: {"response":{...}}

event: response.output_item.added
data: {"output_item":{"type":"message", ...}}

event: response.content_part.added
data: {"part":{"type":"text", "text":""}, ...}

event: response.output_text.delta
data: {"delta":"I am a large language model...", ...}

...

event: response.completed
data: {"response":{...}}
```

## Next Steps

Now that you have the basic loop working, try the advanced features:

- **[Code Interpreter](../features/built-in-tools.md)**: Ask the model to write and execute code.
- **[Stateful Conversations](../features/statefulness.md)**: Use `previous_response_id` to continue a chat.
- **[Architecture](architecture.md)**: Learn how the gateway processes your request.
