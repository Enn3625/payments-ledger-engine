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


@lru_cache
def get_settings() -> Settings:
    return Settings()
