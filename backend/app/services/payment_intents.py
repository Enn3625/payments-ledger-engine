"""Payment intent creation.

Creating an intent deliberately writes nothing to the ledger. The ledger
records money that has moved; an intent only records money the platform expects
to collect. The two are joined in step 3, when a signed webhook confirms the
capture and a balanced transaction is posted.
"""

import uuid

from sqlalchemy.orm import Session

from app.models import Account, PaymentIntent, PaymentIntentStatus


class PaymentIntentError(Exception):
    """Base class for payment-intent rule violations."""


class UnknownMerchantAccountError(PaymentIntentError):
    """The intent references an account that does not exist."""


class CurrencyMismatchError(PaymentIntentError):
    """The intent currency differs from the merchant account currency."""


def create_payment_intent(
    session: Session,
    *,
    amount: int,
    currency: str,
    merchant_account_id: uuid.UUID,
    reference: str | None = None,
    description: str | None = None,
) -> PaymentIntent:
    """Create an intent in `requires_payment`. Flushes, does not commit."""
    account = session.get(Account, merchant_account_id)
    if account is None:
        raise UnknownMerchantAccountError(f"no such merchant account: {merchant_account_id}")
    if account.currency != currency:
        raise CurrencyMismatchError(
            f"merchant account {account.name} settles in {account.currency}, "
            f"intent is in {currency}"
        )

    intent = PaymentIntent(
        amount=amount,
        currency=currency,
        status=PaymentIntentStatus.REQUIRES_PAYMENT,
        merchant_account_id=merchant_account_id,
        reference=reference,
        description=description,
    )
    session.add(intent)
    session.flush()
    return intent
