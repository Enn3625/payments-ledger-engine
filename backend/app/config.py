"""Application settings, loaded from environment / backend/.env."""

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Fine for local development, fatal anywhere else. Anyone holding these can
#: forge a webhook or mint an admin token.
DEV_WEBHOOK_SECRET = "whsec_local_dev_secret"
DEV_JWT_SECRET = "jwt_local_dev_secret_change_me"

#: Environments where the dev defaults are acceptable.
UNSAFE_SECRETS_ALLOWED_IN = frozenset({"local", "test", "ci"})


class UnsafeConfigurationError(RuntimeError):
    """A deployed environment is trying to boot with development secrets."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://ledger:ledger@localhost:55432/ledger"
    test_database_url: str = "postgresql+psycopg://ledger:ledger@localhost:55432/ledger_test"
    environment: str = "local"

    # Ledger-wide defaults. Amounts are stored as integer minor units
    # (paise for INR), never floats -- see app/models/ledger_entry.py.
    base_currency: str = "INR"

    # An idempotency claim older than this belongs to a request that died
    # mid-flight; the next caller with the same key takes it over.
    idempotency_claim_timeout_seconds: int = 60

    # Shared secret the payment provider signs webhook payloads with. Override
    # in every deployed environment -- this default is for local dev only.
    webhook_secret: str = DEV_WEBHOOK_SECRET
    # Signatures older than this are refused, so a captured payload cannot be
    # replayed days later even though its HMAC is still valid.
    webhook_timestamp_tolerance_seconds: int = 300

    # Well-known accounts the capture posting touches. Created by the seed
    # script; overridable so a deployment can use its own naming.
    cash_account_name: str = "assets:cash"
    fee_revenue_account_name: str = "revenue:platform_fees"

    # Anomaly rules. Thresholds are configuration, not code, so tuning them
    # never means a redeploy of the rule engine.
    # Flag a single capture above INR 5,000.00 (in paise).
    anomaly_amount_threshold: int = 500_000
    # Flag more than N captures against one account inside the window.
    anomaly_velocity_max_captures: int = 5
    anomaly_velocity_window_minutes: int = 10

    # JWT signing. Override in every deployed environment: anyone holding this
    # value can mint a valid admin token.
    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Browsers block cross-origin API calls unless the server opts in, and the
    # dashboard is served from a different port in development. Comma-separated
    # so it stays a plain environment variable.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_deployed(self) -> bool:
        return self.environment.strip().lower() not in UNSAFE_SECRETS_ALLOWED_IN

    @field_validator("database_url", "test_database_url", mode="after")
    @classmethod
    def _use_psycopg_driver(cls, url: str) -> str:
        """Normalise the scheme managed Postgres providers hand out.

        Render and Railway supply `postgres://` or `postgresql://`. SQLAlchemy
        resolves both to psycopg2, which is not installed, and the failure looks
        like a missing package rather than a URL problem.
        """
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url

    @model_validator(mode="after")
    def _refuse_dev_secrets_when_deployed(self) -> "Settings":
        if not self.is_deployed:
            return self

        weak = [
            name
            for name, value, default in (
                ("WEBHOOK_SECRET", self.webhook_secret, DEV_WEBHOOK_SECRET),
                ("JWT_SECRET", self.jwt_secret, DEV_JWT_SECRET),
            )
            if value == default
        ]
        if weak:
            raise UnsafeConfigurationError(
                f"ENVIRONMENT={self.environment!r} but {' and '.join(weak)} still "
                "hold the development default. Set real secrets before deploying; "
                "anyone with these values can forge a webhook or mint an admin token."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
