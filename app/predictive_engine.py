"""
Predictive Pacing Engine

IMPORTANT: This engine only RECOMMENDS how many calls to dial.
It NEVER places calls. All calls must go through the Safety Controller.

Formula (Erlang-inspired):
  Given:
    A = available_agents
    R = reserved_agents (agents already being assigned a call)
    C = connected_calls (agents on live calls)
    G = ringing_calls (calls in-flight, not yet answered)
    AR = answer_rate (0.0–1.0), smoothed over recent window
    ATT = average_talk_time (seconds)
    FPR = provider_failure_rate (0.0–1.0)
    PL = provider_latency (seconds, time to ring)

  Expected agents becoming available per time unit:
    agent_turnover = C * (1 / ATT)

  Effective capacity available for new calls:
    effective_available = max(0, A - R)

  How many calls can we initiate such that, given answer_rate,
  we don't exceed agent capacity when they answer?
    
    If AR > 0:
      max_calls = floor(effective_available / AR)
    else:
      max_calls = 0  # can't predict — fall back to progressive

  Adjust for provider failure:
    adjusted = ceil(max_calls / (1 - FPR))  # need extra calls to account for failures

  Subtract currently ringing (already in-flight, may answer soon):
    recommendation = max(0, adjusted - G)

  The recommendation is then passed to the Safety Controller for approval.
"""
import math
import logging
import datetime
from typing import Dict, Any, Optional
from dataclasses import dataclass

from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Historical analytics window (used for smoothing)
SMOOTHING_WINDOW = 50   # last N calls for rolling answer rate


@dataclass
class PacingDecision:
    recommended_calls: int
    effective_available_agents: int
    answer_rate: float
    provider_failure_rate: float
    ringing_calls: int
    connected_calls: int
    fallback_to_progressive: bool
    reason: str
    formula_breakdown: Dict[str, Any]


def compute_pacing_recommendation(
    db: Session,
    available_agents: int,
    reserved_agents: int,
    connected_calls: int,
    ringing_calls: int,
    historical_answer_rate: Optional[float] = None,
    historical_avg_talk_time: Optional[float] = None,
    provider_failure_rate: float = 0.05,
    override_answer_rate: Optional[float] = None,
    override_talk_time: Optional[float] = None,
) -> PacingDecision:
    """
    Compute how many outbound calls the predictive engine recommends.
    This is a RECOMMENDATION ONLY — the Safety Controller enforces the final limit.
    
    Mathematical formula:
      1. effective_available = max(0, available_agents - reserved_agents)
      2. answer_rate = override_answer_rate OR historical_answer_rate OR 0 (→ progressive fallback)
      3. If answer_rate < 0.05: fallback to progressive (too unreliable to predict)
      4. max_calls_before_failure_adjustment = floor(effective_available / answer_rate)
      5. adjusted_for_failure = ceil(max_calls_before_failure_adjustment / (1 - provider_failure_rate))
      6. recommendation = max(0, adjusted_for_failure - ringing_calls)
    """
    # Step 0: Use overrides if provided (simulation scenarios)
    answer_rate = override_answer_rate if override_answer_rate is not None else historical_answer_rate
    avg_talk_time = override_talk_time if override_talk_time is not None else historical_avg_talk_time

    # If answer rate is unknown, fetch from historical data
    if answer_rate is None:
        answer_rate = _compute_historical_answer_rate(db)

    if avg_talk_time is None:
        avg_talk_time = _compute_historical_avg_talk_time(db)

    # Step 1: effective available agents
    effective_available = max(0, available_agents - reserved_agents)

    # Step 2: Reliability check — fall back to progressive if answer rate too low
    MIN_RELIABLE_ANSWER_RATE = 0.05
    if answer_rate < MIN_RELIABLE_ANSWER_RATE:
        logger.info(
            f"Answer rate {answer_rate:.3f} below threshold — falling back to progressive"
        )
        return PacingDecision(
            recommended_calls=effective_available,   # progressive = 1 call per agent
            effective_available_agents=effective_available,
            answer_rate=answer_rate,
            provider_failure_rate=provider_failure_rate,
            ringing_calls=ringing_calls,
            connected_calls=connected_calls,
            fallback_to_progressive=True,
            reason=f"Answer rate {answer_rate:.3f} too low — progressive fallback",
            formula_breakdown={
                "effective_available": effective_available,
                "answer_rate": answer_rate,
                "fallback": True,
            }
        )

    # Step 3: Core formula
    # How many calls can we initiate such that, at the current answer rate,
    # we expect exactly effective_available agents to be needed?
    #   calls_needed * answer_rate ≤ effective_available
    #   calls_needed ≤ effective_available / answer_rate
    max_calls_ideal = math.floor(effective_available / answer_rate)

    # Step 4: Adjust upward to account for provider failures
    # We need more attempts because some will fail before ringing
    safe_failure_rate = min(provider_failure_rate, 0.90)  # cap at 90% failure rate
    adjusted = math.ceil(max_calls_ideal / max(0.1, 1.0 - safe_failure_rate))

    # Step 5: Subtract calls already in flight (ringing = already initiated, may answer)
    #  Those calls will consume agents when answered — account for them
    in_flight_agent_demand = math.ceil(ringing_calls * answer_rate)
    recommendation = max(0, adjusted - ringing_calls)

    formula_breakdown = {
        "available_agents": available_agents,
        "reserved_agents": reserved_agents,
        "effective_available": effective_available,
        "answer_rate": round(answer_rate, 4),
        "max_calls_ideal": max_calls_ideal,
        "provider_failure_rate": round(provider_failure_rate, 4),
        "adjusted_for_failure": adjusted,
        "ringing_calls": ringing_calls,
        "in_flight_agent_demand": in_flight_agent_demand,
        "recommendation": recommendation,
        "avg_talk_time_sec": round(avg_talk_time or 0, 1),
    }

    reason = (
        f"Predictive: {effective_available} effective agents / "
        f"{answer_rate:.1%} answer rate = {max_calls_ideal} ideal, "
        f"adjusted {adjusted} for {provider_failure_rate:.1%} failure, "
        f"minus {ringing_calls} ringing = {recommendation} recommended"
    )

    logger.info(f"Pacing decision: {reason}")

    return PacingDecision(
        recommended_calls=recommendation,
        effective_available_agents=effective_available,
        answer_rate=answer_rate,
        provider_failure_rate=provider_failure_rate,
        ringing_calls=ringing_calls,
        connected_calls=connected_calls,
        fallback_to_progressive=False,
        reason=reason,
        formula_breakdown=formula_breakdown,
    )


def _compute_historical_answer_rate(db: Session) -> float:
    """
    Compute answer rate from historical calls data.
    Uses a rolling window of recent calls to detect trends.
    """
    try:
        # ANSWERED / COMPLETED are both 'answered' calls
        result = db.execute(text("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN call_status IN ('ANSWERED','CONNECTED','COMPLETED')
                    THEN 1 ELSE 0 END) as answered
            FROM historical_calls
            WHERE call_status IS NOT NULL
              AND call_status != ''
        """)).fetchone()

        total = result[0] or 0
        answered = result[1] or 0

        if total == 0:
            logger.warning("No historical calls found — using default answer rate 0.30")
            return 0.30

        rate = answered / total
        logger.info(f"Historical answer rate: {answered}/{total} = {rate:.3f}")
        return rate
    except Exception as e:
        logger.error(f"Error computing historical answer rate: {e}")
        return 0.30  # safe default


def _compute_historical_avg_talk_time(db: Session) -> float:
    """Compute average call duration from historical data (answered calls only)."""
    try:
        result = db.execute(text("""
            SELECT AVG(duration_sec)
            FROM historical_calls
            WHERE duration_sec IS NOT NULL
              AND duration_sec > 0
              AND call_status IN ('ANSWERED','CONNECTED','COMPLETED')
        """)).fetchone()

        avg = result[0]
        if avg is None:
            return 120.0
        return float(avg)
    except Exception as e:
        logger.error(f"Error computing avg talk time: {e}")
        return 120.0


def get_historical_stats(db: Session) -> Dict[str, Any]:
    """Return all historical statistics used for pacing decisions."""
    answer_rate = _compute_historical_answer_rate(db)
    avg_talk_time = _compute_historical_avg_talk_time(db)

    # Provider failure rates from call_attempts
    try:
        provider_stats = db.execute(text("""
            SELECT
                vendor_id,
                COUNT(*) as total,
                SUM(CASE WHEN attempt_status IN ('FAILED','TIMEOUT','ERROR')
                    THEN 1 ELSE 0 END) as failures
            FROM historical_call_attempts
            WHERE vendor_id IS NOT NULL
            GROUP BY vendor_id
        """)).fetchall()

        pstats = []
        for row in provider_stats:
            total = row[1] or 0
            failures = row[2] or 0
            pstats.append({
                "vendor_id": row[0],
                "total_attempts": total,
                "failures": failures,
                "failure_rate": failures / max(total, 1),
            })
    except Exception:
        pstats = []

    return {
        "historical_answer_rate": round(answer_rate, 4),
        "historical_avg_talk_time_sec": round(avg_talk_time, 2),
        "provider_stats": pstats,
        "data_source": "historical_calls (imported from calls.csv)",
        "disclaimer": "These are HISTORICAL statistics from the dataset, not live operational data.",
    }
