# Domain-Driven Design Documentation

This directory documents the five bounded contexts that make up the RuFlo / Claude Flow v3 architecture, as specified in [ADR-002](../adr/ADR-002.md).

Progress is tracked automatically by `.claude/helpers/ddd-tracker.sh`.

## Bounded contexts

| Context            | Responsibility                                      | Status      |
|--------------------|-----------------------------------------------------|-------------|
| agent-lifecycle    | Agent spawn, health, termination, capability registry | In progress |
| task-execution     | Task decomposition, scheduling, dependency resolution | In progress |
| memory-management  | Unified memory service, HNSW indexing, persistence   | In progress |
| coordination       | Swarm topology, consensus, load balancing            | In progress |
| shared-kernel      | Cross-cutting types, events, identifiers             | In progress |

## Layer conventions

Each bounded context follows a four-layer structure:

```
<context>/
├── domain/          # Entities, value objects, aggregates, domain events
├── application/     # Use cases, command/query handlers
├── infrastructure/  # Repositories, external adapters, MCP wrappers
└── api/             # MCP tool definitions, CLI commands
```

## Ubiquitous language

| Term              | Definition |
|-------------------|------------|
| Agent             | An autonomous Claude Code subagent with a specific capability profile |
| Swarm             | A coordinated group of agents working toward a shared objective |
| Queen             | The lead coordinator agent in a hierarchical swarm topology |
| Task              | A unit of work with defined inputs, outputs, and success criteria |
| Handoff           | A structured transfer of task context from one agent to another |
| Memory entry      | A timestamped key-value record in the unified memory store |
| Pattern           | A learned successful strategy, broadcast across the swarm |
| Consensus         | A quorum-based agreement protocol among swarm agents |
| Checkpoint        | A git-tagged snapshot of project state for rollback |
| Bounded context   | An explicit boundary within which a domain model is consistent |
