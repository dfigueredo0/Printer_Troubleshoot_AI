# Bounded context: coordination

## Responsibility

Manages swarm topology selection, consensus protocols, load balancing, and agent-to-agent communication.

## Domain model

### Aggregates and entities

- **`Swarm`** — Aggregate. topology, agents, queen, status, objective.
- **`ConsensusRound`** — Aggregate. question, options, votes, deadline, result.
- **`Handoff`** — Aggregate. from-agent, to-agent, task context, status.

### Domain events

- `SwarmInitialised`
- `TopologyChanged`
- `ConsensusReached`
- `HandoffInitiated`
- `HandoffAccepted`
- `HandoffCompleted`
- `LoadRebalanced`

### Domain services

- **`UnifiedCoordinator`** — Single coordination engine per ADR-003. Owns topology and task assignment.
- **`TopologySelector`** — Chooses hierarchical / mesh / ring based on task complexity.
- **`LoadBalancer`** — Work-stealing algorithm distributing tasks to least-loaded agents.
- **`ConsensusEngine`** — Implements Raft-based quorum for distributed decisions.

## Layer structure

```
coordination/
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
