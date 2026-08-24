"""Read endpoints the dashboard is built on: balances and transactions.

Both are viewer-accessible. Nothing here can change the ledger -- the only
paths that write to it are a verified webhook and an admin retry.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.deps import CurrentUser
from app.db import get_session
from app.models import (
    AccountType,
    AnomalyFlag,
    AnomalyRule,
    EntryDirection,
    LedgerEntry,
    Transaction,
    TransactionStatus,
)
from app.services.ledger import account_balances, trial_balance

router = APIRouter(tags=["ledger"])


class AccountBalanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID
    name: str
    type: AccountType
    currency: str
    debits: int
    credits: int
    #: Signed by the normal balance side, so a healthy account reads positive.
    balance: int
    entry_count: int


class TrialBalanceRead(BaseModel):
    total_debits: int
    total_credits: int
    is_balanced: bool


class BalancesResponse(BaseModel):
    accounts: list[AccountBalanceRead]
    #: The headline number. If this is ever unbalanced, something is very wrong.
    trial_balance: TrialBalanceRead


class LedgerEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    account_name: str
    direction: EntryDirection
    amount: int


class TransactionRead(BaseModel):
    id: uuid.UUID
    description: str
    status: TransactionStatus
    currency: str
    #: Sum of the debit side, i.e. the size of the transaction.
    amount: int
    posted_at: datetime | None
    created_at: datetime
    entries: list[LedgerEntryRead]
    #: Rules that fired on this transaction. Empty is the normal case.
    flags: list[AnomalyRule]


@router.get("/accounts/balances", response_model=BalancesResponse)
def read_balances(
    session: Annotated[Session, Depends(get_session)],
    _user: CurrentUser,
) -> BalancesResponse:
    balances = account_balances(session)
    totals = trial_balance(session)
    return BalancesResponse(
        accounts=[AccountBalanceRead.model_validate(balance) for balance in balances],
        trial_balance=TrialBalanceRead(
            total_debits=totals.total_debits,
            total_credits=totals.total_credits,
            is_balanced=totals.is_balanced,
        ),
    )


@router.get("/transactions", response_model=list[TransactionRead])
def list_transactions(
    session: Annotated[Session, Depends(get_session)],
    _user: CurrentUser,
    flagged_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TransactionRead]:
    """Newest first, with the entries that make each one balance."""
    statement = (
        select(Transaction)
        .options(selectinload(Transaction.entries).joinedload(LedgerEntry.account))
        .order_by(Transaction.created_at.desc())
    )
    if flagged_only:
        statement = statement.where(Transaction.id.in_(select(AnomalyFlag.transaction_id)))

    transactions = list(session.scalars(statement.limit(limit).offset(offset)).all())
    if not transactions:
        return []

    # One extra query for the whole page rather than one per transaction.
    flags_by_transaction: dict[uuid.UUID, list[AnomalyRule]] = {}
    rows = session.execute(
        select(AnomalyFlag.transaction_id, AnomalyFlag.rule).where(
            AnomalyFlag.transaction_id.in_([t.id for t in transactions])
        )
    ).all()
    for transaction_id, rule in rows:
        flags_by_transaction.setdefault(transaction_id, []).append(rule)

    return [
        TransactionRead(
            id=transaction.id,
            description=transaction.description,
            status=transaction.status,
            currency=transaction.currency,
            amount=sum(
                entry.amount
                for entry in transaction.entries
                if entry.direction is EntryDirection.DEBIT
            ),
            posted_at=transaction.posted_at,
            created_at=transaction.created_at,
            entries=[
                LedgerEntryRead(
                    id=entry.id,
                    account_id=entry.account_id,
                    account_name=entry.account.name,
                    direction=entry.direction,
                    amount=entry.amount,
                )
                for entry in transaction.entries
            ],
            flags=flags_by_transaction.get(transaction.id, []),
        )
        for transaction in transactions
    ]
