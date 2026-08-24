"""POST /payment-intents behaviour, ignoring the idempotency protocol itself."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, LedgerEntry, PaymentIntent, PaymentIntentStatus
from tests.conftest import make_account

CREATE_URL = "/payment-intents"


def create_body(merchant: Account, **overrides: object) -> dict:
    body = {
        "amount": 150_000,
        "currency": "INR",
        "merchant_account_id": str(merchant.id),
        "reference": "order_A1B2C3",
        "description": "1x annual subscription",
    }
    body.update(overrides)
    return body


def post_intent(client: TestClient, body: dict, key: str | None = "key-1"):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(CREATE_URL, json=body, headers=headers)


class TestCreate:
    def test_creates_an_intent(self, client: TestClient, session: Session, merchant: Account):
        response = post_intent(client, create_body(merchant))

        assert response.status_code == 201
        payload = response.json()
        assert payload["amount"] == 150_000
        assert payload["currency"] == "INR"
        assert payload["status"] == PaymentIntentStatus.REQUIRES_PAYMENT.value
        assert payload["merchant_account_id"] == str(merchant.id)
        assert payload["reference"] == "order_A1B2C3"

        stored = session.get(PaymentIntent, uuid.UUID(payload["id"]))
        assert stored is not None
        assert stored.amount == 150_000

    def test_creating_an_intent_touches_no_ledger_entries(
        self, client: TestClient, session: Session, merchant: Account
    ):
        """An intent is expected money, not moved money. The ledger stays empty."""
        assert post_intent(client, create_body(merchant)).status_code == 201

        assert session.scalar(select(func.count()).select_from(LedgerEntry)) == 0

    def test_marks_the_response_as_not_replayed(self, client: TestClient, merchant: Account):
        response = post_intent(client, create_body(merchant))

        assert response.headers["Idempotent-Replay"] == "false"

    def test_lowercase_currency_is_normalised(self, client: TestClient, merchant: Account):
        response = post_intent(client, create_body(merchant, currency="inr"))

        assert response.status_code == 201
        assert response.json()["currency"] == "INR"


class TestCreateRejections:
    def test_unknown_merchant_account(self, client: TestClient, session: Session):
        body = {"amount": 1_000, "merchant_account_id": str(uuid.uuid4())}
        response = client.post(CREATE_URL, json=body, headers={"Idempotency-Key": "k"})

        assert response.status_code == 422
        assert "no such merchant account" in response.json()["detail"]
        assert session.scalar(select(func.count()).select_from(PaymentIntent)) == 0

    def test_currency_mismatch_with_merchant_account(self, client: TestClient, session: Session):
        usd_merchant = make_account(session, name="liabilities:usd_payable", currency="USD")
        response = post_intent(client, create_body(usd_merchant, currency="INR"))

        assert response.status_code == 422
        assert "settles in USD" in response.json()["detail"]

    def test_non_positive_amounts(self, client: TestClient, merchant: Account):
        for amount in (0, -1):
            response = post_intent(client, create_body(merchant, amount=amount))
            assert response.status_code == 422

    def test_float_amount_is_not_silently_rounded(self, client: TestClient, merchant: Account):
        """Money is integer minor units; 1500.5 paise is meaningless."""
        response = post_intent(client, create_body(merchant, amount=1500.5))

        assert response.status_code == 422

    def test_non_alphabetic_currency(self, client: TestClient, merchant: Account):
        response = post_intent(client, create_body(merchant, currency="1NR"))

        assert response.status_code == 422

    def test_unknown_fields_are_rejected(self, client: TestClient, merchant: Account):
        response = post_intent(client, create_body(merchant, capture_now=True))

        assert response.status_code == 422

    def test_missing_idempotency_key(self, client: TestClient, merchant: Account):
        response = post_intent(client, create_body(merchant), key=None)

        assert response.status_code == 400
        assert "Idempotency-Key header is required" in response.json()["detail"]

    def test_blank_idempotency_key(self, client: TestClient, merchant: Account):
        response = post_intent(client, create_body(merchant), key="   ")

        assert response.status_code == 400

    def test_oversized_idempotency_key(self, client: TestClient, merchant: Account):
        response = post_intent(client, create_body(merchant), key="x" * 256)

        assert response.status_code == 400
        assert "at most 255 characters" in response.json()["detail"]


class TestRead:
    def test_get_by_id(self, client: TestClient, merchant: Account):
        created = post_intent(client, create_body(merchant)).json()

        response = client.get(f"{CREATE_URL}/{created['id']}")

        assert response.status_code == 200
        assert response.json() == created

    def test_get_unknown_id(self, client: TestClient):
        response = client.get(f"{CREATE_URL}/{uuid.uuid4()}")

        assert response.status_code == 404
