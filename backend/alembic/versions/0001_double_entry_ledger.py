"""Double-entry ledger core: accounts, transactions, ledger_entries.

Ships the correctness machinery with the schema:
  * balanced_transaction  -- deferred constraint trigger, debits == credits
  * posted_has_entries    -- deferred constraint trigger, no empty postings
  * append_only           -- ledger entries can never be updated or deleted

Revision ID: 0001
Revises:
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# create_type=False: the types are created explicitly in upgrade(), so
# op.create_table() must not emit a second CREATE TYPE for the same enum.
ACCOUNT_TYPE = postgresql.ENUM(
    "asset",
    "liability",
    "equity",
    "revenue",
    "expense",
    name="account_type",
    create_type=False,
)
TRANSACTION_STATUS = postgresql.ENUM(
    "pending", "posted", "failed", name="transaction_status", create_type=False
)
ENTRY_DIRECTION = postgresql.ENUM("debit", "credit", name="entry_direction", create_type=False)

CURRENCY_REGEX = r"currency ~ '^[A-Z]{3}$'"


# --------------------------------------------------------------------------
# The invariant. Runs at COMMIT (DEFERRABLE INITIALLY DEFERRED) so a posting
# may insert its debit and its credit as separate statements, but cannot leave
# the transaction half-written. Any writer -- ORM, raw SQL, psql, a seed
# script -- is held to it.
# --------------------------------------------------------------------------
BALANCED_TRANSACTION_FN = """
CREATE OR REPLACE FUNCTION assert_transaction_balanced() RETURNS trigger AS $fn$
DECLARE
    v_txn_id   uuid;
    v_debits   bigint;
    v_credits  bigint;
    v_entries  integer;
BEGIN
    v_txn_id := COALESCE(NEW.transaction_id, OLD.transaction_id);

    SELECT
        COALESCE(SUM(amount) FILTER (WHERE direction = 'debit'), 0),
        COALESCE(SUM(amount) FILTER (WHERE direction = 'credit'), 0),
        COUNT(*)
    INTO v_debits, v_credits, v_entries
    FROM ledger_entries
    WHERE transaction_id = v_txn_id;

    -- No entries at all: nothing to balance. An empty *posted* transaction is
    -- caught by assert_posted_has_entries() instead.
    IF v_entries = 0 THEN
        RETURN NULL;
    END IF;

    IF v_entries < 2 THEN
        RAISE EXCEPTION
            'transaction % has only % ledger entry; double-entry requires at least 2',
            v_txn_id, v_entries
            USING ERRCODE = 'check_violation';
    END IF;

    IF v_debits <> v_credits THEN
        RAISE EXCEPTION
            'transaction % is unbalanced: debits=% credits=% (difference=%)',
            v_txn_id, v_debits, v_credits, v_debits - v_credits
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;
"""

POSTED_HAS_ENTRIES_FN = """
CREATE OR REPLACE FUNCTION assert_posted_has_entries() RETURNS trigger AS $fn$
DECLARE
    v_entries integer;
BEGIN
    IF NEW.status <> 'posted' THEN
        RETURN NULL;
    END IF;

    SELECT COUNT(*) INTO v_entries
    FROM ledger_entries
    WHERE transaction_id = NEW.id;

    IF v_entries = 0 THEN
        RAISE EXCEPTION
            'transaction % is marked posted but has no ledger entries', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;
"""

# Corrections to a ledger are made by posting a reversing transaction, never by
# editing history. TRUNCATE is intentionally still allowed (test teardown).
APPEND_ONLY_FN = """
CREATE OR REPLACE FUNCTION reject_ledger_entry_mutation() RETURNS trigger AS $fn$
BEGIN
    RAISE EXCEPTION
        'ledger_entries is append-only; % is not permitted (post a reversing entry instead)',
        TG_OP
        USING ERRCODE = 'check_violation';
END;
$fn$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    bind = op.get_bind()
    ACCOUNT_TYPE.create(bind, checkfirst=True)
    TRANSACTION_STATUS.create(bind, checkfirst=True)
    ENTRY_DIRECTION.create(bind, checkfirst=True)

    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("type", ACCOUNT_TYPE, nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(CURRENCY_REGEX, name="ck_accounts_currency_iso4217"),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
        sa.UniqueConstraint("name", name="uq_accounts_name"),
    )

    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=False),
        sa.Column(
            "status",
            TRANSACTION_STATUS,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), server_default="INR", nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(CURRENCY_REGEX, name="ck_transactions_currency_iso4217"),
        sa.CheckConstraint(
            "(status = 'posted') = (posted_at IS NOT NULL)",
            name="ck_transactions_posted_at_matches_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_transactions"),
    )
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])
    op.create_index("ix_transactions_status", "transactions", ["status"])

    op.create_table(
        "ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("direction", ENTRY_DIRECTION, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_ledger_entries_amount_positive"),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name="fk_ledger_entries_transaction_id_transactions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_ledger_entries_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ledger_entries"),
    )
    op.create_index("ix_ledger_entries_transaction_id", "ledger_entries", ["transaction_id"])
    op.create_index("ix_ledger_entries_account_id", "ledger_entries", ["account_id"])

    op.execute(BALANCED_TRANSACTION_FN)
    op.execute(POSTED_HAS_ENTRIES_FN)
    op.execute(APPEND_ONLY_FN)

    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_ledger_entries_balanced
        AFTER INSERT OR UPDATE OR DELETE ON ledger_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION assert_transaction_balanced();
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_transactions_posted_has_entries
        AFTER INSERT OR UPDATE ON transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION assert_posted_has_entries();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_ledger_entries_append_only
        BEFORE UPDATE OR DELETE ON ledger_entries
        FOR EACH ROW EXECUTE FUNCTION reject_ledger_entry_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_append_only ON ledger_entries")
    op.execute("DROP TRIGGER IF EXISTS trg_transactions_posted_has_entries ON transactions")
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_balanced ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS reject_ledger_entry_mutation()")
    op.execute("DROP FUNCTION IF EXISTS assert_posted_has_entries()")
    op.execute("DROP FUNCTION IF EXISTS assert_transaction_balanced()")

    op.drop_table("ledger_entries")
    op.drop_table("transactions")
    op.drop_table("accounts")

    bind = op.get_bind()
    ENTRY_DIRECTION.drop(bind, checkfirst=True)
    TRANSACTION_STATUS.drop(bind, checkfirst=True)
    ACCOUNT_TYPE.drop(bind, checkfirst=True)
