# Payments Ledger & Webhook Engine — Build Spec

## Why this project
Most fintech portfolio projects (virtual cards, multi-currency dashboards) are tutorial clones recruiters have seen dozens of times. This project instead demonstrates the parts of payments infrastructure that are actually hard to get right: ledger integrity, idempotency, and webhook security — the same things production payment systems (Razorpay, Stripe) and fintech infra companies (like Decentro) care about most. It directly extends real experience: Razorpay order creation + webhook verification (e-learning platform), and JWT/RBAC backend design (healthcare ERP).

**Constraint: use only synthetic/seeded demo data. Never use real client data, schemas, or business logic from paid client work.**

## Stack
- Backend: FastAPI, PostgreSQL, SQLAlchemy (or SQLModel)
- Frontend: React + TypeScript
- Auth: JWT
- Testing: pytest
- CI: GitHub Actions
- Deployment: Render/Railway (backend + Postgres), Vercel (frontend) — or a single Docker Compose setup if you prefer one-click local demo + a hosted version

## Build order

### 1. Double-entry ledger schema
Core tables:
- `accounts` (id, name, type, created_at)
- `transactions` (id, description, status, created_at)
- `ledger_entries` (id, transaction_id, account_id, amount, direction[debit/credit])

Enforce (in a DB constraint or service-layer check): for every `transaction_id`, sum(debits) == sum(credits). This invariant is the core correctness story — write a test for it early and keep it passing throughout.

### 2. Idempotent payment-intent creation
- `POST /payment-intents` requires an `Idempotency-Key` header.
- Store keys in an `idempotency_keys` table (key, request_hash, response_snapshot, created_at).
- Duplicate key + matching request → return the original cached response, don't reprocess.
- Duplicate key + different request body → 409 conflict.

### 3. Webhook handling with signature verification
- `POST /webhooks/payment-events` verifies an HMAC signature (mimic Razorpay/Stripe: shared secret, signature header, payload hash comparison).
- Store processed event IDs (`webhook_events` table) to reject replays.
- Support safe retries: processing the same valid event twice must not double-apply ledger effects.

### 4. Fraud/anomaly rule engine
Keep rules simple and explainable, e.g.:
- Velocity: > N transactions from one account within X minutes → flag.
- Threshold: transaction amount > configurable limit → flag.
- Store flags in an `anomaly_flags` table with a reason string.

### 5. Auth and RBAC
- JWT auth, two roles: `admin` (full access, can retry webhooks) and `viewer` (read-only).
- Reuse the pattern from your ERP work.

### 6. React dashboard
- Transactions list (with status, flagged indicator)
- Ledger balances per account
- Anomaly flags list
- Manual "retry webhook" button (admin only)
- Keep styling clean and functional — this supports the backend story, it isn't the headline.

### 7. Tests + CI
Minimum test coverage:
- Ledger integrity invariant (debits == credits) never breaks, including under concurrent-ish requests.
- Idempotency: duplicate requests don't double-process.
- Webhook signature verification rejects invalid/replayed events.
- GitHub Actions workflow running pytest on push; add the passing badge to the README.

### 8. Deploy + README
Seed the deployed instance with synthetic demo data (fake accounts/transactions) — set this up as a seed script, not manual DB entry, so it's reproducible.

README must include:
- One-line what/why
- Architecture diagram (even a simple one, e.g. via excalidraw or a text-based diagram)
- Live demo link + demo login credentials (viewer role, so anyone can explore safely)
- Key metrics: webhook processing latency, test coverage %, anything else concrete
- Short "How I used AI" section — be specific (planning schema/architecture with Claude, generating boilerplate, checking codebase for contradictions, then manual review before deploy — same workflow you already use)
- "What I'd do next" section (2-3 sentences): e.g. multi-currency support, real fraud-scoring model, reconciliation batch job

## Notes for scope/time management
- Steps 1-3 are the substantive, differentiating work — don't rush these even if it means spending more time here.
- Steps 4-6 reuse patterns you've already built in production work — should move fast.
- Steps 7-8 are what make the project *credible* to a reviewer, not optional polish. Don't skip the README even at the end of a long build session — it's often the only thing actually read before someone clicks the live link.
