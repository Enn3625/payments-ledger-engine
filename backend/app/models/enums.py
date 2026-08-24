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


class PaymentIntentStatus(str, Enum):
    """Payment-domain state, distinct from the accounting state.

    An intent describes money the platform expects to collect. Nothing hits the
    ledger until a verified webhook says the money actually moved (step 3).
    """

    REQUIRES_PAYMENT = "requires_payment"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class IdempotencyState(str, Enum):
    #: The key is claimed and the original request is still executing.
    IN_PROGRESS = "in_progress"
    #: The response is stored and replayable.
    COMPLETED = "completed"


class WebhookEventStatus(str, Enum):
    #: Recorded, effects not applied yet.
    RECEIVED = "received"
    #: Effects applied. Terminal: a redelivery of this event is a no-op.
    PROCESSED = "processed"
    #: Recognised but irrelevant (an event type we do not act on).
    IGNORED = "ignored"
    #: Verified and recorded, but the effects could not be applied. Retryable.
    FAILED = "failed"


class AnomalyRule(str, Enum):
    """Rules are deliberately simple and explainable.

    A reviewer must be able to read a flag and say exactly why it fired; a
    score from an opaque model would be harder to defend in a dispute.
    """

    #: Too many captures against one account inside a short window.
    VELOCITY = "velocity"
    #: A single capture larger than the configured limit.
    AMOUNT_THRESHOLD = "amount_threshold"


class UserRole(str, Enum):
    """Two roles, because the interesting distinction is read versus write.

    Anything that moves money or changes state is admin-only. A viewer can see
    everything and change nothing, which is what makes a public demo login safe.
    """

    ADMIN = "admin"
    VIEWER = "viewer"


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
PAYMENT_INTENT_STATUS_ENUM = pg_enum(PaymentIntentStatus, "payment_intent_status")
IDEMPOTENCY_STATE_ENUM = pg_enum(IdempotencyState, "idempotency_state")
WEBHOOK_EVENT_STATUS_ENUM = pg_enum(WebhookEventStatus, "webhook_event_status")
ANOMALY_RULE_ENUM = pg_enum(AnomalyRule, "anomaly_rule")
USER_ROLE_ENUM = pg_enum(UserRole, "user_role")

#: Account types whose balance increases on the debit side. The rest
#: (liability, equity, revenue) increase on the credit side.
NORMAL_DEBIT_TYPES = frozenset({AccountType.ASSET, AccountType.EXPENSE})
