"""
SmartDialer SQLAlchemy ORM Models
Covers: Agents, Calls, Borrowers, Accounts, Campaigns, and all historical data tables.
"""
import enum
import datetime
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text,
    ForeignKey, Index, UniqueConstraint, Enum as SAEnum
)
from sqlalchemy.orm import relationship
from .database import Base


# ──────────────────────────────────────────────────────────────────────────────
# ENUMERATIONS
# ──────────────────────────────────────────────────────────────────────────────

class AgentStatus(str, enum.Enum):
    OFFLINE    = "OFFLINE"
    AVAILABLE  = "AVAILABLE"
    RESERVED   = "RESERVED"
    DIALING    = "DIALING"
    CONNECTED  = "CONNECTED"
    WRAP_UP    = "WRAP_UP"
    PAUSED     = "PAUSED"


class CallStatus(str, enum.Enum):
    QUEUED     = "QUEUED"
    RESERVED   = "RESERVED"
    INITIATED  = "INITIATED"
    RINGING    = "RINGING"
    ANSWERED   = "ANSWERED"
    CONNECTED  = "CONNECTED"
    COMPLETED  = "COMPLETED"
    FAILED     = "FAILED"
    CANCELLED  = "CANCELLED"


class DialerMode(str, enum.Enum):
    PROGRESSIVE  = "PROGRESSIVE"
    PREDICTIVE   = "PREDICTIVE"


# ──────────────────────────────────────────────────────────────────────────────
# CORE OPERATIONAL TABLES
# ──────────────────────────────────────────────────────────────────────────────

class Agent(Base):
    """Live agent state. Separate from historical agents.csv data."""
    __tablename__ = "agents"

    id              = Column(String(20), primary_key=True)          # AGT0000001
    employee_code   = Column(String(20), nullable=True)
    name            = Column(String(100), nullable=True)
    vendor_id       = Column(String(20), nullable=True)
    team            = Column(String(20), nullable=True)
    status          = Column(SAEnum(AgentStatus), nullable=False, default=AgentStatus.OFFLINE)
    reservation_id  = Column(String(50), nullable=True)             # idempotency key
    reserved_at     = Column(DateTime, nullable=True)               # for expiry
    reservation_ttl = Column(Integer, default=30)                   # seconds
    current_call_id = Column(String(50), nullable=True)
    joined_at       = Column(DateTime, nullable=True)
    updated_at      = Column(DateTime, default=datetime.datetime.utcnow,
                             onupdate=datetime.datetime.utcnow)
    version         = Column(Integer, default=0)                    # optimistic lock

    calls = relationship("Call", back_populates="agent", foreign_keys="Call.agent_id")

    __table_args__ = (
        Index("idx_agent_status", "status"),
        Index("idx_agent_reservation", "reservation_id"),
    )


class Borrower(Base):
    """Borrower contact data. Loaded from borrowers.csv."""
    __tablename__ = "borrowers"

    id         = Column(String(20), primary_key=True)
    name       = Column(String(100), nullable=True)
    phone      = Column(String(20), nullable=True)
    email      = Column(String(100), nullable=True)
    city       = Column(String(100), nullable=True)
    state      = Column(String(100), nullable=True)
    created_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)
    is_suppressed = Column(Boolean, default=False)

    accounts = relationship("Account", back_populates="borrower")

    __table_args__ = (
        Index("idx_borrower_phone", "phone"),
    )


class Account(Base):
    """Loan account data. Loaded from accounts.csv."""
    __tablename__ = "accounts"

    id                 = Column(String(20), primary_key=True)
    borrower_id        = Column(String(20), ForeignKey("borrowers.id"), nullable=True)
    loan_type          = Column(String(50), nullable=True)
    principal_amount   = Column(Float, nullable=True)
    outstanding_amount = Column(Float, nullable=True)
    dpd                = Column(Integer, nullable=True)              # days past due
    risk_segment       = Column(String(20), nullable=True)
    status             = Column(String(20), nullable=True)
    opened_at          = Column(DateTime, nullable=True)
    timezone           = Column(String(50), nullable=True)
    schema_version     = Column(String(10), nullable=True)
    is_eligible        = Column(Boolean, default=True)              # computed eligibility

    borrower = relationship("Borrower", back_populates="accounts")
    calls    = relationship("Call", back_populates="account")

    __table_args__ = (
        Index("idx_account_borrower", "borrower_id"),
        Index("idx_account_status", "status"),
        Index("idx_account_eligible", "is_eligible"),
    )


class Campaign(Base):
    """Campaign definition. Loaded from campaigns.csv."""
    __tablename__ = "campaigns"

    id                 = Column(String(20), primary_key=True)
    name               = Column(String(100), nullable=True)
    channel            = Column(String(50), nullable=True)
    strategy_version   = Column(String(20), nullable=True)
    start_at           = Column(DateTime, nullable=True)
    end_at             = Column(DateTime, nullable=True)
    target_definition  = Column(String(200), nullable=True)
    is_active          = Column(Boolean, default=True)

    calls = relationship("Call", back_populates="campaign")


class Call(Base):
    """Live call state machine. Central entity for the dialer."""
    __tablename__ = "calls"

    id              = Column(String(50), primary_key=True)
    account_id      = Column(String(20), ForeignKey("accounts.id"), nullable=True)
    borrower_id     = Column(String(20), ForeignKey("borrowers.id"), nullable=True)
    agent_id        = Column(String(20), ForeignKey("agents.id"), nullable=True)
    campaign_id     = Column(String(20), ForeignKey("campaigns.id"), nullable=True)
    vendor_id       = Column(String(20), nullable=True)
    direction       = Column(String(20), default="OUTBOUND")
    status          = Column(SAEnum(CallStatus), nullable=False, default=CallStatus.QUEUED)
    dialer_mode     = Column(SAEnum(DialerMode), nullable=True)
    idempotency_key = Column(String(100), nullable=True, unique=True)
    provider_call_id= Column(String(100), nullable=True)
    duration_sec    = Column(Integer, nullable=True)
    initiated_at    = Column(DateTime, nullable=True)
    answered_at     = Column(DateTime, nullable=True)
    completed_at    = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.datetime.utcnow,
                             onupdate=datetime.datetime.utcnow)
    version         = Column(Integer, default=0)                    # optimistic lock
    error_message   = Column(Text, nullable=True)
    timezone        = Column(String(50), nullable=True)

    agent    = relationship("Agent", back_populates="calls", foreign_keys=[agent_id])
    account  = relationship("Account", back_populates="calls")
    campaign = relationship("Campaign", back_populates="calls")
    events   = relationship("CallEvent", back_populates="call")

    __table_args__ = (
        Index("idx_call_status", "status"),
        Index("idx_call_agent", "agent_id"),
        Index("idx_call_borrower", "borrower_id"),
        Index("idx_call_idempotency", "idempotency_key"),
        Index("idx_call_created", "created_at"),
    )


class CallEvent(Base):
    """Immutable event log for all call state transitions. Enables deduplication."""
    __tablename__ = "call_events"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    call_id         = Column(String(50), ForeignKey("calls.id"), nullable=False)
    event_type      = Column(String(50), nullable=False)            # RINGING, ANSWERED…
    event_at        = Column(DateTime, nullable=False)
    provider        = Column(String(50), nullable=True)
    idempotency_key = Column(String(100), nullable=True)            # dedup key from provider
    raw_payload     = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)

    call = relationship("Call", back_populates="events")

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_event_idempotency"),
        Index("idx_event_call", "call_id"),
        Index("idx_event_type", "event_type"),
    )


class BorrowerReservation(Base):
    """Prevents the same borrower from being dialed twice simultaneously."""
    __tablename__ = "borrower_reservations"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    borrower_id = Column(String(20), nullable=False, unique=True)
    call_id     = Column(String(50), nullable=True)
    reserved_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at  = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_bres_borrower", "borrower_id"),
    )


# ──────────────────────────────────────────────────────────────────────────────
# HISTORICAL DATA TABLES (read-only imports from CSVs)
# ──────────────────────────────────────────────────────────────────────────────

class HistoricalCall(Base):
    """Imported from calls.csv. Used for analytics only."""
    __tablename__ = "historical_calls"

    id          = Column(String(20), primary_key=True)
    account_id  = Column(String(20), nullable=True)
    borrower_id = Column(String(20), nullable=True)
    event_at    = Column(DateTime, nullable=True)
    agent_id    = Column(String(20), nullable=True)
    campaign_id = Column(String(20), nullable=True)
    direction   = Column(String(20), nullable=True)
    vendor_id   = Column(String(20), nullable=True)
    call_status = Column(String(30), nullable=True)
    duration_sec= Column(Float, nullable=True)
    timezone    = Column(String(50), nullable=True)

    __table_args__ = (
        Index("idx_hcall_status", "call_status"),
        Index("idx_hcall_agent", "agent_id"),
        Index("idx_hcall_event", "event_at"),
        Index("idx_hcall_vendor", "vendor_id"),
        Index("idx_hcall_campaign", "campaign_id"),
    )


class HistoricalCallAttempt(Base):
    """Imported from call_attempts.csv."""
    __tablename__ = "historical_call_attempts"

    id             = Column(String(30), primary_key=True)
    account_id     = Column(String(20), nullable=True)
    borrower_id    = Column(String(20), nullable=True)
    event_at       = Column(DateTime, nullable=True)
    call_id        = Column(String(20), nullable=True)
    agent_id       = Column(String(20), nullable=True)
    attempt_no     = Column(Integer, nullable=True)
    vendor_id      = Column(String(20), nullable=True)
    attempt_status = Column(String(30), nullable=True)

    __table_args__ = (
        Index("idx_hattempt_status", "attempt_status"),
        Index("idx_hattempt_vendor", "vendor_id"),
    )


class HistoricalDisposition(Base):
    """Imported from call_dispositions.csv."""
    __tablename__ = "historical_dispositions"

    id                 = Column(String(30), primary_key=True)
    account_id         = Column(String(20), nullable=True)
    borrower_id        = Column(String(20), nullable=True)
    event_at           = Column(DateTime, nullable=True)
    call_id            = Column(String(20), nullable=True)
    agent_id           = Column(String(20), nullable=True)
    disposition_code   = Column(String(50), nullable=True)
    disposition_version= Column(String(20), nullable=True)

    __table_args__ = (
        Index("idx_hdisp_code", "disposition_code"),
    )


class HistoricalPayment(Base):
    """Imported from payments.csv."""
    __tablename__ = "historical_payments"

    id               = Column(String(30), primary_key=True)
    account_id       = Column(String(20), nullable=True)
    borrower_id      = Column(String(20), nullable=True)
    event_at         = Column(DateTime, nullable=True)
    payment_reference= Column(String(50), nullable=True)
    amount           = Column(Float, nullable=True)
    payment_status   = Column(String(30), nullable=True)
    payment_method   = Column(String(50), nullable=True)
    provider_id      = Column(String(20), nullable=True)

    __table_args__ = (
        Index("idx_hpay_account", "account_id"),
        Index("idx_hpay_event", "event_at"),
    )


class HistoricalPTP(Base):
    """Imported from promises_to_pay.csv."""
    __tablename__ = "historical_ptps"

    id              = Column(String(30), primary_key=True)
    account_id      = Column(String(20), nullable=True)
    borrower_id     = Column(String(20), nullable=True)
    event_at        = Column(DateTime, nullable=True)
    agent_id        = Column(String(20), nullable=True)
    promised_amount = Column(Float, nullable=True)
    promised_date   = Column(DateTime, nullable=True)
    status          = Column(String(30), nullable=True)
    source          = Column(String(30), nullable=True)

    __table_args__ = (
        Index("idx_hptp_account", "account_id"),
        Index("idx_hptp_status", "status"),
    )


class HistoricalComplaint(Base):
    """Imported from complaints.csv."""
    __tablename__ = "historical_complaints"

    id              = Column(String(30), primary_key=True)
    account_id      = Column(String(20), nullable=True)
    borrower_id     = Column(String(20), nullable=True)
    event_at        = Column(DateTime, nullable=True)
    complaint_type  = Column(String(50), nullable=True)
    severity        = Column(String(20), nullable=True)
    status          = Column(String(30), nullable=True)
    source          = Column(String(30), nullable=True)
    resolution_at   = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_hcomp_account", "account_id"),
        Index("idx_hcomp_status", "status"),
    )


class HistoricalAgentSession(Base):
    """Imported from agent_sessions.csv."""
    __tablename__ = "historical_agent_sessions"

    id        = Column(String(30), primary_key=True)
    agent_id  = Column(String(20), nullable=True)
    login_at  = Column(DateTime, nullable=True)
    channel   = Column(String(30), nullable=True)
    device_id = Column(String(50), nullable=True)
    timezone  = Column(String(50), nullable=True)
    logout_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_hsession_agent", "agent_id"),
    )


class HistoricalDailyTargeting(Base):
    """Imported from daily_targeting.csv."""
    __tablename__ = "historical_daily_targeting"

    id                  = Column(String(30), primary_key=True)
    account_id          = Column(String(20), nullable=True)
    campaign_id         = Column(String(20), nullable=True)
    target_date         = Column(DateTime, nullable=True)
    priority            = Column(Integer, nullable=True)
    recommended_channel = Column(String(30), nullable=True)
    status              = Column(String(30), nullable=True)

    __table_args__ = (
        Index("idx_htarget_account", "account_id"),
        Index("idx_htarget_campaign", "campaign_id"),
    )


class HistoricalVendorTelephony(Base):
    """Imported from vendor_telephony.csv."""
    __tablename__ = "historical_vendor_telephony"

    id               = Column(String(20), primary_key=True)
    vendor_name      = Column(String(50), nullable=True)
    vendor_account_id= Column(String(50), nullable=True)
    timezone         = Column(String(50), nullable=True)
    status           = Column(String(30), nullable=True)
    schema_version   = Column(String(10), nullable=True)


class DataIngestionLog(Base):
    """Audit trail for all data ingestion runs."""
    __tablename__ = "data_ingestion_log"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    source_file = Column(String(100), nullable=False)
    table_name  = Column(String(100), nullable=False)
    rows_read   = Column(Integer, default=0)
    rows_inserted=Column(Integer, default=0)
    rows_skipped= Column(Integer, default=0)
    rows_error  = Column(Integer, default=0)
    started_at  = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status      = Column(String(20), default="RUNNING")
    notes       = Column(Text, nullable=True)


class AnalyticsCache(Base):
    """Cached analytics results for fast retrieval."""
    __tablename__ = "analytics_cache"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    cache_key  = Column(String(100), nullable=False, unique=True)
    value_json = Column(Text, nullable=False)
    computed_at= Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)


class SimulationRun(Base):
    """Records each simulation run for comparison."""
    __tablename__ = "simulation_runs"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    run_name        = Column(String(100), nullable=True)
    scenario        = Column(String(10), nullable=True)
    dialer_mode     = Column(String(20), nullable=True)
    answer_rate     = Column(Float, nullable=True)
    avg_talk_time   = Column(Float, nullable=True)
    num_agents      = Column(Integer, nullable=True)
    num_borrowers   = Column(Integer, nullable=True)
    calls_initiated = Column(Integer, default=0)
    calls_answered  = Column(Integer, default=0)
    calls_failed    = Column(Integer, default=0)
    calls_connected = Column(Integer, default=0)
    agent_utilization= Column(Float, nullable=True)
    avg_duration    = Column(Float, nullable=True)
    safety_approvals= Column(Integer, default=0)
    safety_rejections=Column(Integer, default=0)
    result_json     = Column(Text, nullable=True)
    created_at      = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at     = Column(DateTime, nullable=True)
