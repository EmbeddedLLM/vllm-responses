# Architecture

Understand how `vLLM Responses` bridges the gap between the OpenAI Responses API and vLLM.

## Overview

At its core, `vLLM Responses` is a translation layer (or "gateway"). It sits between your client application and your vLLM inference server, adding statefulness, built-in tool execution, MCP integration (Built-in MCP and Remote MCP), and spec-compliant streaming.

```mermaid
sequenceDiagram
    participant Client
    participant Gateway as vLLM Responses<br/>Gateway
    participant DB as ResponseStore<br/>(Database)
    participant vLLM as vLLM Server
    participant CI as Code Interpreter<br/>Runtime
    participant MCP as MCP Runtime/Server<br/>(Built-in/Remote)

    Client->>Gateway: POST /v1/responses<br/>(with previous_response_id)
    Gateway->>DB: Load conversation history
    DB-->>Gateway: Previous context
    Gateway->>vLLM: POST /v1/chat/completions<br/>(with full history)
    vLLM-->>Gateway: Streaming chunks

    opt If model requests code_interpreter
        Gateway->>CI: Execute code
        CI-->>Gateway: Execution results
        Gateway->>vLLM: Continue with results
        vLLM-->>Gateway: Final response
    end

    opt If model requests MCP tool
        Gateway->>MCP: Execute tool call
        MCP-->>Gateway: Tool output/error
        Gateway->>vLLM: Continue with MCP result
        vLLM-->>Gateway: Final response
    end

    Gateway-->>Client: SSE: Responses events
    Gateway->>DB: Store response state
```

### Request Flow

1. **Client** sends a request to `/v1/responses`, optionally including a `previous_response_id` to continue a conversation.
1. **Gateway** rehydrates conversation history from the **ResponseStore** (database) if a `previous_response_id` is provided.
1. **Gateway** transforms the Responses request into a Chat Completions request and sends it to **vLLM** with the full conversation history.
1. **vLLM** generates tokens and streams them back to the gateway as Chat Completions chunks.
1. **Gateway** normalizes vLLM output into a stable internal event stream and composes spec-compliant Responses SSE events.
1. If the model requests a **gateway-executed tool** (for example Code Interpreter or an MCP tool):
    - The gateway executes the tool call (locally for built-ins, or via Built-in MCP / Remote MCP transport).
    - Results are fed back to vLLM to continue generation.
    - All of this happens within a single API request.
1. **Gateway** streams Responses events back to the client in real-time.
1. **Gateway** persists terminal storable response state (`completed` and `incomplete`, when `store=true`) to the database for future `previous_response_id` lookups.

______________________________________________________________________

## Key Concepts

### The Responses API

The [OpenAI Responses API](https://www.openresponses.org/specification) is a newer, richer protocol than Chat Completions. It treats a "Response" as a first-class object that can contain multiple output items (messages, tool calls, thinking blocks). This gateway implements the OpenResponses specification, ensuring compatibility with OpenAI's Responses API and other compliant providers.

### Statefulness

Unlike standard Chat Completions where you must send the entire conversation history with every request, the Responses API allows for **stateful conversations**.

- You send an initial request.
- The gateway returns a `response.id`.
- For the next turn, you simply pass `previous_response_id="..."` along with your new input.
- The gateway rehydrates the full context from its **ResponseStore**.

This significantly reduces bandwidth and complexity for client applications.

### Built-in Tools

The gateway includes a runtime for **built-in tools**. The primary example is the **Code Interpreter**. When the model decides to write code, the gateway executes it in a secure, sandboxed environment and returns the results—all within a single API request lifecycle.

### MCP Integration

The gateway can execute MCP tools declared in the request. Built-in MCP uses configured `server_label` inventory via a singleton internal runtime process shared by gateway workers; Remote MCP uses request `server_url`. Both stay in the same Responses lifecycle with consistent `mcp_call` events and output items.
