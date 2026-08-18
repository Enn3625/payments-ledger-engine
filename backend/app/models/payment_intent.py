"""Payment intents: money the platform expects to collect.

An intent is *not* a ledger transaction. It carries payment-domain state
(requires_payment -> succeeded/failed); the ledger only records money that has
actually moved, which is why creating an intent writes no ledger entries.
"""

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import PAYMENT_INTENT_STATUS_ENUM, PaymentIntentStatus

if TYPE_CHECKING:
    from app.models.account import Account


class PaymentIntent(TimestampMixin, Base):
    __tablename__ = "payment_intents"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: Minor units (paise for INR), same convention as ledger_entries.amount.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="INR")
    status: Mapped[PaymentIntentStatus] = mapped_column(
        PAYMENT_INTENT_STATUS_ENUM,
        nullable=False,
        server_default=text("'requires_payment'"),
    )
    #: The account credited when this intent is captured.
    merchant_account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    #: Caller-supplied order identifier, echoed back on webhooks.
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        onupdate=text("now()"),
        nullable=False,
    )

    merchant_account: Mapped["Account"] = relationship()

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso4217"),
        Index("ix_payment_intents_created_at", "created_at"),
        Index("ix_payment_intents_status", "status"),
        Index("ix_payment_intents_reference", "reference"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PaymentIntent {self.id} {self.amount} {self.currency} {self.status.value}>"
