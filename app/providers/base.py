"""
Mock Telecom Provider Interface (Base)
Defines the contract all providers must implement.
"""
import abc
import uuid
import datetime
from typing import Optional, Dict, Any


class ProviderCallResult:
    def __init__(self, success: bool, provider_call_id: Optional[str],
                 error: Optional[str] = None, latency_ms: float = 0.0):
        self.success = success
        self.provider_call_id = provider_call_id
        self.error = error
        self.latency_ms = latency_ms

    def __repr__(self):
        return (f"ProviderCallResult(success={self.success}, "
                f"id={self.provider_call_id}, error={self.error})")


class ProviderEvent:
    def __init__(self, call_id: str, event_type: str,
                 provider_call_id: str, provider: str,
                 idempotency_key: str,
                 event_at: Optional[datetime.datetime] = None):
        self.call_id = call_id
        self.event_type = event_type
        self.provider_call_id = provider_call_id
        self.provider = provider
        self.idempotency_key = idempotency_key
        self.event_at = event_at or datetime.datetime.utcnow()


class BaseProvider(abc.ABC):
    """
    Abstract base for all telecom providers.
    All providers must implement initiate_call() and simulate_call_lifecycle().
    """

    @property
    @abc.abstractmethod
    def provider_id(self) -> str:
        """Unique provider identifier."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable provider name."""

    @abc.abstractmethod
    def initiate_call(self, phone: str, agent_id: str,
                      metadata: Optional[Dict[str, Any]] = None) -> ProviderCallResult:
        """
        Initiate an outbound call.
        Returns a ProviderCallResult immediately (async in production; mocked here).
        """

    @abc.abstractmethod
    def simulate_call_lifecycle(self, phone: str, answer_rate: float,
                                avg_talk_time: float) -> list:
        """
        Simulate the events that would arrive from the provider for a call.
        Returns a list of ProviderEvent objects.
        """

    @abc.abstractmethod
    def is_healthy(self) -> bool:
        """Check provider health."""

    def get_stats(self) -> dict:
        return {"provider_id": self.provider_id, "healthy": self.is_healthy()}
