from datetime import datetime, timezone

from app.agent import reply_to_user
from app.db import SessionLocal, init_db
from app.identity import default_next_checkin, parse_sender
from app.models import User


def main() -> None:
    init_db()
    raw = input("Your number (or whatsapp:+1...): ").strip() or "+10000000000"
    channel, phone = parse_sender(raw)
    db = SessionLocal()
    user = db.query(User).filter(User.phone == phone).one_or_none()
    if not user:
        user = User(phone=phone, channel=channel, next_checkin_at=default_next_checkin())
        db.add(user)
        db.commit()
        db.refresh(user)
    print("Nudge is in this terminal. Empty line to quit.\n")
    while True:
        try:
            body = input("you> ").strip()
        except EOFError:
            break
        if not body:
            break
        user.last_inbound_at = datetime.now(timezone.utc)
        db.commit()
        try:
            reply = reply_to_user(db, user, body)
        except Exception as exc:
            db.rollback()
            reply = f"Something went wrong on this side: {exc}"
        print(f"nudge> {reply}\n")


if __name__ == "__main__":
    main()
