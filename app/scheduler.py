from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.agent import reply_to_user
from app.db import SessionLocal
from app.messaging import send_message
from app.models import User


def due_checkins(db: Session) -> list[User]:
    now = datetime.now(timezone.utc)
    return (
        db.query(User)
        .filter(User.next_checkin_at.is_not(None), User.next_checkin_at <= now)
        .all()
    )


def run_due_checkins() -> int:
    db = SessionLocal()
    sent = 0
    try:
        for user in due_checkins(db):
            text = reply_to_user(
                db,
                user,
                inbound="[system] Time for a proactive check-in. Text them first.",
                proactive=True,
            )
            send_message(user.channel, user.phone, text)
            sent += 1
    finally:
        db.close()
    return sent
