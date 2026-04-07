"""
Replay tool for comparing Seam Orchestrator decisions with naive routing.
"""

from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Sequence

from orchestrator import (
    CandidateExplanation,
    PathDependence,
    PathState,
    WorkloadProfile,
    compute_fae,
    compute_prs,
    is_admissible,
    selection_policy_for,
    selection_vector_for,
    to_jsonable,
    DecodePool,
    ThresholdConfig,
    CapacitySnapshot,
    HealthSnapshot,
)

OUTPUTS_DIR = Path("outputs")
DATA_DIR = Path("data")
TRACE_PATH = DATA_DIR / "sample_trace.csv"


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    header_line = "| " + " | ".join(str(cell) for cell in headers) + " |"
    separator = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join([header_line, separator, *body])


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(payload), indent=2), encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def state_from_metrics(latency_ms: float, jitter_ms: float, drop_rate: float, cfg: ThresholdConfig) -> PathState:
    if drop_rate >= cfg.drop_quarantine or latency_ms >= cfg.latency_quarantine_ms:
        return PathState.QUARANTINE_CANDIDATE
    if latency_ms >= cfg.latency_restricted_ms or jitter_ms >= cfg.jitter_restricted_ms or drop_rate >= cfg.drop_restricted:
        return PathState.DEGRADED_RESTRICTED
    if latency_ms >= cfg.latency_degraded_ms or jitter_ms >= cfg.jitter_degraded_ms or drop_rate >= cfg.drop_degraded:
        return PathState.DEGRADED_USABLE
    return PathState.HEALTHY


def compute_gfs_from_row(latency_ms: float, jitter_ms: float, drop_rate: float, cfg: ThresholdConfig) -> float:
    n_lat = min(latency_ms / cfg.latency_quarantine_ms, 1.0)
    n_jit = min(jitter_ms / (cfg.jitter_restricted_ms * 2), 1.0)
    n_drop = min(drop_rate / cfg.drop_quarantine, 1.0)
    gfs_base = cfg.w_latency * n_lat + cfg.w_jitter * n_jit + cfg.w_drop * n_drop
    interaction = 0.15 * (n_lat * n_drop)
    return min(gfs_base + interaction, 1.0)


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def workload_from_row(row: Dict[str, str]) -> WorkloadProfile:
    return WorkloadProfile(
        name=row["workload_type"],
        criticality=float(row["criticality"]),
        latency_sla_ms=float(row["sla_ms"]),
        jitter_tolerance=float(row["jitter_tolerance"]),
        is_release_path=parse_bool(row["is_release_path"]),
        is_prefill_decode_strict=parse_bool(row["is_prefill_decode_strict"]),
    )


def load_trace(path: Path) -> Dict[str, List[Dict[str, str]]]:
    groups: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            groups[row["request_id"]].append(row)
    return groups


def candidate_from_row(
    row: Dict[str, str],
    group_rows: Sequence[Dict[str, str]],
    workload: WorkloadProfile,
    cfg: ThresholdConfig,
) -> CandidateExplanation:
    pool_id = row["pool_id"]
    max_capacity = int(row["max_capacity"])
    active_sessions = int(row["occupancy"])
    soft_fraction = float(row["soft_capacity_fraction"])
    pool = DecodePool(
        pool_id=pool_id,
        host="trace",
        port=0,
        max_capacity=max_capacity,
        soft_capacity_fraction=soft_fraction,
    )
    pool.active_sessions = active_sessions
    pool.gfs = round(
        compute_gfs_from_row(
            float(row["latency_ms"]),
            float(row["jitter_ms"]),
            float(row["drop_rate"]),
            cfg,
        ),
        4,
    )
    pool.state = state_from_metrics(
        float(row["latency_ms"]),
        float(row["jitter_ms"]),
        float(row["drop_rate"]),
        cfg,
    )

    alternate_pool_ids = [item["pool_id"] for item in group_rows if item["pool_id"] != pool_id]
    available_alt_ids = [
        item["pool_id"]
        for item in group_rows
        if item["pool_id"] != pool_id and int(item["occupancy"]) < int(item["max_capacity"])
    ]
    dependence = PathDependence(
        pool_id=pool_id,
        alternate_pool_ids=alternate_pool_ids,
        available_alternate_pool_ids=available_alt_ids,
    )
    prs = round(compute_prs(pool, cfg, dependence, workload), 4)
    fae = round(compute_fae(pool, cfg, dependence, workload), 4)
    capacity = pool.capacity_snapshot()
    health = HealthSnapshot(
        p99_latency_ms=float(row["latency_ms"]),
        jitter_ms=float(row["jitter_ms"]),
        mean_drop_rate=float(row["drop_rate"]),
        sample_count=1,
    )
    admissible, _, reason = is_admissible(pool, workload, prs, health, capacity)
    candidate = CandidateExplanation(
        pool_id=pool_id,
        path_state=pool.state,
        gfs=pool.gfs,
        prs=prs,
        fae=fae,
        admissible=admissible,
        primary_reason=reason,
        capacity_snapshot=capacity,
        topology=dependence,
        health_snapshot=health,
        selection_policy=selection_policy_for(workload) if admissible else None,
    )
    if admissible:
        candidate.selection_vector = selection_vector_for(candidate, workload)
    return candidate


def pick_naive(policy: str, candidates: Sequence[CandidateExplanation]) -> CandidateExplanation | None:
    if policy == "lowest_latency":
        eligible = [candidate for candidate in candidates if candidate.admissible]
        return min(eligible, key=lambda item: item.health_snapshot.p99_latency_ms) if eligible else None

    if policy == "binary_health_only":
        healthy = [
            candidate
            for candidate in candidates
            if candidate.admissible and candidate.path_state == PathState.HEALTHY
        ]
        if healthy:
            return min(healthy, key=lambda item: item.health_snapshot.p99_latency_ms)
        eligible = [candidate for candidate in candidates if candidate.admissible]
        return min(eligible, key=lambda item: item.health_snapshot.p99_latency_ms) if eligible else None

    if policy == "capacity_only":
        eligible = [candidate for candidate in candidates if candidate.admissible]
        return max(
            eligible,
            key=lambda item: (item.capacity_snapshot.remaining, -item.health_snapshot.p99_latency_ms),
        ) if eligible else None

    if policy == "round_robin":
        eligible = [candidate for candidate in candidates if candidate.admissible]
        return sorted(eligible, key=lambda item: item.pool_id)[0] if eligible else None

    raise ValueError(f"unknown policy {policy}")


def pick_seam(candidates: Sequence[CandidateExplanation], workload: WorkloadProfile) -> CandidateExplanation | None:
    eligible = [candidate for candidate in candidates if candidate.admissible]
    if not eligible:
        return None
    policy = selection_policy_for(workload)
    for candidate in eligible:
        candidate.selection_policy = policy
        candidate.selection_vector = selection_vector_for(candidate, workload)
    eligible.sort(key=lambda candidate: tuple(candidate.selection_vector))
    return eligible[0]


def evaluate_request(
    request_id: str,
    rows: Sequence[Dict[str, str]],
    cfg: ThresholdConfig,
) -> Dict[str, Any]:
    workload = workload_from_row(rows[0])
    candidates = [candidate_from_row(row, rows, workload, cfg) for row in rows]

    policies = {
        "lowest_latency": pick_naive("lowest_latency", candidates),
        "binary_health_only": pick_naive("binary_health_only", candidates),
        "capacity_only": pick_naive("capacity_only", candidates),
        "seam_orchestrator": pick_seam(candidates, workload),
    }

    seam_choice = policies["seam_orchestrator"]
    strict_protected = bool(
        seam_choice
        and workload.effective_criticality() >= 0.7
        and seam_choice.path_state == PathState.HEALTHY
    )
    degraded_used = bool(seam_choice and seam_choice.path_state != PathState.HEALTHY)
    healthy_soft_saturated = any(
        candidate.path_state == PathState.HEALTHY and candidate.capacity_snapshot.soft_saturated
        for candidate in candidates
    )
    headroom_preserved = bool(
        seam_choice
        and seam_choice.path_state != PathState.HEALTHY
        and healthy_soft_saturated
        and workload.effective_criticality() <= 0.35
    )

    rejected = [
        {
            "pool_id": candidate.pool_id,
            "reason": candidate.primary_reason,
            "path_state": candidate.path_state.value,
        }
        for candidate in candidates
        if not candidate.admissible
    ]
    candidate_rows = [
        {
            "pool_id": candidate.pool_id,
            "path_state": candidate.path_state.value,
            "admissible": candidate.admissible,
            "reason": candidate.primary_reason,
            "latency_ms": candidate.health_snapshot.p99_latency_ms,
            "jitter_ms": candidate.health_snapshot.jitter_ms,
            "remaining_capacity": candidate.capacity_snapshot.remaining,
            "soft_saturated": candidate.capacity_snapshot.soft_saturated,
        }
        for candidate in candidates
    ]

    return {
        "request_id": request_id,
        "timestamp": rows[0]["timestamp"],
        "workload_type": workload.name,
        "strict_workload_protected": strict_protected,
        "degraded_path_used": degraded_used,
        "healthy_headroom_preserved": headroom_preserved,
        "candidates": candidate_rows,
        "rejected_candidates": rejected,
        "policies": {
            name: {
                "chosen_pool_id": choice.pool_id if choice else None,
                "path_state": choice.path_state.value if choice else None,
                "reason": choice.primary_reason if choice else "no admissible pool",
                "gfs": choice.gfs if choice else None,
                "prs": choice.prs if choice else None,
                "fae": choice.fae if choice else None,
            }
            for name, choice in policies.items()
        },
    }


def summarize(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"policy_metrics": {}}
    for policy in ("lowest_latency", "binary_health_only", "capacity_only", "seam_orchestrator"):
        chosen = [result["policies"][policy] for result in results]
        strict_requests = [
            result
            for result in results
            if result["workload_type"] in {"interactive", "release", "strict-online"}
        ]
        tolerant_requests = [result for result in results if result["workload_type"] == "batch"]
        strict_choice_count = sum(
            1
            for result in strict_requests
            if result["policies"][policy]["chosen_pool_id"] is not None
        )
        strict_total = sum(
            1 for result in strict_requests
        )
        strict_healthy = sum(
            1
            for result in strict_requests
            if result["policies"][policy]["path_state"] == "healthy"
        )
        strict_protected = sum(
            1
            for result in strict_requests
            if result["policies"][policy]["path_state"] == "healthy"
            and any(
                candidate["pool_id"] != result["policies"][policy]["chosen_pool_id"]
                and candidate["admissible"]
                and candidate["path_state"] in {"degraded_usable", "degraded_restricted"}
                for candidate in result["candidates"]
            )
        )
        tolerant_total = sum(1 for result in tolerant_requests)
        tolerant_degraded = sum(
            1
            for result in tolerant_requests
            if result["policies"][policy]["path_state"] in {"degraded_usable", "degraded_restricted"}
        )
        headroom_preserved = sum(
            1
            for result in tolerant_requests
            if result["policies"][policy]["path_state"] in {"degraded_usable", "degraded_restricted"}
            and any(
                result["policies"][policy]["chosen_pool_id"] != candidate["pool_id"]
                and candidate["path_state"] == "healthy"
                and candidate["admissible"]
                and candidate["soft_saturated"]
                for candidate in result["candidates"]
            )
        )
        summary["policy_metrics"][policy] = {
            "requests": len(chosen),
            "strict_choice_count": strict_choice_count,
            "strict_healthy_pct": round(100.0 * strict_healthy / max(strict_total, 1), 1),
            "tolerant_degraded_pct": round(100.0 * tolerant_degraded / max(tolerant_total, 1), 1),
            "headroom_preserved_count": headroom_preserved,
            "strict_protected_count": strict_protected,
            "no_pool_count": sum(1 for choice in chosen if choice["chosen_pool_id"] is None),
        }
    summary["replay_requests"] = len(results)
    summary["strict_workloads_protected_by_seam"] = sum(
        1 for result in results if result["strict_workload_protected"]
    )
    summary["headroom_preserved_by_seam"] = sum(
        1 for result in results if result["healthy_headroom_preserved"]
    )
    return summary


def generate_outputs(results: Sequence[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    OUTPUTS_DIR.mkdir(exist_ok=True)
    write_json(OUTPUTS_DIR / "replay_summary.json", {"summary": summary, "results": results})

    table_rows = []
    for result in results:
        table_rows.append(
            [
                result["request_id"],
                result["workload_type"],
                result["policies"]["lowest_latency"]["chosen_pool_id"] or "-",
                result["policies"]["binary_health_only"]["chosen_pool_id"] or "-",
                result["policies"]["capacity_only"]["chosen_pool_id"] or "-",
                result["policies"]["seam_orchestrator"]["chosen_pool_id"] or "-",
                "yes" if result["strict_workload_protected"] else "no",
                "yes" if result["healthy_headroom_preserved"] else "no",
            ]
        )

    summary_rows = [
        [
            policy,
            metrics["requests"],
            metrics["strict_choice_count"],
            metrics["strict_healthy_pct"],
            metrics["tolerant_degraded_pct"],
            metrics["strict_protected_count"],
            metrics["headroom_preserved_count"],
            metrics["no_pool_count"],
        ]
        for policy, metrics in summary["policy_metrics"].items()
    ]

    write_text(
        OUTPUTS_DIR / "replay_comparison_table.md",
        "# Replay Comparison Table\n\n"
        + markdown_table(
            [
                "Request",
                "Workload",
                "Lowest latency",
                "Binary health only",
                "Capacity only",
                "Seam Orchestrator",
                "Strict protected",
                "Headroom preserved",
            ],
            table_rows,
        ),
    )
    write_text(
        OUTPUTS_DIR / "replay_summary.md",
        "# Replay Summary\n\n"
        + "Replay compares the same request stream across naive policies and Seam Orchestrator. The goal is not transport benchmarking; it is auditability and side-by-side policy contrast.\n\n"
        + "## Policy Metrics\n\n"
        + markdown_table(
            [
                "Policy",
                "Requests",
                "Strict choices",
                "Strict healthy %",
                "Tolerant degraded %",
                "Strict protected",
                "Headroom preserved",
                "No-pool count",
            ],
            summary_rows,
        )
        + "\n\nReplay takeaway: Seam Orchestrator exposes an auditable decision record for the same request stream, making it clear when strict workloads were kept on healthier paths, when lower-latency but jitterier paths were avoided for tail protection, and when degraded-but-usable capacity was deliberately spent on tolerant work.\n"
        + "\n\n## Request-Level Comparison\n\n"
        + markdown_table(
            [
                "Request",
                "Workload",
                "Lowest latency",
                "Binary health only",
                "Capacity only",
                "Seam Orchestrator",
                "Strict protected",
                "Headroom preserved",
            ],
            table_rows,
        ),
    )


def main() -> None:
    logging.getLogger("seam").setLevel(logging.CRITICAL)
    cfg = ThresholdConfig()
    groups = load_trace(TRACE_PATH)
    results = [evaluate_request(request_id, rows, cfg) for request_id, rows in groups.items()]
    summary = summarize(results)
    generate_outputs(results, summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
