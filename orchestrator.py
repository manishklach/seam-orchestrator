"""
seam_orchestrator/orchestrator.py  — v2

Additions over v1:
  1. FailureAmplificationEstimate (FAE)
  2. WorkloadProfile struct (replaces scalar criticality)
  3. Explicit DecisionRecord with typed reroute reasons
  4. Hardened hysteresis / restoration logic (no flap)

Patent ref: Claims 1, 4-9, 12, 15-17 — FIG. 3, 4, 5, 6, 8
"""

import time
import statistics
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple
from collections import deque
import logging

log = logging.getLogger("seam")


# ── 1. State machine (FIG. 3) ─────────────────────────────────────────────────

class PathState(Enum):
    HEALTHY              = "healthy"
    DEGRADED_USABLE      = "degraded_usable"
    DEGRADED_RESTRICTED  = "degraded_restricted"
    QUARANTINE_CANDIDATE = "quarantine_candidate"
    QUARANTINED          = "quarantined"
    RESTORED             = "restored"

STATE_SEVERITY: Dict = {
    PathState.HEALTHY:              0.00,
    PathState.DEGRADED_USABLE:      0.20,
    PathState.DEGRADED_RESTRICTED:  0.55,
    PathState.QUARANTINE_CANDIDATE: 0.80,
    PathState.QUARANTINED:          1.00,
    PathState.RESTORED:             0.10,
}


# ── 2. WorkloadProfile (replaces scalar criticality) ──────────────────────────

@dataclass
class WorkloadProfile:
    """
    Describes a workload's sensitivity to path degradation.
    Same degraded pool can be admissible for BATCH and inadmissible for RELEASE.
    Patent: Claim 7, 8, 16
    """
    criticality:              float = 0.5
    latency_sla_ms:           float = 50.0
    sync_frequency:           float = 0.5
    checkpoint_size_mb:       float = 0.0
    jitter_tolerance:         float = 0.5
    is_release_path:          bool  = False
    is_prefill_decode_strict: bool  = True

    def effective_criticality(self) -> float:
        base = self.criticality
        if self.is_release_path:
            base = max(base, 0.90)
        if self.is_prefill_decode_strict:
            base = max(base, 0.60)
        return min(base, 1.0)


WORKLOAD_BATCH       = WorkloadProfile(criticality=0.2, latency_sla_ms=200.0,
                                        sync_frequency=0.1, jitter_tolerance=0.9,
                                        is_prefill_decode_strict=False)
WORKLOAD_INTERACTIVE = WorkloadProfile(criticality=0.7, latency_sla_ms=30.0,
                                        sync_frequency=0.6, jitter_tolerance=0.3,
                                        is_prefill_decode_strict=True)
WORKLOAD_RELEASE     = WorkloadProfile(criticality=0.95, latency_sla_ms=15.0,
                                        sync_frequency=0.9, jitter_tolerance=0.1,
                                        is_release_path=True,
                                        is_prefill_decode_strict=True)


# ── 3. DecisionRecord (explicit reroute reasons) ───────────────────────────────

class DecisionOutcome(Enum):
    ADMITTED             = "admitted"
    ADMITTED_DEGRADED    = "admitted_despite_degradation"
    REJECTED_STATE       = "rejected_pool_state"
    REJECTED_PRS         = "rejected_prs_too_high"
    REJECTED_LATENCY_SLA = "rejected_latency_sla_violation"
    REJECTED_JITTER      = "rejected_jitter_intolerance"
    REJECTED_CRITICALITY = "rejected_criticality_too_high_for_state"
    REROUTED             = "rerouted_to_alternate"
    NO_POOL_AVAILABLE    = "no_admissible_pool"


@dataclass
class DecisionRecord:
    timestamp:   float
    pool_id:     str
    outcome:     DecisionOutcome
    reason:      str
    gfs:         float
    prs:         float
    fae:         float
    pool_state:  PathState
    workload:    WorkloadProfile
    chosen_pool: Optional[str] = None


# ── 4. Thresholds ──────────────────────────────────────────────────────────────

@dataclass
class ThresholdConfig:
    latency_degraded_ms:   float = 5.0
    latency_restricted_ms: float = 15.0
    latency_quarantine_ms: float = 40.0
    jitter_degraded_ms:    float = 2.0
    jitter_restricted_ms:  float = 8.0
    drop_degraded:         float = 0.001
    drop_restricted:       float = 0.01
    drop_quarantine:       float = 0.05
    persistence_to_escalate: int = 3
    clean_to_restore:        int = 5
    clean_to_healthy:        int = 8
    w_latency: float = 0.45
    w_jitter:  float = 0.30
    w_drop:    float = 0.25
    fae_cluster_scale: float = 100.0
    fae_max:           float = 10.0


# ── Telemetry sample ──────────────────────────────────────────────────────────

@dataclass
class PathSample:
    timestamp:   float
    latency_ms:  float
    drop_rate:   float
    bytes_moved: int


# ── Decode pool ────────────────────────────────────────────────────────────────

@dataclass
class DecodePool:
    pool_id:         str
    host:            str
    port:            int
    state:           PathState = PathState.HEALTHY
    gfs:             float = 0.0
    prs:             float = 0.0
    fae:             float = 0.0
    history:         deque = field(default_factory=lambda: deque(maxlen=60))
    active_sessions: int   = 0
    bad_window_count:   int = 0
    clean_window_count: int = 0
    decisions: List = field(default_factory=list)


# ── GFS (patent Section 2) ────────────────────────────────────────────────────

def compute_gfs(samples: List[PathSample], cfg: ThresholdConfig) -> float:
    if not samples:
        return 0.0
    latencies  = [s.latency_ms for s in samples]
    drop_rates = [s.drop_rate  for s in samples]
    p99_lat    = _percentile(latencies, 99)
    jitter     = statistics.stdev(latencies) if len(latencies) > 1 else 0.0
    mean_drop  = statistics.mean(drop_rates)
    n_lat  = min(p99_lat   / cfg.latency_quarantine_ms, 1.0)
    n_jit  = min(jitter    / (cfg.jitter_restricted_ms * 2), 1.0)
    n_drop = min(mean_drop / cfg.drop_quarantine, 1.0)
    gfs_base    = cfg.w_latency * n_lat + cfg.w_jitter * n_jit + cfg.w_drop * n_drop
    interaction = 0.15 * (n_lat * n_drop)
    return min(gfs_base + interaction, 1.0)


# ── PRS (patent Section 3) ────────────────────────────────────────────────────

def compute_prs(pool: DecodePool, cfg: ThresholdConfig,
                n_alternate_pools: int, workload: WorkloadProfile) -> float:
    topology_exposure    = 1.0 if n_alternate_pools == 0 else 1.0 / (1.0 + n_alternate_pools)
    workload_sensitivity = workload.effective_criticality()
    path_dependence      = min(0.3 + pool.active_sessions / 100.0, 1.0)
    state_severity       = STATE_SEVERITY[pool.state]
    return min(topology_exposure * workload_sensitivity * path_dependence * state_severity, 1.0)


# ── FAE (patent Section 3 / Claim 6) — NEW ────────────────────────────────────

def compute_fae(pool: DecodePool, cfg: ThresholdConfig,
                n_alternate_pools: int, workload: WorkloadProfile) -> float:
    """
    FAE = expected cluster-level useful-work loss / local resource loss

    A small degradation on a sole-path, release-critical, heavily-loaded pool
    has enormous cluster-level impact vs the same on a lightly-loaded pool with alternates.
    """
    if pool.gfs < 0.01:
        return 0.0

    session_fraction      = pool.active_sessions / max(cfg.fae_cluster_scale, 1.0)
    sole_route_multiplier = 2.5 if n_alternate_pools == 0 else 1.0
    criticality_weight    = workload.effective_criticality()
    release_weight        = 2.0 if workload.is_release_path else 1.0

    cluster_loss = min(
        session_fraction * sole_route_multiplier * criticality_weight * release_weight,
        1.0
    )
    fae = cluster_loss / pool.gfs if pool.gfs > 0 else 0.0
    return min(fae, cfg.fae_max)


# ── State transitions with hysteresis (FIG. 3) ────────────────────────────────

def next_state(pool: DecodePool, gfs: float, cfg: ThresholdConfig) -> PathState:
    current = pool.state

    # Escalation — requires persistence_to_escalate consecutive bad windows
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

    # Recovery — conservative, staged, no direct jump back to HEALTHY
    if current == PathState.QUARANTINE_CANDIDATE and gfs < 0.15:
        return PathState.RESTORED

    if current == PathState.QUARANTINED and gfs < 0.10:
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


# ── Admissibility (patent Section 4, Claims 7, 8, 16) ────────────────────────

def is_admissible(pool: DecodePool, workload: WorkloadProfile,
                  prs: float, fae: float,
                  p99_latency_ms: float,
                  jitter_ms: float) -> Tuple[bool, DecisionOutcome, str]:
    state = pool.state
    ec    = workload.effective_criticality()

    # Hard blocks
    if state == PathState.QUARANTINED:
        return False, DecisionOutcome.REJECTED_STATE, \
               f"pool quarantined (gfs={pool.gfs:.3f})"

    if state == PathState.QUARANTINE_CANDIDATE:
        return False, DecisionOutcome.REJECTED_STATE, \
               f"quarantine candidate — pre-emptive block (bad_windows={pool.bad_window_count})"

    # PRS gate
    if prs > 0.75:
        return False, DecisionOutcome.REJECTED_PRS, \
               f"PRS={prs:.3f} exceeds threshold 0.75"

    # Latency SLA check
    if p99_latency_ms > workload.latency_sla_ms:
        return False, DecisionOutcome.REJECTED_LATENCY_SLA, \
               f"p99={p99_latency_ms:.1f}ms > SLA {workload.latency_sla_ms:.1f}ms"

    # Jitter check for intolerant workloads
    jitter_budget = (1.0 - workload.jitter_tolerance) * 5.0
    if jitter_ms > jitter_budget and workload.jitter_tolerance < 0.4:
        return False, DecisionOutcome.REJECTED_JITTER, \
               f"jitter {jitter_ms:.1f}ms > budget {jitter_budget:.1f}ms"

    # Criticality gates per state
    if state == PathState.DEGRADED_RESTRICTED:
        if ec > 0.50:
            return False, DecisionOutcome.REJECTED_CRITICALITY, \
                   f"DEGRADED_RESTRICTED + effective_criticality={ec:.2f} > 0.50"
        return True, DecisionOutcome.ADMITTED_DEGRADED, \
               f"admitted despite DEGRADED_RESTRICTED (low criticality {ec:.2f})"

    if state == PathState.DEGRADED_USABLE:
        if workload.is_release_path:
            return False, DecisionOutcome.REJECTED_CRITICALITY, \
                   "release-path blocked on DEGRADED_USABLE pool"
        if ec > 0.85:
            return False, DecisionOutcome.REJECTED_CRITICALITY, \
                   f"effective_criticality={ec:.2f} too high for DEGRADED_USABLE"
        return True, DecisionOutcome.ADMITTED_DEGRADED, \
               f"admitted despite DEGRADED_USABLE (criticality {ec:.2f} acceptable)"

    if state == PathState.RESTORED:
        if ec > 0.60:
            return False, DecisionOutcome.REJECTED_CRITICALITY, \
                   f"pool RESTORED — blocking criticality {ec:.2f} > 0.60"
        return True, DecisionOutcome.ADMITTED_DEGRADED, \
               f"admitted on RESTORED pool (criticality {ec:.2f})"

    return True, DecisionOutcome.ADMITTED, "healthy path"


# ── Main orchestrator ──────────────────────────────────────────────────────────

class SeamOrchestrator:

    def __init__(self, cfg: ThresholdConfig = None):
        self.cfg   = cfg or ThresholdConfig()
        self.pools: Dict[str, DecodePool] = {}

    def register_pool(self, pool_id: str, host: str, port: int):
        self.pools[pool_id] = DecodePool(pool_id=pool_id, host=host, port=port)
        log.info(f"Registered decode pool {pool_id} at {host}:{port}")

    def record_transfer(self, pool_id: str, latency_ms: float,
                        drop_rate: float, bytes_moved: int):
        pool = self.pools[pool_id]
        pool.history.append(PathSample(
            timestamp=time.time(), latency_ms=latency_ms,
            drop_rate=drop_rate, bytes_moved=bytes_moved))
        self._update_state(pool)

    def route_session(self, workload: WorkloadProfile) -> Tuple[Optional[str], List[DecisionRecord]]:
        records    = []
        candidates = []
        n_pools    = len(self.pools)

        for pool in self.pools.values():
            recent  = list(pool.history)[-10:]
            p99_lat = _percentile([s.latency_ms for s in recent], 99) if recent else 0.0
            jitter  = statistics.stdev([s.latency_ms for s in recent]) if len(recent) > 1 else 0.0

            prs = compute_prs(pool, self.cfg, n_pools - 1, workload)
            fae = compute_fae(pool, self.cfg, n_pools - 1, workload)
            pool.prs = prs
            pool.fae = fae

            admissible, outcome, reason = is_admissible(
                pool, workload, prs, fae, p99_lat, jitter)

            rec = DecisionRecord(
                timestamp=time.time(), pool_id=pool.pool_id,
                outcome=outcome, reason=reason,
                gfs=pool.gfs, prs=prs, fae=fae,
                pool_state=pool.state, workload=workload)

            if admissible:
                score = pool.gfs + (pool.active_sessions / 200.0)
                candidates.append((score, pool.pool_id, rec))
            else:
                log.info(f"  ✗ {pool.pool_id} [{outcome.value}]: {reason}")
                records.append(rec)

        if not candidates:
            log.warning("No admissible decode pool — all paths blocked")
            return None, records

        candidates.sort(key=lambda x: x[0])
        _, chosen_id, chosen_rec = candidates[0]
        chosen_rec.chosen_pool = chosen_id
        records.append(chosen_rec)
        self.pools[chosen_id].active_sessions += 1
        self.pools[chosen_id].decisions.append(chosen_rec)
        return chosen_id, records

    def release_session(self, pool_id: str):
        if pool_id in self.pools:
            self.pools[pool_id].active_sessions = max(
                0, self.pools[pool_id].active_sessions - 1)

    def _update_state(self, pool: DecodePool):
        recent = list(pool.history)[-10:]
        gfs    = compute_gfs(recent, self.cfg)
        pool.gfs = gfs
        if gfs > 0.18:
            pool.bad_window_count   += 1
            pool.clean_window_count  = 0
        else:
            pool.clean_window_count += 1
            pool.bad_window_count    = 0
        new_state = next_state(pool, gfs, self.cfg)
        if new_state != pool.state:
            log.warning(
                f"  ⚡ {pool.pool_id}: {pool.state.value} → {new_state.value}  "
                f"GFS={gfs:.3f}  bad={pool.bad_window_count}  clean={pool.clean_window_count}")
            pool.state = new_state

    def status(self) -> Dict:
        out = {}
        for pid, p in self.pools.items():
            recent  = list(p.history)[-10:]
            p99_lat = _percentile([s.latency_ms for s in recent], 99) if recent else 0.0
            jitter  = statistics.stdev([s.latency_ms for s in recent]) if len(recent) > 1 else 0.0
            out[pid] = {
                "state": p.state.value, "gfs": round(p.gfs, 3),
                "prs": round(p.prs, 3), "fae": round(p.fae, 3),
                "p99_lat_ms": round(p99_lat, 2), "jitter_ms": round(jitter, 2),
                "sessions": p.active_sessions,
                "bad_windows": p.bad_window_count,
                "clean_windows": p.clean_window_count,
                "samples": len(p.history),
            }
        return out


def _percentile(data: List[float], pct: int) -> float:
    if not data:
        return 0.0
    sd = sorted(data)
    k  = (len(sd) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(sd) - 1)
    return sd[lo] + (sd[hi] - sd[lo]) * (k - lo)
