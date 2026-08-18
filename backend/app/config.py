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


@lru_cache
def get_settings() -> Settings:
    return Settings()
