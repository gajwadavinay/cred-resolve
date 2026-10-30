"""
Pydantic schemas for SmartDialer API request/response validation.
"""
import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from .models import AgentStatus, CallStatus, DialerMode


# ──────────────────────────────────────────────────────────────────────────────
# AGENT SCHEMAS
# ──────────────────────────────────────────────────────────────────────────────

class AgentResponse(BaseModel):
    id: str
    name: Optional[str] = None
    employee_code: Optional[str] = None
    vendor_id: Optional[str] = None
    team: Optional[str] = None
    status: AgentStatus
    reservation_id: Optional[str] = None
    reserved_at: Optional[datetime.datetime] = None
    current_call_id: Optional[str] = None
    updated_at: Optional[datetime.datetime] = None

    model_config = {"from_attributes": True}


class AgentSummary(BaseModel):
    total: int
    offline: int
    available: int
    reserved: int
    dialing: int
    connected: int
    wrap_up: int
    paused: int


# ──────────────────────────────────────────────────────────────────────────────
# BORROWER SCHEMAS
# ──────────────────────────────────────────────────────────────────────────────

class EligibleBorrowerResponse(BaseModel):
    borrower_id: str
    account_id: str
    name: Optional[str] = None
    phone: Optional[str] = None
    dpd: Optional[int] = None
    risk_segment: Optional[str] = None
    outstanding_amount: Optional[float] = None
    eligibility_reason: str = "ELIGIBLE"

    model_config = {"from_attributes": True}


# ──────────────────────────────────────────────────────────────────────────────
# CAMPAIGN SCHEMAS
# ──────────────────────────────────────────────────────────────────────────────

class CampaignResponse(BaseModel):
    id: str
    name: Optional[str] = None
    channel: Optional[str] = None
    strategy_version: Optional[str] = None
    start_at: Optional[datetime.datetime] = None
    end_at: Optional[datetime.datetime] = None
    target_definition: Optional[str] = None
    is_active: bool

    model_config = {"from_attributes": True}


# ──────────────────────────────────────────────────────────────────────────────
# CALL SCHEMAS
# ──────────────────────────────────────────────────────────────────────────────

class CallResponse(BaseModel):
    id: str
    account_id: Optional[str] = None
    borrower_id: Optional[str] = None
    agent_id: Optional[str] = None
    campaign_id: Optional[str] = None
    vendor_id: Optional[str] = None
    status: CallStatus
    dialer_mode: Optional[DialerMode] = None
    duration_sec: Optional[int] = None
    initiated_at: Optional[datetime.datetime] = None
    answered_at: Optional[datetime.datetime] = None
    completed_at: Optional[datetime.datetime] = None
    created_at: Optional[datetime.datetime] = None
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}


class CallEventRequest(BaseModel):
    event_type: str
    event_at: datetime.datetime
    provider: Optional[str] = None
    idempotency_key: Optional[str] = None
    raw_payload: Optional[Dict[str, Any]] = None


# ──────────────────────────────────────────────────────────────────────────────
# DIALER SCHEMAS
# ──────────────────────────────────────────────────────────────────────────────

class DialerRunRequest(BaseModel):
    campaign_id: Optional[str] = None
    max_calls: int = Field(default=10, ge=1, le=1000)
    dry_run: bool = False


class DialerRunResponse(BaseModel):
    mode: str
    campaign_id: Optional[str] = None
    calls_initiated: int
    calls_rejected_safety: int
    calls_rejected_capacity: int
    agent_snapshot: AgentSummary
    pacing_recommendation: Optional[int] = None
    safety_decision: Optional[str] = None
    duration_ms: float
    errors: List[str] = []


class DialerStatusResponse(BaseModel):
    active_calls: int
    ringing_calls: int
    connected_calls: int
    available_agents: int
    reserved_agents: int
    busy_agents: int
    pacing_ratio: float
    mode: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────────
# SIMULATION SCHEMAS
# ──────────────────────────────────────────────────────────────────────────────

class SimulationRequest(BaseModel):
    scenario: str = Field(default="A", pattern="^[A-D]$")
    dialer_mode: str = Field(default="PROGRESSIVE")
    num_agents: int = Field(default=50, ge=1, le=500)
    override_answer_rate: Optional[float] = None    # None = use historical
    override_talk_time: Optional[float] = None      # None = use historical
    campaign_id: Optional[str] = None
    steps: int = Field(default=20, ge=5, le=200)


class SimulationResult(BaseModel):
    run_id: int
    scenario: str
    dialer_mode: str
    answer_rate: float
    avg_talk_time: float
    num_agents: int
    num_borrowers: int
    calls_initiated: int
    calls_answered: int
    calls_failed: int
    calls_connected: int
    agent_utilization: float
    avg_duration: float
    safety_approvals: int
    safety_rejections: int
    pacing_decisions: List[Dict[str, Any]]
    chart_paths: List[str] = []
    summary: str


# ──────────────────────────────────────────────────────────────────────────────
# ANALYTICS SCHEMAS
# ──────────────────────────────────────────────────────────────────────────────

class AnalyticsSummaryResponse(BaseModel):
    total_historical_calls: int
    overall_answer_rate: float
    avg_call_duration_sec: float
    total_agents: int
    active_agents: int
    total_borrowers: int
    eligible_borrowers: int
    total_campaigns: int
    active_campaigns: int
    provider_stats: List[Dict[str, Any]]
    top_dispositions: List[Dict[str, Any]]
    data_quality: Dict[str, Any]


# ──────────────────────────────────────────────────────────────────────────────
# HEALTH SCHEMA
# ──────────────────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    db_connected: bool
    data_imported: bool
    agents_loaded: int
    borrowers_loaded: int
    historical_calls: int
    version: str = "1.0.0"
