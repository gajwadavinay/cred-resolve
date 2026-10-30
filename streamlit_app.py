import streamlit as st
from sqlalchemy import text

from app.agent_manager import get_agent_summary, recover_expired_reservations
from app.borrower_manager import get_eligible_borrowers, release_expired_borrower_reservations
from app.data_cleaning import compute_account_eligibility
from app.data_ingestion import run_full_ingestion
from app.database import SessionLocal
from app.predictive_engine import compute_pacing_recommendation, get_historical_stats
from app.progressive_dialer import run_progressive_cycle
from app.safety_controller import evaluate_request
from app.allocator import allocate_and_dial, get_provider_health
from app.call_manager import get_active_call_counts


st.set_page_config(page_title="SmartDialer Streamlit", layout="wide")


def get_db():
    db = SessionLocal()
    try:
        return db
    finally:
        pass


@st.cache_data(ttl=10)
def fetch_health():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        agents_loaded = db.execute(text("SELECT COUNT(*) FROM agents")).scalar() or 0
        borrowers_loaded = db.execute(text("SELECT COUNT(*) FROM borrowers")).scalar() or 0
        hist_calls = db.execute(text("SELECT COUNT(*) FROM historical_calls")).scalar() or 0
        return {
            "db_connected": True,
            "agents_loaded": agents_loaded,
            "borrowers_loaded": borrowers_loaded,
            "historical_calls": hist_calls,
        }
    finally:
        db.close()


@st.cache_data(ttl=10)
def fetch_agent_summary():
    db = SessionLocal()
    try:
        summary = get_agent_summary(db)
        return summary
    finally:
        db.close()


@st.cache_data(ttl=10)
def fetch_provider_health():
    return get_provider_health()


@st.cache_data(ttl=10)
def fetch_historical_stats():
    db = SessionLocal()
    try:
        return get_historical_stats(db)
    finally:
        db.close()


@st.cache_data(ttl=10)
def fetch_eligible_borrowers(limit: int = 20):
    db = SessionLocal()
    try:
        return get_eligible_borrowers(db, limit=limit)
    finally:
        db.close()


def import_data_action():
    try:
        summary = run_full_ingestion()
        db = SessionLocal()
        try:
            eligibility = compute_account_eligibility(db)
        finally:
            db.close()
        st.session_state["last_import"] = {
            "ingestion_summary": summary,
            "eligibility": eligibility,
        }
        st.success("Data import completed.")
    except Exception as exc:
        st.error(f"Import failed: {exc}")


def run_progressive_action(max_calls: int, dry_run: bool):
    db = SessionLocal()
    try:
        result = run_progressive_cycle(db, max_calls=max_calls, dry_run=dry_run)
        st.session_state["last_progressive"] = result
        st.success("Progressive cycle completed.")
    except Exception as exc:
        st.error(f"Progressive cycle failed: {exc}")
    finally:
        db.close()


def run_predictive_action(max_calls: int, dry_run: bool):
    db = SessionLocal()
    try:
        recover_expired_reservations(db)
        release_expired_borrower_reservations(db)

        agent_snap = get_agent_summary(db)
        call_snap = get_active_call_counts(db)
        hist_stats = get_historical_stats(db)
        answer_rate = hist_stats["historical_answer_rate"]

        pacing = compute_pacing_recommendation(
            db=db,
            available_agents=agent_snap["available"],
            reserved_agents=agent_snap["reserved"],
            connected_calls=call_snap["connected"],
            ringing_calls=call_snap["ringing"] + call_snap["initiated"],
            provider_failure_rate=0.05,
        )

        recommended = min(pacing.recommended_calls, max_calls)
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
            borrowers = get_eligible_borrowers(db, limit=approved)
            for borrower in borrowers[:approved]:
                result = allocate_and_dial(
                    db=db,
                    borrower_info=borrower,
                    dialer_mode="PREDICTIVE",
                    campaign_id=None,
                    dry_run=dry_run,
                )
                if result["success"]:
                    calls_initiated += 1
                else:
                    calls_failed += 1
                    errors.append(result.get("reason", "Unknown error"))

        output = {
            "mode": "PREDICTIVE",
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
            "errors": errors,
        }
        st.session_state["last_predictive"] = output
        st.success("Predictive cycle completed.")
    except Exception as exc:
        st.error(f"Predictive cycle failed: {exc}")
    finally:
        db.close()


st.title("SmartDialer Streamlit Dashboard")

if "last_import" not in st.session_state:
    st.session_state["last_import"] = None
if "last_progressive" not in st.session_state:
    st.session_state["last_progressive"] = None
if "last_predictive" not in st.session_state:
    st.session_state["last_predictive"] = None

health = fetch_health()
summary = fetch_agent_summary()
provider_health = fetch_provider_health()
hist_stats = fetch_historical_stats()

col1, col2, col3, col4 = st.columns(4)
col1.metric("DB", "Online" if health["db_connected"] else "Offline", "✓")
col2.metric("Agents", health["agents_loaded"])
col3.metric("Borrowers", health["borrowers_loaded"])
col4.metric("Historical Calls", health["historical_calls"])

st.subheader("System Overview")
agent_cols = st.columns(3)
agent_cols[0].metric("Available", summary.get("available", 0))
agent_cols[1].metric("Reserved", summary.get("reserved", 0))
agent_cols[2].metric("Connected", summary.get("connected", 0))

st.subheader("Provider Health")
for provider, info in provider_health.items():
    st.write(f"{provider}: {info.get('status', 'unknown')} | failure rate {float(info.get('failure_rate', 0)) * 100:.1f}%")

st.subheader("Historical Stats")
answer_rate = hist_stats.get("historical_answer_rate", 0)
avg_talk = hist_stats.get("historical_avg_talk_time", 0)
st.write(f"Answer rate: {answer_rate * 100:.1f}% | Avg talk time: {avg_talk:.1f}s")

st.sidebar.header("Controls")
st.sidebar.button("Import Raw Data", on_click=import_data_action)

st.sidebar.subheader("Progressive")
prog_max = st.sidebar.number_input("Max calls", min_value=1, max_value=100, value=10)
prog_dry = st.sidebar.checkbox("Dry run", value=False)
if st.sidebar.button("Run Progressive"):
    run_progressive_action(prog_max, prog_dry)

st.sidebar.subheader("Predictive")
pred_max = st.sidebar.number_input("Max calls", min_value=1, max_value=100, value=20, key="pred_max")
pred_dry = st.sidebar.checkbox("Dry run", value=False, key="pred_dry")
if st.sidebar.button("Run Predictive"):
    run_predictive_action(pred_max, pred_dry)

st.subheader("Last Import")
if st.session_state["last_import"]:
    st.json(st.session_state["last_import"])
else:
    st.info("No import run yet.")

st.subheader("Last Progressive Cycle")
if st.session_state["last_progressive"]:
    st.json(st.session_state["last_progressive"])
else:
    st.info("No progressive run yet.")

st.subheader("Last Predictive Cycle")
if st.session_state["last_predictive"]:
    st.json(st.session_state["last_predictive"])
else:
    st.info("No predictive run yet.")

st.subheader("Eligible Borrowers")
limit = st.slider("Borrower rows", 5, 50, 20)
try:
    borrowers = fetch_eligible_borrowers(limit)
    st.dataframe(borrowers[:limit], use_container_width=True)
except Exception as exc:
    st.warning(f"Borrower query failed: {exc}")
