"""Password hashing, signed session cookies, and login-attempt throttling.

Deliberately dependency-light (no Redis/session store): sessions are a
signed, httpOnly cookie holding the user id, verified on each request.
This is a single-process design -- fine for one Render/Railway instance,
but login-attempt throttling (in-memory) and sessions won't be shared if
you ever scale to multiple instances. Swap in Redis for both if you do.
"""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict

import bcrypt
from fastapi import Cookie, Depends, HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .db import get_db
from .models import User

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if os.environ.get("ENVIRONMENT") == "production":
        raise RuntimeError("SECRET_KEY must be set in production (used to sign session cookies).")
    SECRET_KEY = "dev-only-insecure-secret-key-do-not-use-in-production"

COOKIE_NAME = "session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days
IS_PRODUCTION = os.environ.get("ENVIRONMENT") == "production"

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="user-session")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8

# Naive in-memory throttle: email -> list of failed-attempt timestamps.
_failed_attempts: dict[str, list[float]] = defaultdict(list)
MAX_FAILED_ATTEMPTS = 8
FAILED_ATTEMPT_WINDOW_SECONDS = 15 * 60


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


# A fixed, valid bcrypt hash with no corresponding real password, used to
# keep failed-login timing consistent whether or not the email exists (see
# verify_password_or_dummy below).
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt()).decode("utf-8")


def verify_password_or_dummy(password: str, password_hash: str | None) -> bool:
    """Like verify_password, but always does bcrypt work even when
    password_hash is None (unknown user) so response timing doesn't reveal
    whether an email is registered."""
    return verify_password(password, password_hash or _DUMMY_HASH) and password_hash is not None


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def validate_signup(email: str, password: str) -> str | None:
    if not email or not EMAIL_RE.match(email):
        return "Please enter a valid email address."
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


def is_rate_limited(email: str) -> bool:
    now = time.time()
    attempts = [t for t in _failed_attempts[email] if now - t < FAILED_ATTEMPT_WINDOW_SECONDS]
    _failed_attempts[email] = attempts
    return len(attempts) >= MAX_FAILED_ATTEMPTS


def record_failed_attempt(email: str) -> None:
    _failed_attempts[email].append(time.time())


def clear_failed_attempts(email: str) -> None:
    _failed_attempts.pop(email, None)


def create_session_cookie_value(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def _read_session_uid(token: str | None) -> int | None:
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, int) else None


def get_current_user(
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User:
    uid = _read_session_uid(session)
    if uid is None:
        raise HTTPException(status_code=401, detail="Not logged in.")
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return user


def get_current_user_optional(
    session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> User | None:
    uid = _read_session_uid(session)
    if uid is None:
        return None
    return db.get(User, uid)


def set_session_cookie(response, user_id: int) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=create_session_cookie_value(user_id),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=IS_PRODUCTION,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")
