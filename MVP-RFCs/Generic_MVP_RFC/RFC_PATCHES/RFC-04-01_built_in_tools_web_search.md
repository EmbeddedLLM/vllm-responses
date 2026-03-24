# RFC-04-01 — Built-in Tools: Web Search
> **Status:** Draft — open for community review
> **Part of:** RFC-04 (Built-in Tools)
> **Previous:** [RFC-04-00 — Built-in Tools: Code Interpreter](RFC-04-00_built_in_tools_code_interpreter.md)
> **Next:** [RFC-05-00 — MCP Integration: Hosted MCP](RFC-05-00_mcp_hosted.md)

---

## 5. Web Search

### 5.1 Architecture

Web search uses a **profile + adapter** design. A profile is selected at gateway startup and determines which backend handles each action type. The client always uses the same public tool declaration (`{"type": "web_search"}`) — the profile is invisible to them.

```
┌────────────────────────────────────────────────────────────────────┐
│  Web Search Architecture                                           │
│                                                                    │
│  Client declares:  tools=[{"type": "web_search"}]                  │
│                                                                    │
│  At startup: profile selected from configuration                   │
│                                                                    │
│  Profile maps each action type to an adapter:                      │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Profile A                                                   │  │
│  │    search    → Adapter A1 (e.g. a search-engine MCP server)  │  │
│  │    open_page → Adapter A2 (e.g. a fetch MCP server)          │  │
│  ├──────────────────────────────────────────────────────────────┤  │
│  │  Profile B                                                   │  │
│  │    search    → Adapter B1 (e.g. DuckDuckGo direct)           │  │
│  │    open_page → Adapter B2 (e.g. a fetch MCP server)          │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
```

This design means the community can add new search backends by implementing an adapter without changing the core tool logic. The profile system ensures operators can switch backends with a configuration change and without code changes.

### 5.2 Three Action Types

The web search tool exposes three action types to the model. The model decides which to use based on context.

```
┌──────────────────┬────────────────────────────────────────────────┐
│  Action          │  What it does                                  │
├──────────────────┼────────────────────────────────────────────────┤
│  search          │  Query a search engine.                        │
│                  │  Input: query string + optional query list.     │
│                  │  Returns: list of sources (url, title, snippet) │
├──────────────────┼────────────────────────────────────────────────┤
│  open_page       │  Fetch and extract text from a URL.            │
│                  │  Input: url.                                   │
│                  │  Returns: page title + full text content.       │
│                  │  Side effect: stores page in request-local cache│
├──────────────────┼────────────────────────────────────────────────┤
│  find_in_page    │  Search text inside a previously opened page.  │
│                  │  Input: url + search pattern.                  │
│                  │  Returns: matches with surrounding context.     │
│                  │  Reads from request cache — no network call.   │
└──────────────────┴────────────────────────────────────────────────┘
```

### 5.3 The Request-Local Page Cache

`open_page` stores fetched page content in a request-local cache. This cache would live only for the duration of a single request and be discarded after the response is complete. `find_in_page` reads from this cache — it would return an error if called on a URL that was never opened in the same request, which helps prevent stale data from leaking across requests. We are open to feedback on whether this scope is the right default.

```
  Request starts
      │
      │  model calls open_page(url="https://example.com")
      ▼
  Web Search Executor
      │  fetches page content, extracts text
      │  stores in request-local page cache
      │    key: canonicalized URL
      │    value: { url, title, text }
      ▼
  model calls find_in_page(url="https://example.com", pattern="RFC")
      │
      ▼
  Web Search Executor
      │  reads from page cache (no network call)
      │  case-insensitive text search
      │  returns matches with surrounding context window
      ▼
  Request ends → cache discarded
```

### 5.4 Adapter Interface

Each adapter handles one action type. Adapters should declare their runtime requirements (e.g. a specific MCP server that should be available) so the gateway can provision and validate them at startup.

We propose a minimal adapter interface:

```
SearchAdapter
    execute_search(context, query, queries, options) → action outcome
    supported_hints: which request options this adapter respects

OpenPageAdapter
    execute_open_page(context, url, options) → action outcome
```

The `supported_hints` mechanism allows adapters to declare which search options they actually respect (e.g. user location, result count). If the client provides an option that the active adapter does not support, the gateway can log a warning rather than silently ignoring it.

### 5.5 SSE Events Produced

When the model calls web search, the event pipeline produces:

```
1. response.web_search_call.in_progress
   (web search output item created)

2. response.web_search_call.searching
   (gateway is executing the action against the backend)

3. response.web_search_call.completed
   response.output_item.done
   (action complete, results incorporated into tool result fed back to LLM)
```

The action type (`search`, `open_page`, or `find_in_page`) and action-specific data (query, sources, URL, pattern) are included in `output_item.done`. Search sources are only included in the response when the client explicitly requests them via an `include` parameter, to avoid bloating the event stream by default.

### 5.6 End-to-End Web Search Flow

```
  Client: tools=[{"type": "web_search"}], stream=true
          │
          ▼
  Request Orchestrator
          │  registers web search as available tool
          │  creates per-request tool state (includes page cache)
          ▼
  LLM streams tokens
          │
          │  LLM emits tool_call: web_search(action="search", query="...")
          ▼
  Normalizer: tool call started → WebSearchCallStarted
  Composer: response.web_search_call.in_progress
          │
          ▼
  Normalizer: tool execution begun → WebSearchCallSearching
  Composer: response.web_search_call.searching
          │
          ▼
  Web Search Executor
          │  routes to configured adapter for "search" action
          │  adapter queries backend, returns sources
          ▼
  Normalizer: tool result received → WebSearchCallCompleted
  Composer: response.web_search_call.completed
            response.output_item.done
          │
          ▼
  Tool result fed back to LLM (sources as JSON)
  LLM incorporates results, continues generating text
          │
          ▼
  response.output_text.delta (×N) → response.completed
```

---

## Open Questions

The following questions are left explicitly open for community discussion.

**On web search:**

6. **Profile selection scope.** The web search profile is selected once at startup. Should profiles be selectable per-request, or is startup-time selection the right model for the MVP? We would love to hear from the community on this.

7. **Page cache scope.** The page cache is currently per-request. If a multi-turn conversation opens a page in turn 1 and tries to search within it in turn 2, it would not find the cached page. Should the page cache be extended to persist across turns in the response store? Your input here would be especially valuable.

8. **Adapter ecosystem.** Should adapters be a first-class extension point with a plugin registry, or is a fixed set of bundled adapters sufficient for the MVP? We would love to hear from the community on this.

**On both tools:**

9. **Tool execution timeout.** Should there be a configurable per-tool execution timeout? What should the default be, and how should a timeout surface to the client?

10. **Error visibility.** When a tool execution fails, should the error be exposed to the LLM (allowing it to describe the failure to the user), or should it surface as a gateway error that terminates the request? Your input here would be especially valuable.
