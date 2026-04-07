# Scenario F Output Snippet

Policy takeaway: health is not the only objective; headroom can dominate among admissible candidates.

| Workload | Chosen path | Outcome | Routing rationale |
| --- | --- | --- | --- |
| batch | pool-degraded-roomy | rerouted_to_alternate | headroom_first selected pool-degraded-roomy over healthier pool-healthy-tight |
| interactive-sync | pool-healthy-tight | admitted | healthy admissible pool selected |
| release | pool-healthy-tight | admitted | healthy admissible pool selected |
