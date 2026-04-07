# Scenario Summary

| Scenario | Name | Key condition | Key policy outcome | Notable result |
| --- | --- | --- | --- | --- |
| A | Clean baseline | Both decode paths remain healthy. | Policy is largely invisible when every path is healthy and uncongested. | Healthy path selection dominates with minimal explanation burden. |
| B | Latency degradation | Latency rises before hard failure. | High-criticality workloads stop using the degraded path before transport fails outright. | Admissibility shifts before binary reachability changes. |
| C | Jitter storm | Latency variance rises while paths remain live. | Jitter-sensitive workloads are protected without treating the path as fully down. | PathState and admissibility diverge from binary transport health. |
| D | Drop storm to quarantine | Persistent bad windows push a path toward quarantine. | Escalation is fast, recovery is slower, and hysteresis avoids flapping. | State transitions expose why a path becomes temporarily inadmissible. |
| E | Gray failure but not hard failure | A degraded path stays live and transfers succeed. | Admissibility splits by workload sensitivity. | Batch stays admissible on DEGRADED_USABLE while strict workloads move healthy. |
| F | Capacity pressure under gray failure | The healthiest path is near soft capacity while a degraded path keeps headroom. | Policy preserves healthy headroom for stricter workloads and spends degraded headroom on tolerant work. | The healthiest path is not always the selected path. |
