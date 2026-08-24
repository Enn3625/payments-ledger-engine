"""Received webhook events.

Every verified delivery lands here exactly once. `event_id` is UNIQUE, which is
what makes provider retries safe: the second delivery of an event that was
already processed cannot re-apply its ledger effects.

Failed events stay in the table so an admin can retry them (step 6), which is
why "already seen" is not the same question as "already applied".
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import WEBHOOK_EVENT_STATUS_ENUM, WebhookEventStatus


class WebhookEvent(Base, TimestampMixin):
    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: The provider-assigned event id (evt_...). The replay guard.
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The parsed body, kept verbatim for audit and for admin retries.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    #: The signature header as received, so a disputed delivery can be re-checked.
    signature: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[WebhookEventStatus] = mapped_column(
        WEBHOOK_EVENT_STATUS_ENUM,
        nullable=False,
        server_default=text("'received'"),
    )
    #: The ledger transaction this event produced, if any.
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    payment_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("payment_intents.id", ondelete="RESTRICT"),
        nullable=True,
    )
    #: How many times processing has been attempted, including retries.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_webhook_events_event_id"),
        CheckConstraint(
            "(status = 'processed') = (processed_at IS NOT NULL)",
            name="processed_at_matches_status",
        ),
        Index("ix_webhook_events_created_at", "created_at"),
        Index("ix_webhook_events_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WebhookEvent {self.event_id} {self.event_type} {self.status.value}>"
