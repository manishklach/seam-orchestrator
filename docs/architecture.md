# Architecture

`seam-orchestrator` treats disaggregated inference as a systems seam between prefill and decode domains.

## Layering

```text
application/session layer
        |
        v
seam-orchestrator
        |
        v
transfer backend
```

## Application or Session Layer

This layer owns request context and workload intent:

- request class
- SLA expectations
- release sensitivity
- synchronization frequency

It asks the orchestrator for an admissible decode path.

## Orchestrator Layer

This is the central artifact in the repo.

Responsibilities:

- maintain non-binary `PathState`
- score gray degradation with `GFS`
- estimate propagation and blast-radius effects with `PRS` and `FAE`
- apply workload-aware admissibility policy
- reason about capacity and alternate-path scarcity
- emit candidate-by-candidate explanations
- serialize decisions and state transitions as JSONL events

This is what "policy above transport" means in practice. The orchestrator is not responsible for inventing a new transport stack. It is responsible for deciding whether and when to use the path a transport exposes.

## Transfer Backend Layer

The backend contract is intentionally narrow:

- move a KV block
- report latency
- report whether the transfer succeeded

Current adapters:

- `MockBackend`
- `NIXLBackend`
- `UCXBackend`

The repo stays transport-agnostic by keeping this interface small and keeping the main logic above it.

## Seam-Aware Orchestration

The seam between prefill and decode is interesting because it carries both data risk and policy risk.

Two paths can both be "up" while having very different suitability:

- one may be acceptable for batch work
- the other may be required for release-critical traffic

Likewise, a healthier pool may still be the wrong place to send tolerant traffic if it is near soft capacity and needs to be reserved for stricter sessions.

That is why the project emphasizes:

- workload-relative admissibility
- capacity-aware selection
- explicit candidate explanations
- staged recovery instead of binary up/down logic

That is also why the project is intentionally not transport-centric. Transport remains a dependency of the seam. Policy is the subject of the repo.
