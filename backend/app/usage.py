"""Usage metering for the free tier's monthly conversion limit."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import UsageEvent, User

FREE_MONTHLY_LIMIT = 3


def _current_period_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def usage_this_period(db: Session, user: User) -> int:
    stmt = select(func.count()).select_from(UsageEvent).where(
        UsageEvent.user_id == user.id,
        UsageEvent.created_at >= _current_period_start(),
    )
    return db.execute(stmt).scalar_one()


def record_usage(db: Session, user: User, filename: str | None) -> None:
    db.add(UsageEvent(user_id=user.id, filename=filename))
    db.commit()


def enforce_limit_or_raise(db: Session, user: User) -> None:
    if user.is_pro:
        return
    used = usage_this_period(db, user)
    if used >= FREE_MONTHLY_LIMIT:
        raise HTTPException(
            status_code=402,
            detail=(
                f"You've used all {FREE_MONTHLY_LIMIT} free conversions this month. "
                "Upgrade to Pro for unlimited conversions."
            ),
        )
