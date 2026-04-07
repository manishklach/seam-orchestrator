# Experiments

Seam Orchestrator now includes a lightweight experiment harness in [experiments.py](../experiments.py). The experiments are not transport benchmarks. They are controlled policy tests for disaggregated inference, KV cache transfer, workload-aware routing, and admissibility above transport backends such as mock transport, NIXL, or UCX.

## Experiment Design

Each family reuses the existing control surface:

- `PathState`
- `GFS`
- `PRS`
- `FAE`
- workload-aware admissibility
- capacity-aware selection
- hysteresis and staged restore
- decision records

The harness uses synthetic traces to keep runs deterministic and compact. That makes the results easy to regenerate while staying honest about what is being tested: policy behavior, not transport throughput.

## Experiment Families

### 1. Admissibility Boundary Sweep

This extends Scenario E into a controlled sweep. The same degraded candidate path is exposed to increasing latency inflation and jitter. Batch, interactive, and release-sensitive workloads are routed against the same candidate set.

What it tests:

- how fast admissibility falls for strict workloads
- how long tolerant workloads can safely keep using a degraded-but-live path
- when `PathState` moves from `HEALTHY` to `DEGRADED_USABLE` to `DEGRADED_RESTRICTED`

Artifacts:

- [outputs/experiment_admissibility_boundary.md](../outputs/experiment_admissibility_boundary.md)
- [outputs/experiment_admissibility_boundary.csv](../outputs/experiment_admissibility_boundary.csv)
- [outputs/experiment_admissibility_boundary.svg](../outputs/experiment_admissibility_boundary.svg)

### 2. Capacity-Pressure Tradeoff Sweep

This formalizes Scenario F. Healthy-path occupancy rises while a degraded-but-usable path keeps headroom.

What it tests:

- when tolerant workloads are intentionally sent to the degraded path
- whether strict workloads stay on the healthier path
- whether the orchestrator preserves healthy headroom for higher-criticality traffic

Artifacts:

- [outputs/experiment_capacity_tradeoff.md](../outputs/experiment_capacity_tradeoff.md)
- [outputs/experiment_capacity_tradeoff.csv](../outputs/experiment_capacity_tradeoff.csv)
- [outputs/experiment_capacity_tradeoff.svg](../outputs/experiment_capacity_tradeoff.svg)

### 3. Hysteresis and Flapping Stability

This uses a noisy, alternating trace to compare staged restore with a no-hysteresis baseline.

What it tests:

- total state transitions
- oscillation behavior
- the value of separate escalation and restore windows

Artifact:

- [outputs/experiment_hysteresis_stability.md](../outputs/experiment_hysteresis_stability.md)

### 4. Alternate-Path Scarcity and Propagation Pressure

This varies the number of alternate paths while holding the degraded primary candidate roughly constant.

What it tests:

- `PRS` sensitivity to alternate scarcity
- `FAE` sensitivity to path dependence
- how near-unique paths become higher-risk policy objects

Artifact:

- [outputs/experiment_alternate_scarcity.md](../outputs/experiment_alternate_scarcity.md)

### 5. Baseline Comparison

This compares the orchestrator to three simple baselines:

- lowest-latency
- binary-health-only
- capacity-only

What it tests:

- strict workload success on healthy paths
- tolerant use of degraded-but-usable paths
- healthy-path headroom preservation
- exposure proxies via `PRS` and `FAE`

Artifacts:

- [outputs/experiment_baseline_comparison.md](../outputs/experiment_baseline_comparison.md)
- [outputs/experiment_baseline_comparison.svg](../outputs/experiment_baseline_comparison.svg)

## Main Takeaways

- Health is workload-relative. The same degraded path remains acceptable for tolerant work longer than for interactive or release-sensitive work.
- Capacity-aware policy matters. A healthier path is not always the right path when its headroom should be preserved for stricter work.
- Hysteresis matters. Staged restore reduces flapping under noisy conditions.
- Alternate scarcity matters. `PRS` and `FAE` move as topology dependence changes.
- Naive routing loses information. Lowest-latency, binary-health-only, and capacity-only policies all discard parts of the control problem that matter in disaggregated inference.

## Why This Supports the Thesis

The experiments reinforce the repo's core claim:

disaggregated inference needs more than byte movement. It needs a policy layer above transport that can decide whether a KV cache transfer path is admissible for a workload right now, whether healthy headroom should be preserved, and how much gray degradation or alternate-path scarcity should matter.

That is why the evidence here is framed around admissibility, routing, explainability, and policy over transport rather than transport microbenchmarks.
