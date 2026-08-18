"""Domain enums, mirrored as native PostgreSQL enum types."""

from enum import Enum

from sqlalchemy import Enum as SAEnum


class AccountType(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class TransactionStatus(str, Enum):
    #: Created, but no ledger entries posted yet (e.g. an unconfirmed intent).
    PENDING = "pending"
    #: Balanced entries are on the ledger. Terminal and immutable.
    POSTED = "posted"
    #: Abandoned before posting. Never carries ledger entries.
    FAILED = "failed"


class EntryDirection(str, Enum):
    DEBIT = "debit"
    CREDIT = "credit"


def pg_enum(enum_cls: type[Enum], name: str) -> SAEnum:
    """Native PG enum storing the lowercase *values*, not the member names."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


ACCOUNT_TYPE_ENUM = pg_enum(AccountType, "account_type")
TRANSACTION_STATUS_ENUM = pg_enum(TransactionStatus, "transaction_status")
ENTRY_DIRECTION_ENUM = pg_enum(EntryDirection, "entry_direction")

#: Account types whose balance increases on the debit side. The rest
#: (liability, equity, revenue) increase on the credit side.
NORMAL_DEBIT_TYPES = frozenset({AccountType.ASSET, AccountType.EXPENSE})
