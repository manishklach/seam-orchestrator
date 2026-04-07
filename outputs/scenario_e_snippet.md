# Scenario E Output Snippet

Category-defining takeaway: the path stays up, transport succeeds, and admissibility still splits by workload.

| Workload | Chosen path | Outcome | Routing rationale |
| --- | --- | --- | --- |
| batch | pool-0-degraded | rerouted_to_alternate | headroom_first selected pool-0-degraded over healthier pool-1-healthy |
| interactive | pool-1-healthy | admitted | healthy admissible pool selected |
| release | pool-1-healthy | admitted | healthy admissible pool selected |
