# RFC-02-01 — ResponseStore: DB Schema, Engine Factory & Cache
> **Status:** Draft — open for community review
> **Part of:** RFC-02 (ResponseStore: Conversation Memory)
> **Previous:** [RFC-02-00 — Storage Model & Rehydration](RFC-02-00_response_store_model.md)
> **Next:** [RFC-03-00 — Protocol Translation: Problem & Request Orchestration](RFC-03-00_protocol_translation_problem_and_lmengine.md)

---

## 7. Response Store Infrastructure

### 7.1 Schema Proposal

We propose a single-table design. The full response payload lives in a single JSON column to keep the schema stable as the payload evolves. Indexed metadata columns allow efficient lookups by response ID and creation time.

```
Table: response_store (proposed name — open to suggestions)

┌─────────────────────┬──────────────────────────────────────────────┐
│  Column             │  Notes                                       │
├─────────────────────┼──────────────────────────────────────────────┤
│  response_id        │  Primary key (UUID string)                   │
│  previous_resp_id   │  Foreign key reference — nullable, indexed   │
│  model              │  Model name string                           │
│  created_at         │  Timezone-aware datetime — indexed           │
│  expires_at         │  Nullable TTL expiry — indexed               │
│  store              │  Boolean — was store=true on the request?    │
│  schema_version     │  Integer — payload contract version          │
│  payload            │  JSON blob (SQLite TEXT / PostgreSQL JSONB)  │
└─────────────────────┴──────────────────────────────────────────────┘
```

The `schema_version` column would be bumped whenever the payload shape changes in a breaking way. Old rows with lower versions would either receive an upgrader or require a store clear, documented per version. We are open to feedback on this migration approach.

### 7.2 Storage Backends

We propose supporting two backends:

```
┌────────────────┬──────────────────────────────────────────────────┐
│  Backend       │  Recommended use                                 │
├────────────────┼──────────────────────────────────────────────────┤
│  SQLite        │  Development, single-machine, zero-config        │
├────────────────┼──────────────────────────────────────────────────┤
│  PostgreSQL    │  Production, multi-worker, multi-instance        │
└────────────────┴──────────────────────────────────────────────────┘
```

SQLite would be the default — no configuration needed to get started. PostgreSQL would be activated by pointing the database URL configuration at a PostgreSQL connection string.

### 7.3 Connection Factory

One approach is a dedicated module responsible only for engine creation and connection-level tuning, deliberately knowing nothing about the schema or store logic. For SQLite we suggest applying WAL mode, a busy timeout, and foreign key enforcement on every new connection. For PostgreSQL, given that the gateway may run as multiple worker processes, we suggest a no-persistent-pool approach so each query opens and closes its own connection, avoiding `N workers × pool_size` open database connections.

### 7.4 Schema Initialization Safety

Schema creation should run safely even when multiple worker processes start simultaneously. One approach we believe is a reasonable starting point, though we welcome alternatives:

- **SQLite:** run schema initialization in the supervisor process before workers fork. Workers receive a signal (e.g. an environment variable) that schema is ready and skip re-initialization.
- **PostgreSQL:** use a database-level advisory lock. The first instance to acquire it runs the DDL and releases it; others wait with a reasonable timeout.

We welcome community input on whether there are simpler or more portable approaches.

### 7.5 Optional Redis Hot Cache

For multi-worker deployments, `previous_response_id` lookups hit the database on every request. We propose an optional Redis cache to reduce database reads for recently active conversations:

```
Read path (with Redis enabled):

   Check Redis ──► HIT  → return cached entry
               └─ MISS → query Database → write-through to Redis

Read path (without Redis):

   Query Database → return result
```

The cache key should include the schema version so that when `schema_version` is bumped, old cache entries are automatically orphaned — no explicit invalidation needed.

We suggest that cache failures be silently swallowed so that a Redis outage degrades gracefully to database-only reads rather than breaking the gateway entirely. We welcome feedback on whether operators would prefer a configurable behavior here.
