# Scoring Specification

This document makes the current Seam Orchestrator decision model explicit.

The model is intentionally heuristic rather than over-fit. The defaults exist to make the control logic legible, configurable, and easy to audit. They are not presented as production-calibrated constants.

## 1. `GFS`: Gray Failure Score

`GFS` estimates local path degradation from recent transfer samples.

Current inputs:

- p99 latency
- jitter
- mean drop rate
- a cross-signal interaction term

Pseudo-formula:

```text
n_lat  = min(p99_latency_ms / latency_quarantine_ms, 1.0)
n_jit  = min(jitter_ms / (jitter_restricted_ms * 2), 1.0)
n_drop = min(mean_drop_rate / drop_quarantine, 1.0)

gfs_base = w_latency * n_lat + w_jitter * n_jit + w_drop * n_drop
interaction = 0.15 * (n_lat * n_drop)
GFS = min(gfs_base + interaction, 1.0)
```

Default weights from [orchestrator.py](../orchestrator.py):

- `w_latency = 0.45`
- `w_jitter = 0.30`
- `w_drop = 0.25`

Default normalization thresholds:

- `latency_quarantine_ms = 40.0`
- `jitter_restricted_ms = 8.0`
- `drop_quarantine = 0.05`

Interpretation:

- latency dominates slightly because tail delay is the most visible way a still-alive path can poison strict traffic
- jitter matters because p99 protection is often lost on live but unstable paths
- drop rate matters because it signals deeper transport or path instability
- the interaction term is intentionally small and only boosts the score when latency and drops rise together

Configurable defaults:

- all thresholds live in `ThresholdConfig`
- all weights live in `ThresholdConfig`
- the sample window is controlled by `candidate_window`

## 2. `PRS`: Propagation Risk Score

`PRS` estimates how much a bad placement decision could spread risk beyond the local path.

Current inputs:

- topology exposure
- workload sensitivity
- path dependence
- state severity

Pseudo-formula:

```text
topology_exposure =
  1.0                                 if sole_route
  1.0 / (1.0 + available_alternate_count) otherwise

workload_sensitivity = effective_criticality(workload)

path_dependence =
  1.0                                 if sole_route
  min(0.25 + active_sessions / max_capacity, 1.0) otherwise

state_severity = STATE_SEVERITY[path_state]

PRS = min(
  topology_exposure * workload_sensitivity * path_dependence * state_severity,
  1.0
)
```

Where topology matters:

- a sole-route path gets the highest topology exposure
- more viable alternates reduce exposure
- loaded paths amplify dependence even when alternates exist

Where workload matters:

- `effective_criticality()` raises the floor for release-critical or strict prefill/decode workloads
- this is why the same degraded candidate can be acceptable for batch and risky for release-sensitive traffic

Default rejection threshold:

- `prs_reject_threshold = 0.75`

Current code uses the same numeric threshold directly in admissibility.

## 3. `FAE`: Failure Amplification Estimate

`FAE` is the blast-radius signal. It tries to connect local degradation to cluster-level damage.

Intuition:

- a mildly degraded path with alternates is often manageable
- a mildly degraded path that is heavily used, near-unique, or release-critical can create disproportionate useful-work loss

Pseudo-formula:

```text
utilization = active_sessions / max_capacity
session_fraction = active_sessions / fae_cluster_scale

scarcity_multiplier =
  2.5                                  if sole_route
  1.0 + 0.5 / available_alternate_count otherwise

criticality_weight = effective_criticality(workload)
release_weight = 2.0 if is_release_path else 1.0

cluster_loss = min(
  max(session_fraction, utilization * 0.25)
  * scarcity_multiplier
  * criticality_weight
  * release_weight,
  1.0
)

FAE = min(cluster_loss / max(GFS, 0.05), fae_max)
```

Default parameters:

- `fae_cluster_scale = 100.0`
- `fae_max = 10.0`

Why this is economic:

- it is trying to capture how much useful work could be amplified or lost if the wrong path keeps carrying important traffic
- sole-route and release-critical conditions deliberately amplify the estimate

## 4. Admissibility Thresholds

Scoring and admissibility are separate stages.

The current order is:

1. compute `PathState`
2. compute `GFS`, `PRS`, and `FAE`
3. apply admissibility checks
4. rank admissible candidates with a selection policy

Current admissibility checks:

- hard capacity rejection
- quarantine rejection
- `PRS` threshold rejection
- p99 latency SLA rejection
- jitter-budget rejection for jitter-sensitive workloads
- state/criticality rejection

Tail-latency and p99 protection:

- strict workloads can be rejected even when the path is still alive
- a degraded-but-usable path can be acceptable for tolerant workloads while being blocked for strict or release-sensitive traffic
- the p99 latency check is direct:

```text
reject if health.p99_latency_ms > workload.latency_sla_ms
```

Jitter tolerance:

Current jitter budget:

```text
jitter_budget = (1.0 - jitter_tolerance) * 5.0
```

Then:

```text
reject if health.jitter_ms > jitter_budget and jitter_tolerance < 0.4
```

That makes low-tolerance workloads more vulnerable to live-but-jittery paths.

State-sensitive behavior:

- `DEGRADED_USABLE`
  - blocks release-critical traffic
  - blocks very high criticality traffic
  - still admits tolerant work
- `DEGRADED_RESTRICTED`
  - only low-criticality work remains admissible
- `RESTORED`
  - remains policy-constrained until enough clean windows accumulate

## 5. Capacity and Hysteresis

### Capacity

Capacity does not replace health scoring. It is a separate dimension used during admissibility and final ranking.

Each candidate carries:

- `active_sessions`
- `max_capacity`
- `soft_limit`
- `utilization`
- `remaining`
- `soft_saturated`
- `hard_saturated`

Capacity enters the decision in two places:

- hard saturation rejects the candidate outright
- soft saturation influences selection order, especially for tolerant workloads

Current selection policies:

- `headroom_first`
  - used for tolerant workloads
  - prefers spending degraded or roomier capacity before scarce healthy headroom
- `health_first`
  - used for strict or release-sensitive workloads
  - prefers lower severity and lower `GFS`
- `balanced`
  - middle case

### Hysteresis

Hysteresis is in the state machine rather than the scorer.

Current defaults:

- `persistence_to_escalate = 3`
- `clean_to_restore = 5`
- `clean_to_healthy = 8`

Interpretation:

- escalation is faster than recovery
- restoration is staged through `RESTORED`
- a single clean interval does not immediately restore a path to fully healthy

This is what keeps the system from flapping under noisy or borderline conditions.

## 6. What Is Configurable?

Current configurable defaults live in `ThresholdConfig`:

- latency thresholds
- jitter thresholds
- drop thresholds
- persistence and restore windows
- `PRS` rejection threshold
- default pool capacity
- soft-capacity fraction
- `GFS` weights
- `FAE` scaling and ceiling

The current values are defaults for clarity and experimentation.

They should be read as:

- explicit
- auditable
- easy to change

not as claims of universal optimality.
