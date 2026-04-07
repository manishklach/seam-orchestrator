# Replay Comparison Table

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
