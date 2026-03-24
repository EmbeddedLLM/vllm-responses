# RFC-02-01 — ResponseStore: DB Schema, Engine Factory & Redis Cache

> **Status:** Draft — open for community review
> **Part of:** RFC-02 (ResponseStore: Conversation Memory)
> **Previous:** [RFC-02-00 — Storage Model & Rehydration](RFC-02-00_response_store_model.md)
> **Component:** `responses_core/store.py`, `db.py`, `utils/cache.py`
> **Depends on:** RFC-02-00 (storage model)

---

## 1. What This RFC Covers

The infrastructure behind the ResponseStore:

- DB schema (`responses_state` table)
- Storage backends: SQLite (dev) vs PostgreSQL (prod)
- `db.py` — the async engine factory and its per-dialect tuning
- Schema initialization safety (multi-worker, multi-instance)
- Redis hot cache (optional, for multi-worker deployments)
- Open questions

---

## 2. DB Schema

One table. The full response state lives in a single JSON column (`state_json`) to keep the schema stable as the payload evolves.

```
Table: responses_state
┌─────────────────────┬──────────────────────────────────────────────┐
│  Column             │  Notes                                       │
├─────────────────────┼──────────────────────────────────────────────┤
│  response_id        │  Primary key (UUID v7 string)                │
│  previous_response_ │  FK reference (indexed) — nullable           │
│  id                 │                                              │
│  model              │  Model name string                           │
│  created_at         │  Timezone-aware datetime (indexed)           │
│  expires_at         │  Nullable — TTL expiry (indexed)             │
│  store              │  bool — was store=true on the request?       │
│  schema_version     │  int — payload contract version              │
│  state_json         │  JSON (SQLite TEXT / Postgres JSONB)         │
│                     │  Contains StoredResponsePayload              │
└─────────────────────┴──────────────────────────────────────────────┘
```

**Schema versioning:** `schema_version` is bumped whenever `StoredResponsePayload` changes shape in a breaking way. Old rows with lower versions either get an upgrader or require a store clear (documented per version). Current version: `1`.

---

## 3. Storage Backends

```
┌────────────────┬──────────────────────────────────────────────────┐
│  Backend       │  When to use                                     │
├────────────────┼──────────────────────────────────────────────────┤
│  SQLite        │  Development, single-machine, zero-config        │
│                │  db_path: sqlite+aiosqlite:///./agentic_stack.db  │
├────────────────┼──────────────────────────────────────────────────┤
│  PostgreSQL    │  Production, multi-worker, multi-instance        │
│                │  db_path: postgresql+asyncpg://...               │
└────────────────┴──────────────────────────────────────────────────┘
```

SQLite is the default. No config needed to get started. Postgres is activated by setting `AS_DB_PATH` to a `postgresql+asyncpg://` URL.

---

## 4. `db.py` — The Engine Factory

`db.py` owns only engine creation and connection-level tuning. It deliberately knows nothing about the ORM models or the ResponseStore logic.

```
db.py responsibilities
├── create_db_engine()        Sync SQLAlchemy engine (cached, lru_cache(maxsize=1))
├── create_db_engine_async()  Async SQLAlchemy engine (cached, lru_cache(maxsize=1))
├── SQLite PRAGMAs            Applied via connect hook on every new connection:
│   ├── journal_mode = WAL    Readers don't block writers
│   ├── synchronous = NORMAL  Good durability/perf tradeoff in WAL mode
│   ├── busy_timeout = 5000ms Prevents "database is locked" under concurrency
│   └── foreign_keys = ON     Correctness — SQLite has this off by default
├── Postgres NullPool         No persistent connection pool (stateless workers)
├── asyncpg prepared stmts    UUID-named to avoid conflicts across connections
└── OpenTelemetry             SQLAlchemyInstrumentor applied if tracing is enabled
```

### Why NullPool for Postgres?

The gateway runs as Gunicorn workers (multiple processes). Each worker creates its own engine. A persistent connection pool per worker would mean `N workers × pool_size` open Postgres connections. NullPool ensures each query opens and closes its own connection, which is the correct model for a stateless worker fleet.

---

## 5. Schema Initialization Safety

Schema creation (`CREATE TABLE IF NOT EXISTS`) must run exactly once at startup, even when multiple workers start simultaneously.

```
┌──────────────────────────────────────────────────────────────┐
│  SQLite                                                      │
│  Single-writer — schema init runs in the supervisor process  │
│  before any workers fork. Workers inherit AS_DB_SCHEMA_READY │
│  env var = "1" and skip re-initialization.                   │
│                                                              │
│  Multi-worker SQLite is rejected at startup with a clear     │
│  error: use the supervisor (agentic-stack serve) or set      │
│  AS_WORKERS=1.                                               │
├──────────────────────────────────────────────────────────────┤
│  PostgreSQL                                                  │
│  Multi-instance safe via a Postgres advisory lock.           │
│  All instances race to acquire pg_try_advisory_lock on the   │
│  key "agentic-stack:responses_state_schema_v1".              │
│  First one wins, runs DDL, releases lock.                    │
│  Others wait (poll every 100ms, timeout 60s).                │
└──────────────────────────────────────────────────────────────┘
```

---

## 6. Redis Hot Cache (Optional)

For multi-worker deployments, `previous_response_id` lookups hit the DB on every request. The optional Redis cache reduces DB reads for recently active conversations.

```
┌──────────────────────────────────────────────────────────────────┐
│                    ResponseStore.get() read path                 │
│                                                                  │
│   Redis cache enabled?                                           │
│        │                                                         │
│       YES ──► check Redis ──► HIT  ──► return cached entry      │
│        │                  └─ MISS ──► fall through to DB        │
│       NO                                                         │
│        │                                                         │
│        └──────────────────► query DB ──► write-through to Redis  │
│                                         (if cache enabled)       │
└──────────────────────────────────────────────────────────────────┘
```

Cache key format: `agentic-stack:responses_state:v{schema_version}:{response_id}`

The schema version is part of the key. When `schema_version` is bumped, old cache entries are automatically orphaned — no explicit invalidation needed.

**Config (all off by default):**

```
AS_RESPONSE_STORE_CACHE=1                  enable Redis cache
AS_RESPONSE_STORE_CACHE_TTL_SECONDS=3600   cache entry TTL
AS_REDIS_HOST=localhost
AS_REDIS_PORT=6379
```

Cache failures are silently swallowed — a Redis outage degrades to DB-only reads, it does not break the gateway.

---

## 7. Open Questions for Community Review

**Q1 — Single-table vs. normalized schema**
All state lives in `state_json` (one JSON column). This keeps the schema stable and simple but makes it hard to query individual fields (e.g. "list all responses for model X"). Should we add indexed columns for common query fields, or is the current design sufficient for the MVP?

**Q2 — TTL enforcement**
`expires_at` is stored but never enforced — there is no cleanup job. Should the MVP include a background cleanup task, or is this explicitly deferred post-MVP?

**Q3 — `store=false` and ephemeral responses**
When `store=false`, nothing is persisted and the response cannot be retrieved later. Is this the right default behavior, or should `store=true` require an explicit opt-in to protect user privacy?

**Q4 — Redis as optional vs. required in production**
Redis is optional today. For large multi-worker deployments, DB read pressure on `previous_response_id` lookups could become significant. Should the docs recommend Redis as a production requirement above a certain worker count threshold?

**Q5 — Advisory lock timeout**
The Postgres advisory lock times out after 60 seconds. If a worker is stuck holding the lock, all others will fail to start. Is 60 seconds the right timeout, and should there be a way to force-release a stuck lock?
