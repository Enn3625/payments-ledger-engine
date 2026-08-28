"""Measure end-to-end webhook processing latency.

The README quotes a number; this is how it is produced, so anyone can re-run it
and disagree with me.

What is being timed is the whole path a provider experiences: HTTP request in,
HMAC verified over the raw bytes, event recorded, intent locked, balanced
transaction posted, anomaly rules evaluated, single COMMIT, response out.

    python -m scripts.benchmark_webhooks --email admin@ledger.demo --password <pw> \
        --merchant <account-id> --count 50

Latency is reported as percentiles, not a mean. A mean hides the tail, and the
tail is what a provider's retry logic actually reacts to.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings  # noqa: E402
from app.services.signatures import SIGNATURE_HEADER, sign_payload  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8000"


def request(url: str, data: bytes | None, headers: dict[str, str], method: str = "POST"):
    req = urllib.request.Request(url, data=data, method=method)
    for name, value in headers.items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def login(base_url: str, email: str, password: str) -> str:
    form = urllib.parse.urlencode({"username": email, "password": password}).encode()
    status, body = request(
        base_url + "/auth/login", form, {"Content-Type": "application/x-www-form-urlencoded"}
    )
    if status != 200:
        raise SystemExit(f"login failed: HTTP {status} {body}")
    return body["access_token"]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark_webhooks")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--merchant", required=True, help="merchant account id")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=5, help="untimed requests first")
    args = parser.parse_args(argv)

    secret = get_settings().webhook_secret
    token = login(args.url, args.email, args.password)
    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    total = args.count + args.warmup
    print(f"creating {total} intents…")
    intents: list[tuple[str, int]] = []
    for index in range(total):
        amount = 1_000 + index
        status, body = request(
            args.url + "/payment-intents",
            json.dumps(
                {
                    "amount": amount,
                    "merchant_account_id": args.merchant,
                    "reference": f"bench_{uuid.uuid4().hex[:10]}",
                }
            ).encode(),
            {**auth, "Idempotency-Key": f"bench-{uuid.uuid4().hex[:12]}"},
        )
        if status != 201:
            raise SystemExit(f"could not create intent: HTTP {status} {body}")
        intents.append((body["id"], amount))

    print(f"delivering {total} signed captures ({args.warmup} warm-up, {args.count} timed)…")
    timings: list[float] = []
    failures = 0
    for index, (intent_id, amount) in enumerate(intents):
        payload = {
            "id": f"evt_bench_{uuid.uuid4().hex[:12]}",
            "type": "payment.captured",
            "data": {"payment_intent_id": intent_id, "amount": amount, "currency": "INR"},
        }
        raw = json.dumps(payload).encode()
        headers = {
            SIGNATURE_HEADER: sign_payload(raw, secret),
            "Content-Type": "application/json",
        }

        started = time.perf_counter()
        status, _ = request(args.url + "/webhooks/payment-events", raw, headers)
        elapsed_ms = (time.perf_counter() - started) * 1000

        if status != 200:
            failures += 1
        elif index >= args.warmup:
            timings.append(elapsed_ms)

    if not timings:
        raise SystemExit("no successful timed deliveries")

    print("\n--- webhook processing latency (ms) ---")
    print(f"  samples   {len(timings)}")
    print(f"  p50       {percentile(timings, 0.50):.1f}")
    print(f"  p95       {percentile(timings, 0.95):.1f}")
    print(f"  p99       {percentile(timings, 0.99):.1f}")
    print(f"  max       {max(timings):.1f}")
    print(f"  mean      {statistics.mean(timings):.1f}")
    if failures:
        print(f"  failures  {failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
