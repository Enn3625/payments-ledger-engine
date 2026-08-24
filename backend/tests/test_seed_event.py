"""The event sender builds bodies and signatures correctly, without a network."""

import json

import pytest

from app.services.signatures import verify_signature
from scripts.seed_event import _auth, build_parser, capture_event, failure_event, tamper

SECRET = "whsec_test_secret"


class TestEventConstruction:
    def test_capture_event_shape(self):
        event = capture_event("11111111-1111-1111-1111-111111111111", 150_000)

        assert event["type"] == "payment.captured"
        assert event["id"].startswith("evt_")
        assert event["data"]["amount"] == 150_000
        assert event["data"]["currency"] == "INR"
        assert "fee" not in event["data"]  # omitted rather than sent as zero

    def test_fee_is_included_when_set(self):
        event = capture_event("11111111-1111-1111-1111-111111111111", 150_000, fee=2_000)

        assert event["data"]["fee"] == 2_000

    def test_event_ids_are_unique_by_default(self):
        first = capture_event("11111111-1111-1111-1111-111111111111", 1)
        second = capture_event("11111111-1111-1111-1111-111111111111", 1)

        assert first["id"] != second["id"]

    def test_an_explicit_event_id_is_kept(self):
        """Reusing an id is how you test redelivery."""
        event = capture_event("11111111-1111-1111-1111-111111111111", 1, event_id="evt_fixed")

        assert event["id"] == "evt_fixed"

    def test_failure_event_shape(self):
        event = failure_event("11111111-1111-1111-1111-111111111111", reason="card_declined")

        assert event["type"] == "payment.failed"
        assert event["data"]["reason"] == "card_declined"


class TestSigning:
    def test_a_generated_event_verifies(self):
        raw = json.dumps(capture_event("11111111-1111-1111-1111-111111111111", 1_000)).encode()
        from app.services.signatures import sign_payload

        assert verify_signature(raw, sign_payload(raw, SECRET), SECRET) > 0

    def test_tampering_changes_the_bytes(self):
        """--tamper must actually alter the payload, or the demo proves nothing."""
        raw = json.dumps(capture_event("11111111-1111-1111-1111-111111111111", 1_000)).encode()

        mangled = tamper(raw)

        assert mangled != raw
        assert json.loads(mangled)["data"]["amount"] > 1_000

    def test_a_tampered_body_fails_verification(self):
        from app.services.signatures import InvalidSignatureError, sign_payload

        raw = json.dumps(capture_event("11111111-1111-1111-1111-111111111111", 1_000)).encode()
        signature = sign_payload(raw, SECRET)

        with pytest.raises(InvalidSignatureError):
            verify_signature(tamper(raw), signature, SECRET)


class TestAuthPlumbing:
    """Creating and reading intents needs a token now that writes are admin-only."""

    def test_auth_header_is_built_when_a_token_is_present(self):
        assert _auth("abc") == {"Authorization": "Bearer abc"}

    def test_no_header_without_a_token(self):
        assert _auth(None) == {}

    def test_credentials_options_are_accepted(self):
        args = build_parser().parse_args(
            ["--email", "admin@demo.local", "--password", "pw", "capture", "--intent", "x"]
        )

        assert args.email == "admin@demo.local"
        assert args.login_password == "pw"
        assert args.token is None


class TestCommandLine:
    def test_capture_requires_an_intent(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["capture"])

    def test_a_command_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    def test_burst_defaults_trip_the_velocity_rule(self):
        """Six is one more than the default limit of five."""
        args = build_parser().parse_args(["burst", "--merchant", "abc"])

        assert args.count == 6
