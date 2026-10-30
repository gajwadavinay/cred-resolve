"""
Agent State Manager
Implements the full OFFLINE → AVAILABLE → RESERVED → DIALING → CONNECTED → WRAP_UP state machine.
Uses optimistic locking (version column) to prevent concurrent reservation conflicts.
"""
import uuid
import logging
import datetime
from typing import Optional, List, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import and_, text

from . import models
from .models import AgentStatus

logger = logging.getLogger(__name__)

# Valid state transitions
VALID_TRANSITIONS = {
    AgentStatus.OFFLINE:    {AgentStatus.AVAILABLE},
    AgentStatus.AVAILABLE:  {AgentStatus.RESERVED, AgentStatus.PAUSED, AgentStatus.OFFLINE},
    AgentStatus.RESERVED:   {AgentStatus.DIALING, AgentStatus.AVAILABLE, AgentStatus.OFFLINE},
    AgentStatus.DIALING:    {AgentStatus.CONNECTED, AgentStatus.AVAILABLE, AgentStatus.OFFLINE, AgentStatus.WRAP_UP},
    AgentStatus.CONNECTED:  {AgentStatus.WRAP_UP, AgentStatus.OFFLINE},
    AgentStatus.WRAP_UP:    {AgentStatus.AVAILABLE, AgentStatus.OFFLINE},
    AgentStatus.PAUSED:     {AgentStatus.AVAILABLE, AgentStatus.OFFLINE},
}

RESERVATION_TTL_SECONDS = 30   # Abandon reservation after 30 seconds of inactivity
WRAP_UP_MAX_SECONDS = 120       # Auto-release wrap-up after 2 minutes (safety net)


def is_valid_transition(from_status: AgentStatus, to_status: AgentStatus) -> bool:
    return to_status in VALID_TRANSITIONS.get(from_status, set())


# ──────────────────────────────────────────────────────────────────────────────
# RESERVATION
# ──────────────────────────────────────────────────────────────────────────────

def reserve_agent(db: Session, agent_id: str) -> Tuple[Optional[models.Agent], str]:
    """
    Atomically reserve an AVAILABLE agent using optimistic locking.
    Returns (agent, reservation_id) on success, or (None, error_msg) on failure.
    
    Optimistic locking strategy:
      1. SELECT agent WHERE id=? AND status='AVAILABLE'
      2. UPDATE WHERE id=? AND status='AVAILABLE' AND version=<current>
      3. If 0 rows updated → another worker won the race → fail gracefully.
    """
    reservation_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow()
    expires = now + datetime.timedelta(seconds=RESERVATION_TTL_SECONDS)

    try:
        # Step 1: fetch agent
        agent = db.query(models.Agent).filter(
            models.Agent.id == agent_id,
            models.Agent.status == AgentStatus.AVAILABLE,
        ).with_for_update(skip_locked=False).first()

        if agent is None:
            return None, f"Agent {agent_id} is not AVAILABLE"

        # Step 2: attempt optimistic update
        current_version = agent.version
        updated = db.execute(
            text("""
                UPDATE agents SET
                    status = :new_status,
                    reservation_id = :rid,
                    reserved_at = :now,
                    version = version + 1
                WHERE id = :aid
                  AND status = 'AVAILABLE'
                  AND version = :ver
            """),
            {
                "new_status": AgentStatus.RESERVED.value,
                "rid": reservation_id,
                "now": now.isoformat(),
                "aid": agent_id,
                "ver": current_version,
            }
        ).rowcount
        db.commit()

        if updated == 0:
            return None, f"Race condition: agent {agent_id} was taken by another worker"

        # Refresh from DB
        db.refresh(agent)
        logger.debug(f"Agent {agent_id} reserved: {reservation_id}")
        return agent, reservation_id

    except Exception as e:
        db.rollback()
        logger.error(f"reserve_agent error {agent_id}: {e}")
        return None, str(e)


def reserve_any_available_agent(db: Session) -> Tuple[Optional[models.Agent], str]:
    """Reserve the first available agent, regardless of ID."""
    # Recover expired reservations first
    recover_expired_reservations(db)

    agent = db.query(models.Agent).filter(
        models.Agent.status == AgentStatus.AVAILABLE
    ).first()

    if agent is None:
        return None, "No available agents"

    return reserve_agent(db, agent.id)


def release_agent(db: Session, agent_id: str,
                  to_status: AgentStatus = AgentStatus.AVAILABLE) -> bool:
    """Transition agent from RESERVED/DIALING/CONNECTED to target status."""
    agent = db.query(models.Agent).filter_by(id=agent_id).first()
    if agent is None:
        logger.warning(f"release_agent: agent {agent_id} not found")
        return False

    if not is_valid_transition(agent.status, to_status):
        logger.warning(
            f"release_agent: invalid transition {agent.status} → {to_status} for {agent_id}"
        )
        # Force release if agent is stuck
        if agent.status in (AgentStatus.RESERVED, AgentStatus.DIALING):
            to_status = AgentStatus.AVAILABLE
        else:
            return False

    try:
        db.execute(
            text("""
                UPDATE agents SET
                    status = :new_status,
                    reservation_id = NULL,
                    reserved_at = NULL,
                    current_call_id = NULL,
                    version = version + 1,
                    updated_at = :now
                WHERE id = :aid
            """),
            {"new_status": to_status.value, "aid": agent_id,
             "now": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}
        )
        db.commit()
        logger.debug(f"Agent {agent_id} released → {to_status}")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"release_agent error {agent_id}: {e}")
        return False


def transition_agent(db: Session, agent_id: str,
                     to_status: AgentStatus,
                     call_id: Optional[str] = None) -> bool:
    """Generic agent state transition with validation."""
    agent = db.query(models.Agent).filter_by(id=agent_id).first()
    if agent is None:
        return False

    if not is_valid_transition(agent.status, to_status):
        logger.warning(
            f"Invalid transition {agent.status} → {to_status} for agent {agent_id}"
        )
        return False

    try:
        updates = {
            "new_status": to_status.value,
            "aid": agent_id,
            "now": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }
        call_sql = ", current_call_id = :call_id" if call_id is not None else ""
        if call_id is not None:
            updates["call_id"] = call_id

        db.execute(
            text(f"""
                UPDATE agents SET
                    status = :new_status,
                    version = version + 1,
                    updated_at = :now
                    {call_sql}
                WHERE id = :aid
            """),
            updates
        )
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"transition_agent error {agent_id}: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# RECOVERY
# ──────────────────────────────────────────────────────────────────────────────

def recover_expired_reservations(db: Session) -> int:
    """
    Scan for RESERVED agents whose reservation has expired and return them to AVAILABLE.
    This handles worker crashes after agent reservation but before call initiation.
    """
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=RESERVATION_TTL_SECONDS)
    try:
        result = db.execute(
            text("""
                UPDATE agents SET
                    status = 'AVAILABLE',
                    reservation_id = NULL,
                    reserved_at = NULL,
                    current_call_id = NULL,
                    version = version + 1
                WHERE status = 'RESERVED'
                  AND reserved_at IS NOT NULL
                  AND reserved_at < :cutoff
            """),
            {"cutoff": cutoff.isoformat()}
        )
        db.commit()
        count = result.rowcount
        if count > 0:
            logger.info(f"Recovered {count} expired agent reservations")
        return count
    except Exception as e:
        db.rollback()
        logger.error(f"recover_expired_reservations error: {e}")
        return 0


def recover_stuck_wrap_up(db: Session) -> int:
    """Release agents stuck in WRAP_UP for longer than WRAP_UP_MAX_SECONDS."""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(seconds=WRAP_UP_MAX_SECONDS)
    try:
        result = db.execute(
            text("""
                UPDATE agents SET
                    status = 'AVAILABLE',
                    reservation_id = NULL,
                    reserved_at = NULL,
                    current_call_id = NULL,
                    version = version + 1
                WHERE status = 'WRAP_UP'
                  AND updated_at < :cutoff
            """),
            {"cutoff": cutoff.isoformat()}
        )
        db.commit()
        return result.rowcount
    except Exception as e:
        db.rollback()
        return 0


# ──────────────────────────────────────────────────────────────────────────────
# QUERY HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def get_agent_summary(db: Session) -> dict:
    """Return counts per status."""
    from sqlalchemy import func
    rows = db.query(models.Agent.status, func.count()).group_by(models.Agent.status).all()
    counts = {s.value: 0 for s in AgentStatus}
    for status, count in rows:
        if isinstance(status, str):
            counts[status] = count
        else:
            counts[status.value] = count

    return {
        "total": sum(counts.values()),
        "offline": counts.get("OFFLINE", 0),
        "available": counts.get("AVAILABLE", 0),
        "reserved": counts.get("RESERVED", 0),
        "dialing": counts.get("DIALING", 0),
        "connected": counts.get("CONNECTED", 0),
        "wrap_up": counts.get("WRAP_UP", 0),
        "paused": counts.get("PAUSED", 0),
    }


def set_agents_online(db: Session, count: int) -> int:
    """Bring `count` OFFLINE agents to AVAILABLE. Used in simulations."""
    agents = db.query(models.Agent).filter(
        models.Agent.status == AgentStatus.OFFLINE
    ).limit(count).all()

    for agent in agents:
        agent.status = AgentStatus.AVAILABLE
        agent.version += 1

    db.commit()
    return len(agents)


def set_agents_offline(db: Session, count: int) -> int:
    """Bring `count` AVAILABLE agents to OFFLINE. Used in simulations."""
    agents = db.query(models.Agent).filter(
        models.Agent.status == AgentStatus.AVAILABLE
    ).limit(count).all()

    for agent in agents:
        agent.status = AgentStatus.OFFLINE
        agent.version += 1

    db.commit()
    return len(agents)
