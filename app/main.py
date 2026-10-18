from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, Response
from twilio.request_validator import RequestValidator

from app.agent import reply_to_user
from app.config import settings
from app.db import SessionLocal, init_db
from app.identity import default_next_checkin, parse_sender
from app.messaging import send_message, twilio_ready
from app.models import User
from app.scheduler import run_due_checkins

app = FastAPI(title="Nudge", version="0.1.0")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        run_due_checkins,
        "interval",
        seconds=settings.checkin_poll_seconds,
        id="checkins",
        replace_existing=True,
    )
    scheduler.start()
    app.state.scheduler = scheduler


def _get_or_create_user(phone: str, channel: str) -> User:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.phone == phone).one_or_none()
        if not user:
            user = User(
                phone=phone,
                channel=channel,
                next_checkin_at=default_next_checkin(),
            )
            db.add(user)
            db.flush()
        user.channel = channel
        user.last_inbound_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()


def _handle_inbound(from_raw: str, body: str) -> str:
    channel, phone = parse_sender(from_raw)
    user = _get_or_create_user(phone, channel)
    db = SessionLocal()
    try:
        # re-attach
        user = db.query(User).filter(User.id == user.id).one()
        reply = reply_to_user(db, user, body or "")
        send_message(user.channel, user.phone, reply)
        return reply
    finally:
        db.close()


def _valid_twilio(request: Request, form: dict) -> bool:
    if not settings.twilio_auth_token:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    if settings.public_base_url:
        url = settings.public_base_url.rstrip("/") + request.url.path
    validator = RequestValidator(settings.twilio_auth_token)
    return validator.validate(url, form, signature)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "twilio": twilio_ready()}


@app.post("/webhooks/twilio")
async def twilio_webhook(
    request: Request,
    background: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(""),
) -> Response:
    form = dict((await request.form()).items())
    if not _valid_twilio(request, form):
        return PlainTextResponse("invalid signature", status_code=403)
    background.add_task(_handle_inbound, From, Body)
    return Response(content="<Response></Response>", media_type="text/xml")


@app.post("/dev/message")
def dev_message(payload: dict) -> dict:
    """Local testing: {\"from\": \"+15551212\", \"body\": \"I want to run more\"}"""
    sender = payload.get("from") or payload.get("From") or "+10000000000"
    body = payload.get("body") or payload.get("Body") or ""
    reply = _handle_inbound(str(sender), str(body))
    return {"reply": reply}


@app.post("/dev/checkins")
def dev_checkins() -> dict:
    return {"sent": run_due_checkins()}
