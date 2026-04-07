"""
Transfer backends for the seam orchestrator prototype.

This module is intentionally thin. The point of the repository is not to replace
transport stacks such as NIXL, UCX, or libfabric; it is to keep a narrow
backend interface under a policy/control layer that can reason above transport.
"""

from __future__ import annotations

import asyncio
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Tuple


@dataclass
class TransferResult:
    success: bool
    latency_ms: float
    bytes_moved: int
    error: str = ""


class KVTransferBackend(ABC):
    @abstractmethod
    async def send_kv_block(
        self,
        kv_data: bytes,
        dest_host: str,
        dest_port: int,
    ) -> TransferResult:
        """Move a KV-cache block to a decode pool."""

    @abstractmethod
    def name(self) -> str:
        """Human-readable backend identifier."""


class MockBackend(KVTransferBackend):
    """
    Local-development transport shim with synthetic latency profiles.

    The mock backend is useful for policy exploration because it lets us drive
    degraded-but-live paths without turning the project into a transport
    benchmark suite.
    """

    def __init__(self, fault_mode: str = "clean", base_latency_ms: float = 2.0):
        self.fault_mode = fault_mode
        self.base_latency_ms = base_latency_ms

    def name(self) -> str:
        return f"mock({self.fault_mode})"

    async def send_kv_block(
        self,
        kv_data: bytes,
        dest_host: str,
        dest_port: int,
    ) -> TransferResult:
        latency_ms, drop = self._sample_latency()
        await asyncio.sleep(latency_ms / 1000.0)

        if drop:
            return TransferResult(
                success=False,
                latency_ms=latency_ms,
                bytes_moved=0,
                error="simulated drop",
            )

        return TransferResult(
            success=True,
            latency_ms=latency_ms,
            bytes_moved=len(kv_data),
        )

    def _sample_latency(self) -> Tuple[float, bool]:
        base = self.base_latency_ms
        mode = self.fault_mode

        if mode == "clean":
            return max(0.1, random.gauss(base, 0.3)), False

        if mode == "degraded":
            if random.random() < 0.05:
                return base * random.uniform(4, 8), False
            return max(0.1, random.gauss(base * 1.5, 0.8)), False

        if mode == "jitter":
            return max(0.1, random.gauss(base, base * 1.2)), False

        if mode == "drops":
            drop = random.random() < 0.04
            return max(0.1, random.gauss(base * 2, 1.0)), drop

        return base, False


class NIXLBackend(KVTransferBackend):
    """
    Thin wrapper for environments that expose NIXL Python bindings.
    """

    def __init__(self, agent_config: dict | None = None):
        self._agent = None
        self._config = agent_config or {}

    def name(self) -> str:
        return "nixl"

    def _ensure_agent(self):
        if self._agent is None:
            try:
                import nixl  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "nvidia-nixl is not installed. Use MockBackend locally."
                ) from exc
            self._agent = nixl.Agent(self._config)

    async def send_kv_block(
        self,
        kv_data: bytes,
        dest_host: str,
        dest_port: int,
    ) -> TransferResult:
        self._ensure_agent()
        started = time.perf_counter()
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._agent.send(kv_data, dest_host, dest_port),
            )
            latency_ms = (time.perf_counter() - started) * 1000
            return TransferResult(True, latency_ms, len(kv_data))
        except Exception as exc:  # pragma: no cover - optional dependency
            latency_ms = (time.perf_counter() - started) * 1000
            return TransferResult(False, latency_ms, 0, str(exc))


class UCXBackend(KVTransferBackend):
    """
    Thin wrapper for UCX-Py environments.
    """

    def name(self) -> str:
        return "ucx"

    async def send_kv_block(
        self,
        kv_data: bytes,
        dest_host: str,
        dest_port: int,
    ) -> TransferResult:
        started = time.perf_counter()
        try:
            import ucp  # type: ignore

            endpoint = await ucp.create_endpoint(dest_host, dest_port)
            await endpoint.send(kv_data)
            await endpoint.close()
            latency_ms = (time.perf_counter() - started) * 1000
            return TransferResult(True, latency_ms, len(kv_data))
        except Exception as exc:  # pragma: no cover - optional dependency
            latency_ms = (time.perf_counter() - started) * 1000
            return TransferResult(False, latency_ms, 0, str(exc))
