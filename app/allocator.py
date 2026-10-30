"""
Call Allocator: the coordinator between the dialer, safety controller, and providers.
Orchestrates: borrower selection → agent reservation → safety check → provider call.
"""
import uuid
import logging
import datetime
from typing import Optional, List, Dict, Any, Tuple

from sqlalchemy.orm import Session

from . import models
from .models import AgentStatus, CallStatus, DialerMode
from .agent_manager import reserve_any_available_agent, release_agent, transition_agent
from .borrower_manager import reserve_borrower, release_borrower, get_eligible_borrowers
from .call_manager import create_call, transition_call
from .providers.base import BaseProvider, ProviderCallResult
from .providers.provider_a import ProviderA
from .providers.provider_b import ProviderB

logger = logging.getLogger(__name__)

# Provider registry — shared singletons
_provider_a = ProviderA()
_provider_b = ProviderB()
PROVIDERS: Dict[str, BaseProvider] = {
    _provider_a.provider_id: _provider_a,
    _provider_b.provider_id: _provider_b,
}


def get_provider(prefer_healthy: bool = True) -> Optional[BaseProvider]:
    """Select a provider. Prefer Provider A; fall back to B if A is unhealthy."""
    if _provider_a.is_healthy():
        return _provider_a
    if prefer_healthy and _provider_b.is_healthy():
        return _provider_b
    return None


def get_all_providers() -> Dict[str, BaseProvider]:
    return PROVIDERS


def allocate_and_dial(
    db: Session,
    borrower_info: dict,
    dialer_mode: str,
    campaign_id: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Full allocation flow for a single call:
    1. Select provider (health check).
    2. Reserve borrower (dedup).
    3. Reserve agent (atomic).
    4. Create call record.
    5. Initiate call via provider.
    6. Transition call state.
    7. On any failure: release all reservations.
    
    Returns a result dict with success/failure details.
    """
    borrower_id = borrower_info["borrower_id"]
    account_id = borrower_info["account_id"]
    phone = borrower_info.get("phone", "")

    # ── 1. Provider selection ─────────────────────────────────────────────────
    provider = get_provider()
    if provider is None:
        return {"success": False, "reason": "No healthy provider available"}

    # ── 2. Reserve borrower ───────────────────────────────────────────────────
    call_id_placeholder = f"ALLOC-{uuid.uuid4().hex[:8]}"
    ok, reason = reserve_borrower(db, borrower_id, call_id_placeholder)
    if not ok:
        return {"success": False, "reason": f"Borrower reservation failed: {reason}"}

    # ── 3. Reserve agent ──────────────────────────────────────────────────────
    agent, res_id = reserve_any_available_agent(db)
    if agent is None:
        release_borrower(db, borrower_id)
        return {"success": False, "reason": f"Agent reservation failed: {res_id}"}

    agent_id = agent.id

    # ── 4. Create call record ─────────────────────────────────────────────────
    idempotency_key = f"{borrower_id}-{account_id}-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
    call = create_call(
        db=db,
        account_id=account_id,
        borrower_id=borrower_id,
        campaign_id=campaign_id,
        agent_id=agent_id,
        vendor_id=provider.provider_id,
        dialer_mode=dialer_mode,
        idempotency_key=idempotency_key,
    )

    # Transition to RESERVED
    transition_call(db, call.id, CallStatus.RESERVED)

    if dry_run:
        # In dry_run mode, don't actually place the call
        release_agent(db, agent_id)
        release_borrower(db, borrower_id)
        transition_call(db, call.id, CallStatus.CANCELLED)
        return {
            "success": True,
            "dry_run": True,
            "call_id": call.id,
            "agent_id": agent_id,
            "borrower_id": borrower_id,
        }

    # ── 5. Transition agent to DIALING ────────────────────────────────────────
    transition_agent(db, agent_id, AgentStatus.DIALING, call_id=call.id)
    transition_call(db, call.id, CallStatus.INITIATED)

    # ── 6. Initiate call via provider ─────────────────────────────────────────
    result: ProviderCallResult = provider.initiate_call(
        phone=phone,
        agent_id=agent_id,
        metadata={"call_id": call.id, "campaign_id": campaign_id},
    )

    if not result.success:
        # Provider rejected: release everything
        logger.warning(f"Provider {provider.provider_id} rejected call for {borrower_id}: {result.error}")
        release_agent(db, agent_id, to_status=AgentStatus.AVAILABLE)
        release_borrower(db, borrower_id)
        transition_call(db, call.id, CallStatus.FAILED,
                       error_message=result.error)
        return {
            "success": False,
            "reason": f"Provider failed: {result.error}",
            "call_id": call.id,
            "agent_id": agent_id,
        }

    # ── 7. Success: update call with provider ID ──────────────────────────────
    transition_call(db, call.id, CallStatus.RINGING,
                   provider_call_id=result.provider_call_id)
    transition_agent(db, agent_id, AgentStatus.CONNECTED, call_id=call.id)
    transition_call(db, call.id, CallStatus.CONNECTED)

    logger.info(
        f"Call {call.id} initiated: agent={agent_id}, "
        f"borrower={borrower_id}, provider={provider.provider_id}"
    )

    return {
        "success": True,
        "call_id": call.id,
        "agent_id": agent_id,
        "borrower_id": borrower_id,
        "provider_call_id": result.provider_call_id,
        "provider": provider.provider_id,
        "latency_ms": result.latency_ms,
    }


def complete_call(db: Session, call_id: str, agent_id: str) -> bool:
    """Mark a call as completed and return agent to AVAILABLE."""
    transition_call(db, call_id, CallStatus.COMPLETED)
    transition_agent(db, agent_id, AgentStatus.WRAP_UP)
    # In a real system, wrap-up would last a configurable time.
    # For simulation: immediately release.
    release_agent(db, agent_id, to_status=AgentStatus.AVAILABLE)
    # Release borrower reservation
    call = db.query(models.Call).filter_by(id=call_id).first()
    if call:
        release_borrower(db, call.borrower_id)
    return True


def fail_call(db: Session, call_id: str, agent_id: str, reason: str = "") -> bool:
    """Mark a call as failed and return agent to AVAILABLE."""
    call = db.query(models.Call).filter_by(id=call_id).first()
    transition_call(db, call_id, CallStatus.FAILED, error_message=reason)
    release_agent(db, agent_id, to_status=AgentStatus.AVAILABLE)
    if call:
        release_borrower(db, call.borrower_id)
    return True


def get_provider_health() -> Dict[str, Any]:
    """Return health status of all providers."""
    return {pid: p.get_stats() for pid, p in PROVIDERS.items()}
