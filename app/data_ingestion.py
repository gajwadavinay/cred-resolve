"""
Data Ingestion Pipeline: reads raw CSVs, cleans them, and loads into SQLite.
Preserves original data — never silently deletes records.
Tracks every ingestion run in data_ingestion_log.
Uses per-row INSERT with rollback on conflict for full idempotency.
"""
import os
import logging
import datetime
from typing import Optional
import pandas as pd
import numpy as np
from sqlalchemy import text

from .database import SessionLocal, RAW_DIR
from . import models

logger = logging.getLogger(__name__)

TIMEZONE_MAP = {
    "Asia/Kolkata": "Asia/Kolkata",
    "Asia/Dubai": "Asia/Dubai",
    "UTC": "UTC",
    "IST": "Asia/Kolkata",
    "GST": "Asia/Dubai",
}


def _parse_dt(val) -> Optional[datetime.datetime]:
    if not val or str(val).strip() in ("", "nan", "NaT", "None"):
        return None
    val = str(val).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
    ):
        try:
            return datetime.datetime.strptime(val, fmt)
        except ValueError:
            continue
    return None


def _safe_float(val) -> Optional[float]:
    try:
        f = float(val)
        return None if np.isnan(f) else f
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> Optional[int]:
    try:
        f = float(val)
        return None if np.isnan(f) else int(f)
    except (ValueError, TypeError):
        return None


def _log_run(db, source_file, table_name, rows_read, rows_inserted,
             rows_skipped, rows_error, notes=""):
    log = models.DataIngestionLog(
        source_file=source_file, table_name=table_name,
        rows_read=rows_read, rows_inserted=rows_inserted,
        rows_skipped=rows_skipped, rows_error=rows_error,
        finished_at=datetime.datetime.utcnow(), status="DONE", notes=notes,
    )
    db.add(log)
    db.commit()


def _insert_one(db, obj) -> bool:
    """Try to insert one ORM object; rollback silently on any error."""
    try:
        db.add(obj)
        db.flush()
        return True
    except Exception:
        db.rollback()
        return False


# ─────────────────────────────────────────────────────────────────────────────
# LOADERS
# ─────────────────────────────────────────────────────────────────────────────

def load_vendor_telephony(db) -> dict:
    path = os.path.join(RAW_DIR, "vendor_telephony.csv")
    if not os.path.exists(path):
        return {"inserted": 0, "skipped": 0, "errors": 0}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    inserted = skipped = errors = 0
    seen = set(r[0] for r in db.query(models.HistoricalVendorTelephony.id).all())
    for _, row in df.iterrows():
        vid = str(row.get("vendor_id", "")).strip()
        if not vid or vid in seen:
            skipped += 1; continue
        seen.add(vid)
        ok = _insert_one(db, models.HistoricalVendorTelephony(
            id=vid,
            vendor_name=row.get("vendor_name") or None,
            vendor_account_id=row.get("vendor_account_id") or None,
            timezone=row.get("timezone") or None,
            status=row.get("status") or None,
            schema_version=row.get("schema_version") or None,
        ))
        if ok: inserted += 1
        else: errors += 1
    db.commit()
    _log_run(db, "vendor_telephony.csv", "historical_vendor_telephony",
             len(df), inserted, skipped, errors)
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


def load_agents(db) -> dict:
    path = os.path.join(RAW_DIR, "agents.csv")
    if not os.path.exists(path):
        return {"inserted": 0, "skipped": 0, "errors": 0}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    inserted = skipped = errors = 0
    seen = set(r[0] for r in db.query(models.Agent.id).all())
    for _, row in df.iterrows():
        aid = str(row.get("agent_id", "")).strip()
        if not aid or aid in seen:
            skipped += 1; continue
        seen.add(aid)
        raw = str(row.get("status", "OFFLINE")).strip().upper()
        status = models.AgentStatus.AVAILABLE if raw == "ACTIVE" else models.AgentStatus.OFFLINE
        ok = _insert_one(db, models.Agent(
            id=aid,
            employee_code=row.get("employee_code") or None,
            name=row.get("agent_name") or None,
            vendor_id=row.get("vendor_id") or None,
            team=row.get("team") or None,
            status=status,
            joined_at=_parse_dt(row.get("joined_at")),
            updated_at=_parse_dt(row.get("updated_at")),
        ))
        if ok: inserted += 1
        else: errors += 1
    db.commit()
    _log_run(db, "agents.csv", "agents", len(df), inserted, skipped, errors)
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


def load_borrowers(db) -> dict:
    path = os.path.join(RAW_DIR, "borrowers.csv")
    if not os.path.exists(path):
        return {"inserted": 0, "skipped": 0, "errors": 0}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    inserted = skipped = errors = 0
    seen = set(r[0] for r in db.query(models.Borrower.id).all())
    for _, row in df.iterrows():
        bid = str(row.get("borrower_id", "")).strip()
        if not bid or bid in seen:
            skipped += 1; continue
        seen.add(bid)
        ok = _insert_one(db, models.Borrower(
            id=bid,
            name=row.get("name") or None,
            phone=str(row.get("phone", "")).strip() or None,
            email=row.get("email") or None,
            city=row.get("city") or None,
            state=row.get("state") or None,
            created_at=_parse_dt(row.get("created_at")),
            updated_at=_parse_dt(row.get("updated_at")),
        ))
        if ok: inserted += 1
        else: errors += 1
    db.commit()
    _log_run(db, "borrowers.csv", "borrowers", len(df), inserted, skipped, errors)
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


def load_accounts(db) -> dict:
    path = os.path.join(RAW_DIR, "accounts.csv")
    if not os.path.exists(path):
        return {"inserted": 0, "skipped": 0, "errors": 0}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    inserted = skipped = errors = 0
    seen = set(r[0] for r in db.query(models.Account.id).all())
    for _, row in df.iterrows():
        aid = str(row.get("account_id", "")).strip()
        if not aid or aid in seen:
            skipped += 1; continue
        seen.add(aid)
        ok = _insert_one(db, models.Account(
            id=aid,
            borrower_id=row.get("borrower_id") or None,
            loan_type=row.get("loan_type") or None,
            principal_amount=_safe_float(row.get("principal_amount")),
            outstanding_amount=_safe_float(row.get("outstanding_amount")),
            dpd=_safe_int(row.get("dpd")),
            risk_segment=row.get("risk_segment") or None,
            status=str(row.get("status", "")).strip() or None,
            opened_at=_parse_dt(row.get("opened_at")),
            timezone=row.get("timezone") or None,
            schema_version=row.get("schema_version") or None,
            is_eligible=True,
        ))
        if ok: inserted += 1
        else: errors += 1
    db.commit()
    _log_run(db, "accounts.csv", "accounts", len(df), inserted, skipped, errors)
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


def load_campaigns(db) -> dict:
    path = os.path.join(RAW_DIR, "campaigns.csv")
    if not os.path.exists(path):
        return {"inserted": 0, "skipped": 0, "errors": 0}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    inserted = skipped = errors = 0
    seen = set(r[0] for r in db.query(models.Campaign.id).all())
    now = datetime.datetime.utcnow()
    for _, row in df.iterrows():
        cid = str(row.get("campaign_id", "")).strip()
        if not cid or cid in seen:
            skipped += 1; continue
        seen.add(cid)
        end_at = _parse_dt(row.get("end_at"))
        ok = _insert_one(db, models.Campaign(
            id=cid,
            name=row.get("campaign_name") or None,
            channel=row.get("channel") or None,
            strategy_version=row.get("strategy_version") or None,
            start_at=_parse_dt(row.get("start_at")),
            end_at=end_at,
            target_definition=row.get("target_definition") or None,
            is_active=(end_at is None or end_at > now),
        ))
        if ok: inserted += 1
        else: errors += 1
    db.commit()
    _log_run(db, "campaigns.csv", "campaigns", len(df), inserted, skipped, errors)
    return {"inserted": inserted, "skipped": skipped, "errors": errors}


def _bulk_load_chunked(db, path: str, model_class, id_col: str,
                       row_mapper, table_name: str, source_file: str) -> dict:
    """Generic chunked bulk loader with seen-ID deduplication."""
    if not os.path.exists(path):
        return {"inserted": 0, "skipped": 0, "errors": 0}
    total_inserted = total_skipped = total_errors = rows_read = 0
    seen = set(r[0] for r in db.query(getattr(model_class, 'id')).all())

    for chunk in pd.read_csv(path, dtype=str, keep_default_na=False, chunksize=5000):
        rows_read += len(chunk)
        batch = []
        for _, row in chunk.iterrows():
            rid = str(row.get(id_col, "")).strip()
            if not rid or rid in seen:
                total_skipped += 1; continue
            seen.add(rid)
            try:
                mapped = row_mapper(rid, row)
                if mapped:
                    batch.append(mapped)
            except Exception as e:
                logger.warning(f"{source_file} row {rid} map error: {e}")
                total_errors += 1

        if batch:
            try:
                db.bulk_insert_mappings(model_class, batch)
                db.commit()
                total_inserted += len(batch)
            except Exception as e:
                db.rollback()
                logger.warning(f"{source_file} chunk error: {e}")
                # Fall back to one-by-one
                for item in batch:
                    try:
                        db.execute(
                            text(f"INSERT OR IGNORE INTO {model_class.__tablename__} "
                                 f"({','.join(item.keys())}) VALUES ({','.join(':'+k for k in item.keys())})"),
                            item
                        )
                        total_inserted += 1
                    except Exception:
                        total_errors += 1
                db.commit()

    _log_run(db, source_file, table_name, rows_read,
             total_inserted, total_skipped, total_errors)
    return {"inserted": total_inserted, "skipped": total_skipped, "errors": total_errors}


def load_historical_calls(db) -> dict:
    def mapper(cid, row):
        return dict(
            id=cid,
            account_id=row.get("account_id") or None,
            borrower_id=row.get("borrower_id") or None,
            event_at=_parse_dt(row.get("event_at")),
            agent_id=row.get("agent_id") or None,
            campaign_id=row.get("campaign_id") or None,
            direction=row.get("direction") or None,
            vendor_id=row.get("vendor_id") or None,
            call_status=row.get("call_status") or None,
            duration_sec=_safe_float(row.get("duration_sec")),
            timezone=row.get("timezone") or None,
        )
    return _bulk_load_chunked(
        db, os.path.join(RAW_DIR, "calls.csv"),
        models.HistoricalCall, "call_id", mapper,
        "historical_calls", "calls.csv",
    )


def load_historical_call_attempts(db) -> dict:
    def mapper(aid, row):
        return dict(
            id=aid,
            account_id=row.get("account_id") or None,
            borrower_id=row.get("borrower_id") or None,
            event_at=_parse_dt(row.get("event_at")),
            call_id=row.get("call_id") or None,
            agent_id=row.get("agent_id") or None,
            attempt_no=_safe_int(row.get("attempt_no")),
            vendor_id=row.get("vendor_id") or None,
            attempt_status=row.get("attempt_status") or None,
        )
    return _bulk_load_chunked(
        db, os.path.join(RAW_DIR, "call_attempts.csv"),
        models.HistoricalCallAttempt, "attempt_id", mapper,
        "historical_call_attempts", "call_attempts.csv",
    )


def load_historical_dispositions(db) -> dict:
    def mapper(did, row):
        return dict(
            id=did,
            account_id=row.get("account_id") or None,
            borrower_id=row.get("borrower_id") or None,
            event_at=_parse_dt(row.get("event_at")),
            call_id=row.get("call_id") or None,
            agent_id=row.get("agent_id") or None,
            disposition_code=row.get("disposition_code") or None,
            disposition_version=row.get("disposition_version") or None,
        )
    return _bulk_load_chunked(
        db, os.path.join(RAW_DIR, "call_dispositions.csv"),
        models.HistoricalDisposition, "disposition_id", mapper,
        "historical_dispositions", "call_dispositions.csv",
    )


def load_historical_payments(db) -> dict:
    def mapper(pid, row):
        return dict(
            id=pid,
            account_id=row.get("account_id") or None,
            borrower_id=row.get("borrower_id") or None,
            event_at=_parse_dt(row.get("event_at")),
            payment_reference=row.get("payment_reference") or None,
            amount=_safe_float(row.get("amount")),
            payment_status=row.get("payment_status") or None,
            payment_method=row.get("payment_method") or None,
            provider_id=row.get("provider_id") or None,
        )
    return _bulk_load_chunked(
        db, os.path.join(RAW_DIR, "payments.csv"),
        models.HistoricalPayment, "payment_id", mapper,
        "historical_payments", "payments.csv",
    )


def load_historical_ptps(db) -> dict:
    def mapper(pid, row):
        return dict(
            id=pid,
            account_id=row.get("account_id") or None,
            borrower_id=row.get("borrower_id") or None,
            event_at=_parse_dt(row.get("event_at")),
            agent_id=row.get("agent_id") or None,
            promised_amount=_safe_float(row.get("promised_amount")),
            promised_date=_parse_dt(row.get("promised_date")),
            status=row.get("status") or None,
            source=row.get("source") or None,
        )
    return _bulk_load_chunked(
        db, os.path.join(RAW_DIR, "promises_to_pay.csv"),
        models.HistoricalPTP, "ptp_id", mapper,
        "historical_ptps", "promises_to_pay.csv",
    )


def load_historical_complaints(db) -> dict:
    def mapper(cid, row):
        return dict(
            id=cid,
            account_id=row.get("account_id") or None,
            borrower_id=row.get("borrower_id") or None,
            event_at=_parse_dt(row.get("event_at")),
            complaint_type=row.get("complaint_type") or None,
            severity=row.get("severity") or None,
            status=row.get("status") or None,
            source=row.get("source") or None,
            resolution_at=_parse_dt(row.get("resolution_at")),
        )
    return _bulk_load_chunked(
        db, os.path.join(RAW_DIR, "complaints.csv"),
        models.HistoricalComplaint, "complaint_id", mapper,
        "historical_complaints", "complaints.csv",
    )


def load_historical_agent_sessions(db) -> dict:
    def mapper(sid, row):
        return dict(
            id=sid,
            agent_id=row.get("agent_id") or None,
            login_at=_parse_dt(row.get("login_at")),
            channel=row.get("channel") or None,
            device_id=row.get("device_id") or None,
            timezone=row.get("timezone") or None,
            logout_at=_parse_dt(row.get("logout_at")),
        )
    return _bulk_load_chunked(
        db, os.path.join(RAW_DIR, "agent_sessions.csv"),
        models.HistoricalAgentSession, "session_id", mapper,
        "historical_agent_sessions", "agent_sessions.csv",
    )


def load_historical_daily_targeting(db) -> dict:
    def mapper(tid, row):
        return dict(
            id=tid,
            account_id=row.get("account_id") or None,
            campaign_id=row.get("campaign_id") or None,
            target_date=_parse_dt(row.get("target_date")),
            priority=_safe_int(row.get("priority")),
            recommended_channel=row.get("recommended_channel") or None,
            status=row.get("status") or None,
        )
    return _bulk_load_chunked(
        db, os.path.join(RAW_DIR, "daily_targeting.csv"),
        models.HistoricalDailyTargeting, "target_id", mapper,
        "historical_daily_targeting", "daily_targeting.csv",
    )


# ─────────────────────────────────────────────────────────────────────────────
# MASTER INGESTION FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def run_full_ingestion() -> dict:
    """
    Idempotent full data ingestion. Safe to run multiple times.
    Returns a summary dict of rows ingested per table.
    """
    db = SessionLocal()
    summary = {}
    try:
        logger.info("Starting full data ingestion…")
        loaders = [
            ("vendor_telephony", load_vendor_telephony),
            ("agents", load_agents),
            ("borrowers", load_borrowers),
            ("accounts", load_accounts),
            ("campaigns", load_campaigns),
            ("historical_calls", load_historical_calls),
            ("historical_call_attempts", load_historical_call_attempts),
            ("historical_dispositions", load_historical_dispositions),
            ("historical_payments", load_historical_payments),
            ("historical_ptps", load_historical_ptps),
            ("historical_complaints", load_historical_complaints),
            ("historical_agent_sessions", load_historical_agent_sessions),
            ("historical_daily_targeting", load_historical_daily_targeting),
        ]
        for name, loader in loaders:
            try:
                result = loader(db)
                summary[name] = result
                logger.info(f"  {name}: {result}")
            except Exception as e:
                logger.error(f"  {name} FAILED: {e}")
                summary[name] = {"error": str(e)}

        logger.info("Data ingestion complete.")
    finally:
        db.close()
    return summary
