"""Webhook authentication: HMAC-SHA256 signature + timestamp (replay protection).

The sender signs `"{timestamp}.{raw_body}"` with a shared secret and sends:
    X-Timestamp: 1732100000
    X-Signature: sha256=<hex digest>

This is the same pattern Stripe, Slack and HubSpot (v3 signatures) use.
"""
from __future__ import annotations

import hashlib
import hmac
import time


class SignatureError(Exception):
    """Raised when a request cannot be proven to come from the trusted sender."""


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    timestamp: str | None,
    body: bytes,
    signature: str | None,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> None:
    if not timestamp or not signature:
        raise SignatureError("Missing X-Timestamp or X-Signature header")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("X-Timestamp must be a Unix epoch in seconds") from exc

    now = time.time() if now is None else now
    if abs(now - ts) > tolerance_seconds:
        # An attacker who captured an old request cannot replay it later.
        raise SignatureError("Timestamp outside the allowed window (possible replay)")

    expected = compute_signature(secret, timestamp, body)
    # compare_digest takes constant time, so timing does not leak how many characters matched.
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("Signature mismatch")
