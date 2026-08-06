"""Stripe integration: one flat recurring Pro plan (no metered billing --
the free tier's 3-conversions/month cap is enforced entirely in `usage.py`
app-side; Stripe only needs to know "does this user have an active
subscription or not").
"""
from __future__ import annotations

import os

import stripe
from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import User

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def _require_configured() -> None:
    if not (STRIPE_SECRET_KEY and STRIPE_PRICE_ID):
        raise HTTPException(status_code=500, detail="Billing isn't configured on this server yet.")


def get_or_create_customer(db: Session, user: User) -> str:
    if user.stripe_customer_id:
        return user.stripe_customer_id
    customer = stripe.Customer.create(email=user.email, metadata={"user_id": str(user.id)})
    user.stripe_customer_id = customer.id
    db.add(user)
    db.commit()
    return customer.id


def create_checkout_session(db: Session, user: User) -> str:
    _require_configured()
    customer_id = get_or_create_customer(db, user)
    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=f"{APP_BASE_URL}/app.html?checkout=success",
        cancel_url=f"{APP_BASE_URL}/app.html?checkout=cancelled",
        client_reference_id=str(user.id),
    )
    return session.url


def create_portal_session(user: User) -> str:
    _require_configured()
    if not user.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No billing account yet. Subscribe first.")
    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{APP_BASE_URL}/app.html",
    )
    return session.url


def construct_event(payload: bytes, sig_header: str):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Webhook secret not configured.")
    try:
        return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(status_code=400, detail="Invalid webhook signature.") from exc


def sync_subscription_status(
    db: Session, customer_id: str, subscription_id: str | None, status: str | None
) -> None:
    user = db.query(User).filter(User.stripe_customer_id == customer_id).first()
    if not user:
        return
    user.stripe_subscription_id = subscription_id
    user.subscription_status = status
    db.add(user)
    db.commit()
