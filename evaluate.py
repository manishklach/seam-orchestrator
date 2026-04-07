"""
Lightweight Phase 2 evaluation and artifact generation for Seam Orchestrator.

This script produces:
  - aggregate evaluation metrics
  - scenario summary artifacts
  - representative decision traces
  - polished markdown tables for Scenario E and Scenario F
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from orchestrator import (
    DecisionOutcome,
    PathState,
    SeamOrchestrator,
    WorkloadProfile,
    WORKLOAD_BATCH,
    WORKLOAD_INTERACTIVE,
    WORKLOAD_RELEASE,
    to_jsonable,
)
from transport import MockBackend

OUTPUTS_DIR = Path("outputs")
BLOCK_SIZE = 512 * 1024


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    header_line = "| " + " | ".join(headers) + " |"
    separator = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join([header_line, separator, *body])


def chosen_candidate_from_decision(decision) -> Any:
    return next(
        (candidate for candidate in decision.candidate_explanations if candidate.chosen),
        None,
    )


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(payload), indent=2), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


async def warm_pool(
    orchestrator: SeamOrchestrator,
    pool_id: str,
    backend: MockBackend,
    *,
    repeats: int,
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


async def build_scenario_e() -> Tuple[SeamOrchestrator, Dict[str, Any]]:
    orchestrator = SeamOrchestrator(event_log_path=OUTPUTS_DIR / "scenario_e_events.jsonl")
    orchestrator.register_pool("pool-0-degraded", "10.0.0.10", 8080, max_capacity=8)
    orchestrator.register_pool("pool-1-healthy", "10.0.0.11", 8081, max_capacity=8)
    await warm_pool(orchestrator, "pool-0-degraded", MockBackend("jitter", 6.0), repeats=28)
    await warm_pool(orchestrator, "pool-1-healthy", MockBackend("clean", 2.0), repeats=20)
    return orchestrator, {
        "scenario": "E",
        "name": "Gray failure but not hard failure",
        "key_condition": "Degraded path remains up and transfers succeed while latency/jitter lift PathState to DEGRADED_USABLE.",
        "key_policy_outcome": "Batch remains admissible on the degraded path while interactive and release-sensitive workloads move to the healthy path.",
    }


async def build_scenario_f() -> Tuple[SeamOrchestrator, Dict[str, Any]]:
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
    return orchestrator, {
        "scenario": "F",
        "name": "Capacity pressure under gray failure",
        "key_condition": "The healthiest path is near soft capacity while a degraded path still has headroom and remains admissible for tolerant work.",
        "key_policy_outcome": "Policy preserves the healthier path for stricter workloads and uses the degraded path when headroom matters more than raw health.",
    }


def candidate_rows(record: Dict[str, Any]) -> List[List[str]]:
    rows: List[List[str]] = []
    for candidate in record["candidate_explanations"]:
        capacity = candidate["capacity_snapshot"]
        rows.append(
            [
                candidate["pool_id"],
                candidate["path_state"],
                f'{candidate["gfs"]:.3f}',
                f'{candidate["prs"]:.3f}',
                f'{candidate["fae"]:.2f}',
                f'{capacity["active_sessions"]}/{capacity["max_capacity"]}',
                "yes" if candidate["admissible"] else "no",
                "yes" if candidate["chosen"] else "no",
                candidate["skipped_reason"] or candidate["primary_reason"],
            ]
        )
    return rows
def representative_trace(record: Dict[str, Any]) -> Dict[str, Any]:
    chosen = next(
        (candidate for candidate in record["candidate_explanations"] if candidate["chosen"]),
        None,
    )
    return {
        "workload": record["workload_name"],
        "outcome": record["outcome"],
        "reason": record["reason"],
        "chosen_candidate": chosen,
        "candidate_explanations": record["candidate_explanations"],
    }


async def generate_phase2_outputs() -> Dict[str, Any]:
    OUTPUTS_DIR.mkdir(exist_ok=True)
    for stale in OUTPUTS_DIR.glob("*.jsonl"):
        stale.unlink(missing_ok=True)

    scenario_summary = [
        {
            "scenario": "A",
            "name": "Clean baseline",
            "key_condition": "Both decode paths remain healthy.",
            "key_policy_outcome": "Policy is largely invisible when every path is healthy and uncongested.",
            "notable_result": "Healthy path selection dominates with minimal explanation burden.",
        },
        {
            "scenario": "B",
            "name": "Latency degradation",
            "key_condition": "Latency rises before hard failure.",
            "key_policy_outcome": "High-criticality workloads stop using the degraded path before transport fails outright.",
            "notable_result": "Admissibility shifts before binary reachability changes.",
        },
        {
            "scenario": "C",
            "name": "Jitter storm",
            "key_condition": "Latency variance rises while paths remain live.",
            "key_policy_outcome": "Jitter-sensitive workloads are protected without treating the path as fully down.",
            "notable_result": "PathState and admissibility diverge from binary transport health.",
        },
        {
            "scenario": "D",
            "name": "Drop storm to quarantine",
            "key_condition": "Persistent bad windows push a path toward quarantine.",
            "key_policy_outcome": "Escalation is fast, recovery is slower, and hysteresis avoids flapping.",
            "notable_result": "State transitions expose why a path becomes temporarily inadmissible.",
        },
        {
            "scenario": "E",
            "name": "Gray failure but not hard failure",
            "key_condition": "A degraded path stays live and transfers succeed.",
            "key_policy_outcome": "Admissibility splits by workload sensitivity.",
            "notable_result": "Batch stays admissible on DEGRADED_USABLE while strict workloads move healthy.",
        },
        {
            "scenario": "F",
            "name": "Capacity pressure under gray failure",
            "key_condition": "The healthiest path is near soft capacity while a degraded path keeps headroom.",
            "key_policy_outcome": "Policy preserves healthy headroom for stricter workloads and spends degraded headroom on tolerant work.",
            "notable_result": "The healthiest path is not always the selected path.",
        },
    ]

    orchestrator_e, meta_e = await build_scenario_e()
    orchestrator_f, meta_f = await build_scenario_f()

    representative_records: List[Dict[str, Any]] = []
    for workload in (WORKLOAD_BATCH, WORKLOAD_INTERACTIVE, WORKLOAD_RELEASE):
        _, decision = orchestrator_e.route_session(workload)
        representative_records.append(decision.to_dict())
        if decision.chosen_pool_id:
            orchestrator_e.release_session(decision.chosen_pool_id)

    scenario_e_batch = representative_records[0]
    scenario_e_interactive = representative_records[1]
    scenario_e_release = representative_records[2]

    f_workloads = [
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
    scenario_f_records: List[Dict[str, Any]] = []
    for workload in f_workloads:
        _, decision = orchestrator_f.route_session(workload)
        scenario_f_records.append(decision.to_dict())
        if decision.chosen_pool_id:
            orchestrator_f.release_session(decision.chosen_pool_id)

    scenario_f_batch = scenario_f_records[0]
    scenario_f_interactive = scenario_f_records[1]
    scenario_f_release = scenario_f_records[2]

    write_json(OUTPUTS_DIR / "decision_trace_scenario_e.json", representative_trace(scenario_e_interactive))
    write_json(OUTPUTS_DIR / "decision_trace_scenario_f.json", representative_trace(scenario_f_batch))

    write_text(
        OUTPUTS_DIR / "scenario_e_table.md",
        markdown_table(
            ["Candidate path", "PathState", "GFS", "PRS", "FAE", "Capacity", "Admissible", "Chosen", "Reason"],
            candidate_rows(scenario_e_batch),
        ),
    )
    write_text(
        OUTPUTS_DIR / "scenario_f_table.md",
        markdown_table(
            ["Candidate path", "PathState", "GFS", "PRS", "FAE", "Capacity", "Admissible", "Chosen", "Reason"],
            candidate_rows(scenario_f_batch),
        ),
    )

    summary_rows = [
        [item["scenario"], item["name"], item["key_condition"], item["key_policy_outcome"], item["notable_result"]]
        for item in scenario_summary
    ]
    write_json(OUTPUTS_DIR / "scenario_summary.json", {"scenarios": scenario_summary})
    write_text(
        OUTPUTS_DIR / "scenario_summary.md",
        "# Scenario Summary\n\n"
        + markdown_table(
            ["Scenario", "Name", "Key condition", "Key policy outcome", "Notable result"],
            summary_rows,
        ),
    )

    aggregate_trials = 24
    outcome_counter = Counter()
    state_transition_counter = Counter()
    strict_preserved_on_healthy = 0
    strict_total = 0
    tolerant_on_degraded = 0
    tolerant_total = 0
    capacity_tradeoff_batch_reroutes = 0
    degraded_path_selections = 0

    for seed in range(aggregate_trials):
        random.seed(seed)
        scenario_e_trial, _ = await build_scenario_e()
        for workload in (WORKLOAD_BATCH, WORKLOAD_INTERACTIVE, WORKLOAD_RELEASE):
            _, decision = scenario_e_trial.route_session(workload)
            outcome_counter[decision.outcome.value] += 1
            chosen = chosen_candidate_from_decision(decision)
            if chosen and chosen.path_state != PathState.HEALTHY:
                degraded_path_selections += 1
            if workload.effective_criticality() >= 0.7:
                strict_total += 1
                if chosen and chosen.path_state == PathState.HEALTHY:
                    strict_preserved_on_healthy += 1
            if workload.effective_criticality() <= 0.35:
                tolerant_total += 1
                if chosen and chosen.path_state == PathState.DEGRADED_USABLE:
                    tolerant_on_degraded += 1
            if decision.chosen_pool_id:
                scenario_e_trial.release_session(decision.chosen_pool_id)

        scenario_f_trial, _ = await build_scenario_f()
        for workload in (
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
        ):
            _, decision = scenario_f_trial.route_session(workload)
            outcome_counter[decision.outcome.value] += 1
            chosen = chosen_candidate_from_decision(decision)
            if chosen and chosen.path_state != PathState.HEALTHY:
                degraded_path_selections += 1
            if workload.effective_criticality() >= 0.7:
                strict_total += 1
                if chosen and chosen.path_state == PathState.HEALTHY:
                    strict_preserved_on_healthy += 1
            if workload.effective_criticality() <= 0.35:
                tolerant_total += 1
                if chosen and chosen.path_state == PathState.DEGRADED_USABLE:
                    tolerant_on_degraded += 1
                if decision.outcome == DecisionOutcome.REROUTED:
                    capacity_tradeoff_batch_reroutes += 1
            if decision.chosen_pool_id:
                scenario_f_trial.release_session(decision.chosen_pool_id)

        for event_path in (
            OUTPUTS_DIR / "scenario_e_events.jsonl",
            OUTPUTS_DIR / "scenario_f_events.jsonl",
        ):
            if event_path.exists():
                for line in event_path.read_text(encoding="utf-8").splitlines():
                    event = json.loads(line)
                    if event["event_type"] == "state_transition":
                        key = f'{event["payload"]["from_state"]}->{event["payload"]["to_state"]}'
                        state_transition_counter[key] += 1
                event_path.unlink(missing_ok=True)

    evaluation_summary = {
        "trials": aggregate_trials,
        "admissions": outcome_counter[DecisionOutcome.ADMITTED.value],
        "reroutes": outcome_counter[DecisionOutcome.REROUTED.value],
        "rejections": outcome_counter[DecisionOutcome.NO_POOL_AVAILABLE.value],
        "admitted_despite_degradation": degraded_path_selections,
        "state_transition_counts": dict(state_transition_counter),
        "strict_workloads_preserved_on_healthy_pct": round(
            100.0 * strict_preserved_on_healthy / max(strict_total, 1), 1
        ),
        "tolerant_workloads_admitted_to_degraded_pct": round(
            100.0 * tolerant_on_degraded / max(tolerant_total, 1), 1
        ),
        "capacity_pressure_batch_reroutes": capacity_tradeoff_batch_reroutes,
        "restores_observed": state_transition_counter.get("quarantine_candidate->restored", 0)
        + state_transition_counter.get("quarantined->restored", 0),
    }

    write_json(OUTPUTS_DIR / "evaluation_summary.json", evaluation_summary)
    write_text(
        OUTPUTS_DIR / "evaluation_summary.md",
        (
            "# Evaluation Summary\n\n"
            + markdown_table(
                ["Metric", "Value"],
                [
                    ["trials", evaluation_summary["trials"]],
                    ["admissions", evaluation_summary["admissions"]],
                    ["reroutes", evaluation_summary["reroutes"]],
                    ["rejections", evaluation_summary["rejections"]],
                    ["admitted_despite_degradation", evaluation_summary["admitted_despite_degradation"]],
                    ["strict_workloads_preserved_on_healthy_pct", evaluation_summary["strict_workloads_preserved_on_healthy_pct"]],
                    ["tolerant_workloads_admitted_to_degraded_pct", evaluation_summary["tolerant_workloads_admitted_to_degraded_pct"]],
                    ["capacity_pressure_batch_reroutes", evaluation_summary["capacity_pressure_batch_reroutes"]],
                    ["restores_observed", evaluation_summary["restores_observed"]],
                ],
            )
            + "\n\n## State Transition Counts\n\n"
            + markdown_table(
                ["Transition", "Count"],
                [[transition, count] for transition, count in evaluation_summary["state_transition_counts"].items()],
            )
        ),
    )

    return {
        "scenario_summary": scenario_summary,
        "evaluation_summary": evaluation_summary,
        "scenario_e_trace": representative_trace(scenario_e_interactive),
        "scenario_f_trace": representative_trace(scenario_f_batch),
        "scenario_e_batch": scenario_e_batch,
        "scenario_f_batch": scenario_f_batch,
        "scenario_e_meta": meta_e,
        "scenario_f_meta": meta_f,
    }


def main() -> None:
    random.seed(7)
    logging.basicConfig(level=logging.CRITICAL)
    artifacts = asyncio.run(generate_phase2_outputs())
    print(json.dumps(to_jsonable(artifacts["evaluation_summary"]), indent=2))


if __name__ == "__main__":
    main()
