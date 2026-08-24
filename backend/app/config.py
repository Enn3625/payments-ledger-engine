"""Application settings, loaded from environment / backend/.env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    webhook_secret: str = "whsec_local_dev_secret"
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
    jwt_secret: str = "jwt_local_dev_secret_change_me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Browsers block cross-origin API calls unless the server opts in, and the
    # dashboard is served from a different port in development. Comma-separated
    # so it stays a plain environment variable.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
