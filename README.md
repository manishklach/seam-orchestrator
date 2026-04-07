# seam-orchestrator

Transport-agnostic KV orchestration for disaggregated inference.

Disaggregated inference needs a glue layer between prefill and decode domains. `seam-orchestrator` explores that glue as a transport-agnostic orchestration layer for path selection, session routing, and workload-aware admissibility above the transport backend.

## What This Repo Is

`seam-orchestrator` is a prototype policy layer for KV movement in disaggregated inference systems. It sits above transport backends such as mock transports, NIXL, or UCX, and makes workload-aware routing and admissibility decisions across heterogeneous prefill and decode pools.

The thesis is simple:

- The interesting question is not only "can bytes move?"
- The interesting question is "should this path be used for this workload right now?"

That control point becomes strategically important in disaggregated systems, where KV transfer sits on the seam between prefill and decode.

## What This Repo Is Not

- Not a replacement for NIXL.
- Not a replacement for UCX, libfabric, or RDMA transport stacks.
- Not just a failure-injection toy.
- Not a generic monitoring dashboard.
- Not a transport microbenchmark project.

The transport backend remains intentionally narrow. The main artifact here is the policy layer above transport.

## Why This Matters

Disaggregated inference creates a new systems boundary: the seam between prefill and decode. Once KV state moves across that seam, the important systems question is no longer only whether bytes can move. The system has to decide:

- Is this path admissible for release-critical traffic right now?
- Should a degraded but still-live path remain open for batch traffic only?
- Is the healthier pool too close to capacity to spend on tolerant work?
- Does alternate-path scarcity change the risk of using a given pool?

AWS publicly announced support for NIXL with EFA for LLM inference workloads, and AWS/Cerebras publicly described a disaggregated deployment where Trainium performs prefill, Cerebras performs decode, and KV cache moves over EFA between them. That is the broader context for this prototype: not replacing transport, but adding a policy layer above it.

References:

- [AWS adds support for NIXL with EFA](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-support-nixl-with-efa/)
- [Cerebras is coming to AWS](https://www.cerebras.ai/blog/cerebras-is-coming-to-aws)

## Architecture

```text
application/session layer
        |
        v
seam-orchestrator
  - workload-aware admissibility
  - path selection and routing
  - candidate explanation bundles
  - staged restore and hysteresis
        |
        v
transport backends
  - MockBackend
  - NIXLBackend
  - UCXBackend
```

The application or session layer decides that a request needs decode capacity. The orchestrator decides which path is admissible, which candidate should be chosen, and why. The backend below it only moves KV state.

## Core Concepts

| Concept | Purpose |
| --- | --- |
| `PathState` | Non-binary path health model: `HEALTHY`, `DEGRADED_USABLE`, `DEGRADED_RESTRICTED`, `QUARANTINE_CANDIDATE`, `QUARANTINED`, `RESTORED`. |
| `GFS` | Gray Failure Score. A weighted multi-signal score over latency, jitter, drop behavior, plus a cross-signal interaction term. |
| `PRS` | Propagation Risk Score. Estimates how much a bad routing choice could spread risk based on workload sensitivity, path dependence, and alternate-path scarcity. |
| `FAE` | Failure Amplification Estimate. Connects local degradation to cluster-level blast radius. |
| `WorkloadProfile` | Workload descriptor carrying latency SLA, jitter tolerance, sync frequency, checkpoint size, and release sensitivity. |
| Candidate explanation bundle | Structured per-candidate explanation with `PathState`, `GFS`, `PRS`, `FAE`, admissibility, capacity, topology dependence, and chosen/skipped reason. |
| Hysteresis and staged restore | Fast escalation, slower recovery, and staged restore to avoid flapping. |

## Scenario E: Category-Defining Demo

Scenario E is the core proof of the thesis.

- The path is still up.
- Transfers still succeed.
- There is no hard failure.
- The path remains admissible for tolerant traffic.
- The same path is not admissible for stricter workloads.

### Scenario E Summary

| Candidate path | Observed condition | `PathState` | Batch admissibility | Interactive admissibility | Release-critical admissibility |
| --- | --- | --- | --- | --- | --- |
| `pool-0-degraded` | Reachable, elevated latency and jitter, KV transfer still succeeds | `DEGRADED_USABLE` | Admissible | Not admissible | Not admissible |
| `pool-1-healthy` | Clean backup path with low latency and low jitter | `HEALTHY` | Admissible | Admissible | Admissible |

### Why Scenario E Matters

This is the point of the project:

- Health is not binary.
- Admissibility is workload-relative.
- A still-alive path can be acceptable for low-criticality traffic and unacceptable for strict-SLA traffic.

## Scenario F: Capacity Pressure Under Gray Failure

Scenario F extends the thesis from admissibility into policy tradeoffs.

- One candidate path is healthier but near its soft capacity threshold.
- Another candidate path is degraded but still admissible for tolerant work.
- The orchestrator preserves the healthier path for stricter workloads and uses the degraded path for capacity-tolerant work when headroom matters more than raw health.

That makes the artifact stronger as a control-layer prototype: the healthiest path is not always the right choice when headroom is scarce.

## Explainability and Logs

Routing stays decomposable rather than collapsing into one opaque score:

1. Evaluate `PathState` and recent telemetry.
2. Compute `GFS`, `PRS`, and `FAE`.
3. Check workload-aware admissibility.
4. Evaluate capacity snapshot and alternate-path dependence.
5. Select from admissible paths using an explicit selection policy.

Every route decision produces a candidate explanation bundle with:

- candidate id
- `PathState`
- `GFS`
- `PRS`
- `FAE`
- admissible yes/no
- primary reason
- capacity snapshot
- chosen / skipped
- skipped reason

Structured JSONL event logs are written under `outputs/` and include:

- state transitions
- restore events
- admissions
- rejections
- reroutes

## Repository Layout

```text
README.md
orchestrator.py
pipeline.py
transport.py
simulate.py
docs/
  architecture.md
  decision-model.md
  scenarios.md
outputs/
```

## Quick Start

Requirements:

- Python 3.10+
- No external dependencies for the mock-backed scenarios

Run the two most important demos:

```bash
python simulate.py --scenario E
python simulate.py --scenario F
```

Run all scenarios:

```bash
python simulate.py --scenario all
```

## Design Notes

- This repo intentionally keeps transport integration narrow.
- The mock backend exists to exercise policy and routing behavior, not to benchmark transport.
- NIXL and UCX shims are included as lightweight adapters to show where real transfer backends plug in.
- The prototype stays compact on purpose: the goal is to make the policy thesis legible.

## Further Reading

- [docs/architecture.md](docs/architecture.md)
- [docs/decision-model.md](docs/decision-model.md)
- [docs/scenarios.md](docs/scenarios.md)
