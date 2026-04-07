# Replay Summary

Replay compares the same request stream across naive policies and Seam Orchestrator. The goal is not transport benchmarking; it is auditability and side-by-side policy contrast.

## Policy Metrics

| Policy | Requests | Strict choices | Strict healthy % | Tolerant degraded % | Strict protected | Headroom preserved | No-pool count |
| --- | --- | --- | --- | --- | --- | --- | --- |
| lowest_latency | 10 | 5 | 33.3 | 75.0 | 0 | 1 | 1 |
| binary_health_only | 10 | 5 | 66.7 | 50.0 | 2 | 0 | 1 |
| capacity_only | 10 | 5 | 33.3 | 100.0 | 0 | 2 | 1 |
| seam_orchestrator | 10 | 5 | 66.7 | 100.0 | 2 | 2 | 1 |

Replay takeaway: Seam Orchestrator exposes an auditable decision record for the same request stream, making it clear when strict workloads were kept on healthier paths, when lower-latency but jitterier paths were avoided for tail protection, and when degraded-but-usable capacity was deliberately spent on tolerant work.


## Request-Level Comparison

| Request | Workload | Lowest latency | Binary health only | Capacity only | Seam Orchestrator | Strict protected | Headroom preserved |
| --- | --- | --- | --- | --- | --- | --- | --- |
| req-001 | batch | pool-degraded | pool-healthy | pool-degraded | pool-degraded | no | yes |
| req-002 | interactive | pool-degraded | pool-healthy | pool-degraded | pool-healthy | yes | no |
| req-003 | release | pool-healthy | pool-healthy | pool-healthy | pool-healthy | yes | no |
| req-004 | batch | pool-healthy-a | pool-healthy-a | pool-degraded | pool-degraded | no | yes |
| req-005 | interactive | pool-degraded | pool-healthy-a | pool-degraded | pool-healthy-a | yes | no |
| req-006 | batch | pool-primary-degraded | pool-primary-degraded | pool-primary-degraded | pool-primary-degraded | no | no |
| req-007 | release | - | - | - | - | no | no |
| req-008 | interactive | pool-b | pool-b | pool-b | pool-b | no | no |
| req-009 | batch | pool-b | pool-b | pool-b | pool-b | no | yes |
| req-010 | release | pool-b | pool-b | pool-b | pool-b | yes | no |
