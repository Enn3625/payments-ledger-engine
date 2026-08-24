"""Read access to the flags the rule engine raised.

Flags are advisory, so this is a review queue rather than a control surface:
there is nothing here to approve, block or release. Any authenticated user may
read them; nobody can change them through the API.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser
from app.db import get_session
from app.models import AnomalyFlag, AnomalyRule

router = APIRouter(prefix="/anomaly-flags", tags=["anomaly-flags"])


class AnomalyFlagRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    rule: AnomalyRule
    reason: str
    transaction_id: uuid.UUID | None
    payment_intent_id: uuid.UUID | None
    account_id: uuid.UUID | None
    created_at: datetime


@router.get("", response_model=list[AnomalyFlagRead])
def list_flags(
    session: Annotated[Session, Depends(get_session)],
    _user: CurrentUser,
    rule: AnomalyRule | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AnomalyFlag]:
    """Most recent flags first."""
    statement = select(AnomalyFlag).order_by(AnomalyFlag.created_at.desc())
    if rule is not None:
        statement = statement.where(AnomalyFlag.rule == rule)
    return list(session.scalars(statement.limit(limit).offset(offset)).all())
