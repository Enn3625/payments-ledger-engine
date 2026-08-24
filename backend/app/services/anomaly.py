"""The fraud/anomaly rule engine.

Design constraints, in order of importance:

* **Flags never block money.** A rule firing records a concern; the capture
  still posts and the ledger still balances. A heuristic that can silently
  swallow a payment is worse than one that files a note for a human.
* **Every flag is explainable.** The reason string carries the numbers that
  triggered it, so a reviewer can reconstruct the decision months later.
* **Thresholds are configuration.** Tuning them is an environment change, not
  a code change.

Rules run inside the capture transaction, so a flag and the transaction it
describes commit together or not at all.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AnomalyFlag, AnomalyRule, LedgerEntry, PaymentIntent, Transaction


@dataclass(frozen=True, slots=True)
class CaptureContext:
    """Everything the rules are allowed to look at for one capture."""

    intent: PaymentIntent
    transaction: Transaction
    amount: int
    account_id: uuid.UUID


#: A rule returns the reason it fired, or None when it is satisfied.
Rule = Callable[[Session, CaptureContext, Settings], str | None]


def _amount_threshold_rule(
    session: Session, context: CaptureContext, settings: Settings
) -> str | None:
    limit = settings.anomaly_amount_threshold
    if context.amount <= limit:
        return None
    return (
        f"capture of {_money(context.amount, context.intent.currency)} exceeds the "
        f"single-transaction limit of {_money(limit, context.intent.currency)}"
    )


def _velocity_rule(
    session: Session, context: CaptureContext, settings: Settings
) -> str | None:
    window_minutes = settings.anomaly_velocity_window_minutes
    limit = settings.anomaly_velocity_max_captures
    window_start = datetime.now(UTC) - timedelta(minutes=window_minutes)

    # Counts the capture being processed too: it is already flushed to the
    # transaction, so the rule sees the world as it will be after commit.
    recent = session.scalar(
        select(func.count(func.distinct(LedgerEntry.transaction_id))).where(
            LedgerEntry.account_id == context.account_id,
            LedgerEntry.created_at >= window_start,
        )
    )
    if recent is None or recent <= limit:
        return None
    return (
        f"{recent} captures on this account in the last {window_minutes} minutes "
        f"exceeds the limit of {limit}"
    )


RULES: dict[AnomalyRule, Rule] = {
    AnomalyRule.AMOUNT_THRESHOLD: _amount_threshold_rule,
    AnomalyRule.VELOCITY: _velocity_rule,
}


def evaluate_capture(
    session: Session,
    *,
    intent: PaymentIntent,
    transaction: Transaction,
    amount: int,
    settings: Settings,
) -> list[AnomalyFlag]:
    """Run every rule against a freshly posted capture. Flushes, does not commit."""
    context = CaptureContext(
        intent=intent,
        transaction=transaction,
        amount=amount,
        account_id=intent.merchant_account_id,
    )

    flags: list[AnomalyFlag] = []
    for rule, evaluate in RULES.items():
        reason = evaluate(session, context, settings)
        if reason is None:
            continue
        flags.append(
            AnomalyFlag(
                rule=rule,
                reason=reason,
                transaction_id=transaction.id,
                payment_intent_id=intent.id,
                account_id=context.account_id,
            )
        )

    if flags:
        session.add_all(flags)
        session.flush()
    return flags


def _money(minor_units: int, currency: str) -> str:
    """Render paise as rupees for the reason string, without float arithmetic."""
    major, minor = divmod(minor_units, 100)
    return f"{currency} {major:,}.{minor:02d}"
