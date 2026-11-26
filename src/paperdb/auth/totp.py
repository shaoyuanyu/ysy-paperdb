"""RFC 6238 TOTP verification for destructive operations.

Mirrors the site login's parameters (SHA1, 6 digits, 30-second steps) so the
owner reuses the same authenticator entry. The secret is shared via
PAPERDB_TOTP_SECRET; personal tokens (agent-facing) must present a current
code for destructive endpoints, while the service token (server-to-server
web proxy, never exposed to the model) bypasses the gate.

Fail-closed: when the secret is not configured, personal-token destructive
calls are rejected instead of allowed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

STEP = 30
DIGITS = 6
WINDOW = 1  # accept the previous/next step to tolerate clock skew

# Simple in-process failure lock (single-user deployment, one API process).
_MAX_FAILURES = 5
_LOCK_SECONDS = 15 * 60
_failures: list[float] = []


def _decode_secret(secret: str) -> bytes:
    normalized = secret.strip().replace(" ", "").upper()
    padding = "=" * (-len(normalized) % 8)
    return base64.b32decode(normalized + padding, casefold=True)


def _code_at(secret: bytes, counter: int) -> str:
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % (10**DIGITS):0{DIGITS}d}"


def _prune_failures(now: float) -> None:
    _failures[:] = [failed for failed in _failures if now - failed < _LOCK_SECONDS]


def reset_failures() -> None:
    _failures.clear()


def verify_totp(secret: str, code: str, *, now: float | None = None) -> bool:
    """Check a 6-digit code with a ±1 step window. Enforces a failure lock:
    after 5 consecutive failures, verification rejects everything for 15
    minutes (mirrors the site login's limiter)."""
    current = time.time() if now is None else now
    _prune_failures(current)
    if len(_failures) >= _MAX_FAILURES:
        return False
    normalized = (code or "").strip()
    if len(normalized) == DIGITS and normalized.isdigit():
        counter = int(current // STEP)
        key = _decode_secret(secret)
        for offset in range(-WINDOW, WINDOW + 1):
            if hmac.compare_digest(normalized, _code_at(key, counter + offset)):
                reset_failures()
                return True
    _failures.append(current)
    return False


def current_totp(secret: str, *, now: float | None = None) -> str:
    """Current-step code. Used by tests to generate valid codes; must never
    be exposed on request paths."""
    current = time.time() if now is None else now
    return _code_at(_decode_secret(secret), int(current // STEP))


def totp_at(secret: str, counter: int) -> str:
    """Deterministic code at an explicit counter (unit-test vector helper)."""
    return _code_at(_decode_secret(secret), counter)


__all__ = ["current_totp", "reset_failures", "totp_at", "verify_totp"]
