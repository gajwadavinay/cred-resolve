"""Provider A: Fast, reliable, normal event ordering, low failure rate."""
import uuid
import random
import datetime
import time
from typing import Optional, Dict, Any, List

from .base import BaseProvider, ProviderCallResult, ProviderEvent


class ProviderA(BaseProvider):
    """
    Fast, reliable provider.
    - Failure rate: ~3%
    - Latency: 50–200ms
    - Events: always in correct order
    - No duplicate events
    """

    FAILURE_RATE = 0.03
    MIN_LATENCY_MS = 50
    MAX_LATENCY_MS = 200

    def __init__(self):
        self._call_count = 0
        self._failure_count = 0
        self._is_healthy = True

    @property
    def provider_id(self) -> str:
        return "VND_A"

    @property
    def name(self) -> str:
        return "Provider A (Fast/Reliable)"

    def initiate_call(self, phone: str, agent_id: str,
                      metadata: Optional[Dict[str, Any]] = None) -> ProviderCallResult:
        latency = random.uniform(self.MIN_LATENCY_MS, self.MAX_LATENCY_MS)

        if not self._is_healthy:
            return ProviderCallResult(
                success=False,
                provider_call_id=None,
                error="Provider A is unhealthy",
                latency_ms=latency,
            )

        self._call_count += 1

        if random.random() < self.FAILURE_RATE:
            self._failure_count += 1
            return ProviderCallResult(
                success=False,
                provider_call_id=None,
                error="Provider A: call initiation failed",
                latency_ms=latency,
            )

        provider_call_id = f"PCA-{uuid.uuid4().hex[:10].upper()}"
        return ProviderCallResult(
            success=True,
            provider_call_id=provider_call_id,
            latency_ms=latency,
        )

    def simulate_call_lifecycle(self, phone: str, answer_rate: float,
                                avg_talk_time: float) -> List[ProviderEvent]:
        """Simulate events in correct order with no duplicates."""
        now = datetime.datetime.utcnow()
        call_id = f"PCA-{uuid.uuid4().hex[:10].upper()}"
        events = []
        idem_prefix = uuid.uuid4().hex[:8]

        # RINGING always fires
        events.append(ProviderEvent(
            call_id=call_id,
            event_type="RINGING",
            provider_call_id=call_id,
            provider=self.provider_id,
            idempotency_key=f"{idem_prefix}-RINGING",
            event_at=now + datetime.timedelta(seconds=2),
        ))

        if random.random() < answer_rate:
            ring_delay = random.uniform(3, 15)
            events.append(ProviderEvent(
                call_id=call_id,
                event_type="ANSWERED",
                provider_call_id=call_id,
                provider=self.provider_id,
                idempotency_key=f"{idem_prefix}-ANSWERED",
                event_at=now + datetime.timedelta(seconds=ring_delay),
            ))

            # Talk time with some variance
            talk_time = max(10, random.gauss(avg_talk_time, avg_talk_time * 0.2))
            events.append(ProviderEvent(
                call_id=call_id,
                event_type="COMPLETED",
                provider_call_id=call_id,
                provider=self.provider_id,
                idempotency_key=f"{idem_prefix}-COMPLETED",
                event_at=now + datetime.timedelta(seconds=ring_delay + talk_time),
            ))
        else:
            # No answer → FAILED
            events.append(ProviderEvent(
                call_id=call_id,
                event_type="FAILED",
                provider_call_id=call_id,
                provider=self.provider_id,
                idempotency_key=f"{idem_prefix}-FAILED",
                event_at=now + datetime.timedelta(seconds=30),
            ))

        return events

    def is_healthy(self) -> bool:
        return self._is_healthy

    def set_healthy(self, healthy: bool):
        self._is_healthy = healthy

    def get_stats(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "healthy": self._is_healthy,
            "total_calls": self._call_count,
            "failures": self._failure_count,
            "failure_rate": (self._failure_count / max(self._call_count, 1)),
        }
