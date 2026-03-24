# RFC-05-00 — MCP Integration: Hosted MCP

> **Status:** Draft — open for community review
> **Part of:** RFC-05 (MCP Integration)
> **Next:** [RFC-05-01 — Request-Remote MCP, Security & E2E Flow](RFC-05-01_mcp_request_remote_and_security.md)
> **Component:** `mcp/config.py`, `mcp/hosted_registry.py`
> **Depends on:** RFC-01 (project structure), RFC-03 (LMEngine uses `resolve_mcp_declarations()` at request start)

---

## 1. What This RFC Covers

The hosted MCP mode — operator-configured servers that the gateway manages as singletons:

- The two MCP modes overview
- Request MCP declaration format
- Config file format for hosted servers
- `HostedMCPRegistry` lifecycle: startup, availability, stale tool refresh

Request-remote MCP, security policy, resolver logic, and the end-to-end flow are in [RFC-05-01](RFC-05-01_mcp_request_remote_and_security.md).

---

## 2. The Two MCP Modes

Every MCP tool declaration in a request belongs to exactly one of two modes:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Mode                │  How the server is identified                │
├─────────────────────────────────────────────────────────────────────┤
│  hosted              │  server_url is absent in the declaration     │
│                      │  Server was pre-configured by the operator   │
│                      │  at gateway startup via AS_MCP_CONFIG_PATH   │
├─────────────────────────────────────────────────────────────────────┤
│  request_remote      │  server_url is present in the declaration    │
│                      │  Client supplies the URL at request time     │
│                      │  Gateway connects outbound to that URL       │
└─────────────────────────────────────────────────────────────────────┘
```

The routing decision is determined entirely by whether `server_url` is present in the OpenAI `mcp` tool declaration.

---

## 3. Request MCP Declaration Format

A client attaches MCP tools to a request using the OpenAI `tools` array with `type: "mcp"`:

```json
{
  "tools": [
    {
      "type": "mcp",
      "server_label": "my_search",
      "server_url": "https://mcp.example.com/search",
      "authorization": "sk-...",
      "headers": { "X-Custom": "value" },
      "allowed_tools": ["search", "fetch"]
    },
    {
      "type": "mcp",
      "server_label": "code_tools",
      "allowed_tools": ["run_query"]
    }
  ]
}
```

Key fields:

```
┌──────────────────┬────────────────────────────────────────────────────────────┐
│  Field           │  Meaning                                                   │
├──────────────────┼────────────────────────────────────────────────────────────┤
│  server_label    │  Identifier used in SSE events and error messages.         │
│                  │  Must match ^[a-zA-Z0-9_-]+$                               │
│  server_url      │  Present → request-remote mode.                            │
│                  │  Absent  → hosted mode (must match a pre-configured label) │
│  authorization   │  Bearer token. Gateway injects as Authorization: Bearer …  │
│  headers         │  Additional outbound HTTP headers (request-remote only)    │
│  allowed_tools   │  Subset filter. null = all tools from the server           │
│  require_approval│  Only "never" is accepted (approval flows unsupported)     │
│  connector_id    │  Not supported — raises BadInputError if present           │
└──────────────────┴────────────────────────────────────────────────────────────┘
```

---

## 4. Hosted MCP — Config File

Hosted MCP servers are declared in a JSON config file. The path is set via:

```
AS_MCP_CONFIG_PATH=/path/to/mcp.json
```

### 4.1 Config File Format

```json
{
  "mcpServers": {
    "my_search": {
      "url": "https://internal-search.corp/mcp",
      "headers": { "Authorization": "Bearer sk-internal" },
      "transport": "streamable-http"
    },
    "db_tools": {
      "command": "python",
      "args": ["-m", "my_mcp_server"],
      "env": { "DB_URL": "postgresql://..." },
      "cwd": "/opt/tools"
    }
  }
}
```

### 4.2 Transport Detection

The gateway detects transport type from the entry shape — no explicit `transport` field is required:

```
┌──────────────────────────────────────────────┬──────────────────┐
│  Entry has…                                  │  Transport       │
├──────────────────────────────────────────────┼──────────────────┤
│  url field                                   │  HTTP (SSE or    │
│                                              │  streamable-HTTP)│
│  command field (and/or args/env/cwd)         │  stdio           │
└──────────────────────────────────────────────┴──────────────────┘
```

Mixing HTTP and stdio fields (`url` + `command`) in one entry is rejected.

### 4.3 Label Validation

Server labels must match `^[a-zA-Z0-9_-]+$`. Labels with spaces, dots, or other characters are rejected at config load time with a clear error.

### 4.4 Server Entry Types

```
McpServerEntry = HttpMcpServerEntry | StdioMcpServerEntry

HttpMcpServerEntry
├── url         string (required, absolute http/https)
├── headers     object (string → string)  optional
├── auth        string                    optional
└── transport   string                    optional (auto-detected)

StdioMcpServerEntry
├── command     string (required)
├── args        list[string]              optional
├── env         object (string → string)  optional
├── cwd         string                    optional
└── transport   always "stdio"
```

---

## 5. HostedMCPRegistry — Lifecycle

`HostedMCPRegistry` is a singleton created at gateway startup. It owns one FastMCP toolset connection per configured server and keeps the tool inventory in memory.

### 5.1 Startup Sequence

```
Gateway starts
    │
    ▼
HostedMCPRegistry.startup()
    │
    ├── asyncio.Lock (double-checked: idempotent if called twice)
    │
    ├── For each configured server label:
    │       │
    │       ├── build FastMCP toolset (HTTP or stdio)
    │       ├── toolset.__aenter__()          ← opens connection
    │       ├── toolset.get_tools()           ← fetches tool inventory
    │       │   (timeout: startup_timeout_s)
    │       │
    │       ├── SUCCESS → state.server = toolset
    │       │             state.allowed_tools = {name → McpToolInfo}
    │       │             record_mcp_server_startup(status="ok")
    │       │
    │       └── FAILURE → state.startup_error = redacted error text
    │                     state.allowed_tools = {}
    │                     record_mcp_server_startup(status="error")
    │                     (gateway continues — partial failures are tolerated)
    ▼
self._started = True
```

Startup failures are **non-fatal** — the gateway starts regardless. A server that fails startup is marked unavailable; requests that reference it receive a `BadInputError`.

### 5.2 Availability States

```
┌──────────────────────────────────────────────────────────────┐
│  Server state          │  is_server_available()  │  Effect   │
├────────────────────────┼─────────────────────────┼───────────┤
│  Startup succeeded     │  True                   │  Usable   │
│  Startup failed        │  False                  │  Request  │
│  Startup timed out     │  False                  │  gets     │
│  Label unknown         │  N/A (KeyError)         │  400 error│
└──────────────────────────────────────────────────────────────┘
```

### 5.3 Stale Tool Refresh

MCP servers may update their tool list at runtime (e.g. after a hot reload). The registry handles this with a one-retry refresh pattern:

```
call_tool_with_refresh(server_label, tool_name, arguments)
    │
    ├── _call_tool_once()
    │       │
    │       └── pydantic-ai toolset.call_tool()
    │               │
    │               ├── OK  ──────────────────────────► return result
    │               │
    │               └── KeyError matching tool_name
    │                       │
    │                       └── raise HostedMcpStaleToolError
    │
    └── on HostedMcpStaleToolError:
            │
            ├── toolset.get_tools()           ← re-fetch inventory
            │
            ├── tool still missing ──────────► raise HostedMcpToolNotFoundError
            │
            └── tool found ──────────────────► update state cache
                                              ► call_tool() with refreshed handle
                                              ► return result
```

The refreshed tool handle is persisted back into `state.allowed_mcp_tools_by_name` so subsequent calls do not trigger another refresh.
