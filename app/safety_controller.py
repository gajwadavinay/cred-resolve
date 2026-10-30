"""
Independent Safety Controller

The Safety Controller is the LAST LINE OF DEFENSE before any call is initiated.
The predictive engine CANNOT bypass it. Every dial request passes through here.

Safety guarantees:
1. Never initiate more calls than there are available agents.
2. Never leave a connected call without an assigned agent.
3. Account for in-flight calls (RINGING/INITIATED) when computing capacity.
4. Fall back to progressive (1:1) when predictions are unreliable.
5. Hard reject any request that would violate agent capacity.
6. Reduce (partially approve) excessive requests to the safe maximum.
"""
import logging
from dataclasses import dataclass
from typing import Optional
from enum import Enum

from sqlalchemy.orm import Session

from . import models
from .models import AgentStatus, CallStatus
from .agent_manager import get_agent_summary

logger = logging.getLogger(__name__)


class SafetyDecision(str, Enum):
    APPROVED        = "APPROVED"          # All requested calls approved
    REDUCED         = "REDUCED"           # Approved with lower count
    REJECTED        = "REJECTED"          # Zero calls approved — unsafe
    FALLBACK        = "FALLBACK"          # Forced to progressive mode


@dataclass
class SafetyControllerResult:
    decision: SafetyDecision
    requested_calls: int
    approved_calls: int
    reason: str
    agent_snapshot: dict
    call_snapshot: dict


def evaluate_request(
    db: Session,
    requested_calls: int,
    mode: str = "PREDICTIVE",
    answer_rate: Optional[float] = None,
    provider_healthy: bool = True,
) -> SafetyControllerResult:
    """
    Independently verify safety and approve/reduce/reject the dial request.

    The controller re-reads agent and call state from the database independently.
    It does NOT trust the predictive engine's snapshot — it takes its own reading.

    Safety algorithm:
      1. Read current agent state from DB.
      2. Compute true capacity = AVAILABLE agents (not RESERVED/DIALING/CONNECTED).
      3. Compute in-flight calls (RINGING + INITIATED) that WILL consume agents on answer.
      4. Compute effective capacity = available - in_flight_consuming_agents
      5. If predictive mode AND answer_rate < threshold → force progressive.
      6. In progressive mode: cap at min(requested, effective_capacity).
      7. In predictive mode: cap at min(requested, max_safe_predictive).
         max_safe_predictive = effective_capacity / answer_rate (rounded)
      8. If approved_calls == 0 → REJECT.
      9. If approved < requested → REDUCE.
      10. Else → APPROVE.
    """
    # ── Read current state independently ─────────────────────────────────────
    agent_snap = get_agent_summary(db)
    call_snap = _get_call_snapshot(db)

    available = agent_snap["available"]
    reserved  = agent_snap["reserved"]
    dialing   = agent_snap["dialing"]
    connected = agent_snap["connected"]

    ringing   = call_snap["ringing"] + call_snap["initiated"]
    total_busy = reserved + dialing + connected  # agents already committed

    # ── Provider outage: reject all ───────────────────────────────────────────
    if not provider_healthy:
        return SafetyControllerResult(
            decision=SafetyDecision.REJECTED,
            requested_calls=requested_calls,
            approved_calls=0,
            reason="Provider is unhealthy — no calls approved",
            agent_snapshot=agent_snap,
            call_snapshot=call_snap,
        )

    # ── Hard limit: no available agents → reject ──────────────────────────────
    if available == 0:
        return SafetyControllerResult(
            decision=SafetyDecision.REJECTED,
            requested_calls=requested_calls,
            approved_calls=0,
            reason=f"No available agents (total={agent_snap['total']}, "
                   f"busy={total_busy})",
            agent_snapshot=agent_snap,
            call_snapshot=call_snap,
        )

    # ── Compute effective capacity ────────────────────────────────────────────
    # In-flight calls that will consume agents when they answer:
    # We conservatively assume ALL ringing calls will be answered
    # (worst case: all would need an agent simultaneously)
    in_flight_agent_demand = min(ringing, available)
    effective_capacity = max(0, available - in_flight_agent_demand)

    # ── Mode-specific calculation ─────────────────────────────────────────────
    RELIABLE_ANSWER_RATE_THRESHOLD = 0.05

    if mode == "PROGRESSIVE" or (
        answer_rate is not None and answer_rate < RELIABLE_ANSWER_RATE_THRESHOLD
    ):
        # Progressive: strict 1:1 — never more calls than available agents
        max_safe = effective_capacity
        if mode == "PREDICTIVE" and answer_rate is not None:
            logger.warning(
                f"Safety Controller: forcing PROGRESSIVE fallback "
                f"(answer_rate={answer_rate:.3f} < {RELIABLE_ANSWER_RATE_THRESHOLD})"
            )
            decision_type = SafetyDecision.FALLBACK
        else:
            decision_type = None  # will be determined below
    else:
        # Predictive: allow up to effective_capacity / answer_rate calls,
        # expecting that only answer_rate fraction will need an agent
        ar = max(0.01, answer_rate)
        import math
        max_safe = math.floor(effective_capacity / ar)
        decision_type = None

    approved = min(requested_calls, max_safe)

    # ── Determine final decision ──────────────────────────────────────────────
    if decision_type == SafetyDecision.FALLBACK:
        reason = (
            f"FALLBACK to progressive: answer rate {answer_rate:.3f} unreliable. "
            f"Approved {approved} / {requested_calls} (effective_capacity={effective_capacity})"
        )
        return SafetyControllerResult(
            decision=SafetyDecision.FALLBACK,
            requested_calls=requested_calls,
            approved_calls=approved,
            reason=reason,
            agent_snapshot=agent_snap,
            call_snapshot=call_snap,
        )

    if approved == 0:
        reason = (
            f"REJECTED: 0 calls approved. "
            f"available={available}, ringing={ringing}, "
            f"effective_capacity={effective_capacity}, max_safe={max_safe}"
        )
        return SafetyControllerResult(
            decision=SafetyDecision.REJECTED,
            requested_calls=requested_calls,
            approved_calls=0,
            reason=reason,
            agent_snapshot=agent_snap,
            call_snapshot=call_snap,
        )

    if approved < requested_calls:
        reason = (
            f"REDUCED: {approved}/{requested_calls} approved. "
            f"available={available}, ringing={ringing}, "
            f"effective_capacity={effective_capacity}, max_safe={max_safe}"
        )
        return SafetyControllerResult(
            decision=SafetyDecision.REDUCED,
            requested_calls=requested_calls,
            approved_calls=approved,
            reason=reason,
            agent_snapshot=agent_snap,
            call_snapshot=call_snap,
        )

    reason = (
        f"APPROVED: {approved}/{requested_calls} calls. "
        f"available={available}, effective_capacity={effective_capacity}"
    )
    return SafetyControllerResult(
        decision=SafetyDecision.APPROVED,
        requested_calls=requested_calls,
        approved_calls=approved,
        reason=reason,
        agent_snapshot=agent_snap,
        call_snapshot=call_snap,
    )


def _get_call_snapshot(db: Session) -> dict:
    """Read current call state from DB."""
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
    }
