# State Machines

## Agent State Machine

```
         ┌──────────┐
    ┌────►│  OFFLINE  │◄────────────────────────────────┐
    │    └────┬─────┘                                   │
    │         │ login / set_online                      │
    │         ▼                                         │
    │    ┌──────────┐ pause           ┌─────────┐       │
    │    │AVAILABLE │────────────────►│ PAUSED  │       │
    │    └────┬─────┘                └────┬────┘       │
    │         │ reserve_agent()           │ resume      │
    │         ▼                          │             │
    │    ┌──────────┐ expiry/fail    ◄───┘             │
    │◄───│ RESERVED │──────────────► AVAILABLE         │
    │    └────┬─────┘                                   │
    │         │ initiate_call()                         │
    │         ▼                                         │
    │    ┌──────────┐ fail/disconnect                   │
    │◄───│ DIALING  │──────────────► AVAILABLE         │
    │    └────┬─────┘                                   │
    │         │ call_answered()                         │
    │         ▼                                         │
    │    ┌──────────┐ hangup                            │
    │◄───│CONNECTED │──────────────► WRAP_UP            │
    │    └──────────┘                    │               │
    │                                    │ wrap_complete  │
    │    ┌──────────┐                    │               │
    └────│ WRAP_UP  │◄───────────────────┘               │
         └──────────┘                                   │
               │ logout                                 │
               └───────────────────────────────────────┘
```

**Recovery:**
- RESERVED agents with `reserved_at` older than `RESERVATION_TTL_SECONDS` (30s)
  are automatically returned to AVAILABLE by `recover_expired_reservations()`.
- WRAP_UP agents older than `WRAP_UP_MAX_SECONDS` (120s) are auto-released.


## Call State Machine

```
 QUEUED
   │ agent reserved
   ▼
 RESERVED ──────────────────────────────────► CANCELLED
   │ provider initiated
   ▼
 INITIATED ─────────────────────────────────► FAILED
   │ provider: RINGING event
   ▼
 RINGING ──────────────────────────────────► FAILED
   │ provider: ANSWERED event
   ▼
 ANSWERED
   │ confirmed live
   ▼
 CONNECTED ─────────────────────────────────► FAILED
   │ agent/borrower hangs up
   ▼
 COMPLETED  (TERMINAL)

```

**Protections:**
- `TERMINAL_STATES = {COMPLETED, FAILED, CANCELLED}` — never overwritten.
- `STATE_PRIORITY` prevents out-of-order events from regressing state.
- `idempotency_key` UNIQUE constraint prevents duplicate event processing.

## Safety Controller Decision Flow

```
 Dialer requests N calls
          │
          ▼
 [1] Is provider healthy?
          │
     NO ──┼──► REJECTED (0 calls approved)
          │
     YES  ▼
 [2] available_agents == 0?
          │
     YES ─┼──► REJECTED (0 calls approved)
          │
     NO   ▼
 [3] mode == PREDICTIVE AND answer_rate < 0.05?
          │
     YES ─┼──► FALLBACK to progressive
          │       → approved = min(N, effective_available)
     NO   ▼
 [4] Compute max_safe:
         PROGRESSIVE → max_safe = effective_available
         PREDICTIVE  → max_safe = floor(effective_available / answer_rate)
          │
          ▼
 [5] approved = min(N, max_safe)
          │
     approved == 0 ──► REJECTED
          │
     approved < N  ──► REDUCED
          │
     approved == N ──► APPROVED
```
