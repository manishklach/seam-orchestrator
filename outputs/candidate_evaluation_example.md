# Candidate Evaluation Example

Representative interactive routing decision from Scenario E.

This is the core explainability surface: each candidate carries `PathState`, `GFS`, `PRS`, `FAE`, capacity snapshot, admissibility, and the final reason.

| Candidate path | PathState | GFS | PRS | FAE | Capacity | Admissible | Chosen | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pool-0-degraded | degraded_usable | 0.301 | 0.018 | 0.00 | 0/8 | no | no | jitter 5.5ms exceeds budget 3.5ms |
| pool-1-healthy | healthy | 0.033 | 0.000 | 0.00 | 0/8 | yes | yes | healthy path |
