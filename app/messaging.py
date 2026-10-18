from twilio.rest import Client

from app.config import settings
from app.identity import outbound_address


def twilio_ready() -> bool:
    return bool(
        settings.twilio_account_sid
        and settings.twilio_auth_token
        and settings.twilio_phone_number
    )


def send_message(channel: str, phone: str, body: str) -> str | None:
    if not twilio_ready():
        print(f"[dev send {channel} {phone}] {body}")
        return None
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    from_number = settings.twilio_phone_number
    if channel == "whatsapp" and not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"
    msg = client.messages.create(
        body=body,
        from_=from_number,
        to=outbound_address(channel, phone),
    )
    return msg.sid
