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
