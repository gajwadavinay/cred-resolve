"""
SmartDialer FastAPI Application
Main entry point with all endpoints.
"""
import logging
import datetime
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text
import pathlib

from .database import engine, Base, get_db, get_session, PROCESSED_DIR
from . import models  # noqa — must be imported to register all table metadata
from . import schemas
from .data_ingestion import run_full_ingestion
from .data_cleaning import compute_account_eligibility, run_data_profiling
from .agent_manager import get_agent_summary, recover_expired_reservations
from .borrower_manager import get_eligible_borrowers, get_borrower_count
from .call_manager import get_active_call_counts
from .predictive_engine import (
    compute_pacing_recommendation, get_historical_stats, _compute_historical_answer_rate
)
from .safety_controller import evaluate_request, SafetyDecision
from .progressive_dialer import run_progressive_cycle
from .allocator import get_provider_health, allocate_and_dial
from .analytics import get_analytics_summary
from .simulation.simulator import run_simulation, get_simulation_results

# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# CREATE TABLES ON STARTUP
# ──────────────────────────────────────────────────────────────────────────────
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="SmartDialer API",
    description=(
        "A complete collections dialer prototype with Progressive and Predictive modes. "
        "Built with FastAPI, SQLite, and the real collections_30k dataset."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Serve frontend ─────────────────────────────────────────────────────────────
_FRONTEND_DIR = pathlib.Path(__file__).parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

@app.get("/", include_in_schema=False)
def root_redirect():
    """Redirect root to the dashboard UI."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/ui/index.html")


# ──────────────────────────────────────────────────────────────────────────────
# HEALTH
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/health", response_model=schemas.HealthResponse, tags=["Health"])
def health(db: Session = Depends(get_db)):
    """Check system health and data import status."""
    try:
        db.execute(text("SELECT 1"))
        db_connected = True
    except Exception:
        db_connected = False

    agents_loaded = db.execute(text("SELECT COUNT(*) FROM agents")).scalar() or 0
    borrowers_loaded = db.execute(text("SELECT COUNT(*) FROM borrowers")).scalar() or 0
    hist_calls = db.execute(text("SELECT COUNT(*) FROM historical_calls")).scalar() or 0

    return schemas.HealthResponse(
        status="healthy" if db_connected else "degraded",
        db_connected=db_connected,
        data_imported=(hist_calls > 0),
        agents_loaded=agents_loaded,
        borrowers_loaded=borrowers_loaded,
        historical_calls=hist_calls,
    )


# ──────────────────────────────────────────────────────────────────────────────
# DATA IMPORT
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/data/import", tags=["Data"])
def import_data(background_tasks: BackgroundTasks):
    """
    Trigger full data ingestion from raw CSVs into SQLite.
    Idempotent — safe to run multiple times.
    Runs synchronously and returns a summary.
    """
    try:
        summary = run_full_ingestion()
        # Compute eligibility after import
        db = get_session()
        try:
            eligibility = compute_account_eligibility(db)
        finally:
            db.close()

        return {
            "status": "success",
            "ingestion_summary": summary,
            "eligibility": eligibility,
            "message": "Data imported and eligibility computed successfully.",
        }
    except Exception as e:
        logger.error(f"Data import failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/data/profile", tags=["Data"])
def profile_data():
    """Run data profiling and return quality report."""
    try:
        report = run_data_profiling()
        return report
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────────────────────────────────────────
# AGENTS
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/agents", response_model=List[schemas.AgentResponse], tags=["Agents"])
def list_agents(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """List agents, optionally filtered by status."""
    query = db.query(models.Agent)
    if status:
        try:
            s = models.AgentStatus(status.upper())
            query = query.filter(models.Agent.status == s)
        except ValueError:
            raise HTTPException(400, f"Invalid status: {status}")
    agents = query.limit(limit).all()
    return agents


@app.get("/agents/summary", tags=["Agents"])
def agent_summary(db: Session = Depends(get_db)):
    """Return agent counts by status."""
    summary = get_agent_summary(db)
    recover_expired_reservations(db)
    return summary


# ──────────────────────────────────────────────────────────────────────────────
# BORROWERS
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/borrowers/eligible", tags=["Borrowers"])
def eligible_borrowers(
    limit: int = Query(20, ge=1, le=500),
    campaign_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Return eligible borrowers for dialing."""
    borrowers = get_eligible_borrowers(db, limit=limit, campaign_id=campaign_id)
    counts = get_borrower_count(db)
    return {
        "counts": counts,
        "eligible_borrowers": borrowers,
        "note": "Eligibility rules applied: account status, complaints, recent payments, active PTP.",
    }


# ──────────────────────────────────────────────────────────────────────────────
# CAMPAIGNS
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/campaigns", response_model=List[schemas.CampaignResponse], tags=["Campaigns"])
def list_campaigns(
    active_only: bool = True,
    db: Session = Depends(get_db),
):
    """List campaigns."""
    query = db.query(models.Campaign)
    if active_only:
        query = query.filter(models.Campaign.is_active == True)
    return query.limit(100).all()


# ──────────────────────────────────────────────────────────────────────────────
# DIALER
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/dialer/progressive/run", tags=["Dialer"])
def run_progressive(
    request: schemas.DialerRunRequest,
    db: Session = Depends(get_db),
):
    """
    Run one progressive dialing cycle.
    Strict 1:1 agent-to-call ratio. Every request passes through Safety Controller.
    """
    try:
        result = run_progressive_cycle(
            db=db,
            campaign_id=request.campaign_id,
            max_calls=request.max_calls,
            dry_run=request.dry_run,
        )
        return result
    except Exception as e:
        logger.error(f"Progressive dialer error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/dialer/predictive/run", tags=["Dialer"])
def run_predictive(
    request: schemas.DialerRunRequest,
    db: Session = Depends(get_db),
):
    """
    Run one predictive dialing cycle.
    Engine recommends → Safety Controller approves/reduces/rejects → Allocator dials.
    """
    try:
        from .allocator import get_provider_health
        from .borrower_manager import release_expired_borrower_reservations

        # Cleanup
        recover_expired_reservations(db)
        release_expired_borrower_reservations(db)

        # Agent state
        agent_snap = get_agent_summary(db)
        call_snap = get_active_call_counts(db)

        # Historical stats
        hist_stats = get_historical_stats(db)
        answer_rate = hist_stats["historical_answer_rate"]

        # Predictive recommendation
        import time
        start = time.time()
        pacing = compute_pacing_recommendation(
            db=db,
            available_agents=agent_snap["available"],
            reserved_agents=agent_snap["reserved"],
            connected_calls=call_snap["connected"],
            ringing_calls=call_snap["ringing"] + call_snap["initiated"],
            provider_failure_rate=0.05,
        )

        recommended = min(pacing.recommended_calls, request.max_calls)

        # Safety check
        safety = evaluate_request(
            db=db,
            requested_calls=recommended,
            mode="PREDICTIVE",
            answer_rate=answer_rate,
            provider_healthy=True,
        )

        approved = safety.approved_calls
        calls_initiated = 0
        calls_failed = 0
        errors = []

        if approved > 0:
            borrowers = get_eligible_borrowers(db, limit=approved, campaign_id=request.campaign_id)
            for borrower in borrowers[:approved]:
                result = allocate_and_dial(
                    db=db,
                    borrower_info=borrower,
                    dialer_mode="PREDICTIVE",
                    campaign_id=request.campaign_id,
                    dry_run=request.dry_run,
                )
                if result["success"]:
                    calls_initiated += 1
                else:
                    calls_failed += 1
                    errors.append(result.get("reason", ""))

        elapsed = (time.time() - start) * 1000
        return {
            "mode": "PREDICTIVE",
            "campaign_id": request.campaign_id,
            "calls_initiated": calls_initiated,
            "calls_rejected_safety": recommended - approved,
            "calls_rejected_capacity": calls_failed,
            "agent_snapshot": agent_snap,
            "pacing_recommendation": pacing.recommended_calls,
            "pacing_formula": pacing.formula_breakdown,
            "pacing_reason": pacing.reason,
            "safety_decision": safety.decision.value,
            "safety_reason": safety.reason,
            "fallback_to_progressive": pacing.fallback_to_progressive,
            "duration_ms": elapsed,
            "errors": errors,
        }
    except Exception as e:
        logger.error(f"Predictive dialer error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/dialer/status", tags=["Dialer"])
def dialer_status(db: Session = Depends(get_db)):
    """Return current dialer operational status."""
    agent_snap = get_agent_summary(db)
    call_snap = get_active_call_counts(db)
    provider_health = get_provider_health()

    active = call_snap["ringing"] + call_snap["initiated"] + call_snap["connected"]
    busy_agents = agent_snap["reserved"] + agent_snap["dialing"] + agent_snap["connected"]
    total_agents = agent_snap["total"]

    pacing_ratio = busy_agents / max(total_agents, 1)

    return {
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "active_calls": active,
        "ringing_calls": call_snap["ringing"],
        "connected_calls": call_snap["connected"],
        "available_agents": agent_snap["available"],
        "reserved_agents": agent_snap["reserved"],
        "busy_agents": busy_agents,
        "pacing_ratio": round(pacing_ratio, 4),
        "provider_health": provider_health,
        "agent_summary": agent_snap,
        "call_summary": call_snap,
    }


# ──────────────────────────────────────────────────────────────────────────────
# ANALYTICS
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/analytics/summary", tags=["Analytics"])
def analytics_summary(db: Session = Depends(get_db)):
    """Return comprehensive analytics from historical dataset."""
    return get_analytics_summary(db)


@app.get("/analytics/historical-stats", tags=["Analytics"])
def historical_stats(db: Session = Depends(get_db)):
    """Return historical statistics used for pacing decisions."""
    return get_historical_stats(db)


# ──────────────────────────────────────────────────────────────────────────────
# SIMULATION
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/simulation/run", tags=["Simulation"])
def simulation_run(
    request: schemas.SimulationRequest,
    db: Session = Depends(get_db),
):
    """
    Run a simulation scenario.
    Uses real borrower data from the dataset.
    Simulates call outcomes (not real calls).
    """
    try:
        result = run_simulation(
            db=db,
            scenario=request.scenario,
            dialer_mode=request.dialer_mode,
            num_agents=request.num_agents,
            steps=request.steps,
            override_answer_rate=request.override_answer_rate,
            override_talk_time=request.override_talk_time,
            campaign_id=request.campaign_id,
        )
        return result
    except Exception as e:
        logger.error(f"Simulation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/simulation/results", tags=["Simulation"])
def simulation_results(db: Session = Depends(get_db)):
    """List all past simulation runs."""
    return get_simulation_results(db)


@app.post("/simulation/run-all-scenarios", tags=["Simulation"])
def run_all_scenarios(
    num_agents: int = Query(default=50, ge=5, le=200),
    db: Session = Depends(get_db),
):
    """Run all scenarios (A, B, C, D) with both Progressive and Predictive modes."""
    results = []
    for scenario in ["A", "B", "C", "D"]:
        for mode in ["PROGRESSIVE", "PREDICTIVE"]:
            try:
                r = run_simulation(
                    db=db,
                    scenario=scenario,
                    dialer_mode=mode,
                    num_agents=num_agents,
                    steps=15,
                )
                results.append(r)
                logger.info(f"Completed scenario {scenario} / {mode}")
            except Exception as e:
                logger.error(f"Scenario {scenario}/{mode} failed: {e}")
                results.append({"scenario": scenario, "mode": mode, "error": str(e)})

    # Generate charts
    try:
        from .simulation.scenarios import generate_simulation_charts
        chart_paths = generate_simulation_charts(results)
    except Exception as e:
        logger.error(f"Chart generation failed: {e}")
        chart_paths = []

    return {
        "status": "complete",
        "runs": len(results),
        "chart_paths": chart_paths,
        "results": results,
        "summary": _build_comparison_summary(results),
    }


def _build_comparison_summary(results: list) -> dict:
    """Build a text summary comparing Progressive vs Predictive."""
    prog = [r for r in results if r.get("dialer_mode") == "PROGRESSIVE" and "error" not in r]
    pred = [r for r in results if r.get("dialer_mode") == "PREDICTIVE" and "error" not in r]

    def avg(lst, key):
        vals = [r[key] for r in lst if key in r and r[key] is not None]
        return sum(vals) / len(vals) if vals else 0

    return {
        "progressive": {
            "avg_calls_initiated": round(avg(prog, "calls_initiated"), 1),
            "avg_calls_answered": round(avg(prog, "calls_answered"), 1),
            "avg_agent_utilization": round(avg(prog, "agent_utilization"), 4),
            "avg_safety_approvals": round(avg(prog, "safety_approvals"), 1),
        },
        "predictive": {
            "avg_calls_initiated": round(avg(pred, "calls_initiated"), 1),
            "avg_calls_answered": round(avg(pred, "calls_answered"), 1),
            "avg_agent_utilization": round(avg(pred, "agent_utilization"), 4),
            "avg_safety_approvals": round(avg(pred, "safety_approvals"), 1),
        },
        "note": (
            "Predictive mode initiates more calls in high-answer-rate scenarios. "
            "Progressive mode provides deterministic safety. "
            "Safety Controller enforces hard limits in both modes."
        ),
    }
