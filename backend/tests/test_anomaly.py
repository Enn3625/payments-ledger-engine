"""The anomaly rule engine: what fires, what does not, and what it must never do."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    Account,
    AnomalyFlag,
    AnomalyRule,
    LedgerEntry,
    PaymentIntent,
    PaymentIntentStatus,
)
from app.services.anomaly import _money, evaluate_capture
from app.services.ledger import trial_balance
from app.services.payment_intents import create_payment_intent
from app.services.signatures import SIGNATURE_HEADER, sign_payload

WEBHOOK_URL = "/webhooks/payment-events"
FLAGS_URL = "/anomaly-flags"


@pytest.fixture
def secret() -> str:
    return get_settings().webhook_secret


def make_intent(session: Session, merchant: Account, amount: int) -> PaymentIntent:
    intent = create_payment_intent(
        session,
        amount=amount,
        currency="INR",
        merchant_account_id=merchant.id,
        reference=f"order_{amount}",
    )
    session.commit()
    return intent


def capture(client: TestClient, intent: PaymentIntent, secret: str, event_id: str) -> object:
    body = {
        "id": event_id,
        "type": "payment.captured",
        "data": {
            "payment_intent_id": str(intent.id),
            "amount": intent.amount,
            "currency": intent.currency,
        },
    }
    raw = json.dumps(body).encode()
    return client.post(
        WEBHOOK_URL,
        content=raw,
        headers={
            SIGNATURE_HEADER: sign_payload(raw, secret),
            "Content-Type": "application/json",
        },
    )


def flags(session: Session, rule: AnomalyRule | None = None) -> list[AnomalyFlag]:
    statement = select(AnomalyFlag)
    if rule is not None:
        statement = statement.where(AnomalyFlag.rule == rule)
    session.expire_all()
    return list(session.scalars(statement).all())


class TestAmountThreshold:
    def test_a_large_capture_is_flagged(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        """Default limit is INR 5,000.00; capture INR 7,500.00."""
        intent = make_intent(session, merchant, 750_000)

        assert capture(client, intent, secret, "evt_big").status_code == 200

        raised = flags(session, AnomalyRule.AMOUNT_THRESHOLD)
        assert len(raised) == 1
        assert raised[0].payment_intent_id == intent.id
        assert raised[0].transaction_id is not None

    def test_a_capture_under_the_limit_is_not_flagged(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        intent = make_intent(session, merchant, 100_000)

        capture(client, intent, secret, "evt_small")

        assert flags(session) == []

    def test_a_capture_exactly_at_the_limit_is_not_flagged(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        """The rule is `>`, not `>=` -- the limit itself is still allowed."""
        limit = get_settings().anomaly_amount_threshold
        intent = make_intent(session, merchant, limit)

        capture(client, intent, secret, "evt_exact")

        assert flags(session, AnomalyRule.AMOUNT_THRESHOLD) == []

    def test_the_reason_carries_the_numbers(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        """A reviewer must be able to reconstruct the decision from the flag."""
        intent = make_intent(session, merchant, 750_000)

        capture(client, intent, secret, "evt_explain")

        reason = flags(session, AnomalyRule.AMOUNT_THRESHOLD)[0].reason
        assert "INR 7,500.00" in reason
        assert "INR 5,000.00" in reason


class TestVelocity:
    def test_captures_within_the_limit_are_not_flagged(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        """Default limit is 5 captures per window."""
        for n in range(5):
            intent = make_intent(session, merchant, 1_000 + n)
            capture(client, intent, secret, f"evt_v{n}")

        assert flags(session, AnomalyRule.VELOCITY) == []

    def test_the_sixth_capture_trips_the_rule(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        for n in range(6):
            intent = make_intent(session, merchant, 1_000 + n)
            capture(client, intent, secret, f"evt_burst{n}")

        raised = flags(session, AnomalyRule.VELOCITY)
        assert len(raised) == 1
        assert "6 captures" in raised[0].reason
        assert "limit of 5" in raised[0].reason

    def test_velocity_is_scoped_to_one_account(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        """Busy platform, quiet merchant: another account's traffic must not implicate it."""
        from tests.conftest import make_account

        other = make_account(session, name="liabilities:other_merchant")

        for n in range(6):
            intent = make_intent(session, other, 1_000 + n)
            capture(client, intent, secret, f"evt_other{n}")

        quiet = make_intent(session, merchant, 2_000)
        capture(client, quiet, secret, "evt_quiet")

        raised = flags(session, AnomalyRule.VELOCITY)
        assert len(raised) == 1
        assert raised[0].account_id == other.id


class TestFlagsAreAdvisory:
    def test_a_flagged_capture_still_posts_to_the_ledger(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts: dict[str, Account],
        merchant: Account,
        secret: str,
    ):
        """The engine records concerns. It never swallows money."""
        intent = make_intent(session, merchant, 900_000)

        response = capture(client, intent, secret, "evt_flagged")

        assert response.status_code == 200
        assert response.json()["status"] == "processed"

        session.expire_all()
        assert session.scalar(select(func.count()).select_from(LedgerEntry)) == 2
        assert trial_balance(session).is_balanced
        assert session.get(PaymentIntent, intent.id).status is PaymentIntentStatus.SUCCEEDED
        assert len(flags(session)) == 1

    def test_one_capture_can_trip_several_rules(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        for n in range(5):
            capture(client, make_intent(session, merchant, 1_000 + n), secret, f"evt_pre{n}")

        capture(client, make_intent(session, merchant, 900_000), secret, "evt_both")

        raised = {flag.rule for flag in flags(session)}
        assert raised == {AnomalyRule.VELOCITY, AnomalyRule.AMOUNT_THRESHOLD}

    def test_a_failed_capture_raises_no_flags(
        self, client: TestClient, session: Session, merchant: Account, secret: str
    ):
        """No cash account, so the capture fails -- the flag must roll back with it."""
        intent = make_intent(session, merchant, 900_000)

        assert capture(client, intent, secret, "evt_rollback").status_code == 422
        assert flags(session) == []


class TestRuleEngineDirectly:
    def test_thresholds_come_from_settings(
        self, session: Session, chart_of_accounts, merchant: Account
    ):
        """Tuning a rule is configuration, not a code change."""
        from app.services.ledger import credit, debit, post_transaction

        intent = make_intent(session, merchant, 1_000)
        transaction = post_transaction(
            session,
            description="capture",
            entries=[debit(chart_of_accounts["cash"].id, 1_000), credit(merchant.id, 1_000)],
        )

        strict = get_settings().model_copy(update={"anomaly_amount_threshold": 500})
        raised = evaluate_capture(
            session, intent=intent, transaction=transaction, amount=1_000, settings=strict
        )

        assert [flag.rule for flag in raised] == [AnomalyRule.AMOUNT_THRESHOLD]

    @pytest.mark.parametrize(
        ("minor_units", "expected"),
        [(0, "INR 0.00"), (5, "INR 0.05"), (100, "INR 1.00"), (150_000, "INR 1,500.00")],
    )
    def test_money_is_rendered_without_floats(self, minor_units, expected):
        assert _money(minor_units, "INR") == expected


class TestFlagsApi:
    def test_flags_are_listed_newest_first(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        capture(client, make_intent(session, merchant, 900_000), secret, "evt_api_1")
        capture(client, make_intent(session, merchant, 800_000), secret, "evt_api_2")

        response = client.get(FLAGS_URL)

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2
        assert body[0]["created_at"] >= body[1]["created_at"]
        assert body[0]["rule"] == AnomalyRule.AMOUNT_THRESHOLD.value

    def test_flags_can_be_filtered_by_rule(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        capture(client, make_intent(session, merchant, 900_000), secret, "evt_filter")

        assert len(client.get(FLAGS_URL, params={"rule": "amount_threshold"}).json()) == 1
        assert client.get(FLAGS_URL, params={"rule": "velocity"}).json() == []

    def test_unknown_rule_is_rejected(self, client: TestClient):
        assert client.get(FLAGS_URL, params={"rule": "vibes"}).status_code == 422

    def test_empty_when_nothing_is_flagged(self, client: TestClient):
        assert client.get(FLAGS_URL).json() == []
