"""Inbound webhook payloads and the receipt we return.

Providers add fields over time, so the envelope tolerates unknown keys. The
per-type payloads are strict about the fields that move money.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.models import WebhookEventStatus

Amount = Annotated[int, Field(gt=0, le=10**15, strict=True)]
Fee = Annotated[int, Field(ge=0, le=10**15, strict=True)]


class WebhookEnvelope(BaseModel):
    """The outer shape every event shares."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=64)
    created_at: datetime | None = None
    data: dict[str, Any]


class CaptureData(BaseModel):
    """`payment.captured` -- the only event that posts to the ledger."""

    model_config = ConfigDict(extra="allow")

    payment_intent_id: uuid.UUID
    amount: Amount
    currency: str = Field(min_length=3, max_length=3)
    #: Platform fee retained out of `amount`, booked to fee revenue.
    fee: Fee = 0


class FailureData(BaseModel):
    """`payment.failed` -- nothing moved, so nothing is posted."""

    model_config = ConfigDict(extra="allow")

    payment_intent_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=256)


class WebhookReceipt(BaseModel):
    event_id: str
    status: WebhookEventStatus
    #: True when this delivery had already been processed before.
    duplicate: bool
    transaction_id: uuid.UUID | None = None
    payment_intent_id: uuid.UUID | None = None
