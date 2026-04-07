# Replacement Path

This repo now demonstrates a credible replacement prototype path for the KV-transfer glue layer.

That statement needs to be precise.

## What Is Replaceable Here?

The repo shows that the glue layer between disaggregated prefill and decode domains is prototypeable behind a stable orchestration surface.

In this prototype, the following are replaceable or swappable:

- transport backend implementation
- candidate path evaluation inputs
- routing policy over those candidates
- explanation and decision-record generation

Today that backend layer is represented by:

- `MockBackend`
- `NIXLBackend`
- `UCXBackend`

That means the glue layer is not locked to a single backend choice.

## What Remains Backend-Dependent?

This repo does not claim to reproduce the full capabilities of mature production transport stacks.

Backend-dependent concerns still include:

- transport implementation details
- wire-level performance characteristics
- deployment-specific integration work
- hardware/fabric-specific optimizations

This is why the repo is careful not to present itself as a full production replacement for NIXL or UCX/libfabric.

## Why Replacement Feasibility Matters

The existence of a working replacement-capable prototype path matters for two reasons.

First, it shows that the KV-transfer glue layer can be modeled, abstracted, and rebuilt behind a stable interface.

Second, it highlights where the differentiating software value may sit:

- workload-aware admissibility
- routing under degraded or capacity-constrained conditions
- hysteresis and staged restore
- decision records and routing explainability
- policy over heterogeneous backend choices

In other words, the strategic control point is not only byte movement itself. It is the interface plus the policy layer that decides when and how candidate paths should be used.

## The Honest Claim

The honest claim is:

- a replacement-capable prototype path exists
- backend choices are swappable
- the orchestration layer above transport is where much of the interesting system behavior lives

The repo does not claim:

- production parity with mature transport stacks
- exhaustive backend support
- transport benchmark leadership

That balance is intentional.
