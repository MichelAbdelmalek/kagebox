"""Passwords, session and CSRF tokens, and signed short-lived media URLs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_ph = PasswordHasher()  # argon2id with library defaults (RFC 9106 low-memory profile)
_DUMMY_HASH = _ph.hash(secrets.token_urlsafe(16))

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _ph.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Constant work whether or not the user exists."""
    try:
        return _ph.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_media(
    key: bytes, ref: str, user_id: str, ttl_seconds: int, now: float | None = None
) -> str:
    """Token bound to one object, one user and an expiry (at most 5 minutes)."""
    if not 1 <= ttl_seconds <= 300:
        raise ValueError("ttl must be between 1 and 300 seconds")
    exp = int((now if now is not None else time.time()) + ttl_seconds)
    payload = _b64(json.dumps({"r": ref, "u": user_id, "e": exp}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


class BadMediaToken(ValueError):
    pass


def verify_media(key: bytes, token: str, user_id: str, now: float | None = None) -> str:
    try:
        payload, sig = token.split(".", 1)
        expected = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig):
            raise BadMediaToken("bad signature")
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise BadMediaToken("malformed token") from exc
    if data.get("u") != user_id:
        raise BadMediaToken("token belongs to another user")
    if int(data["e"]) < (now if now is not None else time.time()):
        raise BadMediaToken("token expired")
    return str(data["r"])
