"""Test fixtures.

These are integration tests on purpose. The ledger invariant lives in a
DEFERRABLE INITIALLY DEFERRED constraint trigger that only fires at COMMIT, so
a suite that rolls back every transaction would never actually exercise it.
Each test therefore commits for real against `TEST_DATABASE_URL`, and teardown
TRUNCATEs (which bypasses the append-only row trigger by design).
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Account, AccountType

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TABLES = ("ledger_entries", "transactions", "accounts")


def _test_database_url() -> str:
    return os.getenv("TEST_DATABASE_URL") or get_settings().test_database_url


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    url = _test_database_url()
    engine = create_engine(url, future=True)

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment problem, not a bug
        pytest.exit(
            f"cannot reach the test database at {url!r}: {exc}\n"
            "Start it with:  docker compose up -d db",
            returncode=1,
        )

    # Migrations are the schema under test -- including the triggers -- so the
    # suite runs them rather than create_all().
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        check=True,
        env={**os.environ, "ALEMBIC_DATABASE_URL": url},
    )

    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with engine.begin() as connection:
            connection.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))


def make_account(
    session: Session,
    *,
    name: str | None = None,
    type: AccountType = AccountType.ASSET,
    currency: str = "INR",
) -> Account:
    """Persist an account with a collision-proof name."""
    account = Account(
        name=name or f"{type.value}-{uuid.uuid4().hex[:8]}",
        type=type,
        currency=currency,
    )
    session.add(account)
    session.commit()
    return account


@pytest.fixture
def cash(session: Session) -> Account:
    """Asset account: the money the platform is holding."""
    return make_account(session, name="assets:cash", type=AccountType.ASSET)


@pytest.fixture
def payable(session: Session) -> Account:
    """Liability account: money owed out to merchants."""
    return make_account(session, name="liabilities:merchant_payable", type=AccountType.LIABILITY)


@pytest.fixture
def revenue(session: Session) -> Account:
    """Revenue account: platform fees earned."""
    return make_account(session, name="revenue:platform_fees", type=AccountType.REVENUE)
