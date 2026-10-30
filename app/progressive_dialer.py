"""
Progressive Dialer
Rule: exactly one call per available agent. Never more.
Every request goes through the Safety Controller.
"""
import time
import logging
import datetime
from typing import Optional, List, Dict, Any

from sqlalchemy.orm import Session

from .agent_manager import get_agent_summary, recover_expired_reservations
from .borrower_manager import get_eligible_borrowers, release_expired_borrower_reservations
from .safety_controller import evaluate_request, SafetyDecision
from .allocator import allocate_and_dial
from . import schemas

logger = logging.getLogger(__name__)


def run_progressive_cycle(
    db: Session,
    campaign_id: Optional[str] = None,
    max_calls: int = 10,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Execute one progressive dialing cycle.

    Algorithm:
    1. Recover expired reservations (cleanup).
    2. Get agent summary.
    3. Ask Safety Controller: how many calls can we make?
    4. Get eligible borrowers (up to approved count).
    5. For each borrower: allocate_and_dial().
    6. Return results.
    """
    start_time = time.time()

    # Step 1: Cleanup
    recover_expired_reservations(db)
    release_expired_borrower_reservations(db)

    # Step 2: Agent summary
    agent_snap = get_agent_summary(db)

    # Step 3: Safety check — progressive mode is always 1:1
    requested = min(max_calls, agent_snap["available"])
    safety_result = evaluate_request(
        db=db,
        requested_calls=requested,
        mode="PROGRESSIVE",
        answer_rate=None,  # progressive doesn't use answer rate
        provider_healthy=True,
    )

    approved = safety_result.approved_calls
    calls_initiated = 0
    calls_failed = 0
    errors = []

    if safety_result.decision == SafetyDecision.REJECTED:
        logger.info(f"Progressive: rejected — {safety_result.reason}")
        return _build_result("PROGRESSIVE", campaign_id, 0, 0,
                             safety_result, agent_snap, errors, start_time)

    # Step 4: Get eligible borrowers
    borrowers = get_eligible_borrowers(db, limit=approved, campaign_id=campaign_id)

    if not borrowers:
        logger.info("Progressive: no eligible borrowers found")
        return _build_result("PROGRESSIVE", campaign_id, 0, 0,
                             safety_result, agent_snap, errors, start_time)

    # Step 5: Dial
    for borrower in borrowers[:approved]:
        result = allocate_and_dial(
            db=db,
            borrower_info=borrower,
            dialer_mode="PROGRESSIVE",
            campaign_id=campaign_id,
            dry_run=dry_run,
        )
        if result["success"]:
            calls_initiated += 1
        else:
            calls_failed += 1
            errors.append(result.get("reason", "Unknown error"))

    logger.info(
        f"Progressive cycle: initiated={calls_initiated}, failed={calls_failed}, "
        f"approved={approved}, mode=PROGRESSIVE"
    )

    return _build_result("PROGRESSIVE", campaign_id, calls_initiated, calls_failed,
                         safety_result, agent_snap, errors, start_time)


def _build_result(mode, campaign_id, calls_initiated, calls_failed,
                  safety_result, agent_snap, errors, start_time) -> dict:
    elapsed = (time.time() - start_time) * 1000
    return {
        "mode": mode,
        "campaign_id": campaign_id,
        "calls_initiated": calls_initiated,
        "calls_rejected_safety": (safety_result.requested_calls - safety_result.approved_calls),
        "calls_rejected_capacity": calls_failed,
        "agent_snapshot": agent_snap,
        "pacing_recommendation": None,
        "safety_decision": safety_result.decision.value,
        "safety_reason": safety_result.reason,
        "duration_ms": elapsed,
        "errors": errors,
    }
