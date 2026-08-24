"""Anomaly flags raised by the rule engine.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: the type is created explicitly in upgrade(), so
# op.create_table() must not emit a second CREATE TYPE for the same enum.
ANOMALY_RULE = postgresql.ENUM(
    "velocity", "amount_threshold", name="anomaly_rule", create_type=False
)


def upgrade() -> None:
    ANOMALY_RULE.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "anomaly_flags",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule", ANOMALY_RULE, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payment_intent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name="fk_anomaly_flags_transaction_id_transactions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["payment_intent_id"],
            ["payment_intents.id"],
            name="fk_anomaly_flags_payment_intent_id_payment_intents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_anomaly_flags_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_anomaly_flags"),
        # One flag per rule per transaction: re-evaluating the same capture
        # cannot pile up duplicates of the same finding.
        sa.UniqueConstraint(
            "rule", "transaction_id", name="uq_anomaly_flags_rule_transaction"
        ),
    )
    op.create_index("ix_anomaly_flags_created_at", "anomaly_flags", ["created_at"])
    op.create_index("ix_anomaly_flags_rule", "anomaly_flags", ["rule"])


def downgrade() -> None:
    op.drop_table("anomaly_flags")
    ANOMALY_RULE.drop(op.get_bind(), checkfirst=True)
