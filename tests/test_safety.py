"""
Tests for the Safety Controller.
"""
import pytest
from app.models import AgentStatus
from app.safety_controller import evaluate_request, SafetyDecision
from tests.conftest import create_test_agents


class TestSafetyController:

    def test_approves_safe_progressive_request(self, db):
        """Safety Controller: approves request within agent capacity."""
        create_test_agents(db, count=10, status=AgentStatus.AVAILABLE)
        result = evaluate_request(db, requested_calls=5, mode="PROGRESSIVE")
        assert result.decision in (SafetyDecision.APPROVED, SafetyDecision.REDUCED)
        assert result.approved_calls <= 10

    def test_rejects_when_no_agents(self, db):
        """Safety Controller: rejects when zero agents available."""
        create_test_agents(db, count=5, status=AgentStatus.OFFLINE)
        result = evaluate_request(db, requested_calls=5, mode="PROGRESSIVE")
        assert result.decision == SafetyDecision.REJECTED
        assert result.approved_calls == 0

    def test_reduces_excessive_request(self, db):
        """Safety Controller: reduces request that exceeds agent capacity."""
        create_test_agents(db, count=3, status=AgentStatus.AVAILABLE)
        result = evaluate_request(db, requested_calls=100, mode="PROGRESSIVE")
        assert result.decision in (SafetyDecision.APPROVED, SafetyDecision.REDUCED)
        assert result.approved_calls <= 3

    def test_rejects_when_provider_unhealthy(self, db):
        """Safety Controller: rejects all calls when provider is unhealthy."""
        create_test_agents(db, count=10, status=AgentStatus.AVAILABLE)
        result = evaluate_request(
            db, requested_calls=5, mode="PROGRESSIVE", provider_healthy=False
        )
        assert result.decision == SafetyDecision.REJECTED
        assert result.approved_calls == 0

    def test_fallback_when_answer_rate_unreliable(self, db):
        """Safety Controller: falls back to progressive for unreliable answer rate."""
        create_test_agents(db, count=10, status=AgentStatus.AVAILABLE)
        result = evaluate_request(
            db, requested_calls=50, mode="PREDICTIVE", answer_rate=0.02
        )
        assert result.decision == SafetyDecision.FALLBACK
        assert result.approved_calls <= 10

    def test_predictive_allows_more_than_available(self, db):
        """Safety Controller: predictive mode can approve > available agents."""
        create_test_agents(db, count=10, status=AgentStatus.AVAILABLE)
        # At 50% answer rate: can initiate 2x agents = 20 calls
        result = evaluate_request(
            db, requested_calls=20, mode="PREDICTIVE", answer_rate=0.50
        )
        assert result.decision in (SafetyDecision.APPROVED, SafetyDecision.REDUCED)
        assert result.approved_calls >= 10  # At least as many as available

    def test_includes_agent_snapshot(self, db):
        """Safety Controller result includes agent snapshot."""
        create_test_agents(db, count=5, status=AgentStatus.AVAILABLE)
        result = evaluate_request(db, requested_calls=3, mode="PROGRESSIVE")
        assert "available" in result.agent_snapshot
        assert result.agent_snapshot["available"] == 5

    def test_includes_call_snapshot(self, db):
        """Safety Controller result includes call snapshot."""
        create_test_agents(db, count=5, status=AgentStatus.AVAILABLE)
        result = evaluate_request(db, requested_calls=3, mode="PROGRESSIVE")
        assert "ringing" in result.call_snapshot

    def test_zero_requested_gives_zero_approved(self, db):
        """Safety Controller: zero requested → zero approved (rejected)."""
        create_test_agents(db, count=10, status=AgentStatus.AVAILABLE)
        result = evaluate_request(db, requested_calls=0, mode="PROGRESSIVE")
        assert result.approved_calls == 0
