"""Idempotent request execution.

The contract, in the order the checks happen:

* No key            -> the caller is rejected before any work starts (API layer).
* New key           -> claim it, run the work, store the response.
* Same key + same request, original finished   -> replay the stored response.
* Same key + same request, original in flight  -> 409, the client should retry.
* Same key + different request                 -> 409, that is a client bug.

Two properties make this safe under concurrency:

1. The claim is a plain INSERT against a UNIQUE (endpoint, key) index, so the
   database picks the single winner. No advisory locks, no read-then-write race.
2. The work and the "mark this key completed" update commit in one transaction.
   A replay can therefore only ever return a response whose side effects were
   actually committed -- there is no window where a key looks done but is not.

Failed work releases the key rather than caching the failure, so a client that
retries after a 5xx gets a real attempt instead of a memoised error.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import IdempotencyKey, IdempotencyState

MAX_KEY_LENGTH = 255
#: Guard against ping-ponging with a peer that keeps releasing the key.
MAX_CLAIM_ATTEMPTS = 3

#: A handler does the real work and reports what to cache:
#: (http status code, response body, id of the resource it produced).
Handler = Callable[[Session], tuple[int, dict[str, Any], uuid.UUID | None]]


class IdempotencyError(Exception):
    """Base class for idempotency-protocol violations."""


class InvalidIdempotencyKeyError(IdempotencyError):
    """The supplied key is missing or malformed."""


class IdempotencyConflictError(IdempotencyError):
    """The key was reused for a materially different request."""


class RequestInProgressError(IdempotencyError):
    """An identical request holding this key is still running."""


@dataclass(frozen=True, slots=True)
class IdempotentResult:
    status_code: int
    body: dict[str, Any]
    #: True when the body came from the store rather than fresh work.
    replayed: bool
    resource_id: uuid.UUID | None = None


def canonical_request_hash(payload: Mapping[str, Any]) -> str:
    """SHA-256 over a canonical rendering of the request.

    Hashing the parsed payload rather than the raw bytes means key ordering and
    whitespace do not make two identical requests look different, while any
    change to an actual value does.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_key(key: str | None) -> str:
    if key is None or not key.strip():
        raise InvalidIdempotencyKeyError("Idempotency-Key header is required")
    key = key.strip()
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidIdempotencyKeyError(
            f"Idempotency-Key must be at most {MAX_KEY_LENGTH} characters"
        )
    return key


def run_idempotent(
    session_factory: sessionmaker[Session],
    *,
    key: str | None,
    endpoint: str,
    payload: Mapping[str, Any],
    handler: Handler,
    claim_timeout_seconds: int = 60,
) -> IdempotentResult:
    """Execute `handler` at most once for this (endpoint, key)."""
    key = validate_key(key)
    request_hash = canonical_request_hash(payload)

    claim_id = _claim(
        session_factory,
        key=key,
        endpoint=endpoint,
        request_hash=request_hash,
        claim_timeout_seconds=claim_timeout_seconds,
    )
    if isinstance(claim_id, IdempotentResult):
        return claim_id

    return _execute(session_factory, claim_id=claim_id, handler=handler)


def _claim(
    session_factory: sessionmaker[Session],
    *,
    key: str,
    endpoint: str,
    request_hash: str,
    claim_timeout_seconds: int,
    attempt: int = 1,
) -> uuid.UUID | IdempotentResult:
    """Win the key and return the claim id, or return the replayed response."""
    if attempt > MAX_CLAIM_ATTEMPTS:
        raise RequestInProgressError(
            f"could not settle Idempotency-Key {key!r} after {MAX_CLAIM_ATTEMPTS} "
            "attempts; retry shortly"
        )

    with session_factory() as session:
        claim = IdempotencyKey(
            key=key,
            endpoint=endpoint,
            request_hash=request_hash,
            state=IdempotencyState.IN_PROGRESS,
        )
        session.add(claim)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            return claim.id

    # Someone else holds the key. Decide between replay, conflict and takeover.
    with session_factory() as session:
        existing = session.scalar(
            select(IdempotencyKey)
            .where(IdempotencyKey.endpoint == endpoint, IdempotencyKey.key == key)
            .with_for_update()
        )
        if existing is None:
            # The holder released the key between our INSERT and this SELECT.
            return _claim(
                session_factory,
                key=key,
                endpoint=endpoint,
                request_hash=request_hash,
                claim_timeout_seconds=claim_timeout_seconds,
                attempt=attempt + 1,
            )

        if existing.request_hash != request_hash:
            raise IdempotencyConflictError(
                f"Idempotency-Key {key!r} was already used for a different request "
                f"on {endpoint}"
            )

        if existing.state is IdempotencyState.COMPLETED:
            return IdempotentResult(
                status_code=existing.response_status_code or 200,
                body=dict(existing.response_snapshot or {}),
                replayed=True,
                resource_id=existing.resource_id,
            )

        # In progress. A claim older than the timeout belongs to a request that
        # died mid-flight (crash, timeout, killed worker); take it over. The row
        # lock above means only one taker wins.
        age = datetime.now(UTC) - existing.created_at
        if age > timedelta(seconds=claim_timeout_seconds):
            existing.created_at = datetime.now(UTC)
            claim_id = existing.id
            session.commit()
            return claim_id

        raise RequestInProgressError(
            f"a request with Idempotency-Key {key!r} is already in progress; retry shortly"
        )


def _execute(
    session_factory: sessionmaker[Session],
    *,
    claim_id: uuid.UUID,
    handler: Handler,
) -> IdempotentResult:
    """Run the work and complete the claim atomically."""
    session = session_factory()
    try:
        status_code, body, resource_id = handler(session)

        claim = session.get(IdempotencyKey, claim_id, with_for_update=True)
        if claim is None:  # pragma: no cover - only if the row is deleted underneath us
            raise IdempotencyError("idempotency claim disappeared mid-request")
        claim.state = IdempotencyState.COMPLETED
        claim.response_status_code = status_code
        claim.response_snapshot = body
        claim.resource_id = resource_id
        claim.completed_at = datetime.now(UTC)

        session.commit()
        return IdempotentResult(
            status_code=status_code, body=body, replayed=False, resource_id=resource_id
        )
    except Exception:
        session.rollback()
        _release(session_factory, claim_id)
        raise
    finally:
        session.close()


def _release(session_factory: sessionmaker[Session], claim_id: uuid.UUID) -> None:
    """Drop an unfinished claim so the client can retry with the same key."""
    with session_factory() as session:
        claim = session.get(IdempotencyKey, claim_id)
        if claim is not None and claim.state is IdempotencyState.IN_PROGRESS:
            session.delete(claim)
            session.commit()
