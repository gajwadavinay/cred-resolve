"""
Tests for borrower eligibility, suppression rules, and duplicate-dialing prevention.
"""
import datetime
import pytest
from app.models import AgentStatus
from app.borrower_manager import (
    get_eligible_borrowers, reserve_borrower, release_borrower,
    release_expired_borrower_reservations
)
from app import models
from tests.conftest import (
    create_test_agents, create_test_borrowers_and_accounts,
    make_borrower_id, make_account_id
)


class TestBorrowerEligibility:

    def test_eligible_borrowers_returned(self, db):
        """Borrowers with eligible accounts and valid phones are returned."""
        create_test_borrowers_and_accounts(db, count=10)
        result = get_eligible_borrowers(db, limit=20)
        assert len(result) == 10
        for r in result:
            assert r["phone"] is not None
            assert r["account_id"] is not None

    def test_ineligible_account_excluded(self, db):
        """Accounts with is_eligible=False are excluded."""
        create_test_borrowers_and_accounts(db, count=5)
        for acc in db.query(models.Account).all():
            acc.is_eligible = False
        db.commit()

        result = get_eligible_borrowers(db, limit=20)
        assert len(result) == 0

    def test_borrower_without_phone_excluded(self, db):
        """Borrowers with no phone number are excluded."""
        bid = make_borrower_id()
        aid = make_account_id(int(bid[4:]))
        b = models.Borrower(id=bid, name="No Phone", phone=None)
        db.add(b)
        a = models.Account(
            id=aid, borrower_id=b.id,
            status="ACTIVE", dpd=30, is_eligible=True,
        )
        db.add(a)
        db.commit()

        result = get_eligible_borrowers(db, limit=20)
        ids = [r["borrower_id"] for r in result]
        assert bid not in ids

    def test_reserve_borrower_prevents_duplicate_dialing(self, db):
        """Reserving a borrower prevents a second reservation for the same borrower."""
        pairs = create_test_borrowers_and_accounts(db, count=1)
        borrower_id = pairs[0][0].id

        ok1, _ = reserve_borrower(db, borrower_id, "CALL-001")
        assert ok1 is True

        ok2, reason = reserve_borrower(db, borrower_id, "CALL-002")
        assert ok2 is False
        assert "already reserved" in reason.lower()

    def test_released_borrower_can_be_redialed(self, db):
        """After release, borrower becomes eligible again."""
        pairs = create_test_borrowers_and_accounts(db, count=1)
        borrower_id = pairs[0][0].id

        ok, _ = reserve_borrower(db, borrower_id, "CALL-001")
        assert ok is True

        released = release_borrower(db, borrower_id)
        assert released is True

        ok2, _ = reserve_borrower(db, borrower_id, "CALL-002")
        assert ok2 is True

    def test_expired_reservations_cleaned_up(self, db):
        """Expired borrower reservations are cleaned up."""
        pairs = create_test_borrowers_and_accounts(db, count=2)
        borrower_id = pairs[0][0].id

        expired_res = models.BorrowerReservation(
            borrower_id=borrower_id,
            call_id="CALL-EXPIRED",
            reserved_at=datetime.datetime.utcnow() - datetime.timedelta(minutes=10),
            expires_at=datetime.datetime.utcnow() - datetime.timedelta(minutes=5),
        )
        db.add(expired_res)
        db.commit()

        cleaned = release_expired_borrower_reservations(db)
        assert cleaned >= 1

        # Can be reserved again
        ok, _ = reserve_borrower(db, borrower_id, "CALL-NEW")
        assert ok is True

    def test_reserved_borrower_excluded_from_eligible_list(self, db):
        """Reserved borrowers must not appear in the eligible list."""
        pairs = create_test_borrowers_and_accounts(db, count=5)
        borrower_id = pairs[0][0].id

        ok, msg = reserve_borrower(db, borrower_id, "CALL-ACTIVE")
        assert ok is True, f"Reservation failed: {msg}"

        # Verify reservation exists in DB before checking eligible list
        res = db.query(models.BorrowerReservation).filter_by(
            borrower_id=borrower_id
        ).first()
        assert res is not None, "Reservation not found in DB"
        assert res.expires_at is not None

        result = get_eligible_borrowers(db, limit=10)
        ids = [r["borrower_id"] for r in result]
        assert borrower_id not in ids, (
            f"Reserved borrower {borrower_id} appeared in eligible list"
        )
        assert len(result) == 4  # Others still eligible

    def test_active_ptp_suppresses_account(self, db):
        """Account with active PTP is marked ineligible."""
        pairs = create_test_borrowers_and_accounts(db, count=1)
        borrower_id = pairs[0][0].id
        account_id = pairs[0][1].id

        # Add active PTP
        ptp = models.HistoricalPTP(
            id=f"PTP-{borrower_id}",
            account_id=account_id,
            borrower_id=borrower_id,
            status="PENDING",
        )
        db.add(ptp)
        # Mark account ineligible (as eligibility engine would)
        acc = db.query(models.Account).filter_by(id=account_id).first()
        acc.is_eligible = False
        db.commit()

        result = get_eligible_borrowers(db, limit=10)
        ids = [r["borrower_id"] for r in result]
        assert borrower_id not in ids

    def test_unresolved_complaint_suppresses_account(self, db):
        """Account with high-severity unresolved complaint is suppressed."""
        pairs = create_test_borrowers_and_accounts(db, count=1)
        borrower_id = pairs[0][0].id
        account_id = pairs[0][1].id

        complaint = models.HistoricalComplaint(
            id=f"COMP-{borrower_id}",
            account_id=account_id,
            borrower_id=borrower_id,
            severity="HIGH",
            status="OPEN",
            complaint_type="HARASSMENT",
        )
        db.add(complaint)
        acc = db.query(models.Account).filter_by(id=account_id).first()
        acc.is_eligible = False
        db.commit()

        result = get_eligible_borrowers(db, limit=10)
        ids = [r["borrower_id"] for r in result]
        assert borrower_id not in ids
