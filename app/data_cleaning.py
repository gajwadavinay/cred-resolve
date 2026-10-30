"""
Data Cleaning Module: Computes eligibility flags and builds the data profiling report.
Operates on already-imported data in SQLite; never modifies raw CSVs.
"""
import logging
import datetime
from typing import Dict, Any

import pandas as pd
from sqlalchemy import text

from .database import SessionLocal, RAW_DIR, PROCESSED_DIR
from . import models

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURABLE ELIGIBILITY RULES
# Assumption: these are the safe defaults, documented here for review.
# ──────────────────────────────────────────────────────────────────────────────

ELIGIBILITY_CONFIG = {
    # Account must be in one of these statuses to be dialed
    "eligible_account_statuses": ["ACTIVE", "OVERDUE", "DELINQUENT"],

    # Suppress accounts with unresolved complaints of these types
    "suppress_complaint_types": ["HARASSMENT", "DNC"],

    # Suppress accounts with severity >= HIGH unresolved complaint
    "suppress_complaint_severity": ["HIGH", "CRITICAL"],

    # Suppress if payment received within this many days
    "payment_recency_days": 3,

    # Suppress if an active PTP exists (status = PENDING or OPEN)
    "suppress_active_ptp_statuses": ["PENDING", "OPEN", "ACTIVE"],

    # Minimum DPD to be eligible for dialing (0 = no minimum)
    "min_dpd": 0,

    # Borrower must have a phone number
    "require_phone": True,
}


def compute_account_eligibility(db) -> Dict[str, int]:
    """
    Applies eligibility rules and updates accounts.is_eligible.
    Returns counts of eligible / ineligible accounts.
    
    Rules (all configurable via ELIGIBILITY_CONFIG):
    1. Account status must be in eligible_account_statuses.
    2. Borrower must have a phone number (if require_phone=True).
    3. No unresolved high-severity complaints.
    4. No recent payments within payment_recency_days.
    5. No active PTP (Promise to Pay).
    """
    config = ELIGIBILITY_CONFIG
    cutoff_payment = datetime.datetime.utcnow() - datetime.timedelta(
        days=config["payment_recency_days"]
    )

    # ── 1. Start: all accounts INELIGIBLE ──────────────────────────────────
    db.execute(text("UPDATE accounts SET is_eligible = 0"))
    db.commit()

    # ── 2. Eligible: account status in allowed list ────────────────────────
    statuses = ", ".join(f"'{s}'" for s in config["eligible_account_statuses"])
    db.execute(text(f"UPDATE accounts SET is_eligible = 1 WHERE status IN ({statuses})"))
    db.commit()

    # ── 3. Suppress: no phone number ──────────────────────────────────────
    if config["require_phone"]:
        db.execute(text("""
            UPDATE accounts SET is_eligible = 0
            WHERE borrower_id IN (
                SELECT id FROM borrowers
                WHERE phone IS NULL OR phone = '' OR phone = 'nan'
            )
        """))
        db.commit()

    # ── 4. Suppress: unresolved complaints ────────────────────────────────
    severities = ", ".join(f"'{s}'" for s in config["suppress_complaint_severity"])
    types_list = ", ".join(f"'{t}'" for t in config["suppress_complaint_types"])
    db.execute(text(f"""
        UPDATE accounts SET is_eligible = 0
        WHERE id IN (
            SELECT account_id FROM historical_complaints
            WHERE status NOT IN ('RESOLVED', 'CLOSED')
              AND (severity IN ({severities}) OR complaint_type IN ({types_list}))
              AND account_id IS NOT NULL
        )
    """))
    db.commit()

    # ── 5. Suppress: recent payments ──────────────────────────────────────
    db.execute(text(f"""
        UPDATE accounts SET is_eligible = 0
        WHERE id IN (
            SELECT account_id FROM historical_payments
            WHERE payment_status = 'SUCCESS'
              AND event_at >= '{cutoff_payment.strftime('%Y-%m-%d %H:%M:%S')}'
              AND account_id IS NOT NULL
        )
    """))
    db.commit()

    # ── 6. Suppress: active PTP ───────────────────────────────────────────
    ptp_statuses = ", ".join(f"'{s}'" for s in config["suppress_active_ptp_statuses"])
    db.execute(text(f"""
        UPDATE accounts SET is_eligible = 0
        WHERE id IN (
            SELECT account_id FROM historical_ptps
            WHERE status IN ({ptp_statuses})
              AND account_id IS NOT NULL
        )
    """))
    db.commit()

    eligible = db.execute(text("SELECT COUNT(*) FROM accounts WHERE is_eligible = 1")).scalar()
    ineligible = db.execute(text("SELECT COUNT(*) FROM accounts WHERE is_eligible = 0")).scalar()
    total = db.execute(text("SELECT COUNT(*) FROM accounts")).scalar()

    logger.info(f"Eligibility computed: {eligible} eligible / {total} total")
    return {"total": total, "eligible": eligible, "ineligible": ineligible}


def run_data_profiling() -> Dict[str, Any]:
    """
    Reads raw CSVs and produces a data quality report. Does not modify any data.
    """
    import os
    import json

    report = {"generated_at": datetime.datetime.utcnow().isoformat(), "tables": {}}

    files = {
        "accounts": "accounts.csv",
        "agents": "agents.csv",
        "borrowers": "borrowers.csv",
        "calls": "calls.csv",
        "call_attempts": "call_attempts.csv",
        "campaigns": "campaigns.csv",
        "call_dispositions": "call_dispositions.csv",
        "vendor_telephony": "vendor_telephony.csv",
        "promises_to_pay": "promises_to_pay.csv",
        "agent_sessions": "agent_sessions.csv",
        "daily_targeting": "daily_targeting.csv",
        "payments": "payments.csv",
        "complaints": "complaints.csv",
        "account_status_history": "account_status_history.csv",
        "sms_events": "sms_events.csv",
        "whatsapp_events": "whatsapp_events.csv",
        "field_visits": "field_visits.csv",
    }

    for name, fname in files.items():
        path = os.path.join(RAW_DIR, fname)
        if not os.path.exists(path):
            report["tables"][name] = {"error": "file not found"}
            continue
        try:
            df = pd.read_csv(path, dtype=str, keep_default_na=False, nrows=None)
            total_rows = len(df)
            dup_rows = int(df.duplicated().sum())
            missing = {col: int(df[col].isin(["", "nan", "None", "NaT"]).sum())
                       for col in df.columns}
            table_report = {
                "rows": total_rows,
                "columns": list(df.columns),
                "duplicate_rows": dup_rows,
                "missing_values": missing,
            }

            # Check for timestamp columns
            ts_cols = [c for c in df.columns if "at" in c.lower() or "date" in c.lower()]
            ts_issues = {}
            for col in ts_cols:
                bad = df[col].apply(lambda v: _is_bad_timestamp(str(v))).sum()
                if bad > 0:
                    ts_issues[col] = int(bad)
            if ts_issues:
                table_report["timestamp_issues"] = ts_issues

            # Detect timezone column
            if "timezone" in df.columns:
                table_report["timezone_distribution"] = dict(
                    df["timezone"].value_counts().head(10)
                )

            report["tables"][name] = table_report
        except Exception as e:
            report["tables"][name] = {"error": str(e)}

    # Save report
    out_path = os.path.join(PROCESSED_DIR, "data_profiling_report.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"Data profiling report saved to {out_path}")
    report["report_path"] = out_path
    return report


def _is_bad_timestamp(val: str) -> bool:
    if not val or val.strip() in ("", "nan", "None", "NaT"):
        return True
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            datetime.datetime.strptime(val.strip(), fmt)
            return False
        except ValueError:
            pass
    return True
