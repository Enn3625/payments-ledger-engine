"""POST /payment-intents and friends.

Every create goes through `run_idempotent`, so a retried request -- the normal
consequence of a client timeout on a payments API -- returns the original
response instead of charging twice.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import get_session, get_session_factory
from app.models import PaymentIntent
from app.schemas.payment_intent import PaymentIntentCreate, PaymentIntentRead
from app.services.idempotency import (
    IdempotencyConflictError,
    InvalidIdempotencyKeyError,
    RequestInProgressError,
    run_idempotent,
)
from app.services.payment_intents import PaymentIntentError, create_payment_intent

router = APIRouter(prefix="/payment-intents", tags=["payment-intents"])

#: Idempotency keys are scoped to the operation, so the same key on a different
#: route is a different request rather than a collision.
ENDPOINT = "POST /payment-intents"

#: Set on every create response so a client can tell a fresh result from a
#: replayed one (Stripe calls this Idempotent-Replayed).
REPLAY_HEADER = "Idempotent-Replay"


@router.post(
    "",
    response_model=PaymentIntentRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "Idempotency-Key header missing or malformed"},
        409: {"description": "Idempotency-Key reused for a different request, or still in flight"},
        422: {"description": "Unknown merchant account, or currency mismatch"},
    },
)
def create_intent(
    payload: PaymentIntentCreate,
    response: Response,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    settings = get_settings()

    def handler(session: Session) -> tuple[int, dict[str, Any], uuid.UUID]:
        intent = create_payment_intent(session, **payload.model_dump())
        body = PaymentIntentRead.model_validate(intent).model_dump(mode="json")
        return status.HTTP_201_CREATED, body, intent.id

    try:
        result = run_idempotent(
            session_factory,
            key=idempotency_key,
            endpoint=ENDPOINT,
            payload=payload.model_dump(mode="json"),
            handler=handler,
            claim_timeout_seconds=settings.idempotency_claim_timeout_seconds,
        )
    except InvalidIdempotencyKeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except (IdempotencyConflictError, RequestInProgressError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except PaymentIntentError as exc:
        # The claim was released, so the client may retry with the same key.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    response.status_code = result.status_code
    response.headers[REPLAY_HEADER] = "true" if result.replayed else "false"
    return result.body


@router.get("/{intent_id}", response_model=PaymentIntentRead)
def get_intent(
    intent_id: uuid.UUID,
    session: Annotated[Session, Depends(get_session)],
) -> PaymentIntent:
    intent = session.get(PaymentIntent, intent_id)
    if intent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such payment intent: {intent_id}")
    return intent
