"""A transaction groups the ledger entries that must balance together."""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import TRANSACTION_STATUS_ENUM, TransactionStatus

if TYPE_CHECKING:
    from app.models.ledger_entry import LedgerEntry


class Transaction(TimestampMixin, Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    description: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[TransactionStatus] = mapped_column(
        TRANSACTION_STATUS_ENUM,
        nullable=False,
        server_default=text("'pending'"),
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    entries: Mapped[list["LedgerEntry"]] = relationship(
        back_populates="transaction",
        order_by="LedgerEntry.created_at",
    )

    __table_args__ = (
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso4217"),
        CheckConstraint(
            "(status = 'posted') = (posted_at IS NOT NULL)",
            name="posted_at_matches_status",
        ),
        Index("ix_transactions_created_at", "created_at"),
        Index("ix_transactions_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Transaction {self.id} {self.status.value}>"
