# vLLM Architecture Study: From Entry Points to GPT-OSS/Harmony

> A stage-by-stage reference covering how a request flows through vLLM, where GPT-OSS models
> diverge from the standard path, and what that means for the agentic-stack design.
>
> **Context:** agentic-stack is a stateful gateway over vLLM exposing the OpenAI Responses API.
> Its proposed decisions (D1–D5, from ADR-01 / RFC-01-Core.md) informed this study throughout.
> The most relevant is **D1: "Upstream: target vLLM's existing Responses API"** — the gateway
> calls `POST /v1/responses` on vLLM rather than Chat Completions or `llm.generate()` directly.
> This turns out to be the key decision that determines whether GPT-OSS adds complexity to the
> gateway or not.

---

## Stage 1 — Two Servers, One Entry Point Decision

vLLM ships two servers. Only one matters for production.

### `vllm/entrypoints/api_server.py` — benchmark server

A minimal FastAPI app with a single `POST /generate` route. It calls `engine.generate(prompt, sampling_params)` directly. No OpenAI compatibility, no tool support. Used for benchmarking only.

### `vllm/entrypoints/openai/api_server.py` — production server

The full OpenAI-compatible surface. At startup, `init_app_state()` creates all serving class instances:
- Loads the Jinja2 chat template from the model's tokenizer config
- Instantiates a tool parser (e.g. `hermes`, `llama3_json`, etc.)
- Instantiates a reasoning parser (e.g. for `<think>...</think>` tags)
- Creates the `EngineClient` — either `AsyncLLMEngine` (in-process) or `MQLLMEngineClient` (ZMQ multiprocess)

This server exposes `POST /v1/chat/completions`, `POST /v1/responses`, `POST /v1/completions`, `GET /v1/models`, and more.

**Relevance to agentic-stack:** D1 says to call `POST /v1/responses` on this production server.
The benchmark server is irrelevant. The gateway is a client of the production server.

---

## Stage 2 — Chat Template Preprocessing (`_preprocess_chat`)

When a request arrives at `/v1/chat/completions`, the serving layer converts the human-readable
`messages: [...]` list into token IDs before the engine can process it.

This happens in `serving_engine.py::_preprocess_chat()`. Two branches today:

```python
if is_mistral_tokenizer:
    request_prompt = apply_mistral_chat_template(tokenizer, messages, ...)
else:
    request_prompt = apply_hf_chat_template(tokenizer, conversation, ...)
```

Both branches return a string (the rendered prompt), which is then tokenized into `list[int]`.

**There is no GPT-OSS branch here.** GPT-OSS models use the Harmony protocol and cannot use a
Jinja template. This is the gap in the older vLLM branch.

The newer vLLM branch adds a third path for GPT-OSS, but it lives entirely within
`OpenAIServingResponses` (the `/v1/responses` handler), not in `_preprocess_chat`. See Stage 5.

This is also why D2 — "tokenization and chat templates stay in vLLM core" — is sound: the
gateway delegates all of this to the upstream vLLM server and never needs to know which branch
applies to a given model.

---

## Stage 3 — The Engine Boundary

Everything above Stage 3 is "serving layer." Everything below is "engine." The boundary is
defined by one abstract method in `vllm/engine/protocol.py`:

```python
class EngineClient:
    def generate(
        self,
        prompt: PromptType,
        sampling_params: SamplingParams,
        request_id: str,
        ...
    ) -> AsyncGenerator[RequestOutput, None]: ...
```

The serving layer **always** passes a `TokensPrompt`:

```python
TokensPrompt = {"prompt_token_ids": list[int], "multi_modal_data": ...}
```

The engine never sees message dicts, Jinja output, or Harmony objects. It receives token IDs
and returns `RequestOutput` objects.

**`RequestOutput`** yields incrementally:
- `outputs: list[CompletionOutput]` — `CompletionOutput.text` grows each yield
- `outputs[0].token_ids` — accumulated token IDs so far
- `outputs[0].finish_reason` — `None` until the last yield, then `"stop"`, `"length"`, etc.
- `finished: bool` — `True` on the last yield

**This is the most important architectural fact in vLLM.** Both standard models and GPT-OSS
models produce the same `RequestOutput` type. Harmony parsing happens *above* this boundary,
in the serving layer, not inside the engine.

---

## Stage 4 — The Harmony Protocol and `openai_harmony`

### What Harmony is

Harmony is a completely different message protocol from OpenAI's `role/content` format.
Every message carries:

| Field | Purpose |
|---|---|
| `author` | `Role` + optional name (e.g. `Author(Role.TOOL, "functions.search")`) |
| `channel` | `"analysis"` (hidden reasoning), `"commentary"` (tool calls, preambles), `"final"` (visible answer) |
| `recipient` | Where the message is directed — `"assistant"`, `"functions.search"`, `"browser.open"`, `"python"`, etc. |
| `content_type` | `"text"`, `"json"`, `"<|constrain|>json"`, etc. |

The `channel` field drives output routing:

| Channel + recipient | Meaning |
|---|---|
| `analysis`, any | Hidden chain-of-thought (reasoning) |
| `commentary`, no recipient | Preamble text, visible to user |
| `commentary`, `functions.X` | Function tool call |
| `commentary`, `browser.*` | Web search call |
| `commentary`, `python` | Code interpreter call |
| `commentary`, `container.*` | Container tool call |
| `final`, any | The visible answer text |

### `openai_harmony` — the external dependency

All Harmony encoding and decoding lives in the `openai_harmony` Python package. This is not
part of the vLLM repo. Key exports:

```python
from openai_harmony import (
    HarmonyEncodingName,     # HARMONY_GPT_OSS
    Conversation,            # wraps list[Message] for rendering
    StreamableParser,        # token-by-token output parser
    Message, Role, Author,   # message building blocks
    SystemContent,           # system message builder (model identity, tools, date)
    DeveloperContent,        # developer message builder (instructions, function tools)
    ToolDescription,         # function tool schema wrapper
    load_harmony_encoding,   # loads the Harmony tokenizer
)
```

**Relevance to agentic-stack:** `openai_harmony` is a required transitive dependency if the
gateway ever needs to handle Harmony natively. Under D1 (gateway calls `POST /v1/responses`
on vLLM), vLLM owns `openai_harmony` internally and the gateway never imports it. This is
one of the strongest arguments for D1.

### The Renderer — `render_for_completion()`

`harmony_utils.py::render_for_completion()` converts a list of Harmony `Message` objects
directly to token IDs:

```python
def render_for_completion(messages: list[Message]) -> list[int]:
    conversation = Conversation.from_messages(messages)
    token_ids = get_encoding().render_conversation_for_completion(
        conversation, Role.ASSISTANT
    )
    return token_ids
```

This replaces Jinja for GPT-OSS models. Instead of rendering a template string and tokenizing,
Harmony encodes messages directly into the model's token space using `HarmonyEncodingName.HARMONY_GPT_OSS`.

### The Parser — `StreamableParser`

`StreamableParser` processes token IDs one at a time and maintains state:

- `parser.current_channel` — which channel is currently being written
- `parser.current_recipient` — which recipient the current message is addressed to
- `parser.current_content` — text accumulated so far in the current message
- `parser.messages` — list of completed `Message` objects

This replaces both the tool parser and the reasoning parser for GPT-OSS models.

### Input conversion — `parse_chat_inputs_to_harmony_messages()`

Converts OpenAI `messages: [...]` format → Harmony `Message` objects:

| Input role / shape | Harmony output |
|---|---|
| `assistant` + tool calls | `channel = "commentary"`, `recipient = "functions.X"` per call |
| `tool` | `Author(Role.TOOL, "functions.X")`, `channel = "commentary"`, `recipient = "assistant"` |
| `assistant` (text only) | `channel = "final"` |
| `user` / `system` / `developer` | Direct conversion |

Also calls `auto_drop_analysis_messages()` to remove stale chain-of-thought from prior turns
in multi-turn conversations. Harmony models expect analysis messages to be dropped after an
assistant message to the final channel is produced.

### Stop tokens

GPT-OSS uses two EOS-like tokens instead of the standard EOS:

- `<|return|>` — normal completion
- `<|call|>` — tool call in progress

`get_stop_tokens_for_assistant_actions()` returns their token IDs, added to
`sampling_params.stop_token_ids` at serving class initialization.

---

## Stage 5 — `OpenAIServingResponses`: The Full GPT-OSS Request Flow

### Auto-detection at startup

```python
# responses/serving.py
self.use_harmony = self.model_config.hf_config.model_type == "gpt_oss"
```

One flag, set at constructor time from the HuggingFace config. When `True`:
- Harmony stop tokens are injected into `default_sampling_params`
- All standard tool/reasoning parser setup is bypassed
- A completely separate request preparation path is used

### Request entry: `create_responses()`

Two paths fork immediately:

```python
if self.use_harmony:
    messages, engine_prompts = self._make_request_with_harmony(request, prev_response)
else:
    messages, engine_prompts = await self._make_request(request, prev_response)
```

**Harmony path (`_make_request_with_harmony`):**
1. `_construct_input_messages_with_harmony()` — Responses API input items → Harmony `Message` objects
2. `render_for_completion(messages)` — Harmony messages → `list[int]` token IDs using `HARMONY_GPT_OSS` encoding
3. `token_inputs(token_ids)` — wraps as `TokensPrompt`, same format the engine always receives

**Standard path (`_make_request`):**
1. Constructs OpenAI-format message dicts
2. Calls `openai_serving_render.preprocess_chat()` — runs Jinja/Mistral template → token IDs
3. Same `TokensPrompt` wrapping

Both paths converge on identical `engine_client.generate(TokensPrompt, ...)` calls.

### Context object selection

```python
if self.use_harmony:
    context = StreamingHarmonyContext(messages, available_tools)  # streaming
    context = HarmonyContext(messages, available_tools)           # non-streaming
else:
    context = SimpleContext()        # standard, post-generation parsing
    context = ParsableContext(...)   # experimental, token-level parsing
```

**`HarmonyContext`** holds the full per-request conversation state:
- `_messages` — the Harmony `Message` list, grows as the tool loop runs
- `parser` — a `StreamableParser`, rebuilt per output chunk
- `num_init_messages` — index boundary separating input from output in `_messages`
- Token counters: `num_prompt_tokens`, `num_output_tokens`, `num_cached_tokens`,
  `num_reasoning_tokens`, `num_tool_output_tokens`
- `_tool_sessions` — active MCP sessions keyed by tool name

### The agentic tool loop — `_generate_with_builtin_tools()`

The same loop handles all models. The context object provides the model-specific behavior:

```python
while True:
    # 1. Generate
    async for res in engine_client.generate(engine_prompt, sampling_params, ...):
        context.append_output(res)   # feeds token IDs into StreamableParser
        yield context

    # 2. Did the model ask for a built-in tool?
    if not context.need_builtin_tool_call():
        break

    # 3. Execute the tool
    tool_output = await context.call_tool()       # calls MCP server
    context.append_tool_output(tool_output)       # appends result as Harmony Messages

    # 4. Re-render for next turn (Harmony path)
    if isinstance(context, HarmonyContext):
        token_ids = context.render_for_completion()   # re-encode full conversation
        engine_prompt = token_inputs(token_ids)
        sampling_params.max_tokens = max_model_len - len(token_ids)
```

**`HarmonyContext.need_builtin_tool_call()`** checks the last message's `recipient` field:

```python
if recipient.startswith("browser."):   return "browser" in available_tools
if recipient.startswith("python"):     return "python" in available_tools
if recipient.startswith("container."): return "container" in available_tools
```

The `recipient` field carries routing information natively in Harmony — no text parsing needed
to detect which tool was called.

**`HarmonyContext.call_tool()`** dispatches by `recipient` prefix to the matching tool method.
Each calls the MCP server and returns the result as a Harmony `Message` with
`Author(Role.TOOL, "functions.X")`, `channel = "commentary"`, `recipient = "assistant"`.

**`HarmonyContext.render_for_completion()`** calls `render_for_completion(self.messages)` —
the entire conversation including the tool result is re-encoded from scratch for the next turn.

### Output assembly — `harmony_to_response_output()`

`responses/harmony.py::harmony_to_response_output()` dispatches each completed Harmony message
to the right Responses API output item type:

| Harmony message | Responses API output item |
|---|---|
| `channel == "analysis"` | `ResponseReasoningItem` (hidden reasoning) |
| `channel == "final"` | `ResponseOutputMessage` (visible text) |
| `channel == "commentary"`, no recipient | `ResponseOutputMessage` (preamble text) |
| `channel == "commentary"`, `recipient = "functions.X"` | `ResponseFunctionToolCall` |
| `channel == "commentary"`, `recipient = "browser.*"` | `ResponseFunctionWebSearch` |
| any other recipient | `McpCall` |

---

## End-to-End GPT-OSS Request Lifecycle

```
POST /v1/responses
        │
        ▼
create_responses()
        │
        ├─ use_harmony=True  (model_type == "gpt_oss" in HuggingFace config)
        │       │
        │       ▼
        │  _make_request_with_harmony()
        │       │
        │  response_input_to_harmony()     ← Responses API items → Harmony Messages
        │  render_for_completion()         ← Harmony Messages → token IDs (HARMONY_GPT_OSS)
        │  token_inputs(token_ids)         ← TokensPrompt (same as any model)
        │       │
        │  HarmonyContext(messages, tools)
        │
        ├─ _generate_with_builtin_tools() [agentic loop]
        │       │
        │       ├─ engine_client.generate(TokensPrompt)
        │       │       └─ append_output(RequestOutput)
        │       │               └─ StreamableParser.process(token_id) [per token]
        │       │
        │       ├─ need_builtin_tool_call()?
        │       │       └─ check last_msg.recipient prefix
        │       │
        │       ├─ call_tool() → MCP server → Harmony Message (tool result)
        │       ├─ append_tool_output()
        │       └─ render_for_completion() → new TokensPrompt → loop
        │
        └─ responses_full_generator() / responses_stream_generator()
                └─ _make_response_output_items_with_harmony(context)
                        └─ harmony_to_response_output(msg) per message
                                → ResponseReasoningItem
                                  ResponseOutputMessage
                                  ResponseFunctionToolCall
                                  ResponseFunctionWebSearch
                                  McpCall
```

---

## Key Architectural Facts for agentic-stack

**1. The engine boundary is model-agnostic.**
Both standard and GPT-OSS paths produce `TokensPrompt → engine_client.generate() → RequestOutput`.
The engine never knows which model protocol is in use. Harmony parsing happens entirely above
this boundary.

**2. The branch point is a single flag.**
`use_harmony = model_type == "gpt_oss"` set at startup from the HuggingFace config. All GPT-OSS
logic is cleanly isolated from the standard path.

**3. Tool calls are protocol-native in Harmony.**
The `recipient` field in each Harmony message carries routing. No regex or JSON-parsing of
output text is needed to detect which tool was called.

**4. Multi-turn tool use is an agentic loop above the engine.**
`_generate_with_builtin_tools()` is the same loop for all models. `HarmonyContext` and
`SimpleContext` provide the model-specific implementation behind the same interface.

**5. `openai_harmony` is an external package.**
All Harmony encoding/decoding depends on it. It is not in the vLLM repo. Under D1 (gateway
calls `POST /v1/responses` on vLLM), the gateway never imports `openai_harmony` — vLLM
absorbs it entirely.

**6. D1 makes the gateway model-agnostic with respect to GPT-OSS.**
If the gateway always calls `POST /v1/responses` upstream, vLLM handles Harmony rendering,
parsing, and the tool loop. From the gateway's perspective, a GPT-OSS model and a standard
model produce identical Responses API output. The RFC-03-01 Stage 1 Normalizer sees standard
typed output items in both cases and needs no GPT-OSS-specific logic. This is one of the
strongest arguments for D1 as stated in ADR-01.
