# RFC-05-01 — MCP Integration: Request-Remote, Security & E2E Flow

> **Status:** Draft — open for community review
> **Part of:** RFC-05 (MCP Integration)
> **Previous:** [RFC-05-00 — Hosted MCP](RFC-05-00_mcp_hosted.md)
> **Component:** `mcp/resolver.py`, `mcp/policy.py`, `mcp/types.py`

---

## 1. What This RFC Covers

- `resolve_mcp_declarations()` — per-request routing logic
- Tool name mapping and the Normalizer integration
- Security policy: URL validation, Authorization header precedence, secret redaction
- MCP call SSE event lifecycle
- Full end-to-end MCP request flow
- Open questions

---

## 2. resolve_mcp_declarations() — Per-Request Resolution

Called by `LMEngine` at the start of every request that includes MCP tool declarations.

```
resolve_mcp_declarations(
    declarations={"label": OpenAIResponsesMcpTool, ...},
    builtin_mcp_runtime_client=...,     ← None if hosted MCP is disabled
    request_remote_enabled=True/False,
    request_remote_url_checks_enabled=True/False,
)
    │
    ├── For each (server_label, declaration):
    │       │
    │       ├── declaration.connector_id is set? → BadInputError
    │       ├── declaration.require_approval not in {None,"never"}? → BadInputError
    │       │
    │       ├── server_url absent → mode = "hosted"
    │       │       │
    │       │       ├── builtin_mcp_runtime_client is None? → BadInputError
    │       │       ├── list_tools(server_label) → tool inventory
    │       │       └── wrap in BuiltinMcpRuntimeToolset
    │       │
    │       └── server_url present → mode = "request_remote"
    │               │
    │               ├── request_remote_enabled=False? → BadInputError
    │               ├── validate URL (https, no localhost, no IP literals)
    │               ├── build_request_remote_headers()
    │               ├── build FastMCP toolset for the URL
    │               └── toolset.get_tools() → tool inventory
    │
    ├── _select_allowed_tool_infos(runtime_tool_map, allowed_tools)
    │       │
    │       ├── allowed_tools is None → all tools pass through
    │       └── allowed_tools is list → intersect with server's actual tools
    │
    └── empty final tool set → BadInputError
```

The result is a `dict[str, ResolvedMcpServerTools]` — one entry per server label. This dict is passed to `LMEngine`, which registers the toolsets with the pydantic-ai Agent.

---

## 3. Tool Name Mapping and the Normalizer

MCP tool names are globally unique within a request but the gateway needs to track which server each call belongs to. The Normalizer (RFC-03-01) uses `mcp_tool_name_map` — a mapping from tool name to `McpToolRef(server_label, tool_name, mode)` — to attribute each pydantic-ai tool call to its origin server.

```
McpToolRef
├── server_label   "my_search"
├── tool_name      "web_search"
└── mode           "hosted" | "request_remote"
```

When the Normalizer sees a tool call whose name appears in `mcp_tool_name_map`, it emits `McpCallStartedEvent` (instead of the generic function call events). The Composer then produces the `mcp_call` SSE item.

---

## 4. Security Policy

### 4.1 Request-Remote URL Validation

When `request_remote_url_checks_enabled=True` (default), the gateway enforces:

```
┌────────────────────────────────────────┬────────────┐
│  Check                                 │  Result    │
├────────────────────────────────────────┼────────────┤
│  scheme must be https                  │  400 error │
│  host must be present                  │  400 error │
│  host = "localhost" or *.localhost     │  400 error │
│  host is an IP literal (v4 or v6)      │  400 error │
└────────────────────────────────────────┴────────────┘
```

The IP literal check covers both IPv4 (`1.2.3.4`) and IPv6 (`::1`) using Python's `ipaddress.ip_address()`. This prevents SSRF attacks where a client supplies a URL pointing at internal services.

`request_remote_url_checks_enabled` can be set to `False` for internal/trusted deployments where outbound URL control is not needed.

### 4.2 Authorization Header Precedence

When `authorization` is set in the declaration, the gateway:

1. Removes any existing `Authorization` header variants from `headers`
2. Injects `Authorization: Bearer <authorization>`

This ensures the token from the `authorization` field always takes precedence over anything in the `headers` dict.

Multiple `Authorization` header variants in `headers` (without `authorization`) → `BadInputError`.

### 4.3 Secret Redaction

Secrets (tokens, header values) are never forwarded to the client, even in error messages. The gateway collects secret values at resolution time and uses `redact_and_truncate_error_text()` to scrub them before raising `BadInputError`.

For hosted servers, secrets come from `McpServerEntry.secret_values_for_redaction()`:

- HTTP entries: all header values + `auth` field value + bare bearer token (both `Bearer sk-…` and `sk-…` forms)
- Stdio entries: all `env` values

Secrets are sorted longest-first to prevent shorter substrings from masking longer ones.

---

## 5. MCP Call SSE Event Lifecycle

When the model calls an MCP tool, the gateway emits a `mcp_call` output item:

```
mcp_call item lifecycle
├── response.mcp_call.in_progress         ← tool call started
│   { item_id, type, server_label,
│     tool_name, arguments_text }         ← arguments accumulate as deltas
│
├── response.mcp_call.arguments.delta     ← streaming argument chunks
│   (emitted once per token fragment)
│
├── response.mcp_call.arguments.done      ← full arguments JSON assembled
│
├── [gateway executes the tool call]
│
├── response.mcp_call.completed           ← tool returned OK
│   { output }
│
│  OR
│
└── response.mcp_call.failed              ← tool raised an error
    { error }
```

`server_label` and `tool_name` appear in every event so the client can associate output with its originating server without parsing the arguments.

---

## 6. Full MCP Request Flow

```
POST /v1/responses  (tools: [{ type:"mcp", server_label:"X", ... }])
    │
    ▼
LMEngine._build_response_pipeline()
    │
    ├── resolve_mcp_declarations()
    │       │
    │       ├── hosted → HostedMCPRegistry.list_tools("X")
    │       │            wrap in BuiltinMcpRuntimeToolset
    │       │
    │       └── request_remote → validate URL
    │                           build FastMCP toolset
    │                           get_tools() → tool inventory
    │
    ├── Register resolved toolsets with pydantic-ai Agent
    │
    ├── Agent.run_stream_events()
    │       │
    │       ├── Model decides to call tool "tool_name" on server "X"
    │       │
    │       ├── pydantic-ai calls BuiltinMcpRuntimeToolset.call_tool()
    │       │     OR FastMCP toolset.call_tool()
    │       │
    │       └── Result returned to Agent for next model turn
    │
    ├── PydanticAINormalizer maps tool call events → McpCallStartedEvent, etc.
    │
    ├── ResponseComposer emits mcp_call SSE items
    │
    └── SSEEncoder → Client stream
```

---

## 7. Open Questions for Community Review

**Q1 — Hosted MCP availability vs request failure**
When a hosted MCP server fails startup, any request that references it receives a 400. Should the gateway support a `required: false` mode where a hosted server being unavailable causes the tool to be silently omitted rather than failing the request?

**Q2 — Stale tool refresh scope**
The stale tool refresh (RFC-05-00 §5.3) refreshes the full tool inventory for the server. For high-throughput deployments, this could cause a spike if many concurrent requests hit the stale condition simultaneously. Should there be a per-server refresh lock?

**Q3 — request_remote_url_checks_enabled default**
URL checks are enabled by default and block IPs and localhost. For private/internal deployments (e.g. air-gapped, service mesh), operators need to set `request_remote_url_checks_enabled=False`. Is this the right default, or should there be a more granular policy (e.g. allowlist specific CIDR ranges)?

**Q4 — Hosted MCP tool list caching**
The hosted tool inventory is fetched once at startup and then only refreshed on stale errors. If a hosted server updates its tool definitions, clients will not see the change until a stale error triggers a refresh. Should there be a periodic refresh mechanism or a manual refresh endpoint?

**Q5 — connector_id and require_approval**
Both `connector_id` and `require_approval != "never"` are rejected today with a `BadInputError`. These are OpenAI API fields that represent approval workflows not yet supported by the gateway. Should the MVP document these as known gaps with a roadmap, or silently ignore them?
