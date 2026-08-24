"""Webhook event log, and the intent -> ledger transaction link.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: the type is created explicitly in upgrade(), so
# op.create_table() must not emit a second CREATE TYPE for the same enum.
WEBHOOK_EVENT_STATUS = postgresql.ENUM(
    "received",
    "processed",
    "ignored",
    "failed",
    name="webhook_event_status",
    create_type=False,
)


def upgrade() -> None:
    WEBHOOK_EVENT_STATUS.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("signature", sa.String(length=256), nullable=False),
        sa.Column(
            "status",
            WEBHOOK_EVENT_STATUS,
            server_default=sa.text("'received'"),
            nullable=False,
        ),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payment_intent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'processed') = (processed_at IS NOT NULL)",
            name="ck_webhook_events_processed_at_matches_status",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name="fk_webhook_events_transaction_id_transactions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_intent_id"],
            ["payment_intents.id"],
            name="fk_webhook_events_payment_intent_id_payment_intents",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_events"),
        # The replay guard. A redelivered event contends on this index and
        # loses, so its effects cannot be applied a second time.
        sa.UniqueConstraint("event_id", name="uq_webhook_events_event_id"),
    )
    op.create_index("ix_webhook_events_created_at", "webhook_events", ["created_at"])
    op.create_index("ix_webhook_events_status", "webhook_events", ["status"])

    # An intent can carry at most one ledger transaction. Double capture is
    # therefore impossible at the storage layer, not merely discouraged in code.
    op.add_column(
        "payment_intents",
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_payment_intents_transaction_id_transactions",
        "payment_intents",
        "transactions",
        ["transaction_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_payment_intents_transaction_id", "payment_intents", ["transaction_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_payment_intents_transaction_id", "payment_intents", type_="unique")
    op.drop_constraint(
        "fk_payment_intents_transaction_id_transactions", "payment_intents", type_="foreignkey"
    )
    op.drop_column("payment_intents", "transaction_id")

    op.drop_table("webhook_events")
    WEBHOOK_EVENT_STATUS.drop(op.get_bind(), checkfirst=True)
