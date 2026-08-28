"""Deployment safety: settings refuse unsafe configuration, and the seed is honest."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import (
    DEV_JWT_SECRET,
    DEV_WEBHOOK_SECRET,
    Settings,
    UnsafeConfigurationError,
)
from app.models import (
    Account,
    AnomalyFlag,
    LedgerEntry,
    PaymentIntent,
    PaymentIntentStatus,
    Transaction,
    User,
    WebhookEvent,
    WebhookEventStatus,
)
from app.services.ledger import trial_balance
from scripts import seed_demo


class TestDatabaseUrlNormalisation:
    @pytest.mark.parametrize(
        "supplied",
        [
            "postgres://u:p@host:5432/db",
            "postgresql://u:p@host:5432/db",
        ],
    )
    def test_managed_provider_urls_get_the_psycopg_driver(self, supplied: str):
        """Render and Railway hand out URLs SQLAlchemy would route to psycopg2."""
        assert Settings(database_url=supplied).database_url == (
            "postgresql+psycopg://u:p@host:5432/db"
        )

    def test_an_explicit_driver_is_left_alone(self):
        url = "postgresql+psycopg://u:p@host:5432/db"

        assert Settings(database_url=url).database_url == url

    def test_the_test_database_url_is_normalised_too(self):
        assert Settings(test_database_url="postgres://u:p@h/db").test_database_url.startswith(
            "postgresql+psycopg://"
        )


class TestSecretsGate:
    """A convenient default that reaches production is a vulnerability."""

    def test_local_may_use_the_development_secrets(self):
        settings = Settings(environment="local")

        assert settings.webhook_secret == DEV_WEBHOOK_SECRET
        assert settings.is_deployed is False

    @pytest.mark.parametrize("environment", ["production", "staging", "prod", "PRODUCTION"])
    def test_a_deployed_environment_refuses_them(self, environment: str):
        with pytest.raises(UnsafeConfigurationError) as caught:
            Settings(environment=environment)

        assert "WEBHOOK_SECRET" in str(caught.value)
        assert "JWT_SECRET" in str(caught.value)

    def test_it_names_only_the_secret_still_at_its_default(self):
        with pytest.raises(UnsafeConfigurationError) as caught:
            Settings(environment="production", webhook_secret="whsec_real_secret")

        message = str(caught.value)
        assert "JWT_SECRET" in message
        assert "WEBHOOK_SECRET" not in message

    def test_real_secrets_boot_fine(self):
        settings = Settings(
            environment="production",
            webhook_secret="whsec_real_secret",
            jwt_secret="a-real-jwt-secret",
        )

        assert settings.is_deployed is True

    def test_ci_and_test_are_treated_as_local(self):
        for environment in ("test", "ci"):
            assert Settings(environment=environment).is_deployed is False

    def test_the_dev_defaults_are_what_the_gate_checks(self):
        """Guards against someone changing a default and silently disarming this."""
        settings = Settings()

        assert settings.webhook_secret == DEV_WEBHOOK_SECRET
        assert settings.jwt_secret == DEV_JWT_SECRET


class TestDemoSeed:
    """The demo dataset must be produced by the system, not staged to look like it."""

    @pytest.fixture
    def seeded(self, session_factory: sessionmaker[Session], session: Session):
        seed_demo.seed(session_factory, "seed-admin-pw", "seed-viewer-pw")
        session.expire_all()
        return session

    def test_the_seeded_ledger_balances(self, seeded: Session):
        totals = trial_balance(seeded)

        assert totals.is_balanced
        assert totals.total_debits > 0

    def test_it_creates_the_accounts_captures_need(self, seeded: Session):
        names = set(seeded.scalars(select(Account.name)).all())

        assert seed_demo.CASH in names
        assert seed_demo.FEES in names
        assert len(names) == 2 + len(seed_demo.MERCHANTS)

    def test_it_creates_one_admin_and_one_viewer(self, seeded: Session):
        users = {user.email: user.role.value for user in seeded.scalars(select(User)).all()}

        assert users[seed_demo.DEMO_ADMIN_EMAIL] == "admin"
        assert users[seed_demo.DEMO_VIEWER_EMAIL] == "viewer"

    def test_it_produces_flagged_transactions(self, seeded: Session):
        rules = {flag.rule.value for flag in seeded.scalars(select(AnomalyFlag)).all()}

        assert rules == {"amount_threshold", "velocity"}

    def test_it_leaves_a_retryable_failed_delivery(self, seeded: Session):
        """So the dashboard's admin Retry button has something real to act on."""
        failed = seeded.scalars(
            select(WebhookEvent).where(WebhookEvent.status == WebhookEventStatus.FAILED)
        ).all()

        assert len(failed) == 1
        assert "does not match" in failed[0].last_error

    def test_it_includes_a_declined_payment_with_no_ledger_effect(self, seeded: Session):
        declined = seeded.scalars(
            select(PaymentIntent).where(PaymentIntent.status == PaymentIntentStatus.FAILED)
        ).all()

        assert len(declined) == 1
        assert declined[0].transaction_id is None

    def test_running_it_twice_changes_nothing(
        self, session_factory: sessionmaker[Session], session: Session, seeded: Session
    ):
        """Reproducible means re-runnable, not just deterministic on an empty database."""

        def counts() -> dict[str, int]:
            session.expire_all()
            return {
                model.__name__: session.scalar(select(func.count()).select_from(model))
                for model in (
                    Account,
                    Transaction,
                    LedgerEntry,
                    PaymentIntent,
                    WebhookEvent,
                    AnomalyFlag,
                    User,
                )
            }

        before = counts()
        seed_demo.seed(session_factory, "seed-admin-pw", "seed-viewer-pw")

        assert counts() == before
        assert trial_balance(session).is_balanced
