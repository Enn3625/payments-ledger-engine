# Payments Ledger & Webhook Engine

[![CI](https://github.com/Enn3625/payments-ledger-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/Enn3625/payments-ledger-engine/actions/workflows/ci.yml)

A double-entry payments ledger with idempotent payment intents and
signature-verified webhooks — the parts of payments infrastructure that are
genuinely hard to get right, rather than the parts that are easy to demo.

All data is synthetic.

## Live demo

> **Replace this block after deploying.** `<!-- DEMO -->`
> Dashboard: `https://<your-project>.vercel.app`
> API docs: `https://<your-service>.onrender.com/docs`

Sign in as the read-only viewer:

| Email | Password |
| --- | --- |
| `demo@ledger.demo` | `explore-the-ledger` |

A viewer can see everything and change nothing, which is what makes a public
login safe to share. The admin role — the only one that can create an intent or
retry a webhook — is not published.

On Render's free tier the API sleeps when idle, so the first request after a
quiet period takes roughly 50 seconds to wake. The dashboard will look empty
until it does.

## Why these three things

Most of the difficulty in a payments system is not the happy path:

- **Ledger integrity.** Money must never appear or vanish. `sum(debits) ==
  sum(credits)` per transaction is enforced by a `DEFERRABLE INITIALLY DEFERRED`
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
| Hosting | Render (API + Postgres), Vercel (dashboard) |

## Key metrics

| Metric | Value | How it was measured |
| --- | --- | --- |
| Webhook processing latency | **p50 24.5 ms · p95 28.8 ms · p99 33.6 ms** | 60 signed deliveries end to end — HMAC verify, event record, intent lock, balanced posting, anomaly rules, one COMMIT. `scripts/benchmark_webhooks.py` |
| Test suite | **236 tests**, 99% coverage, gated at 95% in CI | `pytest --cov` |
| Ledger correctness | 100% coverage on every module that decides whether money moves | — |
| Concurrency | 8 threads × 5 postings stay balanced; 6 simultaneous redeliveries apply exactly once | `test_ledger_integrity.py`, `test_webhooks.py` |

Latency was measured against Dockerised Postgres on a Windows laptop, so it is
a local-development figure, not a production SLO. Re-run the script yourself:
the point is that the number is reproducible, not that it is impressive.

## Quickstart

```bash
docker compose up -d db                 # PostgreSQL on host port 55432

cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # POSIX: .venv/bin/python
cp .env.example .env
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m scripts.seed_demo                      # synthetic demo data
.venv/Scripts/python -m uvicorn app.main:app --reload          # :8000, docs at /docs

cd ../frontend
npm install && cp .env.example .env
npm run dev                                                     # :5173
```

Or run the API and database together:

```bash
docker compose up --build          # db + api on :8000
```

`backend/scripts/seed_event.py` signs and sends webhook events so you never
hand-roll an HMAC:

```bash
python -m scripts.seed_event --email admin@ledger.demo --password <pw> \
  burst --merchant <account-id> --count 6      # trips the velocity rule
python -m scripts.seed_event capture --intent <id> --tamper   # must be rejected: 401
```

More detail in [backend/README.md](backend/README.md) and
[frontend/README.md](frontend/README.md).

## Deploy

**API + database — Render.** [render.yaml](render.yaml) is a blueprint: *New →
Blueprint*, point it at this repo. It provisions a Postgres instance, wires
`DATABASE_URL`, and generates `WEBHOOK_SECRET` and `JWT_SECRET`. Migrations run
on boot.

Then set `CORS_ORIGINS` to the dashboard URL — it is deliberately not in the
blueprint, because it does not exist until the frontend is deployed, and a
wildcard would let any site on the internet call the API with a user's token.

Seed the deployed database once, from the Render shell:

```bash
python -m scripts.seed_demo
```

**Dashboard — Vercel.** Import the repo, set the root directory to `frontend`,
and set `VITE_API_URL` to the Render URL. [frontend/vercel.json](frontend/vercel.json)
covers the rest.

The app refuses to boot outside `local`/`test`/`ci` if `WEBHOOK_SECRET` or
`JWT_SECRET` still holds its development default. A convenient default that
reaches production is a vulnerability, so this fails loudly rather than warning.

## Tests

236 tests, run against a real PostgreSQL rather than a stub — the ledger
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
| Deployed environments refuse development secrets; the seed is re-runnable | [test_config_and_seed.py](backend/tests/test_config_and_seed.py) |

CI runs on every push and pull request. Beyond lint and tests it checks two
things that are easy to get silently wrong:

- **migrations reverse cleanly** (`upgrade → downgrade base → upgrade`), because
  a migration you cannot roll back is one you cannot safely deploy;
- **models match migrations** (`alembic check`), which catches a model edited
  without a migration before it becomes a production surprise.

## How I used AI

I used Claude as a design partner and an implementation accelerator, not as an
oracle. Concretely:

- **Schema and invariant design were argued out in conversation** before any
  code existed — where to enforce the balance rule, whether flags should block a
  capture, why an intent must not touch the ledger. The decision to enforce
  `debits == credits` in a deferred database trigger rather than only in the
  service layer came out of that discussion, and it is the design choice the
  whole project rests on.
- **Boilerplate was generated, then reviewed line by line**: models, migrations,
  Pydantic schemas, the React panels. Generated code that I could not explain
  did not stay.
- **The codebase was checked against itself.** Adding `alembic check` to CI
  immediately surfaced four real drifts between models and migrations —
  two indexes that existed only in a migration, and three unique constraints
  whose model names did not match the database. Those were bugs, not noise.
- **Every claim was verified by running it**, not by trusting a summary: signed
  webhooks sent against a live server, tampered payloads confirmed rejected,
  concurrency tested with real threads, and the latency figures above measured
  rather than estimated.
- **The failures were instructive.** A CORS misconfiguration surfaced as
  "cannot reach the API" while the request was returning `200` — the fix was
  both the config and the misleading error message that sent me to the wrong
  layer.

## What I'd do next

Multi-currency support is the obvious gap: the schema carries a currency column
and refuses cross-currency postings, but there is no FX rate table or
revaluation, so a second currency would need both. I would also replace the
two hand-written anomaly rules with a scored model once there is enough history
to train on, keeping the rule engine as an explainable floor. Finally, a nightly
reconciliation job comparing ledger balances against the provider's settlement
report would close the loop — the ledger is internally consistent by
construction, but nothing currently proves it agrees with the outside world.
