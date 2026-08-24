"""Anomaly flags raised by the rule engine.

A flag is an observation, not a verdict: it never blocks a capture or alters
the ledger. Payments infrastructure that silently swallows money because a
heuristic fired is worse than one that records the concern and moves on.
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import ANOMALY_RULE_ENUM, AnomalyRule

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.payment_intent import PaymentIntent
    from app.models.transaction import Transaction


class AnomalyFlag(TimestampMixin, Base):
    __tablename__ = "anomaly_flags"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    rule: Mapped[AnomalyRule] = mapped_column(ANOMALY_RULE_ENUM, nullable=False)
    #: Human-readable explanation, with the numbers that triggered the rule.
    #: Explainability is the point: a flag nobody can interpret is noise.
    reason: Mapped[str] = mapped_column(Text, nullable=False)
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
    #: The account the rule was evaluated against, where that makes sense.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=True,
    )

    transaction: Mapped["Transaction | None"] = relationship()
    payment_intent: Mapped["PaymentIntent | None"] = relationship()
    account: Mapped["Account | None"] = relationship()

    __table_args__ = (
        # One flag per rule per transaction, so re-evaluating cannot pile up
        # duplicates of the same finding.
        UniqueConstraint("rule", "transaction_id", name="rule_transaction"),
        Index("ix_anomaly_flags_created_at", "created_at"),
        Index("ix_anomaly_flags_rule", "rule"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AnomalyFlag {self.rule.value}>"
