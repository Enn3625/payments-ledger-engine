"""Authentication: hashing, tokens, login, and what each role may do."""

import base64
import json
import uuid

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User, UserRole
from app.services.auth import (
    InvalidCredentialsError,
    InvalidTokenError,
    PasswordTooLongError,
    authenticate_user,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from tests.conftest import make_user

LOGIN_URL = "/auth/login"
ME_URL = "/auth/me"


def login(client: TestClient, email: str, password: str):
    return client.post(LOGIN_URL, data={"username": email, "password": password})


class TestPasswordHashing:
    def test_the_password_is_not_recoverable_from_the_hash(self):
        hashed = hash_password("correct horse battery staple")

        assert "correct horse" not in hashed
        assert hashed.startswith("$2b$")

    def test_a_correct_password_verifies(self):
        assert verify_password("s3cret", hash_password("s3cret"))

    def test_a_wrong_password_does_not(self):
        assert not verify_password("wrong", hash_password("s3cret"))

    def test_the_same_password_hashes_differently_each_time(self):
        """Per-password salt: identical passwords must not look identical."""
        assert hash_password("same") != hash_password("same")

    def test_a_password_bcrypt_would_truncate_is_rejected(self):
        """Silently ignoring bytes past 72 would weaken long passwords."""
        with pytest.raises(PasswordTooLongError, match="72 bytes"):
            hash_password("x" * 73)

    def test_a_corrupt_stored_hash_denies_access(self):
        assert not verify_password("anything", "not-a-bcrypt-hash")


class TestTokens:
    def test_round_trip(self, admin_user: User):
        token, expires_in = create_access_token(admin_user, get_settings())
        claims = decode_access_token(token, get_settings())

        assert claims["sub"] == str(admin_user.id)
        assert claims["role"] == UserRole.ADMIN.value
        assert expires_in == get_settings().access_token_expire_minutes * 60

    def test_an_expired_token_is_refused(self, admin_user: User):
        expired = get_settings().model_copy(update={"access_token_expire_minutes": -1})
        token, _ = create_access_token(admin_user, expired)

        with pytest.raises(InvalidTokenError):
            decode_access_token(token, get_settings())

    def test_a_token_signed_with_another_secret_is_refused(self, admin_user: User):
        foreign = get_settings().model_copy(update={"jwt_secret": "someone-elses-secret"})
        token, _ = create_access_token(admin_user, foreign)

        with pytest.raises(InvalidTokenError):
            decode_access_token(token, get_settings())

    def test_a_tampered_payload_is_refused(self, admin_user: User):
        """Flip a claim and the signature no longer matches."""
        token, _ = create_access_token(admin_user, get_settings())
        header, payload, signature = token.split(".")
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
        decoded["role"] = UserRole.ADMIN.value
        decoded["sub"] = str(uuid.uuid4())
        forged = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")

        with pytest.raises(InvalidTokenError):
            decode_access_token(f"{header}.{forged}.{signature}", get_settings())

    def test_an_unsigned_token_is_refused(self, admin_user: User):
        """The classic `alg: none` downgrade. Pinning the algorithm stops it."""

        def segment(data: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

        unsigned = (
            segment({"alg": "none", "typ": "JWT"})
            + "."
            + segment({"sub": str(admin_user.id), "role": "admin", "exp": 9_999_999_999})
            + "."
        )

        with pytest.raises(InvalidTokenError):
            decode_access_token(unsigned, get_settings())

    def test_a_token_without_an_expiry_is_refused(self, admin_user: User):
        settings = get_settings()
        forever = jwt.encode(
            {"sub": str(admin_user.id), "role": "admin"},
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )

        with pytest.raises(InvalidTokenError):
            decode_access_token(forever, settings)

    def test_a_token_whose_subject_is_not_a_uuid_is_refused(self, client_factory, admin_user: User):
        """A validly signed token can still name a subject that cannot exist."""
        settings = get_settings()
        odd = jwt.encode(
            {"sub": "not-a-uuid", "role": "admin", "exp": 9_999_999_999},
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )

        assert client_factory(odd).get(ME_URL).status_code == 401

    def test_garbage_is_refused(self):
        with pytest.raises(InvalidTokenError):
            decode_access_token("not.a.token", get_settings())


class TestAuthenticateUser:
    def test_correct_credentials(self, session: Session, admin_user: User):
        found = authenticate_user(session, email="admin@example.com", password="admin-password")

        assert found.id == admin_user.id

    def test_email_is_case_insensitive(self, session: Session, admin_user: User):
        found = authenticate_user(session, email="  ADMIN@Example.COM ", password="admin-password")

        assert found.id == admin_user.id

    def test_wrong_password(self, session: Session, admin_user: User):
        with pytest.raises(InvalidCredentialsError):
            authenticate_user(session, email="admin@example.com", password="nope")

    def test_unknown_email(self, session: Session):
        with pytest.raises(InvalidCredentialsError):
            authenticate_user(session, email="ghost@example.com", password="whatever")

    def test_a_deactivated_account_cannot_log_in(self, session: Session, admin_user: User):
        admin_user.is_active = False
        session.commit()

        with pytest.raises(InvalidCredentialsError):
            authenticate_user(session, email="admin@example.com", password="admin-password")

    def test_every_failure_reads_the_same(self, session: Session, admin_user: User):
        """Wrong password, unknown user and disabled account are indistinguishable."""
        messages = set()
        for email, password in [
            ("admin@example.com", "wrong"),
            ("ghost@example.com", "admin-password"),
        ]:
            with pytest.raises(InvalidCredentialsError) as caught:
                authenticate_user(session, email=email, password=password)
            messages.add(str(caught.value))

        assert len(messages) == 1


class TestLoginEndpoint:
    def test_login_returns_a_usable_token(self, anonymous_client: TestClient, admin_user: User):
        response = login(anonymous_client, "admin@example.com", "admin-password")

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["role"] == UserRole.ADMIN.value
        assert body["expires_in"] > 0

        me = anonymous_client.get(
            ME_URL, headers={"Authorization": f"Bearer {body['access_token']}"}
        )
        assert me.status_code == 200
        assert me.json()["email"] == "admin@example.com"

    def test_wrong_password_is_rejected(self, anonymous_client: TestClient, admin_user: User):
        response = login(anonymous_client, "admin@example.com", "wrong")

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_unknown_email_looks_identical(self, anonymous_client: TestClient, admin_user: User):
        """No user enumeration: both answers are byte-for-byte the same."""
        wrong_password = login(anonymous_client, "admin@example.com", "wrong")
        no_such_user = login(anonymous_client, "ghost@example.com", "wrong")

        assert wrong_password.status_code == no_such_user.status_code == 401
        assert wrong_password.json() == no_such_user.json()

    def test_a_deactivated_user_is_rejected(
        self, anonymous_client: TestClient, session: Session, admin_user: User
    ):
        admin_user.is_active = False
        session.commit()

        assert login(anonymous_client, "admin@example.com", "admin-password").status_code == 401


class TestMeEndpoint:
    def test_requires_a_token(self, anonymous_client: TestClient):
        response = anonymous_client.get(ME_URL)

        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_rejects_a_bad_token(self, anonymous_client: TestClient):
        response = anonymous_client.get(ME_URL, headers={"Authorization": "Bearer nonsense"})

        assert response.status_code == 401

    def test_reports_the_viewer_role(self, viewer_client: TestClient):
        assert viewer_client.get(ME_URL).json()["role"] == UserRole.VIEWER.value


class TestTheDatabaseIsTheSourceOfTruth:
    """A token is a claim about the past; the row is the present."""

    def test_deactivating_a_user_invalidates_their_live_token(
        self, client: TestClient, session: Session, admin_user: User
    ):
        assert client.get(ME_URL).status_code == 200

        admin_user.is_active = False
        session.commit()

        assert client.get(ME_URL).status_code == 401

    def test_demoting_an_admin_takes_effect_immediately(
        self, client: TestClient, session: Session, admin_user: User, merchant
    ):
        """The token still says `role: admin`. The database says otherwise."""
        admin_user.role = UserRole.VIEWER
        session.commit()

        response = client.post(
            "/payment-intents",
            json={"amount": 1_000, "merchant_account_id": str(merchant.id)},
            headers={"Idempotency-Key": "demoted-1"},
        )

        assert response.status_code == 403

    def test_a_token_for_a_deleted_user_is_refused(
        self, session: Session, client_factory, viewer_user: User
    ):
        token, _ = create_access_token(viewer_user, get_settings())
        session.delete(viewer_user)
        session.commit()

        assert client_factory(token).get(ME_URL).status_code == 401


class TestRoleEnforcement:
    """Admin writes, viewer reads, anonymous does neither."""

    @pytest.fixture
    def intent_id(self, session: Session, merchant) -> str:
        from app.services.payment_intents import create_payment_intent

        intent = create_payment_intent(
            session, amount=1_000, currency="INR", merchant_account_id=merchant.id
        )
        session.commit()
        return str(intent.id)

    def test_anonymous_cannot_create_an_intent(self, anonymous_client: TestClient, merchant):
        response = anonymous_client.post(
            "/payment-intents",
            json={"amount": 1_000, "merchant_account_id": str(merchant.id)},
            headers={"Idempotency-Key": "anon-1"},
        )

        assert response.status_code == 401

    def test_viewer_cannot_create_an_intent(self, viewer_client: TestClient, merchant):
        response = viewer_client.post(
            "/payment-intents",
            json={"amount": 1_000, "merchant_account_id": str(merchant.id)},
            headers={"Idempotency-Key": "viewer-1"},
        )

        assert response.status_code == 403
        assert "admin role" in response.json()["detail"]

    def test_admin_can_create_an_intent(self, client: TestClient, merchant):
        response = client.post(
            "/payment-intents",
            json={"amount": 1_000, "merchant_account_id": str(merchant.id)},
            headers={"Idempotency-Key": "admin-1"},
        )

        assert response.status_code == 201

    @pytest.mark.parametrize("path", ["/anomaly-flags", "/payment-intents/{intent}"])
    def test_reads_require_authentication(
        self, anonymous_client: TestClient, intent_id: str, path: str
    ):
        assert anonymous_client.get(path.format(intent=intent_id)).status_code == 401

    @pytest.mark.parametrize("path", ["/anomaly-flags", "/payment-intents/{intent}"])
    def test_a_viewer_may_read(self, viewer_client: TestClient, intent_id: str, path: str):
        assert viewer_client.get(path.format(intent=intent_id)).status_code == 200

    def test_a_rejected_write_leaves_nothing_behind(
        self, viewer_client: TestClient, session: Session, merchant
    ):
        """Authorisation is checked before any work, so no key is burned."""
        from sqlalchemy import func, select

        from app.models import IdempotencyKey, PaymentIntent

        viewer_client.post(
            "/payment-intents",
            json={"amount": 1_000, "merchant_account_id": str(merchant.id)},
            headers={"Idempotency-Key": "viewer-2"},
        )

        assert session.scalar(select(func.count()).select_from(PaymentIntent)) == 0
        assert session.scalar(select(func.count()).select_from(IdempotencyKey)) == 0


class TestWebhookRouteStaysPublic:
    def test_the_provider_needs_no_bearer_token(
        self, anonymous_client: TestClient, session: Session, chart_of_accounts, merchant
    ):
        """The HMAC signature is the provider's authentication."""
        import json as json_module

        from app.services.payment_intents import create_payment_intent
        from app.services.signatures import SIGNATURE_HEADER, sign_payload

        intent = create_payment_intent(
            session, amount=1_000, currency="INR", merchant_account_id=merchant.id
        )
        session.commit()

        body = {
            "id": "evt_public",
            "type": "payment.captured",
            "data": {
                "payment_intent_id": str(intent.id),
                "amount": 1_000,
                "currency": "INR",
            },
        }
        raw = json_module.dumps(body).encode()

        response = anonymous_client.post(
            "/webhooks/payment-events",
            content=raw,
            headers={
                SIGNATURE_HEADER: sign_payload(raw, get_settings().webhook_secret),
                "Content-Type": "application/json",
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "processed"

    def test_health_needs_no_token(self, anonymous_client: TestClient):
        assert anonymous_client.get("/health").status_code == 200


class TestWebhookRetryIsAdminOnly:
    @pytest.fixture
    def failed_event_id(self, client: TestClient, session: Session, merchant) -> str:
        """A capture with no cash account in the chart: fails, and is retryable."""
        import json as json_module

        from app.services.payment_intents import create_payment_intent
        from app.services.signatures import SIGNATURE_HEADER, sign_payload

        intent = create_payment_intent(
            session, amount=1_000, currency="INR", merchant_account_id=merchant.id
        )
        session.commit()

        body = {
            "id": "evt_retryable",
            "type": "payment.captured",
            "data": {
                "payment_intent_id": str(intent.id),
                "amount": 1_000,
                "currency": "INR",
            },
        }
        raw = json_module.dumps(body).encode()
        client.post(
            "/webhooks/payment-events",
            content=raw,
            headers={
                SIGNATURE_HEADER: sign_payload(raw, get_settings().webhook_secret),
                "Content-Type": "application/json",
            },
        )
        return "evt_retryable"

    def test_anonymous_cannot_retry(self, anonymous_client: TestClient, failed_event_id: str):
        response = anonymous_client.post(f"/webhooks/events/{failed_event_id}/retry")

        assert response.status_code == 401

    def test_viewer_cannot_retry(self, viewer_client: TestClient, failed_event_id: str):
        response = viewer_client.post(f"/webhooks/events/{failed_event_id}/retry")

        assert response.status_code == 403

    def test_admin_can_retry(self, client: TestClient, session: Session, failed_event_id: str):
        from tests.conftest import make_account

        make_account(session, name="assets:cash")

        response = client.post(f"/webhooks/events/{failed_event_id}/retry")

        assert response.status_code == 200
        assert response.json()["status"] == "processed"
        assert response.json()["duplicate"] is False

    def test_retrying_an_unknown_event_is_a_404(self, client: TestClient):
        assert client.post("/webhooks/events/evt_nope/retry").status_code == 404

    def test_retrying_a_still_broken_event_is_a_422(self, client: TestClient, failed_event_id: str):
        assert client.post(f"/webhooks/events/{failed_event_id}/retry").status_code == 422


class TestUserModel:
    def test_emails_are_stored_lowercase(self, session: Session):
        user = make_user(session, email="  MiXeD@Example.COM ", password="pw", role=UserRole.VIEWER)

        assert user.email == "mixed@example.com"

    def test_is_admin_helper(self, admin_user: User, viewer_user: User):
        assert admin_user.is_admin
        assert not viewer_user.is_admin
