"""
seam_orchestrator/transport.py

Pluggable transport shim
------------------------
The orchestrator is transport-agnostic.
Swap the backend by changing one line — NIXL, libfabric, UCX, or mock.

In production this would call into:
  - NIXL Python bindings  (nvidia-nixl)
  - UCX-Py               (ucx-py)
  - Direct EFA via rdma-core / libibverbs
  - Or a simple socket for local dev / testing
"""

import asyncio
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TransferResult:
    success:        bool
    latency_ms:     float
    bytes_moved:    int
    error:          str = ""


# ── Abstract base — all backends implement this ───────────────────────────────

class KVTransferBackend(ABC):

    @abstractmethod
    async def send_kv_block(self,
                            kv_data: bytes,
                            dest_host: str,
                            dest_port: int) -> TransferResult:
        """Transfer a KV-cache block to a decode pool host."""
        ...

    @abstractmethod
    def name(self) -> str:
        ...


# ── Mock backend (local dev / CI — no real networking needed) ─────────────────

class MockBackend(KVTransferBackend):
    """
    Simulates realistic latency distributions.
    Inject gray-failure scenarios by setting fault_mode:
      "clean"      — normal operation
      "degraded"   — elevated p99 latency
      "jitter"     — high variance
      "drops"      — occasional transfer failures
    """

    def __init__(self, fault_mode: str = "clean", base_latency_ms: float = 2.0):
        self.fault_mode      = fault_mode
        self.base_latency_ms = base_latency_ms

    def name(self) -> str:
        return f"mock({self.fault_mode})"

    async def send_kv_block(self, kv_data: bytes,
                            dest_host: str, dest_port: int) -> TransferResult:
        latency_ms, drop = self._sample_latency()
        await asyncio.sleep(latency_ms / 1000.0)   # simulate network time

        if drop:
            return TransferResult(success=False, latency_ms=latency_ms,
                                  bytes_moved=0, error="simulated drop")

        return TransferResult(success=True, latency_ms=latency_ms,
                              bytes_moved=len(kv_data))

    def _sample_latency(self) -> tuple[float, bool]:
        mode = self.fault_mode
        base = self.base_latency_ms

        if mode == "clean":
            return max(0.1, random.gauss(base, 0.3)), False

        if mode == "degraded":
            # Elevated p99 — occasional slow transfers
            if random.random() < 0.05:
                return base * random.uniform(4, 8), False
            return max(0.1, random.gauss(base * 1.5, 0.8)), False

        if mode == "jitter":
            return max(0.1, random.gauss(base, base * 1.2)), False

        if mode == "drops":
            drop = random.random() < 0.04   # 4% drop rate
            return max(0.1, random.gauss(base * 2, 1.0)), drop

        return base, False


# ── NIXL shim (production — requires nvidia-nixl installed) ──────────────────

class NIXLBackend(KVTransferBackend):
    """
    Thin wrapper around NIXL Python bindings.
    NIXL handles the actual RDMA over EFA / UCX / libfabric.
    The orchestrator sits above this and makes admission decisions.

    Install:  pip install nvidia-nixl
    Docs:     https://github.com/ai-dynamo/nixl
    """

    def __init__(self, agent_config: dict = None):
        self._agent = None
        self._config = agent_config or {}

    def name(self) -> str:
        return "nixl"

    def _ensure_agent(self):
        if self._agent is None:
            try:
                import nixl                              # noqa: F401  (optional dep)
                self._agent = nixl.Agent(self._config)
            except ImportError:
                raise RuntimeError(
                    "nvidia-nixl not installed. "
                    "Use MockBackend for local development."
                )

    async def send_kv_block(self, kv_data: bytes,
                            dest_host: str, dest_port: int) -> TransferResult:
        self._ensure_agent()
        t0 = time.perf_counter()
        try:
            # NIXL does the RDMA transfer; we just time-box it
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._agent.send(kv_data, dest_host, dest_port)
            )
            latency_ms = (time.perf_counter() - t0) * 1000
            return TransferResult(success=True,
                                  latency_ms=latency_ms,
                                  bytes_moved=len(kv_data))
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000
            return TransferResult(success=False,
                                  latency_ms=latency_ms,
                                  bytes_moved=0,
                                  error=str(e))


# ── UCX shim (alternative — no NVIDIA dep) ────────────────────────────────────

class UCXBackend(KVTransferBackend):
    """
    Direct UCX-Py backend — works with EFA, RoCE, InfiniBand.
    Install:  pip install ucx-py
    """

    def name(self) -> str:
        return "ucx"

    async def send_kv_block(self, kv_data: bytes,
                            dest_host: str, dest_port: int) -> TransferResult:
        t0 = time.perf_counter()
        try:
            import ucp                                   # noqa: F401  (optional dep)
            ep = await ucp.create_endpoint(dest_host, dest_port)
            await ep.send(kv_data)
            await ep.close()
            latency_ms = (time.perf_counter() - t0) * 1000
            return TransferResult(success=True,
                                  latency_ms=latency_ms,
                                  bytes_moved=len(kv_data))
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000
            return TransferResult(success=False,
                                  latency_ms=latency_ms,
                                  bytes_moved=0,
                                  error=str(e))
