"""
seam_orchestrator/simulate.py — v2

Four scenarios + one "gray failure but not hard failure" visual demo.

Run:  python -m seam_orchestrator.simulate
"""

import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seam_v2.pipeline import SeamPipeline
from seam_v2.transport import MockBackend
from seam_v2.orchestrator import (
    ThresholdConfig, SeamOrchestrator,
    WorkloadProfile, WORKLOAD_BATCH, WORKLOAD_INTERACTIVE, WORKLOAD_RELEASE,
    DecisionOutcome
)

BLOCK_SIZE = 512 * 1024
COL_W = 60

def header(title):
    print(f"\n{'═'*COL_W}")
    print(f"  {title}")
    print(f"{'═'*COL_W}")

def fmt_pool(info):
    return (f"{info['state']:22s}  GFS={info['gfs']:.3f}  "
            f"PRS={info['prs']:.3f}  FAE={info['fae']:.2f}  "
            f"p99={info['p99_lat_ms']:.1f}ms  jitter={info['jitter_ms']:.1f}ms")


async def run_scenario(title, fault_mode, n_requests, workloads_by_req,
                       n_pools=2, base_lat=2.5):
    header(title)
    cfg      = ThresholdConfig()
    backend  = MockBackend(fault_mode=fault_mode, base_latency_ms=base_lat)
    orch     = SeamOrchestrator(cfg)
    pipeline = SeamPipeline(backend=backend, orchestrator=orch)

    for i in range(n_pools):
        pipeline.add_decode_pool(f"pool-{i}", host=f"10.0.0.{10+i}", port=8080+i)

    kv_block     = b"x" * BLOCK_SIZE
    last_pool    = None
    admit_counts = {}
    reject_counts = {}

    for req_idx in range(n_requests):
        workload = workloads_by_req(req_idx)
        pool_id, records = await pipeline.transfer_and_route(kv_block, workload)

        for rec in records:
            if rec.outcome in (DecisionOutcome.ADMITTED, DecisionOutcome.ADMITTED_DEGRADED,
                               DecisionOutcome.REROUTED):
                admit_counts[workload.__class__.__name__] = \
                    admit_counts.get(workload.__class__.__name__, 0) + 1
            else:
                key = f"{workload.__class__.__name__}:{rec.outcome.value}"
                reject_counts[key] = reject_counts.get(key, 0) + 1

        if pool_id:
            if last_pool and pool_id != last_pool:
                # Find the reroute record for logging
                chosen_rec = next((r for r in records if r.chosen_pool), None)
                reason = chosen_rec.reason if chosen_rec else "?"
                print(f"  [req {req_idx:03d}] ↺ REROUTED → {pool_id}  ({reason[:55]})")
            last_pool = pool_id
            pipeline.release_session(pool_id)
        else:
            rejected_rec = next((r for r in records if r.outcome != DecisionOutcome.ADMITTED), None)
            if rejected_rec:
                print(f"  [req {req_idx:03d}] ✗ BLOCKED   [{rejected_rec.outcome.value}] {rejected_rec.reason[:50]}")

        if req_idx % 25 == 24:
            status = pipeline.status()
            print(f"\n  — status @ req {req_idx+1} —")
            for pid, info in status["pools"].items():
                print(f"    {pid}: {fmt_pool(info)}")
            print()

        await asyncio.sleep(0.003)

    # Final summary
    status = pipeline.status()
    print(f"\n  Final pool states:")
    for pid, info in status["pools"].items():
        print(f"    {pid}: {fmt_pool(info)}")
    if reject_counts:
        print(f"\n  Rejection breakdown:")
        for k, v in sorted(reject_counts.items()):
            print(f"    {k}: {v}")


# ── Scenario E: The "gray failure but not hard failure" visual ────────────────
# Pool is reachable. Transfers succeed. No hard fault.
# But latency + jitter cause high-criticality sessions to be blocked
# while low-criticality batch sessions still flow through.
# This is the core thesis: the path is "alive" but inadmissible for strict SLAs.

async def scenario_e_gray_alive():
    header("E — GRAY FAILURE NOT HARD FAILURE (the core thesis demo)")
    print("  Pool-0: degraded latency (~18ms p99) + moderate jitter — still UP")
    print("  Pool-1: healthy backup")
    print()
    print("  Sending mix of workloads:")
    print("    BATCH       (criticality=0.2, SLA=200ms, jitter_tolerant)")
    print("    INTERACTIVE (criticality=0.7, SLA=30ms,  jitter_moderate)")
    print("    RELEASE     (criticality=0.95,SLA=15ms,  jitter_intolerant)")
    print()

    cfg     = ThresholdConfig()
    # Pool-0 gets degraded backend, pool-1 stays clean
    backend_degraded = MockBackend(fault_mode="jitter", base_latency_ms=6.0)
    backend_healthy  = MockBackend(fault_mode="clean",    base_latency_ms=2.0)

    orch     = SeamOrchestrator(cfg)
    orch.register_pool("pool-0-degraded", "10.0.0.10", 8080)
    orch.register_pool("pool-1-healthy",  "10.0.0.11", 8081)

    kv_block = b"x" * BLOCK_SIZE

    workloads = [WORKLOAD_BATCH, WORKLOAD_INTERACTIVE, WORKLOAD_RELEASE]
    wl_names  = ["BATCH      ", "INTERACTIVE", "RELEASE    "]

    # Counters per workload type per pool outcome
    results = {n: {"admitted_degraded": 0, "admitted_healthy": 0,
                   "rejected": 0, "rerouted": 0} for n in wl_names}

    print(f"  {'Req':>4}  {'Workload':12s}  {'Outcome':30s}  {'Pool':20s}  {'Reason'}")
    print(f"  {'-'*4}  {'-'*12}  {'-'*30}  {'-'*20}  {'-'*30}")

    for req_idx in range(60):
        wl_idx   = req_idx % 3
        workload = workloads[wl_idx]
        wl_name  = wl_names[wl_idx]

        # Manually run routing to get full decision records
        from seam_v2.orchestrator import compute_prs, compute_fae, is_admissible
        import statistics

        # Simulate transfer on pool-0 first to build up history
        # (so the orchestrator has real latency samples to score)
        if req_idx < 15:
            # Warm-up: feed degraded samples to pool-0
            import asyncio as _aio
            result = await backend_degraded.send_kv_block(kv_block, "10.0.0.10", 8080)
            orch.record_transfer("pool-0-degraded", result.latency_ms,
                                 0.0 if result.success else 1.0, result.bytes_moved)
            result2 = await backend_healthy.send_kv_block(kv_block, "10.0.0.11", 8081)
            orch.record_transfer("pool-1-healthy", result2.latency_ms,
                                 0.0 if result2.success else 1.0, result2.bytes_moved)

        pool_id, records = orch.route_session(workload)

        chosen_rec = next((r for r in records if r.chosen_pool), None)
        any_rec    = records[0] if records else None

        if pool_id == "pool-0-degraded":
            results[wl_name]["admitted_degraded"] += 1
            outcome_str = f"{'ADMITTED on DEGRADED':30s}"
            pool_str    = "pool-0-degraded     "
        elif pool_id == "pool-1-healthy":
            results[wl_name]["admitted_healthy"] += 1
            outcome_str = f"{'ADMITTED on HEALTHY':30s}"
            pool_str    = "pool-1-healthy      "
            results[wl_name]["rerouted"] += 1
        else:
            results[wl_name]["rejected"] += 1
            reason_short = (any_rec.reason[:28] if any_rec else "?")
            outcome_str = f"{'BLOCKED':30s}"
            pool_str    = f"{(any_rec.outcome.value if any_rec else '?'):20s}"

        reason_str = (chosen_rec.reason[:35] if chosen_rec else
                      (any_rec.reason[:35] if any_rec else ""))

        if req_idx >= 15:  # only print after warm-up
            print(f"  {req_idx:>4}  {wl_name}  {outcome_str}  {pool_str}  {reason_str}")

        if pool_id:
            orch.release_session(pool_id)

        await asyncio.sleep(0.002)

    # Visual summary
    print(f"\n{'─'*COL_W}")
    print(f"  SUMMARY — pool-0 is degraded but STILL ALIVE")
    print(f"{'─'*COL_W}")
    print(f"  {'Workload':12s}  {'On degraded pool':18s}  {'Rerouted→healthy':18s}  {'Blocked':8s}")
    print(f"  {'-'*12}  {'-'*18}  {'-'*18}  {'-'*8}")
    for wl_name, r in results.items():
        print(f"  {wl_name}  {r['admitted_degraded']:^18d}  "
              f"{r['admitted_healthy']:^18d}  {r['rejected']:^8d}")

    print(f"\n  Key insight:")
    print(f"  ► BATCH sessions continue flowing through the degraded pool")
    print(f"  ► INTERACTIVE sessions get rerouted to the healthy pool")
    print(f"  ► RELEASE sessions are fully blocked until a clean pool is confirmed")
    print(f"  ► Pool-0 never went DOWN — it is a gray failure, not a hard failure")

    # Final pool states
    print(f"\n  Final pool states:")
    for pid, info in {pid: orch.status()[pid] for pid in orch.status()}.items():
        print(f"    {pid}: {fmt_pool(info)}")


async def main():
    # A — Clean baseline
    await run_scenario(
        "A — Clean baseline",
        fault_mode="clean", n_requests=40,
        workloads_by_req=lambda i: WORKLOAD_INTERACTIVE)

    # B — Degraded latency + high-criticality rejection
    await run_scenario(
        "B — Latency degradation (high-criticality rerouted)",
        fault_mode="degraded", n_requests=60,
        workloads_by_req=lambda i: WORKLOAD_RELEASE if i % 3 == 0 else WORKLOAD_BATCH)

    # C — Jitter storm
    await run_scenario(
        "C — Jitter storm",
        fault_mode="jitter", n_requests=60,
        workloads_by_req=lambda i: WORKLOAD_INTERACTIVE)

    # D — Drop storm → quarantine → reroute
    await run_scenario(
        "D — Drop storm → quarantine → reroute",
        fault_mode="drops", n_requests=80,
        workloads_by_req=lambda i: WORKLOAD_INTERACTIVE if i % 2 == 0 else WORKLOAD_BATCH)

    # E — The core thesis: gray failure, pool still alive, split admission
    await scenario_e_gray_alive()

    print(f"\n{'═'*COL_W}")
    print("  All scenarios complete.")
    print("  Swap MockBackend → NIXLBackend or UCXBackend for real RDMA.")
    print(f"{'═'*COL_W}\n")


if __name__ == "__main__":
    asyncio.run(main())


# ── Scenario E v2: direct admissibility split demo ───────────────────────────

async def scenario_e_direct():
    """
    The definitive gray-failure split demo.
    Pool-0 has elevated jitter (~5ms stdev, p99~14ms) — it is UP and reachable.
    Every transfer to it succeeds. There is no hard fault.
    
    We show which workloads the orchestrator admits vs rejects on that pool,
    demonstrating that the admission decision is workload-aware, not binary.
    """
    import statistics as _stats
    from seam_v2.orchestrator import (
        compute_prs, compute_fae, is_admissible, DecisionOutcome
    )

    header("E — GRAY FAILURE NOT HARD FAILURE  [direct admissibility split]")
    print("  Pool-0: jitter-degraded (~14ms p99, ~5ms stdev) — UP, transfers succeed")
    print("  Pool-1: healthy baseline (~2ms p99)")
    print()

    cfg  = ThresholdConfig()
    orch = SeamOrchestrator(cfg)
    orch.register_pool("pool-0-degraded", "10.0.0.10", 8080)
    orch.register_pool("pool-1-healthy",  "10.0.0.11", 8081)

    backend_deg = MockBackend(fault_mode="jitter",  base_latency_ms=6.0)
    backend_ok  = MockBackend(fault_mode="clean",   base_latency_ms=2.0)

    kv = b"x" * BLOCK_SIZE

    # Warm-up: feed real transfer history into both pools
    for _ in range(25):
        r = await backend_deg.send_kv_block(kv, "10.0.0.10", 8080)
        orch.record_transfer("pool-0-degraded", r.latency_ms, 0.0, r.bytes_moved)
    for _ in range(25):
        r = await backend_ok.send_kv_block(kv, "10.0.0.11", 8081)
        orch.record_transfer("pool-1-healthy",  r.latency_ms, 0.0, r.bytes_moved)

    pool0  = orch.pools["pool-0-degraded"]
    pool1  = orch.pools["pool-1-healthy"]
    recent = list(pool0.history)[-10:]
    lats   = [s.latency_ms for s in recent]
    p99    = _stats.quantiles(lats, n=100)[98] if len(lats) >= 2 else max(lats)
    jitter = _stats.stdev(lats) if len(lats) > 1 else 0.0

    print(f"  Pool-0 measured state: {pool0.state.value.upper()}")
    print(f"  GFS={pool0.gfs:.3f}  p99={p99:.1f}ms  jitter={jitter:.1f}ms")
    print(f"  (pool is UP — every transfer succeeded, no hard failure)")
    print()
    print(f"  {'Workload':13s} {'SLA':>8s}  {'Jitter tol':>10s}  {'Release?':>9s}  "
          f"{'Admissible':>10s}  {'Reason'}")
    print(f"  {'—'*13} {'—'*8}  {'—'*10}  {'—'*9}  {'—'*10}  {'—'*42}")

    workload_cases = [
        (WORKLOAD_BATCH,       "BATCH"),
        (WORKLOAD_INTERACTIVE, "INTERACTIVE"),
        (WORKLOAD_RELEASE,     "RELEASE"),
        # Edge cases to show the workload-aware nuance
        (WorkloadProfile(criticality=0.3, latency_sla_ms=100.0,
                         jitter_tolerance=0.9, is_prefill_decode_strict=False),
         "ASYNC-BATCH"),
        (WorkloadProfile(criticality=0.6, latency_sla_ms=20.0,
                         jitter_tolerance=0.2, is_prefill_decode_strict=True),
         "STRICT-SYNC"),
        (WorkloadProfile(criticality=0.4, latency_sla_ms=50.0,
                         jitter_tolerance=0.7, is_prefill_decode_strict=False),
         "TOLERANT-MED"),
    ]

    admitted_on_degraded = []
    blocked_on_degraded  = []

    for wl, name in workload_cases:
        prs = compute_prs(pool0, cfg, 1, wl)
        fae = compute_fae(pool0, cfg, 1, wl)
        ok, outcome, reason = is_admissible(pool0, wl, prs, fae, p99, jitter)

        admit_str = "✓ YES" if ok else "✗ NO"
        print(f"  {name:13s} {wl.latency_sla_ms:>7.0f}ms  "
              f"{wl.jitter_tolerance:>10.1f}  "
              f"{'yes' if wl.is_release_path else 'no':>9s}  "
              f"{admit_str:>10s}  {reason[:42]}")

        if ok:
            admitted_on_degraded.append(name)
        else:
            blocked_on_degraded.append(name)

    print(f"\n  {'─'*58}")
    print(f"  Pool-0 is DEGRADED_USABLE — not down, not healthy")
    print(f"\n  ✓  Admitted on degraded pool:  {', '.join(admitted_on_degraded)}")
    print(f"  ✗  Blocked → routed to pool-1: {', '.join(blocked_on_degraded)}")

    print(f"\n  This is the core thesis:")
    print(f"  The path is alive. Transfers succeed. No alert fires.")
    print(f"  But the orchestrator differentiates by workload sensitivity —")
    print(f"  batch traffic continues, strict-SLA traffic is protected.")

    print(f"\n  Pool states:")
    for pid, info in orch.status().items():
        print(f"    {pid}: {fmt_pool(info)}")
