# Blog Notes

## Title Ideas

- Policy Over Transport for Disaggregated Inference
- KV Cache Transfer Needs a Policy Layer Above Transport
- Workload-Aware Routing for Disaggregated Inference
- Beyond Byte Movement: Admissibility and Routing at the Prefill/Decode Seam

## Quoteable Lines

- The interesting question in disaggregated inference is no longer only "can bytes move?" It is "is this path admissible for this workload right now?"
- The glue layer is prototypeable and backend-swappable, but the higher-leverage software layer sits above transport in admissibility, routing, hysteresis, and explainability.
- Healthy-path preservation is a policy decision, not a transport primitive.

## Key Findings

- Strict workloads stayed on healthy paths in `95.5%` of baseline-comparison trials.
- Tolerant workloads were still admitted to degraded-but-usable paths in `82.6%` of baseline-comparison trials.
- Healthy-path headroom preservation reached `100.0%` in the headroom-opportunity cases exercised by the sweep.
- Staged restore avoided `7` oscillations versus a no-hysteresis baseline on the synthetic noisy trace.
- Alternate-path scarcity raised both `PRS` and `FAE`, making topology dependence visible in the decision record.

## Public Framing

- This repo is a policy layer above transport for KV movement in disaggregated inference.
- It is not a NIXL clone, not a transport benchmark war, and not a generic failure dashboard.
- It does show that a replacement-capable prototype path exists for the glue layer.
- The more strategically important software layer still sits above transport.

## Useful Search Terms

- disaggregated inference
- KV cache transfer
- workload-aware routing
- policy over transport
- admissibility
- NIXL
- EFA
- heterogeneous prefill/decode
- gray failure
- ai infrastructure
