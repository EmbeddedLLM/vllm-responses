# RFC-06-00 — Config & Deployment
> **Status:** Draft — open for community review
> **Part of:** RFC-06 (Config & Infrastructure)
> **Previous:** [RFC-05-01 — Request-Remote MCP & Security](RFC-05-01_mcp_request_remote_and_security.md)
> **Next:** [RFC-06-01 — Observability](RFC-06-01_observability.md)

---

## 10. Configuration Model

### 10.1 A Single Immutable Configuration Object

We propose that all configuration for a running gateway live in a single frozen object constructed once at startup, passed to every subsystem, and ideally not mutated after that point. This makes configuration explicit and testable — subsystems would receive it as a constructor argument rather than reading from global singletons. We believe this is a reasonable starting point, though we welcome alternatives.

For the MVP, we suggest the following configuration groups:

```
Configuration Object (proposed groups)
│
├── Deployment
│   ├── Runtime mode (standalone / supervisor / integrated / testing)
│   ├── Bind host and port
│   ├── Worker process count
│   └── Maximum concurrent requests
│
├── Upstream LLM
│   ├── Base URL for the Chat Completions API
│   └── Optional bearer token for upstream authentication
│
├── Tool Settings
│   ├── Web search profile (name of the active profile, or disabled)
│   ├── Code interpreter mode (spawn / external / disabled)
│   ├── Code interpreter port
│   ├── Code interpreter worker count
│   ├── WebAssembly cache directory
│   └── Code interpreter startup timeout
│
├── MCP Settings
│   ├── Path to hosted MCP config file (optional)
│   ├── Request-remote mode enabled flag
│   ├── Request-remote URL validation enabled flag
│   ├── Per-server startup timeout
│   └── Per-tool call timeout
│
├── Response Store
│   ├── Database connection URL
│   ├── Redis host and port
│   ├── Redis cache enabled flag
│   └── Cache entry TTL
│
├── Observability
│   ├── Metrics enabled flag and endpoint path
│   ├── Request timing logging flag
│   ├── Model message logging flag (verbose/debug)
│   ├── Tracing enabled flag
│   ├── OTel service name
│   ├── Trace sample ratio
│   └── OTel collector host and port
│
└── Miscellaneous
    ├── Log directory
    ├── Upstream health check timeout and poll interval
    └── Internal request header name (for integrated-mode loopback bypass)
```

### 10.2 Environment Variable Groups

All environment variables should share a common prefix (we suggest `AS_` for agentic-stack, though the community may prefer a different convention). Variables should be grouped logically:

- **Core gateway variables:** bind address and port, worker count, concurrency limit, log directory, upstream LLM URL and auth.
- **Code interpreter variables:** mode (spawn/external/disabled), port, worker count, startup timeout, cache directory, dev fallback flag.
- **Web search variables:** profile selection.
- **MCP variables:** config file path, sidecar URL override, request-remote enabled/disabled, URL validation enabled/disabled, timeout settings.
- **Response store variables:** database URL, Redis host/port, cache enabled/disabled, cache TTL.
- **Observability variables:** metrics enabled/disabled and path, timing and message logging flags, tracing enabled/disabled, OTel service name, sample ratio, collector host/port.

### 10.3 Config Precedence

We propose:

```
CLI arguments  →  highest priority  (where applicable, supervisor + integrated modes)
Env variables  →  second
Hardcoded defaults  →  lowest
```

We suggest there is no gateway-specific config file (aside from the MCP server config file). All tuning is done via CLI arguments and environment variables.

---

## 11. Deployment Modes

We propose four deployment modes for the MVP. The mode would be determined by the entrypoint used, not by a configuration variable that operators set directly. We are open to feedback on whether this is the right model.

```
┌────────────────────┬──────────────────────────────────────────────────────────┐
│  Mode              │  Description                                             │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Standalone        │  Gateway only. The operator points configuration at an  │
│                    │  existing upstream LLM instance. No upstream process     │
│                    │  management. Config from environment variables only.     │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Supervisor        │  A supervisor process launches gateway workers plus      │
│                    │  optional sidecars (code interpreter, MCP runtime).     │
│                    │  Config from CLI args + environment variables.           │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Integrated        │  Gateway runs inside the upstream LLM process (e.g.     │
│                    │  as a vLLM plugin). Port and host inherited from the     │
│                    │  host process. Upstream URL always points to loopback.  │
│                    │  Worker count always 1.                                  │
├────────────────────┼──────────────────────────────────────────────────────────┤
│  Testing / Mock    │  Like standalone, but uses a mock LLM backend instead   │
│                    │  of a real upstream. For test suites and CI.             │
└────────────────────┴──────────────────────────────────────────────────────────┘
```

### 11.1 Config Construction Per Mode

```
Standalone / Testing:
    └── Read environment variables → apply defaults

Supervisor:
    ├── Parse CLI arguments
    ├── Read environment variables
    └── Apply defaults (CLI takes precedence over env)

Integrated:
    ├── Receive host/port from host process CLI args
    ├── Read environment variables
    └── Apply defaults (host CLI args take highest precedence)
```

All paths converge at a common config construction step that resolves any remaining fields from environment variables and applies hardcoded defaults.

---

## Open Questions

The following questions are left explicitly open for community discussion.

**On configuration:**

6. **Env variable prefix.** We suggest `AS_` as the prefix. Is this the right convention? Should the community standardize on a different prefix? We would love to hear from the community on this.

7. **Multi-worker SQLite.** SQLite does not support multiple concurrent writers. Should multi-worker mode with SQLite be rejected at startup (hard guardrail), or should it be a warning that allows read-heavy deployments to proceed at their own risk? Your input here would be especially valuable.

8. **Config file support.** We propose configuration via CLI args and environment variables only, with no gateway config file. Is there a use case for a gateway config file that environment variables cannot address? We would love to hear from the community on this.
