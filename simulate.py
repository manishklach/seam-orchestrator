"""
Scenario driver for the seam orchestrator prototype.

Run:
  python simulate.py --scenario E
  python simulate.py --scenario F
  python simulate.py --scenario all
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

from orchestrator import (
    CandidateExplanation,
    SeamOrchestrator,
    WorkloadProfile,
    WORKLOAD_BATCH,
    WORKLOAD_INTERACTIVE,
    WORKLOAD_RELEASE,
)
from transport import MockBackend

BLOCK_SIZE = 512 * 1024
OUTPUTS_DIR = Path("outputs")
SUMMARY_HEADERS = ["Workload", "Chosen path", "Outcome", "Routing rationale"]
CANDIDATE_HEADERS = [
    "Candidate path",
    "PathState",
    "GFS",
    "PRS",
    "FAE",
    "Capacity",
    "Admissible",
    "Chosen",
    "Reason",
]


def header(title: str) -> None:
    line = "=" * len(title)
    print(f"\n{line}\n{title}\n{line}")


def format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(str(cell)))
    pieces = [
        " | ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(headers)),
        "-+-".join("-" * width for width in widths),
    ]
    for row in rows:
        pieces.append(
            " | ".join(str(cell).ljust(widths[idx]) for idx, cell in enumerate(row))
        )
    return "\n".join(pieces)


def short_state(candidate: CandidateExplanation) -> str:
    return candidate.path_state.value


def render_candidate_rows(candidates: Iterable[CandidateExplanation]) -> List[List[str]]:
    rows: List[List[str]] = []
    for candidate in candidates:
        capacity = candidate.capacity_snapshot
        rows.append(
            [
                candidate.pool_id,
                short_state(candidate),
                f"{candidate.gfs:.3f}",
                f"{candidate.prs:.3f}",
                f"{candidate.fae:.2f}",
                f"{capacity.active_sessions}/{capacity.max_capacity}",
                "yes" if candidate.admissible else "no",
                "yes" if candidate.chosen else "no",
                candidate.skipped_reason or candidate.primary_reason,
            ]
        )
    return rows


async def warm_pool(
    orchestrator: SeamOrchestrator,
    pool_id: str,
    backend: MockBackend,
    *,
    repeats: int = 24,
) -> None:
    kv_data = b"x" * BLOCK_SIZE
    pool = orchestrator.pools[pool_id]
    for _ in range(repeats):
        result = await backend.send_kv_block(kv_data, pool.host, pool.port)
        orchestrator.record_transfer(
            pool_id,
            result.latency_ms,
            0.0 if result.success else 1.0,
            result.bytes_moved,
        )


async def scenario_a_clean_baseline() -> None:
    header("Scenario A - Clean baseline")
    orchestrator = SeamOrchestrator(event_log_path=OUTPUTS_DIR / "scenario_a_events.jsonl")
    orchestrator.register_pool("pool-a", "10.0.0.10", 8080)
    orchestrator.register_pool("pool-b", "10.0.0.11", 8081)
    backend = MockBackend(fault_mode="clean", base_latency_ms=2.0)
    await warm_pool(orchestrator, "pool-a", backend)
    await warm_pool(orchestrator, "pool-b", backend)
    _, decision = orchestrator.route_session(WORKLOAD_INTERACTIVE)
    print(format_table(CANDIDATE_HEADERS, render_candidate_rows(decision.candidate_explanations)))


async def scenario_b_degraded_latency() -> None:
    header("Scenario B - Latency degradation")
    orchestrator = SeamOrchestrator(event_log_path=OUTPUTS_DIR / "scenario_b_events.jsonl")
    orchestrator.register_pool("pool-degraded", "10.0.0.10", 8080)
    orchestrator.register_pool("pool-healthy", "10.0.0.11", 8081)
    await warm_pool(orchestrator, "pool-degraded", MockBackend("degraded", 6.0))
    await warm_pool(orchestrator, "pool-healthy", MockBackend("clean", 2.0))
    _, decision = orchestrator.route_session(WORKLOAD_RELEASE)
    print(format_table(CANDIDATE_HEADERS, render_candidate_rows(decision.candidate_explanations)))


async def scenario_c_jitter_storm() -> None:
    header("Scenario C - Jitter storm")
    orchestrator = SeamOrchestrator(event_log_path=OUTPUTS_DIR / "scenario_c_events.jsonl")
    orchestrator.register_pool("pool-jitter", "10.0.0.10", 8080)
    orchestrator.register_pool("pool-clean", "10.0.0.11", 8081)
    await warm_pool(orchestrator, "pool-jitter", MockBackend("jitter", 7.0))
    await warm_pool(orchestrator, "pool-clean", MockBackend("clean", 2.0))
    _, decision = orchestrator.route_session(WORKLOAD_INTERACTIVE)
    print(format_table(CANDIDATE_HEADERS, render_candidate_rows(decision.candidate_explanations)))


async def scenario_d_drop_quarantine() -> None:
    header("Scenario D - Drop storm to quarantine")
    orchestrator = SeamOrchestrator(event_log_path=OUTPUTS_DIR / "scenario_d_events.jsonl")
    orchestrator.register_pool("pool-drops", "10.0.0.10", 8080)
    orchestrator.register_pool("pool-clean", "10.0.0.11", 8081)
    await warm_pool(orchestrator, "pool-drops", MockBackend("drops", 5.0), repeats=40)
    await warm_pool(orchestrator, "pool-clean", MockBackend("clean", 2.0), repeats=12)
    _, decision = orchestrator.route_session(WORKLOAD_INTERACTIVE)
    print(format_table(CANDIDATE_HEADERS, render_candidate_rows(decision.candidate_explanations)))


async def scenario_e_gray_failure() -> None:
    header("Scenario E - Gray failure but not hard failure")
    print("The path stays up, transfers still succeed, and admission splits by workload.")
    print("Tolerant traffic can use the degraded path while stricter traffic moves cleanly.\n")

    orchestrator = SeamOrchestrator(event_log_path=OUTPUTS_DIR / "scenario_e_events.jsonl")
    orchestrator.register_pool("pool-0-degraded", "10.0.0.10", 8080, max_capacity=8)
    orchestrator.register_pool("pool-1-healthy", "10.0.0.11", 8081, max_capacity=8)

    await warm_pool(orchestrator, "pool-0-degraded", MockBackend("jitter", 6.0), repeats=28)
    await warm_pool(orchestrator, "pool-1-healthy", MockBackend("clean", 2.0), repeats=20)

    workloads = [
        WORKLOAD_BATCH,
        WORKLOAD_INTERACTIVE,
        WORKLOAD_RELEASE,
    ]
    summary_rows: List[List[str]] = []

    for workload in workloads:
        pool_id, decision = orchestrator.route_session(workload)
        chosen = pool_id or "-"
        summary_rows.append(
            [
                workload.name,
                chosen,
                decision.outcome.value,
                decision.reason,
            ]
        )
        if pool_id:
            orchestrator.release_session(pool_id)

    print(format_table(SUMMARY_HEADERS, summary_rows))

    print("\nDecision record for batch traffic:\n")
    _, batch_decision = orchestrator.route_session(WORKLOAD_BATCH)
    print(format_table(CANDIDATE_HEADERS, render_candidate_rows(batch_decision.candidate_explanations)))
    if batch_decision.chosen_pool_id:
        orchestrator.release_session(batch_decision.chosen_pool_id)

    print("\nDecision record for interactive traffic:\n")
    _, interactive_decision = orchestrator.route_session(WORKLOAD_INTERACTIVE)
    print(format_table(CANDIDATE_HEADERS, render_candidate_rows(interactive_decision.candidate_explanations)))
    if interactive_decision.chosen_pool_id:
        orchestrator.release_session(interactive_decision.chosen_pool_id)


async def scenario_f_capacity_pressure() -> None:
    header("Scenario F - Capacity pressure under gray failure")
    print("The healthiest path is near soft capacity, so policy preserves it for stricter")
    print("workloads and uses a degraded-but-admissible path for tolerant traffic.\n")

    orchestrator = SeamOrchestrator(event_log_path=OUTPUTS_DIR / "scenario_f_events.jsonl")
    orchestrator.register_pool(
        "pool-healthy-tight",
        "10.0.0.10",
        8080,
        max_capacity=8,
        soft_capacity_fraction=0.75,
    )
    orchestrator.register_pool(
        "pool-degraded-roomy",
        "10.0.0.11",
        8081,
        max_capacity=8,
        soft_capacity_fraction=0.75,
    )

    await warm_pool(orchestrator, "pool-healthy-tight", MockBackend("clean", 2.0), repeats=24)
    await warm_pool(orchestrator, "pool-degraded-roomy", MockBackend("jitter", 6.0), repeats=24)

    orchestrator.set_active_sessions("pool-healthy-tight", 7)
    orchestrator.set_active_sessions("pool-degraded-roomy", 2)

    workloads = [
        WORKLOAD_BATCH,
        WorkloadProfile(
            name="interactive-sync",
            criticality=0.72,
            latency_sla_ms=25.0,
            sync_frequency=0.7,
            jitter_tolerance=0.25,
            is_prefill_decode_strict=True,
        ),
        WORKLOAD_RELEASE,
    ]

    summary_rows: List[List[str]] = []
    detailed_decisions: Dict[str, List[List[str]]] = {}

    for workload in workloads:
        pool_id, decision = orchestrator.route_session(workload)
        summary_rows.append(
            [
                workload.name,
                pool_id or "-",
                decision.outcome.value,
                decision.reason,
            ]
        )
        detailed_decisions[workload.name] = render_candidate_rows(
            decision.candidate_explanations
        )
        if pool_id:
            orchestrator.release_session(pool_id)

    print(format_table(SUMMARY_HEADERS, summary_rows))

    for workload_name in ("batch", "interactive-sync"):
        print(f"\nDecision record for {workload_name}:\n")
        print(format_table(CANDIDATE_HEADERS, detailed_decisions[workload_name]))


SCENARIOS: Dict[str, Callable[[], asyncio.Future]] = {
    "A": scenario_a_clean_baseline,
    "B": scenario_b_degraded_latency,
    "C": scenario_c_jitter_storm,
    "D": scenario_d_drop_quarantine,
    "E": scenario_e_gray_failure,
    "F": scenario_f_capacity_pressure,
}


async def run_selected(scenario_key: str) -> None:
    OUTPUTS_DIR.mkdir(exist_ok=True)
    if scenario_key == "all":
        for key in ("A", "B", "C", "D", "E", "F"):
            await SCENARIOS[key]()
        return
    await SCENARIOS[scenario_key]()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["all", "A", "B", "C", "D", "E", "F"],
        help="Scenario to run.",
    )
    return parser.parse_args()


def main() -> None:
    random.seed(7)
    logging.basicConfig(level=logging.ERROR)
    args = parse_args()
    asyncio.run(run_selected(args.scenario))


if __name__ == "__main__":
    main()
