# API Reference

The gateway exposes a single primary endpoint compatible with the **OpenAI Responses API**.

!!! tip "Spec Conformance"

    The gateway implements the [OpenResponses](https://www.openresponses.org) specification. This ensures schema validity and correct event ordering.

______________________________________________________________________

## OpenAI Responses API Compatibility

The gateway implements the OpenAI Responses API specification as defined in the [OpenResponses specification](https://www.openresponses.org/specification). This includes:

- **Response structure**: Full `ResponseResource` shape with all required fields
- **Streaming events**: Complete SSE event lifecycle with correct ordering
- **Output items**: Support for messages, function calls, reasoning, and built-in tools
- **Statefulness**: `previous_response_id` for multi-turn conversations

For details on the underlying architecture, see [Architecture](../getting-started/architecture.md).

## Create Response

`POST /v1/responses`

Creates a model response for the given chat conversation.

### Request Body

The request body should be a JSON object with the following parameters:

| Parameter                | Type                 | Required | Description                                                                                                     |
| :----------------------- | :------------------- | :------- | :-------------------------------------------------------------------------------------------------------------- |
| **model**                | `string`             | Yes      | The ID of the model to use (e.g., `meta-llama/Llama-3.2-3B-Instruct`).                                          |
| **input**                | `string` or `array`  | Yes      | The input to the model. Can be a simple string prompt or a list of message objects.                             |
| **stream**               | `boolean`            | No       | If `true`, the response is streamed as [Server-Sent Events](../usage/streaming-events.md). Default: `false`.    |
| **previous_response_id** | `string`             | No       | The ID of a previous response. Used to continue a conversation without re-sending history.                      |
| **tools**                | `array`              | No       | A list of tools the model can call. Can be custom functions or built-in tools.                                  |
| **tool_choice**          | `string` or `object` | No       | Controls which tool is called. Options: `auto`, `none`, `required`, or a specific tool object. Default: `auto`. |
| **store**                | `boolean`            | No       | Whether to store the response in the database. Default: `true`.                                                 |
| **include**              | `array`              | No       | List of additional fields to include in the output (e.g., `code_interpreter_call.outputs`).                     |
| **temperature**          | `float`              | No       | Sampling temperature between 0 and 2. Default: `1.0`.                                                           |
| **top_p**                | `float`              | No       | Nucleus sampling probability mass. Default: `1.0`.                                                              |
| **max_output_tokens**    | `integer`            | No       | Maximum number of tokens to generate.                                                                           |
| **instructions**         | `string`             | No       | A system/developer message to guide the model's behavior. Not persisted across `previous_response_id`.          |
| **reasoning**            | `object`             | No       | Configuration for reasoning models (e.g. `effort`).                                                             |

#### Input Item Schema

When `input` is an array, each item can be a **Message** or a **Tool Output**.

**Message (User/System):**

```json
{
  "role": "user",
  "content": "Hello world"
}
```

**Function Tool Output:**

```json
{
  "type": "function_call_output",
  "call_id": "call_123",
  "output": "Result string"
}
```

______________________________________________________________________

### Response Body (Non-Streaming)

On success, returns a JSON object representing the response.

```json
{
  "id": "resp_01JM...",
  "object": "response",
  "created_at": 1700000000,
  "model": "meta-llama/Llama-3.2-3B-Instruct",
  "status": "completed",
  "output": [
    {
      "id": "msg_01JM...",
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "text",
          "text": "Hello! How can I help you?"
        }
      ]
    }
  ],
  "usage": {
    "input_tokens": 10,
    "output_tokens": 8,
    "total_tokens": 18
  }
}
```

### Response Body (Streaming)

See [Streaming Events](../usage/streaming-events.md) for the full event reference.

______________________________________________________________________

### Error Responses

Errors follow the standard OpenAI error format.

```json
{
  "error": {
    "message": "Invalid model name",
    "type": "invalid_request_error",
    "param": "model",
    "code": "model_not_found"
  }
}
```

| HTTP Status | Error Type              | Description                                   |
| :---------- | :---------------------- | :-------------------------------------------- |
| 400         | `invalid_request_error` | Invalid input or parameters.                  |
| 401         | `authentication_error`  | Missing or invalid API key (if auth enabled). |
| 404         | `invalid_request_error` | Unknown `previous_response_id` or model.      |
| 500         | `api_error`             | Internal server error.                        |
