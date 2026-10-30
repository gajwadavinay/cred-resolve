"""Provider B: Slower, unreliable, duplicate events, out-of-order events, higher failure rate."""
import uuid
import random
import datetime
from typing import Optional, Dict, Any, List

from .base import BaseProvider, ProviderCallResult, ProviderEvent


class ProviderB(BaseProvider):
    """
    Slow, unreliable provider.
    - Failure rate: ~15%
    - Latency: 500–2000ms
    - Occasional duplicate events (~10% of events)
    - Out-of-order events (~5% chance)
    - Occasional timeouts (~5%)
    """

    FAILURE_RATE = 0.15
    TIMEOUT_RATE = 0.05
    DUPLICATE_EVENT_RATE = 0.10
    OUT_OF_ORDER_RATE = 0.05
    MIN_LATENCY_MS = 500
    MAX_LATENCY_MS = 2000

    def __init__(self):
        self._call_count = 0
        self._failure_count = 0
        self._timeout_count = 0
        self._is_healthy = True

    @property
    def provider_id(self) -> str:
        return "VND_B"

    @property
    def name(self) -> str:
        return "Provider B (Slow/Unreliable)"

    def initiate_call(self, phone: str, agent_id: str,
                      metadata: Optional[Dict[str, Any]] = None) -> ProviderCallResult:
        latency = random.uniform(self.MIN_LATENCY_MS, self.MAX_LATENCY_MS)

        if not self._is_healthy:
            return ProviderCallResult(
                success=False,
                provider_call_id=None,
                error="Provider B is unhealthy (outage)",
                latency_ms=latency,
            )

        self._call_count += 1

        # Timeout
        if random.random() < self.TIMEOUT_RATE:
            self._timeout_count += 1
            return ProviderCallResult(
                success=False,
                provider_call_id=None,
                error="Provider B: request timeout",
                latency_ms=self.MAX_LATENCY_MS,
            )

        # Failure
        if random.random() < self.FAILURE_RATE:
            self._failure_count += 1
            return ProviderCallResult(
                success=False,
                provider_call_id=None,
                error="Provider B: call initiation failed",
                latency_ms=latency,
            )

        provider_call_id = f"PCB-{uuid.uuid4().hex[:10].upper()}"
        return ProviderCallResult(
            success=True,
            provider_call_id=provider_call_id,
            latency_ms=latency,
        )

    def simulate_call_lifecycle(self, phone: str, answer_rate: float,
                                avg_talk_time: float) -> List[ProviderEvent]:
        """
        Simulate events with:
        - Possible duplicate events
        - Possible out-of-order events
        - Higher chance of failure
        """
        now = datetime.datetime.utcnow()
        call_id = f"PCB-{uuid.uuid4().hex[:10].upper()}"
        events = []
        idem_prefix = uuid.uuid4().hex[:8]

        # RINGING (slower)
        ringing_delay = random.uniform(3, 8)
        events.append(ProviderEvent(
            call_id=call_id,
            event_type="RINGING",
            provider_call_id=call_id,
            provider=self.provider_id,
            idempotency_key=f"{idem_prefix}-RINGING",
            event_at=now + datetime.timedelta(seconds=ringing_delay),
        ))

        # Inject duplicate RINGING ~10% of the time
        if random.random() < self.DUPLICATE_EVENT_RATE:
            events.append(ProviderEvent(
                call_id=call_id,
                event_type="RINGING",
                provider_call_id=call_id,
                provider=self.provider_id,
                idempotency_key=f"{idem_prefix}-RINGING",  # SAME key → dedup works
                event_at=now + datetime.timedelta(seconds=ringing_delay + 1),
            ))

        # Apply higher effective failure rate
        effective_fail = self.FAILURE_RATE
        if random.random() < effective_fail:
            events.append(ProviderEvent(
                call_id=call_id,
                event_type="FAILED",
                provider_call_id=call_id,
                provider=self.provider_id,
                idempotency_key=f"{idem_prefix}-FAILED",
                event_at=now + datetime.timedelta(seconds=ringing_delay + 5),
            ))
            return events

        if random.random() < answer_rate:
            ring_delay_total = ringing_delay + random.uniform(5, 20)
            talk_time = max(10, random.gauss(avg_talk_time, avg_talk_time * 0.3))

            answered_event = ProviderEvent(
                call_id=call_id,
                event_type="ANSWERED",
                provider_call_id=call_id,
                provider=self.provider_id,
                idempotency_key=f"{idem_prefix}-ANSWERED",
                event_at=now + datetime.timedelta(seconds=ring_delay_total),
            )
            completed_event = ProviderEvent(
                call_id=call_id,
                event_type="COMPLETED",
                provider_call_id=call_id,
                provider=self.provider_id,
                idempotency_key=f"{idem_prefix}-COMPLETED",
                event_at=now + datetime.timedelta(seconds=ring_delay_total + talk_time),
            )

            # Out-of-order: send COMPLETED before ANSWERED ~5% of the time
            if random.random() < self.OUT_OF_ORDER_RATE:
                events.append(completed_event)
                events.append(answered_event)
            else:
                events.append(answered_event)
                events.append(completed_event)

            # Duplicate ANSWERED event ~10%
            if random.random() < self.DUPLICATE_EVENT_RATE:
                events.append(ProviderEvent(
                    call_id=call_id,
                    event_type="ANSWERED",
                    provider_call_id=call_id,
                    provider=self.provider_id,
                    idempotency_key=f"{idem_prefix}-ANSWERED",  # same key
                    event_at=now + datetime.timedelta(seconds=ring_delay_total + 2),
                ))
        else:
            events.append(ProviderEvent(
                call_id=call_id,
                event_type="FAILED",
                provider_call_id=call_id,
                provider=self.provider_id,
                idempotency_key=f"{idem_prefix}-FAILED",
                event_at=now + datetime.timedelta(seconds=ringing_delay + 30),
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
            "timeouts": self._timeout_count,
            "failure_rate": (self._failure_count / max(self._call_count, 1)),
        }
