"""
Simulation Engine: runs Progressive vs Predictive comparisons using the real dataset.
Clearly distinguishes historical statistics from simulated outcomes.
"""
import time
import math
import random
import logging
import datetime
import json
from typing import Dict, Any, Optional, List

from sqlalchemy.orm import Session
from sqlalchemy import text

from ..agent_manager import (
    get_agent_summary, set_agents_online, set_agents_offline,
    recover_expired_reservations
)
from ..borrower_manager import get_eligible_borrowers, release_expired_borrower_reservations
from ..predictive_engine import (
    compute_pacing_recommendation, get_historical_stats, PacingDecision
)
from ..safety_controller import evaluate_request, SafetyDecision
from ..allocator import allocate_and_dial, complete_call, fail_call, get_provider_health
from ..providers.provider_a import ProviderA
from ..providers.provider_b import ProviderB
from .. import models
from ..database import REPORTS_DIR

logger = logging.getLogger(__name__)

# Scenario definitions
SCENARIOS = {
    "A": {"answer_rate": 0.20, "avg_talk_time": 120.0,
          "description": "Low answer rate, short calls"},
    "B": {"answer_rate": 0.50, "avg_talk_time": 90.0,
          "description": "Medium answer rate, short calls"},
    "C": {"answer_rate": 0.70, "avg_talk_time": 180.0,
          "description": "High answer rate, long calls"},
    "D": {"answer_rate": None, "avg_talk_time": None,
          "description": "Dynamic: changing answer rate, provider failures, agent availability"},
}


def run_simulation(
    db: Session,
    scenario: str = "A",
    dialer_mode: str = "PROGRESSIVE",
    num_agents: int = 50,
    steps: int = 20,
    override_answer_rate: Optional[float] = None,
    override_talk_time: Optional[float] = None,
    campaign_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run a simulation with the specified scenario.
    Uses real borrower data from the dataset.
    Simulates call outcomes (not real calls).
    
    IMPORTANT: Simulation outcomes are SIMULATED, not historical.
    Historical data is only used for borrower lists and baseline statistics.
    """
    scenario_cfg = SCENARIOS.get(scenario, SCENARIOS["A"])

    # Resolve answer rate and talk time
    if override_answer_rate is not None:
        answer_rate = override_answer_rate
    elif scenario_cfg["answer_rate"] is not None:
        answer_rate = scenario_cfg["answer_rate"]
    else:
        # Scenario D: use historical
        from ..predictive_engine import _compute_historical_answer_rate, _compute_historical_avg_talk_time
        answer_rate = _compute_historical_answer_rate(db)

    if override_talk_time is not None:
        avg_talk_time = override_talk_time
    elif scenario_cfg["avg_talk_time"] is not None:
        avg_talk_time = scenario_cfg["avg_talk_time"]
    else:
        from ..predictive_engine import _compute_historical_avg_talk_time
        avg_talk_time = _compute_historical_avg_talk_time(db)

    # Get historical stats for reference
    hist_stats = get_historical_stats(db)

    # Set up agents
    _reset_agent_states(db)
    actual_online = set_agents_online(db, num_agents)

    # Get eligible borrowers (real dataset)
    all_borrowers = get_eligible_borrowers(db, limit=min(5000, num_agents * steps * 2))
    num_borrowers = len(all_borrowers)

    logger.info(
        f"Simulation: scenario={scenario}, mode={dialer_mode}, "
        f"agents={actual_online}, borrowers={num_borrowers}, "
        f"answer_rate={answer_rate:.2%}, talk_time={avg_talk_time}s"
    )

    # Simulation state
    total_initiated = 0
    total_answered = 0
    total_failed = 0
    total_connected = 0
    total_safety_approvals = 0
    total_safety_rejections = 0
    pacing_decisions = []
    borrower_idx = 0
    active_calls: List[Dict] = []  # {call_id, agent_id, answer_time, complete_time}
    sim_time = 0.0  # simulated seconds

    TIME_STEP = avg_talk_time / 3   # advance 1/3 of talk time per step

    for step in range(steps):
        sim_time += TIME_STEP

        # ── Resolve completed calls from previous steps ───────────────────────
        still_active = []
        for c in active_calls:
            if sim_time >= c["complete_time"]:
                total_answered += 1 if c.get("answered") else 0
                total_connected += 1 if c.get("answered") else 0
                # Release agent
                agent = db.query(models.Agent).filter_by(id=c["agent_id"]).first()
                if agent:
                    agent.status = models.AgentStatus.AVAILABLE
                    agent.current_call_id = None
                    db.commit()
            else:
                still_active.append(c)
        active_calls = still_active

        # ── Scenario D: dynamic conditions ───────────────────────────────────
        if scenario == "D":
            answer_rate, avg_talk_time = _apply_dynamic_conditions(step, steps, db)
            TIME_STEP = avg_talk_time / 3

        # ── Collect current state ─────────────────────────────────────────────
        recover_expired_reservations(db)
        agent_snap = get_agent_summary(db)
        available = agent_snap["available"]
        ringing = len([c for c in active_calls if not c.get("answered")])
        connected_now = len([c for c in active_calls if c.get("answered")])

        # ── Predictive: compute recommendation ──────────────────────────────
        if dialer_mode == "PREDICTIVE":
            pacing = compute_pacing_recommendation(
                db=db,
                available_agents=available,
                reserved_agents=agent_snap["reserved"],
                connected_calls=connected_now,
                ringing_calls=ringing,
                override_answer_rate=answer_rate,
                override_talk_time=avg_talk_time,
                provider_failure_rate=0.05,
            )
            requested = pacing.recommended_calls
            pacing_decisions.append({
                "step": step,
                "sim_time": round(sim_time, 1),
                "recommendation": requested,
                "answer_rate": round(answer_rate, 4),
                "fallback": pacing.fallback_to_progressive,
                **pacing.formula_breakdown,
            })
        else:
            # Progressive: request exactly what's available
            requested = min(available, 20)
            pacing_decisions.append({
                "step": step,
                "sim_time": round(sim_time, 1),
                "recommendation": requested,
                "answer_rate": round(answer_rate, 4),
                "mode": "PROGRESSIVE_1_TO_1",
            })

        # ── Safety Controller ─────────────────────────────────────────────────
        safety = evaluate_request(
            db=db,
            requested_calls=requested,
            mode=dialer_mode,
            answer_rate=answer_rate,
            provider_healthy=True,
        )

        if safety.decision in (SafetyDecision.APPROVED, SafetyDecision.REDUCED, SafetyDecision.FALLBACK):
            total_safety_approvals += 1
            approved = safety.approved_calls
        else:
            total_safety_rejections += 1
            approved = 0

        # ── Initiate calls ───────────────────────────────────────────────────
        to_dial = min(approved, num_borrowers - borrower_idx)

        for i in range(to_dial):
            if borrower_idx >= num_borrowers:
                break
            borrower = all_borrowers[borrower_idx]
            borrower_idx += 1

            # Reserve an agent from DB
            agent_snap_now = get_agent_summary(db)
            if agent_snap_now["available"] == 0:
                break

            agent = db.query(models.Agent).filter(
                models.Agent.status == models.AgentStatus.AVAILABLE
            ).first()
            if not agent:
                break

            # Simulate call outcome
            call_answered = random.random() < answer_rate
            talk_duration = max(10.0, random.gauss(avg_talk_time, avg_talk_time * 0.2))
            ringing_time = random.uniform(3.0, 15.0)

            if call_answered:
                answer_time = sim_time + ringing_time
                complete_time = answer_time + talk_duration
                active_calls.append({
                    "agent_id": agent.id,
                    "answer_time": answer_time,
                    "complete_time": complete_time,
                    "answered": True,
                })
            else:
                complete_time = sim_time + ringing_time + 30.0
                active_calls.append({
                    "agent_id": agent.id,
                    "answer_time": None,
                    "complete_time": complete_time,
                    "answered": False,
                })
                total_failed += 1

            # Mark agent as connected (simplified for simulation)
            agent.status = models.AgentStatus.CONNECTED
            agent.current_call_id = f"SIM-{step}-{i}"
            db.commit()

            total_initiated += 1

        logger.debug(
            f"Step {step:3d}: available={available}, initiated={to_dial}, "
            f"active={len(active_calls)}, approved={approved}"
        )

    # ── Compute final metrics ─────────────────────────────────────────────────
    # Count remaining active calls as answered (in-progress)
    for c in active_calls:
        if c.get("answered"):
            total_answered += 1
            total_connected += 1

    # Reset agents
    _reset_agent_states(db)

    agent_utilization = total_answered / max(num_agents * steps * 0.1, 1)
    agent_utilization = min(agent_utilization, 1.0)

    avg_duration = avg_talk_time  # simulated

    result = {
        "scenario": scenario,
        "dialer_mode": dialer_mode,
        "answer_rate": answer_rate,
        "avg_talk_time": avg_talk_time,
        "num_agents": actual_online,
        "num_borrowers": num_borrowers,
        "steps": steps,
        "calls_initiated": total_initiated,
        "calls_answered": total_answered,
        "calls_failed": total_failed,
        "calls_connected": total_connected,
        "agent_utilization": round(min(total_answered / max(actual_online * steps, 1) * 3, 1.0), 4),
        "avg_duration": round(avg_talk_time, 2),
        "safety_approvals": total_safety_approvals,
        "safety_rejections": total_safety_rejections,
        "pacing_decisions": pacing_decisions[:10],  # Truncate for response size
        "historical_answer_rate": hist_stats["historical_answer_rate"],
        "historical_avg_talk_time": hist_stats["historical_avg_talk_time_sec"],
        "data_note": (
            "Calls initiated and outcomes are SIMULATED. "
            "Borrower list is from real dataset. "
            f"Historical answer rate: {hist_stats['historical_answer_rate']:.2%}. "
            f"Scenario uses simulated answer rate: {answer_rate:.2%}."
        ),
    }

    # Save to DB
    run = models.SimulationRun(
        scenario=scenario,
        dialer_mode=dialer_mode,
        answer_rate=answer_rate,
        avg_talk_time=avg_talk_time,
        num_agents=actual_online,
        num_borrowers=num_borrowers,
        calls_initiated=total_initiated,
        calls_answered=total_answered,
        calls_failed=total_failed,
        calls_connected=total_connected,
        agent_utilization=result["agent_utilization"],
        avg_duration=avg_talk_time,
        safety_approvals=total_safety_approvals,
        safety_rejections=total_safety_rejections,
        result_json=json.dumps(result, default=str),
        finished_at=datetime.datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    result["run_id"] = run.id

    return result


def _reset_agent_states(db: Session):
    """Return all agents to OFFLINE for a clean simulation start."""
    db.execute(text("""
        UPDATE agents SET
            status = 'OFFLINE',
            reservation_id = NULL,
            reserved_at = NULL,
            current_call_id = NULL,
            version = version + 1
    """))
    db.commit()


def _apply_dynamic_conditions(step: int, total_steps: int, db: Session):
    """
    Scenario D: dynamically change conditions over time.
    - Steps 0–4: normal (answer_rate=0.40, talk_time=120)
    - Steps 5–8: answer rate drops to 0.10 (crisis)
    - Steps 9–12: provider B injected (higher failures)
    - Steps 13+: recovery
    """
    if step < 5:
        return 0.40, 120.0
    elif step < 9:
        return 0.10, 120.0   # Answer rate crash
    elif step < 13:
        return 0.25, 90.0    # Partial recovery
    else:
        return 0.45, 120.0   # Full recovery


def get_simulation_results(db: Session) -> List[Dict[str, Any]]:
    """Retrieve all past simulation runs."""
    runs = db.query(models.SimulationRun).order_by(
        models.SimulationRun.created_at.desc()
    ).limit(50).all()

    return [
        {
            "run_id": r.id,
            "scenario": r.scenario,
            "dialer_mode": r.dialer_mode,
            "answer_rate": r.answer_rate,
            "avg_talk_time": r.avg_talk_time,
            "num_agents": r.num_agents,
            "calls_initiated": r.calls_initiated,
            "calls_answered": r.calls_answered,
            "calls_failed": r.calls_failed,
            "agent_utilization": r.agent_utilization,
            "safety_approvals": r.safety_approvals,
            "safety_rejections": r.safety_rejections,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in runs
    ]
