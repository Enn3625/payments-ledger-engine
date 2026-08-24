"""Idempotency key store.

One row per (endpoint, key). The row is claimed *before* the work runs and
completed afterwards in the same transaction as the work, so a replay can only
ever return a response that was actually committed.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import IDEMPOTENCY_STATE_ENUM, IdempotencyState


class IdempotencyKey(TimestampMixin, Base):
    __tablename__ = "idempotency_keys"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    #: The client-supplied Idempotency-Key header value.
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Scope. The same key on a different route is a different operation.
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    #: SHA-256 over the canonicalised request body. A mismatch means the client
    #: reused a key for a different request, which is a client bug: 409.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[IdempotencyState] = mapped_column(
        IDEMPOTENCY_STATE_ENUM,
        nullable=False,
        server_default=text("'in_progress'"),
    )
    response_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    #: The resource the original request produced, for traceability.
    resource_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("endpoint", "key", name="uq_idempotency_keys_endpoint_key"),
        # A completed row must carry a replayable response; an in-progress row
        # must not pretend to have one.
        CheckConstraint(
            "(state = 'completed') = "
            "(response_status_code IS NOT NULL AND response_snapshot IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name="completed_has_response",
        ),
        Index("ix_idempotency_keys_created_at", "created_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<IdempotencyKey {self.endpoint} {self.key} {self.state.value}>"
