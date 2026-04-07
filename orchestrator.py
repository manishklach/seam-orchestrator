"""
Transport-agnostic orchestration for KV movement in disaggregated inference.

The control point in this prototype sits above the transfer backend. The
orchestrator evaluates whether a path is admissible for a workload right now,
then selects a decode pool using a decomposable policy that considers health,
capacity, topology dependence, and workload sensitivity.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import deque
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("seam")


class PathState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED_USABLE = "degraded_usable"
    DEGRADED_RESTRICTED = "degraded_restricted"
    QUARANTINE_CANDIDATE = "quarantine_candidate"
    QUARANTINED = "quarantined"
    RESTORED = "restored"


STATE_SEVERITY: Dict[PathState, float] = {
    PathState.HEALTHY: 0.00,
    PathState.DEGRADED_USABLE: 0.20,
    PathState.DEGRADED_RESTRICTED: 0.55,
    PathState.QUARANTINE_CANDIDATE: 0.80,
    PathState.QUARANTINED: 1.00,
    PathState.RESTORED: 0.10,
}


class DecisionOutcome(str, Enum):
    ADMITTED = "admitted"
    ADMITTED_DEGRADED = "admitted_despite_degradation"
    REJECTED_STATE = "rejected_pool_state"
    REJECTED_PRS = "rejected_prs_too_high"
    REJECTED_LATENCY_SLA = "rejected_latency_sla_violation"
    REJECTED_JITTER = "rejected_jitter_intolerance"
    REJECTED_CRITICALITY = "rejected_criticality_too_high_for_state"
    REJECTED_CAPACITY = "rejected_capacity"
    REROUTED = "rerouted_to_alternate"
    NO_POOL_AVAILABLE = "no_admissible_pool"


@dataclass
class WorkloadProfile:
    name: str = "custom"
    criticality: float = 0.5
    latency_sla_ms: float = 50.0
    sync_frequency: float = 0.5
    checkpoint_size_mb: float = 0.0
    jitter_tolerance: float = 0.5
    is_release_path: bool = False
    is_prefill_decode_strict: bool = True

    def effective_criticality(self) -> float:
        base = self.criticality
        if self.is_release_path:
            base = max(base, 0.90)
        if self.is_prefill_decode_strict:
            base = max(base, 0.60)
        return min(base, 1.0)


WORKLOAD_BATCH = WorkloadProfile(
    name="batch",
    criticality=0.2,
    latency_sla_ms=200.0,
    sync_frequency=0.1,
    jitter_tolerance=0.9,
    is_prefill_decode_strict=False,
)

WORKLOAD_INTERACTIVE = WorkloadProfile(
    name="interactive",
    criticality=0.7,
    latency_sla_ms=30.0,
    sync_frequency=0.6,
    jitter_tolerance=0.3,
    is_prefill_decode_strict=True,
)

WORKLOAD_RELEASE = WorkloadProfile(
    name="release",
    criticality=0.95,
    latency_sla_ms=15.0,
    sync_frequency=0.9,
    jitter_tolerance=0.1,
    is_release_path=True,
    is_prefill_decode_strict=True,
)


@dataclass
class ThresholdConfig:
    latency_degraded_ms: float = 5.0
    latency_restricted_ms: float = 15.0
    latency_quarantine_ms: float = 40.0
    jitter_degraded_ms: float = 2.0
    jitter_restricted_ms: float = 8.0
    drop_degraded: float = 0.001
    drop_restricted: float = 0.01
    drop_quarantine: float = 0.05
    persistence_to_escalate: int = 3
    clean_to_restore: int = 5
    clean_to_healthy: int = 8
    candidate_window: int = 10
    prs_reject_threshold: float = 0.75
    default_pool_capacity: int = 8
    default_soft_capacity_fraction: float = 0.75
    w_latency: float = 0.45
    w_jitter: float = 0.30
    w_drop: float = 0.25
    fae_cluster_scale: float = 100.0
    fae_max: float = 10.0


@dataclass
class PathSample:
    timestamp: float
    latency_ms: float
    drop_rate: float
    bytes_moved: int


@dataclass
class PathDependence:
    pool_id: str
    alternate_pool_ids: List[str]
    available_alternate_pool_ids: List[str]

    @property
    def alternate_count(self) -> int:
        return len(self.alternate_pool_ids)

    @property
    def available_alternate_count(self) -> int:
        return len(self.available_alternate_pool_ids)

    @property
    def sole_route(self) -> bool:
        return self.available_alternate_count == 0


@dataclass
class CapacitySnapshot:
    active_sessions: int
    max_capacity: int
    soft_limit: int
    utilization: float
    remaining: int
    soft_saturated: bool
    hard_saturated: bool


@dataclass
class HealthSnapshot:
    p99_latency_ms: float
    jitter_ms: float
    mean_drop_rate: float
    sample_count: int


@dataclass
class CandidateExplanation:
    pool_id: str
    path_state: PathState
    gfs: float
    prs: float
    fae: float
    admissible: bool
    primary_reason: str
    capacity_snapshot: CapacitySnapshot
    topology: PathDependence
    health_snapshot: HealthSnapshot
    chosen: bool = False
    skipped_reason: Optional[str] = None
    selection_policy: Optional[str] = None
    selection_vector: List[float] = field(default_factory=list)


@dataclass
class DecisionRecord:
    timestamp: float
    workload_name: str
    outcome: DecisionOutcome
    reason: str
    chosen_pool_id: Optional[str]
    candidate_explanations: List[CandidateExplanation]

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(asdict(self))


@dataclass
class DecodePool:
    pool_id: str
    host: str
    port: int
    max_capacity: int
    soft_capacity_fraction: float
    risk_group: str = "default"
    state: PathState = PathState.HEALTHY
    gfs: float = 0.0
    prs: float = 0.0
    fae: float = 0.0
    history: Deque[PathSample] = field(default_factory=lambda: deque(maxlen=60))
    active_sessions: int = 0
    bad_window_count: int = 0
    clean_window_count: int = 0

    def health_snapshot(self, window: int) -> HealthSnapshot:
        recent = list(self.history)[-window:]
        latencies = [sample.latency_ms for sample in recent]
        drops = [sample.drop_rate for sample in recent]
        return HealthSnapshot(
            p99_latency_ms=_percentile(latencies, 99) if latencies else 0.0,
            jitter_ms=statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
            mean_drop_rate=statistics.mean(drops) if drops else 0.0,
            sample_count=len(recent),
        )

    def capacity_snapshot(self) -> CapacitySnapshot:
        soft_limit = max(1, int(round(self.max_capacity * self.soft_capacity_fraction)))
        remaining = max(self.max_capacity - self.active_sessions, 0)
        utilization = self.active_sessions / max(self.max_capacity, 1)
        return CapacitySnapshot(
            active_sessions=self.active_sessions,
            max_capacity=self.max_capacity,
            soft_limit=soft_limit,
            utilization=utilization,
            remaining=remaining,
            soft_saturated=self.active_sessions >= soft_limit,
            hard_saturated=self.active_sessions >= self.max_capacity,
        )


class DecisionLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, event_type: str, payload: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": round(time.time(), 6),
            "event_type": event_type,
            "payload": to_jsonable(payload),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            json.dump(event, handle, sort_keys=True)
            handle.write("\n")


def compute_gfs(samples: Sequence[PathSample], cfg: ThresholdConfig) -> float:
    if not samples:
        return 0.0
    latencies = [sample.latency_ms for sample in samples]
    drop_rates = [sample.drop_rate for sample in samples]
    p99_lat = _percentile(latencies, 99)
    jitter = statistics.stdev(latencies) if len(latencies) > 1 else 0.0
    mean_drop = statistics.mean(drop_rates)
    n_lat = min(p99_lat / cfg.latency_quarantine_ms, 1.0)
    n_jit = min(jitter / (cfg.jitter_restricted_ms * 2), 1.0)
    n_drop = min(mean_drop / cfg.drop_quarantine, 1.0)
    gfs_base = cfg.w_latency * n_lat + cfg.w_jitter * n_jit + cfg.w_drop * n_drop
    interaction = 0.15 * (n_lat * n_drop)
    return min(gfs_base + interaction, 1.0)


def compute_prs(
    pool: DecodePool,
    cfg: ThresholdConfig,
    dependence: PathDependence,
    workload: WorkloadProfile,
) -> float:
    topology_exposure = 1.0 if dependence.sole_route else 1.0 / (
        1.0 + dependence.available_alternate_count
    )
    workload_sensitivity = workload.effective_criticality()
    path_dependence = 1.0 if dependence.sole_route else min(
        0.25 + pool.active_sessions / max(pool.max_capacity, 1), 1.0
    )
    state_severity = STATE_SEVERITY[pool.state]
    return min(
        topology_exposure * workload_sensitivity * path_dependence * state_severity,
        1.0,
    )


def compute_fae(
    pool: DecodePool,
    cfg: ThresholdConfig,
    dependence: PathDependence,
    workload: WorkloadProfile,
) -> float:
    if pool.gfs < 0.01:
        return 0.0

    utilization = pool.active_sessions / max(pool.max_capacity, 1)
    session_fraction = pool.active_sessions / max(cfg.fae_cluster_scale, 1.0)
    scarcity_multiplier = 2.5 if dependence.sole_route else 1.0 + (
        0.5 / max(dependence.available_alternate_count, 1)
    )
    criticality_weight = workload.effective_criticality()
    release_weight = 2.0 if workload.is_release_path else 1.0
    cluster_loss = min(
        max(session_fraction, utilization * 0.25)
        * scarcity_multiplier
        * criticality_weight
        * release_weight,
        1.0,
    )
    fae = cluster_loss / max(pool.gfs, 0.05)
    return min(fae, cfg.fae_max)


def next_state(pool: DecodePool, gfs: float, cfg: ThresholdConfig) -> PathState:
    current = pool.state

    if gfs > 0.65:
        if pool.bad_window_count >= cfg.persistence_to_escalate:
            return PathState.QUARANTINE_CANDIDATE
        return PathState.DEGRADED_RESTRICTED

    if gfs > 0.40:
        if pool.bad_window_count >= cfg.persistence_to_escalate:
            return PathState.DEGRADED_RESTRICTED
        return PathState.DEGRADED_USABLE

    if gfs > 0.18:
        return PathState.DEGRADED_USABLE

    if current in (PathState.QUARANTINE_CANDIDATE, PathState.QUARANTINED) and gfs < 0.15:
        return PathState.RESTORED

    if current == PathState.RESTORED:
        if pool.clean_window_count >= cfg.clean_to_healthy:
            return PathState.HEALTHY
        return PathState.RESTORED

    if current in (PathState.DEGRADED_USABLE, PathState.DEGRADED_RESTRICTED):
        if pool.clean_window_count >= cfg.clean_to_restore:
            return PathState.RESTORED
        return current

    if current == PathState.HEALTHY and gfs < 0.10:
        return PathState.HEALTHY

    return current


def is_admissible(
    pool: DecodePool,
    workload: WorkloadProfile,
    prs: float,
    health: HealthSnapshot,
    capacity: CapacitySnapshot,
) -> Tuple[bool, DecisionOutcome, str]:
    state = pool.state
    effective_criticality = workload.effective_criticality()

    if capacity.hard_saturated:
        return False, DecisionOutcome.REJECTED_CAPACITY, (
            f"capacity exhausted ({capacity.active_sessions}/{capacity.max_capacity})"
        )

    if state == PathState.QUARANTINED:
        return False, DecisionOutcome.REJECTED_STATE, (
            f"pool quarantined (gfs={pool.gfs:.3f})"
        )

    if state == PathState.QUARANTINE_CANDIDATE:
        return False, DecisionOutcome.REJECTED_STATE, (
            "quarantine candidate pending isolation"
        )

    if prs > 0.75:
        return False, DecisionOutcome.REJECTED_PRS, (
            f"PRS={prs:.3f} exceeds threshold {0.75:.2f}"
        )

    if health.p99_latency_ms > workload.latency_sla_ms:
        return False, DecisionOutcome.REJECTED_LATENCY_SLA, (
            f"p99={health.p99_latency_ms:.1f}ms exceeds SLA {workload.latency_sla_ms:.1f}ms"
        )

    jitter_budget = (1.0 - workload.jitter_tolerance) * 5.0
    if health.jitter_ms > jitter_budget and workload.jitter_tolerance < 0.4:
        return False, DecisionOutcome.REJECTED_JITTER, (
            f"jitter {health.jitter_ms:.1f}ms exceeds budget {jitter_budget:.1f}ms"
        )

    if state == PathState.DEGRADED_RESTRICTED:
        if effective_criticality > 0.50:
            return False, DecisionOutcome.REJECTED_CRITICALITY, (
                f"degraded_restricted blocks criticality {effective_criticality:.2f}"
            )
        return True, DecisionOutcome.ADMITTED_DEGRADED, (
            f"low-criticality workload can use degraded_restricted path ({effective_criticality:.2f})"
        )

    if state == PathState.DEGRADED_USABLE:
        if workload.is_release_path:
            return False, DecisionOutcome.REJECTED_CRITICALITY, (
                "release-critical workload blocked on degraded_usable path"
            )
        if effective_criticality > 0.85:
            return False, DecisionOutcome.REJECTED_CRITICALITY, (
                f"criticality {effective_criticality:.2f} too high for degraded_usable"
            )
        return True, DecisionOutcome.ADMITTED_DEGRADED, (
            f"degraded_usable remains admissible for workload criticality {effective_criticality:.2f}"
        )

    if state == PathState.RESTORED:
        if effective_criticality > 0.60:
            return False, DecisionOutcome.REJECTED_CRITICALITY, (
                f"restored path still blocks criticality {effective_criticality:.2f}"
            )
        return True, DecisionOutcome.ADMITTED_DEGRADED, (
            f"restored path admitted for workload criticality {effective_criticality:.2f}"
        )

    return True, DecisionOutcome.ADMITTED, "healthy path"


def selection_policy_for(workload: WorkloadProfile) -> str:
    effective_criticality = workload.effective_criticality()
    if effective_criticality <= 0.35:
        return "headroom_first"
    if effective_criticality >= 0.75 or workload.is_release_path:
        return "health_first"
    return "balanced"


def selection_vector_for(
    candidate: CandidateExplanation,
    workload: WorkloadProfile,
) -> List[float]:
    severity = STATE_SEVERITY[candidate.path_state]
    utilization = round(candidate.capacity_snapshot.utilization, 4)
    soft_saturated = 1.0 if candidate.capacity_snapshot.soft_saturated else 0.0
    preserve_healthy = 1.0 if candidate.path_state == PathState.HEALTHY else 0.0
    remaining = -float(candidate.capacity_snapshot.remaining)
    policy = selection_policy_for(workload)

    if policy == "headroom_first":
        return [
            preserve_healthy,
            soft_saturated,
            utilization,
            round(severity, 4),
            round(candidate.gfs, 4),
            remaining,
        ]

    if policy == "health_first":
        return [
            round(severity, 4),
            round(candidate.gfs, 4),
            soft_saturated,
            utilization,
            remaining,
        ]

    return [
        soft_saturated,
        round(severity, 4),
        utilization,
        round(candidate.gfs, 4),
        remaining,
    ]


class SeamOrchestrator:
    def __init__(
        self,
        cfg: Optional[ThresholdConfig] = None,
        event_log_path: str | Path = "outputs/decision_events.jsonl",
    ):
        self.cfg = cfg or ThresholdConfig()
        self.pools: Dict[str, DecodePool] = {}
        self.event_log = DecisionLog(event_log_path)

    def register_pool(
        self,
        pool_id: str,
        host: str,
        port: int,
        *,
        max_capacity: Optional[int] = None,
        soft_capacity_fraction: Optional[float] = None,
        risk_group: str = "default",
    ) -> None:
        self.pools[pool_id] = DecodePool(
            pool_id=pool_id,
            host=host,
            port=port,
            max_capacity=max_capacity or self.cfg.default_pool_capacity,
            soft_capacity_fraction=(
                soft_capacity_fraction or self.cfg.default_soft_capacity_fraction
            ),
            risk_group=risk_group,
        )
        log.info("Registered decode pool %s at %s:%s", pool_id, host, port)

    def set_active_sessions(self, pool_id: str, active_sessions: int) -> None:
        self.pools[pool_id].active_sessions = max(0, active_sessions)

    def record_transfer(
        self,
        pool_id: str,
        latency_ms: float,
        drop_rate: float,
        bytes_moved: int,
    ) -> None:
        pool = self.pools[pool_id]
        pool.history.append(
            PathSample(
                timestamp=time.time(),
                latency_ms=latency_ms,
                drop_rate=drop_rate,
                bytes_moved=bytes_moved,
            )
        )
        self._update_state(pool)

    def route_session(self, workload: WorkloadProfile) -> Tuple[Optional[str], DecisionRecord]:
        policy = selection_policy_for(workload)
        candidate_explanations: List[CandidateExplanation] = []
        admissible_candidates: List[CandidateExplanation] = []

        for pool in self.pools.values():
            dependence = self._build_dependence(pool.pool_id)
            health = pool.health_snapshot(self.cfg.candidate_window)
            capacity = pool.capacity_snapshot()
            prs = compute_prs(pool, self.cfg, dependence, workload)
            fae = compute_fae(pool, self.cfg, dependence, workload)
            pool.prs = prs
            pool.fae = fae

            admissible, outcome, reason = is_admissible(
                pool=pool,
                workload=workload,
                prs=prs,
                health=health,
                capacity=capacity,
            )
            explanation = CandidateExplanation(
                pool_id=pool.pool_id,
                path_state=pool.state,
                gfs=round(pool.gfs, 4),
                prs=round(prs, 4),
                fae=round(fae, 4),
                admissible=admissible,
                primary_reason=reason,
                capacity_snapshot=capacity,
                topology=dependence,
                health_snapshot=health,
                selection_policy=policy if admissible else None,
            )

            if admissible:
                explanation.selection_vector = selection_vector_for(explanation, workload)
                admissible_candidates.append(explanation)
            else:
                self.event_log.append(
                    "rejection",
                    {
                        "workload": workload.name,
                        "pool_id": pool.pool_id,
                        "outcome": outcome,
                        "candidate": explanation,
                    },
                )
            candidate_explanations.append(explanation)

        if not admissible_candidates:
            reason = "all candidate paths are inadmissible for this workload"
            record = DecisionRecord(
                timestamp=time.time(),
                workload_name=workload.name,
                outcome=DecisionOutcome.NO_POOL_AVAILABLE,
                reason=reason,
                chosen_pool_id=None,
                candidate_explanations=candidate_explanations,
            )
            self.event_log.append("no_pool_available", record.to_dict())
            return None, record

        admissible_candidates.sort(key=lambda candidate: tuple(candidate.selection_vector))
        chosen = admissible_candidates[0]
        chosen.chosen = True
        healthiest = min(
            admissible_candidates,
            key=lambda candidate: (
                STATE_SEVERITY[candidate.path_state],
                candidate.gfs,
                candidate.capacity_snapshot.utilization,
            ),
        )

        for candidate in candidate_explanations:
            if candidate.pool_id == chosen.pool_id:
                continue
            if not candidate.admissible:
                candidate.skipped_reason = candidate.primary_reason
                continue
            candidate.skipped_reason = self._skip_reason(policy, chosen, candidate)

        chosen_pool = self.pools[chosen.pool_id]
        chosen_pool.active_sessions += 1

        if chosen.pool_id != healthiest.pool_id:
            outcome = DecisionOutcome.REROUTED
            reason = (
                f"{policy} selected {chosen.pool_id} over healthier {healthiest.pool_id}"
            )
            self.event_log.append(
                "reroute",
                {
                    "workload": workload.name,
                    "chosen_pool_id": chosen.pool_id,
                    "healthiest_pool_id": healthiest.pool_id,
                    "policy": policy,
                    "candidates": candidate_explanations,
                },
            )
        elif chosen.path_state == PathState.HEALTHY:
            outcome = DecisionOutcome.ADMITTED
            reason = "healthy admissible pool selected"
            self.event_log.append(
                "admission",
                {
                    "workload": workload.name,
                    "pool_id": chosen.pool_id,
                    "candidate": chosen,
                },
            )
        else:
            outcome = DecisionOutcome.ADMITTED_DEGRADED
            reason = "degraded but admissible pool selected"
            self.event_log.append(
                "admission",
                {
                    "workload": workload.name,
                    "pool_id": chosen.pool_id,
                    "candidate": chosen,
                },
            )

        record = DecisionRecord(
            timestamp=time.time(),
            workload_name=workload.name,
            outcome=outcome,
            reason=reason,
            chosen_pool_id=chosen.pool_id,
            candidate_explanations=candidate_explanations,
        )
        return chosen.pool_id, record

    def release_session(self, pool_id: str) -> None:
        if pool_id in self.pools:
            self.pools[pool_id].active_sessions = max(
                0, self.pools[pool_id].active_sessions - 1
            )

    def status(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for pool_id, pool in self.pools.items():
            health = pool.health_snapshot(self.cfg.candidate_window)
            out[pool_id] = {
                "state": pool.state.value,
                "risk_group": pool.risk_group,
                "gfs": round(pool.gfs, 3),
                "prs": round(pool.prs, 3),
                "fae": round(pool.fae, 3),
                "p99_lat_ms": round(health.p99_latency_ms, 2),
                "jitter_ms": round(health.jitter_ms, 2),
                "drop_rate": round(health.mean_drop_rate, 4),
                "active_sessions": pool.active_sessions,
                "max_capacity": pool.max_capacity,
                "soft_limit": pool.capacity_snapshot().soft_limit,
                "bad_windows": pool.bad_window_count,
                "clean_windows": pool.clean_window_count,
                "samples": len(pool.history),
            }
        return out

    def _build_dependence(self, pool_id: str) -> PathDependence:
        alternate_pool_ids = [other_id for other_id in self.pools if other_id != pool_id]
        available_alt_ids = [
            other_id
            for other_id in alternate_pool_ids
            if self._pool_available_for_failover(self.pools[other_id])
        ]
        return PathDependence(
            pool_id=pool_id,
            alternate_pool_ids=alternate_pool_ids,
            available_alternate_pool_ids=available_alt_ids,
        )

    def _pool_available_for_failover(self, pool: DecodePool) -> bool:
        if pool.state in (PathState.QUARANTINED, PathState.QUARANTINE_CANDIDATE):
            return False
        return not pool.capacity_snapshot().hard_saturated

    def _skip_reason(
        self,
        policy: str,
        chosen: CandidateExplanation,
        skipped: CandidateExplanation,
    ) -> str:
        chosen_capacity = chosen.capacity_snapshot
        skipped_capacity = skipped.capacity_snapshot
        if policy == "headroom_first":
            return (
                f"headroom_first preferred {chosen.pool_id} "
                f"({chosen_capacity.active_sessions}/{chosen_capacity.max_capacity}) over "
                f"{skipped.pool_id} ({skipped_capacity.active_sessions}/{skipped_capacity.max_capacity})"
            )
        if policy == "health_first":
            return (
                f"health_first preferred {chosen.pool_id} in state {chosen.path_state.value}"
            )
        return (
            f"balanced policy ranked {chosen.pool_id} ahead with vector "
            f"{chosen.selection_vector}"
        )

    def _update_state(self, pool: DecodePool) -> None:
        recent = list(pool.history)[-self.cfg.candidate_window :]
        gfs = compute_gfs(recent, self.cfg)
        pool.gfs = gfs

        if gfs > 0.18:
            pool.bad_window_count += 1
            pool.clean_window_count = 0
        else:
            pool.clean_window_count += 1
            pool.bad_window_count = 0

        previous_state = pool.state
        new_state = next_state(pool, gfs, self.cfg)
        if new_state != previous_state:
            pool.state = new_state
            payload = {
                "pool_id": pool.pool_id,
                "from_state": previous_state,
                "to_state": new_state,
                "gfs": round(gfs, 4),
                "bad_windows": pool.bad_window_count,
                "clean_windows": pool.clean_window_count,
            }
            self.event_log.append("state_transition", payload)
            if new_state == PathState.RESTORED:
                self.event_log.append("restore", payload)
            log.warning(
                "%s: %s -> %s gfs=%.3f bad=%s clean=%s",
                pool.pool_id,
                previous_state.value,
                new_state.value,
                gfs,
                pool.bad_window_count,
                pool.clean_window_count,
            )


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return value


def _percentile(data: Iterable[float], pct: int) -> float:
    values = sorted(data)
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    k = (len(values) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)
