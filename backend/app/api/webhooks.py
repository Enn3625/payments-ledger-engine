"""POST /webhooks/payment-events.

The signature is checked against the **raw request bytes**, which is why this
route takes the body as `bytes` and parses it itself instead of letting FastAPI
deserialise it. Verifying a re-serialised body would accept payloads whose
bytes differ from what the provider actually signed.

The async dependency reads the body on the event loop; the endpoint itself
stays synchronous and runs in the threadpool, like the rest of the API.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import get_session_factory
from app.schemas.webhook import WebhookEnvelope, WebhookReceipt
from app.services.signatures import (
    SIGNATURE_HEADER,
    SignatureError,
    verify_signature,
)
from app.services.webhooks import WebhookProcessingError, handle_event

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
