# Seam Orchestrator

Transport-agnostic KV orchestration and workload-aware routing for disaggregated inference.

Disaggregated inference needs a glue layer between heterogeneous prefill and decode domains. The interesting systems question is no longer only "can bytes move?" It is "is this path admissible for this workload right now?"

Seam Orchestrator explores that control layer. It sits above transport backends such as mock transports, NIXL, or UCX, evaluates candidate paths using workload-aware policy, and makes explainable routing decisions that treat admissibility as a first-class concept.

## What This Repo Is

`seam-orchestrator` is a transport-agnostic orchestration layer for KV movement in disaggregated inference. It is designed as a policy/control layer above transport backends, not as a replacement for them.

The thesis is:

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

AWS publicly announced support for NIXL with EFA for LLM inference workloads, and AWS/Cerebras publicly described a disaggregated deployment where Trainium performs prefill, Cerebras performs decode, and KV cache moves over EFA between them. That is the broader context for this repo: not replacing transport, but adding a policy layer above it.

References:

- [AWS adds support for NIXL with EFA](https://aws.amazon.com/about-aws/whats-new/2026/03/aws-support-nixl-with-efa/)
- [Cerebras is coming to AWS](https://www.cerebras.ai/blog/cerebras-is-coming-to-aws)

## Architecture

![Seam Orchestrator Architecture](docs/architecture.svg)

The application/session layer decides that a request needs decode capacity. Seam Orchestrator evaluates candidate paths, applies workload-aware admissibility and routing policy, and emits candidate explanations. The transport backend below it only moves KV state.

### Terminology

| Term | Meaning |
| --- | --- |
| pool | decode resource group |
| path | transfer path to that pool |
| candidate | pool + path + current state snapshot |

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

## Why Not Just Use NIXL Directly?

NIXL and similar transport layers solve byte movement. Seam Orchestrator solves workload-aware admissibility and routing above transport.

- transport backend: can the KV payload move?
- orchestrator: should this candidate path carry this workload now?

These layers are complementary, not mutually exclusive.

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

Takeaway: a path can remain live and still become workload-selective rather than universally usable.

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

### Scenario F Summary

| Candidate path | Health posture | Capacity posture | Policy outcome |
| --- | --- | --- | --- |
| `pool-healthy-tight` | `HEALTHY` | Near soft limit | Preserved for stricter workloads |
| `pool-degraded-roomy` | `DEGRADED_USABLE` | More headroom | Used for tolerant work when admissible |

Takeaway: the healthiest path is not always the selected path when policy must protect headroom for stricter work.

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

## Concrete Output

Representative simulator output:

```text
Workload    | Chosen path     | Outcome               | Routing rationale
------------+-----------------+-----------------------+--------------------------------------------------------------
batch       | pool-0-degraded | rerouted_to_alternate | headroom_first selected pool-0-degraded over healthier pool-1-healthy
interactive | pool-1-healthy  | admitted              | healthy admissible pool selected
release     | pool-1-healthy  | admitted              | healthy admissible pool selected
```

Phase 2 evaluation highlights from `python evaluate.py`:

- strict workloads preserved on healthy paths: `97.9%`
- tolerant workloads admitted to degraded-but-usable paths: `91.7%`
- capacity-pressure batch reroutes observed: `24 / 24` evaluation trials

Phase 2 committed artifacts:

- [outputs/scenario_summary.md](outputs/scenario_summary.md)
- [outputs/scenario_e_table.md](outputs/scenario_e_table.md)
- [outputs/scenario_f_table.md](outputs/scenario_f_table.md)
- [outputs/decision_trace_scenario_e.json](outputs/decision_trace_scenario_e.json)
- [outputs/decision_trace_scenario_f.json](outputs/decision_trace_scenario_f.json)
- [outputs/evaluation_summary.md](outputs/evaluation_summary.md)

## Repository Layout

```text
README.md
evaluate.py
orchestrator.py
pipeline.py
transport.py
simulate.py
docs/
  architecture.md
  decision-model.md
  scenarios.md
  extensions.md
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

Generate Phase 2 artifacts and a lightweight evaluation pass:

```bash
python evaluate.py
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

## What's Next

- broader evaluation sweeps and replay tooling
- richer explainability views over candidate decisions
- checkpoint/storage admissibility as a second embodiment
- broader policy surfaces beyond KV routing while preserving the same architecture

## Further Reading

- [docs/architecture.md](docs/architecture.md)
- [docs/decision-model.md](docs/decision-model.md)
- [docs/scenarios.md](docs/scenarios.md)
- [docs/extensions.md](docs/extensions.md)
