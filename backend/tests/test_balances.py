"""Balance arithmetic: each account type accumulates on its normal side."""

import pytest
from sqlalchemy.orm import Session

from app.models import Account, AccountType
from app.services.ledger import (
    InvalidEntryError,
    account_balance,
    credit,
    debit,
    post_transaction,
    trial_balance,
)
from tests.conftest import make_account


def test_empty_account_has_zero_balance(session: Session, cash: Account):
    assert account_balance(session, cash.id) == 0


def test_asset_account_grows_on_debits(session: Session, cash: Account, payable: Account):
    post_transaction(
        session,
        description="settlement received",
        entries=[debit(cash.id, 250_000), credit(payable.id, 250_000)],
    )
    session.commit()

    assert account_balance(session, cash.id) == 250_000


def test_liability_account_grows_on_credits(session: Session, cash: Account, payable: Account):
    post_transaction(
        session,
        description="merchant payable accrued",
        entries=[debit(cash.id, 250_000), credit(payable.id, 250_000)],
    )
    session.commit()

    assert account_balance(session, payable.id) == 250_000


def test_payout_reduces_both_sides(session: Session, cash: Account, payable: Account):
    post_transaction(
        session,
        description="capture",
        entries=[debit(cash.id, 100_000), credit(payable.id, 100_000)],
    )
    post_transaction(
        session,
        description="payout to merchant",
        entries=[debit(payable.id, 40_000), credit(cash.id, 40_000)],
    )
    session.commit()

    assert account_balance(session, cash.id) == 60_000
    assert account_balance(session, payable.id) == 60_000
    assert trial_balance(session).is_balanced


@pytest.mark.parametrize(
    ("account_type", "expected"),
    [
        (AccountType.ASSET, 1_000),
        (AccountType.EXPENSE, 1_000),
        (AccountType.LIABILITY, -1_000),
        (AccountType.EQUITY, -1_000),
        (AccountType.REVENUE, -1_000),
    ],
)
def test_normal_balance_side_per_account_type(
    session: Session, payable: Account, account_type: AccountType, expected: int
):
    """A debit of 1,000 raises debit-normal accounts and lowers credit-normal ones."""
    account = make_account(session, type=account_type)
    post_transaction(
        session,
        description=f"debit a {account_type.value} account",
        entries=[debit(account.id, 1_000), credit(payable.id, 1_000)],
    )
    session.commit()

    assert account_balance(session, account.id) == expected


def test_fee_split_lands_in_the_right_accounts(
    session: Session, cash: Account, payable: Account, revenue: Account
):
    post_transaction(
        session,
        description="capture INR 1,000.00 with INR 20.00 fee",
        entries=[
            debit(cash.id, 100_000),
            credit(payable.id, 98_000),
            credit(revenue.id, 2_000),
        ],
    )
    session.commit()

    assert account_balance(session, cash.id) == 100_000
    assert account_balance(session, payable.id) == 98_000
    assert account_balance(session, revenue.id) == 2_000

    balance = trial_balance(session)
    assert balance.total_debits == balance.total_credits == 100_000


def test_unknown_account_raises(session: Session):
    import uuid

    with pytest.raises(InvalidEntryError, match="no such account"):
        account_balance(session, uuid.uuid4())
