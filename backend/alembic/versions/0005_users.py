"""API users and roles.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False: the type is created explicitly in upgrade(), so
# op.create_table() must not emit a second CREATE TYPE for the same enum.
USER_ROLE = postgresql.ENUM("admin", "viewer", name="user_role", create_type=False)


def upgrade() -> None:
    USER_ROLE.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        # bcrypt output is always 60 characters.
        sa.Column("password_hash", sa.String(length=60), nullable=False),
        sa.Column("role", USER_ROLE, nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_is_lowercase"),
        sa.CheckConstraint("length(email) >= 3", name="ck_users_email_length"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        # Case is normalised before insert, so this is a real uniqueness
        # guarantee rather than one an attacker can sidestep with capitals.
        sa.UniqueConstraint("email", name="uq_users_email"),
    )


def downgrade() -> None:
    op.drop_table("users")
    USER_ROLE.drop(op.get_bind(), checkfirst=True)
