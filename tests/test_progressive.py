"""
Tests for Progressive Dialer behavior.
"""
import pytest
from app.models import AgentStatus, CallStatus
from app.agent_manager import get_agent_summary, reserve_agent, release_agent
from app.progressive_dialer import run_progressive_cycle
from tests.conftest import create_test_agents, create_test_borrowers_and_accounts, create_test_campaign


class TestProgressiveDialer:

    def test_progressive_never_exceeds_available_agents(self, db):
        """Progressive: calls initiated must never exceed available agents."""
        create_test_agents(db, count=5, status=AgentStatus.AVAILABLE)
        create_test_borrowers_and_accounts(db, count=20)
        create_test_campaign(db)

        result = run_progressive_cycle(db, max_calls=100, dry_run=True)
        assert result["calls_initiated"] <= 5, (
            f"Progressive initiated {result['calls_initiated']} calls but only 5 agents available"
        )

    def test_progressive_with_no_agents(self, db):
        """Progressive: zero calls when no agents are available."""
        create_test_agents(db, count=3, status=AgentStatus.OFFLINE)
        create_test_borrowers_and_accounts(db, count=10)

        result = run_progressive_cycle(db, max_calls=10, dry_run=True)
        assert result["calls_initiated"] == 0

    def test_progressive_with_no_eligible_borrowers(self, db):
        """Progressive: zero calls when no eligible borrowers."""
        create_test_agents(db, count=5, status=AgentStatus.AVAILABLE)
        result = run_progressive_cycle(db, max_calls=10, dry_run=True)
        assert result["calls_initiated"] == 0

    def test_progressive_safety_decision_in_result(self, db):
        """Progressive: result includes safety_decision field."""
        create_test_agents(db, count=3, status=AgentStatus.AVAILABLE)
        create_test_borrowers_and_accounts(db, count=10)

        result = run_progressive_cycle(db, max_calls=5, dry_run=True)
        assert "safety_decision" in result

    def test_progressive_handles_reserved_agents_correctly(self, db):
        """Progressive: RESERVED agents are not double-reserved."""
        agents = create_test_agents(db, count=5, status=AgentStatus.AVAILABLE)

        # Reserve 3 agents manually
        reserve_agent(db, agents[0].id)
        reserve_agent(db, agents[1].id)
        reserve_agent(db, agents[2].id)

        create_test_borrowers_and_accounts(db, count=20)
        result = run_progressive_cycle(db, max_calls=10, dry_run=True)

        # Only 2 agents remain available
        assert result["calls_initiated"] <= 2

    def test_progressive_mode_in_result(self, db):
        """Progressive result must have mode=PROGRESSIVE."""
        create_test_agents(db, count=2, status=AgentStatus.AVAILABLE)
        create_test_borrowers_and_accounts(db, count=5)
        result = run_progressive_cycle(db, max_calls=5, dry_run=True)
        assert result["mode"] == "PROGRESSIVE"

    def test_progressive_agent_snapshot_in_result(self, db):
        """Progressive result must include agent_snapshot."""
        create_test_agents(db, count=3, status=AgentStatus.AVAILABLE)
        result = run_progressive_cycle(db, max_calls=5, dry_run=True)
        assert "agent_snapshot" in result
        assert "available" in result["agent_snapshot"]
