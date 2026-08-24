# Payments Ledger & Webhook Engine

[![CI](https://github.com/Enn3625/payments-ledger-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/Enn3625/payments-ledger-engine/actions/workflows/ci.yml)

A double-entry payments ledger with idempotent payment intents and
signature-verified webhooks — the parts of payments infrastructure that are
genuinely hard to get right, rather than the parts that are easy to demo.

All data is synthetic.

## Why these three things

Most of the difficulty in a payments system is not the happy path:

- **Ledger integrity.** Money must never appear or vanish. `sum(debits) == sum(credits)` per transaction is enforced by a `DEFERRABLE INITIALLY DEFERRED`
  constraint trigger in PostgreSQL, so it holds for the ORM, raw SQL, a seed
  script, or anyone with a `psql` prompt. `ledger_entries` is append-only;
  corrections are reversing entries, never edits.
- **Idempotency.** Clients time out and retry. A retried `POST /payment-intents`
  returns the original response instead of charging twice, resolved by a unique
  index rather than a read-then-write race.
- **Webhook security.** The HMAC covers the raw request bytes and a timestamp,
  compared in constant time. A redelivered event cannot re-apply its ledger
  effects, guarded independently at two levels.

## Architecture

```
                     ┌──────────────────────────┐
   payment provider  │  POST /webhooks/         │   HMAC over raw bytes + timestamp
   ─────────────────>│       payment-events     │   (no bearer token: the
                     └────────────┬─────────────┘    signature IS the auth)
                                  │ verified
                                  v
  ┌────────────┐   admin   ┌─────────────────┐      ┌──────────────────────┐
  │  React     │──────────>│    FastAPI      │─────>│  PostgreSQL          │
  │  dashboard │  JWT      │                 │      │                      │
  │  (Vite)    │<──────────│  intents        │      │  accounts            │
  └────────────┘  viewer   │  webhooks       │      │  transactions        │
                           │  anomaly rules  │      │  ledger_entries ─────┼── deferred
                           │  auth + RBAC    │      │  payment_intents     │   balance
                           └─────────────────┘      │  idempotency_keys    │   trigger
                                                    │  webhook_events      │
                                                    │  anomaly_flags       │
                                                    │  users               │
                                                    └──────────────────────┘

  intent created ──> webhook confirms capture ──> balanced transaction posted
                                              └─> anomaly rules evaluated
                                                  (same DB transaction)
```

An intent is money the platform *expects*; the ledger records money that
actually *moved*. Creating an intent writes nothing to `ledger_entries`.

## Stack

| Layer | Choice |
| --- | --- |
| API | FastAPI, SQLAlchemy 2.0, Alembic, psycopg 3 |
| Database | PostgreSQL 16 |
| Auth | JWT (PyJWT), bcrypt, two roles |
| Dashboard | React 18 + TypeScript, Vite |
| Tests | pytest against a real PostgreSQL |
| CI | GitHub Actions |

## Quickstart

```bash
docker compose up -d db                 # PostgreSQL on host port 55432

cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # POSIX: .venv/bin/python
cp .env.example .env
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m scripts.create_user --email admin@demo.local --role admin
.venv/Scripts/python -m uvicorn app.main:app --reload          # :8000, docs at /docs

cd ../frontend
npm install && cp .env.example .env
npm run dev                                                     # :5173
```

`backend/scripts/seed_event.py` signs and sends webhook events so you never
hand-roll an HMAC:

```bash
python -m scripts.seed_event --email admin@demo.local --password <pw> \
  burst --merchant <account-id> --count 6      # trips the velocity rule
python -m scripts.seed_event capture --intent <id> --tamper   # must be rejected: 401
```

More detail in [backend/README.md](backend/README.md) and
[frontend/README.md](frontend/README.md).

## Tests

216 tests, run against a real PostgreSQL rather than a stub — the ledger
invariant lives in a trigger that only fires at `COMMIT`, so a suite that rolled
everything back would never exercise the thing it claims to verify.

| What is proven | Where |
| --- | --- |
| Debits equal credits, including under concurrent writers and with the service layer bypassed | [test_ledger_integrity.py](backend/tests/test_ledger_integrity.py) |
| Duplicate requests do not double-process; failures release the key | [test_idempotency.py](backend/tests/test_idempotency.py) |
| Invalid, tampered, forged and stale-but-authentic signatures are all rejected | [test_webhook_signatures.py](backend/tests/test_webhook_signatures.py) |
| A redelivered event applies exactly once, and a retry is not a second charge | [test_webhooks.py](backend/tests/test_webhooks.py) |
| Rules flag without ever blocking a capture | [test_anomaly.py](backend/tests/test_anomaly.py) |
| Role enforcement, and that a revoked account loses access before its token expires | [test_auth.py](backend/tests/test_auth.py) |

Coverage is 99%, gated at 95% in CI. Every rule that decides whether money
moves is at 100%; the remainder is request plumbing the test client overrides.

CI runs on every push and pull request. Beyond lint and tests it checks two
things that are easy to get silently wrong:

- **migrations reverse cleanly** (`upgrade → downgrade base → upgrade`), because
  a migration you cannot roll back is one you cannot safely deploy;
- **models match migrations** (`alembic check`), which catches a model edited
  without a migration before it becomes a production surprise.
