# Evaluation Summary

First lightweight aggregate pass over the current scenarios. The goal is not benchmarking transport; it is making the policy layer visibly measurable.

| Metric | Value |
| --- | --- |
| trials | 24 |
| admissions | 93 |
| reroutes | 51 |
| rejections | 0 |
| admitted_despite_degradation | 49 |
| strict_workloads_preserved_on_healthy_pct | 97.9 |
| tolerant_workloads_admitted_to_degraded_pct | 91.7 |
| capacity_pressure_batch_reroutes | 24 |
| restores_observed | 0 |

## State Transition Counts

| Transition | Count |
| --- | --- |
| healthy->degraded_usable | 50 |
| degraded_usable->degraded_restricted | 17 |
| degraded_restricted->degraded_usable | 14 |
| degraded_usable->restored | 3 |
| restored->degraded_usable | 2 |
| restored->healthy | 1 |
