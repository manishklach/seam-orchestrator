"""
seam_orchestrator/pipeline.py — v2
Updated to pass WorkloadProfile instead of scalar criticality.
"""
import asyncio
import logging
from typing import Optional, Tuple, List

from .orchestrator import SeamOrchestrator, ThresholdConfig, WorkloadProfile, DecisionRecord
from .transport import KVTransferBackend, MockBackend

log = logging.getLogger("seam.pipeline")


class SeamPipeline:

    def __init__(self, orchestrator: SeamOrchestrator = None,
                 backend: KVTransferBackend = None):
        self.orch    = orchestrator or SeamOrchestrator()
        self.backend = backend      or MockBackend(fault_mode="clean")

    def add_decode_pool(self, pool_id: str, host: str, port: int):
        self.orch.register_pool(pool_id, host, port)

    async def transfer_and_route(self, kv_data: bytes,
                                 workload: WorkloadProfile
                                 ) -> Tuple[Optional[str], List[DecisionRecord]]:
        pool_id, records = self.orch.route_session(workload)
        if pool_id is None:
            log.error("All decode pools inadmissible")
            return None, records

        pool   = self.orch.pools[pool_id]
        result = await self.backend.send_kv_block(kv_data, pool.host, pool.port)

        drop_rate = 0.0 if result.success else 1.0
        self.orch.record_transfer(pool_id, result.latency_ms, drop_rate, result.bytes_moved)

        if not result.success:
            log.warning(f"Transfer to {pool_id} failed: {result.error}")
            self.orch.release_session(pool_id)
            return None, records

        log.debug(f"KV → {pool_id}  lat={result.latency_ms:.1f}ms")
        return pool_id, records

    def release_session(self, pool_id: str):
        self.orch.release_session(pool_id)

    def status(self) -> dict:
        return {"backend": self.backend.name(), "pools": self.orch.status()}
