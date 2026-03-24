# RFC-05-00 — MCP Integration: Hosted MCP
> **Status:** Draft — open for community review
> **Part of:** RFC-05 (MCP Integration)
> **Previous:** [RFC-04-01 — Built-in Tools: Web Search](RFC-04-01_built_in_tools_web_search.md)
> **Next:** [RFC-05-01 — Request-Remote MCP & Security](RFC-05-01_mcp_request_remote_and_security.md)

---

## 1. Overview

This RFC covers three related infrastructure areas for **agentic-stack**:

- **MCP Integration:** the gateway's support for the Model Context Protocol, including operator-configured hosted servers and client-supplied request-remote servers, along with security enforcement.
- **Configuration:** a single immutable configuration object passed to all subsystems at startup, with support for multiple deployment modes.
- **Observability:** Prometheus metrics, OpenTelemetry tracing, structured logging, and startup health checks.

These three areas are grouped together because they share a common theme: they are all horizontal concerns that affect every part of the system, rather than belonging to a single feature area.

---

## 2. MCP Integration: The Two Modes

Every MCP tool declaration in a request belongs to one of two modes:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Mode              │  How the server is identified                  │
├─────────────────────────────────────────────────────────────────────┤
│  Hosted            │  No server URL in the declaration.             │
│                    │  Server was pre-configured by the operator at  │
│                    │  gateway startup via a config file.            │
│                    │  Identified by a server_label that should match │
│                    │  a label in the config file.                   │
├─────────────────────────────────────────────────────────────────────┤
│  Request-remote    │  A server URL is present in the declaration.   │
│                    │  Client supplies the URL at request time.      │
│                    │  Gateway connects outbound to that URL.        │
└─────────────────────────────────────────────────────────────────────┘
```

The routing decision is determined entirely by whether a server URL is present in the declaration. No other configuration is needed on the client side to distinguish the two modes.

---

## 3. Request MCP Declaration Format

A client attaches MCP tools to a request by including entries in the `tools` array with `type: "mcp"`. The wire format is:

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
│                  │  We suggest restricting to alphanumeric, underscore,       │
│                  │  and hyphen characters.                                    │
│  server_url      │  Present → request-remote mode.                            │
│                  │  Absent  → hosted mode (label should match a pre-configured│
│                  │            server in the operator's config file)           │
│  authorization   │  Bearer token. Gateway injects as Authorization: Bearer …  │
│  headers         │  Additional outbound HTTP headers (request-remote only)    │
│  allowed_tools   │  Subset filter. Absent or null = all tools from server     │
│  require_approval│  For the MVP, we suggest only "never" be accepted (approval│
│                  │  flows are not yet supported — other values would return   │
│                  │  an error, though we welcome discussion on this)           │
└──────────────────┴────────────────────────────────────────────────────────────┘
```

---

## 4. Hosted MCP — Config File Format

Hosted MCP servers are declared in a JSON config file whose path is specified in the gateway configuration. We propose the following format:

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

### 4.1 Transport Detection

We propose inferring the transport type from the entry shape, so an explicit `transport` field is usually not required:

```
┌──────────────────────────────────────────────┬──────────────────┐
│  Entry has…                                  │  Transport       │
├──────────────────────────────────────────────┼──────────────────┤
│  url field                                   │  HTTP (SSE or    │
│                                              │  streamable-HTTP)│
│  command field (and/or args / env / cwd)     │  stdio           │
└──────────────────────────────────────────────┴──────────────────┘
```

Mixing HTTP and stdio fields (`url` + `command`) in one entry should ideally be rejected at config load time with a clear error.

### 4.2 Server Entry Types

```
HTTP server entry
├── url         string — required, absolute http/https URL
├── headers     object (string → string) — optional outbound headers
├── auth        string — optional bearer token (takes precedence over headers)
└── transport   string — optional (auto-detected if absent)

stdio server entry
├── command     string — required executable
├── args        list[string] — optional command arguments
├── env         object (string → string) — optional environment variables
├── cwd         string — optional working directory
└── transport   always "stdio"
```

### 4.3 Label Validation

Server labels should be validated at config load time. We suggest restricting labels to alphanumeric characters, underscores, and hyphens — though we are open to feedback on this. Labels with spaces, dots, or other special characters would ideally be rejected with a clear error message identifying which label is invalid.

---

## 5. Hosted Registry Lifecycle

We propose a singleton hosted registry created at gateway startup that owns one MCP connection per configured server and keeps the tool inventory in memory.

### 5.1 Startup Sequence

```
Gateway starts
    │
    ▼
Hosted Registry startup()
    │
    ├── For each configured server label:
    │       │
    │       ├── Open connection to server (HTTP or stdio)
    │       ├── Fetch tool inventory
    │       │   (with configurable timeout)
    │       │
    │       ├── SUCCESS → mark server available
    │       │             cache tool inventory in memory
    │       │             record startup metric: status="ok"
    │       │
    │       └── FAILURE → mark server unavailable
    │                     record startup error
    │                     record startup metric: status="error"
    │                     (gateway continues — partial failures are tolerated)
    ▼
Registry marked as started
```

Startup failures are **non-fatal**: the gateway would start regardless. A server that fails startup would be marked unavailable; any request that references it would receive a client error response. We welcome feedback on whether there should be a mode where unavailable servers cause requests to degrade gracefully (tool omitted) rather than failing — your input here would be especially valuable.

### 5.2 Availability States

```
┌────────────────────────────────┬──────────────────────────┬────────────────────────┐
│  Server state                  │  is available?           │  Effect on request     │
├────────────────────────────────┼──────────────────────────┼────────────────────────┤
│  Startup succeeded             │  Yes                     │  Usable                │
│  Startup failed / timed out    │  No                      │  Request gets error    │
│  Label not in config           │  N/A (unknown label)     │  Request gets error    │
└────────────────────────────────┴──────────────────────────┴────────────────────────┘
```

### 5.3 Stale Tool Refresh

MCP servers may update their tool list at runtime. We propose a one-retry refresh pattern to handle this gracefully without adding unnecessary complexity:

```
call_tool(server_label, tool_name, arguments)
    │
    ├── Attempt tool call
    │       │
    │       ├── OK  ──────────────────────────► return result
    │       │
    │       └── Tool not found in cached inventory
    │                   │
    │                   └── trigger stale error
    │
    └── On stale error:
            │
            ├── Re-fetch tool inventory from server
            │
            ├── Tool still missing ──────────► raise "tool not found" error
            │
            └── Tool found ──────────────────► update in-memory inventory
                                              ► retry call with refreshed handle
                                              ► return result
```

The refreshed inventory is persisted back into the in-memory cache so subsequent calls do not trigger another refresh unnecessarily. For high-throughput deployments there is a risk of a thundering herd if many concurrent requests hit the stale condition simultaneously — see Open Questions.
