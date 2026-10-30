"""
Borrower Manager: eligibility checking, suppression, and reservation.
Prevents duplicate dialing of the same borrower concurrently.
"""
import logging
import datetime
from typing import Optional, List, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import text, and_

from . import models
from .data_cleaning import ELIGIBILITY_CONFIG

logger = logging.getLogger(__name__)

BORROWER_RESERVATION_TTL = 120  # seconds — borrower locked while call is active


def get_eligible_borrowers(db: Session, limit: int = 100,
                           campaign_id: Optional[str] = None) -> List[dict]:
    """
    Return eligible borrowers not currently reserved.
    Joins accounts and borrowers; filters by is_eligible=1 and no active reservation.
    """
    base_sql = """
        SELECT
            a.id AS account_id,
            a.borrower_id,
            a.outstanding_amount,
            a.dpd,
            a.risk_segment,
            b.name,
            b.phone
        FROM accounts a
        JOIN borrowers b ON a.borrower_id = b.id
        WHERE a.is_eligible = 1
          AND b.phone IS NOT NULL
          AND b.phone != ''
          AND a.borrower_id NOT IN (
              SELECT borrower_id FROM borrower_reservations
              WHERE expires_at > :now
          )
        ORDER BY a.dpd DESC, a.outstanding_amount DESC
        LIMIT :lim
    """
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    rows = db.execute(text(base_sql), {"now": now, "lim": limit}).fetchall()

    result = []
    for row in rows:
        result.append({
            "account_id": row[0],
            "borrower_id": row[1],
            "outstanding_amount": row[2],
            "dpd": row[3],
            "risk_segment": row[4],
            "name": row[5],
            "phone": row[6],
            "eligibility_reason": "ELIGIBLE",
        })
    return result


def reserve_borrower(db: Session, borrower_id: str,
                     call_id: str) -> Tuple[bool, str]:
    """
    Reserve a borrower to prevent concurrent dialing.
    Returns (True, '') on success, (False, reason) on failure.
    """
    now = datetime.datetime.utcnow()
    expires = now + datetime.timedelta(seconds=BORROWER_RESERVATION_TTL)

    # Check if already reserved
    existing = db.query(models.BorrowerReservation).filter(
        models.BorrowerReservation.borrower_id == borrower_id,
        models.BorrowerReservation.expires_at > now,
    ).first()

    if existing:
        return False, f"Borrower {borrower_id} already reserved for call {existing.call_id}"

    try:
        # Remove expired reservation if exists
        db.query(models.BorrowerReservation).filter(
            models.BorrowerReservation.borrower_id == borrower_id
        ).delete()

        reservation = models.BorrowerReservation(
            borrower_id=borrower_id,
            call_id=call_id,
            reserved_at=now,
            expires_at=expires,
        )
        db.add(reservation)
        db.commit()
        return True, ""
    except Exception as e:
        db.rollback()
        logger.error(f"reserve_borrower error {borrower_id}: {e}")
        return False, str(e)


def release_borrower(db: Session, borrower_id: str) -> bool:
    """Release a borrower reservation after call completion or failure."""
    try:
        db.query(models.BorrowerReservation).filter(
            models.BorrowerReservation.borrower_id == borrower_id
        ).delete()
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"release_borrower error {borrower_id}: {e}")
        return False


def release_expired_borrower_reservations(db: Session) -> int:
    """Clean up expired borrower reservations (called periodically)."""
    now = datetime.datetime.utcnow()
    try:
        result = db.query(models.BorrowerReservation).filter(
            models.BorrowerReservation.expires_at < now
        ).delete()
        db.commit()
        return result
    except Exception as e:
        db.rollback()
        return 0


def is_borrower_suppressed(db: Session, borrower_id: str) -> Tuple[bool, str]:
    """
    Check if a borrower is suppressed by any eligibility rule.
    Returns (is_suppressed, reason).
    """
    config = ELIGIBILITY_CONFIG

    # Check account status
    account = db.query(models.Account).filter(
        models.Account.borrower_id == borrower_id,
        models.Account.is_eligible == True,
    ).first()

    if account is None:
        return True, "No eligible account found"

    # Check active complaints
    severities = config["suppress_complaint_severity"]
    comp_types = config["suppress_complaint_types"]
    unresolved_complaint = db.query(models.HistoricalComplaint).filter(
        models.HistoricalComplaint.account_id == account.id,
        models.HistoricalComplaint.status.notin_(["RESOLVED", "CLOSED"]),
    ).filter(
        (models.HistoricalComplaint.severity.in_(severities)) |
        (models.HistoricalComplaint.complaint_type.in_(comp_types))
    ).first()
    if unresolved_complaint:
        return True, f"Unresolved complaint: {unresolved_complaint.complaint_type}"

    # Check recent payment
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(
        days=config["payment_recency_days"]
    )
    recent_payment = db.query(models.HistoricalPayment).filter(
        models.HistoricalPayment.account_id == account.id,
        models.HistoricalPayment.payment_status == "SUCCESS",
        models.HistoricalPayment.event_at >= cutoff,
    ).first()
    if recent_payment:
        return True, "Recent payment received"

    # Check active PTP
    active_ptp = db.query(models.HistoricalPTP).filter(
        models.HistoricalPTP.account_id == account.id,
        models.HistoricalPTP.status.in_(config["suppress_active_ptp_statuses"]),
    ).first()
    if active_ptp:
        return True, f"Active PTP with status {active_ptp.status}"

    return False, ""


def get_borrower_count(db: Session) -> dict:
    from sqlalchemy import text
    total = db.execute(text("SELECT COUNT(*) FROM borrowers")).scalar()
    eligible = db.execute(
        text("SELECT COUNT(DISTINCT borrower_id) FROM accounts WHERE is_eligible = 1")
    ).scalar()
    reserved = db.execute(
        text(f"SELECT COUNT(*) FROM borrower_reservations WHERE expires_at > '{datetime.datetime.utcnow().isoformat()}'")
    ).scalar()
    return {"total": total, "eligible": eligible, "reserved": reserved}
