import asyncio
import time
from dataclasses import dataclass


class DroppedCallError(Exception):
    """Raised by `Chaos.maybe_drop` when the orchestrator has armed a drop."""


@dataclass
class ChaosState:
    drop_next: int = 0
    latency_ms: int = 0
    latency_until_epoch: float = 0.0


class Chaos:
    def __init__(self) -> None:
        self.state = ChaosState()

    def arm_drop(self, count: int = 1) -> None:
        self.state.drop_next += max(0, count)

    def arm_latency(self, ms: int, duration_ms: int) -> None:
        self.state.latency_ms = max(0, ms)
        self.state.latency_until_epoch = time.time() + max(0, duration_ms) / 1000.0

    def reset(self) -> None:
        self.state = ChaosState()

    def snapshot(self) -> dict:
        return {
            "drop_next": self.state.drop_next,
            "latency_ms": self.state.latency_ms,
            "latency_active": time.time() < self.state.latency_until_epoch,
        }

    def maybe_drop(self) -> None:
        if self.state.drop_next > 0:
            self.state.drop_next -= 1
            raise DroppedCallError("simulated drop — Dapr will redeliver")

    async def maybe_delay(self) -> None:
        if self.state.latency_ms > 0 and time.time() < self.state.latency_until_epoch:
            await asyncio.sleep(self.state.latency_ms / 1000.0)
