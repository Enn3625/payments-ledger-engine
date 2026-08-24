"""HMAC verification, in isolation from the database."""

import time

import pytest

from app.services.signatures import (
    InvalidSignatureError,
    MalformedSignatureError,
    MissingSignatureError,
    StaleSignatureError,
    sign_payload,
    verify_signature,
)

SECRET = "whsec_test_secret"
BODY = b'{"id":"evt_1","type":"payment.captured","data":{"amount":1000}}'


class TestAcceptance:
    def test_a_correctly_signed_payload_verifies(self):
        header = sign_payload(BODY, SECRET)

        assert verify_signature(BODY, header, SECRET) > 0

    def test_whitespace_around_components_is_tolerated(self):
        timestamp = int(time.time())
        header = sign_payload(BODY, SECRET, timestamp)
        spaced = header.replace(",", " , ")

        assert verify_signature(BODY, spaced, SECRET) == timestamp


class TestRejection:
    def test_missing_header(self):
        with pytest.raises(MissingSignatureError):
            verify_signature(BODY, None, SECRET)

    @pytest.mark.parametrize(
        "header",
        [
            "",
            "   ",
            "nonsense",
            "t=abc,v1=deadbeef",
            "v1=deadbeef",
            "t=1755511234",
        ],
    )
    def test_malformed_headers(self, header):
        with pytest.raises((MissingSignatureError, MalformedSignatureError)):
            verify_signature(BODY, header, SECRET)

    def test_wrong_secret(self):
        header = sign_payload(BODY, "some_other_secret")

        with pytest.raises(InvalidSignatureError):
            verify_signature(BODY, header, SECRET)

    def test_tampered_body(self):
        header = sign_payload(BODY, SECRET)
        tampered = BODY.replace(b'"amount":1000', b'"amount":100000')

        with pytest.raises(InvalidSignatureError):
            verify_signature(tampered, header, SECRET)

    def test_byte_identical_payloads_are_required(self):
        """Re-serialising the JSON breaks the signature, as it must."""
        header = sign_payload(BODY, SECRET)
        reformatted = BODY.replace(b",", b", ")

        with pytest.raises(InvalidSignatureError):
            verify_signature(reformatted, header, SECRET)

    def test_timestamp_is_covered_by_the_digest(self):
        """Moving the timestamp forward invalidates an otherwise valid digest."""
        timestamp = int(time.time())
        header = sign_payload(BODY, SECRET, timestamp)
        shifted = header.replace(f"t={timestamp}", f"t={timestamp + 1}")

        with pytest.raises(InvalidSignatureError):
            verify_signature(BODY, shifted, SECRET)

    def test_an_old_but_authentic_signature_is_stale(self):
        """A payload captured off the wire must not stay replayable forever."""
        now = int(time.time())
        header = sign_payload(BODY, SECRET, now - 3_600)

        with pytest.raises(StaleSignatureError):
            verify_signature(BODY, header, SECRET, tolerance_seconds=300, now=now)

    def test_a_signature_from_the_far_future_is_stale(self):
        now = int(time.time())
        header = sign_payload(BODY, SECRET, now + 3_600)

        with pytest.raises(StaleSignatureError):
            verify_signature(BODY, header, SECRET, tolerance_seconds=300, now=now)

    def test_a_signature_inside_the_tolerance_is_accepted(self):
        now = int(time.time())
        header = sign_payload(BODY, SECRET, now - 299)

        assert verify_signature(BODY, header, SECRET, tolerance_seconds=300, now=now)
