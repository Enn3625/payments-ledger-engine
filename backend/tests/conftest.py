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
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Account, AccountType

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TABLES = (
    "webhook_events",
    "idempotency_keys",
    "payment_intents",
    "ledger_entries",
    "transactions",
    "accounts",
)


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
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture(autouse=True)
def _clean_database(engine: Engine) -> Iterator[None]:
    """Wipe every table after each test.

    Autouse and last to tear down, so it runs after any session fixture has
    closed its transaction -- TRUNCATE would otherwise block on those locks.
    """
    yield
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """HTTP client wired to the test database instead of the dev database."""
    from app.db import get_session, get_session_factory
    from app.main import app

    def override_factory() -> sessionmaker[Session]:
        return session_factory

    def override_session() -> Iterator[Session]:
        request_session = session_factory()
        try:
            yield request_session
            request_session.commit()
        except Exception:
            request_session.rollback()
            raise
        finally:
            request_session.close()

    app.dependency_overrides[get_session_factory] = override_factory
    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


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


@pytest.fixture
def chart_of_accounts(cash: Account, payable: Account, revenue: Account) -> dict[str, Account]:
    """The accounts a capture posting needs, under their configured names."""
    return {"cash": cash, "payable": payable, "revenue": revenue}


@pytest.fixture
def merchant(payable: Account) -> Account:
    """The account a payment intent settles into."""
    return payable
