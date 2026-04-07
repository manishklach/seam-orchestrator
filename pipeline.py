"""
Pipeline glue between workload admission/routing policy and a transfer backend.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from orchestrator import DecisionRecord, SeamOrchestrator, WorkloadProfile
from transport import KVTransferBackend, MockBackend

log = logging.getLogger("seam.pipeline")


class SeamPipeline:
    def __init__(
        self,
        orchestrator: Optional[SeamOrchestrator] = None,
        backend: Optional[KVTransferBackend] = None,
    ):
        self.orch = orchestrator or SeamOrchestrator()
        self.backend = backend or MockBackend(fault_mode="clean")

    def add_decode_pool(self, pool_id: str, host: str, port: int, **pool_kwargs) -> None:
        self.orch.register_pool(pool_id, host, port, **pool_kwargs)

    async def transfer_and_route(
        self,
        kv_data: bytes,
        workload: WorkloadProfile,
    ) -> Tuple[Optional[str], DecisionRecord]:
        pool_id, decision = self.orch.route_session(workload)
        if pool_id is None:
            log.error("All decode pools inadmissible for workload=%s", workload.name)
            return None, decision

        pool = self.orch.pools[pool_id]
        result = await self.backend.send_kv_block(kv_data, pool.host, pool.port)
        drop_rate = 0.0 if result.success else 1.0
        self.orch.record_transfer(pool_id, result.latency_ms, drop_rate, result.bytes_moved)

        if not result.success:
            log.warning("Transfer to %s failed: %s", pool_id, result.error)
            self.orch.release_session(pool_id)
            return None, decision

        log.debug("KV routed to %s latency=%.1fms", pool_id, result.latency_ms)
        return pool_id, decision

    def release_session(self, pool_id: str) -> None:
        self.orch.release_session(pool_id)

    def status(self) -> dict:
        return {"backend": self.backend.name(), "pools": self.orch.status()}
