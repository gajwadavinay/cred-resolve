# SmartDialer

A complete, production-ready prototype collections dialer with Progressive and Predictive modes.
Built with the real `collections_30k_dataset` as the historical data source.

---

## Architecture

```
Campaign
    │
    ▼
┌──────────────────────────────────────┐
│  Predictive Pacing Engine            │  ← RECOMMEND ONLY. No call access.
│  • Erlang-inspired formula           │
│  • Historical answer rate from DB    │
│  • Accounts for ringing, failures    │
└──────────────┬───────────────────────┘
               │ recommendation (int)
               ▼
┌──────────────────────────────────────┐
│  Safety Controller  ← INDEPENDENT   │  ← Cannot be bypassed
│  • Re-reads DB independently         │
│  • Approve / Reduce / Reject         │
│  • Falls back to Progressive at <5%  │
│  • Rejects all during outages        │
└──────────────┬───────────────────────┘
               │ approved count
               ▼
┌──────────────────────────────────────┐
│  Call Allocator                      │
│  • Reserve borrower (dedup)          │
│  • Reserve agent (atomic lock)       │
│  • Create call record                │
│  • Route to provider                 │
└──────────┬───────────────────────────┘
      ┌────┘────────────────────┐
      ▼                         ▼
 Provider A (Fast)         Provider B (Slow)
 3% failure                15% failure
 Ordered events            Duplicates + OOO
```

---

## Technology Stack

| Component | Technology |
|---|---|
| API Framework | FastAPI 0.115 |
| Database | SQLite (WAL mode) |
| ORM | SQLAlchemy 2.0 |
| Data Pipeline | Pandas, NumPy |
| Charts | Matplotlib |
| Tests | Pytest |
| Validation | Pydantic v2 |

---

## Dataset

The real `collections_30k_dataset` is used as historical data. Dataset intentionally contains:
- Duplicate rows
- Missing values  
- Mixed timezones (UTC / Asia/Kolkata / Asia/Dubai)
- Out-of-order events
- Legacy disposition codes

| File | Rows |
|---|---|
| accounts.csv | 30,000 |
| agents.csv | 30,000 |
| borrowers.csv | 30,600 |
| calls.csv | 91,350 |
| call_attempts.csv | 120,000 |
| call_dispositions.csv | 35,000 |
| payments.csv | 25,500 |
| promises_to_pay.csv | 18,000 |
| complaints.csv | 8,000 |
| daily_targeting.csv | 45,000 |
| agent_sessions.csv | 15,000 |
| campaigns.csv | 120 |

---

## Installation

```bash
# 1. Clone / copy the project
cd smartdialer

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the API server
uvicorn app.main:app --reload --port 8000
```

The database is created automatically on first run at `data/processed/smartdialer.db`.

---

## Quick Start

### Step 1: Import Data
```bash
curl -X POST http://localhost:8000/data/import
```

### Step 2: Check Health
```bash
curl http://localhost:8000/health
```

### Step 3: Run Progressive Dialing
```bash
curl -X POST http://localhost:8000/dialer/progressive/run \
  -H "Content-Type: application/json" \
  -d '{"max_calls": 10, "dry_run": false}'
```

### Step 4: Run Simulation (All Scenarios)
```bash
curl -X POST "http://localhost:8000/simulation/run-all-scenarios?num_agents=50"
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | System health + data import status |
| POST | `/data/import` | Import all CSV data (idempotent) |
| GET | `/data/profile` | Data quality profiling report |
| GET | `/agents` | List agents (filterable by status) |
| GET | `/agents/summary` | Agent counts by status |
| GET | `/borrowers/eligible` | Eligible borrowers for dialing |
| GET | `/campaigns` | List campaigns |
| POST | `/dialer/progressive/run` | Run progressive dialing cycle |
| POST | `/dialer/predictive/run` | Run predictive dialing cycle |
| GET | `/dialer/status` | Live dialer operational status |
| GET | `/analytics/summary` | Historical analytics summary |
| GET | `/analytics/historical-stats` | Stats used for pacing decisions |
| POST | `/simulation/run` | Run a single simulation scenario |
| POST | `/simulation/run-all-scenarios` | Run all A/B/C/D scenarios |
| GET | `/simulation/results` | List past simulation runs |

Swagger UI: http://localhost:8000/docs

---

## Simulation Scenarios

| Scenario | Answer Rate | Talk Time | Description |
|---|---|---|---|
| A | 20% | 120s | Low answer rate |
| B | 50% | 90s | Medium answer rate |
| C | 70% | 180s | High answer rate |
| D | Dynamic | Dynamic | Changing conditions |

```bash
# Scenario C, Predictive mode, 100 agents
curl -X POST http://localhost:8000/simulation/run \
  -H "Content-Type: application/json" \
  -d '{"scenario":"C","dialer_mode":"PREDICTIVE","num_agents":100,"steps":20}'
```

---

## Testing

```bash
# Run full test suite
python -m pytest tests/ -v

# Run specific test category
python -m pytest tests/test_safety.py -v
python -m pytest tests/test_predictive.py -v
python -m pytest tests/test_events.py -v
python -m pytest tests/test_failures.py -v
python -m pytest tests/test_concurrency.py -v
python -m pytest tests/test_data.py -v
```

---

## Load Testing

```bash
python scripts/load_test.py
```
Tests 100, 1,000, and 10,000 agents.

---

## Key Design Decisions

### Predictive Formula
```
effective_available = available_agents - reserved_agents
max_ideal = floor(effective_available / answer_rate)
adjusted_for_failure = ceil(max_ideal / (1 - provider_failure_rate))
recommendation = max(0, adjusted_for_failure - ringing_calls)
```

### Agent Reservation (Optimistic Locking)
```sql
UPDATE agents SET status='RESERVED', version=version+1
WHERE id=:id AND status='AVAILABLE' AND version=:current
-- rowcount==0 → race condition → fail gracefully
```

### Idempotency
Every provider event carries an `idempotency_key`. UNIQUE constraint on `call_events`.
Duplicate events return `{"status": "DUPLICATE"}` — no state change.

### Terminal State Protection
`COMPLETED`, `FAILED`, `CANCELLED` can never be overwritten. Checked before every transition.

---

## Limitations

1. **SQLite concurrency**: Write serialization is a bottleneck at >50 concurrent reservations. For production, use PostgreSQL with `FOR UPDATE SKIP LOCKED`.
2. **No real provider integration**: All calls are mocked. Providers A and B are simulated.
3. **No scheduler**: The dialing cycle must be triggered via API call or script. Production would use a task queue.
4. **Single-node**: No horizontal scaling. Production would add Redis for agent queues.
5. **Data note**: Simulation outcomes are simulated. Only the borrower list comes from the real dataset.

---

## Docs

- [`docs/architecture.md`](docs/architecture.md) — System architecture diagrams
- [`docs/state_machines.md`](docs/state_machines.md) — Agent and call state machines
- [`docs/architecture_decision.md`](docs/architecture_decision.md) — ADR with technical answer to architecture question
