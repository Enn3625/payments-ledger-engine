"""The read endpoints the dashboard is built on."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Account, AnomalyRule, PaymentIntent, WebhookEventStatus
from app.services.ledger import credit, debit, post_transaction
from app.services.payment_intents import create_payment_intent
from app.services.signatures import SIGNATURE_HEADER, sign_payload

BALANCES_URL = "/accounts/balances"
TRANSACTIONS_URL = "/transactions"
EVENTS_URL = "/webhooks/events"


@pytest.fixture
def secret() -> str:
    return get_settings().webhook_secret


def capture(
    client: TestClient, intent: PaymentIntent, secret: str, event_id: str, amount: int | None = None
):
    body = {
        "id": event_id,
        "type": "payment.captured",
        "data": {
            "payment_intent_id": str(intent.id),
            "amount": amount if amount is not None else intent.amount,
            "currency": intent.currency,
        },
    }
    raw = json.dumps(body).encode()
    return client.post(
        "/webhooks/payment-events",
        content=raw,
        headers={
            SIGNATURE_HEADER: sign_payload(raw, secret),
            "Content-Type": "application/json",
        },
    )


def make_intent(session: Session, merchant: Account, amount: int) -> PaymentIntent:
    intent = create_payment_intent(
        session, amount=amount, currency="INR", merchant_account_id=merchant.id
    )
    session.commit()
    return intent


class TestBalances:
    def test_requires_authentication(self, anonymous_client: TestClient):
        assert anonymous_client.get(BALANCES_URL).status_code == 401

    def test_a_viewer_may_read_balances(self, viewer_client: TestClient, chart_of_accounts):
        assert viewer_client.get(BALANCES_URL).status_code == 200

    def test_accounts_with_no_entries_are_listed_at_zero(
        self, client: TestClient, chart_of_accounts
    ):
        """An empty account still belongs in the chart of accounts."""
        body = client.get(BALANCES_URL).json()

        assert {row["name"] for row in body["accounts"]} == {
            "assets:cash",
            "liabilities:merchant_payable",
            "revenue:platform_fees",
        }
        assert all(row["balance"] == 0 and row["entry_count"] == 0 for row in body["accounts"])
        assert body["trial_balance"]["is_balanced"] is True

    def test_balances_are_signed_by_normal_side(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        capture(client, make_intent(session, merchant, 100_000), secret, "evt_bal")

        rows = {row["name"]: row for row in client.get(BALANCES_URL).json()["accounts"]}

        # Asset grows on debits, liability on credits: both read positive.
        assert rows["assets:cash"]["balance"] == 100_000
        assert rows["assets:cash"]["debits"] == 100_000
        assert rows["liabilities:merchant_payable"]["balance"] == 100_000
        assert rows["liabilities:merchant_payable"]["credits"] == 100_000

    def test_the_trial_balance_is_the_headline(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        capture(client, make_intent(session, merchant, 250_000), secret, "evt_trial")

        totals = client.get(BALANCES_URL).json()["trial_balance"]

        assert totals["total_debits"] == totals["total_credits"] == 250_000
        assert totals["is_balanced"] is True


class TestTransactions:
    def test_requires_authentication(self, anonymous_client: TestClient):
        assert anonymous_client.get(TRANSACTIONS_URL).status_code == 401

    def test_empty_ledger(self, client: TestClient):
        assert client.get(TRANSACTIONS_URL).json() == []

    def test_a_transaction_carries_the_entries_that_balance_it(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        capture(client, make_intent(session, merchant, 100_000), secret, "evt_tx")

        [transaction] = client.get(TRANSACTIONS_URL).json()

        assert transaction["status"] == "posted"
        assert transaction["amount"] == 100_000  # the debit side
        assert len(transaction["entries"]) == 2
        assert {entry["direction"] for entry in transaction["entries"]} == {"debit", "credit"}
        assert {entry["account_name"] for entry in transaction["entries"]} == {
            "assets:cash",
            "liabilities:merchant_payable",
        }
        assert transaction["flags"] == []

    def test_a_fee_split_reports_the_debit_side_as_the_amount(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        """Three legs, but the transaction is still worth what came in."""
        intent = make_intent(session, merchant, 100_000)
        body = {
            "id": "evt_fee",
            "type": "payment.captured",
            "data": {
                "payment_intent_id": str(intent.id),
                "amount": 100_000,
                "currency": "INR",
                "fee": 2_000,
            },
        }
        raw = json.dumps(body).encode()
        client.post(
            "/webhooks/payment-events",
            content=raw,
            headers={
                SIGNATURE_HEADER: sign_payload(raw, secret),
                "Content-Type": "application/json",
            },
        )

        [transaction] = client.get(TRANSACTIONS_URL).json()

        assert transaction["amount"] == 100_000
        assert len(transaction["entries"]) == 3

    def test_flagged_transactions_are_marked(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        capture(client, make_intent(session, merchant, 900_000), secret, "evt_flagged")

        [transaction] = client.get(TRANSACTIONS_URL).json()

        assert transaction["flags"] == [AnomalyRule.AMOUNT_THRESHOLD.value]

    def test_flagged_only_filters(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        capture(client, make_intent(session, merchant, 1_000), secret, "evt_clean")
        capture(client, make_intent(session, merchant, 900_000), secret, "evt_big")

        everything = client.get(TRANSACTIONS_URL).json()
        flagged = client.get(TRANSACTIONS_URL, params={"flagged_only": True}).json()

        assert len(everything) == 2
        assert len(flagged) == 1
        assert flagged[0]["amount"] == 900_000

    def test_newest_first(
        self, client: TestClient, session: Session, cash: Account, payable: Account
    ):
        for n in range(3):
            post_transaction(
                session,
                description=f"posting {n}",
                entries=[debit(cash.id, 100 + n), credit(payable.id, 100 + n)],
            )
            session.commit()

        descriptions = [t["description"] for t in client.get(TRANSACTIONS_URL).json()]

        assert descriptions == ["posting 2", "posting 1", "posting 0"]

    def test_paging(self, client: TestClient, session: Session, cash: Account, payable: Account):
        for n in range(5):
            post_transaction(
                session,
                description=f"posting {n}",
                entries=[debit(cash.id, 100 + n), credit(payable.id, 100 + n)],
            )
            session.commit()

        page = client.get(TRANSACTIONS_URL, params={"limit": 2, "offset": 2}).json()

        assert [t["description"] for t in page] == ["posting 2", "posting 1"]

    def test_limit_is_bounded(self, client: TestClient):
        assert client.get(TRANSACTIONS_URL, params={"limit": 5_000}).status_code == 422


class TestWebhookEventsList:
    def test_requires_authentication(self, anonymous_client: TestClient):
        assert anonymous_client.get(EVENTS_URL).status_code == 401

    def test_a_viewer_may_read_the_delivery_log(self, viewer_client: TestClient):
        assert viewer_client.get(EVENTS_URL).status_code == 200

    def test_processed_and_failed_events_are_both_listed(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        capture(client, make_intent(session, merchant, 1_000), secret, "evt_ok")
        # Amount disagrees with the intent, so this one fails and is retryable.
        capture(client, make_intent(session, merchant, 1_000), secret, "evt_bad", amount=999)

        events = {event["event_id"]: event for event in client.get(EVENTS_URL).json()}

        assert events["evt_ok"]["status"] == WebhookEventStatus.PROCESSED.value
        assert events["evt_ok"]["transaction_id"] is not None
        assert events["evt_bad"]["status"] == WebhookEventStatus.FAILED.value
        assert events["evt_bad"]["attempts"] == 1
        assert "does not match" in events["evt_bad"]["last_error"]

    def test_filtering_by_status(
        self,
        client: TestClient,
        session: Session,
        chart_of_accounts,
        merchant: Account,
        secret: str,
    ):
        capture(client, make_intent(session, merchant, 1_000), secret, "evt_ok2")
        capture(client, make_intent(session, merchant, 1_000), secret, "evt_bad2", amount=999)

        failed = client.get(EVENTS_URL, params={"status": "failed"}).json()

        assert [event["event_id"] for event in failed] == ["evt_bad2"]

    def test_unknown_status_is_rejected(self, client: TestClient):
        assert client.get(EVENTS_URL, params={"status": "sideways"}).status_code == 422


class TestCors:
    def test_the_dashboard_origin_is_allowed(self, client: TestClient, chart_of_accounts):
        response = client.get(BALANCES_URL, headers={"Origin": "http://localhost:5173"})

        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"

    def test_an_unknown_origin_is_not_allowed(self, client: TestClient, chart_of_accounts):
        """`*` would let any site on the internet call this API with a user token."""
        response = client.get(BALANCES_URL, headers={"Origin": "https://evil.example"})

        assert "access-control-allow-origin" not in response.headers
