"""Webhook processing: turn a verified event into ledger effects, exactly once.

Two independent guards keep a redelivered event from moving money twice:

1. `webhook_events.event_id` is UNIQUE. The second delivery of an already
   processed event returns the original outcome without touching the ledger.
2. `payment_intents.transaction_id` is UNIQUE. Even a *different* event that
   tried to capture the same intent again could not attach a second ledger
   transaction to it.

Guard 1 is the fast path; guard 2 is the one that still holds if the event id
is forged, reused, or the provider sends the same capture under a new id.

Effects and the "this event is processed" write commit in a single transaction.
There is no state where the ledger moved but the event log disagrees.

Failures are recorded rather than swallowed: the event stays as `failed` with
its payload and error, so it can be retried later from the dashboard.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import (
    Account,
    PaymentIntent,
    PaymentIntentStatus,
    WebhookEvent,
    WebhookEventStatus,
)
from app.schemas.webhook import CaptureData, FailureData, WebhookEnvelope
from app.services.ledger import credit, debit, post_transaction

#: Statuses that mean "this delivery is settled, do not act on it again".
TERMINAL_STATUSES = frozenset({WebhookEventStatus.PROCESSED, WebhookEventStatus.IGNORED})

#: Guard against ping-ponging with a concurrent delivery of the same event.
MAX_INSERT_ATTEMPTS = 3


class WebhookProcessingError(Exception):
    """The event was authentic but its effects could not be applied."""


class MalformedEventDataError(WebhookProcessingError):
    """The event body does not carry the fields its type requires."""


class UnknownPaymentIntentError(WebhookProcessingError):
    """The event references an intent that does not exist."""


class IntentNotCapturableError(WebhookProcessingError):
    """The intent is in a state that cannot accept this event."""


class EventIntentMismatchError(WebhookProcessingError):
    """The event contradicts the intent it points at (amount, currency)."""


class UnknownWebhookEventError(WebhookProcessingError):
    """A retry was requested for an event we never received."""


class MissingLedgerAccountError(WebhookProcessingError):
    """A well-known account the posting needs has not been created."""


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    event_id: str
    status: WebhookEventStatus
    #: True when this delivery had already been settled by an earlier one.
    duplicate: bool
    transaction_id: uuid.UUID | None = None
    payment_intent_id: uuid.UUID | None = None


def handle_event(
    session_factory: sessionmaker[Session],
    *,
    envelope: WebhookEnvelope,
    payload: dict[str, Any],
    signature: str,
    settings: Settings,
    attempt: int = 1,
) -> WebhookOutcome:
    """Apply a verified event at most once. Signature checking happens earlier."""
    if attempt > MAX_INSERT_ATTEMPTS:  # pragma: no cover - needs a pathological race
        raise WebhookProcessingError(
            f"could not record event {envelope.id!r} after {MAX_INSERT_ATTEMPTS} attempts"
        )

    session = session_factory()
    try:
        event = _lock_event(session, envelope.id)

        if event is not None and event.status in TERMINAL_STATUSES:
            return WebhookOutcome(
                event_id=event.event_id,
                status=event.status,
                duplicate=True,
                transaction_id=event.transaction_id,
                payment_intent_id=event.payment_intent_id,
            )

        if event is None:
            event = WebhookEvent(
                event_id=envelope.id,
                event_type=envelope.type,
                payload=payload,
                signature=signature,
                status=WebhookEventStatus.RECEIVED,
            )
            session.add(event)
            try:
                session.flush()
            except IntegrityError:
                # A concurrent delivery of the same event won the insert race.
                # Start over: the lock in _lock_event will wait for it to
                # finish, and we will see its result as a duplicate.
                session.rollback()
                session.close()
                return handle_event(
                    session_factory,
                    envelope=envelope,
                    payload=payload,
                    signature=signature,
                    settings=settings,
                    attempt=attempt + 1,
                )

        event.attempts += 1
        event.last_error = None

        try:
            outcome = _apply(session, event, envelope, settings)
        except WebhookProcessingError as exc:
            session.rollback()
            _record_failure(
                session_factory,
                envelope=envelope,
                payload=payload,
                signature=signature,
                error=str(exc),
            )
            raise

        session.commit()
        return outcome
    finally:
        session.close()


def retry_event(
    session_factory: sessionmaker[Session],
    *,
    event_id: str,
    settings: Settings,
) -> WebhookOutcome:
    """Reprocess a stored event from its saved payload.

    This is what the admin "retry webhook" control calls. It replays the
    original body, so a retry cannot smuggle in different data than the
    provider actually signed.
    """
    with session_factory() as session:
        event = session.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event_id))
        if event is None:
            raise UnknownWebhookEventError(f"no such webhook event: {event_id}")
        payload = dict(event.payload)
        signature = event.signature

    try:
        envelope = WebhookEnvelope.model_validate(payload)
    except ValidationError as exc:
        raise MalformedEventDataError(f"stored payload for {event_id} is unusable") from exc

    return handle_event(
        session_factory,
        envelope=envelope,
        payload=payload,
        signature=signature,
        settings=settings,
    )


def _lock_event(session: Session, event_id: str) -> WebhookEvent | None:
    """Take a row lock so concurrent deliveries queue instead of racing."""
    return session.scalar(
        select(WebhookEvent).where(WebhookEvent.event_id == event_id).with_for_update()
    )


def _apply(
    session: Session,
    event: WebhookEvent,
    envelope: WebhookEnvelope,
    settings: Settings,
) -> WebhookOutcome:
    handler = HANDLERS.get(envelope.type)

    if handler is None:
        # Unknown types are recorded and acknowledged, never rejected: a
        # provider adding a new event type must not start failing deliveries.
        event.status = WebhookEventStatus.IGNORED
        return WebhookOutcome(
            event_id=event.event_id, status=event.status, duplicate=False
        )

    handler(session, event, envelope, settings)

    event.status = WebhookEventStatus.PROCESSED
    event.processed_at = datetime.now(UTC)
    return WebhookOutcome(
        event_id=event.event_id,
        status=event.status,
        duplicate=False,
        transaction_id=event.transaction_id,
        payment_intent_id=event.payment_intent_id,
    )


def _handle_capture(
    session: Session,
    event: WebhookEvent,
    envelope: WebhookEnvelope,
    settings: Settings,
) -> None:
    data = _parse(CaptureData, envelope)
    intent = _lock_intent(session, data.payment_intent_id)

    if intent.transaction_id is not None or intent.status is PaymentIntentStatus.SUCCEEDED:
        raise IntentNotCapturableError(
            f"payment intent {intent.id} has already been captured"
        )
    if intent.status is PaymentIntentStatus.FAILED:
        raise IntentNotCapturableError(f"payment intent {intent.id} has already failed")
    if data.amount != intent.amount:
        raise EventIntentMismatchError(
            f"captured amount {data.amount} does not match intent amount {intent.amount}"
        )
    if data.currency.upper() != intent.currency:
        raise EventIntentMismatchError(
            f"captured currency {data.currency.upper()} does not match intent "
            f"currency {intent.currency}"
        )
    if data.fee >= data.amount:
        raise EventIntentMismatchError(
            f"fee {data.fee} must be smaller than the captured amount {data.amount}"
        )

    cash = _account_by_name(session, settings.cash_account_name)
    entries = [
        debit(cash.id, data.amount),
        credit(intent.merchant_account_id, data.amount - data.fee),
    ]
    if data.fee:
        fee_account = _account_by_name(session, settings.fee_revenue_account_name)
        entries.append(credit(fee_account.id, data.fee))

    transaction = post_transaction(
        session,
        description=f"capture {intent.reference or intent.id}",
        entries=entries,
        currency=intent.currency,
    )

    intent.status = PaymentIntentStatus.SUCCEEDED
    intent.transaction_id = transaction.id
    event.transaction_id = transaction.id
    event.payment_intent_id = intent.id


def _handle_failure(
    session: Session,
    event: WebhookEvent,
    envelope: WebhookEnvelope,
    settings: Settings,
) -> None:
    data = _parse(FailureData, envelope)
    intent = _lock_intent(session, data.payment_intent_id)

    if intent.status is PaymentIntentStatus.SUCCEEDED:
        raise IntentNotCapturableError(
            f"payment intent {intent.id} was already captured; it cannot now fail"
        )

    # Nothing moved, so nothing is posted. The ledger records money, not intent.
    intent.status = PaymentIntentStatus.FAILED
    event.payment_intent_id = intent.id


HANDLERS: dict[str, Callable[[Session, WebhookEvent, WebhookEnvelope, Settings], None]] = {
    "payment.captured": _handle_capture,
    "payment.failed": _handle_failure,
}


def _parse[T](model: type[T], envelope: WebhookEnvelope) -> T:
    try:
        return model.model_validate(envelope.data)
    except ValidationError as exc:
        raise MalformedEventDataError(
            f"{envelope.type} event {envelope.id} has an unusable data block: "
            f"{exc.error_count()} problem(s)"
        ) from exc


def _lock_intent(session: Session, intent_id: uuid.UUID) -> PaymentIntent:
    """Lock the intent so two events cannot decide its fate concurrently."""
    intent = session.get(PaymentIntent, intent_id, with_for_update=True)
    if intent is None:
        raise UnknownPaymentIntentError(f"no such payment intent: {intent_id}")
    return intent


def _account_by_name(session: Session, name: str) -> Account:
    account = session.scalar(select(Account).where(Account.name == name))
    if account is None:
        raise MissingLedgerAccountError(
            f"ledger account {name!r} does not exist; run the seed script"
        )
    return account


def _record_failure(
    session_factory: sessionmaker[Session],
    *,
    envelope: WebhookEnvelope,
    payload: dict[str, Any],
    signature: str,
    error: str,
) -> None:
    """Persist the failure after the effects were rolled back, so it can be retried."""
    with session_factory() as session:
        event = _lock_event(session, envelope.id)
        if event is None:
            event = WebhookEvent(
                event_id=envelope.id,
                event_type=envelope.type,
                payload=payload,
                signature=signature,
                status=WebhookEventStatus.FAILED,
                attempts=1,
            )
            session.add(event)
        else:
            event.status = WebhookEventStatus.FAILED
            event.attempts += 1
        event.last_error = error
        session.commit()
