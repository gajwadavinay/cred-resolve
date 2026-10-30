"""
Tests for duplicate and out-of-order event handling.
"""
import datetime
import uuid
import pytest
from app.models import CallStatus, AgentStatus
from app.call_manager import (
    create_call, transition_call, process_provider_event,
    TERMINAL_STATES, STATE_PRIORITY
)
from tests.conftest import create_test_agents, create_test_borrowers_and_accounts


def _make_call(db, suffix="001"):
    """Create a fresh call with unique idempotency key."""
    pairs = create_test_borrowers_and_accounts(db, count=1)
    borrower, account = pairs[0]
    idem = f"TEST-EVT-{uuid.uuid4().hex[:8]}-{suffix}"
    return create_call(
        db=db,
        account_id=account.id,
        borrower_id=borrower.id,
        campaign_id=None,
        agent_id=None,
        vendor_id="VND_A",
        dialer_mode="PROGRESSIVE",
        idempotency_key=idem,
    ), borrower, account


class TestEventHandling:

    def test_duplicate_answered_event_is_idempotent(self, db):
        """Duplicate ANSWERED event must not change state or create duplicate event log."""
        call, _, _ = _make_call(db)
        transition_call(db, call.id, CallStatus.RESERVED)
        transition_call(db, call.id, CallStatus.INITIATED)
        transition_call(db, call.id, CallStatus.RINGING)

        now = datetime.datetime.utcnow()
        idem_key = f"ANSWERED-DEDUP-{call.id}"

        result1 = process_provider_event(
            db=db, call_id=call.id, event_type="ANSWERED",
            event_at=now, provider="VND_A", idempotency_key=idem_key,
        )
        assert result1["status"] == "PROCESSED"

        # Second identical event
        result2 = process_provider_event(
            db=db, call_id=call.id, event_type="ANSWERED",
            event_at=now, provider="VND_A", idempotency_key=idem_key,
        )
        assert result2["status"] == "DUPLICATE"

        db.expire_all()
        refreshed = db.query(type(call)).filter_by(id=call.id).first()
        assert refreshed.status == CallStatus.ANSWERED

    def test_out_of_order_completed_before_answered_is_rejected(self, db):
        """Out-of-order: COMPLETED arriving before ANSWERED must not transition call."""
        call, _, _ = _make_call(db)
        transition_call(db, call.id, CallStatus.RESERVED)
        transition_call(db, call.id, CallStatus.INITIATED)
        transition_call(db, call.id, CallStatus.RINGING)

        # COMPLETED directly from RINGING is invalid
        result = transition_call(db, call.id, CallStatus.COMPLETED)
        assert result is None  # Rejected

        db.expire_all()
        refreshed = db.query(type(call)).filter_by(id=call.id).first()
        assert refreshed.status == CallStatus.RINGING

    def test_terminal_state_cannot_be_overwritten(self, db):
        """COMPLETED must never be overwritten by FAILED or any other event."""
        call, _, _ = _make_call(db)
        for status in [CallStatus.RESERVED, CallStatus.INITIATED,
                       CallStatus.RINGING, CallStatus.ANSWERED, CallStatus.COMPLETED]:
            transition_call(db, call.id, status)

        db.expire_all()
        call_refreshed = db.query(type(call)).filter_by(id=call.id).first()
        assert call_refreshed.status == CallStatus.COMPLETED

        # Stale FAILED event arrives
        result = transition_call(db, call.id, CallStatus.FAILED)

        db.expire_all()
        call_after = db.query(type(call)).filter_by(id=call.id).first()
        # Must still be COMPLETED
        assert call_after.status == CallStatus.COMPLETED

    def test_duplicate_ringing_with_same_idempotency_key(self, db):
        """Duplicate RINGING events from Provider B must be deduped."""
        call, _, _ = _make_call(db)
        transition_call(db, call.id, CallStatus.RESERVED)
        transition_call(db, call.id, CallStatus.INITIATED)

        idem_key = f"RINGING-PROVB-{call.id}"
        now = datetime.datetime.utcnow()

        result1 = process_provider_event(
            db=db, call_id=call.id, event_type="RINGING",
            event_at=now, provider="VND_B", idempotency_key=idem_key,
        )
        assert result1["status"] == "PROCESSED"

        result2 = process_provider_event(
            db=db, call_id=call.id, event_type="RINGING",
            event_at=now + datetime.timedelta(seconds=1),
            provider="VND_B", idempotency_key=idem_key,
        )
        assert result2["status"] == "DUPLICATE"

    def test_failed_event_terminates_call(self, db):
        """FAILED event must move call to terminal FAILED state."""
        call, _, _ = _make_call(db)
        transition_call(db, call.id, CallStatus.RESERVED)
        transition_call(db, call.id, CallStatus.INITIATED)
        transition_call(db, call.id, CallStatus.RINGING)

        idem_key = f"FAILED-EVT-{call.id}"
        result = process_provider_event(
            db=db, call_id=call.id, event_type="FAILED",
            event_at=datetime.datetime.utcnow(),
            provider="VND_B", idempotency_key=idem_key,
        )
        assert result["status"] == "PROCESSED"

        db.expire_all()
        refreshed = db.query(type(call)).filter_by(id=call.id).first()
        assert refreshed.status == CallStatus.FAILED
        assert refreshed.status in TERMINAL_STATES

    def test_state_priority_ordering(self):
        """STATE_PRIORITY must reflect correct lifecycle order."""
        assert STATE_PRIORITY[CallStatus.QUEUED] < STATE_PRIORITY[CallStatus.INITIATED]
        assert STATE_PRIORITY[CallStatus.INITIATED] < STATE_PRIORITY[CallStatus.RINGING]
        assert STATE_PRIORITY[CallStatus.RINGING] < STATE_PRIORITY[CallStatus.ANSWERED]
        assert STATE_PRIORITY[CallStatus.ANSWERED] < STATE_PRIORITY[CallStatus.COMPLETED]
