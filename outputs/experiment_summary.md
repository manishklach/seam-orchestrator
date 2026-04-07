# Experiment Summary

This is the compact evidence layer for Seam Orchestrator. The experiments are not transport benchmarks; they test admissibility, routing policy, capacity-aware selection, hysteresis, and alternate-path dependence above the transport backend.

## Headline Metrics

| Metric | Value |
| --- | --- |
| strict_workload_success_rate_pct | 95.5 |
| tolerant_degraded_utilization_rate_pct | 82.6 |
| healthy_headroom_preservation_rate_pct | 100.0 |
| hysteresis_oscillations_avoided | 7 |

## Key Findings

- Admissibility is workload-relative: the same degraded candidate remains acceptable for tolerant work longer than for interactive or release-sensitive work.
- Capacity-aware policy preserves scarce healthy headroom for stricter traffic instead of always choosing the best raw health score.
- Hysteresis materially reduces oscillation under noisy conditions compared with a no-hysteresis baseline.
- Alternate-path scarcity raises PRS and FAE, making topology dependence visible in routing decisions.
- Naive routing baselines lose signal that the orchestrator preserves, especially around headroom preservation and tolerant use of degraded-but-usable paths.

## Baseline Deltas

| Comparison | Delta |
| --- | --- |
| strict_vs_capacity_only_pct_points | 64.2 |
| headroom_vs_binary_health_only_pct_points | 100.0 |
| tolerant_use_vs_lowest_latency_pct_points | 82.6 |
