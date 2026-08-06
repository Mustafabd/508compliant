from __future__ import annotations

import logging

import stripe
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from . import billing
from .auth import get_current_user
from .db import get_db
from .models import User

logger = logging.getLogger("uvicorn.error")

router = APIRouter(prefix="/api/billing", tags=["billing"])
webhook_router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/checkout")
def checkout(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"url": billing.create_checkout_session(db, user)}


@router.post("/portal")
def portal(user: User = Depends(get_current_user)):
    return {"url": billing.create_portal_session(user)}


@webhook_router.post("/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    event = billing.construct_event(payload, sig_header)

    data = event["data"]["object"]
    event_type = event["type"]

    if event_type == "checkout.session.completed":
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        if customer_id and subscription_id:
            sub = stripe.Subscription.retrieve(subscription_id)
            billing.sync_subscription_status(db, customer_id, subscription_id, sub.status)
    elif event_type == "customer.subscription.updated":
        customer_id = data.get("customer")
        if customer_id:
            billing.sync_subscription_status(db, customer_id, data.get("id"), data.get("status"))
    elif event_type == "customer.subscription.deleted":
        customer_id = data.get("customer")
        if customer_id:
            billing.sync_subscription_status(db, customer_id, data.get("id"), "canceled")
    else:
        logger.info("Unhandled Stripe webhook event type: %s", event_type)

    return {"received": True}
