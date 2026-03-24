# RFC-05-01 — MCP Integration: Request-Remote & Security
> **Status:** Draft — open for community review
> **Part of:** RFC-05 (MCP Integration)
> **Previous:** [RFC-05-00 — Hosted MCP](RFC-05-00_mcp_hosted.md)
> **Next:** [RFC-06-00 — Config & Deployment](RFC-06-00_config_and_deployment.md)

---

## 6. Request-Remote MCP and Per-Request Resolution

At the start of every request that includes MCP tool declarations, the gateway performs per-request resolution to determine which MCP servers and tools are available for that request.

```
Per-request resolution (for each MCP tool declaration):
    │
    ├── Validate declaration format
    │   (reject unsupported fields, invalid labels)
    │
    ├── server_url absent → hosted mode
    │       │
    │       ├── Hosted registry enabled? (if not → error)
    │       ├── Server available? (if not → error)
    │       └── Return tool inventory from registry
    │
    └── server_url present → request-remote mode
            │
            ├── Request-remote enabled? (if not → error)
            ├── Validate URL (see Security Policy)
            ├── Build outbound auth headers
            ├── Open connection to server
            └── Fetch tool inventory
    │
    ├── Apply allowed_tools filter (if specified)
    │   (intersect declared allowed_tools with server's actual tools)
    │
    └── empty final tool set → error (no usable tools from this server)
```

The result of resolution is a mapping from server label to resolved tool inventory. This mapping is passed to the orchestrator, which registers the tools with the LLM for that request.

**Tool name mapping.** MCP tool names should ideally be globally unique within a request. The gateway would need to track which server each tool call belongs to. We suggest maintaining a mapping from tool name to `(server_label, tool_name, mode)` so that each tool call can be attributed to its originating server in SSE events and logs.

---

## 7. Security Policy

### 7.1 Request-Remote URL Validation

When URL validation is enabled (we propose enabling it by default, though we are open to feedback on this), the gateway should enforce:

```
┌────────────────────────────────────────┬────────────────────────────────────────┐
│  Check                                 │  Result on failure                     │
├────────────────────────────────────────┼────────────────────────────────────────┤
│  Scheme should be https                │  Client error (400)                    │
│  Host should be present                │  Client error (400)                    │
│  Host should not be "localhost" or     │  Client error (400)                    │
│  any *.localhost variant               │                                        │
│  Host should not be an IPv4 literal    │  Client error (400)                    │
│  Host should not be an IPv6 literal    │  Client error (400)                    │
└────────────────────────────────────────┴────────────────────────────────────────┘
```

The IP literal check (both IPv4 and IPv6) is intended to prevent SSRF (Server-Side Request Forgery) attacks where a client supplies a URL pointing at internal network services. We suggest using the platform's IP address parsing library to cover edge cases.

URL validation should be configurable so that operators running in trusted internal environments (e.g. air-gapped deployments, service meshes) can disable it. We welcome discussion on whether a more granular policy (e.g. an allowlist of trusted CIDR ranges) would be more appropriate than a binary on/off switch.

### 7.2 Authorization Header Precedence

When an `authorization` field is provided in the declaration alongside a `headers` map, we propose:

1. Remove any existing `Authorization` header (in any case variant) from the `headers` map.
2. Inject `Authorization: Bearer {authorization_value}`.

This ensures the token from the `authorization` field always takes precedence over anything in the `headers` map, preventing accidental double-auth configurations. If `headers` contains multiple `Authorization` variants without an `authorization` field, that should be treated as an error.

### 7.3 Secret Redaction

We suggest that secrets (tokens, header values) should not appear in client-facing error messages. One approach is to collect all secret values at resolution time and use them to redact any error text before it is returned to the client.

For hosted servers, secrets to redact include:
- All HTTP header values from the server config entry.
- The `auth` field value.
- Both the raw token and the full `Bearer {token}` form.
- For stdio servers: all environment variable values.

We suggest sorting secrets longest-first before applying redaction, to prevent shorter substrings from masking longer ones (e.g. avoid a common prefix matching before the full token).

---

## 8. MCP SSE Event Lifecycle

When the model calls an MCP tool, the gateway emits a `mcp_call` output item with the following lifecycle:

```
mcp_call item lifecycle:

├── response.mcp_call.in_progress
│   { item_id, type, server_label, tool_name, arguments_text (empty initially) }
│
├── response.mcp_call_arguments.delta     (one per argument token fragment)
│   (streamed in real time as the model produces the arguments JSON)
│
├── response.mcp_call_arguments.done
│   (full arguments JSON assembled)
│
│  [gateway executes the tool call]
│
├── response.mcp_call.completed           (tool returned a result)
│   { output }
│
│  OR
│
└── response.mcp_call.failed              (tool raised an error)
    { error }
```

`server_label` and `tool_name` would ideally appear in every event so the client can associate output with its originating server without needing to parse the arguments JSON.

---

## 9. End-to-End MCP Request Flow

```
POST /v1/responses
    (tools: [{ type:"mcp", server_label:"X", server_url?:"..." }])
    │
    ▼
Request Orchestrator
    │
    ├── Per-request resolution
    │       │
    │       ├── hosted → fetch tool inventory from hosted registry
    │       │
    │       └── request-remote → validate URL
    │                           open connection, fetch tool inventory
    │
    ├── Register resolved tools with LLM for this request
    │
    ├── LLM streams tokens
    │       │
    │       ├── LLM decides to call tool "tool_name" on server "X"
    │       │
    │       ├── Gateway executes tool call
    │       │     (hosted: via registry connection)
    │       │     (request-remote: via per-request connection)
    │       │
    │       └── Result returned, LLM continues
    │
    ├── Normalizer maps tool call events → McpCall intermediate events
    │
    ├── Composer emits mcp_call SSE items
    │
    └── SSE Encoder → Client stream
```

---

## Open Questions

The following questions are left explicitly open for community discussion.

**On MCP integration:**

1. **Hosted server availability vs. request failure.** When a hosted server fails startup, any request referencing it would receive an error. Should there be a `required: false` mode where an unavailable hosted server causes the tool to be silently omitted rather than failing the request? We would love to hear from the community on this.

2. **Stale tool refresh thundering herd.** If many concurrent requests hit the stale condition simultaneously, they would all trigger a refresh. Should there be a per-server refresh lock? Or should the inventory be periodically refreshed in the background to prevent staleness entirely? Your input here would be especially valuable.

3. **URL validation granularity.** The proposal is a binary on/off for URL validation. For deployments with a mix of trusted internal servers and untrusted external servers, a more granular policy (allowlist by CIDR range or by hostname pattern) might be more appropriate. Is this worth the complexity for the MVP? We would love to hear from the community on this.

4. **Request-remote connection lifecycle.** Per-request connections to remote MCP servers are opened and closed within the request. For high-throughput deployments this could be expensive. Should there be connection pooling for frequently used request-remote servers?

5. **MCP approval workflows.** `require_approval != "never"` would be rejected under this proposal. These are OpenAI API fields representing human-in-the-loop approval flows. Should the MVP document this as a known gap with a forward roadmap? Your input here would be especially valuable.
