"""Seed the reproducible synthetic demo dataset.

All data here is invented. Nothing is copied from, or modelled on, any real
client, merchant or transaction.

The important property is that **nothing is inserted into the ledger directly**.
Accounts and intents go through the same service functions the API uses, and
every captured payment is applied by `handle_event` -- the same function the
verified webhook route calls once a signature checks out. So the demo ledger is
produced by the system rather than staged to look like it was, and the deferred
balance trigger validates all of it at COMMIT.

    python -m scripts.seed_demo                 # idempotent: safe to re-run
    python -m scripts.seed_demo --reset         # wipe demo data first

Re-running without `--reset` is a no-op once seeded, because every webhook event
carries a fixed id and redelivering a processed event is deliberately harmless.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Account, AccountType, PaymentIntent, User, UserRole  # noqa: E402
from app.schemas.webhook import WebhookEnvelope  # noqa: E402
from app.services.auth import create_user, hash_password  # noqa: E402
from app.services.payment_intents import create_payment_intent  # noqa: E402
from app.services.webhooks import WebhookProcessingError, handle_event  # noqa: E402

# --------------------------------------------------------------------------
# The demo cast. Fictional merchants, fictional orders.
# --------------------------------------------------------------------------
CASH = "assets:cash"
FEES = "revenue:platform_fees"

MERCHANTS = [
    ("liabilities:payable:northwind_books", "Northwind Books"),
    ("liabilities:payable:acme_tools", "Acme Tools"),
    ("liabilities:payable:sunrise_cafe", "Sunrise Cafe"),
]

DEMO_ADMIN_EMAIL = "admin@ledger.demo"
DEMO_VIEWER_EMAIL = "demo@ledger.demo"

TABLES_TO_RESET = (
    "anomaly_flags",
    "webhook_events",
    "idempotency_keys",
    "payment_intents",
    "ledger_entries",
    "transactions",
    "accounts",
    "users",
)


def reset(session: Session) -> None:
    """TRUNCATE bypasses the append-only trigger, which is why it is allowed."""
    session.execute(text(f"TRUNCATE {', '.join(TABLES_TO_RESET)} CASCADE"))
    session.commit()
    print("wiped existing demo data")


def ensure_account(session: Session, name: str, account_type: AccountType) -> Account:
    account = session.scalar(select(Account).where(Account.name == name))
    if account is None:
        account = Account(name=name, type=account_type, currency="INR")
        session.add(account)
        session.flush()
    return account


def ensure_user(session: Session, email: str, password: str, role: UserRole) -> User:
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        return create_user(session, email=email, password=password, role=role)
    # Re-running the seed should restore the documented password, so a demo
    # someone has been poking at can always be put back to a known state.
    user.password_hash = hash_password(password)
    user.role = role
    user.is_active = True
    return user


def ensure_intent(
    session: Session,
    *,
    reference: str,
    amount: int,
    merchant_account_id: uuid.UUID,
    description: str,
) -> PaymentIntent:
    """Find the seeded intent by its reference, or create it.

    Without this the seed is only idempotent from the ledger's point of view:
    replayed webhooks apply once, but every run would leave behind another
    unclaimed intent.
    """
    existing = session.scalar(select(PaymentIntent).where(PaymentIntent.reference == reference))
    if existing is not None:
        return existing
    return create_payment_intent(
        session,
        amount=amount,
        currency="INR",
        merchant_account_id=merchant_account_id,
        reference=reference,
        description=description,
    )


def capture(
    session_factory,
    *,
    event_id: str,
    intent_id: uuid.UUID,
    amount: int,
    fee: int = 0,
) -> None:
    """Apply a capture through the real webhook handler."""
    data: dict[str, object] = {
        "payment_intent_id": str(intent_id),
        "amount": amount,
        "currency": "INR",
    }
    if fee:
        data["fee"] = fee
    payload = {"id": event_id, "type": "payment.captured", "data": data}

    try:
        handle_event(
            session_factory,
            envelope=WebhookEnvelope.model_validate(payload),
            payload=payload,
            signature="t=0,v1=seeded",
            settings=get_settings(),
        )
    except WebhookProcessingError as error:
        # Expected for the deliberately-broken deliveries below.
        print(f"  {event_id}: not applied ({error})")


def seed(session_factory, admin_password: str, viewer_password: str) -> None:
    settings = get_settings()

    with session_factory() as session:
        ensure_account(session, CASH, AccountType.ASSET)
        ensure_account(session, FEES, AccountType.REVENUE)
        merchants = [ensure_account(session, name, AccountType.LIABILITY) for name, _ in MERCHANTS]
        ensure_user(session, DEMO_ADMIN_EMAIL, admin_password, UserRole.ADMIN)
        ensure_user(session, DEMO_VIEWER_EMAIL, viewer_password, UserRole.VIEWER)
        session.commit()
        merchant_ids = [merchant.id for merchant in merchants]
        print(f"accounts: {2 + len(merchants)}   users: 2")

    northwind, acme, sunrise = merchant_ids

    # 1. Ordinary captures, some with a platform fee.
    ordinary = [
        (northwind, 120_000, 2_400, "order_NB_1001"),
        (northwind, 45_000, 900, "order_NB_1002"),
        (acme, 310_000, 6_200, "order_AT_2001"),
        (sunrise, 8_500, 0, "order_SC_3001"),
        (sunrise, 22_000, 440, "order_SC_3002"),
    ]
    for index, (merchant_id, amount, fee, reference) in enumerate(ordinary):
        with session_factory() as session:
            intent = ensure_intent(
                session,
                reference=reference,
                amount=amount,
                merchant_account_id=merchant_id,
                description="synthetic demo order",
            )
            session.commit()
            intent_id = intent.id
        capture(
            session_factory,
            event_id=f"evt_demo_{index:03d}",
            intent_id=intent_id,
            amount=amount,
            fee=fee,
        )
    print(f"captured {len(ordinary)} ordinary payments")

    # 2. One capture above the amount threshold, so a flag is visible.
    with session_factory() as session:
        large = ensure_intent(
            session,
            reference="order_AT_2002",
            amount=settings.anomaly_amount_threshold + 250_000,
            merchant_account_id=acme,
            description="synthetic bulk order",
        )
        session.commit()
        large_id, large_amount = large.id, large.amount
    capture(session_factory, event_id="evt_demo_large", intent_id=large_id, amount=large_amount)
    print("captured 1 payment over the amount threshold")

    # 3. A burst on one merchant, to trip the velocity rule.
    burst = settings.anomaly_velocity_max_captures + 1
    for index in range(burst):
        with session_factory() as session:
            intent = ensure_intent(
                session,
                reference=f"order_SC_burst_{index}",
                amount=1_500 + index * 100,
                merchant_account_id=sunrise,
                description="synthetic burst order",
            )
            session.commit()
            intent_id = intent.id
        capture(
            session_factory,
            event_id=f"evt_demo_burst_{index}",
            intent_id=intent_id,
            amount=1_500 + index * 100,
        )
    print(f"captured {burst} rapid payments on one merchant (velocity rule)")

    # 4. A declined payment: intent fails, ledger untouched.
    with session_factory() as session:
        declined = ensure_intent(
            session,
            reference="order_NB_1003",
            amount=64_000,
            merchant_account_id=northwind,
            description="synthetic declined order",
        )
        session.commit()
        declined_id = declined.id
    failure_payload = {
        "id": "evt_demo_declined",
        "type": "payment.failed",
        "data": {"payment_intent_id": str(declined_id), "reason": "card_declined"},
    }
    try:
        handle_event(
            session_factory,
            envelope=WebhookEnvelope.model_validate(failure_payload),
            payload=failure_payload,
            signature="t=0,v1=seeded",
            settings=settings,
        )
    except WebhookProcessingError as error:  # pragma: no cover - re-run path
        print(f"  evt_demo_declined: not applied ({error})")
    print("recorded 1 declined payment (no ledger entries)")

    # 5. A delivery that cannot be applied, so the dashboard has something for
    #    the admin Retry button to act on. The amount disagrees with the
    #    intent, which is exactly the kind of mismatch that must never post.
    with session_factory() as session:
        mismatched = ensure_intent(
            session,
            reference="order_AT_2003",
            amount=99_000,
            merchant_account_id=acme,
            description="synthetic order with a bad delivery",
        )
        session.commit()
        mismatched_id = mismatched.id
    capture(session_factory, event_id="evt_demo_mismatch", intent_id=mismatched_id, amount=12_345)
    print("recorded 1 failed delivery (retryable from the dashboard)")

    # 6. Redeliver a processed event, proving replays are harmless.
    capture(
        session_factory,
        event_id="evt_demo_000",
        intent_id=uuid.uuid4(),  # ignored: the event id already exists
        amount=120_000,
    )
    print("redelivered an already-processed event (no second posting)")


def summarise(session: Session) -> None:
    from app.services.ledger import trial_balance

    counts = {
        table: session.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608 - fixed names
        for table in (
            "accounts",
            "transactions",
            "ledger_entries",
            "payment_intents",
            "webhook_events",
            "anomaly_flags",
            "users",
        )
    }
    totals = trial_balance(session)
    print("\n--- seeded ---")
    for table, count in counts.items():
        print(f"  {table:<16} {count}")
    print(
        f"  trial balance    debits={totals.total_debits} "
        f"credits={totals.total_credits} balanced={totals.is_balanced}"
    )
    if not totals.is_balanced:  # pragma: no cover - would mean the trigger failed
        raise SystemExit("seeded ledger is unbalanced, which should be impossible")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_demo", description="Seed reproducible synthetic demo data."
    )
    parser.add_argument("--reset", action="store_true", help="wipe existing data before seeding")
    parser.add_argument("--admin-password", default="demo-admin-2026")
    parser.add_argument("--viewer-password", default="explore-the-ledger")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.reset:
        with SessionLocal() as session:
            reset(session)

    seed(SessionLocal, args.admin_password, args.viewer_password)

    with SessionLocal() as session:
        summarise(session)

    print(f"\nadmin:  {DEMO_ADMIN_EMAIL} / {args.admin_password}")
    print(f"viewer: {DEMO_VIEWER_EMAIL} / {args.viewer_password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
