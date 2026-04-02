# Bounded context: shared-kernel

## Responsibility

Defines types, identifiers, base classes, and domain events shared across all bounded contexts. Must remain minimal and stable.

## Domain model

### Aggregates and entities

- **`EntityId`** — Value object. Typed UUID wrapper used across all contexts.
- **`DomainEvent`** — Base class for all domain events. Carries id, timestamp, aggregate id.
- **`Result<T>`** — Value object. Typed success/failure wrapper replacing thrown errors.

### Domain events

- `— (shared kernel publishes no events of its own; it defines the base types)`

### Domain services

- **`IdGenerator`** — Deterministic UUID v4 generation with optional namespacing.
- **`EventBus`** — In-process pub/sub for domain events. Async, non-blocking.
- **`Logger`** — Structured JSON logger with level, context, and correlation-id fields.

## Layer structure

```
shared-kernel/
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

[ADR-002](../adr/ADR-002.md)
