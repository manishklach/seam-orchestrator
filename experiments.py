"""
Controlled experiment harness for Seam Orchestrator.
"""

from __future__ import annotations

import csv
import json
import logging
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Optional, Sequence, Tuple

from orchestrator import (
    CandidateExplanation,
    PathState,
    SeamOrchestrator,
    ThresholdConfig,
    WORKLOAD_BATCH,
    WORKLOAD_INTERACTIVE,
    WORKLOAD_RELEASE,
    WorkloadProfile,
    to_jsonable,
)

OUTPUTS_DIR = Path("outputs")
SCRATCH_EVENT_LOG = OUTPUTS_DIR / "_experiment_events.jsonl"
BLOCK_SIZE = 512 * 1024
SVG_WIDTH = 860
SVG_HEIGHT = 360
SERIES_COLORS = ["#2563EB", "#F97316", "#16A34A", "#9333EA", "#DC2626"]


@dataclass
class PoolTraceConfig:
    pool_id: str
    mean_latency_ms: float
    jitter_ms: float
    drop_probability: float = 0.0
    active_sessions: int = 0
    max_capacity: int = 8
    soft_capacity_fraction: float = 0.75
    sample_count: int = 24


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


def write_csv(path: Path, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def svg_line_chart(
    title: str,
    x_label: str,
    y_label: str,
    x_values: Sequence[float],
    series: Sequence[Tuple[str, Sequence[float]]],
    *,
    y_min: float = 0.0,
    y_max: float = 100.0,
) -> str:
    plot_left = 70
    plot_top = 50
    plot_width = SVG_WIDTH - 160
    plot_height = SVG_HEIGHT - 120
    plot_bottom = plot_top + plot_height
    plot_right = plot_left + plot_width
    x_min = min(x_values)
    x_max = max(x_values)
    x_span = max(x_max - x_min, 1.0)
    y_span = max(y_max - y_min, 1.0)

    def map_x(value: float) -> float:
        return plot_left + ((value - x_min) / x_span) * plot_width

    def map_y(value: float) -> float:
        return plot_bottom - ((value - y_min) / y_span) * plot_height

    y_ticks = [y_min + (y_span * step / 4) for step in range(5)]
    x_ticks = list(x_values)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_WIDTH}" height="{SVG_HEIGHT}" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" role="img" aria-label="{title}">',
        '<rect width="100%" height="100%" fill="#0B1220"/>',
        f'<text x="{plot_left}" y="28" fill="#E5EEF9" font-family="Arial, Helvetica, sans-serif" font-size="20" font-weight="700">{title}</text>',
        f'<text x="{plot_left}" y="{SVG_HEIGHT - 16}" fill="#9FB3C8" font-family="Arial, Helvetica, sans-serif" font-size="12">{x_label}</text>',
        f'<text x="16" y="{plot_top - 12}" fill="#9FB3C8" font-family="Arial, Helvetica, sans-serif" font-size="12">{y_label}</text>',
        f'<line x1="{plot_left}" y1="{plot_bottom}" x2="{plot_right}" y2="{plot_bottom}" stroke="#5A6B82" stroke-width="1"/>',
        f'<line x1="{plot_left}" y1="{plot_top}" x2="{plot_left}" y2="{plot_bottom}" stroke="#5A6B82" stroke-width="1"/>',
    ]

    for tick in y_ticks:
        y = map_y(tick)
        parts.append(
            f'<line x1="{plot_left}" y1="{y:.1f}" x2="{plot_right}" y2="{y:.1f}" stroke="#1F3148" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{plot_left - 12}" y="{y + 4:.1f}" text-anchor="end" fill="#9FB3C8" font-family="Arial, Helvetica, sans-serif" font-size="11">{tick:.0f}</text>'
        )

    for tick in x_ticks:
        x = map_x(tick)
        parts.append(
            f'<line x1="{x:.1f}" y1="{plot_top}" x2="{x:.1f}" y2="{plot_bottom}" stroke="#132238" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{plot_bottom + 18}" text-anchor="middle" fill="#9FB3C8" font-family="Arial, Helvetica, sans-serif" font-size="11">{tick:g}</text>'
        )

    legend_x = plot_right + 20
    legend_y = plot_top + 14
    for idx, (label, values) in enumerate(series):
        color = SERIES_COLORS[idx % len(SERIES_COLORS)]
        points = " ".join(f"{map_x(x):.1f},{map_y(y):.1f}" for x, y in zip(x_values, values))
        parts.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{points}"/>'
        )
        for x, y in zip(x_values, values):
            parts.append(
                f'<circle cx="{map_x(x):.1f}" cy="{map_y(y):.1f}" r="3.5" fill="{color}"/>'
            )
        parts.append(
            f'<rect x="{legend_x}" y="{legend_y + idx * 22 - 10}" width="12" height="12" fill="{color}" rx="2"/>'
        )
        parts.append(
            f'<text x="{legend_x + 18}" y="{legend_y + idx * 22}" fill="#D5E2F2" font-family="Arial, Helvetica, sans-serif" font-size="12">{label}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def svg_grouped_bar_chart(
    title: str,
    categories: Sequence[str],
    series: Sequence[Tuple[str, Sequence[float]]],
    *,
    y_max: float = 100.0,
) -> str:
    plot_left = 70
    plot_top = 50
    plot_width = SVG_WIDTH - 120
    plot_height = SVG_HEIGHT - 120
    plot_bottom = plot_top + plot_height
    plot_right = plot_left + plot_width
    y_ticks = [0, y_max * 0.25, y_max * 0.50, y_max * 0.75, y_max]
    group_width = plot_width / max(len(categories), 1)
    bar_width = max(group_width / max(len(series) + 1, 2), 12)

    def map_y(value: float) -> float:
        return plot_bottom - (value / max(y_max, 1.0)) * plot_height

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SVG_WIDTH}" height="{SVG_HEIGHT}" viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" role="img" aria-label="{title}">',
        '<rect width="100%" height="100%" fill="#0B1220"/>',
        f'<text x="{plot_left}" y="28" fill="#E5EEF9" font-family="Arial, Helvetica, sans-serif" font-size="20" font-weight="700">{title}</text>',
        f'<line x1="{plot_left}" y1="{plot_bottom}" x2="{plot_right}" y2="{plot_bottom}" stroke="#5A6B82" stroke-width="1"/>',
        f'<line x1="{plot_left}" y1="{plot_top}" x2="{plot_left}" y2="{plot_bottom}" stroke="#5A6B82" stroke-width="1"/>',
    ]

    for tick in y_ticks:
        y = map_y(tick)
        parts.append(
            f'<line x1="{plot_left}" y1="{y:.1f}" x2="{plot_right}" y2="{y:.1f}" stroke="#1F3148" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{plot_left - 12}" y="{y + 4:.1f}" text-anchor="end" fill="#9FB3C8" font-family="Arial, Helvetica, sans-serif" font-size="11">{tick:.0f}</text>'
        )

    for group_idx, category in enumerate(categories):
        center = plot_left + group_width * group_idx + group_width / 2
        parts.append(
            f'<text x="{center:.1f}" y="{plot_bottom + 18}" text-anchor="middle" fill="#9FB3C8" font-family="Arial, Helvetica, sans-serif" font-size="11">{category}</text>'
        )
        series_total_width = len(series) * bar_width
        group_start = center - series_total_width / 2
        for series_idx, (_, values) in enumerate(series):
            color = SERIES_COLORS[series_idx % len(SERIES_COLORS)]
            value = values[group_idx]
            x = group_start + series_idx * bar_width
            y = map_y(value)
            height = max(plot_bottom - y, 0.0)
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width - 4:.1f}" height="{height:.1f}" fill="{color}" rx="3"/>'
            )

    legend_x = plot_right - 140
    legend_y = plot_top + 14
    for idx, (label, _) in enumerate(series):
        color = SERIES_COLORS[idx % len(SERIES_COLORS)]
        parts.append(
            f'<rect x="{legend_x}" y="{legend_y + idx * 22 - 10}" width="12" height="12" fill="{color}" rx="2"/>'
        )
        parts.append(
            f'<text x="{legend_x + 18}" y="{legend_y + idx * 22}" fill="#D5E2F2" font-family="Arial, Helvetica, sans-serif" font-size="12">{label}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def dominant_state(states: Sequence[str]) -> str:
    if not states:
        return "-"
    return Counter(states).most_common(1)[0][0]


def candidate_for_pool(decision_record, pool_id: str) -> Optional[CandidateExplanation]:
    for candidate in decision_record.candidate_explanations:
        if candidate.pool_id == pool_id:
            return candidate
    return None


def chosen_candidate(decision_record) -> Optional[CandidateExplanation]:
    for candidate in decision_record.candidate_explanations:
        if candidate.chosen:
            return candidate
    return None


def populate_pool(
    orchestrator: SeamOrchestrator,
    cfg: PoolTraceConfig,
    rng: random.Random,
) -> None:
    for _ in range(cfg.sample_count):
        latency_ms = max(0.1, rng.gauss(cfg.mean_latency_ms, max(cfg.jitter_ms, 0.05)))
        drop_rate = 1.0 if rng.random() < cfg.drop_probability else 0.0
        bytes_moved = 0 if drop_rate else BLOCK_SIZE
        orchestrator.record_transfer(cfg.pool_id, latency_ms, drop_rate, bytes_moved)
    orchestrator.set_active_sessions(cfg.pool_id, cfg.active_sessions)


def build_orchestrator(
    traces: Sequence[PoolTraceConfig],
    *,
    cfg: Optional[ThresholdConfig] = None,
    seed: int = 0,
) -> SeamOrchestrator:
    SCRATCH_EVENT_LOG.unlink(missing_ok=True)
    orchestrator = SeamOrchestrator(cfg=cfg, event_log_path=SCRATCH_EVENT_LOG)
    rng = random.Random(seed)
    for idx, trace in enumerate(traces):
        orchestrator.register_pool(
            trace.pool_id,
            f"10.0.0.{10 + idx}",
            8080 + idx,
            max_capacity=trace.max_capacity,
            soft_capacity_fraction=trace.soft_capacity_fraction,
        )
    for trace in traces:
        populate_pool(orchestrator, trace, rng)
    return orchestrator


def baseline_choice(
    name: str,
    candidates: Sequence[CandidateExplanation],
    workload: WorkloadProfile,
) -> Optional[CandidateExplanation]:
    del workload
    eligible = [
        candidate
        for candidate in candidates
        if candidate.path_state
        not in (PathState.QUARANTINED, PathState.QUARANTINE_CANDIDATE)
        and not candidate.capacity_snapshot.hard_saturated
    ]
    if not eligible:
        return None

    if name == "lowest_latency":
        return min(
            eligible,
            key=lambda candidate: (
                candidate.health_snapshot.p99_latency_ms,
                candidate.gfs,
                candidate.capacity_snapshot.utilization,
            ),
        )

    if name == "binary_health_only":
        healthy = [candidate for candidate in eligible if candidate.path_state == PathState.HEALTHY]
        if healthy:
            return min(
                healthy,
                key=lambda candidate: candidate.health_snapshot.p99_latency_ms,
            )
        return min(
            eligible,
            key=lambda candidate: (
                candidate.health_snapshot.p99_latency_ms,
                candidate.capacity_snapshot.utilization,
            ),
        )

    if name == "capacity_only":
        return min(
            eligible,
            key=lambda candidate: (
                -candidate.capacity_snapshot.remaining,
                candidate.health_snapshot.p99_latency_ms,
            ),
        )

    raise ValueError(f"unknown baseline {name}")


def record_baseline_metrics(
    bucket: Dict[str, Any],
    workload: WorkloadProfile,
    candidate: Optional[CandidateExplanation],
    *,
    headroom_preservation_opportunity: bool,
) -> None:
    bucket["trials"] += 1
    if candidate is None:
        bucket["rejections"] += 1
        return

    bucket["admissions"] += 1
    if candidate.path_state != PathState.HEALTHY:
        bucket["admitted_despite_degradation"] += 1
    bucket["avg_prs_values"].append(candidate.prs)
    bucket["avg_fae_values"].append(candidate.fae)

    criticality = workload.effective_criticality()
    if criticality >= 0.7:
        bucket["strict_total"] += 1
        if candidate.path_state == PathState.HEALTHY:
            bucket["strict_healthy_success"] += 1
    if criticality <= 0.35:
        bucket["tolerant_total"] += 1
        if candidate.path_state == PathState.DEGRADED_USABLE:
            bucket["tolerant_degraded_success"] += 1
        if headroom_preservation_opportunity and candidate.path_state != PathState.HEALTHY:
            bucket["headroom_preserved"] += 1
        if headroom_preservation_opportunity:
            bucket["headroom_opportunities"] += 1


def finalize_baseline_metrics(bucket: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "trials": bucket["trials"],
        "admissions": bucket["admissions"],
        "rejections": bucket["rejections"],
        "admitted_despite_degradation": bucket["admitted_despite_degradation"],
        "strict_workload_success_rate_pct": round(
            100.0 * bucket["strict_healthy_success"] / max(bucket["strict_total"], 1), 1
        ),
        "tolerant_degraded_utilization_rate_pct": round(
            100.0 * bucket["tolerant_degraded_success"] / max(bucket["tolerant_total"], 1), 1
        ),
        "healthy_headroom_preservation_rate_pct": round(
            100.0 * bucket["headroom_preserved"] / max(bucket["headroom_opportunities"], 1), 1
        ),
        "mean_prs": round(mean(bucket["avg_prs_values"]), 3) if bucket["avg_prs_values"] else 0.0,
        "mean_fae": round(mean(bucket["avg_fae_values"]), 3) if bucket["avg_fae_values"] else 0.0,
    }


def experiment_admissibility_boundary() -> Dict[str, Any]:
    severity_levels = [1, 2, 3, 4, 5, 6]
    workloads = [WORKLOAD_BATCH, WORKLOAD_INTERACTIVE, WORKLOAD_RELEASE]
    rate_rows: List[List[Any]] = []
    chart_series: List[Tuple[str, List[float]]] = []
    series_values: Dict[str, List[float]] = {workload.name: [] for workload in workloads}
    records: List[Dict[str, Any]] = []

    for severity in severity_levels:
        admitted = Counter()
        chosen = Counter()
        gfs_values: List[float] = []
        p99_values: List[float] = []
        jitter_values: List[float] = []
        states: List[str] = []
        for trial in range(18):
            degraded_mean = 2.5 + severity * 2.8
            degraded_jitter = 0.5 + severity * 1.05
            degraded_drop = 0.0 if severity < 5 else 0.004 * (severity - 4)
            orchestrator = build_orchestrator(
                [
                    PoolTraceConfig("pool-degraded", degraded_mean, degraded_jitter, degraded_drop),
                    PoolTraceConfig("pool-healthy", 2.0, 0.25, 0.0),
                ],
                seed=severity * 101 + trial,
            )
            for workload in workloads:
                _, decision = orchestrator.route_session(workload)
                degraded = candidate_for_pool(decision, "pool-degraded")
                if degraded is None:
                    continue
                admitted[workload.name] += 1 if degraded.admissible else 0
                chosen[workload.name] += 1 if degraded.chosen else 0
                gfs_values.append(degraded.gfs)
                p99_values.append(degraded.health_snapshot.p99_latency_ms)
                jitter_values.append(degraded.health_snapshot.jitter_ms)
                states.append(degraded.path_state.value)
                if decision.chosen_pool_id:
                    orchestrator.release_session(decision.chosen_pool_id)

        row = [
            severity,
            f"{mean(gfs_values):.3f}",
            f"{mean(p99_values):.1f}",
            f"{mean(jitter_values):.1f}",
            dominant_state(states),
        ]
        record = {
            "severity": severity,
            "mean_gfs": round(mean(gfs_values), 3),
            "mean_p99_latency_ms": round(mean(p99_values), 1),
            "mean_jitter_ms": round(mean(jitter_values), 1),
            "dominant_path_state": dominant_state(states),
            "admissibility_rate_pct": {},
            "chosen_rate_pct": {},
        }
        for workload in workloads:
            rate = round(100.0 * admitted[workload.name] / 18, 1)
            chosen_rate = round(100.0 * chosen[workload.name] / 18, 1)
            row.append(rate)
            record["admissibility_rate_pct"][workload.name] = rate
            record["chosen_rate_pct"][workload.name] = chosen_rate
            series_values[workload.name].append(rate)
        rate_rows.append(row)
        records.append(record)

    for workload in workloads:
        chart_series.append((workload.name, series_values[workload.name]))

    write_json(OUTPUTS_DIR / "experiment_admissibility_boundary.json", {"points": records})
    write_csv(
        OUTPUTS_DIR / "experiment_admissibility_boundary.csv",
        [
            "severity",
            "mean_gfs",
            "mean_p99_latency_ms",
            "mean_jitter_ms",
            "dominant_path_state",
            "batch_admissibility_rate_pct",
            "interactive_admissibility_rate_pct",
            "release_admissibility_rate_pct",
        ],
        rate_rows,
    )
    write_text(
        OUTPUTS_DIR / "experiment_admissibility_boundary.md",
        "# Experiment 1: Admissibility Boundary Sweep\n\n"
        + "Same candidate path, increasing latency and jitter, three workload classes.\n\n"
        + markdown_table(
            [
                "Severity",
                "Mean GFS",
                "Mean p99 (ms)",
                "Mean jitter (ms)",
                "Dominant PathState",
                "Batch admissibility %",
                "Interactive admissibility %",
                "Release admissibility %",
            ],
            rate_rows,
        ),
    )
    write_text(
        OUTPUTS_DIR / "experiment_admissibility_boundary.svg",
        svg_line_chart(
            "Experiment 1: Admissibility vs degradation",
            "Degradation severity index",
            "Admissibility rate (%)",
            severity_levels,
            chart_series,
        ),
    )
    return {"points": records}


def experiment_capacity_tradeoff() -> Dict[str, Any]:
    occupancies = [2, 4, 5, 6, 7]
    rows: List[List[Any]] = []
    records: List[Dict[str, Any]] = []
    batch_on_degraded: List[float] = []
    strict_on_healthy: List[float] = []

    for occupancy in occupancies:
        counters = Counter()
        for trial in range(18):
            orchestrator = build_orchestrator(
                [
                    PoolTraceConfig(
                        "pool-healthy-tight",
                        2.0,
                        0.25,
                        0.0,
                        active_sessions=occupancy,
                    ),
                    PoolTraceConfig(
                        "pool-degraded-roomy",
                        6.2,
                        5.2,
                        0.0,
                        active_sessions=2,
                    ),
                ],
                seed=occupancy * 103 + trial,
            )
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
            for workload in workloads:
                _, decision = orchestrator.route_session(workload)
                chosen = chosen_candidate(decision)
                key = f"{workload.name}:{chosen.pool_id if chosen else 'none'}"
                counters[key] += 1
                counters[f"outcome:{decision.outcome.value}"] += 1
                if decision.chosen_pool_id:
                    orchestrator.release_session(decision.chosen_pool_id)

        batch_rate = round(100.0 * counters["batch:pool-degraded-roomy"] / 18, 1)
        strict_rate = round(
            100.0
            * (counters["interactive-sync:pool-healthy-tight"] + counters["release:pool-healthy-tight"])
            / 36,
            1,
        )
        reroute_rate = round(
            100.0 * counters["outcome:rerouted_to_alternate"] / 54,
            1,
        )
        rows.append(
            [
                occupancy,
                batch_rate,
                strict_rate,
                reroute_rate,
                counters["batch:pool-degraded-roomy"],
                counters["interactive-sync:pool-healthy-tight"] + counters["release:pool-healthy-tight"],
            ]
        )
        batch_on_degraded.append(batch_rate)
        strict_on_healthy.append(strict_rate)
        records.append(
            {
                "healthy_active_sessions": occupancy,
                "batch_on_degraded_rate_pct": batch_rate,
                "strict_on_healthy_rate_pct": strict_rate,
                "reroute_rate_pct": reroute_rate,
            }
        )

    write_json(OUTPUTS_DIR / "experiment_capacity_tradeoff.json", {"points": records})
    write_csv(
        OUTPUTS_DIR / "experiment_capacity_tradeoff.csv",
        [
            "healthy_active_sessions",
            "batch_on_degraded_rate_pct",
            "strict_on_healthy_rate_pct",
            "reroute_rate_pct",
            "batch_on_degraded_count",
            "strict_on_healthy_count",
        ],
        rows,
    )
    write_text(
        OUTPUTS_DIR / "experiment_capacity_tradeoff.md",
        "# Experiment 2: Capacity-Pressure Tradeoff Sweep\n\n"
        + "Healthy-path occupancy rises while a degraded-but-usable path keeps room. The policy question is when to preserve healthy headroom.\n\n"
        + markdown_table(
            [
                "Healthy active sessions",
                "Batch on degraded %",
                "Strict on healthy %",
                "Reroute rate %",
                "Batch degraded count",
                "Strict healthy count",
            ],
            rows,
        ),
    )
    write_text(
        OUTPUTS_DIR / "experiment_capacity_tradeoff.svg",
        svg_line_chart(
            "Experiment 2: Capacity pressure and path selection",
            "Healthy path active sessions",
            "Selection rate (%)",
            occupancies,
            [
                ("batch on degraded", batch_on_degraded),
                ("strict on healthy", strict_on_healthy),
            ],
        ),
    )
    return {"points": records}


def hysteresis_state_series(cfg: ThresholdConfig, *, seed: int) -> List[str]:
    del seed
    orchestrator = build_orchestrator(
        [PoolTraceConfig("pool-noisy", 2.0, 0.2, 0.0, sample_count=0)],
        cfg=cfg,
        seed=0,
    )
    pool_id = "pool-noisy"
    latencies: List[float] = (
        [20.0] * 6
        + [2.0] * 3
        + [21.0] * 6
        + [2.0] * 3
        + [19.0] * 6
        + [2.0] * 4
        + [22.0] * 5
        + [2.0] * 5
    )
    states: List[str] = []
    for latency in latencies:
        orchestrator.record_transfer(pool_id, latency, 0.0, BLOCK_SIZE)
        states.append(orchestrator.pools[pool_id].state.value)
    return states


def oscillation_count(states: Sequence[str]) -> int:
    count = 0
    for idx in range(1, len(states) - 1):
        if states[idx] == PathState.HEALTHY.value and states[idx - 1] != PathState.HEALTHY.value:
            lookahead = states[idx + 1 : idx + 4]
            if any(state != PathState.HEALTHY.value for state in lookahead):
                count += 1
        elif (
            states[idx] == PathState.RESTORED.value
            and states[idx - 1] != PathState.RESTORED.value
            and idx + 1 < len(states)
            and states[idx + 1] != PathState.RESTORED.value
        ):
            count += 1
    return count


def transition_count(states: Sequence[str]) -> int:
    return sum(1 for prev, curr in zip(states, states[1:]) if prev != curr)


def experiment_hysteresis_stability() -> Dict[str, Any]:
    default_cfg = ThresholdConfig(candidate_window=1, clean_to_restore=5, clean_to_healthy=8)
    no_hysteresis_cfg = ThresholdConfig(
        candidate_window=1,
        persistence_to_escalate=1,
        clean_to_restore=1,
        clean_to_healthy=1,
    )
    default_states = hysteresis_state_series(default_cfg, seed=707)
    no_hysteresis_states = hysteresis_state_series(no_hysteresis_cfg, seed=707)
    summary = {
        "default": {
            "transition_count": transition_count(default_states),
            "oscillation_count": oscillation_count(default_states),
            "state_distribution": dict(Counter(default_states)),
            "first_24_states": default_states[:24],
        },
        "no_hysteresis_baseline": {
            "transition_count": transition_count(no_hysteresis_states),
            "oscillation_count": oscillation_count(no_hysteresis_states),
            "state_distribution": dict(Counter(no_hysteresis_states)),
            "first_24_states": no_hysteresis_states[:24],
        },
    }
    avoided = (
        summary["no_hysteresis_baseline"]["oscillation_count"]
        - summary["default"]["oscillation_count"]
    )
    summary["oscillations_avoided"] = avoided

    rows = [
        [
            "default",
            summary["default"]["transition_count"],
            summary["default"]["oscillation_count"],
            dominant_state(default_states),
            " ".join(default_states[:12]),
        ],
        [
            "no_hysteresis_baseline",
            summary["no_hysteresis_baseline"]["transition_count"],
            summary["no_hysteresis_baseline"]["oscillation_count"],
            dominant_state(no_hysteresis_states),
            " ".join(no_hysteresis_states[:12]),
        ],
    ]
    write_json(OUTPUTS_DIR / "experiment_hysteresis_stability.json", summary)
    write_text(
        OUTPUTS_DIR / "experiment_hysteresis_stability.md",
        "# Experiment 3: Hysteresis and Flapping Stability\n\n"
        + f"Oscillations avoided by staged restore: **{avoided}**.\n\n"
        + markdown_table(
            [
                "Configuration",
                "State transitions",
                "Oscillations",
                "Dominant state",
                "First 12 states",
            ],
            rows,
        ),
    )
    return summary


def experiment_alternate_scarcity() -> Dict[str, Any]:
    alternate_counts = [0, 1, 3]
    rows: List[List[Any]] = []
    records: List[Dict[str, Any]] = []

    for alternate_count in alternate_counts:
        primary_metrics = defaultdict(list)
        for trial in range(12):
            traces = [
                PoolTraceConfig(
                    "pool-primary-degraded",
                    6.6,
                    4.9,
                    0.0,
                    active_sessions=5,
                )
            ]
            for alt_idx in range(alternate_count):
                traces.append(
                    PoolTraceConfig(
                        f"pool-alt-{alt_idx}",
                        2.1,
                        0.25,
                        0.0,
                        active_sessions=1,
                    )
                )
            orchestrator = build_orchestrator(traces, seed=alternate_count * 211 + trial)
            for workload in (WORKLOAD_BATCH, WORKLOAD_RELEASE):
                _, decision = orchestrator.route_session(workload)
                primary = candidate_for_pool(decision, "pool-primary-degraded")
                if primary is None:
                    continue
                primary_metrics[f"{workload.name}_prs"].append(primary.prs)
                primary_metrics[f"{workload.name}_fae"].append(primary.fae)
                primary_metrics[f"{workload.name}_admissible"].append(1 if primary.admissible else 0)
                primary_metrics[f"{workload.name}_chosen"].append(1 if primary.chosen else 0)
                if decision.chosen_pool_id:
                    orchestrator.release_session(decision.chosen_pool_id)

        row = [
            alternate_count,
            f"{mean(primary_metrics['batch_prs']):.3f}",
            f"{mean(primary_metrics['batch_fae']):.2f}",
            round(100.0 * mean(primary_metrics["batch_admissible"]), 1),
            round(100.0 * mean(primary_metrics["batch_chosen"]), 1),
            f"{mean(primary_metrics['release_prs']):.3f}",
            f"{mean(primary_metrics['release_fae']):.2f}",
        ]
        rows.append(row)
        records.append(
            {
                "alternate_count": alternate_count,
                "batch_prs": round(mean(primary_metrics["batch_prs"]), 3),
                "batch_fae": round(mean(primary_metrics["batch_fae"]), 2),
                "batch_admissible_rate_pct": round(100.0 * mean(primary_metrics["batch_admissible"]), 1),
                "batch_chosen_rate_pct": round(100.0 * mean(primary_metrics["batch_chosen"]), 1),
                "release_prs": round(mean(primary_metrics["release_prs"]), 3),
                "release_fae": round(mean(primary_metrics["release_fae"]), 2),
            }
        )

    write_json(OUTPUTS_DIR / "experiment_alternate_scarcity.json", {"points": records})
    write_text(
        OUTPUTS_DIR / "experiment_alternate_scarcity.md",
        "# Experiment 4: Alternate-Path Scarcity and Propagation Pressure\n\n"
        + "The same degraded candidate becomes a materially different policy object when alternates disappear.\n\n"
        + markdown_table(
            [
                "Alternate count",
                "Batch PRS",
                "Batch FAE",
                "Batch admissible %",
                "Batch chosen %",
                "Release PRS",
                "Release FAE",
            ],
            rows,
        ),
    )
    return {"points": records}


def experiment_baseline_comparison() -> Dict[str, Any]:
    baseline_names = ["orchestrator", "lowest_latency", "binary_health_only", "capacity_only"]
    buckets: Dict[str, Dict[str, Any]] = {
        name: {
            "trials": 0,
            "admissions": 0,
            "rejections": 0,
            "admitted_despite_degradation": 0,
            "strict_total": 0,
            "strict_healthy_success": 0,
            "tolerant_total": 0,
            "tolerant_degraded_success": 0,
            "headroom_opportunities": 0,
            "headroom_preserved": 0,
            "avg_prs_values": [],
            "avg_fae_values": [],
        }
        for name in baseline_names
    }

    workloads = [
        WORKLOAD_BATCH,
        WORKLOAD_INTERACTIVE,
        WORKLOAD_RELEASE,
        WorkloadProfile(
            name="strict-online",
            criticality=0.82,
            latency_sla_ms=22.0,
            sync_frequency=0.75,
            jitter_tolerance=0.2,
            is_prefill_decode_strict=True,
        ),
    ]
    rng = random.Random(1701)

    for trial in range(90):
        workload = workloads[trial % len(workloads)]
        degraded_mean = rng.uniform(4.8, 9.5)
        degraded_jitter = rng.uniform(2.8, 6.8)
        degraded_drop = rng.choice([0.0, 0.0, 0.0, 0.01])
        healthy_occupancy = rng.randint(2, 7)
        degraded_occupancy = rng.randint(0, 4)
        alternate_count = rng.choice([1, 1, 2])

        traces = [
            PoolTraceConfig(
                "pool-primary-degraded",
                degraded_mean,
                degraded_jitter,
                degraded_drop,
                active_sessions=degraded_occupancy,
            ),
            PoolTraceConfig(
                "pool-healthy-0",
                2.0,
                0.25,
                0.0,
                active_sessions=healthy_occupancy,
            ),
        ]
        for alt_idx in range(1, alternate_count):
            traces.append(
                PoolTraceConfig(
                    f"pool-healthy-{alt_idx}",
                    2.2,
                    0.3,
                    0.0,
                    active_sessions=rng.randint(1, 5),
                )
            )

        orchestrator = build_orchestrator(traces, seed=5000 + trial)
        _, decision = orchestrator.route_session(workload)
        candidates = decision.candidate_explanations
        headroom_preservation_opportunity = any(
            candidate.path_state == PathState.HEALTHY and candidate.capacity_snapshot.soft_saturated
            for candidate in candidates
        ) and any(
            candidate.path_state in (PathState.DEGRADED_USABLE, PathState.DEGRADED_RESTRICTED)
            and candidate.admissible
            for candidate in candidates
        )

        record_baseline_metrics(
            buckets["orchestrator"],
            workload,
            chosen_candidate(decision),
            headroom_preservation_opportunity=headroom_preservation_opportunity,
        )
        for baseline_name in baseline_names[1:]:
            record_baseline_metrics(
                buckets[baseline_name],
                workload,
                baseline_choice(baseline_name, candidates, workload),
                headroom_preservation_opportunity=headroom_preservation_opportunity,
            )
        if decision.chosen_pool_id:
            orchestrator.release_session(decision.chosen_pool_id)

    finalized = {name: finalize_baseline_metrics(bucket) for name, bucket in buckets.items()}
    rows = [
        [
            name,
            metrics["strict_workload_success_rate_pct"],
            metrics["tolerant_degraded_utilization_rate_pct"],
            metrics["healthy_headroom_preservation_rate_pct"],
            metrics["mean_prs"],
            metrics["mean_fae"],
            metrics["rejections"],
        ]
        for name, metrics in finalized.items()
    ]
    write_json(OUTPUTS_DIR / "experiment_baseline_comparison.json", finalized)
    write_text(
        OUTPUTS_DIR / "experiment_baseline_comparison.md",
        "# Experiment 5: Baseline Comparison\n\n"
        + "Simple baselines confirm that policy over transport retains information that naive routing policies throw away.\n\n"
        + markdown_table(
            [
                "Policy",
                "Strict healthy success %",
                "Tolerant degraded utilization %",
                "Healthy headroom preserved %",
                "Mean PRS",
                "Mean FAE",
                "Rejections",
            ],
            rows,
        ),
    )
    write_text(
        OUTPUTS_DIR / "experiment_baseline_comparison.svg",
        svg_grouped_bar_chart(
            "Experiment 5: Baseline comparison",
            ["strict success", "tolerant degraded use", "headroom preserved"],
            [
                (
                    "orchestrator",
                    [
                        finalized["orchestrator"]["strict_workload_success_rate_pct"],
                        finalized["orchestrator"]["tolerant_degraded_utilization_rate_pct"],
                        finalized["orchestrator"]["healthy_headroom_preservation_rate_pct"],
                    ],
                ),
                (
                    "lowest_latency",
                    [
                        finalized["lowest_latency"]["strict_workload_success_rate_pct"],
                        finalized["lowest_latency"]["tolerant_degraded_utilization_rate_pct"],
                        finalized["lowest_latency"]["healthy_headroom_preservation_rate_pct"],
                    ],
                ),
                (
                    "binary_health_only",
                    [
                        finalized["binary_health_only"]["strict_workload_success_rate_pct"],
                        finalized["binary_health_only"]["tolerant_degraded_utilization_rate_pct"],
                        finalized["binary_health_only"]["healthy_headroom_preservation_rate_pct"],
                    ],
                ),
                (
                    "capacity_only",
                    [
                        finalized["capacity_only"]["strict_workload_success_rate_pct"],
                        finalized["capacity_only"]["tolerant_degraded_utilization_rate_pct"],
                        finalized["capacity_only"]["healthy_headroom_preservation_rate_pct"],
                    ],
                ),
            ],
        ),
    )
    return finalized


def generate_experiment_summary(results: Dict[str, Any]) -> Dict[str, Any]:
    baseline = results["baseline_comparison"]
    orchestrator = baseline["orchestrator"]
    summary = {
        "key_findings": [
            "Admissibility is workload-relative: the same degraded candidate remains acceptable for tolerant work longer than for interactive or release-sensitive work.",
            "Capacity-aware policy preserves scarce healthy headroom for stricter traffic instead of always choosing the best raw health score.",
            "Hysteresis materially reduces oscillation under noisy conditions compared with a no-hysteresis baseline.",
            "Alternate-path scarcity raises PRS and FAE, making topology dependence visible in routing decisions.",
            "Naive routing baselines lose signal that the orchestrator preserves, especially around headroom preservation and tolerant use of degraded-but-usable paths.",
        ],
        "headline_metrics": {
            "strict_workload_success_rate_pct": orchestrator["strict_workload_success_rate_pct"],
            "tolerant_degraded_utilization_rate_pct": orchestrator["tolerant_degraded_utilization_rate_pct"],
            "healthy_headroom_preservation_rate_pct": orchestrator["healthy_headroom_preservation_rate_pct"],
            "hysteresis_oscillations_avoided": results["hysteresis_stability"]["oscillations_avoided"],
        },
        "baseline_deltas": {
            "strict_vs_capacity_only_pct_points": round(
                orchestrator["strict_workload_success_rate_pct"]
                - baseline["capacity_only"]["strict_workload_success_rate_pct"],
                1,
            ),
            "headroom_vs_binary_health_only_pct_points": round(
                orchestrator["healthy_headroom_preservation_rate_pct"]
                - baseline["binary_health_only"]["healthy_headroom_preservation_rate_pct"],
                1,
            ),
            "tolerant_use_vs_lowest_latency_pct_points": round(
                orchestrator["tolerant_degraded_utilization_rate_pct"]
                - baseline["lowest_latency"]["tolerant_degraded_utilization_rate_pct"],
                1,
            ),
        },
    }
    write_json(OUTPUTS_DIR / "experiment_summary.json", summary)
    write_text(
        OUTPUTS_DIR / "experiment_summary.md",
        "# Experiment Summary\n\n"
        + "This is the compact evidence layer for Seam Orchestrator. The experiments are not transport benchmarks; they test admissibility, routing policy, capacity-aware selection, hysteresis, and alternate-path dependence above the transport backend.\n\n"
        + "## Headline Metrics\n\n"
        + markdown_table(
            ["Metric", "Value"],
            [
                ["strict_workload_success_rate_pct", summary["headline_metrics"]["strict_workload_success_rate_pct"]],
                [
                    "tolerant_degraded_utilization_rate_pct",
                    summary["headline_metrics"]["tolerant_degraded_utilization_rate_pct"],
                ],
                [
                    "healthy_headroom_preservation_rate_pct",
                    summary["headline_metrics"]["healthy_headroom_preservation_rate_pct"],
                ],
                ["hysteresis_oscillations_avoided", summary["headline_metrics"]["hysteresis_oscillations_avoided"]],
            ],
        )
        + "\n\n## Key Findings\n\n"
        + "\n".join(f"- {finding}" for finding in summary["key_findings"])
        + "\n\n## Baseline Deltas\n\n"
        + markdown_table(
            ["Comparison", "Delta"],
            [[key, value] for key, value in summary["baseline_deltas"].items()],
        ),
    )
    return summary


def run_all_experiments() -> Dict[str, Any]:
    OUTPUTS_DIR.mkdir(exist_ok=True)
    results = {
        "admissibility_boundary": experiment_admissibility_boundary(),
        "capacity_tradeoff": experiment_capacity_tradeoff(),
        "hysteresis_stability": experiment_hysteresis_stability(),
        "alternate_scarcity": experiment_alternate_scarcity(),
        "baseline_comparison": experiment_baseline_comparison(),
    }
    results["summary"] = generate_experiment_summary(results)
    SCRATCH_EVENT_LOG.unlink(missing_ok=True)
    return results


def main() -> None:
    logging.getLogger("seam").setLevel(logging.CRITICAL)
    results = run_all_experiments()
    console_summary = {
        "strict_workload_success_rate_pct": results["summary"]["headline_metrics"]["strict_workload_success_rate_pct"],
        "tolerant_degraded_utilization_rate_pct": results["summary"]["headline_metrics"]["tolerant_degraded_utilization_rate_pct"],
        "healthy_headroom_preservation_rate_pct": results["summary"]["headline_metrics"]["healthy_headroom_preservation_rate_pct"],
        "hysteresis_oscillations_avoided": results["summary"]["headline_metrics"]["hysteresis_oscillations_avoided"],
    }
    print(json.dumps(console_summary, indent=2))


if __name__ == "__main__":
    main()
