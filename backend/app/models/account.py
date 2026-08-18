"""Chart of accounts."""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import ACCOUNT_TYPE_ENUM, AccountType

if TYPE_CHECKING:
    from app.models.ledger_entry import LedgerEntry


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    type: Mapped[AccountType] = mapped_column(ACCOUNT_TYPE_ENUM, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")

    entries: Mapped[list["LedgerEntry"]] = relationship(back_populates="account")

    __table_args__ = (CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso4217"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Account {self.name} ({self.type.value})>"
