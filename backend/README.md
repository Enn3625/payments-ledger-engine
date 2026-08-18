# Backend — local development

FastAPI + SQLAlchemy 2.0 + PostgreSQL. Amounts are integer **minor units**
(paise for INR); floats are never used for money.

## Setup

```bash
docker compose up -d db                 # published on host port 55432; from the repo root; also creates ledger_test
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Linux/macOS: .venv/bin/python
cp .env.example .env
```

## Common commands

| Task | Command (run from `backend/`) |
| --- | --- |
| Apply migrations | `.venv/Scripts/python -m alembic upgrade head` |
| New migration | `.venv/Scripts/python -m alembic revision -m "..."` |
| Preview migration SQL | `.venv/Scripts/python -m alembic upgrade head --sql` |
| Run tests | `.venv/Scripts/python -m pytest` |
| Lint / format | `.venv/Scripts/python -m ruff check . && .venv/Scripts/python -m ruff format .` |
| Dev server | `.venv/Scripts/python -m uvicorn app.main:app --reload` |

The test suite talks to `TEST_DATABASE_URL` (`ledger_test`), never the dev
database, and TRUNCATEs between tests.

## How the ledger invariant is enforced

`sum(debits) == sum(credits)` per transaction is checked in two places:

1. **Service layer** (`app/services/ledger.py`) — fails fast with a readable
   error before any SQL runs.
2. **PostgreSQL** (`alembic/versions/0001_double_entry_ledger.py`) — a
   `DEFERRABLE INITIALLY DEFERRED` constraint trigger re-checks at `COMMIT`.
   This is the actual guarantee: it binds the ORM, raw SQL, seed scripts and
   anyone poking at the database with `psql`.

Deferring the check to commit time is what makes multi-statement postings
possible — a transaction may be unbalanced *mid-flight*, never at rest.

Two related rules ride along:

- A transaction in `posted` status must have at least two entries
  (`pending` transactions may have none — that is what a payment intent is).
- `ledger_entries` is **append-only**: `UPDATE` and `DELETE` are rejected by a
  trigger. Corrections are made by posting a reversing transaction.

Because the invariant only fires at `COMMIT`, the tests commit for real instead
of rolling back — a rollback-per-test suite would never exercise it.

## How idempotency is enforced

`POST /payment-intents` requires an `Idempotency-Key` header. The store is one
row per `(endpoint, key)` in `idempotency_keys`, holding a SHA-256 of the
canonicalised request body plus the response that was actually returned.

| Situation | Result |
| --- | --- |
| No key | `400` before any work starts |
| New key | Work runs, response stored, `Idempotent-Replay: false` |
| Same key, same request, original finished | Stored response replayed verbatim with its original status code, `Idempotent-Replay: true` |
| Same key, same request, original still running | `409` — retry shortly |
| Same key, different request | `409` — that is a client bug |

Two properties make it safe under concurrency:

1. The claim is a plain `INSERT` against `UNIQUE (endpoint, key)`, so PostgreSQL
   picks the single winner. No read-then-write race, no advisory locks.
2. The work and the "mark this key completed" update commit in **one**
   transaction, so a replay can only ever return a response whose side effects
   were committed.

Failed work *releases* the key instead of caching the failure, so a client
retrying after a 5xx gets a real attempt rather than a memoised error. A claim
left behind by a process that died is taken over after
`IDEMPOTENCY_CLAIM_TIMEOUT_SECONDS` (default 60), so a crash cannot lock a key
forever.

The request hash is computed over the *parsed* payload, not the raw bytes:
reordered JSON keys are the same request, but any changed value is not.
