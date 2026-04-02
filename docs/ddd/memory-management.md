# Bounded context: memory-management

## Responsibility

Provides a single interface for storing, retrieving, and semantically searching memory entries across all agents and sessions.

## Domain model

### Aggregates and entities

- **`MemoryEntry`** — Aggregate. key, value, type, namespace, timestamp, embedding vector.
- **`MemoryIndex`** — Aggregate. HNSW graph index over all entry embeddings.
- **`Namespace`** — Value object. Scoping label (e.g. `agent-teams`, `session`, `patterns`).

### Domain events

- `MemoryStored`
- `MemoryRetrieved`
- `MemoryExpired`
- `IndexRebuilt`

### Domain services

- **`UnifiedMemoryService`** — Single read/write entrypoint. Routes to hot (HNSW) or cold (SQLite) tier.
- **`EmbeddingService`** — Converts text to vectors for semantic search.
- **`RetentionPolicy`** — Expires short-term entries after 24 h, long-term after 30 d.

## Layer structure

```
memory-management/
├── domain/
│   ├── entities/
│   ├── value-objects/
│   ├── aggregates/
│   └── events/
├── application/
│   ├── commands/
│   └── queries/
├── infrastructure/
│   ├── repositories/
│   └── adapters/
└── api/
    └── mcp-tools/
```

## Related ADRs

[ADR-006](../adr/ADR-006.md), [ADR-009](../adr/ADR-009.md)
