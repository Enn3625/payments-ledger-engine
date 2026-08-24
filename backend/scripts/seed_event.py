"""Sign and send webhook events, the way the payment provider would.

Hand-signing an HMAC for every test is tedious and error-prone, so this does it
for you. It is also the tool the demo seed uses, which means the demo exercises
exactly the same signing path as production traffic.

Usage (from `backend/`, with the API running):

    python -m scripts.seed_event capture --intent <uuid>
    python -m scripts.seed_event capture --intent <uuid> --fee 2000
    python -m scripts.seed_event capture --intent <uuid> --tamper
    python -m scripts.seed_event fail --intent <uuid> --reason card_declined
    python -m scripts.seed_event burst --merchant <uuid> --count 6
    python -m scripts.seed_event raw --file event.json

Every command takes `--url` (default http://127.0.0.1:8000) and `--secret`
(default: WEBHOOK_SECRET from your settings).

The webhook route itself is public -- the signature is its authentication -- but
creating and reading intents is not, so `burst`, and `capture` without an
explicit `--amount`, need credentials:

    ... --email admin@demo.local --password <pw>      # logs in for you
    ... --token <jwt>                                 # or bring your own
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:  # allow both `-m scripts.seed_event` and a direct path
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings  # noqa: E402
from app.services.signatures import SIGNATURE_HEADER, sign_payload  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8000"
WEBHOOK_PATH = "/webhooks/payment-events"
INTENTS_PATH = "/payment-intents"
LOGIN_PATH = "/auth/login"


# --------------------------------------------------------------------------
# Event construction (pure -- no network, so it is testable on its own)
# --------------------------------------------------------------------------
def capture_event(
    intent_id: str,
    amount: int,
    currency: str = "INR",
    fee: int = 0,
    event_id: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "payment_intent_id": intent_id,
        "amount": amount,
        "currency": currency,
    }
    if fee:
        data["fee"] = fee
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex[:16]}",
        "type": "payment.captured",
        "data": data,
    }


def failure_event(
    intent_id: str, reason: str | None = None, event_id: str | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {"payment_intent_id": intent_id}
    if reason:
        data["reason"] = reason
    return {
        "id": event_id or f"evt_{uuid.uuid4().hex[:16]}",
        "type": "payment.failed",
        "data": data,
    }


def tamper(raw: bytes) -> bytes:
    """Inflate the amount *after* signing, to prove the signature catches it."""
    body = json.loads(raw)
    original = body.get("data", {}).get("amount", 0)
    body.setdefault("data", {})["amount"] = original * 100 + 1
    return json.dumps(body).encode()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _request(
    url: str,
    raw: bytes | None = None,
    headers: dict[str, str] | None = None,
    *,
    method: str = "POST",
    content_type: str = "application/json",
) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=raw, method=method)
    if raw is not None:
        request.add_header("Content-Type", content_type)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        payload = error.read()
        try:
            return error.code, json.loads(payload or b"null")
        except json.JSONDecodeError:
            return error.code, payload.decode(errors="replace")


def _auth(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def login(base_url: str, email: str, password: str) -> str:
    """Exchange credentials for a bearer token, the way the dashboard does."""
    form = urllib.parse.urlencode({"username": email, "password": password}).encode()
    status, body = _request(
        base_url + LOGIN_PATH,
        form,
        content_type="application/x-www-form-urlencoded",
    )
    if status != 200:
        raise SystemExit(f"login failed for {email}: HTTP {status} {body}")
    return body["access_token"]


def send_event(
    base_url: str, body: dict[str, Any], secret: str, *, mangle: bool = False
) -> tuple[int, Any]:
    """Sign the exact bytes, then (optionally) change them before sending."""
    raw = json.dumps(body).encode()
    signature = sign_payload(raw, secret)
    if mangle:
        raw = tamper(raw)
    return _request(base_url + WEBHOOK_PATH, raw, {SIGNATURE_HEADER: signature})


def create_intent(
    base_url: str, merchant_id: str, amount: int, reference: str, token: str | None
) -> tuple[int, Any]:
    """Creating an intent is an admin write, so this needs a token."""
    body = {
        "amount": amount,
        "merchant_account_id": merchant_id,
        "reference": reference,
    }
    return _request(
        base_url + INTENTS_PATH,
        json.dumps(body).encode(),
        {"Idempotency-Key": f"seed-{uuid.uuid4().hex[:12]}"} | _auth(token),
    )


def fetch_intent(base_url: str, intent_id: str, token: str | None) -> dict[str, Any]:
    status, body = _request(
        f"{base_url}{INTENTS_PATH}/{intent_id}", headers=_auth(token), method="GET"
    )
    if status == 401:
        raise SystemExit(
            "reading an intent requires authentication: pass --token, or "
            "--email/--password to log in. Alternatively pass --amount explicitly."
        )
    if status != 200:
        raise SystemExit(f"could not read intent {intent_id}: HTTP {status} {body}")
    return body


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def _report(label: str, status: int, body: Any) -> None:
    print(f"{label}: HTTP {status}")
    print(json.dumps(body, indent=2) if not isinstance(body, str) else body)


def cmd_capture(args: argparse.Namespace) -> int:
    amount = args.amount
    currency = args.currency
    if amount is None:
        # Default to the intent's own amount, since a mismatch is refused.
        intent = fetch_intent(args.url, args.intent, args.token)
        amount, currency = intent["amount"], intent["currency"]

    body = capture_event(args.intent, amount, currency, args.fee, args.event_id)
    status, response = send_event(args.url, body, args.secret, mangle=args.tamper)
    _report("capture" + (" (tampered)" if args.tamper else ""), status, response)
    return 0 if status < 400 else 1


def cmd_fail(args: argparse.Namespace) -> int:
    body = failure_event(args.intent, args.reason, args.event_id)
    status, response = send_event(args.url, body, args.secret)
    _report("failure", status, response)
    return 0 if status < 400 else 1


def cmd_burst(args: argparse.Namespace) -> int:
    """Create and capture N intents against one merchant, to trip the velocity rule."""
    failures = 0
    for n in range(args.count):
        status, intent = create_intent(
            args.url, args.merchant, args.amount + n, f"order_burst_{n}", args.token
        )
        if status >= 400:
            _report(f"intent {n + 1}/{args.count}", status, intent)
            failures += 1
            continue

        body = capture_event(intent["id"], intent["amount"], intent["currency"])
        status, response = send_event(args.url, body, args.secret)
        marker = "ok" if status < 400 else "FAILED"
        print(f"  {n + 1}/{args.count} {marker} HTTP {status} intent={intent['id']}")
        if status >= 400:
            print(json.dumps(response, indent=2))
            failures += 1

    print(f"\n{args.count - failures}/{args.count} captured. Check {args.url}/anomaly-flags")
    return 0 if failures == 0 else 1


def cmd_raw(args: argparse.Namespace) -> int:
    """Sign and send a hand-written body, for poking at the edges."""
    raw = Path(args.file).read_bytes()
    signature = sign_payload(raw, args.secret)
    status, response = _request(args.url + WEBHOOK_PATH, raw, {SIGNATURE_HEADER: signature})
    _report(f"raw {args.file}", status, response)
    return 0 if status < 400 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_event",
        description="Sign and send webhook events against a running API.",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"API base URL (default {DEFAULT_URL})")
    parser.add_argument(
        "--secret",
        default=None,
        help="signing secret (default: WEBHOOK_SECRET from settings)",
    )
    parser.add_argument("--token", help="bearer token for the admin-only API calls")
    parser.add_argument("--email", help="log in with these credentials instead of --token")
    parser.add_argument("--password", dest="login_password", help="password for --email")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="send a payment.captured event")
    capture.add_argument("--intent", required=True, help="payment intent id")
    capture.add_argument("--amount", type=int, help="minor units (default: the intent amount)")
    capture.add_argument("--currency", default="INR")
    capture.add_argument("--fee", type=int, default=0, help="platform fee in minor units")
    capture.add_argument("--event-id", help="reuse an id to test redelivery")
    capture.add_argument(
        "--tamper",
        action="store_true",
        help="change the body after signing; the API must answer 401",
    )
    capture.set_defaults(handler=cmd_capture)

    fail = subparsers.add_parser("fail", help="send a payment.failed event")
    fail.add_argument("--intent", required=True)
    fail.add_argument("--reason", default="card_declined")
    fail.add_argument("--event-id")
    fail.set_defaults(handler=cmd_fail)

    burst = subparsers.add_parser("burst", help="create and capture N intents (velocity rule)")
    burst.add_argument("--merchant", required=True, help="merchant account id")
    burst.add_argument("--count", type=int, default=6)
    burst.add_argument("--amount", type=int, default=1_000, help="minor units per capture")
    burst.set_defaults(handler=cmd_burst)

    raw = subparsers.add_parser("raw", help="sign and send a JSON file verbatim")
    raw.add_argument("--file", required=True)
    raw.set_defaults(handler=cmd_raw)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.secret is None:
        args.secret = get_settings().webhook_secret

    try:
        if args.token is None and args.email:
            args.token = login(args.url, args.email, args.login_password or "")
        return args.handler(args)
    except urllib.error.HTTPError as error:
        # HTTPError subclasses URLError, so it must be caught first: otherwise a
        # 401 gets reported as "cannot reach the server", which is simply false.
        print(f"HTTP {error.code} from {args.url}: {error.reason}", file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print(f"cannot reach {args.url}: {error.reason}", file=sys.stderr)
        print("start the API with:  python -m uvicorn app.main:app --reload", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
