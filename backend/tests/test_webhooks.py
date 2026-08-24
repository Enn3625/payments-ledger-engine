"""Webhook processing end to end: verification, ledger effects, replays, retries."""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import (
    Account,
    LedgerEntry,
    PaymentIntent,
    PaymentIntentStatus,
    Transaction,
    WebhookEvent,
    WebhookEventStatus,
)
from app.schemas.webhook import WebhookEnvelope
from app.services.ledger import account_balance, trial_balance
from app.services.payment_intents import create_payment_intent
from app.services.signatures import SIGNATURE_HEADER, sign_payload
from app.services.webhooks import (
    MalformedEventDataError,
    UnknownWebhookEventError,
    handle_event,
    retry_event,
)

WEBHOOK_URL = "/webhooks/payment-events"
CAPTURE = "payment.captured"


@pytest.fixture
def secret() -> str:
    return get_settings().webhook_secret


@pytest.fixture
def intent(session: Session, merchant: Account) -> PaymentIntent:
    """An intent waiting to be captured: INR 1,500.00 owed to the merchant."""
    created = create_payment_intent(
        session,
        amount=150_000,
        currency="INR",
        merchant_account_id=merchant.id,
        reference="order_A1B2C3",
    )
    session.commit()
    return created


def capture_event(intent: PaymentIntent, event_id: str = "evt_1", **data: object) -> dict:
    body = {
        "id": event_id,
        "type": CAPTURE,
        "data": {
            "payment_intent_id": str(intent.id),
            "amount": intent.amount,
            "currency": intent.currency,
        },
    }
    body["data"].update(data)
    return body


def post_event(
    client: TestClient,
    body: dict,
    secret: str,
    *,
    timestamp: int | None = None,
    signature: str | None = None,
):
    """Send an event the way a provider would: raw bytes plus a signature."""
    raw = json.dumps(body).encode()
    header = signature if signature is not None else sign_payload(raw, secret, timestamp)
    return client.post(
        WEBHOOK_URL,
        content=raw,
        headers={SIGNATURE_HEADER: header, "Content-Type": "application/json"},
    )


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


class TestSignatureGate:
    def test_valid_signature_is_accepted(
        self, client: TestClient, chart_of_accounts, intent: PaymentIntent, secret: str
    ):
        assert post_event(client, capture_event(intent), secret).status_code == 200

    def test_missing_signature_is_rejected(self, client: TestClient, intent: PaymentIntent):
        response = client.post(WEBHOOK_URL, json=capture_event(intent))

        assert response.status_code == 401

    def test_wrong_secret_is_rejected(self, client: TestClient, intent: PaymentIntent):
        response = post_event(client, capture_event(intent), "attacker_guess")

        assert response.status_code == 401

    def test_tampered_body_is_rejected(
        self, client: TestClient, session: Session, intent: PaymentIntent, secret: str
    ):
        """Sign a small capture, then inflate the amount in the bytes sent."""
        body = capture_event(intent)
        raw = json.dumps(body).encode()
        header = sign_payload(raw, secret)
        tampered = raw.replace(b'"amount": 150000', b'"amount": 999999999')

        response = client.post(
            WEBHOOK_URL,
            content=tampered,
            headers={SIGNATURE_HEADER: header, "Content-Type": "application/json"},
        )

        assert response.status_code == 401
        assert count(session, LedgerEntry) == 0

    def test_stale_signature_is_rejected(
        self, client: TestClient, session: Session, intent: PaymentIntent, secret: str
    ):
        """A replayed capture from an hour ago is authentic but too old."""
        response = post_event(
            client, capture_event(intent), secret, timestamp=int(time.time()) - 3_600
        )

        assert response.status_code == 401
        assert count(session, WebhookEvent) == 0

    def test_rejected_events_reveal_nothing_about_which_check_failed(
        self, client: TestClient, intent: PaymentIntent, secret: str
    ):
        stale = post_event(
            client, capture_event(intent), secret, timestamp=int(time.time()) - 3_600
        )
        forged = post_event(client, capture_event(intent), "attacker_guess")

        assert stale.json() == forged.json()

    def test_body_must_be_a_webhook_envelope(self, client: TestClient, secret: str):
        response = post_event(client, {"nonsense": True}, secret)

        assert response.status_code == 400


class TestCapture:
    def test_capture_posts_a_balanced_transaction(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts: dict[str, Account],
        intent: PaymentIntent,
        secret: str,
    ):
        response = post_event(client, capture_event(intent), secret)

        assert response.status_code == 200
        receipt = response.json()
        assert receipt["status"] == WebhookEventStatus.PROCESSED.value
        assert receipt["duplicate"] is False
        assert receipt["transaction_id"] is not None

        session.expire_all()
        assert count(session, Transaction) == 1
        assert count(session, LedgerEntry) == 2
        assert trial_balance(session).is_balanced
        assert account_balance(session, chart_of_accounts["cash"].id) == 150_000
        assert account_balance(session, chart_of_accounts["payable"].id) == 150_000

    def test_capture_with_a_fee_splits_three_ways(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts: dict[str, Account],
        intent: PaymentIntent,
        secret: str,
    ):
        post_event(client, capture_event(intent, fee=2_000), secret)

        session.expire_all()
        assert account_balance(session, chart_of_accounts["cash"].id) == 150_000
        assert account_balance(session, chart_of_accounts["payable"].id) == 148_000
        assert account_balance(session, chart_of_accounts["revenue"].id) == 2_000
        assert trial_balance(session).is_balanced

    def test_capture_settles_the_intent(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        intent: PaymentIntent,
        secret: str,
    ):
        response = post_event(client, capture_event(intent), secret)

        session.expire_all()
        settled = session.get(PaymentIntent, intent.id)
        assert settled.status is PaymentIntentStatus.SUCCEEDED
        assert str(settled.transaction_id) == response.json()["transaction_id"]

    def test_event_is_linked_to_what_it_produced(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        intent: PaymentIntent,
        secret: str,
    ):
        post_event(client, capture_event(intent, event_id="evt_trace"), secret)

        event = session.scalar(select(WebhookEvent).where(WebhookEvent.event_id == "evt_trace"))
        assert event.status is WebhookEventStatus.PROCESSED
        assert event.payment_intent_id == intent.id
        assert event.transaction_id is not None
        assert event.processed_at is not None
        assert event.attempts == 1


class TestReplayProtection:
    def test_redelivery_does_not_double_apply(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts: dict[str, Account],
        intent: PaymentIntent,
        secret: str,
    ):
        """The same event twice: the second delivery must move no money."""
        body = capture_event(intent, event_id="evt_retry")

        first = post_event(client, body, secret)
        second = post_event(client, body, secret)

        assert first.status_code == second.status_code == 200
        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is True
        assert second.json()["transaction_id"] == first.json()["transaction_id"]

        session.expire_all()
        assert count(session, LedgerEntry) == 2
        assert count(session, Transaction) == 1
        assert account_balance(session, chart_of_accounts["cash"].id) == 150_000

    def test_redelivery_survives_many_retries(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        intent: PaymentIntent,
        secret: str,
    ):
        body = capture_event(intent, event_id="evt_flood")
        responses = [post_event(client, body, secret) for _ in range(5)]

        assert all(r.status_code == 200 for r in responses)
        assert [r.json()["duplicate"] for r in responses] == [False, True, True, True, True]

        session.expire_all()
        assert count(session, LedgerEntry) == 2

    def test_a_fresh_event_id_cannot_capture_the_same_intent_again(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        intent: PaymentIntent,
        secret: str,
    ):
        """Dedupe by event id is not enough; the intent itself must be guarded."""
        post_event(client, capture_event(intent, event_id="evt_a"), secret)
        second = post_event(client, capture_event(intent, event_id="evt_b"), secret)

        assert second.status_code == 422
        assert "already been captured" in second.json()["detail"]

        session.expire_all()
        assert count(session, LedgerEntry) == 2

    def test_the_database_refuses_a_second_transaction_on_one_intent(
        self,
        session: Session,
        chart_of_accounts,
        intent: PaymentIntent,
        cash: Account,
        payable: Account,
    ):
        """The backstop, with the service layer bypassed entirely."""
        from app.services.ledger import credit, debit, post_transaction

        first = post_transaction(
            session,
            description="capture one",
            entries=[debit(cash.id, 10), credit(payable.id, 10)],
        )
        second = post_transaction(
            session,
            description="capture two",
            entries=[debit(cash.id, 10), credit(payable.id, 10)],
        )
        intent.transaction_id = first.id
        session.commit()

        other = PaymentIntent(
            amount=1,
            currency="INR",
            merchant_account_id=payable.id,
            transaction_id=first.id,
        )
        session.add(other)
        with pytest.raises(IntegrityError, match="uq_payment_intents_transaction_id"):
            session.commit()
        session.rollback()
        assert second is not None


class TestFailureEvents:
    def test_failure_marks_the_intent_without_touching_the_ledger(
        self, client: TestClient, session: Session, intent: PaymentIntent, secret: str
    ):
        body = {
            "id": "evt_fail",
            "type": "payment.failed",
            "data": {"payment_intent_id": str(intent.id), "reason": "card_declined"},
        }

        response = post_event(client, body, secret)

        assert response.status_code == 200
        assert response.json()["transaction_id"] is None

        session.expire_all()
        assert session.get(PaymentIntent, intent.id).status is PaymentIntentStatus.FAILED
        assert count(session, LedgerEntry) == 0

    def test_a_captured_intent_cannot_later_fail(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        intent: PaymentIntent,
        secret: str,
    ):
        post_event(client, capture_event(intent), secret)

        response = post_event(
            client,
            {
                "id": "evt_late_failure",
                "type": "payment.failed",
                "data": {"payment_intent_id": str(intent.id)},
            },
            secret,
        )

        assert response.status_code == 422
        session.expire_all()
        assert session.get(PaymentIntent, intent.id).status is PaymentIntentStatus.SUCCEEDED


class TestRejectedEvents:
    def test_unknown_event_types_are_acknowledged_and_ignored(
        self, client: TestClient, session: Session, secret: str
    ):
        """A provider adding a new event type must not start failing deliveries."""
        body = {"id": "evt_new_type", "type": "payout.reversed", "data": {}}

        response = post_event(client, body, secret)

        assert response.status_code == 200
        assert response.json()["status"] == WebhookEventStatus.IGNORED.value
        assert count(session, LedgerEntry) == 0

    def test_unknown_payment_intent(self, client: TestClient, session: Session, secret: str):
        body = {
            "id": "evt_ghost",
            "type": CAPTURE,
            "data": {
                "payment_intent_id": str(uuid.uuid4()),
                "amount": 1_000,
                "currency": "INR",
            },
        }

        response = post_event(client, body, secret)

        assert response.status_code == 422
        assert "no such payment intent" in response.json()["detail"]

    def test_amount_mismatch_is_refused(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        intent: PaymentIntent,
        secret: str,
    ):
        """An authentic event that disagrees with the intent is not applied."""
        response = post_event(client, capture_event(intent, amount=1), secret)

        assert response.status_code == 422
        assert "does not match intent amount" in response.json()["detail"]
        assert count(session, LedgerEntry) == 0

    def test_currency_mismatch_is_refused(
        self, client: TestClient, chart_of_accounts, intent: PaymentIntent, secret: str
    ):
        response = post_event(client, capture_event(intent, currency="USD"), secret)

        assert response.status_code == 422

    def test_fee_larger_than_the_capture_is_refused(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        intent: PaymentIntent,
        secret: str,
    ):
        response = post_event(client, capture_event(intent, fee=intent.amount), secret)

        assert response.status_code == 422
        assert count(session, LedgerEntry) == 0

    def test_malformed_event_data(self, client: TestClient, secret: str):
        body = {"id": "evt_bad_data", "type": CAPTURE, "data": {"amount": "lots"}}

        response = post_event(client, body, secret)

        assert response.status_code == 422
        assert "unusable data block" in response.json()["detail"]

    def test_missing_ledger_accounts_are_reported_clearly(
        self, client: TestClient, intent: PaymentIntent, secret: str
    ):
        """No `assets:cash` in the chart of accounts -- say so, do not half-post."""
        response = post_event(client, capture_event(intent), secret)

        assert response.status_code == 422
        assert "run the seed script" in response.json()["detail"]


class TestFailureRecovery:
    def test_a_failed_event_is_stored_for_retry(
        self, client: TestClient, session: Session, intent: PaymentIntent, secret: str
    ):
        post_event(client, capture_event(intent, event_id="evt_no_accounts"), secret)

        event = session.scalar(
            select(WebhookEvent).where(WebhookEvent.event_id == "evt_no_accounts")
        )
        assert event.status is WebhookEventStatus.FAILED
        assert event.attempts == 1
        assert "does not exist" in event.last_error
        assert event.payload["data"]["amount"] == intent.amount

    def test_retrying_a_failed_event_applies_it(
        self,
        client: TestClient,
        session: Session,
        session_factory: sessionmaker[Session],
        intent: PaymentIntent,
        merchant: Account,
        secret: str,
    ):
        """Fails for want of a cash account, then succeeds once one exists."""
        from tests.conftest import make_account

        failed = post_event(client, capture_event(intent, event_id="evt_recoverable"), secret)
        assert failed.status_code == 422

        make_account(session, name="assets:cash")

        outcome = retry_event(session_factory, event_id="evt_recoverable", settings=get_settings())

        assert outcome.status is WebhookEventStatus.PROCESSED
        assert outcome.duplicate is False

        session.expire_all()
        assert count(session, LedgerEntry) == 2
        assert trial_balance(session).is_balanced
        event = session.scalar(
            select(WebhookEvent).where(WebhookEvent.event_id == "evt_recoverable")
        )
        assert event.attempts == 2
        assert event.last_error is None

    def test_retrying_a_processed_event_changes_nothing(
        self,
        client: TestClient,
        session: Session,
        session_factory: sessionmaker[Session],
        chart_of_accounts,
        intent: PaymentIntent,
        secret: str,
    ):
        post_event(client, capture_event(intent, event_id="evt_done"), secret)

        outcome = retry_event(session_factory, event_id="evt_done", settings=get_settings())

        assert outcome.duplicate is True
        session.expire_all()
        assert count(session, LedgerEntry) == 2

    def test_retrying_an_unknown_event(self, session_factory: sessionmaker[Session]):
        with pytest.raises(UnknownWebhookEventError):
            retry_event(session_factory, event_id="evt_never_seen", settings=get_settings())

    def test_a_failed_intent_cannot_later_be_captured(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        intent: PaymentIntent,
        secret: str,
    ):
        """Events can arrive out of order; a declined payment stays declined."""
        post_event(
            client,
            {
                "id": "evt_declined",
                "type": "payment.failed",
                "data": {"payment_intent_id": str(intent.id)},
            },
            secret,
        )

        late_capture = post_event(client, capture_event(intent, event_id="evt_late"), secret)

        assert late_capture.status_code == 422
        assert "already failed" in late_capture.json()["detail"]
        assert count(session, LedgerEntry) == 0

    def test_repeated_failures_accumulate_attempts(
        self, client: TestClient, session: Session, intent: PaymentIntent, secret: str
    ):
        """Each retry of a still-broken event is counted, not silently swallowed."""
        body = capture_event(intent, event_id="evt_still_broken")

        assert post_event(client, body, secret).status_code == 422
        assert post_event(client, body, secret).status_code == 422

        session.expire_all()
        event = session.scalar(
            select(WebhookEvent).where(WebhookEvent.event_id == "evt_still_broken")
        )
        assert event.status is WebhookEventStatus.FAILED
        assert event.attempts == 2

    def test_retrying_an_event_with_an_unusable_stored_payload(
        self,
        client: TestClient,
        session: Session,
        session_factory: sessionmaker[Session],
        intent: PaymentIntent,
        secret: str,
    ):
        post_event(client, capture_event(intent, event_id="evt_corrupt"), secret)

        event = session.scalar(select(WebhookEvent).where(WebhookEvent.event_id == "evt_corrupt"))
        event.payload = {"not": "an envelope"}
        session.commit()

        with pytest.raises(MalformedEventDataError, match="unusable"):
            retry_event(session_factory, event_id="evt_corrupt", settings=get_settings())


class TestConcurrentDelivery:
    def test_simultaneous_redeliveries_apply_once(
        self,
        session: Session,
        session_factory: sessionmaker[Session],
        chart_of_accounts: dict[str, Account],
        intent: PaymentIntent,
    ):
        """Providers retry aggressively; two deliveries can land at the same instant."""
        body = capture_event(intent, event_id="evt_concurrent")
        envelope = WebhookEnvelope.model_validate(body)
        settings = get_settings()

        def deliver(_: int) -> bool:
            outcome = handle_event(
                session_factory,
                envelope=envelope,
                payload=body,
                signature="t=1,v1=test",
                settings=settings,
            )
            return outcome.duplicate

        with ThreadPoolExecutor(max_workers=6) as pool:
            duplicates = list(pool.map(deliver, range(6)))

        assert duplicates.count(False) == 1
        assert duplicates.count(True) == 5

        session.expire_all()
        assert count(session, WebhookEvent) == 1
        assert count(session, Transaction) == 1
        assert count(session, LedgerEntry) == 2
        assert account_balance(session, chart_of_accounts["cash"].id) == 150_000
        assert trial_balance(session).is_balanced
