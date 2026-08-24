"""POST /webhooks/payment-events.

The signature is checked against the **raw request bytes**, which is why this
route takes the body as `bytes` and parses it itself instead of letting FastAPI
deserialise it. Verifying a re-serialised body would accept payloads whose
bytes differ from what the provider actually signed.

The async dependency reads the body on the event loop; the endpoint itself
stays synchronous and runs in the threadpool, like the rest of the API.

The inbound route carries no bearer token on purpose: the payment provider has
no login, and the HMAC signature *is* its authentication. The admin retry route
below is the opposite -- a human action, so it needs a human identity.
"""

import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import AdminUser, CurrentUser
from app.config import get_settings
from app.db import get_session, get_session_factory
from app.models import WebhookEvent, WebhookEventStatus
from app.schemas.webhook import WebhookEnvelope, WebhookReceipt
from app.services.signatures import (
    SIGNATURE_HEADER,
    SignatureError,
    verify_signature,
)
from app.services.webhooks import (
    UnknownWebhookEventError,
    WebhookProcessingError,
    handle_event,
    retry_event,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def get_raw_body(request: Request) -> bytes:
    """The exact bytes the provider signed."""
    return await request.body()


@router.post(
    "/payment-events",
    response_model=WebhookReceipt,
    responses={
        400: {"description": "Body is not a usable webhook envelope"},
        401: {"description": "Missing, malformed, invalid or stale signature"},
        422: {"description": "Authentic event whose effects could not be applied"},
    },
)
def receive_payment_event(
    raw_body: Annotated[bytes, Depends(get_raw_body)],
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
) -> WebhookReceipt:
    settings = get_settings()

    try:
        verify_signature(
            raw_body,
            signature,
            settings.webhook_secret,
            tolerance_seconds=settings.webhook_timestamp_tolerance_seconds,
        )
    except SignatureError as exc:
        # 401 for every signature problem, with no detail about which check
        # failed -- a prober should not learn whether the digest or the
        # timestamp was wrong.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "signature verification failed") from exc

    try:
        payload: dict[str, Any] = json.loads(raw_body)
        envelope = WebhookEnvelope.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "body is not a valid webhook envelope"
        ) from exc

    try:
        outcome = handle_event(
            session_factory,
            envelope=envelope,
            payload=payload,
            signature=signature or "",
            settings=settings,
        )
    except WebhookProcessingError as exc:
        # Recorded as `failed` and retryable; the provider gets a hard error
        # rather than a silent acknowledgement.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return WebhookReceipt(
        event_id=outcome.event_id,
        status=outcome.status,
        duplicate=outcome.duplicate,
        transaction_id=outcome.transaction_id,
        payment_intent_id=outcome.payment_intent_id,
    )


class WebhookEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_id: str
    event_type: str
    status: WebhookEventStatus
    attempts: int
    last_error: str | None
    transaction_id: uuid.UUID | None
    payment_intent_id: uuid.UUID | None
    created_at: datetime
    processed_at: datetime | None


@router.get("/events", response_model=list[WebhookEventRead])
def list_webhook_events(
    session: Annotated[Session, Depends(get_session)],
    _user: CurrentUser,
    status_filter: Annotated[WebhookEventStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[WebhookEvent]:
    """The delivery log, newest first. Readable by any authenticated user."""
    statement = select(WebhookEvent).order_by(WebhookEvent.created_at.desc())
    if status_filter is not None:
        statement = statement.where(WebhookEvent.status == status_filter)
    return list(session.scalars(statement.limit(limit).offset(offset)).all())


@router.post(
    "/events/{event_id}/retry",
    response_model=WebhookReceipt,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Requires the admin role"},
        404: {"description": "No such webhook event"},
        422: {"description": "The event still cannot be applied"},
    },
)
def retry_webhook_event(
    event_id: str,
    session_factory: Annotated[sessionmaker[Session], Depends(get_session_factory)],
    _admin: AdminUser,
) -> WebhookReceipt:
    """Reprocess a stored event. Admin only.

    The stored payload is replayed as-is, so a retry cannot smuggle in data the
    provider never signed. Retrying an already-processed event is a no-op that
    reports `duplicate: true` rather than posting to the ledger again.
    """
    settings = get_settings()

    try:
        outcome = retry_event(session_factory, event_id=event_id, settings=settings)
    except UnknownWebhookEventError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except WebhookProcessingError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return WebhookReceipt(
        event_id=outcome.event_id,
        status=outcome.status,
        duplicate=outcome.duplicate,
        transaction_id=outcome.transaction_id,
        payment_intent_id=outcome.payment_intent_id,
    )
