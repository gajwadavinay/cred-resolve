"""
Analytics Module: historical stats, provider performance, data quality, and campaign analytics.
Uses historical data only. Clearly labelled to distinguish from live operational data.
"""
import logging
import json
import datetime
from typing import Dict, Any, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy import text

logger = logging.getLogger(__name__)


def get_analytics_summary(db: Session) -> Dict[str, Any]:
    """Full analytics summary for the /analytics/summary endpoint."""

    total_calls = _scalar(db, "SELECT COUNT(*) FROM historical_calls")
    answered_calls = _scalar(db, """
        SELECT COUNT(*) FROM historical_calls
        WHERE call_status IN ('ANSWERED','CONNECTED','COMPLETED')
    """)
    answer_rate = (answered_calls / max(total_calls, 1)) if total_calls else 0.0

    avg_duration = _scalar(db, """
        SELECT AVG(duration_sec) FROM historical_calls
        WHERE duration_sec IS NOT NULL AND duration_sec > 0
          AND call_status IN ('ANSWERED','CONNECTED','COMPLETED')
    """) or 0.0

    total_agents = _scalar(db, "SELECT COUNT(*) FROM agents")
    active_agents = _scalar(db, "SELECT COUNT(*) FROM agents WHERE status = 'AVAILABLE'")

    total_borrowers = _scalar(db, "SELECT COUNT(*) FROM borrowers")
    eligible_borrowers = _scalar(db, """
        SELECT COUNT(DISTINCT borrower_id) FROM accounts WHERE is_eligible = 1
    """)

    total_campaigns = _scalar(db, "SELECT COUNT(*) FROM campaigns")
    active_campaigns = _scalar(db, "SELECT COUNT(*) FROM campaigns WHERE is_active = 1")

    provider_stats = get_provider_stats(db)
    top_dispositions = get_top_dispositions(db)
    data_quality = get_data_quality_summary(db)

    return {
        "data_note": "All statistics are derived from HISTORICAL imported data (not live calls).",
        "total_historical_calls": total_calls,
        "overall_answer_rate": round(answer_rate, 4),
        "avg_call_duration_sec": round(avg_duration, 2),
        "total_agents": total_agents,
        "active_agents": active_agents,
        "total_borrowers": total_borrowers,
        "eligible_borrowers": eligible_borrowers,
        "total_campaigns": total_campaigns,
        "active_campaigns": active_campaigns,
        "provider_stats": provider_stats,
        "top_dispositions": top_dispositions,
        "data_quality": data_quality,
    }


def get_provider_stats(db: Session) -> List[Dict[str, Any]]:
    """Provider performance from historical call_attempts."""
    try:
        rows = db.execute(text("""
            SELECT
                ha.vendor_id,
                vt.vendor_name,
                COUNT(*) as total_attempts,
                SUM(CASE WHEN ha.attempt_status IN ('FAILED','TIMEOUT','ERROR') THEN 1 ELSE 0 END) as failures,
                SUM(CASE WHEN ha.attempt_status = 'SUCCESS' THEN 1 ELSE 0 END) as successes
            FROM historical_call_attempts ha
            LEFT JOIN historical_vendor_telephony vt ON ha.vendor_id = vt.id
            WHERE ha.vendor_id IS NOT NULL
            GROUP BY ha.vendor_id, vt.vendor_name
            ORDER BY total_attempts DESC
        """)).fetchall()

        result = []
        for row in rows:
            total = row[2] or 0
            failures = row[3] or 0
            result.append({
                "vendor_id": row[0],
                "vendor_name": row[1] or row[0],
                "total_attempts": total,
                "failures": failures,
                "successes": row[4] or 0,
                "failure_rate": round(failures / max(total, 1), 4),
            })
        return result
    except Exception as e:
        logger.error(f"get_provider_stats error: {e}")
        return []


def get_top_dispositions(db: Session, limit: int = 10) -> List[Dict[str, Any]]:
    try:
        rows = db.execute(text(f"""
            SELECT disposition_code, COUNT(*) as cnt
            FROM historical_dispositions
            WHERE disposition_code IS NOT NULL AND disposition_code != ''
            GROUP BY disposition_code
            ORDER BY cnt DESC
            LIMIT {limit}
        """)).fetchall()
        total = sum(r[1] for r in rows) or 1
        return [{"code": r[0], "count": r[1], "pct": round(r[1]/total*100, 2)} for r in rows]
    except Exception as e:
        logger.error(f"get_top_dispositions error: {e}")
        return []


def get_data_quality_summary(db: Session) -> Dict[str, Any]:
    """Report on data quality issues found during ingestion."""
    try:
        ingestion_logs = db.execute(text("""
            SELECT source_file, rows_read, rows_inserted, rows_skipped, rows_error
            FROM data_ingestion_log
            ORDER BY started_at DESC
        """)).fetchall()

        issues = []
        for log in ingestion_logs:
            if log[4] > 0 or log[3] > 0:
                issues.append({
                    "file": log[0],
                    "rows_read": log[1],
                    "rows_inserted": log[2],
                    "rows_skipped": log[3],
                    "rows_error": log[4],
                })

        return {
            "ingestion_runs": len(ingestion_logs),
            "tables_with_issues": len(issues),
            "issues": issues[:10],
        }
    except Exception as e:
        return {"error": str(e)}


def get_campaign_analytics(db: Session, campaign_id: Optional[str] = None) -> Dict[str, Any]:
    """Per-campaign performance analytics."""
    where = f"AND campaign_id = '{campaign_id}'" if campaign_id else ""
    try:
        rows = db.execute(text(f"""
            SELECT
                campaign_id,
                COUNT(*) as total_calls,
                SUM(CASE WHEN call_status IN ('ANSWERED','CONNECTED','COMPLETED') THEN 1 ELSE 0 END) as answered,
                AVG(CASE WHEN duration_sec > 0 THEN duration_sec ELSE NULL END) as avg_duration
            FROM historical_calls
            WHERE campaign_id IS NOT NULL {where}
            GROUP BY campaign_id
            ORDER BY total_calls DESC
            LIMIT 20
        """)).fetchall()

        return {
            "campaigns": [
                {
                    "campaign_id": r[0],
                    "total_calls": r[1],
                    "answered": r[2],
                    "answer_rate": round((r[2] or 0) / max(r[1] or 1, 1), 4),
                    "avg_duration_sec": round(r[3] or 0, 2),
                }
                for r in rows
            ]
        }
    except Exception as e:
        logger.error(f"get_campaign_analytics error: {e}")
        return {"error": str(e)}


def get_agent_analytics(db: Session) -> Dict[str, Any]:
    """Per-agent performance analytics from historical calls."""
    try:
        rows = db.execute(text("""
            SELECT
                agent_id,
                COUNT(*) as total_calls,
                SUM(CASE WHEN call_status IN ('ANSWERED','CONNECTED','COMPLETED') THEN 1 ELSE 0 END) as answered,
                AVG(duration_sec) as avg_duration
            FROM historical_calls
            WHERE agent_id IS NOT NULL
            GROUP BY agent_id
            ORDER BY total_calls DESC
            LIMIT 20
        """)).fetchall()

        return {
            "top_agents": [
                {
                    "agent_id": r[0],
                    "total_calls": r[1],
                    "answered": r[2],
                    "answer_rate": round((r[2] or 0) / max(r[1] or 1, 1), 4),
                    "avg_duration_sec": round(r[3] or 0, 2),
                }
                for r in rows
            ]
        }
    except Exception as e:
        return {"error": str(e)}


def _scalar(db: Session, sql: str):
    """Execute a SQL scalar query safely."""
    try:
        result = db.execute(text(sql)).scalar()
        return result if result is not None else 0
    except Exception as e:
        logger.error(f"Analytics query error: {e} | SQL: {sql}")
        return 0
