"""
Call Manager: implements the full call state machine with:
- Idempotent event processing
- Out-of-order event handling
- Duplicate event deduplication
- Terminal state protection
"""
import uuid
import logging
import datetime
from typing import Optional, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import text

from . import models
from .models import CallStatus, AgentStatus

logger = logging.getLogger(__name__)

# Terminal states — once reached, no further transitions are allowed
TERMINAL_STATES = {CallStatus.COMPLETED, CallStatus.FAILED, CallStatus.CANCELLED}

# Valid call state transitions
CALL_TRANSITIONS = {
    CallStatus.QUEUED:    {CallStatus.RESERVED, CallStatus.CANCELLED},
    CallStatus.RESERVED:  {CallStatus.INITIATED, CallStatus.FAILED, CallStatus.CANCELLED},
    CallStatus.INITIATED: {CallStatus.RINGING, CallStatus.FAILED, CallStatus.CANCELLED},
    CallStatus.RINGING:   {CallStatus.ANSWERED, CallStatus.FAILED, CallStatus.CANCELLED},
    CallStatus.ANSWERED:  {CallStatus.CONNECTED, CallStatus.COMPLETED, CallStatus.FAILED},
    CallStatus.CONNECTED: {CallStatus.COMPLETED, CallStatus.FAILED},
    CallStatus.COMPLETED: set(),
    CallStatus.FAILED:    set(),
    CallStatus.CANCELLED: set(),
}

# Priority of states — higher number = later in lifecycle
STATE_PRIORITY = {
    CallStatus.QUEUED:    0,
    CallStatus.RESERVED:  1,
    CallStatus.INITIATED: 2,
    CallStatus.RINGING:   3,
    CallStatus.ANSWERED:  4,
    CallStatus.CONNECTED: 5,
    CallStatus.COMPLETED: 6,
    CallStatus.FAILED:    6,
    CallStatus.CANCELLED: 6,
}


def create_call(db: Session, account_id: str, borrower_id: str,
                campaign_id: Optional[str], agent_id: Optional[str],
                vendor_id: Optional[str], dialer_mode: str,
                idempotency_key: Optional[str] = None) -> models.Call:
    """Create a new call in QUEUED state."""
    if idempotency_key:
        # Idempotent: return existing call if key already exists
        existing = db.query(models.Call).filter_by(
            idempotency_key=idempotency_key
        ).first()
        if existing:
            logger.debug(f"Idempotent call: returning existing call {existing.id}")
            return existing

    call_id = f"LIVE-{uuid.uuid4().hex[:12].upper()}"
    call = models.Call(
        id=call_id,
        account_id=account_id,
        borrower_id=borrower_id,
        agent_id=agent_id,
        campaign_id=campaign_id,
        vendor_id=vendor_id,
        direction="OUTBOUND",
        status=CallStatus.QUEUED,
        dialer_mode=dialer_mode,
        idempotency_key=idempotency_key or str(uuid.uuid4()),
        created_at=datetime.datetime.utcnow(),
    )
    db.add(call)
    db.commit()
    db.refresh(call)
    logger.debug(f"Created call {call_id}")
    return call


def transition_call(db: Session, call_id: str, new_status: CallStatus,
                    agent_id: Optional[str] = None,
                    provider_call_id: Optional[str] = None,
                    error_message: Optional[str] = None) -> Optional[models.Call]:
    """
    Transition a call to a new state.
    - Enforces valid transitions.
    - Protects terminal states (COMPLETED/FAILED/CANCELLED) from overwrite.
    - Handles out-of-order events via STATE_PRIORITY.
    """
    call = db.query(models.Call).filter_by(id=call_id).first()
    if call is None:
        logger.warning(f"transition_call: call {call_id} not found")
        return None

    current = call.status

    # PROTECTION: Never overwrite terminal states
    if current in TERMINAL_STATES:
        logger.warning(
            f"Ignoring late event: call {call_id} already in terminal state {current}"
        )
        return call

    # Out-of-order detection: if new event has lower priority than current, skip it
    if STATE_PRIORITY.get(new_status, 0) < STATE_PRIORITY.get(current, 0):
        logger.warning(
            f"Out-of-order event: call {call_id} is {current}, got {new_status} — ignoring"
        )
        return call

    # Validate transition
    if new_status not in CALL_TRANSITIONS.get(current, set()):
        logger.warning(
            f"Invalid call transition {current} → {new_status} for {call_id}"
        )
        return None

    now = datetime.datetime.utcnow()
    updates: Dict[str, Any] = {
        "status": new_status,
        "version": call.version + 1,
        "updated_at": now,
    }

    if new_status == CallStatus.INITIATED:
        updates["initiated_at"] = now
    elif new_status == CallStatus.ANSWERED:
        updates["answered_at"] = now
    elif new_status in (CallStatus.COMPLETED, CallStatus.FAILED):
        updates["completed_at"] = now
        if call.answered_at:
            updates["duration_sec"] = int((now - call.answered_at).total_seconds())

    if agent_id:
        updates["agent_id"] = agent_id
    if provider_call_id:
        updates["provider_call_id"] = provider_call_id
    if error_message:
        updates["error_message"] = error_message

    for k, v in updates.items():
        setattr(call, k, v)

    db.commit()
    db.refresh(call)
    logger.debug(f"Call {call_id}: {current} → {new_status}")
    return call


def process_provider_event(db: Session, call_id: str, event_type: str,
                           event_at: datetime.datetime, provider: str,
                           idempotency_key: Optional[str] = None,
                           raw_payload: Optional[Dict] = None) -> dict:
    """
    Process an incoming provider event with full deduplication and out-of-order handling.
    
    Steps:
    1. Check idempotency: reject duplicate events.
    2. Log the raw event.
    3. Attempt state transition.
    4. Return result.
    """
    import json

    # Step 1: Deduplication
    if idempotency_key:
        existing_event = db.query(models.CallEvent).filter_by(
            idempotency_key=idempotency_key
        ).first()
        if existing_event:
            return {"status": "DUPLICATE", "event_id": existing_event.id,
                    "message": "Event already processed"}

    # Step 2: Log raw event
    event = models.CallEvent(
        call_id=call_id,
        event_type=event_type,
        event_at=event_at,
        provider=provider,
        idempotency_key=idempotency_key,
        raw_payload=json.dumps(raw_payload) if raw_payload else None,
    )
    try:
        db.add(event)
        db.flush()  # Get ID without committing
    except Exception:
        db.rollback()
        # Unique constraint violation = duplicate
        return {"status": "DUPLICATE", "message": "Duplicate idempotency key"}

    # Step 3: Map event type to call status
    status_map = {
        "RINGING":   CallStatus.RINGING,
        "ANSWERED":  CallStatus.ANSWERED,
        "CONNECTED": CallStatus.CONNECTED,
        "COMPLETED": CallStatus.COMPLETED,
        "FAILED":    CallStatus.FAILED,
        "CANCELLED": CallStatus.CANCELLED,
        "NO_ANSWER": CallStatus.FAILED,
        "BUSY":      CallStatus.FAILED,
        "TIMEOUT":   CallStatus.FAILED,
    }

    new_status = status_map.get(event_type.upper())
    if new_status:
        call = transition_call(db, call_id, new_status,
                               error_message=event_type if new_status == CallStatus.FAILED else None)
        if call is None:
            db.rollback()
            return {"status": "ERROR", "message": f"Invalid transition for event {event_type}"}

    db.commit()
    return {"status": "PROCESSED", "event_id": event.id, "call_status": new_status.value if new_status else "NO_CHANGE"}


def get_active_call_counts(db: Session) -> dict:
    """Count calls in each active state."""
    from sqlalchemy import func
    rows = db.query(models.Call.status, func.count()).group_by(models.Call.status).all()
    counts = {}
    for status, count in rows:
        k = status.value if hasattr(status, 'value') else str(status)
        counts[k] = count
    return {
        "queued": counts.get("QUEUED", 0),
        "reserved": counts.get("RESERVED", 0),
        "initiated": counts.get("INITIATED", 0),
        "ringing": counts.get("RINGING", 0),
        "answered": counts.get("ANSWERED", 0),
        "connected": counts.get("CONNECTED", 0),
        "completed": counts.get("COMPLETED", 0),
        "failed": counts.get("FAILED", 0),
        "cancelled": counts.get("CANCELLED", 0),
    }
