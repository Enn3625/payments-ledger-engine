"""The idempotency protocol itself: replay, conflict, races and recovery."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.payment_intents import ENDPOINT
from app.models import Account, IdempotencyKey, IdempotencyState, PaymentIntent
from app.schemas.payment_intent import PaymentIntentCreate
from app.services.idempotency import (
    MAX_CLAIM_ATTEMPTS,
    IdempotencyConflictError,
    InvalidIdempotencyKeyError,
    RequestInProgressError,
    _claim,
    canonical_request_hash,
    run_idempotent,
    validate_key,
)
from app.services.payment_intents import create_payment_intent
from tests.test_payment_intents import create_body, post_intent


def hashed(body: dict) -> str:
    """The hash the endpoint will compute for this request body."""
    return canonical_request_hash(PaymentIntentCreate(**body).model_dump(mode="json"))


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


class TestRequestHashing:
    def test_key_order_and_whitespace_do_not_matter(self):
        assert canonical_request_hash({"a": 1, "b": 2}) == canonical_request_hash({"b": 2, "a": 1})

    def test_any_value_change_changes_the_hash(self):
        assert canonical_request_hash({"amount": 1000}) != canonical_request_hash({"amount": 1001})

    def test_missing_and_null_fields_are_distinguishable(self):
        assert canonical_request_hash({"reference": None}) != canonical_request_hash({})


class TestKeyValidation:
    def test_key_is_trimmed(self):
        assert validate_key("  key-1  ") == "key-1"

    @pytest.mark.parametrize("key", [None, "", "   "])
    def test_empty_keys_are_rejected(self, key):
        with pytest.raises(InvalidIdempotencyKeyError, match="required"):
            validate_key(key)


class TestReplay:
    def test_duplicate_request_returns_the_original_response(
        self, client: TestClient, session: Session, merchant: Account
    ):
        body = create_body(merchant)

        first = post_intent(client, body, key="order-42")
        second = post_intent(client, body, key="order-42")

        assert first.status_code == 201
        assert second.status_code == 201  # the *original* status, replayed verbatim
        assert second.json() == first.json()
        assert second.headers["Idempotent-Replay"] == "true"
        assert count(session, PaymentIntent) == 1

    def test_replay_survives_many_retries(
        self, client: TestClient, session: Session, merchant: Account
    ):
        body = create_body(merchant)
        responses = [post_intent(client, body, key="order-43") for _ in range(5)]

        assert {r.status_code for r in responses} == {201}
        assert len({r.json()["id"] for r in responses}) == 1
        assert count(session, PaymentIntent) == 1

    def test_semantically_identical_bodies_replay(
        self, client: TestClient, session: Session, merchant: Account
    ):
        """Reordered JSON keys are the same request, so this is a replay."""
        body = create_body(merchant)
        reordered = dict(reversed(list(body.items())))

        first = post_intent(client, body, key="order-44")
        second = post_intent(client, reordered, key="order-44")

        assert second.json()["id"] == first.json()["id"]
        assert count(session, PaymentIntent) == 1

    def test_a_different_key_creates_a_second_intent(
        self, client: TestClient, session: Session, merchant: Account
    ):
        """Idempotency is scoped to the key, not to the payload."""
        body = create_body(merchant)

        first = post_intent(client, body, key="order-45")
        second = post_intent(client, body, key="order-46")

        assert first.json()["id"] != second.json()["id"]
        assert count(session, PaymentIntent) == 2


class TestConflict:
    def test_same_key_different_body_is_rejected(
        self, client: TestClient, session: Session, merchant: Account
    ):
        first = post_intent(client, create_body(merchant), key="order-47")
        second = post_intent(client, create_body(merchant, amount=999_999), key="order-47")

        assert first.status_code == 201
        assert second.status_code == 409
        assert "already used for a different request" in second.json()["detail"]
        assert count(session, PaymentIntent) == 1

    def test_conflict_does_not_disturb_the_stored_response(
        self, client: TestClient, merchant: Account
    ):
        body = create_body(merchant)
        first = post_intent(client, body, key="order-48")
        post_intent(client, create_body(merchant, amount=1), key="order-48")

        replay = post_intent(client, body, key="order-48")

        assert replay.status_code == 201
        assert replay.json() == first.json()

    def test_in_flight_request_returns_409(
        self, client: TestClient, session: Session, merchant: Account
    ):
        """A concurrent duplicate is told to retry, not served a partial answer."""
        body = create_body(merchant)
        session.add(
            IdempotencyKey(
                key="order-49",
                endpoint=ENDPOINT,
                request_hash=hashed(body),
                state=IdempotencyState.IN_PROGRESS,
            )
        )
        session.commit()

        response = post_intent(client, body, key="order-49")

        assert response.status_code == 409
        assert "already in progress" in response.json()["detail"]
        assert count(session, PaymentIntent) == 0

    def test_keys_are_scoped_per_endpoint(
        self, session_factory: sessionmaker[Session], merchant: Account
    ):
        """The same key on another operation is a different request, not a clash."""
        payload = {"amount": 1_000}
        calls: list[str] = []

        def handler(session: Session) -> tuple[int, dict, None]:
            calls.append("ran")
            return 200, {"ok": True}, None

        for endpoint in ("POST /payment-intents", "POST /refunds"):
            run_idempotent(
                session_factory,
                key="shared-key",
                endpoint=endpoint,
                payload=payload,
                handler=handler,
            )

        assert len(calls) == 2


class TestFailureRecovery:
    def test_failed_work_releases_the_key(
        self, client: TestClient, session: Session, merchant: Account
    ):
        """A rejected request must not burn the key -- the client can retry it."""
        bad_body = create_body(merchant, merchant_account_id=str(uuid.uuid4()))

        failed = post_intent(client, bad_body, key="order-50")

        assert failed.status_code == 422
        assert count(session, IdempotencyKey) == 0

        retried = post_intent(client, create_body(merchant), key="order-50")

        assert retried.status_code == 201
        assert count(session, PaymentIntent) == 1

    def test_the_stored_error_is_not_replayed(
        self, session_factory: sessionmaker[Session], merchant: Account
    ):
        attempts: list[int] = []

        def flaky_handler(session: Session) -> tuple[int, dict, None]:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("downstream blew up")
            return 200, {"ok": True}, None

        with pytest.raises(RuntimeError):
            run_idempotent(
                session_factory,
                key="order-51",
                endpoint=ENDPOINT,
                payload={"amount": 1},
                handler=flaky_handler,
            )

        result = run_idempotent(
            session_factory,
            key="order-51",
            endpoint=ENDPOINT,
            payload={"amount": 1},
            handler=flaky_handler,
        )

        assert len(attempts) == 2
        assert result.replayed is False
        assert result.body == {"ok": True}

    def test_a_stale_claim_is_taken_over(
        self, client: TestClient, session: Session, merchant: Account
    ):
        """A claim from a process that died must not lock the key forever."""
        body = create_body(merchant)
        session.add(
            IdempotencyKey(
                key="order-52",
                endpoint=ENDPOINT,
                request_hash=hashed(body),
                state=IdempotencyState.IN_PROGRESS,
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        session.commit()

        response = post_intent(client, body, key="order-52")

        assert response.status_code == 201
        assert response.headers["Idempotent-Replay"] == "false"
        assert count(session, PaymentIntent) == 1


class TestClaimRecovery:
    """The two paths that only a real race would otherwise reach."""

    def test_key_released_between_insert_and_lookup_is_reclaimed(
        self, session_factory: sessionmaker[Session], session: Session
    ):
        """We lose the INSERT race, then the winner releases the key before we look."""
        session.add(
            IdempotencyKey(
                key="order-55",
                endpoint=ENDPOINT,
                request_hash=canonical_request_hash({"amount": 1}),
                state=IdempotencyState.IN_PROGRESS,
            )
        )
        session.commit()

        class ReleasingFactory:
            """Deletes the blocking row just before the conflict is inspected."""

            def __init__(self, inner: sessionmaker[Session]) -> None:
                self.inner = inner
                self.calls = 0

            def __call__(self) -> Session:
                self.calls += 1
                if self.calls == 2:
                    with self.inner() as cleanup:
                        cleanup.query(IdempotencyKey).delete()
                        cleanup.commit()
                return self.inner()

        result = run_idempotent(
            ReleasingFactory(session_factory),
            key="order-55",
            endpoint=ENDPOINT,
            payload={"amount": 1},
            handler=lambda _: (200, {"ok": True}, None),
        )

        assert result.replayed is False
        assert result.body == {"ok": True}

    def test_endless_contention_gives_up_rather_than_spinning(
        self, session_factory: sessionmaker[Session]
    ):
        with pytest.raises(RequestInProgressError, match="after 3 attempts"):
            _claim(
                session_factory,
                key="order-56",
                endpoint=ENDPOINT,
                request_hash="deadbeef",
                claim_timeout_seconds=60,
                attempt=MAX_CLAIM_ATTEMPTS + 1,
            )


class TestConcurrency:
    def test_only_one_of_many_simultaneous_duplicates_does_the_work(
        self, session_factory: sessionmaker[Session], session: Session, merchant: Account
    ):
        executions: list[int] = []
        payload = {"amount": 5_000, "merchant_account_id": str(merchant.id)}

        def handler(worker_session: Session) -> tuple[int, dict, uuid.UUID]:
            executions.append(1)
            time.sleep(0.2)  # widen the window every duplicate has to race through
            intent = create_payment_intent(
                worker_session,
                amount=5_000,
                currency="INR",
                merchant_account_id=merchant.id,
            )
            return 201, {"id": str(intent.id)}, intent.id

        def attempt(_: int) -> str:
            try:
                result = run_idempotent(
                    session_factory,
                    key="order-53",
                    endpoint=ENDPOINT,
                    payload=payload,
                    handler=handler,
                )
            except RequestInProgressError:
                return "in_progress"
            return "replayed" if result.replayed else "executed"

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))

        assert len(executions) == 1
        assert outcomes.count("executed") == 1
        assert set(outcomes) <= {"executed", "replayed", "in_progress"}

        session.expire_all()
        assert count(session, PaymentIntent) == 1
        assert count(session, IdempotencyKey) == 1

    def test_concurrent_conflicting_bodies_still_conflict(
        self, session_factory: sessionmaker[Session], session: Session, merchant: Account
    ):
        def handler(worker_session: Session) -> tuple[int, dict, uuid.UUID]:
            time.sleep(0.1)
            intent = create_payment_intent(
                worker_session,
                amount=1_000,
                currency="INR",
                merchant_account_id=merchant.id,
            )
            return 201, {"id": str(intent.id)}, intent.id

        def attempt(amount: int) -> str:
            try:
                run_idempotent(
                    session_factory,
                    key="order-54",
                    endpoint=ENDPOINT,
                    payload={"amount": amount},
                    handler=handler,
                )
            except IdempotencyConflictError:
                return "conflict"
            except RequestInProgressError:
                return "in_progress"
            return "ok"

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(attempt, [1_000, 2_000, 3_000, 4_000, 5_000, 6_000]))

        assert outcomes.count("ok") == 1
        assert "conflict" in outcomes or "in_progress" in outcomes

        session.expire_all()
        assert count(session, PaymentIntent) == 1
