# RFC-04-01 — Built-in Tools: Web Search

> **Status:** Draft — open for community review
> **Part of:** RFC-04 (Built-in Tools: Code Interpreter & Web Search)
> **Previous:** [RFC-04-00 — Code Interpreter](RFC-04-00_built_in_tools_code_interpreter.md)
> **Component:** `tools/web_search/`
> **Depends on:** RFC-01 (structure), RFC-03 (protocol translation — tools execute inside the request lifecycle)

---

## 1. What This RFC Covers

- Web Search: the action model (`search`, `open_page`, `find_in_page`)
- Profile + adapter pattern
- Request-local page cache
- The SSE events produced
- Open questions for both built-in tools

---

## 2. Architecture

Web search uses a **profile + adapter** design. A profile is selected at gateway startup and determines which backend handles each action type. The client always uses the same public tool shape `{"type": "web_search"}` — the profile is invisible to them.

```
┌────────────────────────────────────────────────────────────────────┐
│  Web Search Architecture                                           │
│                                                                    │
│  Request declares:  tools=[{"type": "web_search"}]                 │
│                                                                    │
│  At startup: profile resolved from AS_WEB_SEARCH_PROFILE           │
│  Default profile: exa_mcp                                          │
│                                                                    │
│  Profile maps action → adapter:                                    │
│                                                                    │
│  ┌────────────────┬───────────────────────────────────────────┐   │
│  │  Profile       │  Action bindings                          │   │
│  ├────────────────┼───────────────────────────────────────────┤   │
│  │  exa_mcp       │  search    → exa_mcp_search               │   │
│  │  (default)     │  open_page → exa_mcp_open_page            │   │
│  ├────────────────┼───────────────────────────────────────────┤   │
│  │  duckduckgo_   │  search    → duckduckgo_common_search      │   │
│  │  plus_fetch    │  open_page → fetch_mcp_open_page           │   │
│  └────────────────┴───────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
```

---

## 3. Three Action Types

The web search tool exposes three actions to the model. The model decides which to call based on context.

```
┌──────────────────┬────────────────────────────────────────────────┐
│  Action          │  What it does                                  │
├──────────────────┼────────────────────────────────────────────────┤
│  search          │  Query a search engine                         │
│                  │  Input: query (string) + optional queries list  │
│                  │  Returns: list of sources (url, title, snippet) │
├──────────────────┼────────────────────────────────────────────────┤
│  open_page       │  Fetch and extract text from a URL             │
│                  │  Input: url                                    │
│                  │  Returns: page title + full text content       │
│                  │  Side effect: stores page in request cache     │
├──────────────────┼────────────────────────────────────────────────┤
│  find_in_page    │  Search text inside a previously opened page   │
│                  │  Input: url + pattern (case-insensitive)       │
│                  │  Returns: matches with ±60 char context window │
│                  │  Reads from request cache (no network call)    │
└──────────────────┴────────────────────────────────────────────────┘
```

---

## 4. The Request-Local Page Cache

`open_page` stores fetched page content in a **request-local** `WebSearchPageCache`. This cache lives only for the duration of a single request — it is discarded after the response is complete.

```
  Request starts
      │
      │  model calls open_page(url="https://example.com")
      ▼
  WebSearchExecutor.open_page()
      │  fetches page, extracts text
      │  stores in WebSearchPageCache
      │    key: canonicalized URL
      │    value: { url, title, text }
      ▼
  model calls find_in_page(url="https://example.com", pattern="RFC")
      │
      ▼
  WebSearchExecutor.find_in_page()
      │  reads from WebSearchPageCache  ← no network call
      │  case-insensitive substring search
      │  returns matches with ±60 char context
      ▼
  Request ends → cache discarded
```

`find_in_page` will error if called on a URL that was never opened in the same request. This is intentional — it prevents stale data from leaking between requests.

---

## 5. Adapter Pattern

Each adapter implements one action (`search` or `open_page`). Adapters declare their runtime requirements (e.g. a specific MCP server) so the gateway can provision them at startup.

```
adapters/
├── base.py               SearchAdapter, OpenPageAdapter abstract interfaces
├── duckduckgo_common.py  search via DuckDuckGo (no MCP required)
├── exa_mcp.py            search + open_page via Exa MCP server
└── fetch_mcp.py          open_page via fetch MCP server
```

```
┌──────────────────────────────────────────────────────────────────┐
│  Adapter interface (base.py)                                     │
│                                                                  │
│  SearchAdapter                                                   │
│    search(ctx, query, queries, options) → ActionOutcome          │
│    hint_support: SearchAdapterHintSupport                        │
│      .user_location: bool                                        │
│      .search_context_size: bool                                  │
│                                                                  │
│  OpenPageAdapter                                                 │
│    open_page(ctx, url, options) → ActionOutcome                  │
└──────────────────────────────────────────────────────────────────┘
```

`hint_support` flags tell the executor which request options a given adapter actually respects. If an option is set but the adapter doesn't support it, a warning is logged once per request.

---

## 6. SSE Events Produced

```
1. WebSearchCallStarted    →  response.web_search_call.in_progress
   (one per tool invocation)

2. WebSearchCallSearching  →  response.web_search_call.searching
   (gateway is executing the action)

3. WebSearchCallCompleted  →  response.web_search_call.completed
                               response.output_item.done
```

The `action` field on `output_item.done` reflects which of the three action types was called, and carries action-specific data (query + sources for `search`, url for `open_page`, url + pattern for `find_in_page`). Sources are only populated in the response when `include=["web_search_call.action.sources"]` is set.

---

## 7. Open Questions for Community Review

**Q1 — Web search profile at startup vs. per-request**
The web search profile (`exa_mcp`, `duckduckgo_plus_fetch`) is selected once at gateway startup via `AS_WEB_SEARCH_PROFILE`. Changing it requires a restart. Should profiles be selectable per-request, or is startup-time selection the right model for MVP?

**Q2 — find_in_page scope**
`find_in_page` only works on pages opened in the same request. If a multi-turn conversation opened a page in turn 1 and tries to `find_in_page` in turn 2, it will fail. Should the page cache be extended to persist across turns in the ResponseStore, or is per-request scope the right boundary?
