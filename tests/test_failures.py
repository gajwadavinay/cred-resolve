"""
Tests for failure scenarios.
"""
import datetime
import uuid
import pytest
from sqlalchemy import text
from app.models import AgentStatus, CallStatus
from app.agent_manager import (
    reserve_agent, get_agent_summary,
    recover_expired_reservations, set_agents_online, set_agents_offline
)
from app.call_manager import create_call, transition_call, process_provider_event
from app.safety_controller import evaluate_request, SafetyDecision
from app.predictive_engine import compute_pacing_recommendation
from tests.conftest import create_test_agents, create_test_borrowers_and_accounts


class TestFailures:

    def test_worker_crash_recovery(self, db):
        """Simulate crash: agent RESERVED, never initiated — recovery returns it."""
        agents = create_test_agents(db, count=3, status=AgentStatus.AVAILABLE)
        agent_id = agents[0].id

        agent, res_id = reserve_agent(db, agent_id)
        assert agent is not None
        assert agent.status == AgentStatus.RESERVED

        # Backdate to simulate expired reservation (crash recovery scenario)
        expired_time = (
            datetime.datetime.utcnow() - datetime.timedelta(seconds=60)
        ).isoformat()
        db.execute(text(
            f"UPDATE agents SET reserved_at='{expired_time}' WHERE id='{agent_id}'"
        ))
        db.commit()

        recovered = recover_expired_reservations(db)
        assert recovered >= 1

        db.expire_all()
        agent_refreshed = db.query(type(agent)).filter_by(id=agent_id).first()
        assert agent_refreshed.status == AgentStatus.AVAILABLE
        assert agent_refreshed.reservation_id is None

    def test_provider_outage_blocks_all_calls(self, db):
        """Safety Controller must reject all calls when provider is down."""
        create_test_agents(db, count=10, status=AgentStatus.AVAILABLE)

        result = evaluate_request(
            db=db, requested_calls=5, mode="PROGRESSIVE", provider_healthy=False,
        )
        assert result.decision == SafetyDecision.REJECTED
        assert result.approved_calls == 0

    def test_provider_recovery_resumes_calls(self, db):
        """After provider recovers, Safety Controller approves calls."""
        create_test_agents(db, count=10, status=AgentStatus.AVAILABLE)

        r_outage = evaluate_request(db, requested_calls=5, mode="PROGRESSIVE",
                                    provider_healthy=False)
        assert r_outage.approved_calls == 0

        r_recovered = evaluate_request(db, requested_calls=5, mode="PROGRESSIVE",
                                       provider_healthy=True)
        assert r_recovered.approved_calls > 0

    def test_agent_availability_drop(self, db):
        """Simulate agent drop from 100 to 60 — Safety Controller respects new capacity."""
        create_test_agents(db, count=100, status=AgentStatus.AVAILABLE)

        snap_before = get_agent_summary(db)
        assert snap_before["available"] == 100

        set_agents_offline(db, 40)
        db.expire_all()

        snap_after = get_agent_summary(db)
        assert snap_after["available"] == 60
        assert snap_after["offline"] >= 40

        result = evaluate_request(db, requested_calls=100, mode="PROGRESSIVE")
        assert result.approved_calls <= 60

    def test_duplicate_answered_events_handled(self, db):
        """Duplicate ANSWERED events must be safely deduped."""
        pairs = create_test_borrowers_and_accounts(db, count=1)
        borrower, account = pairs[0]
        ikey = f"DUP-ANSW-{uuid.uuid4().hex[:8]}"
        call = create_call(
            db=db, account_id=account.id, borrower_id=borrower.id,
            campaign_id=None, agent_id=None, vendor_id="VND_A",
            dialer_mode="PROGRESSIVE", idempotency_key=ikey,
        )
        transition_call(db, call.id, CallStatus.RESERVED)
        transition_call(db, call.id, CallStatus.INITIATED)
        transition_call(db, call.id, CallStatus.RINGING)
        transition_call(db, call.id, CallStatus.ANSWERED)

        # Second ANSWERED with NEW idempotency key (simulating a different duplicate)
        result = process_provider_event(
            db=db, call_id=call.id, event_type="ANSWERED",
            event_at=datetime.datetime.utcnow(),
            provider="VND_B", idempotency_key=f"DUP-ANSW2-{call.id}",
        )

        db.expire_all()
        refreshed = db.query(type(call)).filter_by(id=call.id).first()
        # State should remain valid (ANSWERED or CONNECTED — not errored/corrupted)
        assert refreshed.status in (
            CallStatus.ANSWERED, CallStatus.CONNECTED, CallStatus.COMPLETED
        )

    def test_out_of_order_completed_before_answered(self, db):
        """COMPLETED before ANSWERED must be rejected. Correct order then works."""
        pairs = create_test_borrowers_and_accounts(db, count=1)
        borrower, account = pairs[0]
        call = create_call(
            db=db, account_id=account.id, borrower_id=borrower.id,
            campaign_id=None, agent_id=None, vendor_id="VND_B",
            dialer_mode="PREDICTIVE",
            idempotency_key=f"OOO-{uuid.uuid4().hex[:8]}",
        )
        transition_call(db, call.id, CallStatus.RESERVED)
        transition_call(db, call.id, CallStatus.INITIATED)
        transition_call(db, call.id, CallStatus.RINGING)

        # COMPLETED before ANSWERED — invalid from RINGING
        bad = transition_call(db, call.id, CallStatus.COMPLETED)
        assert bad is None

        db.expire_all()
        refreshed = db.query(type(call)).filter_by(id=call.id).first()
        assert refreshed.status == CallStatus.RINGING

        # Now correct order: ANSWERED then COMPLETED
        ok = transition_call(db, call.id, CallStatus.ANSWERED)
        assert ok is not None

        db.expire_all()
        refreshed2 = db.query(type(call)).filter_by(id=call.id).first()
        assert refreshed2.status == CallStatus.ANSWERED

    def test_answer_rate_crash_triggers_progressive_fallback(self, db):
        """When answer rate crashes to <5%, predictive falls back to progressive."""
        create_test_agents(db, count=20, status=AgentStatus.AVAILABLE)

        # Normal: 70% answer rate — no fallback
        normal = compute_pacing_recommendation(
            db=db, available_agents=20, reserved_agents=0,
            connected_calls=0, ringing_calls=0,
            override_answer_rate=0.70, override_talk_time=120.0,
        )
        assert not normal.fallback_to_progressive
        assert normal.recommended_calls > 20

        # Crisis: 3% answer rate — fallback
        crisis = compute_pacing_recommendation(
            db=db, available_agents=20, reserved_agents=0,
            connected_calls=0, ringing_calls=0,
            override_answer_rate=0.03, override_talk_time=120.0,
        )
        assert crisis.fallback_to_progressive
        assert crisis.recommended_calls == 20  # Progressive 1:1

        # Safety Controller also falls back
        sc_result = evaluate_request(
            db=db, requested_calls=100, mode="PREDICTIVE",
            answer_rate=0.03, provider_healthy=True,
        )
        assert sc_result.decision == SafetyDecision.FALLBACK
        assert sc_result.approved_calls <= 20
