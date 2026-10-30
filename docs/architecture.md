# SmartDialer Architecture

## System Overview

```
Campaign
    │
    ▼
┌─────────────────────────────────┐
│  Pacing Engine (RECOMMEND ONLY) │  predictive_engine.py
│  - Computes optimal call count  │
│  - Uses historical answer rate  │
│  - Accounts for provider failure│
│  - NEVER initiates calls        │
└──────────────┬──────────────────┘
               │ recommendation (int)
               ▼
┌─────────────────────────────────┐
│  Safety Controller (GATE)       │  safety_controller.py
│  - Reads DB state independently │
│  - Verifies agent capacity      │
│  - Approves / Reduces / Rejects │
│  - Falls back to progressive    │
│  - Cannot be bypassed           │
└──────────────┬──────────────────┘
               │ approved count
               ▼
┌─────────────────────────────────┐
│  Call Allocator                 │  allocator.py
│  - Reserves borrower            │
│  - Reserves agent (atomic)      │
│  - Creates call record          │
│  - Routes to provider           │
└──────┬───────────────┬──────────┘
       │               │
       ▼               ▼
┌────────────┐  ┌────────────────┐
│ Provider A │  │   Provider B   │
│ Fast/Relia │  │ Slow/Unreliable│
│ 3% failure │  │ 15% failure    │
│ No dupes   │  │ Dupes + OOO    │
└────────────┘  └────────────────┘
```

## Data Flow

```
Raw CSVs (19 files)
    │
    ▼  data_ingestion.py
SQLite Database
    │
    ├── historical_calls        (91k rows → analytics)
    ├── historical_call_attempts(120k rows → provider stats)
    ├── historical_dispositions (35k rows → top dispositions)
    ├── historical_payments     (25.5k rows → recent payment suppression)
    ├── historical_ptps         (18k rows → PTP suppression)
    ├── historical_complaints   (8k rows → complaint suppression)
    ├── agents                  (30k rows → operational state)
    ├── accounts                (30k rows → eligibility)
    ├── borrowers               (30.6k rows → contact data)
    └── campaigns               (120 rows → campaign config)
    │
    ▼  data_cleaning.py
Eligibility flags computed
    │
    ▼  FastAPI /dialer/progressive/run or /predictive/run
Dialing cycle executed
```

## Key Design Decisions

### 1. Separation of Prediction and Safety
The Predictive Engine (predictive_engine.py) returns only an integer — it has no
reference to the database connection used by the Safety Controller, and no method
to bypass it. The Safety Controller (safety_controller.py) re-reads state from the
DB independently before every approval decision.

### 2. Optimistic Locking for Agent Reservation
```sql
UPDATE agents SET
    status = 'RESERVED',
    reservation_id = :rid,
    reserved_at = :now,
    version = version + 1
WHERE id = :aid
  AND status = 'AVAILABLE'
  AND version = :current_version
```
If rowcount == 0 → race condition detected → fail gracefully.

### 3. Idempotent Event Processing
Every provider event carries an `idempotency_key`. The CallEvent table has a
UNIQUE constraint on this column. Duplicate events are detected at INSERT and
returned to the caller as DUPLICATE status — no state change occurs.

### 4. Terminal State Protection
The call state machine checks STATE_PRIORITY before applying any transition.
States in TERMINAL_STATES (COMPLETED, FAILED, CANCELLED) can never be overwritten.
