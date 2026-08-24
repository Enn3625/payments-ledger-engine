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
| Send a signed webhook | `.venv/Scripts/python -m scripts.seed_event capture --intent <id>` |
| Create a user | `.venv/Scripts/python -m scripts.create_user --email <e> --role admin` |

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

## How webhooks are secured and applied

`POST /webhooks/payment-events` takes a Stripe-style signature header:

```
X-Webhook-Signature: t=<unix seconds>,v1=<hex hmac-sha256 of "{t}.{raw body}">
```

Three details do the security work:

- The HMAC covers the **raw request bytes**, which is why the route reads the
  body as `bytes` and parses it itself. Verifying a re-serialised body would
  accept payloads whose bytes differ from what the provider signed.
- Comparison uses `hmac.compare_digest`, so the digest cannot be walked byte by
  byte with a timing oracle.
- The timestamp is *inside* the signed payload and must be within
  `WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS`. Without it, a payload captured off the
  wire stays replayable forever, because its signature never expires.

Every signature failure returns the same `401` body, so a prober cannot learn
which check rejected it.

### Applying an event exactly once

`payment.captured` is the only event that posts to the ledger:

| Account | Direction | Amount |
| --- | --- | --- |
| `assets:cash` | debit | full captured amount |
| merchant account (from the intent) | credit | amount − fee |
| `revenue:platform_fees` | credit | fee, if any |

Two independent guards stop a redelivery from moving money twice:

1. `webhook_events.event_id` is UNIQUE — the same event redelivered returns the
   original outcome with `duplicate: true` and touches nothing.
2. `payment_intents.transaction_id` is UNIQUE — even a *different* event id
   cannot attach a second ledger transaction to the same intent.

Guard 1 is the fast path; guard 2 still holds if an event id is forged, reused,
or the provider re-sends a capture under a new id.

Effects and the "this event is processed" write commit in one transaction, so
there is no state where the ledger moved but the event log disagrees. Failures
are stored as `failed` with the payload and error, ready for
`retry_event()` — the function the admin retry button will call in step 6.
Unknown event types are recorded as `ignored` and acknowledged, so a provider
adding a new type never starts failing deliveries.

## The anomaly rule engine

Rules run on capture, inside the same transaction as the ledger posting, so a
flag and the transaction it describes commit together or not at all.

| Rule | Fires when | Setting |
| --- | --- | --- |
| `amount_threshold` | one capture exceeds the limit | `ANOMALY_AMOUNT_THRESHOLD` (default INR 5,000.00) |
| `velocity` | more than N captures hit one account inside the window | `ANOMALY_VELOCITY_MAX_CAPTURES` / `ANOMALY_VELOCITY_WINDOW_MINUTES` (default 5 per 10 min) |

Three properties are deliberate:

- **Flags never block money.** A rule firing records a concern; the capture
  still posts and the ledger still balances. Infrastructure that silently
  swallows a payment because a heuristic fired is worse than one that files a
  note for a human.
- **Every flag is explainable.** The reason carries the numbers that triggered
  it — `capture of INR 9,500.00 exceeds the single-transaction limit of
  INR 5,000.00` — so a reviewer can reconstruct the decision months later. That
  is also why these are rules rather than a score from an opaque model.
- **Thresholds are configuration.** Tuning them is an environment change, not a
  redeploy of the engine.

## Sending webhook events

`scripts/seed_event.py` signs and posts events so you never hand-roll an HMAC:

```bash
python -m scripts.seed_event capture --intent <uuid>            # amount defaults to the intent
python -m scripts.seed_event capture --intent <uuid> --fee 2000
python -m scripts.seed_event capture --intent <uuid> --event-id evt_x   # reuse to test redelivery
python -m scripts.seed_event capture --intent <uuid> --tamper   # alters the body after signing -> 401
python -m scripts.seed_event fail --intent <uuid> --reason card_declined
python -m scripts.seed_event burst --merchant <uuid> --count 6  # trips the velocity rule
python -m scripts.seed_event raw --file event.json              # sign a hand-written body
```

Creating and reading intents is admin-only, so `burst` and a `capture` without
an explicit `--amount` need `--email`/`--password` (it logs in for you) or a
`--token`. The webhook route itself needs neither.

It uses the same `sign_payload()` the tests use, so the demo path and the
production verification path cannot drift apart. Exit status is non-zero when
the API rejects the event, which makes it usable in scripts.


## Endpoints the dashboard reads

| Route | Role | Notes |
| --- | --- | --- |
| `GET /accounts/balances` | viewer | Per-account totals plus the trial balance, in one grouped query |
| `GET /transactions` | viewer | Newest first, with entries and anomaly flags; `?flagged_only=true` |
| `GET /anomaly-flags` | viewer | `?rule=` filter |
| `GET /webhooks/events` | viewer | Delivery log; `?status=` filter |
| `POST /webhooks/events/{id}/retry` | admin | Replays the stored payload |

Both list endpoints load their related rows in a fixed number of queries rather
than one per row, so the page cost does not grow with the ledger.

CORS is configured, not wildcarded: `CORS_ORIGINS` is a comma-separated
allowlist. `*` would let any site on the internet call this API with a user's
token.

## Auth and RBAC

JWT bearer tokens, two roles, and no self-service signup: an API that mints its
own admins is a liability, so accounts are provisioned out of band with
`scripts/create_user.py`.

```bash
python -m scripts.create_user --email admin@demo.local --role admin
python -m scripts.create_user --email viewer@demo.local --role viewer
```

| Route | Anonymous | Viewer | Admin |
| --- | --- | --- | --- |
| `POST /auth/login`, `GET /health` | allowed | allowed | allowed |
| `POST /webhooks/payment-events` | allowed | allowed | allowed |
| `GET /auth/me`, `GET /anomaly-flags`, `GET /payment-intents/{id}` | 401 | allowed | allowed |
| `POST /payment-intents` | 401 | 403 | allowed |
| `POST /webhooks/events/{id}/retry` | 401 | 403 | allowed |

The inbound webhook route stays public on purpose: the payment provider has no
login, and its HMAC signature *is* its authentication. The admin retry route is
the opposite -- a human action, so it needs a human identity.

Details that matter more than the happy path:

- **The database is the source of truth, not the token.** Every request re-loads
  the user, so deactivating an account or demoting an admin takes effect on
  their next call rather than whenever their token expires. Both are tested.
- **Login cannot be used to enumerate accounts.** Wrong password, unknown email
  and deactivated account all return the same 401 body, and an unknown email
  still pays the cost of a bcrypt check so absence is not detectable by timing.
- **The algorithm is pinned on decode**, which is what stops an `alg: none`
  downgrade. Tokens without an `exp` are refused outright.
- **bcrypt truncates silently past 72 bytes**, so longer passwords are rejected
  rather than quietly weakened.

Because the login form is OAuth2 password flow, the **Authorize** button in
`/docs` works: log in there once and every protected endpoint is callable from
the Swagger UI.

Velocity is scoped per account, so one busy merchant never implicates another.
`UNIQUE (rule, transaction_id)` means re-evaluating a capture cannot pile up
duplicates of the same finding. Flags are readable at `GET /anomaly-flags`,
newest first, optionally filtered by `?rule=`.
