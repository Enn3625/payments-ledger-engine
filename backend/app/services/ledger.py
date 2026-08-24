"""Ledger posting and balance calculation.

The double-entry invariant (sum of debits == sum of credits, per transaction)
is enforced twice, on purpose:

1. Here, in the service layer, so callers get a fast, readable error before a
   round trip to the database.
2. In PostgreSQL, by a DEFERRABLE INITIALLY DEFERRED constraint trigger that
   re-checks at COMMIT time (see the 0001 migration). That one is the actual
   guarantee -- it holds even for code paths, scripts or psql sessions that
   never go through this module.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    NORMAL_DEBIT_TYPES,
    Account,
    AccountType,
    EntryDirection,
    LedgerEntry,
    Transaction,
    TransactionStatus,
)

MIN_ENTRIES_PER_TRANSACTION = 2


class LedgerError(Exception):
    """Base class for ledger rule violations."""


class UnbalancedTransactionError(LedgerError):
    """Debits and credits do not sum to the same value."""


class InvalidEntryError(LedgerError):
    """An individual entry is malformed (bad amount, too few entries, ...)."""


@dataclass(frozen=True, slots=True)
class EntryDraft:
    """One side of a posting, before it is persisted."""

    account_id: uuid.UUID
    amount: int  # minor units, strictly positive
    direction: EntryDirection


def debit(account_id: uuid.UUID, amount: int) -> EntryDraft:
    return EntryDraft(account_id=account_id, amount=amount, direction=EntryDirection.DEBIT)


def credit(account_id: uuid.UUID, amount: int) -> EntryDraft:
    return EntryDraft(account_id=account_id, amount=amount, direction=EntryDirection.CREDIT)


def validate_entries(entries: Sequence[EntryDraft]) -> None:
    """Raise unless `entries` form a well-formed, balanced posting."""
    if len(entries) < MIN_ENTRIES_PER_TRANSACTION:
        raise InvalidEntryError(
            f"a transaction needs at least {MIN_ENTRIES_PER_TRANSACTION} entries, "
            f"got {len(entries)}"
        )

    for entry in entries:
        if not isinstance(entry.amount, int) or isinstance(entry.amount, bool):
            raise InvalidEntryError(f"amount must be an int of minor units, got {entry.amount!r}")
        if entry.amount <= 0:
            raise InvalidEntryError(
                f"amount must be positive (direction carries the sign), got {entry.amount}"
            )

    debits = sum(e.amount for e in entries if e.direction is EntryDirection.DEBIT)
    credits = sum(e.amount for e in entries if e.direction is EntryDirection.CREDIT)
    if debits != credits:
        raise UnbalancedTransactionError(
            f"unbalanced transaction: debits={debits} credits={credits} "
            f"(difference={debits - credits})"
        )


def post_transaction(
    session: Session,
    *,
    description: str,
    entries: Sequence[EntryDraft],
    currency: str | None = None,
    transaction_id: uuid.UUID | None = None,
) -> Transaction:
    """Write a balanced transaction and its entries. Flushes, does not commit.

    Committing is the caller's job so a posting can join a larger unit of work
    (a webhook handler updating its event log, for instance).
    """
    validate_entries(entries)

    resolved_currency = currency or _infer_currency(session, entries)

    transaction = Transaction(
        id=transaction_id or uuid.uuid4(),
        description=description,
        status=TransactionStatus.POSTED,
        currency=resolved_currency,
        posted_at=datetime.now(UTC),
    )
    session.add(transaction)
    session.add_all(
        LedgerEntry(
            transaction_id=transaction.id,
            account_id=entry.account_id,
            amount=entry.amount,
            direction=entry.direction,
        )
        for entry in entries
    )
    session.flush()
    return transaction


def _infer_currency(session: Session, entries: Sequence[EntryDraft]) -> str:
    """All accounts in one posting must share a currency (single-currency ledger)."""
    account_ids = {entry.account_id for entry in entries}
    currencies = set(
        session.scalars(select(Account.currency).where(Account.id.in_(account_ids))).all()
    )
    if not currencies:
        raise InvalidEntryError(f"no such accounts: {sorted(map(str, account_ids))}")
    if len(currencies) > 1:
        raise InvalidEntryError(f"cross-currency posting is not supported: {sorted(currencies)}")
    return currencies.pop()


def account_balance(session: Session, account_id: uuid.UUID) -> int:
    """Balance in minor units, signed by the account's normal balance side."""
    account = session.get(Account, account_id)
    if account is None:
        raise InvalidEntryError(f"no such account: {account_id}")

    totals = session.execute(
        select(LedgerEntry.direction, func.coalesce(func.sum(LedgerEntry.amount), 0))
        .where(LedgerEntry.account_id == account_id)
        .group_by(LedgerEntry.direction)
    ).all()
    by_direction = {direction: total for direction, total in totals}
    debits = by_direction.get(EntryDirection.DEBIT, 0)
    credits = by_direction.get(EntryDirection.CREDIT, 0)

    if account.type in NORMAL_DEBIT_TYPES:
        return debits - credits
    return credits - debits


@dataclass(frozen=True, slots=True)
class TrialBalance:
    """Ledger-wide totals. In a healthy ledger the two sides are equal."""

    total_debits: int
    total_credits: int

    @property
    def is_balanced(self) -> bool:
        return self.total_debits == self.total_credits


@dataclass(frozen=True, slots=True)
class AccountBalance:
    """One row of the balances view."""

    account_id: uuid.UUID
    name: str
    type: AccountType
    currency: str
    debits: int
    credits: int
    #: Signed by the normal balance side, so a healthy account reads positive.
    balance: int
    entry_count: int


def account_balances(session: Session) -> list[AccountBalance]:
    """Every account with its totals, in one grouped query rather than N+1."""
    totals = session.execute(
        select(
            Account,
            func.coalesce(
                func.sum(LedgerEntry.amount).filter(LedgerEntry.direction == EntryDirection.DEBIT),
                0,
            ),
            func.coalesce(
                func.sum(LedgerEntry.amount).filter(LedgerEntry.direction == EntryDirection.CREDIT),
                0,
            ),
            func.count(LedgerEntry.id),
        )
        .outerjoin(LedgerEntry, LedgerEntry.account_id == Account.id)
        .group_by(Account.id)
        .order_by(Account.name)
    ).all()

    balances = []
    for account, debits, credits, entry_count in totals:
        signed = debits - credits if account.type in NORMAL_DEBIT_TYPES else credits - debits
        balances.append(
            AccountBalance(
                account_id=account.id,
                name=account.name,
                type=account.type,
                currency=account.currency,
                debits=debits,
                credits=credits,
                balance=signed,
                entry_count=entry_count,
            )
        )
    return balances


def trial_balance(session: Session) -> TrialBalance:
    totals = session.execute(
        select(LedgerEntry.direction, func.coalesce(func.sum(LedgerEntry.amount), 0)).group_by(
            LedgerEntry.direction
        )
    ).all()
    by_direction = {direction: total for direction, total in totals}
    return TrialBalance(
        total_debits=by_direction.get(EntryDirection.DEBIT, 0),
        total_credits=by_direction.get(EntryDirection.CREDIT, 0),
    )
