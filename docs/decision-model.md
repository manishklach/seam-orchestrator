# Decision Model

The orchestrator keeps routing explainable by separating signal calculation, admissibility, and selection. The model is intentionally policy-first: transport reports what happened on the path, while the orchestrator decides whether that path should carry a workload now.

## 1. Path State

Each decode path is modeled as one of:

- `HEALTHY`
- `DEGRADED_USABLE`
- `DEGRADED_RESTRICTED`
- `QUARANTINE_CANDIDATE`
- `QUARANTINED`
- `RESTORED`

This is a non-binary model of path quality. A path can be alive without being equally suitable for every workload.

## 2. Gray Failure Score (`GFS`)

`GFS` is the local degradation score. It combines:

- p99 latency
- jitter
- drop behavior
- a cross-signal interaction term

The point is to capture gray degradation rather than only hard failure.

## 3. Propagation Risk Score (`PRS`)

`PRS` estimates how much a bad placement decision can spread beyond the local path.

Inputs:

- workload sensitivity
- path state severity
- current session concentration
- alternate-path scarcity

This is where topology dependence becomes explicit. A degraded sole route is more dangerous than a degraded pool with multiple viable alternates.

## 4. Failure Amplification Estimate (`FAE`)

`FAE` connects local degradation to cluster-level impact.

The intuition:

- a small degradation on a lightly used pool with alternates is often manageable
- the same degradation on a heavily used or sole critical path can amplify into major useful-work loss

`FAE` is the blast-radius bridge in the model.

## 5. Capacity Snapshot

Each candidate pool carries a structured capacity snapshot:

- active sessions
- max capacity
- soft limit
- utilization
- remaining slots
- soft saturation flag
- hard saturation flag

Capacity does not replace health scoring. It adds a separate resource dimension to the decision.

## 6. Admissibility

Admissibility is workload-relative.

Examples:

- release-critical workloads can be blocked on `DEGRADED_USABLE`
- lower-criticality workloads can still use `DEGRADED_RESTRICTED`
- soft or hard capacity pressure can change whether a pool remains a good candidate

This step is intentionally separate from final selection. A pool can be admissible yet still lose the final routing decision because another admissible pool better matches the current policy objective.

## 7. Selection Policy

After inadmissible pools are filtered out, the orchestrator chooses among admissible candidates using a decomposable policy:

- `headroom_first` for tolerant workloads
- `health_first` for strict workloads
- `balanced` for middle cases

This keeps the decision interpretable. The repo does not collapse everything into one opaque mega-score.

## 8. Candidate Explanation Bundle

Every evaluated candidate includes:

- pool id
- current `PathState`
- `GFS`
- `PRS`
- `FAE`
- admissible yes/no
- primary reason
- capacity snapshot
- topology dependence
- chosen / skipped flag
- skipped reason

This is the core explainability artifact.

## 9. Hysteresis and Restore

State transitions use separate bad-window and clean-window counters:

- fast escalation
- slower restore
- staged return through `RESTORED`
- no direct flap from degraded to healthy

The same path can therefore remain observable, explainable, and policy-constrained throughout recovery.
