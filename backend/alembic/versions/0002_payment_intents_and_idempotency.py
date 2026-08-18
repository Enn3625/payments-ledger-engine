"""Payment intents and the idempotency key store.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: the types are created explicitly in upgrade(), so
# op.create_table() must not emit a second CREATE TYPE for the same enum.
PAYMENT_INTENT_STATUS = postgresql.ENUM(
    "requires_payment",
    "processing",
    "succeeded",
    "failed",
    name="payment_intent_status",
    create_type=False,
)
IDEMPOTENCY_STATE = postgresql.ENUM(
    "in_progress", "completed", name="idempotency_state", create_type=False
)

CURRENCY_REGEX = r"currency ~ '^[A-Z]{3}$'"

# An idempotency row is either an open claim or a replayable response. There is
# no third state where a client could be handed a half-written answer.
COMPLETED_HAS_RESPONSE = (
    "(state = 'completed') = "
    "(response_status_code IS NOT NULL AND response_snapshot IS NOT NULL "
    "AND completed_at IS NOT NULL)"
)


def upgrade() -> None:
    bind = op.get_bind()
    PAYMENT_INTENT_STATUS.create(bind, checkfirst=True)
    IDEMPOTENCY_STATE.create(bind, checkfirst=True)

    op.create_table(
        "payment_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column(
            "status",
            PAYMENT_INTENT_STATUS,
            server_default=sa.text("'requires_payment'"),
            nullable=False,
        ),
        sa.Column("merchant_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference", sa.String(length=128), nullable=True),
        sa.Column("description", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_payment_intents_amount_positive"),
        sa.CheckConstraint(CURRENCY_REGEX, name="ck_payment_intents_currency_iso4217"),
        sa.ForeignKeyConstraint(
            ["merchant_account_id"],
            ["accounts.id"],
            name="fk_payment_intents_merchant_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payment_intents"),
    )
    op.create_index("ix_payment_intents_created_at", "payment_intents", ["created_at"])
    op.create_index("ix_payment_intents_status", "payment_intents", ["status"])
    op.create_index("ix_payment_intents_reference", "payment_intents", ["reference"])

    op.create_table(
        "idempotency_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            IDEMPOTENCY_STATE,
            server_default=sa.text("'in_progress'"),
            nullable=False,
        ),
        sa.Column("response_status_code", sa.Integer(), nullable=True),
        sa.Column("response_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            COMPLETED_HAS_RESPONSE, name="ck_idempotency_keys_completed_has_response"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_keys"),
        # The race is resolved here: concurrent requests carrying the same key
        # contend on this unique index, and exactly one of them wins the claim.
        sa.UniqueConstraint("endpoint", "key", name="uq_idempotency_keys_endpoint_key"),
    )
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    op.drop_table("payment_intents")

    bind = op.get_bind()
    IDEMPOTENCY_STATE.drop(bind, checkfirst=True)
    PAYMENT_INTENT_STATUS.drop(bind, checkfirst=True)
