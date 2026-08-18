"""Individual debit/credit lines. Append-only: never updated, never deleted.

Amounts are integer *minor units* (paise for INR). Floats are never used for
money anywhere in this codebase.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import ENTRY_DIRECTION_ENUM, EntryDirection

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.transaction import Transaction


class LedgerEntry(TimestampMixin, Base):
    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[EntryDirection] = mapped_column(ENTRY_DIRECTION_ENUM, nullable=False)

    transaction: Mapped["Transaction"] = relationship(back_populates="entries")
    account: Mapped["Account"] = relationship(back_populates="entries")

    __table_args__ = (
        # Signed amounts are expressed by `direction`, so the magnitude is
        # always strictly positive. Zero-value entries are noise, not data.
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_ledger_entries_transaction_id", "transaction_id"),
        Index("ix_ledger_entries_account_id", "account_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LedgerEntry {self.direction.value} {self.amount}>"
