"""
Tests for concurrent agent reservation (preventing double-booking).

NOTE on SQLite concurrency:
  SQLite with StaticPool (single connection) serializes all operations.
  True multi-thread contention is tested with a file-based DB.
  This demonstrates the LOCKING LOGIC, not raw DB throughput.
"""
import threading
import pytest
import datetime
import os
import tempfile
from sqlalchemy import create_engine, event as sa_event, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models
from app.models import AgentStatus
from app.agent_manager import (
    reserve_agent, release_agent, get_agent_summary,
    recover_expired_reservations
)
from tests.conftest import create_test_agents


def _make_file_db():
    """Create a temporary file-based SQLite for real concurrency tests."""
    tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpfile.close()
    db_path = tmpfile.name

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @sa_event.listens_for(engine, "connect")
    def set_pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return engine, Session, db_path


class TestConcurrency:

    def test_optimistic_lock_prevents_double_reservation(self, db):
        """
        Core locking test: two sequential reservation attempts on the same agent.
        First succeeds, second fails (version mismatch).
        """
        agents = create_test_agents(db, count=1, status=AgentStatus.AVAILABLE)
        agent_id = agents[0].id

        # First reservation
        agent1, res1 = reserve_agent(db, agent_id)
        assert agent1 is not None

        # Second reservation on same agent — must fail (agent is now RESERVED)
        agent2, msg = reserve_agent(db, agent_id)
        assert agent2 is None
        assert "not AVAILABLE" in msg or "Race condition" in msg

    def test_concurrent_reservations_on_single_agent_file_db(self):
        """
        File-based SQLite WAL: concurrent threads compete for one agent.
        At most 1 should succeed.
        """
        engine, Session, db_path = _make_file_db()
        try:
            # Create one AVAILABLE agent
            setup_db = Session()
            setup_db.add(models.Agent(
                id="CONC-AGT-001", name="Concurrency Test Agent",
                status=AgentStatus.AVAILABLE, version=0
            ))
            setup_db.commit()
            setup_db.close()

            results = []
            lock = threading.Lock()

            def try_reserve():
                local_db = Session()
                try:
                    agent, msg = reserve_agent(local_db, "CONC-AGT-001")
                    with lock:
                        results.append(agent is not None)
                except Exception:
                    with lock:
                        results.append(False)
                finally:
                    local_db.close()

            threads = [threading.Thread(target=try_reserve) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            successful = sum(1 for r in results if r)
            assert successful <= 1, (
                f"Expected at most 1 successful reservation, got {successful}"
            )
        finally:
            engine.dispose()
            try:
                os.unlink(db_path)
            except Exception:
                pass

    def test_multiple_agents_reserved_without_duplicates(self, db):
        """
        Sequential unique reservations: each call to reserve_any_available_agent
        must pick a different agent.
        """
        agents = create_test_agents(db, count=5, status=AgentStatus.AVAILABLE)
        reserved_ids = []

        for _ in range(5):
            from app.agent_manager import reserve_any_available_agent
            agent, msg = reserve_any_available_agent(db)
            if agent:
                assert agent.id not in reserved_ids, (
                    f"Agent {agent.id} was reserved twice!"
                )
                reserved_ids.append(agent.id)

        assert len(reserved_ids) == 5

    def test_expired_reservation_is_recovered(self, db):
        """Reservation expiry: expired RESERVED agents are returned to AVAILABLE."""
        agents = create_test_agents(db, count=3, status=AgentStatus.AVAILABLE)
        agent_id = agents[0].id

        # Manually create an expired reservation
        expired_time = (
            datetime.datetime.utcnow() - datetime.timedelta(seconds=60)
        ).isoformat()
        db.execute(text(
            f"UPDATE agents SET status='RESERVED', "
            f"reservation_id='EXPIRED-RESV', reserved_at='{expired_time}' "
            f"WHERE id='{agent_id}'"
        ))
        db.commit()

        snap = get_agent_summary(db)
        assert snap["reserved"] >= 1

        recovered = recover_expired_reservations(db)
        assert recovered >= 1

        db.expire_all()
        snap2 = get_agent_summary(db)
        assert snap2["reserved"] == 0
        assert snap2["available"] >= 3
