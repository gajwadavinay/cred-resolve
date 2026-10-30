"""
Pytest configuration and shared fixtures.
Uses a fresh in-memory SQLite database for each individual test function.
"""
import pytest
import os

os.environ["SMARTDIALER_TEST_MODE"] = "1"

from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models
from app.models import AgentStatus, CallStatus

# Agent/borrower ID counters — unique across entire test session
_agent_counter = 0
_borrower_counter = 0


@pytest.fixture(scope="function")
def db():
    """
    Fresh in-memory SQLite per test. Fully isolated.
    All modules that need a DB session receive this fixture.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    @sa_event.listens_for(engine, "connect")
    def set_pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()

    # Patch the global SessionLocal so that modules using it in threads
    # also get the test engine (for concurrency tests)
    import app.database as db_module
    original_session_local = db_module.SessionLocal
    db_module.SessionLocal = Session

    yield session

    db_module.SessionLocal = original_session_local
    session.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def make_agent_id():
    global _agent_counter
    _agent_counter += 1
    return f"TAGT{_agent_counter:07d}"


def make_borrower_id():
    global _borrower_counter
    _borrower_counter += 1
    return f"TBRW{_borrower_counter:07d}"


def make_account_id(n):
    return f"TACC{n:07d}"


def create_test_agents(db, count: int = 10, status: AgentStatus = AgentStatus.AVAILABLE):
    """Helper: create N test agents with unique IDs."""
    agents = []
    for _ in range(count):
        aid = make_agent_id()
        agent = models.Agent(
            id=aid,
            name=f"Test Agent {aid}",
            status=status,
            version=0,
        )
        db.add(agent)
        agents.append(agent)
    db.commit()
    return agents


def create_test_borrowers_and_accounts(db, count: int = 20):
    """Helper: create borrowers and eligible accounts with unique IDs."""
    pairs = []
    for i in range(count):
        bid = make_borrower_id()
        aid = make_account_id(int(bid[4:]))
        b = models.Borrower(
            id=bid,
            name=f"Test Borrower {bid}",
            phone=f"9{bid[4:]}",
        )
        db.add(b)
        a = models.Account(
            id=aid,
            borrower_id=b.id,
            status="ACTIVE",
            dpd=30 + i,
            outstanding_amount=10000.0 + i * 100,
            is_eligible=True,
        )
        db.add(a)
        pairs.append((b, a))
    db.commit()
    return pairs


def create_test_campaign(db):
    """Helper: create a test campaign."""
    import uuid
    cid = f"TCMP{uuid.uuid4().hex[:8].upper()}"
    campaign = models.Campaign(
        id=cid,
        name="Test Campaign",
        channel="VOICE",
        is_active=True,
    )
    db.add(campaign)
    db.commit()
    return campaign
