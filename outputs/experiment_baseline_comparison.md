# Experiment 5: Baseline Comparison

Simple baselines confirm that policy over transport retains information that naive routing policies throw away.

| Policy | Strict healthy success % | Tolerant degraded utilization % | Healthy headroom preserved % | Mean PRS | Mean FAE | Rejections |
| --- | --- | --- | --- | --- | --- | --- |
| orchestrator | 95.5 | 82.6 | 100.0 | 0.004 | 3.024 | 0 |
| lowest_latency | 100.0 | 0.0 | 0.0 | 0.0 | 3.354 | 0 |
| binary_health_only | 100.0 | 0.0 | 0.0 | 0.0 | 3.354 | 0 |
| capacity_only | 31.3 | 65.2 | 100.0 | 0.019 | 0.776 | 0 |
