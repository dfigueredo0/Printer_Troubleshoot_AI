# Bounded context: task-execution

## Responsibility

Decomposes high-level objectives into executable tasks, manages dependencies, schedules work, and tracks completion.

## Domain model

### Aggregates and entities

- **`Task`** — Aggregate. id, description, status, assignedAgent, dependencies, result.
- **`TaskGraph`** — Aggregate. Directed acyclic graph of tasks for a single objective.
- **`Dependency`** — Value object. Edge between two tasks indicating execution order.

### Domain events

- `TaskCreated`
- `TaskAssigned`
- `TaskStarted`
- `TaskCompleted`
- `TaskFailed`
- `TaskCancelled`

### Domain services

- **`TaskDecomposer`** — Breaks an objective string into a TaskGraph using the planner agent.
- **`Scheduler`** — Assigns ready tasks to available agents respecting dependency order.
- **`ResultSynthesizer`** — Aggregates per-task outputs into a unified deliverable.

## Layer structure

```
task-execution/
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

[ADR-003](../adr/ADR-003.md), [ADR-005](../adr/ADR-005.md), [ADR-007](../adr/ADR-007.md)
