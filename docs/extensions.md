# Extensions

`seam-orchestrator` is strongest today around KV movement between prefill and decode domains. That is the wedge. The architecture is broader than that wedge.

## Second Embodiment: Checkpoint / Storage Admissibility

A storage path can be live and still be the wrong place to send a full checkpoint.

Example:

- a storage backend is reachable
- writes are succeeding
- latency and queueing are elevated
- full checkpointing would amplify congestion and threaten stricter work
- incremental checkpointing or in-memory checkpoint deferral remains admissible

The same control model still applies:

- `PathState` captures whether the storage path is healthy, degraded, restricted, or recovering
- `GFS` captures local degradation
- `PRS` captures how much broader work could be affected by choosing that path
- `FAE` captures the blast-radius effect of choosing a congested path for a large write
- admissibility decides whether a full checkpoint, incremental checkpoint, or deferred checkpoint is acceptable now

## Why This Matters

The point is not that every embodiment should reuse identical metrics. The point is that the architectural question repeats:

- the path is alive
- the operation is possible
- the policy question is whether it should run now, for this workload, on this path

That is the same policy-over-transport pattern explored by the KV routing scenarios.

## Minimal Mapping

| KV movement embodiment | Checkpoint / storage embodiment |
| --- | --- |
| decode pool candidate | storage target candidate |
| transfer path health | storage path health |
| batch vs release decode traffic | incremental vs full checkpointing |
| degraded-but-usable path | live-but-congested storage target |
| preserve healthy path for strict work | preserve healthy storage bandwidth for stricter work |

This is why the repo is framed as an orchestration layer rather than a single special-case demo.
