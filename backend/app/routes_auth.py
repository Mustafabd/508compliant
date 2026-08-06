from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import auth
from .db import get_db
from .models import User
from .usage import FREE_MONTHLY_LIMIT, usage_this_period

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SignupBody(BaseModel):
    email: str
    password: str


class LoginBody(BaseModel):
    email: str
    password: str


def _user_payload(db: Session, user: User) -> dict:
    return {
        "email": user.email,
        "is_pro": user.is_pro,
        "subscription_status": user.subscription_status,
        "usage_this_month": usage_this_period(db, user),
        "free_monthly_limit": FREE_MONTHLY_LIMIT,
    }


@router.post("/signup")
def signup(body: SignupBody, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    error = auth.validate_signup(email, body.password)
    if error:
        raise HTTPException(status_code=400, detail=error)

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    user = User(email=email, password_hash=auth.hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    auth.set_session_cookie(response, user.id)
    return _user_payload(db, user)


@router.post("/login")
def login(body: LoginBody, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()

    if auth.is_rate_limited(email):
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again in a few minutes.")

    user = db.query(User).filter(User.email == email).first()
    if not auth.verify_password_or_dummy(body.password, user.password_hash if user else None):
        auth.record_failed_attempt(email)
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    auth.clear_failed_attempts(email)
    auth.set_session_cookie(response, user.id)
    return _user_payload(db, user)


@router.post("/logout")
def logout(response: Response):
    auth.clear_session_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    return _user_payload(db, user)
