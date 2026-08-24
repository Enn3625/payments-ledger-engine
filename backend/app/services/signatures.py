"""HMAC signature verification for inbound webhooks.

Modelled on the Stripe scheme (Razorpay is the same idea with a bare hex
digest and no timestamp):

    X-Webhook-Signature: t=1755511234,v1=<hex sha256 hmac>

and the signed payload is `"{t}.{raw_body}"`.

Three details do the actual security work:

* The HMAC is computed over the **raw request bytes**. Re-serialising the
  parsed JSON first would let an attacker vary bytes that survive a round trip
  (key order, whitespace, unicode escapes) while keeping the signature valid.
* Comparison is `hmac.compare_digest`, not `==`, so the check cannot be walked
  byte by byte with a timing oracle.
* The timestamp is inside the signed payload and must be recent. Without it, a
  payload captured off the wire stays replayable forever, because its signature
  never expires.
"""

import hashlib
import hmac
import time

SIGNATURE_HEADER = "X-Webhook-Signature"
SCHEME_VERSION = "v1"


class SignatureError(Exception):
    """Base class for signature verification failures."""


class MissingSignatureError(SignatureError):
    """No signature header was supplied."""


class MalformedSignatureError(SignatureError):
    """The header exists but is not in the expected format."""


class InvalidSignatureError(SignatureError):
    """The digest does not match the payload."""


class StaleSignatureError(SignatureError):
    """The signature is authentic but too old to accept."""


def sign_payload(raw_body: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a signature header value. Used by tests and the demo sender."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    digest = _digest(raw_body, secret, timestamp)
    return f"t={timestamp},{SCHEME_VERSION}={digest}"


def verify_signature(
    raw_body: bytes,
    header: str | None,
    secret: str,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> int:
    """Verify `header` against `raw_body`. Returns the signed timestamp."""
    if header is None or not header.strip():
        raise MissingSignatureError(f"{SIGNATURE_HEADER} header is required")

    timestamp, provided = _parse_header(header)
    expected = _digest(raw_body, secret, timestamp)

    if not hmac.compare_digest(expected, provided):
        raise InvalidSignatureError("signature does not match payload")

    now = int(time.time()) if now is None else now
    if abs(now - timestamp) > tolerance_seconds:
        raise StaleSignatureError(
            f"signature timestamp is outside the {tolerance_seconds}s tolerance"
        )

    return timestamp


def _parse_header(header: str) -> tuple[int, str]:
    parts: dict[str, str] = {}
    for chunk in header.split(","):
        name, separator, value = chunk.strip().partition("=")
        if not separator:
            raise MalformedSignatureError(f"malformed {SIGNATURE_HEADER} header")
        parts[name.strip()] = value.strip()

    if "t" not in parts or SCHEME_VERSION not in parts:
        raise MalformedSignatureError(
            f"{SIGNATURE_HEADER} must contain t and {SCHEME_VERSION} components"
        )

    try:
        timestamp = int(parts["t"])
    except ValueError as exc:
        raise MalformedSignatureError("signature timestamp is not an integer") from exc

    return timestamp, parts[SCHEME_VERSION]


def _digest(raw_body: bytes, secret: str, timestamp: int) -> str:
    signed_payload = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
