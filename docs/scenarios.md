# Scenarios

The simulator includes six focused scenarios that illustrate a policy layer above transport. The scenarios are meant to make workload-relative admissibility and routing tradeoffs legible, not to benchmark the transport adapters.

## Scenario A: Clean Baseline

Purpose:

- establish the healthy case
- show that the orchestrator stays mostly invisible when paths are clean

## Scenario B: Latency Degradation

Purpose:

- show that elevated latency alone can make a path inappropriate for stricter traffic
- demonstrate workload-aware admissibility without hard failure

## Scenario C: Jitter Storm

Purpose:

- show that jitter-sensitive workloads can be blocked or diverted even when a path remains live

## Scenario D: Drop Storm to Quarantine

Purpose:

- show quarantine and restore mechanics
- exercise staged escalation and non-flapping recovery

## Scenario E: Gray Failure but Not Hard Failure

Purpose:

- demonstrate the category-defining idea
- show a pool that is still up and transferring KV successfully
- admit tolerant work on that path while stricter work moves elsewhere

Expected behavior:

- batch traffic can continue on the degraded path
- interactive traffic prefers the clean backup
- release-critical traffic is not admissible on the degraded path

## Scenario F: Capacity Pressure Under Gray Failure

Purpose:

- show that routing is not only about health
- preserve the healthiest pool for stricter workloads when it is near soft capacity
- allow a degraded-but-admissible pool to absorb tolerant traffic

Expected behavior:

- tolerant batch work may choose the roomier degraded path
- stricter interactive work prefers the healthier path
- the decision is framed as a policy tradeoff, not score worship

## Reading the Output

Each scenario prints:

- a summary table by workload
- a candidate explanation table

The detailed tables surface:

- `PathState`
- `GFS`
- `PRS`
- `FAE`
- capacity snapshot
- admissibility
- chosen / skipped reason

Structured machine-readable logs are also written under `outputs/` as JSONL.

## Reading Scenario E and Scenario F Together

Scenario E and Scenario F are the two anchor demos.

- Scenario E shows that a live path can remain admissible for tolerant traffic while becoming inadmissible for stricter traffic.
- Scenario F shows that even among admissible paths, policy may preserve the healthier path for stricter work and place tolerant work on a degraded-but-roomier path.

Together, they define the category this repo is exploring: policy over transport for disaggregated inference.
