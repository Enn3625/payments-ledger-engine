"""Request/response models for payment intents."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import PaymentIntentStatus

#: Amounts cross the wire as integer minor units, so an amount of 150000 in INR
#: is Rs 1,500.00. Floats are rejected outright rather than rounded.
Amount = Annotated[int, Field(gt=0, le=10**15, strict=True)]
Currency = Annotated[str, Field(min_length=3, max_length=3)]


class PaymentIntentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Amount
    currency: Currency = "INR"
    merchant_account_id: uuid.UUID
    reference: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=256)

    @field_validator("currency")
    @classmethod
    def _uppercase_iso4217(cls, value: str) -> str:
        value = value.upper()
        if not value.isalpha():
            raise ValueError("currency must be a 3-letter ISO 4217 code")
        return value


class PaymentIntentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    amount: int
    currency: str
    status: PaymentIntentStatus
    merchant_account_id: uuid.UUID
    reference: str | None
    description: str | None
    created_at: datetime
