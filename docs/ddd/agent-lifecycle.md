# Bounded context: agent-lifecycle

## Responsibility

Manages the full lifecycle of individual agents — from capability registration and spawn through health monitoring to graceful termination.

## Domain model

### Aggregates and entities

- **`Agent`** — Core aggregate. Holds id, type, capabilities, status, and spawned-at timestamp.
- **`Capability`** — Value object. Describes a named skill an agent exposes (e.g. `code-review`, `memory-search`).
- **`AgentHealth`** — Value object. Heartbeat timestamp, error count, last-active.

### Domain events

- `AgentSpawned`
- `AgentTerminated`
- `AgentHealthUpdated`
- `CapabilityRegistered`

### Domain services

- **`AgentRegistry`** — Maintains the active agent roster. Source of truth for swarm membership.
- **`CapabilityMatcher`** — Finds agents whose capabilities satisfy a task's requirements.

## Layer structure

```
agent-lifecycle/
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

[ADR-001](../adr/ADR-001.md), [ADR-003](../adr/ADR-003.md)
